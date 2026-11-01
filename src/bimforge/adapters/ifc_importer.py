"""IFC importer based on IfcOpenShell (LGPL-3).

Strategy: tessellate every product once (world coordinates, metres), then
* rebuild walls, slabs, spaces, doors and windows as parametric elements when
  their geometry fits the parametric model (straight walls, flat slabs without
  holes, openings that fit their host), and
* keep everything else as ImportedElement meshes, so nothing is lost visually
  and it still takes part in coordination and IFC export.
Original GlobalIds are kept in element.properties["ifc_guid"].
"""

from __future__ import annotations

import importlib.util
import math
import multiprocessing
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from shapely.geometry import Polygon

from bimforge.core.model import (
    Door,
    Element,
    ImportedElement,
    Opening,
    Project,
    Slab,
    Space,
    Storey,
    Wall,
    Window,
)
from bimforge.core.validation import validate
from bimforge.plugins.sdk import Importer, ImportResult, PluginInfo

MeshData = tuple[np.ndarray, np.ndarray]  # vertices (N, 3), faces (M, 3)

SKIP_CLASSES = ("IfcOpeningElement", "IfcSpatialElement", "IfcAnnotation", "IfcGrid", "IfcVirtualElement", "IfcPort")
PLANE_TOLERANCE = 0.01
DEFAULT_STOREY_HEIGHT = 3.0

_CATEGORY_RULES = [
    ("structure", re.compile(r"Column|Beam|Member|Plate|Footing|Pile|Reinforc|Tendon", re.I)),
    ("plumbing", re.compile(r"Pipe|Sanitary|Valve|Tank|Pump|WasteTerminal|FireSuppression|Boiler", re.I)),
    ("electrical", re.compile(r"Cable|Electric|Light|Outlet|Switch|Protective|Lamp|Transformer|Junction|Alarm", re.I)),
    ("mechanical", re.compile(r"Duct|AirTerminal|Fan|Damper|Coil|Chiller|UnitaryEquipment|Flow|Distribution", re.I)),
]


def category_for(ifc_class: str) -> str:
    for category, pattern in _CATEGORY_RULES:
        if pattern.search(ifc_class):
            return category
    return "architecture"


def footprint(vertices: np.ndarray, faces: np.ndarray) -> Polygon | shapely.MultiPolygon | None:
    """Plan outline of a mesh: union of its triangles projected to XY."""
    triangles = [Polygon(vertices[f, :2]) for f in faces]
    triangles = [t for t in triangles if t.is_valid and t.area > 1e-9]
    if not triangles:
        return None
    return shapely.unary_union(triangles, grid_size=1e-5).buffer(0)


def _is_two_level(z: np.ndarray) -> bool:
    lo, hi = z.min(), z.max()
    return bool(np.all((np.abs(z - lo) <= PLANE_TOLERANCE) | (np.abs(z - hi) <= PLANE_TOLERANCE)))


def _ring(polygon: Polygon) -> list[tuple[float, float]]:
    return [(round(float(x), 6), round(float(y), 6)) for x, y in list(polygon.exterior.coords)[:-1]]


class IfcImporter(Importer):
    info = PluginInfo(id="bimforge.import.ifc", name="IFC (IfcOpenShell)", version="0.1.0", license="LGPL-3.0")
    extensions = (".ifc",)

    @classmethod
    def available(cls) -> bool:
        return importlib.util.find_spec("ifcopenshell") is not None

    def import_file(self, path: Path, options: Mapping[str, Any] | None = None) -> ImportResult:
        import ifcopenshell

        try:
            f = ifcopenshell.open(str(path))
        except Exception as exc:  # noqa: BLE001 - ifcopenshell raises its own error types
            raise ValueError(f"Cannot read IFC '{path}': {exc}") from exc
        return _Reader(f, Path(path)).read()


