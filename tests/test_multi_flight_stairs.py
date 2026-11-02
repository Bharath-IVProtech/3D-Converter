"""One staircase with several flights joined by landings, not several separate stairs."""

import pytest

from bimforge.app.commands import CommandBus, ModifyElement, MoveElements, flipped_stair
from bimforge.app.events import EventBus
from bimforge.app.persistence import load_project, save_project
from bimforge.conversion import ConversionSettings, PlanConverter
from bimforge.conversion.stairs import StairCandidate, link_flights, orient_by_notes, walls_clear_of_stairs
from bimforge.conversion.walls import WallCandidate
from bimforge.coordination.quantities import take_off
from bimforge.core.model import Project, Stair, Wall
from bimforge.core.units import LengthUnit
from bimforge.drawing.primitives import Drawing2D, Layer, Segment, Text
from bimforge.geometry.builders import Tessellator
from bimforge.geometry.kernel import NumpyKernel

tess = Tessellator(NumpyKernel())


def flight(start, end, width=1.0, risers=10):
    return StairCandidate(start=start, end=end, width=width, risers=risers, confidence=0.9)


def treads(x0, x1, y0, count, going=0.25, layer="T"):
    return [Segment(start=(x0, y0 + i * going), end=(x1, y0 + i * going), layer=layer) for i in range(count)]


class TestLinking:
    def test_u_turn_stair_side_by_side_with_a_landing(self):
        """The classic dog-leg: up one flight, across the landing, back up beside it."""
        (stair,) = link_flights([flight((0, 0), (0, 2.5)), flight((1.2, 2.5), (1.2, 0))])
        assert len(stair.further) == 1
        assert stair.end[1] == stair.further[0][0][1]  # they meet at the same end: the landing
        assert stair.start[1] == stair.further[0][1][1] != stair.end[1]  # the free ends are together

    def test_up_note_decides_which_end_of_a_u_turn_has_the_landing(self):
        flights = [flight((0, 0), (0, 2.5)), flight((1.2, 2.5), (1.2, 0))]
        (stair,) = link_flights(flights, notes=[((0, -0.3), "UP")])
        assert stair.end[1] == 2.5 and stair.further[0][0][1] == 2.5  # landing away from the note
        (stair,) = link_flights(flights, notes=[((0, 2.8), "UP")])
        assert stair.end[1] == 0 and stair.further[0][0][1] == 0

    def test_straight_and_angled_landings(self):
        assert len(link_flights([flight((0, 0), (0, 2.5)), flight((0, 3.7), (0, 6.2))])) == 1
        (stair,) = link_flights([flight((0, 0), (0, 2.5)), flight((0.05, 2.9), (1.72, 4.77), width=1.2)])
        assert len(stair.further) == 1

    def test_three_flights_chain_in_climbing_order(self):
        a, b, c = flight((0, 0), (0, 2.5)), flight((1.2, 2.5), (1.2, 0)), flight((2.4, 0), (2.4, 2.5))
        (stair,) = link_flights([c, a, b])
        assert len(stair.further) == 2 and stair.risers + sum(f[3] for f in stair.further) == 30

    @pytest.mark.parametrize("other", [
        flight((14, 0), (14, 2.5)),  # far away
        flight((0, 3.0), (0, 5.5), width=2.5),  # very different width
        flight((0, 3.0), (0, 6.0)),  # different tread depth (0.30 against 0.25)
    ])
    def test_unrelated_flights_stay_separate(self, other):
        assert len(link_flights([flight((0, 0), (0, 2.5)), other])) == 2

    def test_a_wall_between_flights_keeps_them_separate(self):
        wall = WallCandidate((-3, 3), (3, 3), 0.2, 1.0)
        assert len(link_flights([flight((0, 0), (0, 2.5)), flight((0, 3.5), (0, 6))], [wall])) == 2

    def test_a_short_false_wall_on_the_landing_does_not_separate_them(self):
        edge = WallCandidate((-0.5, 3), (0.5, 3), 0.2, 1.0)
        assert len(link_flights([flight((0, 0), (0, 2.5)), flight((0, 3.5), (0, 6))], [edge])) == 1

    def test_short_walls_on_a_stair_are_removed_real_walls_kept(self):
        (stair,) = link_flights([flight((0, 0), (0, 2.5)), flight((0, 3.5), (0, 6))])
        on_landing = WallCandidate((-0.4, 3), (0.4, 3), 0.1, 1.0)
        beside = WallCandidate((1, -1), (1, 7), 0.2, 1.0)
        long_across = WallCandidate((-5, 1), (5, 1), 0.2, 1.0)
        assert walls_clear_of_stairs([on_landing, beside, long_across], [stair]) == [beside, long_across]


class TestDirection:
    def test_up_note_marks_the_bottom(self):
        (stair,) = link_flights([flight((0, 0), (0, 2.5)), flight((1.2, 2.5), (1.2, 0))])
        far_end = stair.further[0][1]
        orient_by_notes([stair], [(far_end, "UP")])
        assert stair.direction_known and stair.start == far_end

    def test_dn_note_marks_the_top(self):
        s = flight((0, 0), (0, 2.5))
        orient_by_notes([s], [((0, 2.6), "DN")])
        assert s.direction_known and s.start == (0, 0)

    def test_no_nearby_note_leaves_direction_unknown(self):
        s = flight((0, 0), (0, 2.5))
        orient_by_notes([s], [((9, 9), "UP"), ((0, 0.1), "UPVC")])
        assert not s.direction_known


