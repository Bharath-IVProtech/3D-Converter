"""Door and window placement from block symbols.

A symbol is placed in the wall gap it sits in (merging the two wall pieces
around the gap), or, failing that, inside the wall it overlaps.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from bimforge.conversion.layers import LayerRole, classify_block, classify_name
from bimforge.conversion.settings import ConversionSettings
from bimforge.conversion.walls import WallCandidate, merge_pair, collinear_gap
from bimforge.core.model import Point2
from bimforge.drawing.primitives import Arc, BlockRef
from bimforge.geometry import vec2 as v
from bimforge.geometry.spatial import segment_distances

OpeningKind = Literal["door", "window"]

# Max distance (beyond half the wall thickness) between a symbol anchor and its wall.
SYMBOL_REACH = 0.3
MIN_OPENING_GAP = 0.3


@dataclass
class OpeningSymbol:
    kind: OpeningKind
    anchors: tuple[Point2, ...]  # insertion point and symbol centre, metres
    width: float | None
    confidence: float
    handle: str
    name: str


@dataclass
class OpeningCandidate:
    kind: OpeningKind
    center: Point2
    width: float
    confidence: float
    handle: str
    in_gap: bool


def symbols_from_blocks(blocks: Sequence[BlockRef], scale: float) -> list[OpeningSymbol]:
    symbols = []
    for block in blocks:
        role = classify_block(block.name, block.layer)
        if role not in (LayerRole.DOOR, LayerRole.WINDOW):
            continue
        named = classify_name(block.name) is role
        anchors = tuple(v.scale(p, scale) for p in (block.position, block.world_center))
        symbols.append(
            OpeningSymbol(
                kind="door" if role is LayerRole.DOOR else "window",
                anchors=anchors,
                width=block.width * scale if block.width else None,
                confidence=0.9 if named else 0.75,
                handle=block.handle,
                name=block.name,
            )
        )
    return symbols


def _anchor_distance(symbol: OpeningSymbol, a: Point2, b: Point2) -> float:
    return min(v.point_segment_distance(p, a, b) for p in symbol.anchors)


def place_openings(
    walls: Sequence[WallCandidate],
    symbols: Sequence[OpeningSymbol],
    settings: ConversionSettings,
) -> tuple[list[WallCandidate], list[OpeningCandidate], list[OpeningSymbol]]:
    """Returns (walls after merging across openings, placed openings, unplaced symbols)."""
    cos_tol = math.cos(math.radians(settings.parallel_tolerance_deg))
    walls = list(walls)
    placed: list[OpeningCandidate] = []
    unplaced: list[OpeningSymbol] = []

    for symbol in symbols:
        best: tuple[float, float, int, int, Point2, Point2] | None = None
        # Only walls that could border a gap next to this symbol are worth pairing.
        starts = np.array([w.start for w in walls], dtype=np.float64).reshape(-1, 2)
        ends = np.array([w.end for w in walls], dtype=np.float64).reshape(-1, 2)
        reach = settings.max_opening_gap + settings.max_wall_thickness + SYMBOL_REACH
        near = set()
        for anchor in symbol.anchors:
            near.update(np.nonzero(segment_distances(anchor, starts, ends) <= reach)[0].tolist())
        nearby = sorted(near)
        for i in nearby:
            for j in nearby:
                if i == j:
                    continue
                hit = collinear_gap(walls[i], walls[j], cos_tol)
                if hit is None or not MIN_OPENING_GAP <= hit[0] <= settings.max_opening_gap:
                    continue
                dist = _anchor_distance(symbol, hit[1], hit[2])
                # Ties (anchor inside several nested gaps) go to the narrowest gap, i.e. adjacent walls,
                # so a wider gap never swallows a wall piece lying between its two walls.
                if dist <= walls[i].thickness / 2 + SYMBOL_REACH and (best is None or (dist, hit[0]) < best[:2]):
                    best = (dist, hit[0], i, j, hit[1], hit[2])
        if best is not None:
            _, _, i, j, g0, g1 = best
            walls[i] = merge_pair(walls[i], walls[j])
            del walls[j]
            centre = ((g0[0] + g1[0]) / 2, (g0[1] + g1[1]) / 2)
            placed.append(
                OpeningCandidate(symbol.kind, centre, math.dist(g0, g1), symbol.confidence, symbol.handle, True)
            )
            continue

        hosted = _host_in_wall(symbol, walls, settings)
        if hosted is not None:
            placed.append(hosted)
        else:
            unplaced.append(symbol)
    return walls, placed, unplaced


def _host_in_wall(
    symbol: OpeningSymbol, walls: Sequence[WallCandidate], settings: ConversionSettings
) -> OpeningCandidate | None:
    best: tuple[float, Point2] | None = None
    # All walls at once: this runs for every unplaced symbol, and big plans have thousands of walls.
    starts = np.array([w.start for w in walls], dtype=np.float64).reshape(-1, 2)
    directions = np.array([w.direction for w in walls], dtype=np.float64).reshape(-1, 2)
    lengths = np.array([w.length for w in walls], dtype=np.float64)
    reach = np.array([w.thickness / 2 + SYMBOL_REACH for w in walls], dtype=np.float64)
    for anchor in reversed(symbol.anchors):  # prefer the symbol centre
        offset = np.asarray(anchor, dtype=np.float64) - starts
        t = offset[:, 0] * directions[:, 0] + offset[:, 1] * directions[:, 1]
        lateral = np.abs(-offset[:, 0] * directions[:, 1] + offset[:, 1] * directions[:, 0])
        fits = np.flatnonzero((t >= 0) & (t <= lengths) & (lateral <= reach))
        if len(fits):
            i = fits[np.argmin(lateral[fits])]
            if best is None or lateral[i] < best[0]:
                best = (float(lateral[i]), (float(starts[i, 0] + directions[i, 0] * t[i]),
                                            float(starts[i, 1] + directions[i, 1] * t[i])))
    if best is None:
        return None
    default = settings.default_door_width if symbol.kind == "door" else settings.default_window_width
    # Lower confidence than a gap match: width and position come from the symbol alone.
    return OpeningCandidate(
        symbol.kind, best[1], symbol.width or default, symbol.confidence * 0.75, symbol.handle, False
    )


# --- symbols drawn as raw geometry (no blocks): typical of PDFs and hand-drafted DXFs -------

DOOR_RADIUS_RANGE = (0.5, 1.3)  # m: single-leaf door widths
DOOR_SWEEP_RANGE = (75.0, 105.0)  # degrees: a quarter-circle swing
INFILL_COVERAGE = 0.7  # glazing lines must span this share of the gap
INFILL_OVERHANG = 0.15  # ...and stay within it (m)


def symbols_from_arcs(arcs: Sequence[Arc], scale: float) -> list[OpeningSymbol]:
    """Door swings: quarter-circle arcs about the hinge, radius = leaf width."""
    symbols = []
    for arc in arcs:
        radius = arc.radius * scale
        sweep = (arc.end_angle - arc.start_angle) % 360
        if not (DOOR_RADIUS_RANGE[0] <= radius <= DOOR_RADIUS_RANGE[1]
                and DOOR_SWEEP_RANGE[0] <= sweep <= DOOR_SWEEP_RANGE[1]):
            continue
        cx, cy = arc.center[0] * scale, arc.center[1] * scale
        ends = [(cx + radius * math.cos(math.radians(a)), cy + radius * math.sin(math.radians(a)))
                for a in (arc.start_angle, arc.end_angle)]
        # The hinge sits at one jamb; one end of the swing reaches the other jamb.
        symbols.append(OpeningSymbol("door", ((cx, cy), *ends), radius, 0.7, arc.handle, "door swing arc"))
    return symbols


def without_duplicates(primary: Sequence[OpeningSymbol], extra: Sequence[OpeningSymbol],
                       distance: float = 0.3) -> list[OpeningSymbol]:
    """Extra symbols that are not already represented by a primary symbol nearby."""
    return [s for s in extra
            if not any(math.dist(a, b) <= distance for p in primary for a in p.anchors for b in s.anchors)]


def find_infill_windows(
    walls: Sequence[WallCandidate],
    lines: Sequence[tuple[Point2, Point2]],
    symbols: Sequence[OpeningSymbol],
    settings: ConversionSettings,
) -> tuple[list[WallCandidate], list[OpeningSymbol]]:
    """Windows drawn as glazing lines across a wall gap (no window block).

    A gap between two collinear walls is a window when lines (or thin walls
    paired from them) run along the wall inside the gap and span most of it.
    Thin walls used as glazing are removed. Gaps already claimed by a symbol
    (e.g. a door swing) are skipped. Returns (walls, window symbols).
    """
    from bimforge.conversion.walls import _line_groups

    cos_tol = math.cos(math.radians(settings.parallel_tolerance_deg))
    taken = [a for s in symbols for a in s.anchors]
    consumed: set[int] = set()
    found: list[OpeningSymbol] = []
    # Boxes around every wall and line: only what lies within a gap's own box can be its glazing.
    wall_boxes = np.array([(*w.start, *w.end) for w in walls], dtype=np.float64).reshape(-1, 4)
    usable_lines = [ln for ln in lines if math.dist(*ln) >= 0.2]
    line_boxes = np.array([(*p, *q) for p, q in usable_lines], dtype=np.float64).reshape(-1, 4)

    def within(boxes: np.ndarray, x0: float, y0: float, x1: float, y1: float, margin: float) -> np.ndarray:
        xs, ys = boxes[:, [0, 2]], boxes[:, [1, 3]]
        return np.flatnonzero((xs.min(axis=1) >= x0 - margin) & (xs.max(axis=1) <= x1 + margin)
                              & (ys.min(axis=1) >= y0 - margin) & (ys.max(axis=1) <= y1 + margin))

    for group in _line_groups(walls, settings):
        ux, uy = walls[group[0]].direction
        along = sorted(group, key=lambda i: min(ux * p[0] + uy * p[1] for p in (walls[i].start, walls[i].end)))
        for a, b in zip(along, along[1:]):
            hit = collinear_gap(walls[a], walls[b], cos_tol)
            if hit is None or not MIN_OPENING_GAP <= hit[0] <= settings.max_opening_gap:
                continue
            g0, g1 = hit[1], hit[2]
            host = walls[a]
            band = host.thickness / 2 + 0.01
            if any(v.point_segment_distance(p, g0, g1) <= band + SYMBOL_REACH for p in taken):
                continue
            u = v.unit(v.sub(g1, g0))
            n = v.left_normal(u)
            gap = hit[0]

            def spans(p: Point2, q: Point2) -> float:
                """Share of the gap covered by p-q if it runs along the wall inside the band, else 0."""
                if abs(v.dot(v.unit(v.sub(q, p)), u)) < cos_tol:
                    return 0.0
                if max(abs(v.dot(v.sub(x, g0), n)) for x in (p, q)) > band:
                    return 0.0
                t = sorted((v.dot(v.sub(p, g0), u), v.dot(v.sub(q, g0), u)))
                if t[0] < -INFILL_OVERHANG or t[1] > gap + INFILL_OVERHANG:
                    return 0.0  # runs past the gap: a wall or other line, not glazing in this opening
                return max(0.0, min(gap, t[1]) - max(0.0, t[0])) / gap

            box = (min(g0[0], g1[0]), min(g0[1], g1[1]), max(g0[0], g1[0]), max(g0[1], g1[1]),
                   band + INFILL_OVERHANG + 1e-6)
            thin = [int(i) for i in within(wall_boxes, *box)
                    if walls[i].thickness < host.thickness - 0.005 and spans(walls[i].start, walls[i].end) >= INFILL_COVERAGE]
            glazed = thin or [usable_lines[i] for i in within(line_boxes, *box) if spans(*usable_lines[i]) >= INFILL_COVERAGE]
            if not glazed:
                continue
            consumed.update(thin)
            centre = ((g0[0] + g1[0]) / 2, (g0[1] + g1[1]) / 2)
            found.append(OpeningSymbol("window", (centre,), gap, 0.7, "", "glazing lines"))
    return [w for i, w in enumerate(walls) if i not in consumed], found


CHAIN_SEGMENT = (0.01, 0.3)  # m: pieces of an arc flattened into straight lines
CHAIN_MIN_PIECES = 5
CHAIN_BREAK_TURN = math.radians(30)  # a sharper turn ends one arc (e.g. where two door leaves meet)
CHAIN_FIT_TOLERANCE = 0.03  # RMS distance from the fitted circle, as a share of the radius


def _fit_circle(points: Sequence[Point2]) -> tuple[Point2, float, float] | None:
    """Least-squares (Kasa) circle fit: (centre, radius, relative RMS error)."""
    xs = np.array([p[0] for p in points])
    ys = np.array([p[1] for p in points])
    a = np.column_stack([xs, ys, np.ones_like(xs)])
    b = xs**2 + ys**2
    try:
        (c0, c1, c2), *_ = np.linalg.lstsq(a, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    cx, cy = c0 / 2, c1 / 2
    r2 = c2 + cx**2 + cy**2
    if r2 <= 0:
        return None
    r = math.sqrt(r2)
    rms = math.sqrt(float(np.mean((np.hypot(xs - cx, ys - cy) - r) ** 2)))
    return (float(cx), float(cy)), r, rms / r


def arcs_from_chains(segments: Sequence["Segment"]) -> list[Arc]:
    """Arcs drawn as chains of short straight lines (how many PDF exports flatten door swings).

    Segments must be in metres. Chains are split at sharp turns, then each
    piece that fits a circle well becomes an Arc.
    """
    def key(p: Point2) -> tuple[int, int]:
        return (round(p[0] * 1000), round(p[1] * 1000))  # millimetre snapping joins the pieces

    edges: dict[tuple, tuple] = {}
    for seg in segments:
        if CHAIN_SEGMENT[0] <= seg.length <= CHAIN_SEGMENT[1]:
            a, b = key(seg.start), key(seg.end)
            if a != b:
                edges[(a, b) if a < b else (b, a)] = (seg.start, seg.end)  # drops lines drawn twice
    neighbours: dict[tuple, list[tuple]] = {}
    points: dict[tuple, Point2] = {}
    for (a, b), (pa, pb) in edges.items():
        neighbours.setdefault(a, []).append(b)
        neighbours.setdefault(b, []).append(a)
        points[a], points[b] = pa, pb

    seen: set[tuple] = set()
    chains: list[list[tuple]] = []
    starts = [n for n, adj in neighbours.items() if len(adj) != 2] + list(neighbours)
    for start in starts:
        for first in neighbours[start]:
            edge = (start, first) if start < first else (first, start)
            if edge in seen:
                continue
            chain, prev, cur = [start, first], start, first
            seen.add(edge)
            while len(neighbours[cur]) == 2:
                nxt = neighbours[cur][0] if neighbours[cur][1] == prev else neighbours[cur][1]
                edge = (cur, nxt) if cur < nxt else (nxt, cur)
                if edge in seen:
                    break
                seen.add(edge)
                chain.append(nxt)
                prev, cur = cur, nxt
            chains.append(chain)

    arcs: list[Arc] = []
    for chain in chains:
        pts = [points[n] for n in chain]
        piece = [pts[0]]
        for i in range(1, len(pts)):
            piece.append(pts[i])
            if i + 1 < len(pts):
                d1 = v.sub(pts[i], pts[i - 1])
                d2 = v.sub(pts[i + 1], pts[i])
                turn = abs(math.atan2(v.cross(d1, d2), v.dot(d1, d2)))
                if turn > CHAIN_BREAK_TURN:
                    arcs += _arc_from_points(piece)
                    piece = [pts[i]]
        arcs += _arc_from_points(piece)
    return arcs


def _arc_from_points(pts: list[Point2]) -> list[Arc]:
    if len(pts) < CHAIN_MIN_PIECES + 1:
        return []
    fit = _fit_circle(pts)
    if fit is None or fit[2] > CHAIN_FIT_TOLERANCE:
        return []
    (cx, cy), r, _ = fit
    angles = np.unwrap([math.atan2(p[1] - cy, p[0] - cx) for p in pts])
    a0, a1 = math.degrees(angles[0]), math.degrees(angles[-1])
    lo, hi = min(a0, a1), max(a0, a1)
    return [Arc(center=(cx, cy), radius=r, start_angle=lo % 360, end_angle=hi % 360)]
