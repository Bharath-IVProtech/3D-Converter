"""Detectors for elements real drawings draw without blocks or layers: stairs, flattened door arcs,
hatched and filled walls, columns."""

import math

import ezdxf
import pytest

from bimforge.adapters.dxf_importer import DxfImporter
from bimforge.conversion import ConversionSettings, PlanConverter
from bimforge.conversion.fills import filled_elements
from bimforge.conversion.openings import arcs_from_chains, find_infill_windows, symbols_from_arcs
from bimforge.conversion.stairs import detect_stairs
from bimforge.conversion.walls import WallCandidate, hatch_walls
from bimforge.core.model import Column, Door, Project, Stair, Wall
from bimforge.core.units import LengthUnit
from bimforge.drawing.primitives import Drawing2D, Layer, Polyline, Segment
from bimforge.geometry.builders import Tessellator
from bimforge.geometry.kernel import NumpyKernel

S = ConversionSettings()


def seg(x0, y0, x1, y1, layer="L"):
    return Segment(start=(x0, y0), end=(x1, y1), layer=layer)


class TestStairs:
    def test_straight_flight(self):
        treads = [seg(0, i * 0.28, 1.2, i * 0.28) for i in range(10)]
        (stair,) = detect_stairs(treads)
        assert stair.risers == 10 and stair.width == pytest.approx(1.2)
        assert math.dist(stair.start, stair.end) == pytest.approx(10 * 0.28)
        assert stair.used == set(range(10))

    def test_diagonal_flight_and_duplicated_treads(self):
        a = math.radians(40)
        u, n = (math.cos(a), math.sin(a)), (-math.sin(a), math.cos(a))
        treads = []
        for i in range(8):
            o = (n[0] * i * 0.25, n[1] * i * 0.25)
            line = seg(o[0], o[1], o[0] + u[0] * 1.5, o[1] + u[1] * 1.5)
            treads += [line, line]  # drawn twice, in two line weights
        (stair,) = detect_stairs(treads)
        assert stair.risers == 8 and stair.width == pytest.approx(1.5)
        assert len(stair.used) == 16

    @pytest.mark.parametrize("segments", [
        [seg(0, i * 0.28, 1.2, i * 0.28) for i in range(3)],  # too few treads
        [seg(0, y, 1.2, y) for y in (0, 0.28, 0.6, 0.75, 1.3)],  # uneven spacing
        [seg(0, i * 0.3, 12, i * 0.3) for i in range(10)],  # table rows: far too long for treads
        [seg(i * 0.3, 0, i * 0.3 + 1.2, 0.0001) for i in range(6)],  # collinear, not stacked
    ])
    def test_not_stairs(self, segments):
        assert detect_stairs(segments) == []

    def test_stair_geometry_steps_up(self):
        p = Project()
        stair = Stair(start=(0, 0), end=(2.8, 0), width=1.0, risers=10, height=1.7)
        p.add(stair)
        mesh = Tessellator(NumpyKernel()).mesh_for(stair, p)
        assert mesh.bounds()[1][2] == pytest.approx(1.7)
        # Solid of stepped blocks: sum of (going * width * k * riser) for k = 1..10
        assert mesh.volume() == pytest.approx(0.28 * 1.0 * 0.17 * sum(range(1, 11)))


