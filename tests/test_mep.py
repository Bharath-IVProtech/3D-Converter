import math

import pytest

from bimforge.app.commands import AddElements, AddSystem, CommandBus, ModifyElement, ModifySystem, RemoveSystem
from bimforge.app.events import EventBus
from bimforge.core.model import (
    CableCarrierSegment,
    DistributionSystem,
    Fixture,
    PipeSegment,
    Project,
    SystemType,
)
from bimforge.core.validation import ValidationError
from bimforge.disciplines import electrical, plumbing
from bimforge.disciplines.catalog import FAMILIES, connection_point
from bimforge.geometry.builders import Tessellator
from bimforge.geometry.kernel import NumpyKernel
from bimforge.geometry.sweep import placed_box, rect_run, tube

tess = Tessellator(NumpyKernel())


class TestSweeps:
    def test_tube_volume_converges_to_cylinder(self):
        mesh = tube([(0, 0, 0), (2, 0, 0)], 0.05, sides=128)
        assert mesh.volume() == pytest.approx(math.pi * 0.05**2 * 2, rel=1e-3)

    @pytest.mark.parametrize("path", [[(0, 0, 0), (0, 0, 3)], [(0, 0, 0), (1, 1, 1)], [(0, 0, 0), (-2, 0, 0)]])
    def test_tube_orientation_any_direction(self, path):
        assert tube(path, 0.05).volume() > 0

    def test_rect_run_multi_leg(self):
        assert rect_run([(0, 0, 1), (0, 3, 1), (0, 3, 4)], 0.3, 0.1).volume() == pytest.approx(0.18)

    def test_placed_box_rotation_keeps_volume_and_centre(self):
        mesh = placed_box((1, 2, 0.5), (0.4, 0.7, 0.8), 37)
        assert mesh.volume() == pytest.approx(0.224)
        assert mesh.vertices[:, :2].mean(axis=0) == pytest.approx((1, 2))

    def test_tessellator_lifts_mep_by_storey_elevation(self):
        p = Project()
        p.default_storey.elevation = 3.0
        pipe = PipeSegment(path=[(0, 0, 0.5), (2, 0, 0.5)], diameter=0.1, storey_id=p.default_storey.id)
        tray = CableCarrierSegment(path=[(0, 1, 2.5), (2, 1, 2.5)], width=0.3, depth=0.1, storey_id=p.default_storey.id)
        wc = Fixture(family="wc", position=(1, 1, 0), storey_id=p.default_storey.id)
        for e in (pipe, tray, wc):
            p.add(e)
        assert tess.mesh_for(pipe, p).bounds()[0][2] == pytest.approx(3.45)
        assert tess.mesh_for(tray, p).volume() == pytest.approx(0.06)
        assert tess.mesh_for(wc, p).bounds()[1][2] == pytest.approx(3.8)


def test_connection_point_rotates_with_fixture():
    wc = Fixture(family="wc", position=(5, 5, 0), rotation=90)
    x, y, z = connection_point(wc, storey_elevation=3.0)
    assert (x, y, z) == pytest.approx((4.9, 5.0, 3.0))  # local (0, 0.1) rotated 90° -> (-0.1, 0)


class TestPlumbingTables:
    @pytest.mark.parametrize("dfu, min_dn, expected", [(1, 0, 32), (3, 0, 40), (7, 0, 65), (3, 80, 80), (21, 0, 100)])
    def test_drain_size(self, dfu, min_dn, expected):
        assert plumbing.drain_size_for(dfu, min_dn) == expected

    def test_drain_size_overflow(self):
        with pytest.raises(ValueError):
            plumbing.drain_size_for(1e6)

    @pytest.mark.parametrize("dn, slope", [(50, 0.02), (100, 0.01), (200, 0.005)])
    def test_min_slope(self, dn, slope):
        assert plumbing.min_drain_slope(dn) == slope

    def test_supply_flow_monotonic_and_sizes_grow(self):
        flows = [plumbing.supply_flow_lps(w) for w in (0.5, 1, 5, 10, 50, 100, 200)]
        assert flows == sorted(flows) and flows[0] > 0
        assert plumbing.supply_size_for(1)[0] == 15
        assert plumbing.supply_size_for(60)[0] > plumbing.supply_size_for(5)[0]
        od, velocity = plumbing.supply_size_for(30)
        assert velocity <= plumbing.MAX_SUPPLY_VELOCITY

    def test_apply_slope_and_leg_slopes(self):
        graded = plumbing.apply_slope([(0, 0, 0.5), (4, 0, 0.5), (4, 0, -1.0), (4, 2, -1.0)], 0.02)
        assert [p[2] for p in graded] == pytest.approx([0.5, 0.42, -1.08, -1.12])
        assert plumbing.leg_slopes(graded) == [pytest.approx(0.02), None, pytest.approx(0.02)]


