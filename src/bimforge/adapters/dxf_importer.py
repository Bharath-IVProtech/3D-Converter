"""DXF importer based on ezdxf (MIT). Reads a drawing into a Drawing2D.

Real-world plans are often drawn inside blocks (a whole floor inserted as one
block, or bound xrefs), with walls as wide polylines or multilines. So by
default every block reference is kept as a BlockRef (doors, windows and other
symbols are recognised from those) *and* exploded recursively into its
primitives, each tagged with the block it came from.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox, recover
from ezdxf.math import Vec3

from bimforge.adapters import drawing_cache
from bimforge.core.units import from_dxf_insunits
from bimforge.drawing.primitives import (
    Arc,
    BlockRef,
    Dimension,
    Drawing2D,
    Layer,
    Polyline,
    Segment,
    Text,
)
from bimforge.plugins.sdk import Importer, ImportResult, PluginInfo

MAX_BLOCK_DEPTH = 8
FLIPPABLE = {"LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "TEXT", "ATTRIB"}
IGNORED = {"VIEWPORT", "POINT", "IMAGE", "WIPEOUT", "OLE2FRAME", "ATTDEF", "SEQEND", "VERTEX"}


def _xy(v: Any) -> tuple[float, float]:
    return (float(v[0]), float(v[1]))


def _flattener(entity: Any):
    """Turns an entity's own coordinates into drawing coordinates, or None if they already are.

    Arcs, polylines, text and block references are stored relative to their "extrusion" direction.
    Mirroring an object in CAD turns that direction over (0, 0, -1), which negates x, so reading the
    numbers directly would put mirrored objects on the far side of the origin.
    """
    if not entity.dxf.hasattr("extrusion"):
        return None
    ocs = entity.ocs()
    if not ocs.transform:
        return None
    return lambda p: _xy(ocs.to_wcs(Vec3(p[0], p[1], 0.0)))


def _world_angle(flat, degrees: float) -> float:
    """An angle measured in an entity's own coordinates, as seen in the drawing."""
    a = math.radians(degrees)
    x0, y0 = flat((0.0, 0.0))
    x1, y1 = flat((math.cos(a), math.sin(a)))
    return math.degrees(math.atan2(y1 - y0, x1 - x0))


@dataclass
class _Stats:
    skipped: Counter[str] = field(default_factory=Counter)
    bulged: int = 0
    exploded: int = 0
    failed: Counter[str] = field(default_factory=Counter)
    xrefs: set[str] = field(default_factory=set)

    def add_to(self, other: _Stats) -> None:
        other.skipped.update(self.skipped)
        other.bulged += self.bulged
        other.failed.update(self.failed)
        other.xrefs |= self.xrefs


@dataclass
class _Contents:
    """What a block holds, in the block's own coordinates."""

    primitives: list = field(default_factory=list)
    stats: _Stats = field(default_factory=_Stats)


Affine = tuple[float, float, float, float, float, float]  # x' = a x + b y + c, y' = d x + e y + f


def _placements(insert: Any, base: tuple[float, float]) -> list[Affine]:
    """Where a block reference puts the block: one placement, or a grid of them for a MINSERT."""
    get = insert.dxf.get
    sx, sy = get("xscale", 1.0), get("yscale", 1.0)
    angle = math.radians(get("rotation", 0.0))
    cos, sin = math.cos(angle), math.sin(angle)
    ix, iy = _xy(insert.dxf.insert)
    bx, by = base
    flat = _flattener(insert)
    a, b, d, e = cos * sx, -sin * sy, sin * sx, cos * sy
    placements = []
    for row in range(max(int(get("row_count", 1)), 1)):
        for column in range(max(int(get("column_count", 1)), 1)):
            ox, oy = column * get("column_spacing", 0.0), row * get("row_spacing", 0.0)
            px, py = ix + cos * ox - sin * oy, iy + sin * ox + cos * oy
            c, f = px - a * bx - b * by, py - d * bx - e * by
            if flat is None:
                placements.append((a, b, c, d, e, f))
                continue
            # The whole placement happens in the flipped coordinates.
            x0, y0 = flat((0.0, 0.0))
            x1, y1 = flat((1.0, 0.0))
            x2, y2 = flat((0.0, 1.0))
            ua, ub, ud, ue = x1 - x0, x2 - x0, y1 - y0, y2 - y0
            placements.append((ua * a + ub * d, ua * b + ub * e, ua * c + ub * f + x0,
                               ud * a + ue * d, ud * b + ue * e, ud * c + ue * f + y0))
    return placements


