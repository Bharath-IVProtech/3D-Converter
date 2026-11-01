"""Regression tests for issues found in code review."""

import json
import sqlite3
from pathlib import Path

import ezdxf
import pytest

from bimforge.adapters.dwg_importer import _version_key
from bimforge.adapters.dxf_importer import DxfImporter
from bimforge.app.commands import CommandBus, MergeProject
from bimforge.app.events import EventBus
from bimforge.app.persistence import load_project, save_project
from bimforge.conversion import ConversionSettings, PlanConverter
from bimforge.conversion.openings import OpeningSymbol, place_openings
from bimforge.conversion.walls import WallCandidate, pair_wall_faces
from bimforge.core.model import (
    DistributionSystem,
    Door,
    Fixture,
    ImportedElement,
    Opening,
    Project,
    Storey,
    SystemType,
    Wall,
    Window,
)
from bimforge.disciplines import electrical
from bimforge.drawing.primitives import Segment

S = ConversionSettings()


def test_electrical_overload_is_reported_not_raised():
    p = Project()
    board = Fixture(name="DB", family="db", position=(0, 0, 1.4))
    p.add(board)
    circuit = DistributionSystem(name="C1", system_type=SystemType.POWER, properties={"panel_id": board.id})
    p.systems[circuit.id] = circuit
    for i in range(20):  # 20 x 2000 VA = 174 A, beyond the largest tabulated breaker
        p.add(Fixture(family="water_heater", position=(i, 0, 1.6), system_id=circuit.id))
    row = electrical.evaluate(p, circuit)
    assert not row.ok and "exceeds the largest tabulated breaker" in row.notes
    assert any("C1" in issue.title for issue in electrical.check(p))
    assert electrical.panel_schedule(p, board.id)[0].breaker_a == electrical.BREAKER_RATINGS[-1]


def test_window_that_cannot_fit_a_low_wall_is_skipped(sample_dxf):
    drawing = DxfImporter().import_file(sample_dxf).drawing
    result = PlanConverter(ConversionSettings(wall_height=0.8)).run(drawing, Project().default_storey)
    assert not [p for p in result.proposals if isinstance(p.element, Window)]
    assert any("does not fit below the wall height" in w for w in result.warnings)


def test_newest_oda_version_is_chosen():
    paths = [r"C:\Program Files\ODA\ODAFileConverter 25.9.0\ODAFileConverter.exe",
             r"C:\Program Files\ODA\ODAFileConverter 25.12.0\ODAFileConverter.exe"]
    assert "25.12.0" in max(paths, key=_version_key)


def test_door_block_with_offset_base_point_lands_in_its_gap(tmp_path):
    doc = ezdxf.new("R2018")
    doc.units = ezdxf.units.MM
    door = doc.blocks.new("DOOR_900", base_point=(450, 0))  # base point at the leaf centre, not the hinge
    door.add_line((0, 0), (0, 900))
    door.add_arc((0, 0), 900, 0, 90)
    msp = doc.modelspace()
    for y in (0, 200):
        msp.add_line((0, y), (3000, y))
        msp.add_line((3900, y), (7000, y))
    msp.add_blockref("DOOR_900", (3450, 200))  # hinge lands at x = 3000
    path = tmp_path / "base_point.dxf"
    doc.saveas(path)
    result = PlanConverter().run(DxfImporter().import_file(path).drawing, Project().default_storey)
    (door_el,) = [p.element for p in result.proposals if isinstance(p.element, Door)]
    host = next(p.element for p in result.proposals if p.element.id == door_el.host_id)
    assert host.point_at(door_el.offset)[0] == pytest.approx(3.45)


def test_door_uses_the_adjacent_gap_not_a_wider_nested_one():
    a = WallCandidate((0, 0), (3, 0), 0.2, 1.0)
    b = WallCandidate((4, 0), (5, 0), 0.2, 1.0)
    c = WallCandidate((6, 0), (9, 0), 0.2, 1.0)
    door = OpeningSymbol("door", ((3.5, 0.0),), 1.0, 0.9, "", "door")
    walls, openings, unplaced = place_openings([a, c, b], [door], S)  # (a, c) would be considered first
    assert len(walls) == 2 and not unplaced
    assert openings[0].width == pytest.approx(1.0) and openings[0].center == pytest.approx((3.5, 0))


def test_project_loads_even_if_one_element_points_to_a_missing_level(tmp_path):
    p = Project()
    p.add(Wall(start=(0, 0), end=(3, 0)))
    orphan = Wall(start=(0, 1), end=(3, 1))
    p.add(orphan)
    path = tmp_path / "p.bimx"
    save_project(p, path)
    with sqlite3.connect(path) as con:
        data = json.loads(con.execute("SELECT data FROM elements WHERE id = ?", (orphan.id,)).fetchone()[0])
        data["storey_id"] = "gone"
        con.execute("UPDATE elements SET data = ? WHERE id = ?", (json.dumps(data), orphan.id))
    con.close()
    result = load_project(path)
    assert len(result.project) == 1 and any(orphan.id in w for w in result.warnings)


def test_ifc_wall_with_an_unfilled_void_keeps_its_hole(tmp_path):
    pytest.importorskip("ifcopenshell")
    from bimforge.adapters.ifc_exporter import IfcExporter
    from bimforge.adapters.ifc_importer import IfcImporter

    p = Project()
    wall = Wall(name="W", start=(0, 0), end=(5, 0))
    plain = Wall(name="Plain", start=(0, 3), end=(5, 3))
    p.add(wall)
    p.add(plain)
    p.add(Opening(host_id=wall.id, offset=2.5, width=1.0, height=1.0, sill_height=1.0))  # a service hole
    path = tmp_path / "void.ifc"
    IfcExporter().export(p, path)
    imported = IfcImporter().import_file(path).project
    assert [w.name for w in imported.elements_of(Wall)] == ["Plain"]
    (mesh,) = [e for e in imported.elements_of(ImportedElement) if e.name == "W"]
    assert mesh.source_class == "IfcWall"


def test_zero_parallel_tolerance_does_not_divide_by_zero():
    faces = [Segment(start=(0, 0), end=(5, 0)), Segment(start=(0, 0.2), end=(5, 0.2))]
    (wall,) = pair_wall_faces(faces, ConversionSettings(parallel_tolerance_deg=0.0))
    assert wall.thickness == pytest.approx(0.2)


def test_import_with_two_levels_at_the_same_new_height_adds_one_level():
    source = Project()
    source.building.storeys[:] = [Storey(name="A", elevation=6.0), Storey(name="B", elevation=6.0)]
    for storey in source.storeys:
        source.add(Wall(start=(0, 0), end=(1, 0), storey_id=storey.id))
    target = Project()
    CommandBus(target, EventBus()).execute(MergeProject(source))
    assert [s.elevation for s in target.storeys] == [0.0, 6.0]
    assert {w.storey_id for w in target.elements_of(Wall)} == {target.storeys[1].id}