def two_flights(**changes) -> Stair:
    base = dict(start=(0, 0), end=(0, 2.5), width=1.0, risers=10, height=3.4,
                further_flights=[(1.2, 2.5, 1.2, 0, 1.0, 10)])
    return Stair(**{**base, **changes})


class TestGeometry:
    def test_steps_climb_continuously_to_the_top(self):
        p = Project()
        stair = two_flights()
        p.add(stair)
        mesh = tess.mesh_for(stair, p)
        assert stair.total_risers == 20 and stair.riser_height == pytest.approx(0.17)
        assert mesh.bounds()[1][2] == pytest.approx(3.4)
        # The second flight's first step (beside the landing) is above the first flight's top, not at the floor.
        first_step = mesh.vertices[(mesh.vertices[:, 0] > 1.0) & (mesh.vertices[:, 1] > 2.26)]
        assert first_step[:, 2].max() == pytest.approx(1.7 + 0.17)

    def test_straight_stair_volume_is_steps_plus_landing(self):
        p = Project()
        stair = two_flights(further_flights=[(0, 3.5, 0, 6, 1.0, 10)])  # 1 m landing, 1 m wide
        p.add(stair)
        steps = 0.25 * 1.0 * 0.17 * sum(range(1, 21))
        landing = 1.0 * 1.0 * 1.7  # solid up to the height of the first flight's top
        assert tess.mesh_for(stair, p).volume() == pytest.approx(steps + landing)

    def test_single_flight_stair_is_unchanged(self):
        p = Project()
        stair = Stair(start=(0, 0), end=(2.8, 0), width=1.0, risers=10, height=1.7)
        p.add(stair)
        assert tess.mesh_for(stair, p).volume() == pytest.approx(0.28 * 1.0 * 0.17 * sum(range(1, 11)))


class TestEditing:
    def test_flip_swaps_bottom_and_top_and_undoes(self):
        p = Project()
        stair = two_flights()
        bus = CommandBus(p, EventBus())
        p.add(stair)
        original = (stair.start, stair.end, list(stair.further_flights))
        bus.execute(ModifyElement(stair.id, flipped_stair(stair)))
        assert (stair.start, stair.end) == ((1.2, 0), (1.2, 2.5))
        assert stair.further_flights == [(0, 2.5, 0, 0, 1.0, 10)]
        bus.undo()
        assert (stair.start, stair.end, stair.further_flights) == original

    def test_flipping_twice_restores(self):
        stair = two_flights()
        original = stair.flights
        for _ in range(2):
            for name, value in flipped_stair(stair).items():
                setattr(stair, name, value)
        assert stair.flights == original

    def test_move_carries_every_flight(self):
        p = Project()
        stair = two_flights()
        p.add(stair)
        CommandBus(p, EventBus()).execute(MoveElements([stair.id], 2, 1))
        assert stair.start == (2, 1) and stair.further_flights == [(3.2, 3.5, 3.2, 1, 1.0, 10)]

    def test_saved_counted_and_exported_as_one_stair(self, tmp_path):
        p = Project()
        stair = two_flights(name="S1")
        p.add(stair)
        save_project(p, tmp_path / "p.bimx")
        assert load_project(tmp_path / "p.bimx").project.get(stair.id) == stair
        rows = [r for r in take_off(p) if r.category == "Stairs"]
        assert [(r.item, r.count) for r in rows] == [("Stair 1000 mm wide, 2 flights, 20 risers", 1)]
        ifcopenshell = pytest.importorskip("ifcopenshell")
        from bimforge.adapters.ifc_exporter import IfcExporter

        IfcExporter().export(p, tmp_path / "s.ifc")
        assert [e.Name for e in ifcopenshell.open(str(tmp_path / "s.ifc")).by_type("IfcStair")] == ["S1"]


def test_plan_with_a_u_turn_stair_gives_one_stair_following_the_up_note():
    prims = [Segment(start=a, end=b, layer="W") for a, b in (
        ((0, 0), (8, 0)), ((0.3, 0.3), (7.7, 0.3)), ((0, 8), (8, 8)), ((0.3, 7.7), (7.7, 7.7)),
        ((0, 0), (0, 8)), ((0.3, 0.3), (0.3, 7.7)), ((8, 0), (8, 8)), ((7.7, 0.3), (7.7, 7.7)))]
    prims += treads(2, 3, 2, 10) + treads(3.2, 4.2, 2, 10)
    prims.append(Text(position=(2.5, 1.7), text="UP", height=0.15, layer="Text"))
    drawing = Drawing2D(unit=LengthUnit.METRE, primitives=prims,
                        layers={n: Layer(name=n) for n in ("W", "T", "Text")})
    result = PlanConverter(ConversionSettings(wall_layers=("W",))).run(drawing, Project().default_storey)
    (proposal,) = [p for p in result.proposals if isinstance(p.element, Stair)]
    stair = proposal.element
    assert len(stair.flights) == 2 and stair.total_risers == 20
    assert stair.start[1] < stair.end[1] and stair.start[0] == pytest.approx(2.5)  # starts at the UP note
    assert "2 flights with landing" in proposal.note and "check direction" not in proposal.note
    assert len([p for p in result.proposals if isinstance(p.element, Wall)]) == 4
