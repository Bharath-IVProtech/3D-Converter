"""Turn parametric elements into meshes using a GeometryKernel."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from bimforge.core.model import (
    CableCarrierSegment,
    Column,
    Door,
    Element,
    Fixture,
    ImportedElement,
    Opening,
    PipeSegment,
    Point2,
    Point3,
    Project,
    Slab,
    Stair,
    Wall,
    Window,
)
from bimforge.disciplines.catalog import family_of
from bimforge.geometry.mesh import Mesh
from bimforge.geometry.sweep import placed_box, rect_run, tube
from bimforge.plugins.sdk import GeometryKernel

# (start, end, bottom, top) in wall-local coordinates: along the wall and above its base.
Box2 = tuple[float, float, float, float]

DOOR_PANEL_THICKNESS = 0.05
GLAZING_THICKNESS = 0.02


def wall_pieces(length: float, height: float, openings: Sequence[Box2]) -> list[Box2]:
    """Split a wall elevation into solid rectangles around rectangular openings.

    Openings are clipped to the wall. Overlapping openings raise ValueError,
    because the result would be ambiguous and almost always signals bad input.
    """
    clipped = sorted(
        (max(0.0, s0), min(length, s1), max(0.0, z0), min(height, z1))
        for s0, s1, z0, z1 in openings
        if s1 > 0 and s0 < length and z1 > 0 and z0 < height
    )
    for (_, a_end, _, _), (b_start, _, _, _) in zip(clipped, clipped[1:]):
        if b_start < a_end - 1e-9:
            raise ValueError("Openings overlap")

    pieces: list[Box2] = []
    cursor = 0.0
    for s0, s1, z0, z1 in clipped:
        if s0 > cursor:
            pieces.append((cursor, s0, 0.0, height))
        if z0 > 0:
            pieces.append((s0, s1, 0.0, z0))
        if z1 < height:
            pieces.append((s0, s1, z1, height))
        cursor = s1
    if cursor < length:
        pieces.append((cursor, length, 0.0, height))
    return [p for p in pieces if p[1] - p[0] > 1e-9 and p[3] - p[2] > 1e-9]


def _band(wall: Wall, s0: float, s1: float, half_thickness: float) -> list[Point2]:
    """Plan rectangle covering [s0, s1] along the wall, +/- half_thickness across it."""
    (ux, uy), (nx, ny) = wall.direction, wall.normal
    sx, sy = wall.start
    return [
        (sx + ux * s + nx * side * half_thickness, sy + uy * s + ny * side * half_thickness)
        for s, side in ((s0, -1), (s1, -1), (s1, 1), (s0, 1))
    ]


def wall_mesh(wall: Wall, openings: Sequence[Opening], base_z: float, kernel: GeometryKernel) -> Mesh:
    z = base_z + wall.base_offset
    boxes = [(*o.span, o.sill_height, o.sill_height + o.height) for o in openings]
    return Mesh.merge(
        kernel.extrude(_band(wall, s0, s1, wall.thickness / 2), z + z0, z1 - z0)
        for s0, s1, z0, z1 in wall_pieces(wall.length, wall.height, boxes)
    )


def opening_mesh(opening: Opening, host: Wall, base_z: float, kernel: GeometryKernel) -> Mesh:
    """Simplified filler: a door leaf or a glazing pane centred in the wall."""
    panel = DOOR_PANEL_THICKNESS if isinstance(opening, Door) else GLAZING_THICKNESS
    s0, s1 = opening.span
    z = base_z + host.base_offset + opening.sill_height
    return kernel.extrude(_band(host, s0, s1, min(panel, host.thickness) / 2), z, opening.height)


def stair_mesh(stair: Stair, base_z: float, kernel: GeometryKernel) -> Mesh:
    """Solid stepped flight: one block per riser, each rising to its tread."""
    (sx, sy), (ex, ey) = stair.start, stair.end
    length = stair.length
    ux, uy = (ex - sx) / length, (ey - sy) / length
    nx, ny = -uy, ux
    h = stair.width / 2
    g, r = stair.going, stair.riser_height
    z0 = base_z + stair.base_offset
    blocks = []
    for k in range(stair.risers):
        a, b = k * g, (k + 1) * g
        profile = [(sx + ux * s + nx * side * h, sy + uy * s + ny * side * h)
                   for s, side in ((a, -1), (b, -1), (b, 1), (a, 1))]
        blocks.append(kernel.extrude(profile, z0, (k + 1) * r))
    return Mesh.merge(blocks)


def column_mesh(column: Column, base_z: float, kernel: GeometryKernel) -> Mesh:
    c, s = math.cos(math.radians(column.rotation)), math.sin(math.radians(column.rotation))
    x, y = column.position
    w, d = column.width / 2, column.depth / 2
    profile = [(x + lx * c - ly * s, y + lx * s + ly * c) for lx, ly in ((-w, -d), (w, -d), (w, d), (-w, d))]
    return kernel.extrude(profile, base_z + column.base_offset, column.height)


def slab_mesh(slab: Slab, base_z: float, kernel: GeometryKernel) -> Mesh:
    return kernel.extrude(slab.boundary, base_z + slab.top_offset - slab.thickness, slab.thickness)


def _lift(path: Sequence[Point3], base_z: float) -> list[Point3]:
    """Storey-relative path to world coordinates."""
    return [(x, y, z + base_z) for x, y, z in path]


class Tessellator:
    """Builds the display mesh for any element in a project."""

    def __init__(self, kernel: GeometryKernel) -> None:
        self.kernel = kernel

    def mesh_for(self, element: Element, project: Project) -> Mesh | None:
        base_z = project.storey_elevation(element)
        if isinstance(element, Wall):
            return wall_mesh(element, project.hosted_openings(element.id), base_z, self.kernel)
        if isinstance(element, (Door, Window)):
            host = project.get(element.host_id)
            return opening_mesh(element, host, base_z, self.kernel) if isinstance(host, Wall) else None
        if isinstance(element, Slab):
            return slab_mesh(element, base_z, self.kernel)
        if isinstance(element, Stair):
            return stair_mesh(element, base_z, self.kernel)
        if isinstance(element, Column):
            return column_mesh(element, base_z, self.kernel)
        if isinstance(element, PipeSegment) and len(element.path) >= 2:
            return tube(_lift(element.path, base_z), element.diameter / 2)
        if isinstance(element, CableCarrierSegment) and len(element.path) >= 2:
            if element.carrier_type == "CONDUITSEGMENT":
                return tube(_lift(element.path, base_z), element.width / 2, sides=8)
            return rect_run(_lift(element.path, base_z), element.width, element.depth)
        if isinstance(element, Fixture):
            family = family_of(element)
            size = family.size if family else (0.3, 0.3, 0.3)
            x, y, z = element.position
            return placed_box((x, y, base_z + z), size, element.rotation)
        if isinstance(element, ImportedElement) and element.faces:
            return Mesh(np.asarray(element.vertices, dtype=np.float64), np.asarray(element.faces, dtype=np.int64))
        return None
