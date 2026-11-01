import pytest

from bimforge.app.commands import (
    AddElements,
    AddStorey,
    CommandBus,
    CopyToStorey,
    MergeProject,
    ModifyStorey,
    MoveElements,
    RemoveStorey,
)
from bimforge.app.events import ElementsChanged, EventBus
from bimforge.core.model import Door, ImportedElement, Project, Slab, Space, Storey, Wall, Window
from bimforge.core.validation import ValidationError
from bimforge.geometry.builders import Tessellator
from bimforge.geometry.kernel import NumpyKernel


@pytest.fixture
def bus():
    project = Project()
    events = EventBus()
    changes: list[ElementsChanged] = []
    events.subscribe(ElementsChanged, changes.append)
    b = CommandBus(project, events)
    b.changes = changes
    return b


def box_room(storey_id):
    """Four 0.2 m walls joined at corners (centreline 6 x 4) plus a door in the south wall."""
    corners = [(0, 0), (6, 0), (6, 4), (0, 4)]
    walls = [Wall(name=f"W{i}", start=corners[i], end=corners[(i + 1) % 4], storey_id=storey_id) for i in range(4)]
    door = Door(name="D", host_id=walls[0].id, offset=3.0, storey_id=storey_id)
    return walls, door


class TestLevels:
    def test_add_sorts_by_elevation_and_undo(self, bus):
        roof = Storey(name="Roof", elevation=6.0)
        first = Storey(name="Level 1", elevation=3.0)
        bus.execute(AddStorey(roof))
        bus.execute(AddStorey(first))
        assert [s.name for s in bus.project.storeys] == ["Level 0", "Level 1", "Roof"]
        assert bus.changes[-1].storeys
        bus.undo()
        assert [s.name for s in bus.project.storeys] == ["Level 0", "Roof"]

    def test_modify_moves_elements_with_level(self, bus):
        level = Storey(name="L1", elevation=3.0)
        bus.execute(AddStorey(level))
        wall = Wall(start=(0, 0), end=(4, 0), storey_id=level.id)
        bus.execute(AddElements([wall]))
        tess = Tessellator(NumpyKernel())
        assert tess.mesh_for(wall, bus.project).bounds()[0][2] == pytest.approx(3.0)
        bus.execute(ModifyStorey(level.id, {"elevation": 4.5, "name": "Mezzanine"}))
        assert bus.changes[-1].modified == (wall.id,)
        assert tess.mesh_for(wall, bus.project).bounds()[0][2] == pytest.approx(4.5)
        bus.undo()
        assert (level.elevation, level.name) == (3.0, "L1")

    def test_modify_rejects_bad_values(self, bus):
        with pytest.raises(ValidationError):
            bus.execute(ModifyStorey(bus.project.default_storey.id, {"height": 0}))
        with pytest.raises(ValidationError):
            bus.execute(ModifyStorey(bus.project.default_storey.id, {"id": "x"}))

    def test_remove_only_empty_and_not_last(self, bus):
        ground = bus.project.default_storey
        with pytest.raises(ValidationError, match="at least one"):
            bus.execute(RemoveStorey(ground.id))
        upper = Storey(name="L1", elevation=3)
        bus.execute(AddStorey(upper))
        bus.execute(AddElements([Wall(start=(0, 0), end=(1, 0), storey_id=upper.id)]))
        with pytest.raises(ValidationError, match="not empty"):
            bus.execute(RemoveStorey(upper.id))
        bus.undo()
        bus.execute(RemoveStorey(upper.id))
        assert upper not in bus.project.storeys
        bus.undo()
        assert upper in bus.project.storeys

    def test_copy_typical_floor(self, bus):
        ground = bus.project.default_storey
        walls, door = box_room(ground.id)
        bus.execute(AddElements([*walls, door]))
        upper = Storey(name="L1", elevation=3.0)
        bus.execute(AddStorey(upper))
        command = CopyToStorey([w.id for w in walls] + [door.id], upper.id)
        bus.execute(command)
        copies = command.created
        assert len(copies) == 5 and all(c.storey_id == upper.id for c in copies)
        assert {c.id for c in copies}.isdisjoint({w.id for w in walls} | {door.id})
        copied_door = next(c for c in copies if isinstance(c, Door))
        assert copied_door.host_id in {c.id for c in copies}
        bus.undo()
        assert len(bus.project) == 5

    def test_copy_drops_openings_without_host(self, bus):
        ground = bus.project.default_storey
        walls, door = box_room(ground.id)
        bus.execute(AddElements([*walls, door]))
        upper = Storey(name="L1", elevation=3.0)
        bus.execute(AddStorey(upper))
        command = CopyToStorey([door.id, walls[1].id], upper.id)
        bus.execute(command)
        assert [type(c) for c in command.created] == [Wall]


