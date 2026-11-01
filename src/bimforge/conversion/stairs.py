"""Stair flights from their treads: runs of parallel, evenly spaced lines of similar length.

Works at any angle (the diagonal walkway stair) and for treads drawn twice in
different line styles. Tread lines found here are removed from wall
detection, because to a face-pairing wall detector a stair looks like a stack
of thin walls.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

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
