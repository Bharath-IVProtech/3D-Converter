"""Mirrored CAD objects must stay where they were drawn, and big drawings must report tidily."""

import math

import numpy as np
import pytest

ezdxf = pytest.importorskip("ezdxf")

from bimforge.adapters.dxf_importer import DxfImporter  # noqa: E402
from bimforge.conversion.layers import LayerRole, classify_block  # noqa: E402
from bimforge.conversion.pipeline import _unplaced_notes  # noqa: E402
from bimforge.conversion.openings import OpeningSymbol  # noqa: E402
from bimforge.drawing.primitives import Arc, BlockRef, Polyline, Text  # noqa: E402
from bimforge.geometry.mesh_ops import feature_edges, ray_hits, thin_lines  # noqa: E402


def read(doc):
    return DxfImporter().read_document(doc, "test").drawing


def mirrored_doc():
    """A block at (1000, 500), mirrored left to right as CAD's MIRROR command stores it."""
    doc = ezdxf.new("R2018")
    block = doc.blocks.new("UNIT")
    block.add_lwpolyline([(10, 0), (20, 0), (20, 5)])
    block.add_arc((30, 0), radius=2, start_angle=0, end_angle=90)
    block.add_circle((40, 0), radius=1)
    block.add_text("A1", dxfattribs={"height": 1, "insert": (50, 0)})
    doc.modelspace().add_blockref("UNIT", (-1000, 500), dxfattribs={"extrusion": (0, 0, -1)})
    return doc


class TestMirroredObjects:
    def test_a_flipped_block_stays_on_its_side_of_the_origin(self):
        drawing = read(mirrored_doc())
        (polyline,) = drawing.of_type(Polyline)
        assert polyline.points[0] == pytest.approx((990, 500))
        assert polyline.points[2] == pytest.approx((980, 505))
        arc, circle = drawing.of_type(Arc)
        assert arc.center == pytest.approx((970, 500)) and circle.center == pytest.approx((960, 500))
        assert drawing.of_type(Text)[0].position == pytest.approx((950, 500))

    def test_a_flipped_arc_covers_the_mirrored_quarter(self):
        arc = read(mirrored_doc()).of_type(Arc)[0]
        assert arc.start_angle == pytest.approx(90) and arc.end_angle == pytest.approx(180)

    def test_flipped_block_reference_places_its_symbol_correctly(self):
        (ref,) = read(mirrored_doc()).of_type(BlockRef)
        assert ref.position == pytest.approx((1000, 500))
        assert ref.to_world((10, 0)) == pytest.approx((990, 500))

    def test_rotated_then_flipped_reference(self):
        doc = ezdxf.new("R2018")
        block = doc.blocks.new("UNIT")
        block.add_lwpolyline([(10, 0), (20, 0)])
        doc.modelspace().add_blockref("UNIT", (-100, 0), dxfattribs={"extrusion": (0, 0, -1), "rotation": 30})
        drawing = read(doc)
        (ref,), (polyline,) = drawing.of_type(BlockRef), drawing.of_type(Polyline)
        assert ref.to_world((10, 0)) == pytest.approx(polyline.points[0])

    def test_negative_scale_mirror_is_placed_correctly(self):
        doc = ezdxf.new("R2018")
        block = doc.blocks.new("UNIT")
        block.add_lwpolyline([(10, 0), (20, 0)])
        block.add_arc((30, 0), radius=2, start_angle=0, end_angle=90)
        doc.modelspace().add_blockref("UNIT", (1000, 500), dxfattribs={"xscale": -1})
        drawing = read(doc)
        assert drawing.of_type(Polyline)[0].points[0] == pytest.approx((990, 500))
        assert drawing.of_type(Arc)[0].center == pytest.approx((970, 500))

    def test_ordinary_objects_are_untouched(self):
        doc = ezdxf.new("R2018")
        doc.modelspace().add_lwpolyline([(1, 2), (3, 4)])
        assert read(doc).of_type(Polyline)[0].points == ((1, 2), (3, 4))


@pytest.mark.parametrize("name", ["M_Door Tag - M_Door Tag-1556307-BASEMENT FLOOR PLAN", "DOOR_TAG", "Door Schedule",
                                  "XREF$0$FHC Door - FSD 1200x2250-V24"])
def test_things_that_only_mention_doors_are_not_doors(name):
    assert classify_block(name, "A-DOOR") is LayerRole.UNKNOWN


def test_real_door_blocks_still_count():
    assert classify_block("M_Single-Flush - 900x2100", "A-DOOR") is LayerRole.DOOR
    assert classify_block("DOOR-900", "0") is LayerRole.DOOR


