"""Orchestrates Drawing2D -> reviewed proposals -> parametric elements."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from bimforge.conversion.labels import (
    TextBlock,
    column_tag,
    door_tag,
    group_text,
    level_from_title,
    louvre_note,
    project_title,
    room_name,
    stair_name,
    window_tag,
)
from bimforge.conversion.layers import WALL_LAYER_SCORE, LayerRole, classify_name
from bimforge.conversion.openings import (
    OpeningCandidate,
    arcs_from_chains,
    find_infill_windows,
    place_openings,
    symbols_from_arcs,
    symbols_from_blocks,
    without_duplicates,
)
from bimforge.conversion.rooms import detect_rooms, footprint, ring
from bimforge.conversion.settings import ConversionSettings
from bimforge.conversion.ducts import (
    AIR_SYSTEMS,
    detect_ducts,
    duct_wall_holes,
    find_louvres,
    parse_labels,
    parse_louvre_sizes,
)
from bimforge.conversion.fills import filled_elements
from bimforge.conversion.stairs import detect_stairs, link_flights, orient_by_notes, walls_clear_of_stairs
from bimforge.conversion.walls import WallCandidate, hatch_walls, join_corners, merge_collinear, pair_wall_faces
from bimforge.core.model import Column, DistributionSystem, Door, DuctSegment, Element, Opening, Slab, Space, Stair, Storey, Wall, Window
from bimforge.core.units import LengthUnit, guess_unit_from_extent
from bimforge.drawing.primitives import Arc, BlockRef, Drawing2D, Polyline, Segment, Text
from bimforge.geometry import vec2 as v
from bimforge.geometry.spatial import segment_distances

HOST_TOLERANCE = 0.05
NOTE_EXAMPLES = 3  # how many example names a note gives
AUTO_LAYER_MIN_LAYERS = 5  # only guess the wall layer when there are many unnamed layers
AUTO_LAYER_CANDIDATES = 12
COORD_DECIMALS = 6  # micrometre rounding removes float noise such as 8e-17


def _key(seg: Segment) -> tuple:
    """Position key of a segment, independent of its direction (for matching copies of a line)."""
    a = (round(seg.start[0], 4), round(seg.start[1], 4))
    b = (round(seg.end[0], 4), round(seg.end[1], 4))
    return (a, b) if a <= b else (b, a)


def _within(point, box) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _inside_any_column(wall: WallCandidate, columns: list) -> bool:
    mid = ((wall.start[0] + wall.end[0]) / 2, (wall.start[1] + wall.end[1]) / 2)
    return any(math.dist(mid, c.position) <= max(c.width, c.depth) / 2 for c in columns)


ISOLATED_MAX_LENGTH = 2.0


def _demote_isolated(walls: list[WallCandidate]) -> list[WallCandidate]:
    """Short walls touching no other wall are usually equipment plinths or boxes: leave them unticked."""
    if len(walls) < 2:
        return walls
    starts = np.array([w.start for w in walls], dtype=np.float64)
    ends = np.array([w.end for w in walls], dtype=np.float64)
    for i, w in enumerate(walls):
        if w.length >= ISOLATED_MAX_LENGTH:
            continue
        touching = False
        for p in (w.start, w.end):
            d = segment_distances(p, starts, ends)
            d[i] = np.inf
            touching |= bool(np.any(d <= w.thickness + 0.02))
        if not touching:
            w.confidence *= 0.5
    return walls


def _connectedness(walls: list[WallCandidate]) -> float:
    """Share of wall ends that meet another wall. Real walls form a network; table rows and
    hatching are parallel strokes with free ends."""
    if len(walls) < 2:
        return 0.0
    starts = np.array([w.start for w in walls], dtype=np.float64)
    ends = np.array([w.end for w in walls], dtype=np.float64)
    joined = 0
    for i, w in enumerate(walls):
        for p in (w.start, w.end):
            d = segment_distances(p, starts, ends)
            d[i] = np.inf
            joined += bool(np.any(d <= w.thickness + 0.02))
    return joined / (2 * len(walls))


def _unplaced_notes(symbols: list) -> list[str]:
    """One note per kind of symbol, so a big drawing doesn't produce hundreds of lines."""
    notes = []
    for kind in sorted({s.kind for s in symbols}):
        names = Counter(s.name.split("$")[-1] for s in symbols if s.kind == kind)
        examples = ", ".join(f"'{name}'" for name, _ in names.most_common(NOTE_EXAMPLES))
        more = len(names) - NOTE_EXAMPLES
        notes.append(f"{sum(names.values())} {kind} symbols are not in any wall, so they were left out: {examples}"
                     + (f" and {more} other kinds" if more > 0 else ""))
    return notes


