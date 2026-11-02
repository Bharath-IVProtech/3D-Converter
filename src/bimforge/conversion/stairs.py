"""Stair flights from their treads: runs of parallel, evenly spaced lines of similar length.

Works at any angle (the diagonal walkway stair) and for treads drawn twice in
different line styles. Tread lines found here are removed from wall
detection, because to a face-pairing wall detector a stair looks like a stack
of thin walls.
"""

from __future__ import annotations

import bisect
import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

from shapely.geometry import MultiPoint, Point
from shapely.ops import unary_union

from bimforge.core.model import Point2
from bimforge.drawing.primitives import Segment

TREAD_LENGTH = (0.6, 3.0)  # stair width range, m
GOING = (0.2, 0.36)  # tread depth range, m
GOING_TOLERANCE = 0.03  # treads in a flight are evenly spaced
MIN_TREADS = 4
DUPLICATE = 0.015  # the same tread drawn twice (e.g. in two line weights)
ANGLE_TOLERANCE = math.radians(2.0)


@dataclass
class StairCandidate:
    start: Point2
    end: Point2
    width: float
    risers: int
    confidence: float
    used: set[int] = field(default_factory=set)  # indices of the input segments that are treads
    further: list[tuple[Point2, Point2, float, int]] = field(default_factory=list)  # linked flights, climbing order
    direction_known: bool = False  # an UP or DN note told us which end is the bottom

    @property
    def centre(self) -> Point2:
        return ((self.start[0] + self.end[0]) / 2, (self.start[1] + self.end[1]) / 2)


@dataclass
class _Line:
    index: int
    rho: float  # offset across the treads (direction of travel)
    t0: float  # extent along the tread
    t1: float

    def overlap(self, other: _Line) -> float:
        shorter = min(self.t1 - self.t0, other.t1 - other.t0)
        return max(0.0, min(self.t1, other.t1) - max(self.t0, other.t0)) / shorter if shorter > 0 else 0.0


def _similar(a: _Line, b: _Line) -> bool:
    la, lb = a.t1 - a.t0, b.t1 - b.t0
    return a.overlap(b) >= 0.8 and 0.8 <= la / lb <= 1.25


def detect_stairs(segments: Sequence[Segment]) -> list[StairCandidate]:
    """Segments must already be in metres."""
    candidates = [i for i, s in enumerate(segments) if TREAD_LENGTH[0] <= s.length <= TREAD_LENGTH[1]]
    angle = {}
    for i in candidates:
        (x0, y0), (x1, y1) = segments[i].start, segments[i].end
        theta = math.atan2(y1 - y0, x1 - x0) % math.pi
        angle[i] = theta - math.pi if theta > math.pi - ANGLE_TOLERANCE else theta
    order = sorted(candidates, key=angle.__getitem__)
    groups: list[list[int]] = []
    for i in order:
        if groups and angle[i] - angle[groups[-1][-1]] <= ANGLE_TOLERANCE:
            groups[-1].append(i)
        else:
            groups.append([i])

    stairs: list[StairCandidate] = []
    for group in groups:
        if len(group) < MIN_TREADS:
            continue
        mean = statistics.fmean(angle[i] for i in group)
        ux, uy = math.cos(mean), math.sin(mean)
        nx, ny = -uy, ux
        lines = []
        for i in group:
            (x0, y0), (x1, y1) = segments[i].start, segments[i].end
            ta, tb = ux * x0 + uy * y0, ux * x1 + uy * y1
            lines.append(_Line(i, (nx * (x0 + x1) + ny * (y0 + y1)) / 2, min(ta, tb), max(ta, tb)))
        lines.sort(key=lambda line: line.rho)
        rhos = [line.rho for line in lines]
        taken: set[int] = set()
        for k, first in enumerate(lines):
            if first.index in taken:
                continue
            run, duplicates = [first], set()
            while True:
                last = run[-1]
                lo = bisect.bisect_left(rhos, last.rho - DUPLICATE)
                hi = bisect.bisect_right(rhos, last.rho + GOING[1])
                spacing = statistics.median(b.rho - a.rho for a, b in zip(run, run[1:])) if len(run) > 1 else None
                nxt = None
                for other in lines[lo:hi]:
                    if other is last or other.index in taken or other.index in duplicates or not _similar(last, other):
                        continue
                    gap = other.rho - last.rho
                    if abs(gap) <= DUPLICATE:
                        duplicates.add(other.index)  # same tread drawn twice
                        continue
                    if gap < GOING[0] or (spacing is not None and abs(gap - spacing) > GOING_TOLERANCE):
                        continue
                    if nxt is None or gap < nxt.rho - last.rho:
                        nxt = other
                if nxt is None:
                    break
                run.append(nxt)
            if len(run) < MIN_TREADS:
                continue
            going = statistics.median(b.rho - a.rho for a, b in zip(run, run[1:]))
            t0 = statistics.median(line.t0 for line in run)
            t1 = statistics.median(line.t1 for line in run)
            tm = (t0 + t1) / 2
            r0, r1 = run[0].rho - going / 2, run[-1].rho + going / 2
            used = {line.index for line in run} | duplicates
            taken |= used
            stairs.append(StairCandidate(
                start=(ux * tm + nx * r0, uy * tm + ny * r0),
                end=(ux * tm + nx * r1, uy * tm + ny * r1),
                width=t1 - t0,
                risers=len(run),
                confidence=min(0.95, 0.6 + 0.04 * len(run)),
                used=used,
            ))
    return stairs