def _stamp(primitives: list, m: Affine, parent_layer: str, out: list) -> None:
    """Copy a block's objects to a placement. Objects on layer 0 take the layer of the reference."""
    a, b, c, d, e, f = m
    scale_x, scale_y = math.hypot(a, d), math.hypot(b, e)
    largest = max(scale_x, scale_y, 1e-12)
    uniform = abs(scale_x - scale_y) <= 1e-9 * largest and abs(a * b + d * e) <= 1e-9 * largest * largest
    mirrored = a * e - b * d < 0

    def point(p):
        return (a * p[0] + b * p[1] + c, d * p[0] + e * p[1] + f)

    def angle(degrees: float) -> float:
        r = math.radians(degrees)
        x, y = math.cos(r), math.sin(r)
        return math.degrees(math.atan2(d * x + e * y, a * x + b * y))

    for p in primitives:
        layer = parent_layer if p.layer == "0" else p.layer
        kind = type(p)
        if kind is Segment:
            sx, sy = p.start
            ex, ey = p.end
            out.append(Segment(start=(a * sx + b * sy + c, d * sx + e * sy + f),
                               end=(a * ex + b * ey + c, d * ex + e * ey + f), layer=layer, block=p.block))
        elif kind is Polyline:
            out.append(Polyline(points=tuple((a * x + b * y + c, d * x + e * y + f) for x, y in p.points), closed=p.closed,
                                width=p.width * (scale_x + scale_y) / 2, filled=p.filled, layer=layer, block=p.block))
        elif kind is Arc:
            full = p.end_angle - p.start_angle >= 360
            if uniform:
                start, end = p.start_angle, p.end_angle
                if not full:
                    start, end = (angle(end) % 360, angle(start) % 360) if mirrored else (angle(start) % 360, angle(end) % 360)
                out.append(Arc(center=point(p.center), radius=p.radius * scale_x, start_angle=start, end_angle=end,
                               layer=layer, block=p.block))
            else:  # squeezed unevenly an arc is no longer an arc: keep its true shape as short lines
                out.append(Polyline(points=_arc_points(p, point), closed=full, layer=layer, block=p.block))
        elif kind is Text:
            out.append(Text(position=point(p.position), text=p.text, height=p.height * scale_y, rotation=angle(p.rotation),
                            layer=layer, block=p.block))
        elif kind is BlockRef:
            sx, sy = p.scale
            r = math.radians(p.rotation)
            cos, sin = math.cos(r), math.sin(r)
            # The combined turn and scale, split back into a rotation and two scales.
            ax, ay = (a * cos + b * sin) * sx, (d * cos + e * sin) * sx
            bx, by = (-a * sin + b * cos) * sy, (-d * sin + e * cos) * sy
            x_scale = math.hypot(ax, ay)
            y_scale = (ax * by - ay * bx) / (x_scale or 1.0)
            out.append(BlockRef(name=p.name, position=point(p.position), rotation=math.degrees(math.atan2(ay, ax)),
                                scale=(x_scale, y_scale), extent=p.extent, attributes=p.attributes, layer=layer, block=p.block))
        elif kind is Dimension:
            out.append(Dimension(p1=point(p.p1), p2=point(p.p2), measurement=p.measurement, text=p.text, layer=layer, block=p.block))