def _bathroom() -> tuple[Project, DistributionSystem, DistributionSystem]:
    """WC and basin draining into a branch, which joins a main; plus a cold-water supply run."""
    p = Project()
    s = p.default_storey.id
    drain = DistributionSystem(name="SAN-1", system_type=SystemType.SANITARY)
    cold = DistributionSystem(name="CW-1", system_type=SystemType.COLD_WATER)
    p.systems[drain.id] = drain
    p.systems[cold.id] = cold
    wc = Fixture(name="WC", family="wc", position=(1, 0.9, 0), system_id=drain.id, storey_id=s)  # port (1, 1, 0)
    basin = Fixture(name="Basin", family="basin", position=(2, 1.15, 0), system_id=drain.id, storey_id=s)  # (2, 1, 0)
    for e in (wc, basin):
        p.add(e)
    branch_wc = PipeSegment(name="B-WC", path=[(1, 1, 0), (1, 0, -0.01)], system_id=drain.id, storey_id=s)
    branch_basin = PipeSegment(name="B-Basin", path=[(2, 1, 0), (2, 0, -0.02)], system_id=drain.id, storey_id=s)
    main = PipeSegment(name="Main", path=[(0, 0, 0), (5, 0, -0.05)], system_id=drain.id, storey_id=s)
    supply = PipeSegment(name="CW", path=[(0, 2, 0.6), (2, 2, 0.6), (2, 1.15, 0.6)], system_id=cold.id, storey_id=s)
    basin_cw = Fixture(name="Basin CW", family="basin", position=(2, 1.15, 0.6), system_id=cold.id, storey_id=s)
    for e in (branch_wc, branch_basin, main, supply, basin_cw):
        p.add(e)
    return p, drain, cold


class TestNetworkSizing:
    def test_drainage_accumulates_downstream(self):
        p, drain, _ = _bathroom()
        sizes = {p.get(r.pipe_id).name: r for r in plumbing.size_network(p, drain.id)}
        assert sizes["B-WC"].fixture_units == 3 and sizes["B-WC"].size_mm == 80  # WC min trap
        assert sizes["B-Basin"].fixture_units == 1 and sizes["B-Basin"].size_mm == 40  # basin min DN40
        assert sizes["Main"].fixture_units == 4 and sizes["Main"].size_mm == 80  # not smaller than the WC branch

    def test_supply_sizes_by_velocity(self):
        p, _, cold = _bathroom()
        (result,) = plumbing.size_network(p, cold.id)
        assert result.fixture_units == pytest.approx(0.7) and result.size_mm == 15

    def test_checks_flag_flat_drain_and_unassigned(self):
        p, drain, _ = _bathroom()
        titles = [i.title for i in plumbing.check(p)]
        assert any("Main" in t and "slope" in t for t in titles)  # default 25 mm pipe needs 2 %; the main falls 1 %
        flat = PipeSegment(name="Flat", path=[(0, 3, 0), (3, 3, 0)], diameter=0.05, system_id=drain.id)
        p.add(flat)
        titles = [i.title for i in plumbing.check(p)]
        assert any(t.startswith("Flat: slope 0.0%") for t in titles)
        p.add(PipeSegment(name="Loose", path=[(0, 4, 0), (1, 4, 0)]))
        assert any("Loose is not assigned" in i.title for i in plumbing.check(p))