# --- Joining flights into one staircase ---------------------------------------------------

LANDING_REACH = 2.5  # m: the most a landing separates two flights of one staircase
WIDTH_RATIO = 1.5  # flights of one staircase have similar widths...
GOING_TOLERANCE_LINK = 0.03  # ...and the same tread depth
SEPARATING_WALL = 2.5  # m: a wall this long between two flights means they are separate stairs
NOTE_REACH = 2.0  # m: how far an UP or DN note may be from the flight it describes
_UP = re.compile(r"^(UP|UPSTAIRS)\b", re.I)
_DOWN = re.compile(r"^(DN|DOWN)\b", re.I)


def _going(c: StairCandidate) -> float:
    return math.dist(c.start, c.end) / c.risers


def _crosses(p: Point2, q: Point2, walls: Sequence) -> bool:
    """Does the straight path between two flight ends pass through a wall?"""
    d = (q[0] - p[0], q[1] - p[1])
    for wall in walls:
        if wall.length < SEPARATING_WALL:
            continue  # landing edge lines often pair into short false walls
        e = (wall.end[0] - wall.start[0], wall.end[1] - wall.start[1])
        denom = d[0] * e[1] - d[1] * e[0]
        if abs(denom) < 1e-9:
            continue
        w = (wall.start[0] - p[0], wall.start[1] - p[1])
        t, u = (w[0] * e[1] - w[1] * e[0]) / denom, (w[0] * d[1] - w[1] * d[0]) / denom
        if 0 < t < 1 and 0 < u < 1:
            return True
    return False