def _duct_name(c, n: int) -> str:
    """The size label as drawn ("425x300 EA"); a run nobody labelled keeps a plain number."""
    if not c.labelled:
        return f"{c.kind or 'Duct'}-{n}"
    mm = lambda metres: f"{metres * 1000:.0f}"  # noqa: E731
    size = f"{mm(c.width)}x{mm(c.depth)}"
    if c.end_width:
        size += f" to {mm(c.end_width)}x{mm(c.depth)}"
    return f"{size} {c.kind}".strip()


def _r(point):
    return (round(point[0], COORD_DECIMALS) + 0.0, round(point[1], COORD_DECIMALS) + 0.0)


@dataclass
class Proposal:
    """One detected element awaiting user review."""

    element: Element
    confidence: float
    accepted: bool
    sources: tuple[str, ...] = ()  # source drawing entity handles
    note: str = ""

    @property
    def kind(self) -> str:
        return type(self.element).__name__


@dataclass
class ConversionResult:
    proposals: list[Proposal]
    unit: LengthUnit
    warnings: list[str] = field(default_factory=list)
    systems: list[DistributionSystem] = field(default_factory=list)  # systems the proposals refer to
    labels: list[TextBlock] = field(default_factory=list)  # the drawing's text, grouped into blocks
    project_name: str | None = None  # from the title block
    level_name: str | None = None  # from the plan's title

    def accepted_elements(self) -> list[Element]:
        """Accepted elements, dropping openings whose host wall was rejected."""
        accepted = [p.element for p in self.proposals if p.accepted]
        ids = {e.id for e in accepted}
        return [e for e in accepted if not isinstance(e, Opening) or e.host_id in ids]

    def summary(self) -> dict[str, int]:
        return dict(Counter(p.kind for p in self.proposals))