class _Reader:
    def __init__(self, f: Any, path: Path) -> None:
        import ifcopenshell.util.unit

        self.f = f
        self.path = path
        self.scale = ifcopenshell.util.unit.calculate_unit_scale(f)
        self.warnings: list[str] = []
        self.fallbacks: Counter[str] = Counter()
        self.project = Project(path.stem)
        self.storeys: dict[int, Storey] = {}  # IFC entity id -> Storey
        self.walls: dict[int, Wall] = {}  # IFC entity id -> Wall

    # --- top level ---------------------------------------------------------------

    def read(self) -> ImportResult:
        ifc_projects = self.f.by_type("IfcProject")
        if ifc_projects and ifc_projects[0].Name:
            self.project.name = ifc_projects[0].Name
        buildings = self.f.by_type("IfcBuilding")
        if buildings and buildings[0].Name:
            self.project.building.name = buildings[0].Name
        self._read_storeys()
        meshes = self._tessellate()

        products = [p for p in self.f.by_type("IfcProduct") if not any(p.is_a(c) for c in SKIP_CLASSES)]
        fillers = [p for p in products if p.is_a("IfcDoor") or p.is_a("IfcWindow")]
        filler_ids = {p.id() for p in fillers}
        wall_products = {}
        for product in products:
            if product.id() in filler_ids or product.id() not in meshes:
                continue
            mesh = meshes[product.id()]
            if product.is_a("IfcWall"):
                element = self._wall(product, mesh)
                if element is not None:
                    wall_products[product.id()] = product
            elif product.is_a("IfcSlab"):
                element = self._slabs(product, mesh)
            else:
                element = None
            if element is None:
                self._add(self._imported(product, mesh))
        filler_of: dict[str, Any] = {}  # opening element id -> IFC filler
        for product in fillers:
            if product.id() in meshes:
                element = self._filler(product, meshes[product.id()])
                if element is None:
                    self._add(self._imported(product, meshes[product.id()]))
                else:
                    filler_of[element.id] = product
        self._revert_unfilled_walls(wall_products, filler_of, meshes)
        for space in self.f.by_type("IfcSpace"):
            self._space(space)

        if self.fallbacks:
            summary = ", ".join(f"{n} {c}" for c, n in self.fallbacks.most_common())
            self.warnings.append(f"Imported as reference geometry (not editable parametrically): {summary}")
        return ImportResult(project=self.project, warnings=self.warnings)

    def _add(self, element: Element) -> None:
        self.project.add(element)

    def _revert_unfilled_walls(self, wall_products: dict[int, Any], filler_of: dict[str, Any],
                               meshes: dict[int, MeshData]) -> None:
        """A parametric wall is a solid box: keep it only if every IFC void in it became an Opening.

        Otherwise (empty voids, or fillers that did not fit) the exact mesh is kept, so holes are not lost.
        """
        for ifc_id, product in wall_products.items():
            wall = self.walls[ifc_id]
            voids = len(getattr(product, "HasOpenings", ()) or ())
            hosted = self.project.hosted_openings(wall.id)
            if voids <= len(hosted):
                continue
            for opening in hosted:
                self.project.remove(opening.id)
                filler = filler_of.get(opening.id)
                if filler is not None and filler.id() in meshes:
                    self._add(self._imported(filler, meshes[filler.id()]))
            self.project.remove(wall.id)
            del self.walls[ifc_id]
            self._add(self._imported(product, meshes[ifc_id]))

    # --- spatial structure -------------------------------------------------------

    def _read_storeys(self) -> None:
        import ifcopenshell.util.placement

        found = []
        for ifc_storey in self.f.by_type("IfcBuildingStorey"):
            if ifc_storey.ObjectPlacement is not None:
                elevation = float(ifcopenshell.util.placement.get_local_placement(ifc_storey.ObjectPlacement)[2, 3])
            else:
                elevation = float(ifc_storey.Elevation or 0.0)
            found.append((elevation * self.scale, ifc_storey))
        if not found:
            self.warnings.append("No storeys in file; everything was placed on Level 0")
            return
        found.sort(key=lambda t: t[0])
        storeys = []
        for i, (elevation, ifc_storey) in enumerate(found):
            above = found[i + 1][0] if i + 1 < len(found) else None
            height = above - elevation if above is not None and above - elevation > 0.1 else DEFAULT_STOREY_HEIGHT
            storey = Storey(name=ifc_storey.Name or f"Level {i}", elevation=round(elevation, 6), height=round(height, 6))
            storeys.append(storey)
            self.storeys[ifc_storey.id()] = storey
        self.project.building.storeys[:] = storeys

    def _storey_for(self, product: Any, z_min: float | None = None) -> Storey:
        import ifcopenshell.util.element as ue

        node, seen = product, 0
        while node is not None and seen < 20:
            seen += 1
            if node.is_a("IfcBuildingStorey"):
                return self.storeys.get(node.id(), self.project.default_storey)
            node = ue.get_container(node) or ue.get_aggregate(node)
        if z_min is not None:  # not contained: pick the level it stands on
            below = [s for s in self.project.storeys if s.elevation <= z_min + 0.05]
            if below:
                return below[-1]
        return self.project.default_storey

    # --- geometry ----------------------------------------------------------------

    def _settings(self) -> Any:
        import ifcopenshell.geom

        settings = ifcopenshell.geom.settings()
        settings.set("use-world-coords", True)
        return settings

    def _tessellate(self) -> dict[int, MeshData]:
        import ifcopenshell.geom

        out: dict[int, MeshData] = {}
        iterator = ifcopenshell.geom.iterator(self._settings(), self.f, max(1, multiprocessing.cpu_count() - 1))
        if iterator.initialize():
            while True:
                shape = iterator.get()
                verts = np.asarray(shape.geometry.verts, dtype=np.float64).reshape(-1, 3)
                faces = np.asarray(shape.geometry.faces, dtype=np.int64).reshape(-1, 3)
                if len(faces):
                    out[shape.id] = (verts, faces)
                if not iterator.next():
                    break
        return out

    def _shape(self, product: Any) -> MeshData | None:
        import ifcopenshell.geom

        try:
            shape = ifcopenshell.geom.create_shape(self._settings(), product)
        except RuntimeError:
            return None
        verts = np.asarray(shape.geometry.verts, dtype=np.float64).reshape(-1, 3)
        faces = np.asarray(shape.geometry.faces, dtype=np.int64).reshape(-1, 3)
        return (verts, faces) if len(faces) else None

    def _common(self, product: Any) -> dict[str, Any]:
        return {
            "name": product.Name or product.is_a(),
            "properties": {"ifc_guid": product.GlobalId, "ifc_class": product.is_a()},
        }

    def _imported(self, product: Any, mesh: MeshData) -> ImportedElement:
        verts, faces = mesh
        self.fallbacks[product.is_a()] += 1
        return ImportedElement(
            **self._common(product),
            storey_id=self._storey_for(product, float(verts[:, 2].min())).id,
            source_class=product.is_a(),
            source_id=product.GlobalId,
            category=category_for(product.is_a()),
            vertices=[tuple(round(float(c), 6) for c in v) for v in verts],
            faces=[tuple(int(i) for i in f) for f in faces],
        )

    # --- parametric mappings -----------------------------------------------------

    def _is_curved(self, product: Any) -> bool:
        representation = product.Representation
        for rep in representation.Representations if representation else ():
            if rep.RepresentationIdentifier == "Axis" and rep.Items:
                item = rep.Items[0]
                return not (item.is_a("IfcPolyline") and len(item.Points) == 2) and not item.is_a("IfcLine")
        return False

    def _wall(self, product: Any, mesh: MeshData) -> Wall | None:
        import ifcopenshell.util.placement

        if product.ObjectPlacement is None or self._is_curved(product):
            return None
        verts, _ = mesh
        matrix = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
        axis = matrix[:3, 0]
        if abs(axis[2]) > 0.01 or math.hypot(axis[0], axis[1]) < 1e-9:
            return None  # tilted wall
        u = axis[:2] / np.linalg.norm(axis[:2])
        n = np.array([-u[1], u[0]])
        origin = matrix[:2, 3] * self.scale
        rel = verts[:, :2] - origin
        along, across = rel @ u, rel @ n
        s0, s1, t0, t1 = along.min(), along.max(), across.min(), across.max()
        thickness = t1 - t0
        if not (0.01 <= thickness <= 3.0) or s1 - s0 < 0.05:
            return None
        z0, z1 = float(verts[:, 2].min()), float(verts[:, 2].max())
        storey = self._storey_for(product, z0)
        centre = (t0 + t1) / 2
        start = origin + u * s0 + n * centre
        end = origin + u * s1 + n * centre
        wall = Wall(
            **self._common(product),
            storey_id=storey.id,
            start=(round(float(start[0]), 6), round(float(start[1]), 6)),
            end=(round(float(end[0]), 6), round(float(end[1]), 6)),
            thickness=round(float(thickness), 6),
            height=round(z1 - z0, 6),
            base_offset=round(z0 - storey.elevation, 6),
        )
        self._add(wall)
        self.walls[product.id()] = wall
        return wall

    def _slabs(self, product: Any, mesh: MeshData) -> Slab | None:
        verts, faces = mesh
        if not _is_two_level(verts[:, 2]):
            return None  # sloped or stepped
        outline = footprint(verts, faces)
        if outline is None or outline.is_empty:
            return None
        parts = list(outline.geoms) if outline.geom_type == "MultiPolygon" else [outline]
        if any(len(p.interiors) for p in parts):
            return None  # holes are not parametric yet; keep the exact mesh
        z0, z1 = float(verts[:, 2].min()), float(verts[:, 2].max())
        storey = self._storey_for(product, z0)
        first: Slab | None = None
        for i, part in enumerate(parts):
            common = self._common(product)
            if len(parts) > 1:
                common["name"] = f"{common['name']} ({i + 1})"
                if i:  # only the first part keeps the source GlobalId
                    common["properties"] = {k: v for k, v in common["properties"].items() if k != "ifc_guid"}
            slab = Slab(
                **common,
                storey_id=storey.id,
                boundary=_ring(part),
                thickness=round(z1 - z0, 6),
                top_offset=round(z1 - storey.elevation, 6),
            )
            self._add(slab)
            first = first or slab
        return first

    def _host(self, product: Any) -> tuple[Wall | None, Any]:
        for rel in getattr(product, "FillsVoids", ()) or ():
            opening = rel.RelatingOpeningElement
            for void in getattr(opening, "VoidsElements", ()) or ():
                return self.walls.get(void.RelatingBuildingElement.id()), opening
        return None, None

    def _filler(self, product: Any, mesh: MeshData) -> Opening | None:
        wall, ifc_opening = self._host(product)
        if wall is None:
            return None
        # The void is the most accurate description of the hole; fall back to the filler's own geometry.
        verts = (self._shape(ifc_opening) if ifc_opening is not None else None) or mesh
        verts = verts[0]
        u = np.array(wall.direction)
        along = (verts[:, :2] - np.array(wall.start)) @ u
        s0, s1 = max(float(along.min()), 0.0), min(float(along.max()), wall.length)
        z0, z1 = float(verts[:, 2].min()), float(verts[:, 2].max())
        base = self.project.storey_elevation(wall) + wall.base_offset
        sill = max(z0 - base, 0.0)
        height = min(z1 - base, wall.height) - sill
        cls = Door if product.is_a("IfcDoor") else Window
        common = self._common(product)
        if ifc_opening is not None:
            common["properties"]["ifc_void_guid"] = ifc_opening.GlobalId
        opening = cls(
            **common,
            storey_id=wall.storey_id,
            host_id=wall.id,
            offset=round((s0 + s1) / 2, 6),
            width=round(s1 - s0, 6),
            height=round(height, 6),
            sill_height=round(sill, 6),
        )
        if s1 - s0 <= 0.01 or height <= 0.01 or validate(opening, self.project):
            return None
        self._add(opening)
        return opening

    def _space(self, product: Any) -> None:
        mesh = self._shape(product)
        if mesh is None:
            return
        verts, faces = mesh
        outline = footprint(verts, faces)
        if outline is None or outline.is_empty:
            return
        if outline.geom_type == "MultiPolygon":
            outline = max(outline.geoms, key=lambda g: g.area)
        z0, z1 = float(verts[:, 2].min()), float(verts[:, 2].max())
        self._add(
            Space(
                **self._common(product),
                storey_id=self._storey_for(product, z0).id,
                boundary=_ring(outline),
                height=round(z1 - z0, 6),
            )
        )