def test_many_unplaced_symbols_make_one_note():
    symbols = [OpeningSymbol(kind="door", anchors=((0, 0),), width=None, confidence=0.9, handle="", name=f"X$0$D{i % 5}")
               for i in range(200)]
    (note,) = _unplaced_notes(symbols)
    assert note.startswith("200 door symbols") and "2 other kinds" in note and "X$0$" not in note


class TestMeshOps:
    CUBE_V = np.array([(x, y, z) for z in (0, 1) for y in (0, 1) for x in (0, 1)], dtype=float)
    CUBE_F = np.array([(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 5), (0, 5, 4),
                       (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)])

    def test_a_cube_has_twelve_edges_and_no_diagonals(self):
        edges = feature_edges(self.CUBE_V, self.CUBE_F)
        assert len(edges) == 12
        assert np.allclose(np.linalg.norm(edges[:, 1] - edges[:, 0], axis=1), 1.0)

    def test_where_two_pieces_of_one_solid_touch_is_not_an_edge(self):
        second = self.CUBE_V + (1, 0, 0)
        vertices = np.vstack([self.CUBE_V, second])
        edges = feature_edges(vertices, np.vstack([self.CUBE_F, self.CUBE_F + 8]))
        at_seam = np.all(np.isclose(edges[:, :, 0], 1.0), axis=1)
        assert not at_seam.any() and len(edges) == 16

    def test_an_open_sheet_shows_its_border(self):
        vertices = np.array([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], dtype=float)
        assert len(feature_edges(vertices, np.array([(0, 1, 2), (0, 2, 3)]))) == 4

    def test_duplicated_corners_still_merge(self):
        faces = self.CUBE_F.ravel().reshape(-1, 3)
        vertices = self.CUBE_V[faces.ravel()]
        assert len(feature_edges(vertices, np.arange(len(vertices)).reshape(-1, 3))) == 12

    def test_nothing_to_draw(self):
        assert feature_edges(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)).shape == (0, 2, 3)

    def test_ray_finds_nearest_face_from_either_side(self):
        a = self.CUBE_V[self.CUBE_F[:, 0]]
        e1, e2 = self.CUBE_V[self.CUBE_F[:, 1]] - a, self.CUBE_V[self.CUBE_F[:, 2]] - a
        t = ray_hits(np.array([0.25, 0.3, 5.0]), np.array([0.0, 0.0, -1.0]), a, e1, e2)
        assert np.min(t) == pytest.approx(4.0) and np.isfinite(t).sum() == 2
        assert np.all(np.isinf(ray_hits(np.array([9.0, 9.0, 5.0]), np.array([0.0, 0.0, -1.0]), a, e1, e2)))

    def test_ray_ignores_what_is_behind_it(self):
        a, e1, e2 = np.array([[0.0, 0, 0]]), np.array([[1.0, 0, 0]]), np.array([[0.0, 1, 0]])
        assert math.isinf(ray_hits(np.array([0.2, 0.2, -1.0]), np.array([0.0, 0, -1.0]), a, e1, e2)[0])

    def test_small_drawings_keep_every_line(self):
        ends = np.array([(0, 0, 0.001, 0), (5, 5, 6, 6), (5, 5, 6, 6)], dtype=float)
        assert len(thin_lines(ends, 10, 0.03)) == 2

    def test_big_drawings_drop_the_tiniest_and_keep_the_longest(self):
        short = np.column_stack([np.arange(100.0), np.zeros(100), np.arange(100.0) + 0.001, np.zeros(100)])
        long = np.column_stack([np.arange(10.0), np.ones(10), np.arange(10.0), 2 + np.arange(10.0)])
        kept = thin_lines(np.vstack([short, long]), 5, 0.03)
        assert len(kept) == 5 and np.min(np.hypot(kept[:, 2] - kept[:, 0], kept[:, 3] - kept[:, 1])) >= 6