class PlanConverter:
    def __init__(self, settings: ConversionSettings | None = None) -> None:
        self.settings = settings or ConversionSettings()
        self._wall_layers: tuple[str, ...] = self.settings.wall_layers

    def run(self, drawing: Drawing2D, storey: Storey) -> ConversionResult:
        s = self.settings
        warnings: list[str] = []
        unit = self._resolve_unit(drawing, warnings)
        k = unit.to_metres

        self._wall_layers = s.wall_layers or self._auto_wall_layers(drawing, k, warnings)
        everything = self._all_segments(drawing, k)
        # Stairs first: their treads would otherwise be paired into a stack of thin "walls".
        stairs = detect_stairs(everything)
        treads = {_key(everything[i]) for stair in stairs for i in stair.used}
        segments, wide = self._wall_candidates_input(drawing, k, warnings)
        segments = [x for x in segments if _key(x) not in treads]
        layer_scores = self._layer_scores(drawing)
        blocks = [b for b in drawing.of_type(BlockRef) if drawing.is_visible(b)]
        sizes = self._block_sizes(drawing, k)
        filled = [x for x in drawing.of_type(Polyline) if x.filled and drawing.is_visible(x)
                  and self._block_allowed(x.block, sizes)]
        filled_walls, columns = filled_elements(filled, k, s)

        walls = pair_wall_faces(segments, s, layer_scores) + wide + filled_walls
        hatching = [x for x in everything if x.length <= 1.0 and not self._is_wall_layer(x.layer)
                    and _key(x) not in treads]
        walls += hatch_walls(walls, [x for x in segments if x.length >= 1.0], hatching, s)
        walls = [w for w in walls if not _inside_any_column(w, columns)]
        walls = merge_collinear(walls, s.max_junction_gap, s)
        walls = join_corners(walls)
        walls = _demote_isolated(walls)
        symbols = symbols_from_blocks(blocks, k)
        symbols += without_duplicates(symbols, symbols_from_arcs(self._loose_arcs(drawing), k))
        symbols += without_duplicates(symbols, symbols_from_arcs(arcs_from_chains(everything), 1.0))
        lines = [(seg.start, seg.end) for seg in segments]
        walls, windows = find_infill_windows(walls, lines, symbols, s)
        walls, openings, unplaced = place_openings(walls, symbols + windows, s)
        warnings += _unplaced_notes(unplaced)

        notes = [((t.position[0] * k, t.position[1] * k), t.text) for t in drawing.of_type(Text)]
        stairs = link_flights(stairs, walls, notes)
        orient_by_notes(stairs, notes)
        walls = walls_clear_of_stairs(walls, stairs)
        self._labels = group_text(drawing.of_type(Text), k)

        proposals = self._wall_proposals(walls, storey)
        proposals += self._opening_proposals(openings, proposals, storey, warnings)
        proposals += self._room_and_slab_proposals(walls, storey)
        if walls:  # stairs and columns outside the building are legends, tables or key plans
            xs = [c for w in walls for c in (w.start[0], w.end[0])]
            ys = [c for w in walls for c in (w.start[1], w.end[1])]
            box = (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)
            stairs = [x for x in stairs if _within(x.centre, box)]
            columns = [x for x in columns if _within(x.position, box)]
        proposals += self._stair_proposals(stairs, storey)
        proposals += self._column_proposals(columns, storey)
        duct_proposals, systems, duct_candidates = self._duct_proposals(drawing, everything, walls, k, storey)
        proposals += duct_proposals
        proposals += self._hole_proposals(drawing, everything, duct_candidates, proposals, storey)
        top = max((p.element.path[0][2] + p.element.depth / 2 for p in duct_proposals), default=0.0)
        if walls and top > s.wall_height:
            warnings.append(f"Ducts reach {top:.2f} m but walls are {s.wall_height:g} m high, so they float above the "
                            "walls. Set the real wall height in Edit > Conversion settings.")
        if not walls:
            warnings.append(self._no_walls_hint(drawing, unit))
        return ConversionResult(proposals, unit, warnings, systems, self._labels, project_title(self._labels),
                                level_from_title(self._labels))

    # --- steps ---------------------------------------------------------------

    def _is_wall_layer(self, layer: str) -> bool:
        if self._wall_layers:
            return layer in self._wall_layers
        return classify_name(layer) in (LayerRole.WALL, LayerRole.UNKNOWN)

    def _layer_scores(self, drawing: Drawing2D) -> dict[str, float]:
        if self._wall_layers:
            return {name: 1.0 for name in self._wall_layers}
        return {name: WALL_LAYER_SCORE.get(classify_name(name), 0.0) for name in drawing.layers}

    def _auto_wall_layers(self, drawing: Drawing2D, k: float, warnings: list[str]) -> tuple[str, ...]:
        """Pick the wall layer when names give no clue (PDF line styles, numbered CAD layers).

        Each busy layer is tried on its own and scored by
            length of plausible walls (90-500 mm thick, >= 1 m long)
            x share of wall ends that meet another wall (walls form a network; table rows do not)
            x share of that length in its most common thickness (real walls repeat a few thicknesses;
              tables, text frames and hatching do not)
            x (1 + rooms they enclose / 5).
        A layer is chosen only if it clearly beats the runner-up.
        """
        s = self.settings
        by_layer: dict[str, list[Segment]] = {}
        for seg in drawing.segments():
            if drawing.is_visible(seg) and not seg.block:
                by_layer.setdefault(seg.layer, []).append(seg)
        if len(by_layer) < AUTO_LAYER_MIN_LAYERS or any(classify_name(n) is LayerRole.WALL for n in by_layer):
            return ()
        scores: dict[str, float] = {}
        busiest = sorted(by_layer, key=lambda n: -len(by_layer[n]))[:AUTO_LAYER_CANDIDATES]
        for name in busiest:
            if classify_name(name) is not LayerRole.UNKNOWN:
                continue
            walls = merge_collinear(pair_wall_faces([x.scaled(k) for x in by_layer[name]], s), s.max_junction_gap, s)
            plausible = [w for w in walls if 0.09 <= w.thickness <= 0.5 and w.length >= 1.0]
            total = sum(w.length for w in plausible)
            if total < 5.0:
                continue
            bins: Counter[int] = Counter()
            for w in plausible:
                bins[round(w.thickness / 0.02)] += w.length
            joined = join_corners(walls)
            rooms = len(detect_rooms(joined, s.min_room_area))
            scores[name] = (total * (max(bins.values()) / total) * _connectedness(joined)
                            * (1 + min(rooms, 10) / 5))
        ranked = sorted(scores, key=scores.__getitem__, reverse=True)
        if not ranked or (len(ranked) > 1 and scores[ranked[0]] < 2 * scores[ranked[1]]):
            return ()
        chosen = [ranked[0]]
        if " pt " in ranked[0]:
            colour = ranked[0].split(" pt ", 1)[1]
            chosen += sorted(n for n in by_layer if n != ranked[0] and n.split(" pt ", 1)[-1] == colour)
        warnings.append(f"Walls taken from line style/layer {', '.join(repr(n) for n in chosen)} (the most "
                        "wall-like). Change this in Edit > Conversion settings if needed.")
        return tuple(chosen)

    def _all_segments(self, drawing: Drawing2D, k: float) -> list[Segment]:
        """Every visible straight line in metres, except annotation, grids and symbol blocks."""
        sizes = self._block_sizes(drawing, k)
        return [seg.scaled(k) for seg in drawing.segments()
                if drawing.is_visible(seg) and self._block_allowed(seg.block, sizes)
                and classify_name(seg.layer) not in (LayerRole.ANNOTATION, LayerRole.GRID)]

    def _stair_proposals(self, stairs: list, storey: Storey) -> list[Proposal]:
        out = []
        for n, c in enumerate(stairs, start=1):
            total = c.risers + sum(f[3] for f in c.further)
            # Risers of ~170 mm, but never taller than the level.
            height = round(min(self.settings.wall_height, total * 0.17), 3)
            name = stair_name(c.centre, self._labels) or f"Stair {n}"
            stair = Stair(name=name, storey_id=storey.id, start=_r(c.start), end=_r(c.end),
                          width=round(c.width, 3), risers=c.risers, height=height,
                          further_flights=[(*_r(a), *_r(b), round(w, 3), r) for a, b, w, r in c.further])
            shape = f"{len(c.further) + 1} flights with landing" + ("s" if len(c.further) > 1 else "") + ", " if c.further else ""
            check = "check height" if c.direction_known else "check direction (Edit > Flip stair direction) and height"
            note = f"{shape}{total} treads, going {stair.going * 1000:.0f} mm; {check}"
            out.append(Proposal(stair, c.confidence, c.confidence >= self.settings.accept_threshold, note=note))
        return out

    def _column_proposals(self, columns: list, storey: Storey) -> list[Proposal]:
        out = []
        for n, c in enumerate(columns, start=1):
            column = Column(name=column_tag(c.position, self._labels) or f"C{n}", storey_id=storey.id, position=_r(c.position), width=round(c.width, 3),
                            depth=round(c.depth, 3), rotation=round(c.rotation, 2), height=self.settings.wall_height)
            out.append(Proposal(column, c.confidence, c.confidence >= self.settings.accept_threshold,
                                note="filled rectangle"))
        return out

    def _duct_proposals(self, drawing: Drawing2D, everything: list[Segment], walls: list[WallCandidate], k: float,
                        storey: Storey) -> tuple[list[Proposal], list[DistributionSystem]]:
        lines = [x for x in everything if not self._is_named_wall(x.layer)]
        labels = parse_labels(drawing.of_type(Text), k)
        duct_layers = sorted({x.layer for x in lines if classify_name(x.layer) is LayerRole.DUCT})
        candidates = detect_ducts(lines, labels, self.settings, duct_layers)
        if walls:  # legend swatches and key plans lie outside the building
            xs = [c for w in walls for c in (w.start[0], w.end[0])]
            ys = [c for w in walls for c in (w.start[1], w.end[1])]
            box = (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)
            candidates = [c for c in candidates if _within(((c.start[0] + c.end[0]) / 2, (c.start[1] + c.end[1]) / 2), box)]
        systems: dict[str, DistributionSystem] = {}
        out = []
        for n, c in enumerate(candidates, start=1):
            system = None
            if c.kind in AIR_SYSTEMS:
                system_type, name, colour = AIR_SYSTEMS[c.kind]
                system = systems.setdefault(c.kind, DistributionSystem(name=name, system_type=system_type, color=colour))
            z = round(c.bottom + c.depth / 2, 4)  # the path runs along the duct's centre
            duct = DuctSegment(name=_duct_name(c, n), storey_id=storey.id, width=c.width, depth=c.depth,
                               system_id=system.id if system else None, end_width=c.end_width,
                               path=[(*_r(c.start), z), (*_r(c.end), z)])
            source = "size from label" if c.labelled else "no matching label: size and level assumed"
            note = f"{c.width * 1000:.0f} x {c.depth * 1000:.0f} mm, bottom {c.bottom * 1000:.0f} mm; {source}"
            if c.end_width:
                note = f"transition {c.width * 1000:.0f} to {c.end_width * 1000:.0f} mm, between two labelled runs"
            out.append(Proposal(duct, c.confidence, c.confidence >= self.settings.accept_threshold, note=note))
        return out, list(systems.values()), candidates

    def _hole_proposals(self, drawing: Drawing2D, everything: list[Segment], ducts: list, proposals: list[Proposal],
                        storey: Storey) -> list[Proposal]:
        """Holes in walls for louvres (first, they are exact) and for ducts passing through."""
        if not ducts:
            return []
        walls = [p for p in proposals if isinstance(p.element, Wall)]
        elements = [p.element for p in walls]
        bottoms = sorted(c.bottom for c in ducts)
        colours = {c.colour for c in ducts}
        sizes = parse_louvre_sizes(drawing.of_type(Text))
        holes = find_louvres(everything, colours, sizes, elements, bottoms[len(bottoms) // 2])
        holes += duct_wall_holes(ducts, elements)
        existing = [p.element for p in proposals if isinstance(p.element, Opening)]
        out: list[Proposal] = []
        for n, hole in enumerate(holes, start=1):
            host = walls[hole.wall_index]
            opening = Opening(name=f"{hole.kind} opening {n}", storey_id=storey.id, host_id=host.element.id,
                              offset=hole.offset, width=hole.width, height=hole.height, sill_height=hole.sill)
            span = opening.span
            if hole.kind == "Louvre" and 0 <= hole.offset <= host.element.length:
                opening.name = louvre_note(host.element.point_at(hole.offset), self._labels) or opening.name
            if span[0] < 0 or span[1] > host.element.length or opening.sill_height + opening.height > host.element.height:
                continue
            # The wall builder cannot stack openings, so any overlap along the wall conflicts.
            if any(self._overlaps(opening, other) for other in existing):
                continue
            existing.append(opening)
            confidence = min(0.7, host.confidence)
            out.append(Proposal(opening, confidence, confidence >= self.settings.accept_threshold,
                                note=f"{hole.width * 1000:.0f} x {hole.height * 1000:.0f} mm at "
                                     f"{hole.sill * 1000:.0f} mm; check the sill"))
        return out

    def _is_named_wall(self, layer: str) -> bool:
        return layer in self._wall_layers or classify_name(layer) is LayerRole.WALL

    def _block_sizes(self, drawing: Drawing2D, k: float) -> dict[str, float]:
        sizes: dict[str, float] = {}
        for ref in drawing.of_type(BlockRef):
            if ref.extent is not None:
                (x0, y0), (x1, y1) = ref.extent
                size = max(abs((x1 - x0) * ref.scale[0]), abs((y1 - y0) * ref.scale[1])) * k
                sizes[ref.name] = max(sizes.get(ref.name, 0.0), size)
        return sizes

    def _loose_arcs(self, drawing: Drawing2D) -> list[Arc]:
        """Arcs not inside door/window blocks (those are already symbols)."""
        return [a for a in drawing.of_type(Arc) if drawing.is_visible(a)
                and classify_name(a.block) not in (LayerRole.DOOR, LayerRole.WINDOW)
                and classify_name(a.layer) not in (LayerRole.ANNOTATION, LayerRole.FURNITURE, LayerRole.GRID)]

    def _block_allowed(self, block: str, sizes: dict[str, float]) -> bool:
        """Lines exploded from door/window/furniture symbols must not become walls."""
        if not block:
            return True
        role = classify_name(block)
        if role is LayerRole.WALL:
            return True
        if role is not LayerRole.UNKNOWN:
            return False
        return sizes.get(block, math.inf) >= self.settings.min_wall_block_size

    def _wall_candidates_input(
        self, drawing: Drawing2D, k: float, warnings: list[str]
    ) -> tuple[list, list[WallCandidate]]:
        """Face segments to pair, plus walls drawn directly as wide polylines."""
        s = self.settings
        sizes = self._block_sizes(drawing, k)
        segments = []
        wide: list[WallCandidate] = []
        for primitive in drawing.primitives:
            if not drawing.is_visible(primitive) or not self._is_wall_layer(primitive.layer):
                continue
            if not self._block_allowed(primitive.block, sizes):
                continue
            if isinstance(primitive, Polyline) and s.min_wall_thickness <= primitive.width * k <= s.max_wall_thickness:
                for seg in primitive.segments():
                    seg = seg.scaled(k)
                    if seg.length >= s.min_wall_overlap:
                        wide.append(WallCandidate(seg.start, seg.end, primitive.width * k, 0.85, (seg.handle,)))
                continue
            if isinstance(primitive, Segment):
                segments.append(primitive.scaled(k))
            elif isinstance(primitive, Polyline):
                segments.extend(seg.scaled(k) for seg in primitive.segments())
        if wide:
            warnings.append(f"{len(wide)} wall segments read from wide polylines (centreline + width)")
        return segments, wide

    def _no_walls_hint(self, drawing: Drawing2D, unit: LengthUnit) -> str:
        counts = Counter(seg.layer for seg in drawing.segments() if drawing.is_visible(seg))
        busiest = ", ".join(f"{name} ({n})" for name, n in counts.most_common(6)) or "none"
        chosen = f" Wall layers chosen: {', '.join(self.settings.wall_layers)}." if self.settings.wall_layers else ""
        return (
            "No walls detected. Walls must be drawn as pairs of parallel lines (or wide polylines) "
            f"{self.settings.min_wall_thickness * 1000:.0f}-{self.settings.max_wall_thickness * 1000:.0f} mm apart."
            f"{chosen} Drawing units: {unit.symbol}. Layers with the most lines: {busiest}. "
            "Pick the wall layers and check the units in Edit > Conversion settings."
        )

    def _resolve_unit(self, drawing: Drawing2D, warnings: list[str]) -> LengthUnit:
        if self.settings.unit_override:
            return self.settings.unit_override
        if drawing.unit:
            return drawing.unit
        extent = drawing.extent()
        size = max(extent[1][0] - extent[0][0], extent[1][1] - extent[0][1]) if extent else 0.0
        unit = guess_unit_from_extent(size)
        warnings.append(f"Drawing has no units; assumed {unit.symbol} from its extent. Override if wrong.")
        return unit

    def _wall_proposals(self, walls: list[WallCandidate], storey: Storey) -> list[Proposal]:
        out = []
        for n, w in enumerate(walls, start=1):
            wall = Wall(
                name=f"Wall {n}",
                storey_id=storey.id,
                start=_r(w.start),
                end=_r(w.end),
                thickness=round(w.thickness, 4),
                height=self.settings.wall_height,
            )
            out.append(Proposal(wall, w.confidence, w.confidence >= self.settings.accept_threshold, w.sources))
        return out

    def _opening_proposals(
        self,
        openings: list[OpeningCandidate],
        wall_proposals: list[Proposal],
        storey: Storey,
        warnings: list[str],
    ) -> list[Proposal]:
        s = self.settings
        out: list[Proposal] = []
        skipped: Counter[str] = Counter()
        for c in sorted(openings, key=lambda o: -o.confidence):
            host = self._find_host(c.center, wall_proposals)
            if host is None:
                warnings.append(f"Could not host {c.kind} {c.handle}")
                continue
            wall: Wall = host.element  # type: ignore[assignment]
            width = min(c.width, wall.length)
            offset = min(max(v.dot(v.sub(c.center, wall.start), wall.direction), width / 2), wall.length - width / 2)
            if c.kind == "door":
                el: Opening = Door(host_id=wall.id, offset=offset, width=width, height=min(s.door_height, wall.height))
            else:
                height = min(s.window_height, wall.height - s.window_sill_height)
                if height <= 0:
                    warnings.append(f"Window {c.handle} does not fit below the wall height (sill "
                                    f"{s.window_sill_height:g} m); skipped")
                    continue
                el = Window(host_id=wall.id, offset=offset, width=width, height=height, sill_height=s.window_sill_height)
            el.storey_id = storey.id
            if any(self._overlaps(el, p.element) for p in out):
                skipped[c.kind] += 1
                continue
            confidence = min(c.confidence, host.confidence)
            note = "placed in wall gap" if c.in_gap else "placed from symbol position"
            out.append(Proposal(el, confidence, confidence >= s.accept_threshold, (c.handle,), note))

        for kind, count in skipped.items():
            warnings.append(f"{count} {kind} symbols overlap another opening in the same wall, so they were left out")
        hosts = {w.element.id: w.element for w in wall_proposals}
        counters: Counter[str] = Counter()
        for p in out:
            counters[p.kind] += 1
            wall = hosts[p.element.host_id]
            at = wall.point_at(p.element.offset)
            tag = door_tag(at, self._labels) if isinstance(p.element, Door) else window_tag(at, self._labels)
            p.element.name = tag or f"{p.kind} {counters[p.kind]}"
        return out

    @staticmethod
    def _find_host(point, wall_proposals: list[Proposal]) -> Proposal | None:
        best, best_d = None, math.inf
        for p in wall_proposals:
            w: Wall = p.element  # type: ignore[assignment]
            d = v.point_segment_distance(point, w.start, w.end)
            if d <= w.thickness / 2 + HOST_TOLERANCE and d < best_d:
                best, best_d = p, d
        return best

    @staticmethod
    def _overlaps(a: Opening, b: Element) -> bool:
        if not isinstance(b, Opening) or a.host_id != b.host_id:
            return False
        (a0, a1), (b0, b1) = a.span, b.span
        return a0 < b1 and b0 < a1

    def _room_and_slab_proposals(self, walls: list[WallCandidate], storey: Storey) -> list[Proposal]:
        s = self.settings
        rooms = detect_rooms(walls, s.min_room_area)
        if not rooms:
            return []
        # Rooms and slabs are derived from walls, so they can't be more certain than them.
        derived = min(0.8, sum(w.confidence for w in walls) / len(walls))
        out = [
            Proposal(
                Space(name=room_name(poly, self._labels) or f"Room {n}", storey_id=storey.id,
                      boundary=[_r(p) for p in ring(poly)], height=s.wall_height),
                derived,
                derived >= s.accept_threshold,
                note=f"{poly.area:.2f} m2 (centreline area)",
            )
            for n, poly in enumerate(rooms, start=1)
        ]
        outline = footprint(rooms, walls)
        if outline:
            slab = Slab(name="Floor slab", storey_id=storey.id, boundary=[_r(p) for p in outline], thickness=s.slab_thickness)
            out.append(Proposal(slab, derived, derived >= s.accept_threshold, note="from building outline"))
        return out
