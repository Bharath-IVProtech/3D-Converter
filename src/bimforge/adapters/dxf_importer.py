"""DXF importer based on ezdxf (MIT). Reads a drawing into a Drawing2D.

Real-world plans are often drawn inside blocks (a whole floor inserted as one
block, or bound xrefs), with walls as wide polylines or multilines. So by
default every block reference is kept as a BlockRef (doors, windows and other
symbols are recognised from those) *and* exploded recursively into its
primitives, each tagged with the block it came from.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox, recover

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
IGNORED = {"VIEWPORT", "POINT", "IMAGE", "WIPEOUT", "OLE2FRAME", "ATTDEF", "SEQEND", "VERTEX"}


def _xy(v: Any) -> tuple[float, float]:
    return (float(v[0]), float(v[1]))


@dataclass
class _Stats:
    skipped: Counter[str] = field(default_factory=Counter)
    bulged: int = 0
    exploded: int = 0
    failed: Counter[str] = field(default_factory=Counter)
    xrefs: set[str] = field(default_factory=set)


class DxfImporter(Importer):
    info = PluginInfo(id="bimforge.import.dxf", name="DXF (ezdxf)", version="0.2.0", license="MIT")
    extensions = (".dxf",)

    def import_file(self, path: Path, options: Mapping[str, Any] | None = None) -> ImportResult:
        options = options or {}
        try:
            doc, auditor = recover.readfile(str(path))
        except (OSError, ezdxf.DXFStructureError) as exc:
            raise ValueError(f"Cannot read DXF '{path}': {exc}") from exc
        return self.read_document(doc, str(path), bool(options.get("explode_blocks", True)),
                                  auditor_errors=len(auditor.errors))

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
        return ImportResult(drawing=drawing, warnings=list(drawing.warnings))

    # --- entities -----------------------------------------------------------------

    def _convert(self, entity: Any, out: Drawing2D, stats: _Stats, explode: bool, block: str, depth: int,
                 parent_layer: str | None) -> None:
        kind = entity.dxftype()
        layer = entity.dxf.get("layer", "0")
        if parent_layer and layer == "0":
            layer = parent_layer  # entities on layer 0 inside a block take the insert's layer
        common = {"layer": layer, "handle": entity.dxf.get("handle") or "", "block": block}

        if kind == "LINE":
            out.primitives.append(Segment(start=_xy(entity.dxf.start), end=_xy(entity.dxf.end), **common))
        elif kind == "LWPOLYLINE":
            points = list(entity.get_points("xyseb"))
            stats.bulged += any(b for *_, b in points)
            width = float(entity.dxf.get("const_width", 0.0) or 0.0)
            if not width and points and all(s == e == points[0][2] for _, _, s, e, _ in points[:-1]):
                width = float(points[0][2])
            out.primitives.append(Polyline(points=tuple((x, y) for x, y, *_ in points), closed=entity.closed,
                                           width=width, **common))
        elif kind == "POLYLINE":
            if entity.is_2d_polyline:
                pts = tuple(_xy(v.dxf.location) for v in entity.vertices)
                width = float(entity.dxf.get("default_start_width", 0.0) or 0.0)
                out.primitives.append(Polyline(points=pts, closed=entity.is_closed, width=width, **common))
            else:
                stats.skipped["3D POLYLINE/MESH"] += 1
        elif kind == "ARC":
            out.primitives.append(Arc(center=_xy(entity.dxf.center), radius=entity.dxf.radius,
                                      start_angle=entity.dxf.start_angle, end_angle=entity.dxf.end_angle, **common))
        elif kind == "CIRCLE":
            out.primitives.append(Arc(center=_xy(entity.dxf.center), radius=entity.dxf.radius, **common))
        elif kind in ("TEXT", "ATTRIB"):
            out.primitives.append(Text(position=_xy(entity.dxf.insert), text=entity.dxf.text,
                                       height=entity.dxf.get("height", 0.0), rotation=entity.dxf.get("rotation", 0.0),
                                       **common))
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
        out.primitives.append(BlockRef(
            name=name,
            position=_xy(entity.dxf.insert),
            rotation=entity.dxf.get("rotation", 0.0),
            scale=(entity.dxf.get("xscale", 1.0), entity.dxf.get("yscale", 1.0)),
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
            self._explode(entity, out, stats, explode, block=name, depth=depth + 1, layer=layer)

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
            corners = [_xy(entity.dxf.get(f"vtx{i}", (0, 0))) for i in range(4)]
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
        block = self._doc.blocks.get(name)
        if block is None:
            return None
        try:
            box = bbox.extents(block, fast=True)
        except Exception:  # noqa: BLE001
            return None
        if not box.has_data:
            return None
        return (_xy(box.extmin), _xy(box.extmax))
