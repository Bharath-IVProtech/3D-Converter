import csv

import pytest

from bimforge.app.commands import CommandBus, SetIssues
from bimforge.app.events import EventBus
from bimforge.app.persistence import load_project, save_project
from bimforge.core.model import (
    CableCarrierSegment,
    DistributionSystem,
    Door,
    Fixture,
    Issue,
    IssueStatus,
    PipeSegment,
    Project,
    Slab,
    SystemType,
    Wall,
)
from bimforge.coordination.checks import run_checks
from bimforge.coordination.clash import (
    ClashDetector,
    ClashTest,
    merge_issues,
    point_inside,
    results_to_issues,
)
from bimforge.coordination.quantities import export_csv, export_xlsx, take_off
from bimforge.geometry.builders import Tessellator
from bimforge.geometry.kernel import NumpyKernel
from bimforge.geometry.sweep import placed_box

pytest.importorskip("fcl")
tess = Tessellator(NumpyKernel())
SLAB = [(0, 0), (6, 0), (6, 6), (0, 6)]


def project_with(*elements) -> Project:
    p = Project()
    for e in elements:
        p.add(e)
    return p


def run(project, test):
    return ClashDetector(project, tess).run(test)


PvS = ClashTest("P vs S", ("plumbing",), ("structure",))
PvE = ClashTest("P vs E", ("plumbing",), ("electrical",), clearance=0.1)


class TestClash:
    def test_riser_through_slab_is_hard_clash(self):
        slab = Slab(boundary=SLAB, thickness=0.2)  # z -0.2..0
        riser = PipeSegment(path=[(3, 3, -1), (3, 3, 1)], diameter=0.1)
        (r,) = run(project_with(slab, riser), PvS)
        assert r.kind == "hard" and {r.a_id, r.b_id} == {slab.id, riser.id}
        assert r.point[2] == pytest.approx(-0.1, abs=0.15)

    def test_pipe_fully_inside_slab_found_by_containment(self):
        slab = Slab(boundary=SLAB, thickness=0.4)  # z -0.4..0
        buried = PipeSegment(path=[(1, 3, -0.2), (4, 3, -0.2)], diameter=0.05)
        (r,) = run(project_with(slab, buried), PvS)
        assert r.kind == "hard"

    def test_clear_pipe_has_no_clash(self):
        slab = Slab(boundary=SLAB, thickness=0.2)
        pipe = PipeSegment(path=[(1, 3, 0.5), (4, 3, 0.5)], diameter=0.05)
        assert run(project_with(slab, pipe), PvS) == []

    def test_clearance_clash_reports_gap(self):
        pipe = PipeSegment(path=[(0, 0, 2.5), (5, 0, 2.5)], diameter=0.05)  # top at 2.525
        tray = CableCarrierSegment(path=[(2, -1, 2.6), (2, 1, 2.6)], width=0.3, depth=0.05)  # bottom at 2.575
        (r,) = run(project_with(pipe, tray), PvE)
        assert r.kind == "clearance" and r.distance == pytest.approx(0.05, abs=0.002)
        far = CableCarrierSegment(path=[(2, -1, 3.0), (2, 1, 3.0)], width=0.3, depth=0.05)
        assert run(project_with(pipe, far), PvE) == []

    def test_fixture_on_slab_not_reported(self):
        slab = Slab(boundary=SLAB, thickness=0.2)
        wc = Fixture(family="wc", position=(3, 3, 0))
        test = ClashTest("P vs S", ("plumbing",), ("structure",))
        assert run(project_with(slab, wc), test) == []

    def test_each_pair_reported_once_in_symmetric_tests(self):
        a = PipeSegment(path=[(0, 0, 0), (2, 0, 0)], diameter=0.1)
        b = CableCarrierSegment(path=[(1, -1, 0), (1, 1, 0)], width=0.2, depth=0.1)
        test = ClashTest("MEP", ("plumbing", "electrical"), ("plumbing", "electrical"))
        assert len(run(project_with(a, b), test)) == 1

    def test_point_inside(self):
        import numpy as np

        box = placed_box((0, 0, 0), (1, 1, 1), 0)
        assert point_inside(np.array([0.1, 0.2, 0.5]), box)
        assert not point_inside(np.array([2.0, 0.0, 0.5]), box)


