"""HVAC duct recognition from outlines and size labels."""

import math

import pytest

from bimforge.app.commands import CommandBus, commit_conversion
from bimforge.app.events import EventBus
from bimforge.app.persistence import load_project, save_project
from bimforge.conversion import ConversionSettings, PlanConverter
from bimforge.conversion.ducts import detect_ducts, parse_labels
from bimforge.coordination.clash import DEFAULT_TESTS, ClashDetector
from bimforge.coordination.quantities import take_off
from bimforge.core.model import DuctSegment, Project, Slab, SystemType, Wall
from bimforge.core.units import LengthUnit
from bimforge.drawing.primitives import Drawing2D, Layer, Segment, Text
from bimforge.geometry.builders import Tessellator
from bimforge.geometry.kernel import NumpyKernel

S = ConversionSettings()
OUTLINE = "1.90 pt cyan"


def seg(x0, y0, x1, y1, layer=OUTLINE):
    return Segment(start=(x0, y0), end=(x1, y1), layer=layer)


def words(x, y, *line_words):
    """Text as a PDF gives it: one Text per word."""
    return [Text(position=(x + i * 0.5, y), text=w, height=0.15) for i, w in enumerate(line_words)]


class TestLabels:
    def test_separate_words_with_bottom_of_duct(self):
        texts = words(10, 10, "800x300", "EA") + words(10, 9.7, "B.O.D", "4150")
        (label,) = parse_labels(texts, 1.0)
        assert (label.width, label.depth, label.kind, label.bottom) == (0.8, 0.3, "EA", 4.15)

    def test_single_string_label(self):
        (label,) = parse_labels([Text(position=(0, 0), text="450x300 SA B.O.D 4263", height=0.15)], 1.0)
        assert (label.width, label.kind, label.bottom) == (0.45, "SA", 4.263)

    def test_drawing_units_are_scaled(self):
        (label,) = parse_labels([Text(position=(10000, 5000), text="600x300 FA", height=150)], 0.001)
        assert label.position == pytest.approx((10, 5)) and label.width == 0.6

    @pytest.mark.parametrize("text", [
        "Wall cutout:425x425",  # an opening, not a duct
        "1300x700 Cutout For Ventilation",  # no air kind
        "1X355X355H.X2400CFM",  # louvre schedule
        "3x800x200x800CFM EA",  # grille schedule
        "1X2200X500H. EA",
    ])
    def test_other_dimensions_are_not_duct_labels(self, text):
        assert parse_labels([Text(position=(0, 0), text=text, height=0.15)], 1.0) == []


def plan() -> tuple[list, list]:
    lines = [
        seg(0, 0, 6, 0), seg(0, 0.45, 6, 0.45),  # main run, 450 wide
        seg(6.45, 0.45, 6.45, 4), seg(6.9, 0.45, 6.9, 4),  # branch, also 450 wide
    ]
    texts = words(2, 1, "450x300", "EA") + words(2, 0.7, "B.O.D", "4150")
    return lines, texts