class TestJoinedWalls:
    def test_corner_joined_walls_stretch(self, bus):
        ground = bus.project.default_storey
        walls, door = box_room(ground.id)
        bus.execute(AddElements([*walls, door]))
        south, east, north, west = walls
        bus.execute(MoveElements([south.id], 0, -1))
        assert south.start == (0, -1) and south.end == (6, -1)
        assert east.start == (6, -1) and east.end == (6, 4)  # stretched
        assert west.end == (0, -1) and west.start == (0, 4)
        assert north.start == (6, 4)  # untouched
        bus.undo()
        assert east.start == (6, 0) and west.end == (0, 0)

    def test_t_junction_slides_and_keeps_opening_world_position(self, bus):
        ground = bus.project.default_storey
        long_wall = Wall(name="long", start=(0, 0), end=(10, 0), storey_id=ground.id)
        stem = Wall(name="stem", start=(5, 0), end=(5, 4), storey_id=ground.id)
        door = Door(host_id=stem.id, offset=2.0, storey_id=ground.id)
        bus.execute(AddElements([long_wall, stem, door]))
        # Move along the long wall: the stem must not slide sideways, only follow perpendicular moves.
        bus.execute(MoveElements([long_wall.id], 1.0, -0.5))
        assert stem.start == pytest.approx((5, -0.5))
        assert stem.end == (5, 4)
        assert stem.point_at(door.offset) == pytest.approx((5, 2))  # door stays where it was in the world

    def test_disabled_join_following(self, bus):
        ground = bus.project.default_storey
        walls, _ = box_room(ground.id)
        bus.execute(AddElements(walls))
        bus.execute(MoveElements([walls[0].id], 0, -1, move_joined=False))
        assert walls[1].start == (6, 0)

    def test_move_rejected_if_joined_wall_would_break(self, bus):
        ground = bus.project.default_storey
        walls, door = box_room(ground.id)
        east_door = Door(name="ED", host_id=walls[1].id, offset=2.0, storey_id=ground.id)
        bus.execute(AddElements([*walls, door, east_door]))
        # Moving the south wall up 3.9 m shrinks the east wall to 0.1 m; the door cannot fit.
        with pytest.raises(ValidationError):
            bus.execute(MoveElements([walls[0].id], 0, 3.9))
        assert walls[1].start == (6, 0) and walls[0].start == (0, 0)

    def test_other_levels_unaffected(self, bus):
        upper = Storey(name="L1", elevation=3.0)
        bus.execute(AddStorey(upper))
        ground_walls, _ = box_room(bus.project.default_storey.id)
        upper_walls, _ = box_room(upper.id)
        bus.execute(AddElements(ground_walls + upper_walls))
        bus.execute(MoveElements([ground_walls[0].id], 0, -1))
        assert upper_walls[1].start == (6, 0)


def test_merge_project_maps_levels_and_undoes(bus):
    source = Project("Imported")
    l1 = Storey(name="Imported L1", elevation=3.0)
    source.building.storeys.append(l1)
    on_ground = Wall(start=(0, 0), end=(1, 0), storey_id=source.default_storey.id)
    on_l1 = Wall(start=(0, 0), end=(1, 0), storey_id=l1.id)
    source.add(on_ground)
    source.add(on_l1)
    bus.execute(MergeProject(source))
    p = bus.project
    assert [s.elevation for s in p.storeys] == [0.0, 3.0]  # ground matched, L1 added
    assert on_ground.storey_id == p.default_storey.id
    assert on_l1.storey_id == p.storeys[1].id
    bus.undo()
    assert len(p) == 0 and len(p.storeys) == 1


