"""Orchestrates Drawing2D -> reviewed proposals -> parametric elements."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

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
from bimforge.conversion.fills import filled_elements
from bimforge.conversion.stairs import detect_stairs
from bimforge.conversion.walls import WallCandidate, hatch_walls, join_corners, merge_collinear, pair_wall_faces
from bimforge.core.model import Column, Door, Element, Opening, Slab, Space, Stair, Storey, Wall, Window
from bimforge.core.units import LengthUnit, guess_unit_from_extent
from bimforge.drawing.primitives import Arc, BlockRef, Drawing2D, Polyline, Segment
from bimforge.geometry import vec2 as v
from bimforge.geometry.spatial import segment_distances

HOST_TOLERANCE = 0.05
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
        filled = [x for x in drawing.of_type(Polyline) if x.filled and drawing.is_visible(x)
                  and self._block_allowed(x.block, self._block_sizes(drawing, k))]
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
        for symbol in unplaced:
            warnings.append(f"{symbol.kind.title()} symbol '{symbol.name}' ({symbol.handle}) is not in any wall")

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
        if not walls:
            warnings.append(self._no_walls_hint(drawing, unit))
        return ConversionResult(proposals, unit, warnings)

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
            # Risers of ~170 mm, but never taller than the level.
            height = round(min(self.settings.wall_height, c.risers * 0.17), 3)
            stair = Stair(name=f"Stair {n}", storey_id=storey.id, start=_r(c.start), end=_r(c.end),
                          width=round(c.width, 3), risers=c.risers, height=height)
            note = f"{c.risers} treads, going {stair.going * 1000:.0f} mm; check direction and height"
            out.append(Proposal(stair, c.confidence, c.confidence >= self.settings.accept_threshold, note=note))
        return out

    def _column_proposals(self, columns: list, storey: Storey) -> list[Proposal]:
        out = []
        for n, c in enumerate(columns, start=1):
            column = Column(name=f"C{n}", storey_id=storey.id, position=_r(c.position), width=round(c.width, 3),
                            depth=round(c.depth, 3), rotation=round(c.rotation, 2), height=self.settings.wall_height)
            out.append(Proposal(column, c.confidence, c.confidence >= self.settings.accept_threshold,
                                note="filled rectangle"))
        return out

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
        sizes: dict[str, float] = {}
        for ref in drawing.of_type(BlockRef):
            if ref.extent is not None:
                (x0, y0), (x1, y1) = ref.extent
                size = max(abs((x1 - x0) * ref.scale[0]), abs((y1 - y0) * ref.scale[1])) * k
                sizes[ref.name] = max(sizes.get(ref.name, 0.0), size)
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
                el = Window(host_id=wall.id, offset=offset, width=width, height=height, sill_height=s.window_sill_height)
            el.storey_id = storey.id
            if any(self._overlaps(el, p.element) for p in out):
                warnings.append(f"{c.kind.title()} {c.handle} overlaps another opening; skipped")
                continue
            confidence = min(c.confidence, host.confidence)
            note = "placed in wall gap" if c.in_gap else "placed from symbol position"
            out.append(Proposal(el, confidence, confidence >= s.accept_threshold, (c.handle,), note))

        counters: Counter[str] = Counter()
        for p in out:
            counters[p.kind] += 1
            p.element.name = f"{p.kind} {counters[p.kind]}"
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
                Space(name=f"Room {n}", storey_id=storey.id, boundary=[_r(p) for p in ring(poly)], height=s.wall_height),
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
