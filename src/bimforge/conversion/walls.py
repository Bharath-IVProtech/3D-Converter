"""Wall detection from pairs of parallel face lines.

Steps (all in metres):
1. pair_wall_faces  - match parallel segments at wall-thickness distance.
2. merge_collinear  - fuse pieces split by T-junctions or (with a predicate) openings.
3. join_corners     - extend centrelines to meet at L and T junctions.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from bimforge.conversion.settings import ConversionSettings
from bimforge.core.model import Point2
from bimforge.drawing.primitives import Segment
from bimforge.geometry import vec2 as v
from bimforge.geometry.spatial import GridIndex, bbox_of, segment_distances

LATERAL_TOLERANCE = 0.02
THICKNESS_TOLERANCE = 0.01


@dataclass
class WallCandidate:
    start: Point2
    end: Point2
    thickness: float
    confidence: float
    sources: tuple[str, ...] = ()

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def direction(self) -> Point2:
        return v.unit(v.sub(self.end, self.start))


@dataclass
class _Pair:
    i: int
    j: int
    a_range: tuple[float, float]  # overlap in segment i's parameter (distance from its start)
    b_range: tuple[float, float]
    offset: float  # signed distance of segment j from segment i, along i's left normal
    score: float


def _used_fraction(used: list[tuple[float, float]], lo: float, hi: float) -> float:
    covered = sum(max(0.0, min(hi, e) - max(lo, s)) for s, e in used)
    return covered / (hi - lo)


def _project_range(seg: Segment, a: Point2, b: Point2) -> tuple[float, float]:
    u = v.unit(v.sub(seg.end, seg.start))
    ta, tb = v.dot(v.sub(a, seg.start), u), v.dot(v.sub(b, seg.start), u)
    return (min(ta, tb), max(ta, tb))


def pair_wall_faces(
    segments: Sequence[Segment],
    settings: ConversionSettings,
    layer_scores: Mapping[str, float] | None = None,
    longest_first: bool = False,
) -> list[WallCandidate]:
    """Match parallel face lines into wall centrelines.

    Each segment is tested against all later ones with vectorised NumPy maths,
    which keeps plans of 10-20k lines to a few seconds.
    """
    layer_scores = layer_scores or {}
    segs = [s for s in segments if s.length >= settings.min_wall_overlap]
    if len(segs) < 2:
        return []
    cos_tol = math.cos(math.radians(settings.parallel_tolerance_deg))
    starts = np.array([s.start for s in segs], dtype=np.float64)
    ends = np.array([s.end for s in segs], dtype=np.float64)
    lengths = np.linalg.norm(ends - starts, axis=1)
    dirs = (ends - starts) / lengths[:, None]
    scores = np.array([layer_scores.get(s.layer, 0.7) for s in segs])

    pairs: list[_Pair] = []
    for i, a in enumerate(segs):
        j = np.arange(i + 1, len(segs))
        ua = dirs[i]
        na = np.array([-ua[1], ua[0]])
        cos_angle = np.abs(dirs[j] @ ua)
        rel_s, rel_e = starts[j] - starts[i], ends[j] - starts[i]
        d0, d1 = rel_s @ na, rel_e @ na
        offset = (d0 + d1) / 2
        tb0, tb1 = np.minimum(rel_s @ ua, rel_e @ ua), np.maximum(rel_s @ ua, rel_e @ ua)
        t0, t1 = np.maximum(0.0, tb0), np.minimum(lengths[i], tb1)
        ok = (
            (cos_angle >= cos_tol)
            & (np.abs(offset) >= settings.min_wall_thickness)
            & (np.abs(offset) <= settings.max_wall_thickness)
            & (np.abs(d0 - d1) <= settings.max_face_drift)
            & (t1 - t0 >= settings.min_wall_overlap)
        )
        for k in np.nonzero(ok)[0]:
            jj = int(j[k])
            b = segs[jj]
            angle_deg = math.degrees(math.acos(min(1.0, float(cos_angle[k]))))
            tolerance = settings.parallel_tolerance_deg
            parallel = 1 - angle_deg / tolerance if tolerance > 0 else 1.0
            coverage = float(t1[k] - t0[k]) / min(lengths[i], lengths[jj])
            score = min(scores[i], scores[jj]) * (0.5 + 0.25 * parallel + 0.25 * min(1.0, coverage))
            lo, hi = float(t0[k]), float(t1[k])
            p_ = (a.start[0] + ua[0] * lo, a.start[1] + ua[1] * lo)
            q_ = (a.start[0] + ua[0] * hi, a.start[1] + ua[1] * hi)
            pairs.append(_Pair(i, jj, (lo, hi), _project_range(b, p_, q_), float(offset[k]), float(score)))

    # Thinnest first: a face pairs with its nearest parallel partner, not one across a room.
    # (longest_first suits callers that already know the spacing, so only the run length is in question.)
    if longest_first:
        pairs.sort(key=lambda p: -(p.a_range[1] - p.a_range[0]))
    else:
        pairs.sort(key=lambda p: (round(abs(p.offset), 4), -(p.a_range[1] - p.a_range[0])))
    used: dict[int, list[tuple[float, float]]] = {}
    walls: list[WallCandidate] = []
    for p in pairs:
        ua, ub = used.setdefault(p.i, []), used.setdefault(p.j, [])
        if _used_fraction(ua, *p.a_range) > 0.2 or _used_fraction(ub, *p.b_range) > 0.2:
            continue
        ua.append(p.a_range)
        ub.append(p.b_range)
        a = segs[p.i]
        u = (float(dirs[p.i][0]), float(dirs[p.i][1]))
        centre_shift = v.scale(v.left_normal(u), p.offset / 2)
        start = v.add(v.add(a.start, v.scale(u, p.a_range[0])), centre_shift)
        end = v.add(v.add(a.start, v.scale(u, p.a_range[1])), centre_shift)
        walls.append(WallCandidate(start, end, abs(p.offset), p.score, (a.handle, segs[p.j].handle)))
    return walls


def collinear_gap(a: WallCandidate, b: WallCandidate, cos_tol: float) -> tuple[float, Point2, Point2] | None:
    """If a and b share a centreline and thickness, return (gap, gap_start, gap_end).

    Negative gap means they overlap. gap_start/gap_end are the facing endpoints.
    """
    if abs(a.thickness - b.thickness) > THICKNESS_TOLERANCE:
        return None
    ua = a.direction
    if abs(v.dot(ua, b.direction)) < cos_tol:
        return None
    na = v.left_normal(ua)
    if any(abs(v.dot(v.sub(p, a.start), na)) > LATERAL_TOLERANCE for p in (b.start, b.end)):
        return None
    tb = sorted((v.dot(v.sub(b.start, a.start), ua), v.dot(v.sub(b.end, a.start), ua)))
    if tb[0] >= a.length:
        gap, g0, g1 = tb[0] - a.length, a.length, tb[0]
    elif tb[1] <= 0:
        gap, g0, g1 = -tb[1], tb[1], 0.0
    else:
        return (-1.0, a.start, a.start)  # overlapping
    return (gap, v.add(a.start, v.scale(ua, g0)), v.add(a.start, v.scale(ua, g1)))


def merge_pair(a: WallCandidate, b: WallCandidate) -> WallCandidate:
    ua = a.direction
    ts = [0.0, a.length] + [v.dot(v.sub(p, a.start), ua) for p in (b.start, b.end)]
    return WallCandidate(
        v.add(a.start, v.scale(ua, min(ts))),
        v.add(a.start, v.scale(ua, max(ts))),
        (a.thickness + b.thickness) / 2,
        min(a.confidence, b.confidence),
        a.sources + b.sources,
    )


GapPredicate = Callable[[WallCandidate, WallCandidate, Point2, Point2], bool]


def _line_groups(walls: Sequence[WallCandidate], settings: ConversionSettings) -> list[list[int]]:
    """Indices of walls that lie on the same line (angle, lateral offset and thickness within tolerance)."""
    angle_tol = math.radians(settings.parallel_tolerance_deg)
    angles = []
    for w in walls:
        dx, dy = w.direction
        theta = math.atan2(dy, dx) % math.pi
        angles.append(theta - math.pi if theta > math.pi - angle_tol else theta)  # fold near-horizontal together
    order = sorted(range(len(walls)), key=lambda i: angles[i])
    groups: list[list[int]] = []
    for angle_group in _chain(order, lambda i: angles[i], angle_tol):
        mean = sum(angles[i] for i in angle_group) / len(angle_group)
        nx, ny = -math.sin(mean), math.cos(mean)
        rho = {i: nx * walls[i].start[0] + ny * walls[i].start[1] for i in angle_group}
        by_offset = sorted(angle_group, key=rho.__getitem__)
        for line in _chain(by_offset, rho.__getitem__, LATERAL_TOLERANCE):
            by_thickness = sorted(line, key=lambda i: walls[i].thickness)
            groups.extend(_chain(by_thickness, lambda i: walls[i].thickness, THICKNESS_TOLERANCE))
    return groups


def _chain(ordered: list[int], key: Callable[[int], float], tolerance: float) -> list[list[int]]:
    """Split an ordered list where consecutive keys differ by more than tolerance."""
    out: list[list[int]] = []
    for i in ordered:
        if out and key(i) - key(out[-1][-1]) <= tolerance:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def merge_collinear(
    walls: Sequence[WallCandidate],
    max_gap: float,
    settings: ConversionSettings,
    allow: GapPredicate | None = None,
) -> list[WallCandidate]:
    """Fuse collinear walls whose gap is <= max_gap (and passes `allow`).

    Walls are grouped by line, then swept once along each line: O(n log n).
    """
    cos_tol = math.cos(math.radians(settings.parallel_tolerance_deg))
    merged: list[tuple[int, WallCandidate]] = []  # (first original index, wall) keeps output order stable
    for group in _line_groups(walls, settings):
        ux, uy = walls[group[0]].direction
        along = sorted(group, key=lambda i: min(ux * p[0] + uy * p[1] for p in (walls[i].start, walls[i].end)))
        first, current = along[0], walls[along[0]]
        for i in along[1:]:
            nxt = walls[i]
            hit = collinear_gap(current, nxt, cos_tol)
            joinable = hit is not None and hit[0] <= max_gap
            if joinable and hit[0] > 0 and allow is not None and not allow(current, nxt, hit[1], hit[2]):
                joinable = False
            if joinable:
                current, first = merge_pair(current, nxt), min(first, i)
            else:
                merged.append((first, current))
                first, current = i, nxt
        merged.append((first, current))
    return [w for _, w in sorted(merged, key=lambda t: t[0])]


def join_corners(walls: Sequence[WallCandidate], tolerance: float = 0.01) -> list[WallCandidate]:
    """Extend wall endpoints that stop short of (or overshoot) a crossing wall's centreline."""
    result = [WallCandidate(w.start, w.end, w.thickness, w.confidence, w.sources) for w in walls]
    if not result:
        return result
    max_t = max(w.thickness for w in result)
    margin = 0.75 * max_t + max_t / 2 + 2 * tolerance
    grid = GridIndex(cell=2.0)
    for i, w in enumerate(result):
        grid.insert(i, *bbox_of((w.start, w.end), margin))
    for a in result:
        nearby = grid.near(a.start, margin) | grid.near(a.end, margin)
        for j in sorted(nearby):
            b = result[j]
            if a is b:
                continue
            da, db = v.sub(a.end, a.start), v.sub(b.end, b.start)
            x = v.line_intersection(a.start, da, b.start, db)
            if x is None:
                continue
            s = v.dot(v.sub(x, b.start), b.direction)
            if not -b.thickness / 2 - tolerance <= s <= b.length + b.thickness / 2 + tolerance:
                continue
            reach = 0.75 * max(a.thickness, b.thickness) + tolerance
            if math.dist(a.start, x) <= reach:
                a.start = x
            elif math.dist(a.end, x) <= reach:
                a.end = x
    return result


