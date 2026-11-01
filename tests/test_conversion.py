import pytest

from bimforge.adapters.dxf_importer import DxfImporter
from bimforge.conversion import ConversionSettings, PlanConverter
from bimforge.conversion.layers import LayerRole, classify_block, classify_name
from bimforge.conversion.walls import WallCandidate, join_corners, merge_collinear, pair_wall_faces
from bimforge.core.model import Door, Project, Slab, Space, Wall, Window
from bimforge.core.units import LengthUnit
from bimforge.drawing.primitives import BlockRef, Drawing2D, Layer, Segment

S = ConversionSettings()


def seg(x0, y0, x1, y1, layer="A-WALL"):
    return Segment(start=(x0, y0), end=(x1, y1), layer=layer)


class TestLayerClassification:
    @pytest.mark.parametrize(
        "name, role",
        [
            ("A-WALL", LayerRole.WALL),
            ("A-WALL-PATT", LayerRole.WALL),
            ("Walls_Ext", LayerRole.WALL),
            ("A-DOOR", LayerRole.DOOR),
            ("A-GLAZ", LayerRole.WINDOW),
            ("A-ANNO-DIMS", LayerRole.ANNOTATION),
            ("A-FURN", LayerRole.FURNITURE),
            ("S-GRID", LayerRole.GRID),
            ("0", LayerRole.UNKNOWN),
        ],
    )
    def test_layer_names(self, name, role):
        assert classify_name(name) is role

    def test_block_name_beats_layer(self):
        assert classify_block("DOOR_900", "0") is LayerRole.DOOR
        assert classify_block("BLK_17", "A-GLAZ") is LayerRole.WINDOW


class TestWallPairing:
    def test_two_parallel_faces_make_one_wall(self):
        walls = pair_wall_faces([seg(0, 0, 5, 0), seg(0, 0.2, 5, 0.2)], S, {"A-WALL": 1.0})
        assert len(walls) == 1
        w = walls[0]
        assert w.thickness == pytest.approx(0.2)
        assert w.start == pytest.approx((0, 0.1)) and w.end == pytest.approx((5, 0.1))
        assert w.confidence == pytest.approx(1.0)

    def test_overlap_only_is_used(self):
        walls = pair_wall_faces([seg(0, 0, 5, 0), seg(1, 0.2, 3, 0.2)], S)
        assert walls[0].start == pytest.approx((1, 0.1)) and walls[0].end == pytest.approx((3, 0.1))

    def test_reversed_face_direction(self):
        walls = pair_wall_faces([seg(0, 0, 5, 0), seg(5, -0.3, 0, -0.3)], S)
        assert len(walls) == 1 and walls[0].thickness == pytest.approx(0.3)
        assert walls[0].start[1] == pytest.approx(-0.15)

    @pytest.mark.parametrize(
        "other",
        [
            seg(0, 1.0, 5, 1.0),  # too far apart: a corridor, not a wall
            seg(0, 0.01, 5, 0.01),  # too thin: a double line or hatch
            seg(0, 0.2, 5, 0.5),  # not parallel
            seg(6, 0.2, 9, 0.2),  # no overlap
        ],
    )
    def test_rejections(self, other):
        assert pair_wall_faces([seg(0, 0, 5, 0), other], S) == []

    def test_face_shared_by_split_opposite_faces(self):
        # Outer face continuous, inner face split by a T-junction.
        segments = [seg(0, 0, 10, 0), seg(0, 0.2, 4.95, 0.2), seg(5.05, 0.2, 10, 0.2)]
        walls = pair_wall_faces(segments, S)
        assert len(walls) == 2
        merged = merge_collinear(walls, S.max_junction_gap, S)
        assert len(merged) == 1
        assert merged[0].length == pytest.approx(10)

    def test_nearest_partner_wins(self):
        # Middle line could pair with either neighbour; the thinner pairing is taken first.
        segments = [seg(0, 0, 5, 0), seg(0, 0.1, 5, 0.1), seg(0, 0.5, 5, 0.5)]
        walls = pair_wall_faces(segments, S)
        assert walls[0].thickness == pytest.approx(0.1)


class TestJoinsAndMerges:
    def test_l_corner_extends_both_walls(self):
        a = WallCandidate((0.2, 0.1), (5, 0.1), 0.2, 1.0)
        b = WallCandidate((0.1, 0.2), (0.1, 5), 0.2, 1.0)
        a2, b2 = join_corners([a, b])
        assert a2.start == pytest.approx((0.1, 0.1))
        assert b2.start == pytest.approx((0.1, 0.1))

    def test_t_junction_extends_only_the_stem(self):
        top = WallCandidate((0, 0.1), (10, 0.1), 0.2, 1.0)
        stem = WallCandidate((5, 0.2), (5, 4), 0.1, 1.0)
        top2, stem2 = join_corners([top, stem])
        assert stem2.start == pytest.approx((5, 0.1))
        assert (top2.start, top2.end) == ((0, 0.1), (10, 0.1))

    def test_far_walls_not_joined(self):
        a = WallCandidate((0, 0), (5, 0), 0.2, 1.0)
        b = WallCandidate((2, 2), (2, 5), 0.2, 1.0)
        _, b2 = join_corners([a, b])
        assert b2.start == (2, 2)

    def test_merge_requires_same_thickness(self):
        a = WallCandidate((0, 0), (4, 0), 0.2, 1.0)
        b = WallCandidate((4.1, 0), (8, 0), 0.1, 1.0)
        assert len(merge_collinear([a, b], 0.5, S)) == 2

    def test_merge_predicate_blocks_large_gaps(self):
        a = WallCandidate((0, 0), (4, 0), 0.2, 0.9)
        b = WallCandidate((5, 0), (8, 0), 0.2, 0.7)
        assert len(merge_collinear([a, b], 2.0, S, allow=lambda *_: False)) == 2
        merged = merge_collinear([a, b], 2.0, S, allow=lambda *_: True)
        assert len(merged) == 1 and merged[0].confidence == pytest.approx(0.7)