class TestBlockSizes:
    @staticmethod
    def extent(doc, name):
        importer = DxfImporter()
        importer._doc = doc
        importer._boxes = {}
        return importer._block_extent(name)

    def test_a_door_swing_counts_only_the_quarter_it_covers(self):
        doc = ezdxf.new("R2018")
        door = doc.blocks.new("DOOR")
        door.add_line((0, 0), (0, 900))
        door.add_arc((0, 0), radius=900, start_angle=0, end_angle=90)
        assert self.extent(doc, "DOOR") == ((0, 0), (900, 900))

    def test_an_arc_past_an_axis_reaches_the_full_radius(self):
        doc = ezdxf.new("R2018")
        block = doc.blocks.new("B")
        block.add_arc((0, 0), radius=10, start_angle=350, end_angle=10)
        (x0, y0), (x1, y1) = self.extent(doc, "B")
        assert x1 == pytest.approx(10) and x0 == pytest.approx(10 * math.cos(math.radians(10)))
        assert y0 == pytest.approx(-y1)

    def test_base_point_and_nested_blocks_with_scale_and_turn(self):
        doc = ezdxf.new("R2018")
        inner = doc.blocks.new("INNER")
        inner.add_line((0, 0), (10, 0))
        outer = doc.blocks.new("OUTER", base_point=(100, 100))
        outer.add_blockref("INNER", (100, 100), dxfattribs={"xscale": 2, "rotation": 90})
        (x0, y0), (x1, y1) = self.extent(doc, "OUTER")
        assert (x0, y0) == pytest.approx((0, 0)) and (x1, y1) == pytest.approx((0, 20))

    def test_text_does_not_widen_a_symbol(self):
        doc = ezdxf.new("R2018")
        block = doc.blocks.new("B")
        block.add_line((0, 0), (900, 0))
        block.add_text("A VERY LONG DOOR NUMBER", dxfattribs={"height": 100, "insert": (5000, 0)})
        assert self.extent(doc, "B")[1][0] == pytest.approx(900)

    def test_a_block_holding_only_a_hatch_is_still_measured(self):
        doc = ezdxf.new("R2018")
        block = doc.blocks.new("B")
        block.add_hatch().paths.add_polyline_path([(0, 0), (5, 0), (5, 5)])
        assert self.extent(doc, "B")[1] == pytest.approx((5, 5))

    def test_a_block_that_contains_itself_does_not_hang(self):
        doc = ezdxf.new("R2018")
        block = doc.blocks.new("B")
        block.add_line((0, 0), (1, 0))
        block.add_blockref("B", (0, 0))
        assert self.extent(doc, "B") == ((0, 0), (1, 0))


