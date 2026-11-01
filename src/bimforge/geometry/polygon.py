"""2D polygon utilities used for extrusion profiles."""

from __future__ import annotations

from collections.abc import Sequence

from bimforge.core.model import Point2

_EPS = 1e-12


def signed_area(points: Sequence[Point2]) -> float:
    """Shoelace area; positive for counter-clockwise rings."""
    n = len(points)
    return sum(points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1] for i in range(n)) / 2


def _cross(o: Point2, a: Point2, b: Point2) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def clean_ring(points: Sequence[Point2], tol: float = 1e-9) -> list[Point2]:
    """Drop the closing duplicate, repeated vertices and collinear vertices."""
    ring = [tuple(map(float, p)) for p in points]
    if len(ring) > 1 and abs(ring[0][0] - ring[-1][0]) <= tol and abs(ring[0][1] - ring[-1][1]) <= tol:
        ring.pop()
    changed = True
    while changed and len(ring) >= 3:
        changed = False
        for i in range(len(ring)):
            prev, cur, nxt = ring[i - 1], ring[i], ring[(i + 1) % len(ring)]
            duplicate = abs(cur[0] - prev[0]) <= tol and abs(cur[1] - prev[1]) <= tol
            if duplicate or abs(_cross(prev, cur, nxt)) <= tol:
                del ring[i]
                changed = True
                break
    return ring


def _inside_triangle(p: Point2, a: Point2, b: Point2, c: Point2) -> bool:
    return _cross(a, b, p) >= -_EPS and _cross(b, c, p) >= -_EPS and _cross(c, a, p) >= -_EPS


def triangulate(points: Sequence[Point2]) -> tuple[list[Point2], list[tuple[int, int, int]]]:
    """Ear-clipping triangulation of a simple polygon (no holes).

    Returns the cleaned, counter-clockwise ring and triangles indexing into it.
    """
    ring = clean_ring(points)
    if len(ring) < 3:
        raise ValueError("Polygon needs at least 3 non-collinear vertices")
    if signed_area(ring) < 0:
        ring.reverse()

    remaining = list(range(len(ring)))
    triangles: list[tuple[int, int, int]] = []
    while len(remaining) > 3:
        for k in range(len(remaining)):
            i, j, m = remaining[k - 1], remaining[k], remaining[(k + 1) % len(remaining)]
            a, b, c = ring[i], ring[j], ring[m]
            if _cross(a, b, c) <= _EPS:
                continue  # reflex vertex
            if any(_inside_triangle(ring[q], a, b, c) for q in remaining if q not in (i, j, m)):
                continue
            triangles.append((i, j, m))
            del remaining[k]
            break
        else:
            raise ValueError("Polygon is not simple; cannot triangulate")
    triangles.append((remaining[0], remaining[1], remaining[2]))
    return ring, triangles