class TestDetection:
    def test_outline_pair_becomes_a_sized_duct_at_its_level(self):
        lines, texts = plan()
        ducts = detect_ducts(lines, parse_labels(texts, 1.0), S)
        assert len(ducts) == 2
        assert all((d.width, d.depth, d.bottom, d.kind) == (0.45, 0.3, 4.15, "EA") for d in ducts)
        run = max(ducts, key=lambda d: abs(d.end[0] - d.start[0]))
        assert run.start[1] == pytest.approx(0.225)

    def test_fan_gap_is_bridged_but_not_a_long_gap(self):
        lines = [seg(0, 0, 2, 0), seg(0, 0.45, 2, 0.45), seg(2.9, 0, 5, 0), seg(2.9, 0.45, 5, 0.45)]
        labels = parse_labels(words(1, 1, "450x300", "EA"), 1.0)
        assert len(detect_ducts(lines, labels, S)) == 1
        lines = [seg(0, 0, 2, 0), seg(0, 0.45, 2, 0.45), seg(5, 0, 7, 0), seg(5, 0.45, 7, 0.45)]
        assert len(detect_ducts(lines, labels, S)) == 2

    def test_centre_line_does_not_steal_the_outline(self):
        lines = [seg(0, 0, 6, 0), seg(0, 0.45, 6, 0.45)]
        lines += [seg(i, 0.225, i + 0.4, 0.225) for i in range(6)]  # dashed centre line, same style
        (duct,) = detect_ducts(lines, parse_labels(words(1, 1, "450x300", "EA"), 1.0), S)
        assert math.dist(duct.start, duct.end) == pytest.approx(6)

    def test_heaviest_style_of_a_colour_is_the_outline(self):
        outline = [seg(0, 0, 6, 0), seg(0, 0.45, 6, 0.45)]
        keyplan = [seg(50, y, 60, y, "0.50 pt cyan") for y in (0, 0.45)]  # thin but long
        ducts = detect_ducts(outline + keyplan, parse_labels(words(1, 1, "450x300", "EA"), 1.0), S)
        assert all(d.start[0] < 10 for d in ducts) and len(ducts) == 1

    def test_grey_and_text_styles_are_ignored(self):
        lines = [seg(0, 0, 6, 0, "0.50 pt grey 54%"), seg(0, 0.45, 6, 0.45, "0.50 pt grey 54%"),
                 seg(0, 2, 6, 2, "0.25 pt blue"), seg(0, 2.45, 6, 2.45, "0.25 pt blue")]
        assert detect_ducts(lines, parse_labels(words(1, 1, "450x300", "EA"), 1.0), S) == []

    def test_no_labels_needs_a_duct_layer(self):
        lines = [seg(0, 0, 6, 0, "M-HVAC-DUCT"), seg(0, 0.5, 6, 0.5, "M-HVAC-DUCT")]
        assert detect_ducts(lines, [], S) == []
        (duct,) = detect_ducts(lines, [], S, duct_layers=["M-HVAC-DUCT"])
        assert duct.width == pytest.approx(0.5) and not duct.labelled and duct.confidence < 0.85

    def test_corner_runs_meet(self):
        lines = [seg(0, 0, 5, 0), seg(0, 0.4, 5, 0.4), seg(5.3, 0.8, 5.3, 5), seg(5.7, 0.8, 5.7, 5)]
        labels = parse_labels(words(1, 1, "400x300", "EA"), 1.0)
        a, b = detect_ducts(lines, labels, S)
        corner = (5.5, 0.2)
        assert corner == pytest.approx(a.end if a.end[0] > a.start[0] else a.start)
        ends = [b.start, b.end]
        assert any(p == pytest.approx(corner) for p in ends)


def drawing_with_walls_and_ducts() -> Drawing2D:
    walls = "0.50 pt grey 54%"
    prims = []
    for x0, y0, x1, y1 in ((0, 0, 10, 0), (0, 0.3, 10, 0.3), (0, 8, 10, 8), (0, 7.7, 10, 7.7),
                           (0, 0, 0, 8), (0.3, 0.3, 0.3, 7.7), (10, 0, 10, 8), (9.7, 0.3, 9.7, 7.7)):
        prims.append(seg(x0, y0, x1, y1, walls))
    prims += [seg(1, 4, 9, 4), seg(1, 4.45, 9, 4.45)]
    prims += words(3, 5, "450x300", "SA") + words(3, 4.7, "B.O.D", "2700")
    prims += [seg(40, 4, 48, 4), seg(40, 4.45, 48, 4.45)]  # legend or key plan, off the building
    return Drawing2D(unit=LengthUnit.METRE, primitives=prims,
                     layers={n: Layer(name=n) for n in (walls, OUTLINE, "Text")})


