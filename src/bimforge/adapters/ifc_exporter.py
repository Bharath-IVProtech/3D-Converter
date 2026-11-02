"""IFC4 exporter based on IfcOpenShell (LGPL-3, dynamically linked)."""

from __future__ import annotations

import importlib.util
import math
import uuid
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from bimforge.core.model import (
    CableCarrierSegment,
    Column,
    Door,
    DuctSegment,
    Fixture,
    ImportedElement,
    Opening,
    PipeSegment,
    Project,
    Slab,
    Space,
    Stair,
    SystemType,
    Wall,
    Window,
)
from bimforge.disciplines.catalog import family_of
from bimforge.geometry.builders import Tessellator
from bimforge.geometry.kernel import NumpyKernel
from bimforge.plugins.sdk import Exporter, PluginInfo


SYSTEM_TYPES = {  # IfcDistributionSystemEnum
    SystemType.COLD_WATER: "DOMESTICCOLDWATER",
    SystemType.HOT_WATER: "DOMESTICHOTWATER",
    SystemType.SANITARY: "SEWAGE",
    SystemType.VENT: "VENT",
    SystemType.STORMWATER: "STORMWATER",
    SystemType.POWER: "ELECTRICAL",
    SystemType.LIGHTING: "LIGHTING",
    SystemType.DATA: "DATA",
    SystemType.SUPPLY_AIR: "VENTILATION",
    SystemType.EXHAUST_AIR: "EXHAUST",
    SystemType.FRESH_AIR: "VENTILATION",
    SystemType.RETURN_AIR: "VENTILATION",
}


def _placement(x: float, y: float, z: float, angle: float = 0.0) -> np.ndarray:
    c, s = math.cos(angle), math.sin(angle)
    m = np.eye(4)
    m[:3, :3] = [[c, -s, 0], [s, c, 0], [0, 0, 1]]
    m[:3, 3] = [x, y, z]
    return m