class TestIssueLifecycle:
    def _issue(self, ids=("a", "b"), test="T", kind="hard"):
        return Issue(title="x", kind=kind, element_ids=ids, test=test)

    def test_new_found_again_resolved_reopened(self):
        first = merge_issues({}, [self._issue()], ["T"])
        (issue,) = first.values()
        assert issue.status is IssueStatus.NEW
        issue.comment = "needs sleeve"
        second = merge_issues(first, [self._issue(ids=("b", "a"))], ["T"])  # same pair, other order
        assert list(second) == [issue.id] and second[issue.id].status is IssueStatus.ACTIVE
        assert second[issue.id].comment == "needs sleeve"
        third = merge_issues(second, [], ["T"])
        assert third[issue.id].status is IssueStatus.RESOLVED
        fourth = merge_issues(third, [self._issue()], ["T"])
        assert fourth[issue.id].status is IssueStatus.ACTIVE
        assert first[issue.id].status is IssueStatus.NEW  # inputs untouched (undo safety)

    def test_approved_and_other_tests_untouched(self):
        existing = merge_issues({}, [self._issue(), self._issue(ids=("c", "d"), test="Other")], ["T", "Other"])
        approved = next(i for i in existing.values() if i.test == "T")
        approved.status = IssueStatus.APPROVED
        after = merge_issues(existing, [], ["T"])
        assert after[approved.id].status is IssueStatus.APPROVED
        other = next(i for i in after.values() if i.test == "Other")
        assert other.status is IssueStatus.NEW

    def test_set_issues_undo(self):
        p = Project()
        bus = CommandBus(p, EventBus())
        issues = merge_issues({}, [self._issue()], ["T"])
        bus.execute(SetIssues(issues))
        assert len(p.issues) == 1
        bus.undo()
        assert p.issues == {}

    def test_results_to_issues_titles(self):
        pipe = PipeSegment(name="CW-01", path=[(0, 0, 2.5), (5, 0, 2.5)], diameter=0.05)
        tray = CableCarrierSegment(name="CT-01", path=[(2, -1, 2.6), (2, 1, 2.6)], width=0.3, depth=0.05)
        p = project_with(pipe, tray)
        (issue,) = results_to_issues(p, run(p, PvE))
        assert issue.title == "CW-01 × CT-01 clearance (50 mm)" and issue.kind == "clearance"


def test_run_checks_combines_disciplines():
    p = Project()
    p.add(PipeSegment(name="Loose", path=[(0, 0, 0), (1, 0, 0)]))
    p.add(Fixture(name="S1", family="socket", position=(0, 0, 0.3)))
    tests = {i.test for i in run_checks(p)}
    assert tests == {"Plumbing rules", "Electrical rules"}


class TestQuantities:
    def _project(self):
        p = Project()
        wall = Wall(start=(0, 0), end=(5, 0), thickness=0.2, height=3)
        p.add(wall)
        p.add(Door(host_id=wall.id, offset=2, width=1.0, height=2.0))
        p.add(Slab(boundary=[(0, 0), (5, 0), (5, 4), (0, 4)], thickness=0.25))
        cw = DistributionSystem(name="CW", system_type=SystemType.COLD_WATER)
        p.systems[cw.id] = cw
        p.add(PipeSegment(path=[(0, 0, 0), (3, 0, 0), (3, 4, 0)], diameter=0.022, system_id=cw.id))
        p.add(PipeSegment(path=[(0, 1, 0), (2, 1, 0)], diameter=0.022, system_id=cw.id))
        p.add(CableCarrierSegment(path=[(0, 0, 2.7), (5, 0, 2.7)], width=0.3, depth=0.1))
        p.add(Fixture(family="wc", position=(1, 1, 0)))
        p.add(Fixture(family="wc", position=(2, 1, 0)))
        return p

    def test_take_off(self):
        rows = {(r.category, r.item): r for r in take_off(self._project())}
        assert rows[("Walls", "Wall 200 mm - length")].quantity == 5
        assert rows[("Walls", "Wall 200 mm - net area")].quantity == pytest.approx(15 - 2)
        assert rows[("Walls", "Wall 200 mm - volume")].quantity == pytest.approx(13 * 0.2)
        assert rows[("Doors", "1000 × 2000 mm")].count == 1
        assert rows[("Slabs", "Slab 250 mm - volume")].quantity == pytest.approx(5)
        pipes = rows[("Pipes", "cold water Ø22 mm")]
        assert (pipes.quantity, pipes.count) == (pytest.approx(9), 2)
        assert rows[("Electrical containment", "Cable tray 300 × 100 mm")].quantity == 5
        assert rows[("Plumbing fixtures", "Water closet (cistern)")].count == 2

    def test_exports(self, tmp_path):
        rows = take_off(self._project())
        export_csv(rows, tmp_path / "boq.csv")
        with open(tmp_path / "boq.csv", encoding="utf-8-sig") as fh:
            table = list(csv.reader(fh))
        assert table[0] == ["Category", "Item", "Level", "Unit", "Quantity", "Count"]
        assert len(table) == len(rows) + 1
        pytest.importorskip("openpyxl")
        from openpyxl import load_workbook

        export_xlsx(rows, tmp_path / "boq.xlsx")
        ws = load_workbook(tmp_path / "boq.xlsx").active
        assert ws["A2"].value == "Category" and ws.max_row == len(rows) + 2


def test_issues_and_systems_persist(tmp_path):
    p = Project()
    s = DistributionSystem(name="C1", system_type=SystemType.POWER, properties={"breaker_a": 16})
    p.systems[s.id] = s
    issue = Issue(title="clash", kind="hard", element_ids=("a", "b"), point=(1.0, 2.0, 3.0),
                  status=IssueStatus.APPROVED, comment="ok")
    p.issues[issue.id] = issue
    save_project(p, tmp_path / "p.bimx")
    loaded = load_project(tmp_path / "p.bimx").project
    assert loaded.issues == p.issues
    assert loaded.systems[s.id].properties == {"breaker_a": 16}