class TestStampingMatchesExploding:
    """A block is now read once and stamped. The stamps must equal what ezdxf's slow explode gives."""

    PLACEMENTS = [
        {},
        {"rotation": 37},
        {"xscale": 2, "yscale": 2},
        {"xscale": -1},
        {"yscale": -1, "rotation": 15},
        {"extrusion": (0, 0, -1), "rotation": 30},
    ]

    @staticmethod
    def doc(attribs):
        doc = ezdxf.new("R2018")
        inner = doc.blocks.new("INNER", base_point=(1, 2))
        inner.add_line((1, 2), (5, 2), dxfattribs={"layer": "A-WALL"})
        inner.add_lwpolyline([(0, 0), (4, 0), (4, 3)], close=True, dxfattribs={"const_width": 0.5})
        inner.add_arc((2, 2), radius=1.5, start_angle=20, end_angle=110)
        inner.add_circle((6, 6), radius=2)
        inner.add_text("T", dxfattribs={"insert": (3, 3), "height": 0.5, "rotation": 10})
        outer = doc.blocks.new("OUTER", base_point=(-3, 1))
        outer.add_blockref("INNER", (10, 0), dxfattribs={"rotation": 45, "layer": "NEST"})
        outer.add_line((0, 0), (0, 9))
        doc.modelspace().add_blockref("OUTER", (100, 50), dxfattribs={"layer": "TOP", **attribs})
        return doc

    @staticmethod
    def exploded(doc):
        """The old way: ezdxf copies and transforms every object at every level."""
        lines, polylines, arcs, texts = [], [], [], []

        def walk(entity, parent):
            layer = entity.dxf.get("layer", "0")
            layer = parent if layer == "0" else layer
            kind = entity.dxftype()
            if kind == "INSERT":
                for child in entity.virtual_entities():
                    walk(child, layer)
            elif kind == "LINE":
                lines.append((layer, entity.dxf.start, entity.dxf.end))
            elif kind == "LWPOLYLINE":
                ocs = entity.ocs()
                polylines.append((layer, [ocs.to_wcs((x, y, 0)) for x, y in entity.get_points("xy")]))
            elif kind in ("ARC", "CIRCLE"):
                arcs.append((layer, entity.ocs().to_wcs(entity.dxf.center), entity.dxf.radius))
            elif kind == "TEXT":
                texts.append((layer, entity.ocs().to_wcs(entity.dxf.insert)))

        for entity in doc.modelspace():
            walk(entity, "0")
        return lines, polylines, arcs, texts

    @pytest.mark.parametrize("attribs", PLACEMENTS)
    def test_same_objects_in_the_same_places(self, attribs):
        from bimforge.drawing.primitives import Segment

        doc = self.doc(attribs)
        lines, polylines, arcs, texts = self.exploded(doc)
        drawing = read(doc)
        got = drawing.of_type(Segment)
        assert len(got) == len(lines) == 2
        for (layer, a, b) in lines:
            match = [s for s in got if s.start == pytest.approx((a[0], a[1]), abs=1e-6) and s.end == pytest.approx((b[0], b[1]), abs=1e-6)]
            assert match and match[0].layer == layer
        (polyline,) = drawing.of_type(Polyline)
        assert polyline.layer == polylines[0][0]
        for p, q in zip(polyline.points, polylines[0][1]):
            assert p == pytest.approx((q[0], q[1]), abs=1e-6)
        got_arcs = sorted(drawing.of_type(Arc), key=lambda x: x.radius)
        for found, (layer, centre, radius) in zip(got_arcs, sorted(arcs, key=lambda x: x[2])):
            assert found.center == pytest.approx((centre[0], centre[1]), abs=1e-6)
            assert found.radius == pytest.approx(radius) and found.layer == layer
        assert drawing.of_type(Text)[0].position == pytest.approx((texts[0][1][0], texts[0][1][1]), abs=1e-6)

    @pytest.mark.parametrize("attribs", PLACEMENTS)
    def test_nested_references_keep_their_position(self, attribs):
        doc = self.doc(attribs)
        refs = {r.name: r for r in read(doc).of_type(BlockRef)}
        lines = self.exploded(doc)[0]
        start_of_inner_line = next(a for layer, a, b in lines if layer == "A-WALL")
        assert refs["INNER"].to_world((0, 0)) == pytest.approx((start_of_inner_line[0], start_of_inner_line[1]), abs=1e-6)

    def test_uneven_scale_keeps_an_arc_as_short_lines(self):
        doc = ezdxf.new("R2018")
        doc.blocks.new("B").add_arc((0, 0), radius=1, start_angle=0, end_angle=90)
        doc.modelspace().add_blockref("B", (0, 0), dxfattribs={"xscale": 2, "yscale": 1})
        (polyline,) = read(doc).of_type(Polyline)
        assert polyline.points[0] == pytest.approx((2, 0)) and polyline.points[-1] == pytest.approx((0, 1), abs=1e-9)

    def test_a_grid_of_placements(self):
        from bimforge.drawing.primitives import Segment

        doc = ezdxf.new("R2018")
        doc.blocks.new("B").add_line((0, 0), (1, 0))
        doc.modelspace().add_blockref("B", (0, 0), dxfattribs={"column_count": 3, "column_spacing": 10, "row_count": 2, "row_spacing": 5})
        starts = sorted(s.start for s in read(doc).of_type(Segment))
        assert starts == [(0, 0), (0, 5), (10, 0), (10, 5), (20, 0), (20, 5)]


class TestDrawingCache:
    @staticmethod
    def big():
        from bimforge.drawing.primitives import Drawing2D, Segment

        drawing = Drawing2D(source="x")
        drawing.primitives = [Segment(start=(0, 0), end=(1, 0))] * 100_000
        drawing.warnings.append("a note")
        return drawing

    def test_same_file_is_remembered_and_a_changed_file_is_not(self, tmp_path, monkeypatch):
        from bimforge.adapters import drawing_cache

        monkeypatch.setenv("BIMFORGE_CACHE_DIR", str(tmp_path / "cache"))
        source = tmp_path / "plan.dwg"
        source.write_bytes(b"one")
        assert drawing_cache.load(source) is None
        drawing_cache.save(source, "", self.big())
        again = drawing_cache.load(source)
        assert len(again.primitives) == 100_000 and again.warnings == ["a note"]
        source.write_bytes(b"something else")
        assert drawing_cache.load(source) is None

    def test_small_drawings_are_not_kept(self, tmp_path, monkeypatch):
        from bimforge.adapters import drawing_cache
        from bimforge.drawing.primitives import Drawing2D

        monkeypatch.setenv("BIMFORGE_CACHE_DIR", str(tmp_path / "cache"))
        source = tmp_path / "plan.dwg"
        source.write_bytes(b"one")
        drawing_cache.save(source, "", Drawing2D(source="x"))
        assert not (tmp_path / "cache").exists()

    def test_damaged_entry_is_ignored_and_only_recent_ones_stay(self, tmp_path, monkeypatch):
        from bimforge.adapters import drawing_cache

        monkeypatch.setenv("BIMFORGE_CACHE_DIR", str(tmp_path / "cache"))
        sources = [tmp_path / f"p{i}.dwg" for i in range(5)]
        for source in sources:
            source.write_bytes(b"x")
            drawing_cache.save(source, "", self.big())
        assert len(list((tmp_path / "cache").glob("*.drawing"))) == drawing_cache.KEEP
        entry = next((tmp_path / "cache").glob("*.drawing"))
        entry.write_bytes(b"not a drawing")
        for source in sources:
            drawing_cache.load(source)  # must not raise


