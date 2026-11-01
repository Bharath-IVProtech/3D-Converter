"""Triangle mesh exchanged between geometry kernels and render backends."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, eq=False)
class Mesh:
    vertices: np.ndarray  # (N, 3) float64
    faces: np.ndarray  # (M, 3) int64, counter-clockwise seen from outside

    def __post_init__(self) -> None:
        if self.vertices.ndim != 2 or self.vertices.shape[1] != 3:
            raise ValueError("vertices must have shape (N, 3)")
        if self.faces.ndim != 2 or self.faces.shape[1] != 3:
            raise ValueError("faces must have shape (M, 3)")

    @staticmethod
    def empty() -> Mesh:
        return Mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))

    @staticmethod
    def merge(meshes: Iterable[Mesh]) -> Mesh:
        vertices, faces, base = [], [], 0
        for mesh in meshes:
            vertices.append(mesh.vertices)
            faces.append(mesh.faces + base)
            base += len(mesh.vertices)
        if not vertices:
            return Mesh.empty()
        return Mesh(np.vstack(vertices), np.vstack(faces))

    @property
    def is_empty(self) -> bool:
        return len(self.faces) == 0

    def volume(self) -> float:
        """Signed volume via the divergence theorem; positive for closed, outward-facing meshes."""
        if self.is_empty:
            return 0.0
        a, b, c = (self.vertices[self.faces[:, i]] for i in range(3))
        return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return self.vertices.min(axis=0), self.vertices.max(axis=0)