HATCH_BIN = 0.25  # m, for measuring how evenly hatching covers a face
HATCH_MIN_COVERAGE = 0.6
HATCH_MIN_DENSITY = 6.0  # hatch line ends per metre of face
HATCH_ANGLE = (20.0, 70.0)  # degrees between hatch lines and the face


def hatch_walls(
    walls: Sequence[WallCandidate],
    faces: Sequence[Segment],
    hatch: Sequence[Segment],
    settings: ConversionSettings,
) -> list[WallCandidate]:
    """Walls drawn as one face line with hatching along one side (the other face is only implied).

    A long face line that is not already part of a wall gets a wall on the side
    where hatch lines densely and evenly cover it; the wall's thickness is how
    far the hatching reaches from the face.
    """
    if not faces or not hatch:
        return []
    pts = np.array([p for h in hatch for p in (h.start, h.end)], dtype=np.float64)
    dirs = np.array([v.unit(v.sub(h.end, h.start)) if h.length > 0 else (1.0, 0.0) for h in hatch])
    dirs = np.repeat(dirs, 2, axis=0)  # one direction per end point
    grid = GridIndex(cell=2.0)
    for i, (x, y) in enumerate(pts):
        grid.insert(i, x, y, x, y)
    wall_arrays = _wall_arrays(walls)
    max_t = settings.max_wall_thickness
    cos_lo, cos_hi = math.cos(math.radians(HATCH_ANGLE[1])), math.cos(math.radians(HATCH_ANGLE[0]))
    found: list[WallCandidate] = []
    for face in faces:
        if face.length < 1.0 or _inside_wall(face, wall_arrays, found):
            continue
        u = v.unit(v.sub(face.end, face.start))
        n = v.left_normal(u)
        near = sorted(grid.query(*bbox_of((face.start, face.end), max_t)))
        if not near:
            continue
        near_idx = np.asarray(near)
        # Hatching crosses the wall at an angle; equipment and duct outlines run parallel or square to it.
        cos_angle = np.abs(dirs[near_idx] @ np.asarray(u))
        diagonal = (cos_angle >= cos_lo) & (cos_angle <= cos_hi)
        rel = pts[near_idx] - np.asarray(face.start)
        along, across = rel @ np.asarray(u), rel @ np.asarray(n)
        best: tuple[float, float, int] | None = None  # (coverage, thickness, side)
        for side in (1, -1):
            beside = (along >= 0) & (along <= face.length) & (across * side > 0.005) & (across * side <= max_t)
            mask = beside & diagonal
            if mask.sum() < HATCH_MIN_DENSITY * face.length:
                continue
            bins = np.unique((along[mask] // HATCH_BIN).astype(int))
            coverage = len(bins) / max(1, math.ceil(face.length / HATCH_BIN))
            depth = across[mask] * side
            thickness = float(np.percentile(depth, 95))
            # The hatch band must end sharply at the implied second face, well inside the search range.
            spill = np.count_nonzero(depth > thickness + 0.05) / len(depth)
            if (coverage >= HATCH_MIN_COVERAGE and settings.min_wall_thickness <= thickness <= 0.85 * max_t
                    and spill <= 0.05 and (best is None or coverage > best[0])):
                best = (coverage, thickness, side)
        if best is None:
            continue
        _, t, side = best
        shift = v.scale(n, side * t / 2)
        found.append(WallCandidate(v.add(face.start, shift), v.add(face.end, shift), round(t, 3), 0.65,
                                   (face.handle,)))
    return found


def _wall_arrays(walls: Sequence[WallCandidate]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    starts = np.array([w.start for w in walls], dtype=np.float64).reshape(-1, 2)
    ends = np.array([w.end for w in walls], dtype=np.float64).reshape(-1, 2)
    return starts, ends, np.array([w.thickness / 2 + 0.03 for w in walls], dtype=np.float64)


def _inside_wall(face: Segment, arrays: tuple[np.ndarray, np.ndarray, np.ndarray], found: Sequence[WallCandidate]) -> bool:
    """Is this line one of the faces (or the inside) of a wall we already have?

    `arrays` holds the walls from before (compared all at once, as there can be thousands),
    `found` the few added since.
    """
    mid = ((face.start[0] + face.end[0]) / 2, (face.start[1] + face.end[1]) / 2)
    starts, ends, reach = arrays
    if len(starts) and np.any(segment_distances(mid, starts, ends) <= reach):
        return True
    return any(v.point_segment_distance(mid, w.start, w.end) <= w.thickness / 2 + 0.03 for w in found)
