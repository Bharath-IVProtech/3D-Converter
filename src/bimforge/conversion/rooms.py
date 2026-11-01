"""Rooms from closed wall-centreline loops, and a floor slab from their union."""

from __future__ import annotations

from collections.abc import Sequence

import shapely
from shapely.geometry import LineString, Polygon

from bimforge.conversion.walls import WallCandidate
from bimforge.core.model import Point2

NODING_GRID = 1e-3  # metres; snap-rounds T-junction endpoints onto the wall they meet


def detect_rooms(walls: Sequence[WallCandidate], min_area: float) -> list[Polygon]:
    if not walls:
        return []
    noded = shapely.unary_union([LineString([w.start, w.end]) for w in walls], grid_size=NODING_GRID)
    faces = shapely.polygonize(list(shapely.get_parts(noded)))
    rooms = [p for p in shapely.get_parts(faces) if p.area >= min_area]
    return sorted(rooms, key=lambda p: (round(p.centroid.y, 2), round(p.centroid.x, 2)))


def footprint(rooms: Sequence[Polygon], walls: Sequence[WallCandidate]) -> list[Point2] | None:
    """Outer outline at the wall faces: room union grown by half the thickest wall."""
    if not rooms:
        return None
    grow = max(w.thickness for w in walls) / 2
    outline = shapely.unary_union(rooms).buffer(grow, join_style="mitre")
    if outline.geom_type == "MultiPolygon":
        outline = max(outline.geoms, key=lambda g: g.area)
    return ring(outline)


def ring(polygon: Polygon) -> list[Point2]:
    return [(float(x), float(y)) for x, y in list(polygon.exterior.coords)[:-1]]