class TestPipeline:
    def setup_method(self):
        self.project = Project()
        self.result = PlanConverter(ConversionSettings(wall_layers=("0.50 pt grey 54%",))).run(
            drawing_with_walls_and_ducts(), self.project.default_storey)
        self.ducts = [p.element for p in self.result.proposals if isinstance(p.element, DuctSegment)]

    def test_duct_is_proposed_inside_the_building_only(self):
        (duct,) = self.ducts
        assert duct.path[0][2] == pytest.approx(2.7 + 0.15)  # centre = bottom + half depth
        assert (duct.width, duct.depth) == (0.45, 0.3)
        assert "bottom 2700 mm" in next(p.note for p in self.result.proposals if p.element is duct)

    def test_walls_are_not_affected(self):
        walls = [p.element for p in self.result.proposals if isinstance(p.element, Wall)]
        assert len(walls) == 4 and all(w.thickness == pytest.approx(0.3) for w in walls)

    def test_commit_adds_system_and_undoes_in_one_step(self):
        bus = CommandBus(self.project, EventBus())
        bus.execute(commit_conversion(self.result, self.project))
        (system,) = self.project.systems.values()
        assert system.system_type is SystemType.SUPPLY_AIR and self.ducts[0].system_id == system.id
        bus.undo()
        assert len(self.project) == 0 and not self.project.systems

    def test_second_import_reuses_the_system(self):
        bus = CommandBus(self.project, EventBus())
        bus.execute(commit_conversion(self.result, self.project))
        again = PlanConverter(ConversionSettings(wall_layers=("0.50 pt grey 54%",))).run(
            drawing_with_walls_and_ducts(), self.project.default_storey)
        bus.execute(commit_conversion(again, self.project))
        assert len(self.project.systems) == 1
        assert len({d.system_id for d in self.project.elements_of(DuctSegment)}) == 1

    def test_rejected_duct_does_not_add_its_system(self):
        for p in self.result.proposals:
            if isinstance(p.element, DuctSegment):
                p.accepted = False
        CommandBus(self.project, EventBus()).execute(commit_conversion(self.result, self.project))
        assert not self.project.systems


def project_with_duct() -> tuple[Project, DuctSegment]:
    p = Project()
    duct = DuctSegment(name="SA-1", width=0.8, depth=0.3, path=[(0, 0, 4.3), (5, 0, 4.3)],
                       storey_id=p.default_storey.id)
    p.add(duct)
    return p, duct


def test_duct_geometry_quantities_and_save(tmp_path):
    p, duct = project_with_duct()
    mesh = Tessellator(NumpyKernel()).mesh_for(duct, p)
    assert mesh.volume() == pytest.approx(0.8 * 0.3 * 5)
    lo, hi = mesh.bounds()
    assert (lo[2], hi[2]) == pytest.approx((4.15, 4.45))
    rows = {r.item: r for r in take_off(p)}
    assert rows["Duct 800 x 300 mm, unassigned - length"].quantity == 5
    assert rows["Duct 800 x 300 mm, unassigned - sheet area"].quantity == pytest.approx(11)
    save_project(p, tmp_path / "p.bimx")
    assert load_project(tmp_path / "p.bimx").project.get(duct.id) == duct


def test_duct_exports_to_ifc(tmp_path):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    from bimforge.adapters.ifc_exporter import IfcExporter

    p, _ = project_with_duct()
    assert IfcExporter().export(p, tmp_path / "d.ifc") == []
    assert [e.Name for e in ifcopenshell.open(str(tmp_path / "d.ifc")).by_type("IfcDuctSegment")] == ["SA-1"]


def test_duct_through_a_slab_is_a_clash():
    pytest.importorskip("fcl")
    p = Project()
    slab = Slab(boundary=[(0, 0), (6, 0), (6, 6), (0, 6)], thickness=0.2)
    duct = DuctSegment(path=[(3, 3, -1), (3, 3, 1)], width=0.4, depth=0.3)
    p.add(slab)
    p.add(duct)
    test = next(t for t in DEFAULT_TESTS if t.name == "Mechanical vs Structure")
    (clash,) = ClashDetector(p, Tessellator(NumpyKernel())).run(test)
    assert clash.kind == "hard"