class TestElectrical:
    @pytest.mark.parametrize("current, breaker", [(0.5, 6), (6, 6), (6.1, 10), (19, 20)])
    def test_breaker(self, current, breaker):
        assert electrical.breaker_for(current) == breaker

    @pytest.mark.parametrize("breaker, cable", [(6, 1.5), (16, 1.5), (20, 2.5), (25, 2.5), (32, 4.0)])
    def test_cable(self, breaker, cable):
        assert electrical.cable_for(breaker) == cable

    def _project(self):
        p = Project()
        board = Fixture(name="DB-1", family="db", position=(0, 0, 1.4))
        p.add(board)
        ring = DistributionSystem(name="C1", system_type=SystemType.POWER, properties={"panel_id": board.id})
        lights = DistributionSystem(name="C2", system_type=SystemType.LIGHTING, properties={"panel_id": board.id})
        p.systems.update({ring.id: ring, lights.id: lights})
        for i in range(10):
            p.add(Fixture(name=f"S{i}", family="socket", position=(i, 5, 0.3), system_id=ring.id))
        for i in range(4):
            p.add(Fixture(name=f"L{i}", family="light", position=(i * 2, 2, 2.62), system_id=lights.id))
        return p, board, ring, lights

    def test_panel_schedule(self):
        p, board, ring, lights = self._project()
        rows = {r.name: r for r in electrical.panel_schedule(p, board.id)}
        power = rows["C1"]
        assert power.points == 10 and power.load_va == 2000
        assert power.design_current == pytest.approx(2000 / 230)
        assert power.breaker_a == 10 and power.cable_mm2 == 1.5
        assert power.length_m == pytest.approx((9 + 5 + 1.1) * 1.2, abs=0.05)
        assert power.ok
        assert rows["C2"].load_va == 80 and rows["C2"].breaker_a == 6

    def test_overload_and_voltage_drop_flagged(self):
        p, board, ring, _ = self._project()
        ring.properties.update(breaker_a=6, length_m=200)
        row = electrical.evaluate(p, ring)
        assert not row.ok and "exceeds breaker" in row.notes and "voltage drop" in row.notes
        assert any("C1" in i.title for i in electrical.check(p))

    def test_unassigned_load_flagged(self):
        p, *_ = self._project()
        p.add(Fixture(name="Loose socket", family="socket", position=(0, 0, 0.3)))
        assert any("Loose socket is not on any circuit" in i.title for i in electrical.check(p))

    def test_sld_svg(self):
        p, board, *_ = self._project()
        svg = electrical.single_line_diagram_svg(p, board.id)
        assert svg.startswith("<svg") and "C1" in svg and "C2" in svg and "10 A" in svg


class TestSystemCommands:
    def test_add_modify_remove_with_undo(self):
        p = Project()
        bus = CommandBus(p, EventBus())
        system = DistributionSystem(name="CW", system_type=SystemType.COLD_WATER)
        bus.execute(AddSystem(system))
        pipe = PipeSegment(path=[(0, 0, 0), (1, 0, 0)], system_id=system.id)
        bus.execute(AddElements([pipe]))
        bus.execute(ModifySystem(system.id, {"name": "CW-main", "properties": {"pressure_bar": 3}}))
        assert system.name == "CW-main" and system.properties == {"pressure_bar": 3}
        bus.execute(RemoveSystem(system.id))
        assert pipe.system_id is None and system.id not in p.systems
        bus.undo()
        assert pipe.system_id == system.id and p.systems[system.id] is system
        bus.undo()
        assert system.name == "CW"

    def test_unknown_system_rejected(self):
        p = Project()
        bus = CommandBus(p, EventBus())
        pipe = PipeSegment(path=[(0, 0, 0), (1, 0, 0)])
        bus.execute(AddElements([pipe]))
        with pytest.raises(ValidationError, match="unknown system"):
            bus.execute(ModifyElement(pipe.id, {"system_id": "nope"}))


def test_every_family_is_complete():
    for family in FAMILIES.values():
        assert all(v > 0 for v in family.size), family.key
        assert family.ifc_class.startswith("Ifc")
