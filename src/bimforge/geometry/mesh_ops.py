"""Whole-mesh calculations for the 3D view: which edges to draw, and what a click hits."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

MERGE_DECIMALS = 5  # vertices closer than about 10 micrometres are the same point


def feature_edges(vertices: np.ndarray, faces: np.ndarray, angle_degrees: float = 35.0) -> np.ndarray:
    """The outline a person would draw, as (N, 2, 3) line ends: sharp folds and open borders.

    Edges shared by more than two faces are left out: they are where separate pieces of one solid
    touch, such as the wall above a door, and not edges anyone would see.
    """
    if len(faces) == 0:
        return np.zeros((0, 2, 3))
    _, first, index = np.unique(np.round(vertices, MERGE_DECIMALS), axis=0, return_index=True, return_inverse=True)
    index = index.ravel()
    merged = index[faces]
    a, b, c = (vertices[faces[:, i]] for i in range(3))
    normals = np.cross(b - a, c - a)
    lengths = np.linalg.norm(normals, axis=1)
    solid = lengths > 1e-12
    merged, normals = merged[solid], normals[solid] / lengths[solid][:, None]
    if len(merged) == 0:
        return np.zeros((0, 2, 3))
    edges = np.sort(merged[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
    owner = np.repeat(np.arange(len(merged)), 3)
    order = np.lexsort((edges[:, 1], edges[:, 0]))
    edges, owner = edges[order], owner[order]
    starts = np.flatnonzero(np.r_[True, np.any(edges[1:] != edges[:-1], axis=1)])
    counts = np.diff(np.r_[starts, len(edges)])
    border = starts[counts == 1]
    pairs = starts[counts == 2]
    folded = pairs[np.einsum("ij,ij->i", normals[owner[pairs]], normals[owner[pairs + 1]]) < math.cos(math.radians(angle_degrees))]
    chosen = edges[np.r_[border, folded]]
    return vertices[first][chosen]


def ray_hits(origin: np.ndarray, direction: np.ndarray, a: np.ndarray, e1: np.ndarray, e2: np.ndarray) -> np.ndarray:
    """Distance along the ray to each triangle (corner a, edges e1 and e2); inf where it misses.

    Both sides of a triangle count, and the distance is in units of `direction`.
    """
    if len(a) == 0:
        return np.zeros(0)
    p = np.cross(direction, e2)
    det = np.einsum("ij,ij->i", e1, p)
    with np.errstate(divide="ignore", invalid="ignore"):
        inverse = 1.0 / det
        s = origin - a
        u = np.einsum("ij,ij->i", s, p) * inverse
        q = np.cross(s, e1)
        v = (q @ direction) * inverse
        t = np.einsum("ij,ij->i", e2, q) * inverse
        hit = (np.abs(det) > 1e-14) & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0)
    return np.where(hit, t, np.inf)


def thin_lines(ends: np.ndarray, limit: int, shortest: float) -> np.ndarray:
    """At most `limit` distinct lines (N, 4), without tiny ones, keeping the longest.

    The small print of a big drawing (one light fitting can be 800 tiny lines) can't be seen at
    plan scale but slows every frame. Only what's displayed is thinned: detection reads all of it.
    """
    if len(ends) == 0:
        return ends
    ends = np.unique(np.round(ends, 4), axis=0)
    lengths = np.hypot(ends[:, 2] - ends[:, 0], ends[:, 3] - ends[:, 1])
    if len(ends) <= limit:
        return ends
    keep = lengths >= shortest
    ends, lengths = ends[keep], lengths[keep]
    if len(ends) > limit:
        ends = ends[np.argpartition(-lengths, limit - 1)[:limit]]
    return ends


def main_extent(boxes: np.ndarray, gap_share: float = 0.4, minority: float = 0.1) -> tuple[float, ...] | None:
    """The bounds of the main body of a drawing, leaving out a few objects stranded far away.

    Boxes are (N, 6) xmin, xmax, ymin, ymax, zmin, zmax. A group is stranded when an empty gap of at
    least `gap_share` of the whole width separates it from the rest and it holds at most `minority` of
    the objects, so two plans side by side both count.
    """
    b = np.asarray(boxes, dtype=np.float64)
    if len(b) == 0:
        return None
    keep = np.ones(len(b), dtype=bool)
    for _ in range(4):
        changed = False
        for axis in (0, 1):
            centres = (b[:, axis * 2] + b[:, axis * 2 + 1]) / 2
            kept = np.sort(centres[keep])
            if len(kept) < 5:
                continue
            gaps = np.diff(kept)
            i = int(np.argmax(gaps))
            if gaps[i] <= 0 or gaps[i] < gap_share * (kept[-1] - kept[0]):
                continue
            split = (kept[i] + kept[i + 1]) / 2
            below, above = i + 1, len(kept) - i - 1
            if min(below, above) > minority * len(kept):
                continue
            keep &= centres > split if below < above else centres < split
            changed = True
        if not changed:
            break
    k = b[keep]
    return (k[:, 0].min(), k[:, 1].max(), k[:, 2].min(), k[:, 3].max(), k[:, 4].min(), k[:, 5].max())


def fit_camera(
    bounds: Sequence[float],
    direction: np.ndarray,
    up: np.ndarray,
    tan_half_height: float,
    aspect: float,
    margin: float = 1.06,
) -> tuple[np.ndarray, float, float]:
    """Where to aim so a box fills the window, seen along `direction`: (focal point, distance, half height).

    It looks at the box's actual corners rather than a sphere around it, so a long flat plan uses the
    full width of a wide window. The half height is what an orthographic view needs.
    """
    forward = direction / np.linalg.norm(direction)
    right = np.cross(forward, up)
    right /= np.linalg.norm(right)
    upward = np.cross(right, forward)
    xmin, xmax, ymin, ymax, zmin, zmax = bounds
    centre = np.array([(xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2])
    corners = np.array([(x, y, z) for x in (xmin, xmax) for y in (ymin, ymax) for z in (zmin, zmax)]) - centre
    across, along, toward = corners @ right, corners @ upward, corners @ -forward
    mid_across, mid_along = (across.max() + across.min()) / 2, (along.max() + along.min()) / 2
    half_across = (across - mid_across) * margin
    half_along = (along - mid_along) * margin
    tan_half_width = tan_half_height * aspect
    distance = float(np.max(np.maximum(np.abs(half_across) / tan_half_width, np.abs(half_along) / tan_half_height) + toward))
    half_height = float(max(np.abs(half_along).max(), np.abs(half_across).max() / aspect))
    return centre + right * mid_across + upward * mid_along, distance, half_height
