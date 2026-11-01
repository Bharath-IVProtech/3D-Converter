import json
import sqlite3

import pytest

from bimforge.app.persistence import ProjectFileError, load_project, save_project
from bimforge.core.model import (
    CableCarrierSegment,
    DistributionSystem,
    Door,
    Fixture,
    PipeSegment,
    Project,
    Slab,
    Space,
    Storey,
    SystemType,
    Wall,
    Window,
)


@pytest.fixture
def project() -> Project:
    p = Project("Clinic")
    ground = p.default_storey
    first = Storey(name="Level 1", elevation=3.2, height=3.2)
    p.building.storeys.append(first)
    cold = DistributionSystem(name="CW-1", system_type=SystemType.COLD_WATER, color=(0.1, 0.4, 0.9))
    p.systems[cold.id] = cold
    wall = Wall(name="W1", start=(0, 0), end=(5, 0), thickness=0.25, storey_id=ground.id, properties={"fire": "EI60"})
    p.add(wall)
    p.add(Door(name="D1", host_id=wall.id, offset=1.0, storey_id=ground.id))
    p.add(Window(name="WIN1", host_id=wall.id, offset=3.5, storey_id=ground.id))
    p.add(Slab(boundary=[(0, 0), (5, 0), (5, 4), (0, 4)], storey_id=first.id))
    p.add(Space(name="Room", boundary=[(0, 0), (5, 0), (5, 4)], storey_id=ground.id))
    p.add(PipeSegment(path=[(0, 0, 0.3), (2, 0, 0.3)], system_id=cold.id, diameter=0.032, storey_id=ground.id))
    p.add(CableCarrierSegment(path=[(0, 1, 2.8), (4, 1, 2.8)], storey_id=first.id))
    p.add(Fixture(position=(1, 2, 0), family="WC", rotation=90, storey_id=ground.id))
    return p


def _snapshot(p: Project) -> dict:
    return {e.id: (type(e), e) for e in p.elements()}


def test_roundtrip_preserves_everything(project, tmp_path):
    path = tmp_path / "clinic.bimx"
    save_project(project, path)
    result = load_project(path)
    loaded = result.project
    assert result.warnings == []
    assert (loaded.id, loaded.name) == (project.id, project.name)
    assert [(s.id, s.name, s.elevation) for s in loaded.storeys] == [(s.id, s.name, s.elevation) for s in project.storeys]
    assert loaded.systems == project.systems
    assert _snapshot(loaded) == _snapshot(project)  # dataclass equality: types, tuples and all fields


def test_coordinates_come_back_as_tuples(project, tmp_path):
    path = tmp_path / "p.bimx"
    save_project(project, path)
    loaded = load_project(path).project
    wall = loaded.elements_of(Wall)[0]
    assert isinstance(wall.start, tuple)
    assert all(isinstance(p, tuple) for p in loaded.elements_of(Slab)[0].boundary)
    assert loaded.elements_of(Wall)[0].properties == {"fire": "EI60"}


def test_save_overwrites_atomically(project, tmp_path):
    path = tmp_path / "p.bimx"
    save_project(project, path)
    project.add(Wall(start=(0, 5), end=(5, 5)))
    save_project(project, path)
    assert len(load_project(path).project) == len(project)
    assert not (tmp_path / "p.bimx.tmp").exists()


def test_unknown_element_class_is_skipped_with_warning(project, tmp_path):
    path = tmp_path / "p.bimx"
    save_project(project, path)
    with sqlite3.connect(path) as con:
        con.execute("INSERT INTO elements VALUES ('x1', 'HeatPump', NULL, '{}')")
    con.close()
    result = load_project(path)
    assert len(result.project) == len(project)
    assert any("HeatPump" in w for w in result.warnings)


def test_missing_and_extra_fields_tolerated(tmp_path):
    p = Project()
    wall = Wall(start=(0, 0), end=(3, 0))
    p.add(wall)
    path = tmp_path / "p.bimx"
    save_project(p, path)
    with sqlite3.connect(path) as con:
        data = json.loads(con.execute("SELECT data FROM elements").fetchone()[0])
        del data["height"]  # older file without the field
        data["acoustic_rating"] = 52  # newer file with an extra field
        con.execute("UPDATE elements SET data = ?", (json.dumps(data),))
    con.close()
    loaded = load_project(path).project.get(wall.id)
    assert loaded.height == 3.0


def test_newer_schema_rejected(project, tmp_path):
    path = tmp_path / "p.bimx"
    save_project(project, path)
    with sqlite3.connect(path) as con:
        con.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    con.close()
    with pytest.raises(ProjectFileError, match="newer version"):
        load_project(path)


def test_not_a_project_file(tmp_path):
    bogus = tmp_path / "bogus.bimx"
    bogus.write_text("hello")
    with pytest.raises(ProjectFileError):
        load_project(bogus)
    with pytest.raises(ProjectFileError):
        load_project(tmp_path / "missing.bimx")