class TestFlattenedArcs:
    @staticmethod
    def flattened(cx, cy, r, a0, a1, pieces=10):
        pts = [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / pieces)),
                cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / pieces))) for i in range(pieces + 1)]
        return [seg(*p, *q) for p, q in zip(pts, pts[1:])]

    def test_single_swing(self):
        (arc,) = arcs_from_chains(self.flattened(5, 5, 0.9, 90, 180))
        assert arc.center == pytest.approx((5, 5), abs=1e-3) and arc.radius == pytest.approx(0.9, rel=1e-3)
        (symbol,) = symbols_from_arcs([arc], 1.0)
        assert symbol.kind == "door" and symbol.width == pytest.approx(0.9, rel=1e-3)

    def test_double_door_leaves_meeting_at_a_cusp_are_split(self):
        # Two leaves hinged at (0, 0) and (0, 1.92), swings meeting at (0.96, 0.96).
        lines = self.flattened(0, 0, 0.96, 0, 90) + self.flattened(0, 1.92, 0.96, 270, 360)
        arcs = arcs_from_chains(lines)
        assert sorted(round(a.radius, 2) for a in arcs) == [0.96, 0.96]
        assert len(symbols_from_arcs(arcs, 1.0)) == 2

    def test_polygonal_outline_is_not_an_arc(self):
        box = [seg(0, 0, 0.2, 0), seg(0.2, 0, 0.2, 0.2), seg(0.2, 0.2, 0, 0.2), seg(0, 0.2, 0, 0)]
        zigzag = [seg(i * 0.1, (i % 2) * 0.1, (i + 1) * 0.1, ((i + 1) % 2) * 0.1) for i in range(8)]
        assert arcs_from_chains(box + zigzag) == []


class TestHatchWalls:
    def test_single_face_with_diagonal_hatch(self):
        face = seg(0, 0, 0, 6)  # inner face; the outer face (x = -0.3) is only implied by the hatch
        hatch = [seg(-0.3, y, 0, y + 0.3) for y in [i * 0.1 for i in range(57)]]
        (wall,) = hatch_walls([], [face], hatch, S)
        assert wall.thickness == pytest.approx(0.3, abs=0.02)
        assert wall.start[0] == pytest.approx(-0.15, abs=0.01)

    def test_parallel_equipment_lines_are_not_hatch(self):
        face = seg(0, 0, 0, 6)
        boxes = [seg(-0.4, y, -0.4, y + 0.5) for y in range(6)] + [seg(-0.4, y, 0, y) for y in range(7)]
        assert hatch_walls([], [face], boxes, S) == []

    def test_existing_wall_faces_are_skipped(self):
        wall = WallCandidate((0.15, 0), (0.15, 6), 0.3, 1.0)
        hatch = [seg(0, y, 0.3, y + 0.3) for y in [i * 0.1 for i in range(57)]]
        assert hatch_walls([wall], [seg(0, 0, 0, 6), seg(0.3, 0, 0.3, 6)], hatch, S) == []


def test_glazing_must_stay_inside_the_gap():
    """Regression: a long thin wall alongside a short gap was mistaken for glazing and deleted."""
    a = WallCandidate((0, 0), (3, 0), 0.33, 1.0)
    b = WallCandidate((4.6, 0), (8, 0), 0.33, 1.0)
    long_thin = WallCandidate((-2, 0.02), (12, 0.02), 0.3, 1.0)  # runs far past the 1.6 m gap
    walls, windows = find_infill_windows([a, b, long_thin], [], [], S)
    assert long_thin in walls and windows == []
    glazing = WallCandidate((3.0, 0.0), (4.6, 0.0), 0.1, 1.0)
    walls, windows = find_infill_windows([a, b, glazing], [], [], S)
    assert glazing not in walls and len(windows) == 1