@pytest.fixture(scope="module")
def result(sample_dxf):
    drawing = DxfImporter().import_file(sample_dxf).drawing
    return PlanConverter().run(drawing, Project().default_storey)


class TestPipeline:
    def test_detects_expected_elements(self, result):
        assert result.unit is LengthUnit.MILLIMETRE
        assert result.summary() == {"Wall": 5, "Door": 1, "Window": 1, "Space": 2, "Slab": 1}
        assert result.warnings == []

    def test_walls_are_joined_on_centrelines(self, result):
        walls = [p.element for p in result.proposals if isinstance(p.element, Wall)]
        exterior = sorted(round(w.length, 3) for w in walls if w.thickness == pytest.approx(0.2))
        assert exterior == [7.8, 7.8, 9.8, 9.8]
        partition = next(w for w in walls if w.thickness == pytest.approx(0.1))
        assert sorted([partition.start[1], partition.end[1]]) == pytest.approx([0.1, 7.9])

    def test_openings_are_hosted_at_gap_centres(self, result):
        by_id = {p.element.id: p.element for p in result.proposals}
        door = next(e for e in by_id.values() if isinstance(e, Door))
        window = next(e for e in by_id.values() if isinstance(e, Window))
        door_host, window_host = by_id[door.host_id], by_id[window.host_id]
        assert door_host.point_at(door.offset) == pytest.approx((5.0, 3.45))
        assert door.width == pytest.approx(0.9)
        assert window_host.point_at(window.offset) == pytest.approx((2.6, 0.1))
        assert window.width == pytest.approx(1.2) and window.sill_height == pytest.approx(0.9)

    def test_rooms_and_slab(self, result):
        spaces = [p.element for p in result.proposals if isinstance(p.element, Space)]
        assert sorted(round(s.area, 2) for s in spaces) == [38.22, 38.22]
        slab = next(p.element for p in result.proposals if isinstance(p.element, Slab))
        xs, ys = zip(*slab.boundary)
        assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((0, 10, 0, 8))

    def test_rejecting_host_drops_its_openings(self, result):
        door = next(p.element for p in result.proposals if isinstance(p.element, Door))
        host = next(p for p in result.proposals if p.element.id == door.host_id)
        host.accepted = False
        try:
            ids = {e.id for e in result.accepted_elements()}
            assert door.id not in ids and host.element.id not in ids
        finally:
            host.accepted = True


def test_unitless_drawing_guesses_millimetres():
    drawing = Drawing2D(
        layers={"A-WALL": Layer(name="A-WALL")},
        primitives=[seg(0, 0, 8000, 0), seg(0, 200, 8000, 200)],
    )
    result = PlanConverter().run(drawing, Project().default_storey)
    assert result.unit is LengthUnit.MILLIMETRE
    assert any("no units" in w for w in result.warnings)
    wall = result.proposals[0].element
    assert wall.thickness == pytest.approx(0.2) and wall.length == pytest.approx(8)


def test_hidden_and_annotation_layers_ignored():
    drawing = Drawing2D(
        unit=LengthUnit.METRE,
        layers={"A-WALL": Layer(name="A-WALL", visible=False), "DIMS": Layer(name="DIMS")},
        primitives=[seg(0, 0, 5, 0), seg(0, 0.2, 5, 0.2), seg(0, 0, 5, 0, "DIMS"), seg(0, 0.2, 5, 0.2, "DIMS")],
    )
    assert PlanConverter().run(drawing, Project().default_storey).proposals == []


def test_symbol_without_gap_is_hosted_with_lower_confidence():
    drawing = Drawing2D(
        unit=LengthUnit.METRE,
        layers={"A-WALL": Layer(name="A-WALL")},
        primitives=[
            seg(0, 0, 6, 0),
            seg(0, 0.2, 6, 0.2),
            BlockRef(name="WINDOW", position=(2, 0), extent=((0, 0), (1.5, 0.2)), layer="0"),
        ],
    )
    result = PlanConverter().run(drawing, Project().default_storey)
    window = next(p for p in result.proposals if p.kind == "Window")
    assert window.element.width == pytest.approx(1.5)
    assert window.element.offset == pytest.approx(2.75)
    assert window.confidence < 0.9
    assert window.note == "placed from symbol position"
