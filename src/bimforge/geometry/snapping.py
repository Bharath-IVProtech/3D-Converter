"""Where a drawing click lands: wall ends, wall midpoints, along walls, straight lines, the grid."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from bimforge.core.model import Point2
from bimforge.geometry import vec2 as v

GRID = 0.05  # m
SNAP_RADIUS = 0.25  # m
CENTRE_SNAP = 0.15  # m: an opening this close to a wall's middle lands on it


@dataclass(frozen=True)
class Snap:
    point: Point2
    kind: str  # "end", "middle", "wall", "straight" or "grid"


def snap_point(
    point: Point2,
    segments: Sequence[tuple[Point2, Point2]] = (),
    last: Point2 | None = None,
    straight: bool = False,
    radius: float = SNAP_RADIUS,
    grid: float = GRID,
) -> Snap:
    """Exact geometry beats a straight-line lock, which beats the grid.

    Holding the straight-line lock while near a wall end still lands on the end, so
    walls can always be closed exactly.
    """
    best, best_d = None, radius
    for a, b in segments:
        for candidate in (a, b):
            d = math.dist(point, candidate)
            if d < best_d:
                best, best_d = Snap(candidate, "end"), d
    if best is not None:
        return best
    for a, b in segments:
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        d = math.dist(point, mid)
        if d < best_d:
            best, best_d = Snap(mid, "middle"), d
    if best is not None:
        return best
    if straight and last is not None:
        dx, dy = point[0] - last[0], point[1] - last[1]
        if abs(dx) >= abs(dy):
            return Snap((_to_grid(point[0], grid), last[1]), "straight")
        return Snap((last[0], _to_grid(point[1], grid)), "straight")
    for a, b in segments:
        ab = v.sub(b, a)
        denom = v.dot(ab, ab)
        if denom == 0:
            continue
        t = max(0.0, min(1.0, v.dot(v.sub(point, a), ab) / denom))
        foot = v.add(a, v.scale(ab, t))
        d = math.dist(point, foot)
        if d < best_d:
            best, best_d = Snap(foot, "wall"), d
    if best is not None:
        return best
    return Snap((_to_grid(point[0], grid), _to_grid(point[1], grid)), "grid")


def _to_grid(value: float, grid: float) -> float:
    return round(round(value / grid) * grid, 6)


def nearest_wall(point: Point2, walls: Iterable, reach: float = 1.0):
    """The closest wall within reach of the point, with the distance along it: (wall, along) or None."""
    best, best_d = None, reach
    for wall in walls:
        ab = v.sub(wall.end, wall.start)
        denom = v.dot(ab, ab)
        if denom == 0:
            continue
        t = max(0.0, min(1.0, v.dot(v.sub(point, wall.start), ab) / denom))
        d = math.dist(point, v.add(wall.start, v.scale(ab, t)))
        if d < best_d:
            best, best_d = (wall, t * wall.length), d
    return best


def fit_opening(along: float, wall_length: float, width: float, grid: float = GRID) -> float | None:
    """Where an opening's centre lands: the wall's middle if close, else on the grid, always inside the wall."""
    if width > wall_length:
        return None
    middle = wall_length / 2
    if abs(along - middle) < CENTRE_SNAP:
        return middle
    return max(width / 2, min(wall_length - width / 2, _to_grid(along, grid)))


def ids_in_box(boxes: Mapping[str, Sequence[float]], first: Point2, second: Point2) -> list[str]:
    """Box selection, as in CAD: dragged rightwards takes what is fully inside, leftwards what it touches."""
    x0, x1 = sorted((first[0], second[0]))
    y0, y1 = sorted((first[1], second[1]))
    crossing = second[0] < first[0]
    chosen = []
    for key, (xmin, xmax, ymin, ymax, *_) in boxes.items():
        if crossing:
            hit = xmin <= x1 and xmax >= x0 and ymin <= y1 and ymax >= y0
        else:
            hit = xmin >= x0 and xmax <= x1 and ymin >= y0 and ymax <= y1
        if hit:
            chosen.append(key)
    return chosen