class TestFilledShapes:
    def test_wall_and_column(self):
        poche = Polyline(points=((0, 0), (5, 0), (5, 0.25), (0, 0.25)), closed=True, filled=True)
        column = Polyline(points=((10, 10), (10.4, 10), (10.4, 10.6), (10, 10.6)), closed=True, filled=True)
        arrow = Polyline(points=((0, 5), (0.3, 5.1), (0, 5.2)), closed=True, filled=True)
        walls, columns = filled_elements([poche, column, arrow], 1.0, S)
        (wall,) = walls
        assert wall.thickness == pytest.approx(0.25) and wall.length == pytest.approx(5)
        (col,) = columns
        assert col.position == pytest.approx((10.2, 10.3)) and (col.width, col.depth) == pytest.approx((0.6, 0.4))

    def test_dxf_hatched_walls_and_solid_column(self, tmp_path):
        doc = ezdxf.new("R2018")
        doc.units = ezdxf.units.MM
        msp = doc.modelspace()
        for x0, y0, x1, y1 in ((0, 0, 6000, 250), (0, 3750, 6000, 4000), (0, 0, 250, 4000), (5750, 0, 6000, 4000)):
            hatch = msp.add_hatch(dxfattribs={"layer": "HATCH-POCHE"})
            hatch.paths.add_polyline_path([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], is_closed=True)
        msp.add_solid([(2900, 1900), (3300, 1900), (2900, 2300), (3300, 2300)], dxfattribs={"layer": "S-COLS"})
        path = tmp_path / "poche.dxf"
        doc.saveas(path)
        result = PlanConverter().run(DxfImporter().import_file(path).drawing, Project().default_storey)
        walls = [p.element for p in result.proposals if isinstance(p.element, Wall)]
        assert len(walls) == 4 and all(w.thickness == pytest.approx(0.25) for w in walls)
        (column,) = [p.element for p in result.proposals if isinstance(p.element, Column)]
        assert column.position == pytest.approx((3.1, 2.1)) and column.width == pytest.approx(0.4)
        assert result.summary()["Space"] == 1


def test_plan_with_stair_and_flattened_double_door_end_to_end():
    """A room with a stair inside and a double door whose swings are flattened into short lines."""
    prims = []

    def wall_faces(x0, y0, x1, y1):
        prims.extend([seg(x0, y0, x1, y0, "W"), seg(x0, y1, x1, y1, "W")])

    wall_faces(0, 0, 3, 0.3)  # south wall, door gap 3.0-4.92
    wall_faces(4.92, 0, 10, 0.3)
    wall_faces(0, 7.7, 10, 8)
    prims += [seg(0, 0, 0, 8, "W"), seg(0.3, 0.3, 0.3, 7.7, "W"), seg(10, 0, 10, 8, "W"), seg(9.7, 0.3, 9.7, 7.7, "W")]
    prims += [seg(5, 2 + i * 0.27, 6.2, 2 + i * 0.27, "T") for i in range(12)]  # stair treads
    prims += TestFlattenedArcs.flattened(3.0, 0.3, 0.96, 0, 90) + TestFlattenedArcs.flattened(4.92, 0.3, 0.96, 90, 180)
    drawing = Drawing2D(unit=LengthUnit.METRE, primitives=prims, layers={n: Layer(name=n) for n in ("W", "T", "L")})
    result = PlanConverter().run(drawing, Project().default_storey)
    summary = result.summary()
    assert summary["Stair"] == 1 and summary["Door"] == 1 and summary["Wall"] == 4
    door = next(p.element for p in result.proposals if isinstance(p.element, Door))
    assert door.width == pytest.approx(1.92, abs=0.01)
    stair = next(p.element for p in result.proposals if isinstance(p.element, Stair))
    assert stair.risers == 12 and stair.width == pytest.approx(1.2)
    # The treads must not have produced thin "walls".
    assert all(p.element.thickness > 0.2 for p in result.proposals if isinstance(p.element, Wall))


def test_stairs_and_columns_export_to_ifc(tmp_path):
    """Regression: IFC export failed with "'Stair' object has no attribute 'system_id'"."""
    ifcopenshell = pytest.importorskip("ifcopenshell")
    from bimforge.adapters.ifc_exporter import IfcExporter

    p = Project()
    p.add(Stair(name="S1", start=(0, 0), end=(2.8, 0), width=1.0, risers=10, height=1.7))
    p.add(Column(name="C1", position=(5, 5), width=0.4, depth=0.4))
    path = tmp_path / "out.ifc"
    assert IfcExporter().export(p, path) == []
    model = ifcopenshell.open(str(path))
    assert [e.Name for e in model.by_type("IfcStair")] == ["S1"]
    assert [e.Name for e in model.by_type("IfcColumn")] == ["C1"]
