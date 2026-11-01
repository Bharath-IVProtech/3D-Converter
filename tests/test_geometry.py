import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bimforge.core.model import Door, Project, Slab, Wall, Window
from bimforge.geometry.builders import Tessellator, wall_mesh, wall_pieces
from bimforge.geometry.kernel import NumpyKernel
from bimforge.geometry.mesh import Mesh
from bimforge.geometry.polygon import signed_area, triangulate

kernel = NumpyKernel()


def test_triangulate_l_shape_preserves_area():
    l_shape = [(0, 0), (4, 0), (4, 1), (1, 1), (1, 3), (0, 3)]
    ring, tris = triangulate(l_shape)
    assert len(tris) == len(ring) - 2
    area = sum(abs(signed_area([ring[i], ring[j], ring[k]])) for i, j, k in tris)
    assert area == pytest.approx(6.0)


def test_triangulate_accepts_clockwise_and_closed_rings():
    ring, tris = triangulate([(0, 0), (0, 2), (2, 2), (2, 0), (0, 0)])
    assert signed_area(ring) > 0
    assert len(tris) == 2


def test_triangulate_drops_collinear_vertices():
    ring, _ = triangulate([(0, 0), (1, 0), (2, 0), (2, 2), (0, 2)])
    assert len(ring) == 4


def test_triangulate_rejects_degenerate():
    with pytest.raises(ValueError):
        triangulate([(0, 0), (1, 0), (2, 0)])


@given(
    w=st.floats(0.05, 50),
    d=st.floats(0.05, 50),
    h=st.floats(0.05, 20),
    z0=st.floats(-10, 10),
)
def test_extruded_box_volume(w, d, h, z0):
    mesh = kernel.extrude([(0, 0), (w, 0), (w, d), (0, d)], z0, h)
    assert mesh.volume() == pytest.approx(w * d * h, rel=1e-9)
    lo, hi = mesh.bounds()
    assert lo[2] == pytest.approx(z0) and hi[2] == pytest.approx(z0 + h)


def test_extrude_concave_profile_volume_and_closed():
    mesh = kernel.extrude([(0, 0), (4, 0), (4, 1), (1, 1), (1, 3), (0, 3)], 0, 2)
    assert mesh.volume() == pytest.approx(12.0)
    # Closed manifold: every undirected edge is shared by exactly two faces.
    edges: dict[tuple[int, int], int] = {}
    for f in mesh.faces:
        for a, b in ((f[0], f[1]), (f[1], f[2]), (f[2], f[0])):
            key = (min(a, b), max(a, b))
            edges[key] = edges.get(key, 0) + 1
    assert set(edges.values()) == {2}


def test_extrude_rejects_non_positive_height():
    with pytest.raises(ValueError):
        kernel.extrude([(0, 0), (1, 0), (1, 1)], 0, 0)


def test_mesh_merge_offsets_indices():
    a = kernel.extrude([(0, 0), (1, 0), (1, 1), (0, 1)], 0, 1)
    b = kernel.extrude([(5, 0), (6, 0), (6, 1), (5, 1)], 0, 1)
    merged = Mesh.merge([a, b])
    assert len(merged.vertices) == len(a.vertices) + len(b.vertices)
    assert merged.faces.max() == len(merged.vertices) - 1
    assert merged.volume() == pytest.approx(2.0)