def link_flights(flights: Sequence[StairCandidate], walls: Sequence = (),
                 notes: Sequence[tuple[Point2, str]] = ()) -> list[StairCandidate]:
    """Merge flights joined by a landing into one staircase.

    Two flights belong together when an end of one lies within a landing's reach of an end of
    the other, they have similar widths and the same tread depth, and no wall lies between them.
    Closest pairs are joined first, and each flight end joins at most once.

    A U-turn stair's two ends are equally close, so a plan alone does not show which has the
    landing. UP and DN notes settle it: they mark a free end, so the landing is at the other.
    """
    marked = [p for p, text in notes if _UP.match(text.strip()) or _DOWN.match(text.strip())]

    def has_note(point: Point2) -> bool:
        return any(math.dist(point, m) <= NOTE_REACH for m in marked)

    pairs = []
    for i, a in enumerate(flights):
        for j, b in enumerate(flights[i + 1:], start=i + 1):
            if not 1 / WIDTH_RATIO <= a.width / b.width <= WIDTH_RATIO:
                continue
            if abs(_going(a) - _going(b)) > GOING_TOLERANCE_LINK:
                continue
            for ea, pa in enumerate((a.start, a.end)):
                for eb, pb in enumerate((b.start, b.end)):
                    gap = math.dist(pa, pb)
                    if gap <= LANDING_REACH and not _crosses(pa, pb, walls):
                        pairs.append((has_note(pa) or has_note(pb), round(gap, 3), i, ea, j, eb))
    links: dict[tuple[int, int], tuple[int, int]] = {}  # (flight, end) <-> (flight, end)
    group = list(range(len(flights)))

    def root(i: int) -> int:
        while group[i] != i:
            i = group[i]
        return i

    for _, _, i, ea, j, eb in sorted(pairs):
        if (i, ea) in links or (j, eb) in links or root(i) == root(j):
            continue
        links[(i, ea)], links[(j, eb)] = (j, eb), (i, ea)
        group[root(j)] = root(i)

    result: list[StairCandidate] = []
    done: set[int] = set()
    for i, flight in enumerate(flights):
        if i in done:
            continue
        free = [e for e in (0, 1) if (i, e) not in links]
        if not free:
            continue  # visited from the free end of its chain
        chain: list[tuple[Point2, Point2, float, int, StairCandidate]] = []
        current, entry = i, free[0]  # enter this flight at its free end
        while True:
            done.add(current)
            c = flights[current]
            a, b = (c.start, c.end) if entry == 0 else (c.end, c.start)
            chain.append((a, b, c.width, c.risers, c))
            onward = links.get((current, 1 - entry))
            if onward is None:
                break
            current, entry = onward
        (a, b, width, risers, _), *rest = chain
        result.append(StairCandidate(
            start=a, end=b, width=width, risers=risers,
            confidence=min(x[4].confidence for x in chain),
            used=set().union(*(x[4].used for x in chain)),
            further=[(p, q, w, r) for p, q, w, r, _ in rest],
        ))
    return result


def footprint(stair: StairCandidate):
    """The plan area a staircase covers: its flights and the landings joining them."""
    flights = [(stair.start, stair.end, stair.width), *[(a, b, w) for a, b, w, _ in stair.further]]
    parts = []
    previous = None
    for a, b, width in flights:
        ux, uy = (b[0] - a[0]) / math.dist(a, b), (b[1] - a[1]) / math.dist(a, b)
        h = width / 2
        start_edge = [(a[0] - uy * h, a[1] + ux * h), (a[0] + uy * h, a[1] - ux * h)]
        end_edge = [(b[0] - uy * h, b[1] + ux * h), (b[0] + uy * h, b[1] - ux * h)]
        parts.append(MultiPoint(start_edge + end_edge).convex_hull)
        if previous is not None:
            parts.append(MultiPoint(previous + start_edge).convex_hull)
        previous = end_edge
    return unary_union(parts)


def walls_clear_of_stairs(walls: Sequence, stairs: Sequence[StairCandidate], max_length: float = 3.0) -> list:
    """Remove short "walls" standing on a staircase: its handrails and landing edges, paired by mistake."""
    areas = [footprint(s).buffer(0.05) for s in stairs]
    kept = []
    for wall in walls:
        mid = Point((wall.start[0] + wall.end[0]) / 2, (wall.start[1] + wall.end[1]) / 2)
        if wall.length <= max_length and any(area.contains(mid) for area in areas):
            continue
        kept.append(wall)
    return kept


def orient_by_notes(stairs: Sequence[StairCandidate], notes: Sequence[tuple[Point2, str]]) -> None:
    """Point each staircase uphill using nearby UP / DN notes, when the drawing has them.

    An UP note marks the bottom end of a flight and a DN note the top end.
    """
    for stair in stairs:
        flights = [(stair.start, stair.end, stair.width, stair.risers), *stair.further]
        bottom, top = flights[0][0], flights[-1][1]
        best: tuple[float, bool] | None = None  # (distance, is the staircase already pointing uphill?)
        for position, text in notes:
            is_up, is_down = bool(_UP.match(text.strip())), bool(_DOWN.match(text.strip()))
            if not (is_up or is_down):
                continue
            near_bottom, near_top = math.dist(position, bottom), math.dist(position, top)
            nearest = min(near_bottom, near_top)
            if nearest <= NOTE_REACH and (best is None or nearest < best[0]):
                at_bottom = near_bottom <= near_top
                best = (nearest, at_bottom == is_up)
        if best is None:
            continue
        stair.direction_known = True
        if not best[1]:
            reverse = [(q, p, w, r) for p, q, w, r in reversed(flights)]
            stair.start, stair.end, stair.width, stair.risers = reverse[0]
            stair.further = reverse[1:]