def _arc_points(arc: Arc, point) -> tuple:
    start, end = arc.start_angle, arc.end_angle
    sweep = (end - start) if end > start else end - start + 360
    pieces = max(int(sweep / 15), 2)
    return tuple(point((arc.center[0] + arc.radius * math.cos(math.radians(start + sweep * i / pieces)),
                        arc.center[1] + arc.radius * math.sin(math.radians(start + sweep * i / pieces))))
                 for i in range(pieces + (sweep < 360)))


class DxfImporter(Importer):
    info = PluginInfo(id="bimforge.import.dxf", name="DXF (ezdxf)", version="0.2.0", license="MIT")
    extensions = (".dxf",)

    def import_file(self, path: Path, options: Mapping[str, Any] | None = None) -> ImportResult:
        options = options or {}
        explode = bool(options.get("explode_blocks", True))
        path = Path(path)
        cached = drawing_cache.load(path, f"explode={explode}") if not options.get("no_cache") else None
        if cached is not None:
            return ImportResult(drawing=cached, warnings=list(cached.warnings))
        doc, errors = self._read_file(path, trusted=bool(options.get("trusted")))
        result = self.read_document(doc, str(path), explode, auditor_errors=errors)
        if not options.get("no_cache"):
            drawing_cache.save(path, f"explode={explode}", result.drawing)
        return result

    @staticmethod
    def _read_file(path: Path, trusted: bool) -> tuple[Any, int]:
        """A file from the DWG converter is well formed, so it skips the much slower repairing read."""
        if trusted:
            try:
                return ezdxf.readfile(str(path)), 0
            except Exception:  # noqa: BLE001 - fall through to the repairing read
                pass
        try:
            doc, auditor = recover.readfile(str(path))
        except (OSError, ezdxf.DXFStructureError) as exc:
            raise ValueError(f"Cannot read DXF '{path}': {exc}") from exc
        return doc, len(auditor.errors)

    def read_document(self, doc: Any, source: str, explode: bool = True, auditor_errors: int = 0) -> ImportResult:
        drawing = Drawing2D(source=source, unit=from_dxf_insunits(doc.header.get("$INSUNITS", 0)))
        if auditor_errors:
            drawing.warnings.append(f"Recovered {auditor_errors} structural errors in the file")
        for layer in doc.layers:
            drawing.layers[layer.dxf.name] = Layer(
                name=layer.dxf.name,
                color=abs(layer.dxf.color),
                linetype=layer.dxf.linetype,
                visible=layer.is_on() and not layer.is_frozen(),
            )

        layout = doc.modelspace()
        if len(layout) == 0:
            for name in doc.layout_names_in_taborder():
                candidate = doc.layouts.get(name)
                if name != "Model" and len(candidate):
                    drawing.warnings.append(f"Model space is empty; read paper-space layout '{name}' instead")
                    layout = candidate
                    break

        self._doc = doc
        self._extents: dict[str, tuple | None] = {}
        self._boxes: dict[str, tuple[float, float, float, float] | None] = {}
        self._contents: dict[str, _Contents] = {}
        stats = _Stats()
        for entity in layout:
            self._convert(entity, drawing, stats, explode, block="", depth=0, parent_layer=None)

        if stats.exploded:
            drawing.warnings.append(f"Exploded {stats.exploded} block references to read their contents")
        if stats.xrefs:
            drawing.warnings.append(
                "External references were not loaded (bind them in your CAD program first): "
                + ", ".join(sorted(stats.xrefs))
            )
        if stats.failed:
            drawing.warnings.append("Could not explode: " + ", ".join(f"{n}x {k}" for k, n in stats.failed.items()))
        if stats.bulged:
            drawing.warnings.append(f"{stats.bulged} polylines contain arc segments; arcs were approximated as chords")
        if stats.skipped:
            summary = ", ".join(f"{n}x {k}" for k, n in stats.skipped.most_common())
            drawing.warnings.append(f"Unsupported entities skipped: {summary}")
        self._contents = {}  # these can be as big as the drawing
        return ImportResult(drawing=drawing, warnings=list(drawing.warnings))

    # --- entities -----------------------------------------------------------------

    def _convert(self, entity: Any, out: Drawing2D, stats: _Stats, explode: bool, block: str, depth: int,
                 parent_layer: str | None) -> None:
        kind = entity.dxftype()
        layer = entity.dxf.get("layer", "0")
        if parent_layer and layer == "0":
            layer = parent_layer  # entities on layer 0 inside a block take the insert's layer
        common = {"layer": layer, "handle": entity.dxf.get("handle") or "", "block": block}
        flat = _flattener(entity) if kind in FLIPPABLE else None

        if kind == "LINE":
            out.primitives.append(Segment(start=_xy(entity.dxf.start), end=_xy(entity.dxf.end), **common))
        elif kind == "LWPOLYLINE":
            points = list(entity.get_points("xyseb"))
            stats.bulged += any(b for *_, b in points)
            width = float(entity.dxf.get("const_width", 0.0) or 0.0)
            if not width and points and all(s == e == points[0][2] for _, _, s, e, _ in points[:-1]):
                width = float(points[0][2])
            corners = tuple(flat((x, y)) if flat else (x, y) for x, y, *_ in points)
            out.primitives.append(Polyline(points=corners, closed=entity.closed, width=width, **common))
        elif kind == "POLYLINE":
            if entity.is_2d_polyline:
                pts = tuple(flat(v.dxf.location) if flat else _xy(v.dxf.location) for v in entity.vertices)
                width = float(entity.dxf.get("default_start_width", 0.0) or 0.0)
                out.primitives.append(Polyline(points=pts, closed=entity.is_closed, width=width, **common))
            else:
                stats.skipped["3D POLYLINE/MESH"] += 1
        elif kind == "ARC":
            start, end = entity.dxf.start_angle, entity.dxf.end_angle
            centre = _xy(entity.dxf.center)
            if flat:  # seen from behind, an arc runs the other way round
                start, end = _world_angle(flat, end) % 360, _world_angle(flat, start) % 360
                centre = flat(centre)
            out.primitives.append(Arc(center=centre, radius=entity.dxf.radius, start_angle=start, end_angle=end, **common))
        elif kind == "CIRCLE":
            centre = flat(entity.dxf.center) if flat else _xy(entity.dxf.center)
            out.primitives.append(Arc(center=centre, radius=entity.dxf.radius, **common))
        elif kind in ("TEXT", "ATTRIB"):
            rotation = entity.dxf.get("rotation", 0.0)
            out.primitives.append(Text(position=flat(entity.dxf.insert) if flat else _xy(entity.dxf.insert),
                                       text=entity.dxf.text, height=entity.dxf.get("height", 0.0),
                                       rotation=_world_angle(flat, rotation) if flat else rotation, **common))
        elif kind == "MTEXT":
            out.primitives.append(Text(position=_xy(entity.dxf.insert), text=entity.plain_text(),
                                       height=entity.dxf.get("char_height", 0.0), **common))
        elif kind == "DIMENSION":
            measurement = entity.get_measurement()
            out.primitives.append(Dimension(
                p1=_xy(entity.dxf.get("defpoint2", (0, 0))), p2=_xy(entity.dxf.get("defpoint3", (0, 0))),
                measurement=float(measurement) if isinstance(measurement, (int, float)) else 0.0,
                text=entity.dxf.get("text", ""), **common))
        elif kind == "INSERT":
            self._insert(entity, out, stats, explode, block, depth, layer, common)
        elif kind in ("HATCH", "SOLID"):
            # A hatch or solid boundary marks a filled area: a poche wall, a column, a slab edge.
            for points in self._fill_boundaries(entity, kind):
                if len(points) >= 3:
                    out.primitives.append(Polyline(points=tuple(points), closed=True, filled=True, **common))
        elif kind == "MLINE":
            # Multilines are a common way to draw walls; their component lines are the wall faces.
            self._explode(entity, out, stats, explode=True, block=block, depth=depth, layer=layer)
        elif kind in IGNORED:
            pass
        else:
            stats.skipped[kind] += 1

    def _insert(self, entity: Any, out: Drawing2D, stats: _Stats, explode: bool, block: str, depth: int,
                layer: str, common: dict) -> None:
        name = entity.dxf.name
        if name not in self._extents:
            self._extents[name] = self._block_extent(name)
        flat = _flattener(entity)
        rotation, xscale = entity.dxf.get("rotation", 0.0), entity.dxf.get("xscale", 1.0)
        if flat:  # a flip after a turn is the opposite turn after a flip
            rotation, xscale = -rotation, -xscale
        out.primitives.append(BlockRef(
            name=name,
            position=flat(entity.dxf.insert) if flat else _xy(entity.dxf.insert),
            rotation=rotation,
            scale=(xscale, entity.dxf.get("yscale", 1.0)),
            extent=self._extents[name],
            attributes={a.dxf.tag: a.dxf.text for a in entity.attribs},
            **common,
        ))
        definition = self._doc.blocks.get(name)
        if definition is not None and (definition.block.is_xref or definition.block.is_xref_overlay):
            stats.xrefs.add(name)
            return
        if explode and depth < MAX_BLOCK_DEPTH:
            stats.exploded += depth == 0
            contents = self._block_contents(name, depth + 1)
            if contents is None:
                stats.failed["INSERT"] += 1
                return
            for placement in _placements(entity, _xy(definition.block.dxf.get("base_point", (0.0, 0.0, 0.0)))):
                _stamp(contents.primitives, placement, layer, out.primitives)
                contents.stats.add_to(stats)

    def _block_contents(self, name: str, depth: int) -> _Contents | None:
        """A block's objects in its own coordinates, worked out once however often it is placed.

        Asking ezdxf to explode each placement deep-copies every object each time. On a real basement
        plan, where one light fitting is hundreds of lines and appears hundreds of times, that was
        most of the read.
        """
        if name in self._contents:
            return self._contents[name]
        definition = self._doc.blocks.get(name)
        if definition is None:
            return None
        contents = self._contents[name] = _Contents()  # a block that contains itself adds nothing more
        for entity in definition:
            self._convert(entity, contents, contents.stats, True, block=name, depth=depth, parent_layer=None)
        return contents

    def _explode(self, entity: Any, out: Drawing2D, stats: _Stats, explode: bool, block: str, depth: int,
                 layer: str) -> None:
        try:
            children = list(entity.virtual_entities())
        except Exception:  # noqa: BLE001 - non-uniform scaling and broken blocks cannot always be exploded
            stats.failed[entity.dxftype()] += 1
            return
        for child in children:
            self._convert(child, out, stats, explode, block=block, depth=depth, parent_layer=layer)

    @staticmethod
    def _fill_boundaries(entity: Any, kind: str) -> list[list[tuple[float, float]]]:
        if kind == "SOLID":
            flat = _flattener(entity) or _xy
            corners = [flat(entity.dxf.get(f"vtx{i}", (0, 0))) for i in range(4)]
            if corners[3] == corners[2]:
                corners = corners[:3]
            else:  # SOLID vertex order is 0-1-3-2 around the shape
                corners = [corners[0], corners[1], corners[3], corners[2]]
            return [corners]
        boundaries = []
        try:
            from ezdxf import path as ezpath

            for p in ezpath.from_hatch(entity):
                box = p.bbox() if hasattr(p, "bbox") else None
                size = max(box.size.x, box.size.y) if box is not None and box.has_data else 1.0
                boundaries.append([_xy(v) for v in p.flattening(max(size / 200, 1e-6))])
        except Exception:  # noqa: BLE001 - malformed hatch boundaries are skipped
            return []
        return boundaries

    def _block_extent(self, name: str) -> tuple | None:
        """A block's box relative to its base point, so BlockRef.to_world / world_center land on the symbol."""
        box = self._block_box(name)
        if box is None:
            return None
        return ((box[0], box[1]), (box[2], box[3]))

    def _block_box(self, name: str) -> tuple[float, float, float, float] | None:
        """Measured directly, reusing nested blocks' boxes. A big drawing nests thousands of blocks,
        and asking ezdxf to do this copies every nested object (minutes on a real basement plan)."""
        if name in self._boxes:
            return self._boxes[name]
        self._boxes[name] = None  # a block that contains itself adds nothing
        block = self._doc.blocks.get(name)
        if block is None:
            return None
        points: list[tuple[float, float]] = []
        unmeasured = False
        for entity in block:
            found = self._entity_points(entity)
            if found is None:
                unmeasured = True
            else:
                points += found
        bx, by = _xy(block.block.dxf.get("base_point", (0.0, 0.0, 0.0)))
        if points:
            xs, ys = [q[0] for q in points], [q[1] for q in points]
            box = (min(xs) - bx, min(ys) - by, max(xs) - bx, max(ys) - by)
        elif unmeasured:  # only shapes without simple points, such as hatches
            box = self._library_box(block, bx, by)
        else:
            box = None
        self._boxes[name] = box
        return box

    def _entity_points(self, entity: Any) -> list[tuple[float, float]] | None:
        """Points that bound an entity in its block's coordinates; None for what can't be measured simply.

        Text and attributes are left out: a door's number shouldn't widen the door.
        """
        kind = entity.dxftype()
        flat = _flattener(entity) if kind in FLIPPABLE or kind == "INSERT" else None
        to_block = flat or _xy
        if kind == "LINE":
            return [_xy(entity.dxf.start), _xy(entity.dxf.end)]
        if kind == "LWPOLYLINE":
            return [to_block(q) for q in entity.get_points("xy")]
        if kind == "POLYLINE":
            return [to_block(v.dxf.location) for v in entity.vertices]
        if kind == "CIRCLE":
            (x, y), r = _xy(entity.dxf.center), entity.dxf.radius
            return [to_block((x + dx, y + dy)) for dx, dy in ((r, r), (-r, -r), (r, -r), (-r, r))]
        if kind == "ARC":
            (x, y), r = _xy(entity.dxf.center), entity.dxf.radius
            a0, a1 = entity.dxf.start_angle % 360, entity.dxf.end_angle % 360
            sweep = (a1 - a0) % 360 or 360
            angles = [a0, a1] + [q for q in (0, 90, 180, 270) if (q - a0) % 360 <= sweep]
            return [to_block((x + r * math.cos(math.radians(q)), y + r * math.sin(math.radians(q)))) for q in angles]
        if kind == "ELLIPSE":
            cx, cy = _xy(entity.dxf.center)
            mx, my = _xy(entity.dxf.major_axis)
            return [(cx + mx, cy + my), (cx - mx, cy - my), (cx - my * entity.dxf.ratio, cy + mx * entity.dxf.ratio),
                    (cx + my * entity.dxf.ratio, cy - mx * entity.dxf.ratio)]
        if kind == "SPLINE":
            return [_xy(q) for q in entity.control_points]
        if kind == "SOLID":
            return [to_block(entity.dxf.get(f"vtx{i}", (0, 0))) for i in range(4)]
        if kind == "INSERT":
            child = self._block_box(entity.dxf.name)
            if child is None:
                return []
            ref = BlockRef(name=entity.dxf.name, position=_xy(entity.dxf.insert), rotation=entity.dxf.get("rotation", 0.0),
                           scale=(entity.dxf.get("xscale", 1.0), entity.dxf.get("yscale", 1.0)))
            x0, y0, x1, y1 = child
            corners = [ref.to_world(q) for q in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            return [flat(q) for q in corners] if flat else corners
        if kind in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "LEADER") or kind in IGNORED:
            return []
        return None

    @staticmethod
    def _library_box(block: Any, bx: float, by: float) -> tuple[float, float, float, float] | None:
        try:
            box = bbox.extents(block, fast=True)
        except Exception:  # noqa: BLE001
            return None
        if not box.has_data:
            return None
        (x0, y0), (x1, y1) = _xy(box.extmin), _xy(box.extmax)
        return (x0 - bx, y0 - by, x1 - bx, y1 - by)