class TestWallPieces:
    def test_no_openings_is_one_piece(self):
        assert wall_pieces(5, 3, []) == [(0.0, 5, 0.0, 3)]

    def test_door_leaves_head_piece(self):
        pieces = wall_pieces(5, 3, [(1, 2, 0, 2.1)])
        assert pieces == [(0.0, 1, 0.0, 3), (1, 2, 2.1, 3), (2, 5, 0.0, 3)]

    def test_window_leaves_sill_and_head(self):
        pieces = wall_pieces(5, 3, [(1, 2, 0.9, 2.1)])
        assert (1, 2, 0.0, 0.9) in pieces and (1, 2, 2.1, 3) in pieces
        assert len(pieces) == 4

    def test_opening_at_wall_end_and_full_height(self):
        assert wall_pieces(5, 3, [(4, 6, 0, 5)]) == [(0.0, 4, 0.0, 3)]

    def test_overlapping_openings_rejected(self):
        with pytest.raises(ValueError):
            wall_pieces(5, 3, [(1, 2, 0, 2), (1.5, 2.5, 0, 2)])

    @given(st.lists(st.tuples(st.floats(0, 9), st.floats(0.1, 1), st.floats(0, 2), st.floats(0.1, 1)), max_size=5))
    def test_pieces_plus_openings_cover_wall_area(self, raw):
        # Build non-overlapping openings by laying them out sequentially.
        openings, cursor = [], 0.0
        for gap, width, sill, height in raw:
            s0 = cursor + gap / 5
            openings.append((s0, s0 + width, sill, sill + height))
            cursor = s0 + width
        length, height = 10.0, 3.0
        pieces = wall_pieces(length, height, openings)
        solid = sum((s1 - s0) * (z1 - z0) for s0, s1, z0, z1 in pieces)
        voids = sum(
            max(0, min(length, s1) - max(0, s0)) * max(0, min(height, z1) - max(0, z0))
            for s0, s1, z0, z1 in openings
        )
        assert solid + voids == pytest.approx(length * height)


def test_wall_mesh_with_window_subtracts_opening_volume():
    wall = Wall(start=(0, 0), end=(4, 0), thickness=0.2, height=3)
    window = Window(host_id=wall.id, offset=2, width=1.2, height=1.2, sill_height=0.9)
    mesh = wall_mesh(wall, [window], base_z=0, kernel=kernel)
    assert mesh.volume() == pytest.approx(4 * 0.2 * 3 - 1.2 * 1.2 * 0.2)


def test_wall_mesh_follows_diagonal_centreline():
    wall = Wall(start=(1, 1), end=(4, 5), thickness=0.3, height=2.5)  # length 5
    mesh = wall_mesh(wall, [], base_z=3.0, kernel=kernel)
    assert mesh.volume() == pytest.approx(5 * 0.3 * 2.5)
    lo, hi = mesh.bounds()
    assert lo[2] == pytest.approx(3.0) and hi[2] == pytest.approx(5.5)
    centroid = mesh.vertices.mean(axis=0)
    assert centroid[:2] == pytest.approx([2.5, 3.0])


def test_tessellator_uses_storey_elevation_and_hosts():
    project = Project()
    storey = project.default_storey
    storey.elevation = 3.0
    wall = Wall(start=(0, 0), end=(3, 0), storey_id=storey.id)
    door = Door(host_id=wall.id, offset=1.5, storey_id=storey.id)
    slab = Slab(boundary=[(0, 0), (3, 0), (3, 3), (0, 3)], thickness=0.25, storey_id=storey.id)
    for e in (wall, door, slab):
        project.add(e)
    tess = Tessellator(kernel)
    assert tess.mesh_for(wall, project).volume() == pytest.approx(3 * 0.2 * 3 - 0.9 * 2.1 * 0.2)
    door_lo, _ = tess.mesh_for(door, project).bounds()
    assert door_lo[2] == pytest.approx(3.0)
    slab_lo, slab_hi = tess.mesh_for(slab, project).bounds()
    assert (slab_lo[2], slab_hi[2]) == pytest.approx((2.75, 3.0))


def test_wall_direction_and_normal():
    wall = Wall(start=(0, 0), end=(0, 2))
    assert wall.direction == pytest.approx((0, 1))
    assert wall.normal == pytest.approx((-1, 0))
    assert wall.point_at(1) == pytest.approx((0, 1))
    assert math.isclose(wall.length, 2)
