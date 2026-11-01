"""Uniform grid spatial index for 2D boxes: fast 'what is near here' queries on plans."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Hashable, Iterable

import numpy as np

from bimforge.core.model import Point2


class GridIndex:
    def __init__(self, cell: float = 2.0) -> None:
        self.cell = cell
        self._cells: dict[tuple[int, int], list[Hashable]] = defaultdict(list)

    def _range(self, lo: float, hi: float) -> range:
        return range(math.floor(lo / self.cell), math.floor(hi / self.cell) + 1)

    def insert(self, key: Hashable, xmin: float, ymin: float, xmax: float, ymax: float) -> None:
        for cx in self._range(xmin, xmax):
            for cy in self._range(ymin, ymax):
                self._cells[(cx, cy)].append(key)

    def query(self, xmin: float, ymin: float, xmax: float, ymax: float) -> set[Hashable]:
        found: set[Hashable] = set()
        for cx in self._range(xmin, xmax):
            for cy in self._range(ymin, ymax):
                found.update(self._cells.get((cx, cy), ()))
        return found

    def near(self, point: Point2, radius: float) -> set[Hashable]:
        x, y = point
        return self.query(x - radius, y - radius, x + radius, y + radius)


def segment_distances(point: Point2, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """Distance from one point to many segments (N, 2) at once."""
    p = np.asarray(point, dtype=np.float64)
    ab = ends - starts
    denom = np.einsum("ij,ij->i", ab, ab)
    t = np.clip(np.einsum("ij,ij->i", p - starts, ab) / np.where(denom == 0, 1, denom), 0, 1)
    closest = starts + ab * t[:, None]
    return np.linalg.norm(closest - p, axis=1)


def bbox_of(points: Iterable[Point2], margin: float = 0.0) -> tuple[float, float, float, float]:
    xs, ys = zip(*points)
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
