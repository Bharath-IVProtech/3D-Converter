"""Elements drawn as filled shapes: solid/hatched (poché) walls and columns."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from shapely.geometry import Polygon

from bimforge.conversion.settings import ConversionSettings
from bimforge.conversion.walls import WallCandidate
from bimforge.core.model import Point2
from bimforge.drawing.primitives import Polyline

COLUMN_SIDE = (0.15, 1.5)  # m
COLUMN_MAX_ASPECT = 3.0
WALL_MIN_ASPECT = 3.0
RECTANGULARITY = 0.85  # shape area / its minimum rotated rectangle


@dataclass
class ColumnCandidate:
    position: Point2
    width: float
    depth: float
    rotation: float  # degrees
    confidence: float


def _rectangle(points: Sequence[Point2]) -> tuple[Point2, float, float, float] | None:
    """(centre, long side, short side, angle of long side in degrees) of a near-rectangular shape."""
    if len(points) < 3:
        return None
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area < 1e-4:
        return None
    box = poly.minimum_rotated_rectangle
    corners = list(box.exterior.coords)[:4]
    if len(corners) < 4 or poly.area / box.area < RECTANGULARITY:
        return None
    a, b, c = corners[0], corners[1], corners[2]
    s1, s2 = math.dist(a, b), math.dist(b, c)
    if s1 >= s2:
        long_, short, (p, q) = s1, s2, (a, b)
    else:
        long_, short, (p, q) = s2, s1, (b, c)
    centre = box.centroid
    return (centre.x, centre.y), long_, short, math.degrees(math.atan2(q[1] - p[1], q[0] - p[0]))


def filled_elements(shapes: Sequence[Polyline], scale: float,
                    settings: ConversionSettings) -> tuple[list[WallCandidate], list[ColumnCandidate]]:
    """Long thin filled shapes become walls; small compact ones become columns."""
    walls: list[WallCandidate] = []
    columns: list[ColumnCandidate] = []
    for shape in shapes:
        found = _rectangle([(x * scale, y * scale) for x, y in shape.points])
        if found is None:
            continue
        (cx, cy), long_, short, angle = found
        if short <= 0:
            continue
        aspect = long_ / short
        if COLUMN_SIDE[0] <= short and long_ <= COLUMN_SIDE[1] and aspect <= COLUMN_MAX_ASPECT:
            columns.append(ColumnCandidate((cx, cy), long_, short, angle, 0.75))
        elif settings.min_wall_thickness <= short <= settings.max_wall_thickness and aspect >= WALL_MIN_ASPECT:
            ux, uy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
            h = long_ / 2
            walls.append(WallCandidate((cx - ux * h, cy - uy * h), (cx + ux * h, cy + uy * h), short, 0.8,
                                       (shape.handle,)))
    return walls, columns
