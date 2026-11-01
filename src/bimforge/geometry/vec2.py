"""Small 2D vector helpers on plain tuples (hot loops avoid numpy call overhead)."""

from __future__ import annotations

import math

from bimforge.core.model import Point2


def sub(a: Point2, b: Point2) -> Point2:
    return (a[0] - b[0], a[1] - b[1])


def add(a: Point2, b: Point2) -> Point2:
    return (a[0] + b[0], a[1] + b[1])


def scale(a: Point2, k: float) -> Point2:
    return (a[0] * k, a[1] * k)


def dot(a: Point2, b: Point2) -> float:
    return a[0] * b[0] + a[1] * b[1]


def cross(a: Point2, b: Point2) -> float:
    return a[0] * b[1] - a[1] * b[0]


def unit(a: Point2) -> Point2:
    length = math.hypot(*a)
    if length == 0:
        raise ValueError("Zero-length vector")
    return (a[0] / length, a[1] / length)


def left_normal(u: Point2) -> Point2:
    return (-u[1], u[0])


def point_segment_distance(p: Point2, a: Point2, b: Point2) -> float:
    ab = sub(b, a)
    denom = dot(ab, ab)
    t = 0.0 if denom == 0 else max(0.0, min(1.0, dot(sub(p, a), ab) / denom))
    return math.dist(p, add(a, scale(ab, t)))


def line_intersection(p: Point2, r: Point2, q: Point2, s: Point2) -> Point2 | None:
    """Intersection of infinite lines p + t*r and q + u*s; None if parallel."""
    denom = cross(r, s)
    if abs(denom) < 1e-12:
        return None
    t = cross(sub(q, p), s) / denom
    return add(p, scale(r, t))