# --- IFC import ---------------------------------------------------------------------


@pytest.fixture
def ifc_tools():
    pytest.importorskip("ifcopenshell")
    from bimforge.adapters.ifc_exporter import IfcExporter
    from bimforge.adapters.ifc_importer import IfcImporter

    return IfcExporter(), IfcImporter()


def two_storey_project() -> Project:
    p = Project("Tower")
    ground = p.default_storey
    upper = Storey(name="Level 1", elevation=3.2, height=3.2)
    p.building.storeys.append(upper)
    for storey in (ground, upper):
        walls, door = box_room(storey.id)
        for e in (*walls, door):
            p.add(e)
        p.add(Window(name="Win", host_id=walls[2].id, offset=2.0, storey_id=storey.id))
        p.add(Slab(name="Floor", boundary=[(0, 0), (6, 0), (6, 4), (0, 4)], storey_id=storey.id))
        p.add(Space(name="Room", boundary=[(0, 0), (6, 0), (6, 4), (0, 4)], storey_id=storey.id))
    # A column: something the parametric model does not cover.
    cube = [(x, y, z) for z in (0, 3) for y in (1, 1.3) for x in (1, 1.3)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 5), (0, 5, 4),
             (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    p.add(ImportedElement(name="C1", source_class="IfcColumn", category="structure",
                          vertices=cube, faces=faces, storey_id=ground.id))
    return p


def test_ifc_roundtrip_is_parametric(ifc_tools, tmp_path):
    exporter, importer = ifc_tools
    original = two_storey_project()
    path = tmp_path / "tower.ifc"
    exporter.export(original, path)
    result = importer.import_file(path)
    p = result.project

    assert [(s.name, s.elevation) for s in p.storeys] == [("Level 0", 0.0), ("Level 1", 3.2)]
    assert p.storeys[0].height == pytest.approx(3.2)
    counts = {k.__name__: len(p.elements_of(k)) for k in (Wall, Door, Window, Slab, Space, ImportedElement)}
    assert counts == {"Wall": 8, "Door": 2, "Window": 2, "Slab": 2, "Space": 2, "ImportedElement": 1}

    for storey in p.storeys:
        walls = sorted((w for w in p.elements_of(Wall) if w.storey_id == storey.id), key=lambda w: w.name)
        assert [w.thickness for w in walls] == pytest.approx([0.2] * 4)
        south = next(w for w in walls if w.name == "W0")
        assert sorted([south.start, south.end]) == [pytest.approx((0, 0)), pytest.approx((6, 0))]
        door = next(d for d in p.elements_of(Door) if d.storey_id == storey.id)
        assert door.host_id == south.id
        assert south.point_at(door.offset) == pytest.approx((3, 0))
        assert (door.width, door.height, door.sill_height) == pytest.approx((0.9, 2.1, 0.0))
        window = next(w for w in p.elements_of(Window) if w.storey_id == storey.id)
        assert window.sill_height == pytest.approx(0.9)

    column = p.elements_of(ImportedElement)[0]
    assert (column.source_class, column.category, column.name) == ("IfcColumn", "structure", "C1")
    assert any("IfcColumn" in w for w in result.warnings)


def test_ifc_guids_survive_roundtrip(ifc_tools, tmp_path):
    import ifcopenshell

    exporter, importer = ifc_tools
    first = tmp_path / "a.ifc"
    exporter.export(two_storey_project(), first)
    reimported = importer.import_file(first).project
    second = tmp_path / "b.ifc"
    exporter.export(reimported, second)
    guids = lambda path: {e.GlobalId for e in ifcopenshell.open(str(path)).by_type("IfcElement")}  # noqa: E731
    assert guids(first) == guids(second)


def test_bad_ifc_raises_value_error(ifc_tools, tmp_path):
    _, importer = ifc_tools
    bad = tmp_path / "bad.ifc"
    bad.write_text("not an ifc")
    with pytest.raises(ValueError):
        importer.import_file(bad)
