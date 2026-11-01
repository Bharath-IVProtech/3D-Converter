"""Default, dependency-free geometry kernel.

Supports the prismatic geometry the MVP needs (extrusions). An OpenCASCADE
kernel plugin adds booleans, sweeps and STEP I/O behind the same interface.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from bimforge.core.model import Point2
from bimforge.geometry.mesh import Mesh
from bimforge.geometry.polygon import triangulate
from bimforge.plugins.sdk import GeometryKernel, PluginInfo


class NumpyKernel(GeometryKernel):
    info = PluginInfo(id="bimforge.kernel.numpy", name="NumPy prism kernel", version="0.1.0")
    capabilities = frozenset({"extrude"})

    def extrude(self, profile: Sequence[Point2], z0: float, height: float) -> Mesh:
        if height <= 0:
            raise ValueError("Extrusion height must be positive")
        ring, triangles = triangulate(profile)
        n = len(ring)
        xy = np.asarray(ring, dtype=np.float64)
        bottom = np.column_stack([xy, np.full(n, z0)])
        top = np.column_stack([xy, np.full(n, z0 + height)])
        vertices = np.vstack([bottom, top])

        tris = np.asarray(triangles, dtype=np.int64)
        bottom_faces = tris[:, ::-1]  # facing down
        top_faces = tris + n  # facing up
        i = np.arange(n)
        j = (i + 1) % n
        # Ring is CCW, so (i, j, j+n) faces outward.
        sides = np.vstack([np.column_stack([i, j, j + n]), np.column_stack([i, j + n, i + n])])
        return Mesh(vertices, np.vstack([bottom_faces, top_faces, sides]))