class TestFraming:
    @staticmethod
    def boxes(centres, size=1.0):
        c = np.array(centres, dtype=float)
        return np.column_stack([c[:, 0], c[:, 0] + size, c[:, 1], c[:, 1] + size, np.zeros(len(c)), np.full(len(c), 3.0)])

    def test_a_few_objects_far_away_do_not_count(self):
        from bimforge.geometry.mesh_ops import main_extent

        main = [(x, y) for x in range(20) for y in range(10)]
        frame = main_extent(self.boxes(main + [(5000, 5), (5001, 6)]))
        assert frame[1] == 20 and frame[3] == 10

    def test_two_plans_side_by_side_both_count(self):
        from bimforge.geometry.mesh_ops import main_extent

        left = [(x, 0) for x in range(30)]
        right = [(x + 500, 0) for x in range(30)]
        assert main_extent(self.boxes(left + right))[1] == 530

    def test_ordinary_plans_are_untouched(self):
        from bimforge.geometry.mesh_ops import main_extent

        assert main_extent(self.boxes([(x * 3, 0) for x in range(10)]))[:2] == (0, 28)
        assert main_extent(np.zeros((0, 6))) is None

    @pytest.mark.parametrize("aspect", [0.6, 1.0, 1.8, 2.6])
    @pytest.mark.parametrize("direction", [(0, 0, -1.0), (-1.0, 1.0, -0.8), (0, 1.0, 0)])
    def test_a_fit_shows_every_corner_and_fills_one_direction(self, aspect, direction):
        from bimforge.geometry.mesh_ops import fit_camera

        bounds = (-2700.0, -2400.0, 350.0, 500.0, 0.0, 3.0)
        d = np.array(direction)
        up = np.array([0, 1.0, 0]) if abs(d[2]) == 1 else np.array([0, 0, 1.0])
        tan_v = math.tan(math.radians(15))
        target, distance, _ = fit_camera(bounds, d, up, tan_v, aspect)
        forward = d / np.linalg.norm(d)
        right = np.cross(forward, up)
        right /= np.linalg.norm(right)
        upward = np.cross(right, forward)
        camera = target - forward * distance
        widest = 0.0
        for x in bounds[:2]:
            for y in bounds[2:4]:
                for z in bounds[4:]:
                    v = np.array([x, y, z]) - camera
                    depth = v @ forward
                    assert depth > 0
                    sx, sy = (v @ right) / depth / (tan_v * aspect), (v @ upward) / depth / tan_v
                    assert abs(sx) <= 1.0 and abs(sy) <= 1.0
                    widest = max(widest, abs(sx), abs(sy))
        assert widest > 0.85


def test_equipment_notes_are_not_room_names():
    from bimforge.conversion.labels import TextBlock, is_room_name

    for text in ("FHC", "PIPE DROP"):
        assert not is_room_name(TextBlock(text, (0, 0), 0.2, (0, 0, 1, 1)))


def test_a_mirrored_arc_covers_the_points_it_really_does():
    from ezdxf import path as ezpath

    for attribs in ({"xscale": -1}, {"yscale": -1}, {"extrusion": (0, 0, -1)}, {"xscale": -1, "rotation": 30}):
        doc = ezdxf.new("R2018")
        doc.blocks.new("B").add_arc((10, 0), radius=5, start_angle=65, end_angle=97)
        reference = doc.modelspace().add_blockref("B", (100, 50), dxfattribs=attribs)
        true_points = list(ezpath.make_path(next(iter(reference.virtual_entities()))).flattening(0.01))
        arc = read(doc).of_type(Arc)[0]
        sweep = (arc.end_angle - arc.start_angle) % 360
        assert sweep == pytest.approx(32)
        for q in true_points:
            angle = math.degrees(math.atan2(q.y - arc.center[1], q.x - arc.center[0]))
            assert (angle - arc.start_angle + 0.5) % 360 <= sweep + 1