class IfcExporter(Exporter):
    info = PluginInfo(id="bimforge.export.ifc", name="IFC4 (IfcOpenShell)", version="0.1.0", license="LGPL-3.0")
    extensions = (".ifc",)

    @classmethod
    def available(cls) -> bool:
        return importlib.util.find_spec("ifcopenshell") is not None

    def export(self, project: Project, path: Path, options: Mapping[str, Any] | None = None) -> list[str]:
        import ifcopenshell.api.aggregate
        import ifcopenshell.api.context
        import ifcopenshell.api.feature
        import ifcopenshell.api.geometry
        import ifcopenshell.api.project
        import ifcopenshell.api.root
        import ifcopenshell.api.spatial
        import ifcopenshell.api.unit

        api = ifcopenshell.api

        warnings: list[str] = []
        f = api.project.create_file(version="IFC4")
        ifc_project = api.root.create_entity(f, ifc_class="IfcProject", name=project.name)
        api.unit.assign_unit(f)  # SI metres, matching the domain model
        model_ctx = api.context.add_context(f, context_type="Model")
        body = api.context.add_context(
            f, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=model_ctx
        )

        site = api.root.create_entity(f, ifc_class="IfcSite", name=project.site.name)
        building = api.root.create_entity(f, ifc_class="IfcBuilding", name=project.building.name)
        api.aggregate.assign_object(f, products=[site], relating_object=ifc_project)
        api.aggregate.assign_object(f, products=[building], relating_object=site)

        storeys = {}
        for storey in project.storeys:
            ifc_storey = api.root.create_entity(f, ifc_class="IfcBuildingStorey", name=storey.name)
            ifc_storey.Elevation = storey.elevation
            api.geometry.edit_object_placement(f, product=ifc_storey, matrix=_placement(0, 0, storey.elevation))
            api.aggregate.assign_object(f, products=[ifc_storey], relating_object=building)
            storeys[storey.id] = ifc_storey

        used_guids: set[str] = set()

        def create(element, ifc_class: str, representation, matrix, key: str | None = None,
                   guid_property: str = "ifc_guid"):
            try:
                product = api.root.create_entity(f, ifc_class=ifc_class, name=element.name or None)
            except Exception:  # noqa: BLE001 - class missing from IFC4 or abstract: keep the geometry anyway
                product = api.root.create_entity(f, ifc_class="IfcBuildingElementProxy", name=element.name or None)
            # Keep the source GlobalId (from an IFC import) unless it was already used, e.g. by a copy.
            guid = element.properties.get(guid_property)
            if not guid or guid in used_guids:
                guid = _guid(key or element.id)
            used_guids.add(guid)
            product.GlobalId = guid
            api.geometry.edit_object_placement(f, product=product, matrix=matrix)
            api.geometry.assign_representation(f, product=product, representation=representation)
            container = storeys.get(element.storey_id) or next(iter(storeys.values()))
            if product.is_a("IfcSpatialElement"):  # spaces decompose the storey
                api.aggregate.assign_object(f, products=[product], relating_object=container)
            else:
                api.spatial.assign_container(f, products=[product], relating_structure=container)
            return product

        ifc_walls = {}
        for wall in project.elements_of(Wall):
            z = project.storey_elevation(wall) + wall.base_offset
            angle = math.atan2(wall.direction[1], wall.direction[0])
            # IfcOpenShell's wall body spans y in [0, thickness]; shift the origin to the centreline.
            nx, ny = wall.normal
            ox, oy = wall.start[0] - nx * wall.thickness / 2, wall.start[1] - ny * wall.thickness / 2
            rep = api.geometry.add_wall_representation(
                f, context=body, length=wall.length, height=wall.height, thickness=wall.thickness
            )
            ifc_walls[wall.id] = create(wall, "IfcWall", rep, _placement(ox, oy, z, angle))

        for opening in project.elements_of(Opening):
            host = project.get(opening.host_id)
            if not isinstance(host, Wall) or opening.host_id not in ifc_walls:
                warnings.append(f"Opening {opening.id} has no exported host wall; skipped")
                continue
            self._export_opening(api, f, body, project, opening, host, ifc_walls[host.id], create)

        for slab in project.elements_of(Slab):
            top = project.storey_elevation(slab) + slab.top_offset
            rep = api.geometry.add_slab_representation(
                f, context=body, depth=slab.thickness, polyline=[tuple(p) for p in slab.boundary]
            )
            create(slab, "IfcSlab", rep, _placement(0, 0, top - slab.thickness))

        for space in project.elements_of(Space):
            rep = api.geometry.add_slab_representation(
                f, context=body, depth=space.height, polyline=[tuple(p) for p in space.boundary]
            )
            create(space, "IfcSpace", rep, _placement(0, 0, project.storey_elevation(space)))

        for element in project.elements_of(ImportedElement):
            if not element.faces:
                continue
            rep = api.geometry.add_mesh_representation(
                f,
                context=body,
                vertices=[[list(p) for p in element.vertices]],
                faces=[[list(face) for face in element.faces]],
            )
            create(element, element.source_class, rep, _placement(0, 0, 0))

        self._export_mep(api, f, body, project, create)

        unsupported = len(project) - sum(
            len(project.elements_of(k))
            for k in (Wall, Opening, Slab, Space, ImportedElement, PipeSegment, CableCarrierSegment, Fixture, Stair,
                      Column, DuctSegment)
        )
        if unsupported:
            warnings.append(f"{unsupported} elements of types not yet mapped to IFC were skipped")

        path.parent.mkdir(parents=True, exist_ok=True)
        f.write(str(path))
        return warnings

    @staticmethod
    def _export_mep(api, f, body, project: Project, create) -> None:
        """Pipes, containment and fixtures as tessellated bodies, grouped into IfcDistributionSystems."""
        import ifcopenshell.api.system  # noqa: F401 - registers api.system

        tessellator = Tessellator(NumpyKernel())
        members: dict[str, list] = defaultdict(list)
        elements = project.elements_of(PipeSegment) + project.elements_of(CableCarrierSegment)
        elements += project.elements_of(Fixture) + project.elements_of(Stair) + project.elements_of(Column)
        elements += project.elements_of(DuctSegment)
        for element in elements:
            mesh = tessellator.mesh_for(element, project)
            if mesh is None or mesh.is_empty:
                continue
            if isinstance(element, PipeSegment):
                ifc_class, predefined = "IfcPipeSegment", "RIGIDSEGMENT"
            elif isinstance(element, CableCarrierSegment):
                ifc_class, predefined = "IfcCableCarrierSegment", element.carrier_type
            elif isinstance(element, DuctSegment):
                ifc_class, predefined = "IfcDuctSegment", "RIGIDSEGMENT"
            elif isinstance(element, Stair):
                ifc_class, predefined = "IfcStair", "STRAIGHT_RUN_STAIR"
            elif isinstance(element, Column):
                ifc_class, predefined = "IfcColumn", "COLUMN"
            else:
                family = family_of(element)
                ifc_class, predefined = (family.ifc_class, family.predefined_type) if family else (
                    "IfcBuildingElementProxy", None)
            rep = api.geometry.add_mesh_representation(
                f, context=body, vertices=[mesh.vertices.round(6).tolist()], faces=[mesh.faces.tolist()]
            )
            product = create(element, ifc_class, rep, _placement(0, 0, 0))
            if predefined and hasattr(product, "PredefinedType"):
                try:
                    product.PredefinedType = predefined
                except Exception:  # noqa: BLE001 - enum value not valid for the fallback class
                    pass
            system_id = getattr(element, "system_id", None)  # stairs and columns belong to no system
            if system_id in project.systems:
                members[system_id].append(product)
        for system in project.systems.values():
            ifc_system = api.system.add_system(f, ifc_class="IfcDistributionSystem")
            ifc_system.Name = system.name
            ifc_system.GlobalId = _guid(f"system-{system.id}")
            ifc_system.PredefinedType = SYSTEM_TYPES.get(system.system_type, "NOTDEFINED")
            if members[system.id]:
                api.system.assign_system(f, products=members[system.id], system=ifc_system)

    @staticmethod
    def _export_opening(api, f, body, project, opening: Opening, host: Wall, ifc_wall, create) -> None:
        z = project.storey_elevation(host) + host.base_offset + opening.sill_height
        angle = math.atan2(host.direction[1], host.direction[0])
        s0, _ = opening.span
        nx, ny = host.normal
        cx, cy = host.point_at(s0)
        # Void box cuts slightly past both wall faces to avoid coplanar faces.
        margin = 0.01
        depth = host.thickness + 2 * margin
        origin = (cx - nx * depth / 2, cy - ny * depth / 2)
        matrix = _placement(origin[0], origin[1], z, angle)

        void_rep = api.geometry.add_wall_representation(
            f, context=body, length=opening.width, height=opening.height, thickness=depth
        )
        void = create(
            opening, "IfcOpeningElement", void_rep, matrix, key=f"{opening.id}-void", guid_property="ifc_void_guid"
        )
        api.feature.add_feature(f, feature=void, element=ifc_wall)

        if isinstance(opening, (Door, Window)):
            panel = 0.05 if isinstance(opening, Door) else 0.02
            fill_origin = (cx - nx * panel / 2, cy - ny * panel / 2)
            fill_rep = api.geometry.add_wall_representation(
                f, context=body, length=opening.width, height=opening.height, thickness=panel
            )
            filler = create(opening, opening.ifc_class, fill_rep, _placement(*fill_origin, z, angle))
            filler.OverallWidth, filler.OverallHeight = opening.width, opening.height
            api.feature.add_filling(f, opening=void, element=filler)


def _guid(element_id: str) -> str:
    """Stable IFC GlobalId derived from the element id, so re-exports keep identities."""
    import ifcopenshell.guid

    return ifcopenshell.guid.compress(uuid.uuid5(uuid.NAMESPACE_URL, f"bimforge:{element_id}").hex)
