"""Swept and placed solids for MEP: round/rectangular runs along 3D polylines, and fixture boxes."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from bimforge.core.model import Point3
from bimforge.geometry.mesh import Mesh


def _frame(direction: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two unit vectors perpendicular to `direction`; the second points 'up' for non-vertical runs."""
    up = np.array([0.0, 0.0, 1.0]) if abs(direction[2]) < 0.99 else np.array([1.0, 0.0, 0.0])
    side = np.cross(up, direction)
    side /= np.linalg.norm(side)
    return side, np.cross(direction, side)  # (side, up, direction) is right-handed


def _prism(a: np.ndarray, b: np.ndarray, profile: np.ndarray) -> Mesh:
    """Closed prism from a to b with a CCW 2D profile given in the (side, up) frame."""
    d = b - a
    d /= np.linalg.norm(d)
    side, up = _frame(d)
    ring = profile[:, :1] * side + profile[:, 1:] * up
    n = len(profile)
    vertices = np.vstack([a + ring, b + ring, a, b])
    i = np.arange(n)
    j = (i + 1) % n
    ca, cb = 2 * n, 2 * n + 1
    # The profile is CCW about +d, so these windings face outward.
    sides = np.vstack([np.column_stack([i, j + n, i + n]), np.column_stack([i, j, j + n])])
    caps = np.vstack([np.column_stack([np.full(n, ca), j, i]), np.column_stack([np.full(n, cb), i + n, j + n])])
    return Mesh(vertices, np.vstack([sides, caps]))


def _legs(path: Sequence[Point3]):
    pts = [np.asarray(p, dtype=np.float64) for p in path]
    for a, b in zip(pts, pts[1:]):
        if np.linalg.norm(b - a) > 1e-9:
            yield a, b


def tube(path: Sequence[Point3], radius: float, sides: int = 12) -> Mesh:
    """Round run (pipe, conduit): one closed cylinder per leg, overlapping at bends."""
    angles = np.linspace(0, 2 * math.pi, sides, endpoint=False)
    profile = np.column_stack([np.cos(angles), np.sin(angles)]) * radius
    return Mesh.merge(_prism(a, b, profile) for a, b in _legs(path))


def rect_run(path: Sequence[Point3], width: float, depth: float) -> Mesh:
    """Rectangular run (cable tray, duct): width horizontal, depth vertical."""
    w, h = width / 2, depth / 2
    profile = np.array([[-w, -h], [w, -h], [w, h], [-w, h]])
    return Mesh.merge(_prism(a, b, profile) for a, b in _legs(path))


def placed_box(position: Point3, size: tuple[float, float, float], rotation_deg: float) -> Mesh:
    """Box centred on position in plan, standing on position z, rotated about z."""
    w, d, h = size
    c, s = math.cos(math.radians(rotation_deg)), math.sin(math.radians(rotation_deg))
    x, y, z = position
    local = [(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)]
    ring = np.array([(x + lx * c - ly * s, y + lx * s + ly * c) for lx, ly in local])
    vertices = np.vstack([np.column_stack([ring, np.full(4, z)]), np.column_stack([ring, np.full(4, z + h)])])
    faces = np.array([
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ])
    return Mesh(vertices, faces)
