import pytest

from bimforge.app.commands import CommandBus, ModifyElement, MoveElements, RemoveElements
from bimforge.app.events import ElementsChanged, EventBus
from bimforge.core.model import Door, Project, Slab, Wall, Window
from bimforge.core.validation import ValidationError


@pytest.fixture
def setup():
    project = Project()
    events = EventBus()
    changes: list[ElementsChanged] = []
    events.subscribe(ElementsChanged, changes.append)
    bus = CommandBus(project, events)
    wall = Wall(name="W", start=(0, 0), end=(6, 0), height=3)
    door = Door(name="D", host_id=wall.id, offset=1.5)
    window = Window(name="Win", host_id=wall.id, offset=4.0)
    slab = Slab(boundary=[(0, 0), (6, 0), (6, 4), (0, 4)])
    for e in (wall, door, window, slab):
        project.add(e)
    return project, bus, changes, wall, door, window, slab


class TestModify:
    def test_apply_and_undo(self, setup):
        project, bus, changes, wall, *_ = setup
        bus.execute(ModifyElement(wall.id, {"thickness": 0.3, "name": "Party wall"}))
        assert (wall.thickness, wall.name) == (0.3, "Party wall")
        assert changes[-1].modified == (wall.id,)
        bus.undo()
        assert (wall.thickness, wall.name) == (0.2, "W")
        bus.redo()
        assert wall.thickness == 0.3

    @pytest.mark.parametrize(
        "changes, message",
        [
            ({"thickness": 0}, "thickness"),
            ({"end": (0, 0)}, "length"),
            ({"height": 2.0}, "wall height"),  # window head at 2.1 m no longer fits
            ({"end": (3, 0)}, "beyond"),  # shortened wall no longer contains the window
        ],
    )
    def test_invalid_wall_edits_rejected_and_rolled_back(self, setup, changes, message):
        project, bus, _, wall, *_ = setup
        before = (wall.start, wall.end, wall.thickness, wall.height)
        with pytest.raises(ValidationError, match=message):
            bus.execute(ModifyElement(wall.id, changes))
        assert (wall.start, wall.end, wall.thickness, wall.height) == before
        assert bus.undo_label is None

    def test_opening_overlap_rejected(self, setup):
        _, bus, _, _, door, window, _ = setup
        with pytest.raises(ValidationError, match="overlaps"):
            bus.execute(ModifyElement(door.id, {"offset": 3.6}))
        assert door.offset == 1.5

    def test_unknown_or_id_field_rejected(self, setup):
        _, bus, _, wall, *_ = setup
        with pytest.raises(ValidationError):
            bus.execute(ModifyElement(wall.id, {"colour": "red"}))
        with pytest.raises(ValidationError):
            bus.execute(ModifyElement(wall.id, {"id": "x"}))

    def test_missing_element(self, setup):
        _, bus, *_ = setup
        with pytest.raises(KeyError):
            bus.execute(ModifyElement("nope", {"name": "x"}))


class TestMove:
    def test_wall_moves_and_hosted_openings_are_reported(self, setup):
        project, bus, changes, wall, door, window, _ = setup
        bus.execute(MoveElements([wall.id], 1.0, 2.0))
        assert (wall.start, wall.end) == ((1.0, 2.0), (7.0, 2.0))
        assert door.offset == 1.5  # relative to the host, so it travels with it
        assert set(changes[-1].modified) == {wall.id, door.id, window.id}
        bus.undo()
        assert (wall.start, wall.end) == ((0, 0), (6, 0))

    def test_opening_slides_along_host_and_is_clamped(self, setup):
        _, bus, _, wall, door, window, _ = setup
        bus.execute(MoveElements([window.id], 0.5, 3.0))  # y component ignored
        assert window.offset == pytest.approx(4.5)
        bus.execute(MoveElements([window.id], 10, 0))
        assert window.offset == pytest.approx(6 - 0.6)

    def test_opening_moving_with_its_host_is_not_moved_twice(self, setup):
        _, bus, _, wall, door, *_ = setup
        bus.execute(MoveElements([wall.id, door.id], 1, 0))
        assert door.offset == 1.5

    def test_failed_move_is_atomic(self, setup):
        _, bus, _, wall, door, window, slab = setup
        before = list(slab.boundary)
        with pytest.raises(ValidationError):
            # Door slides onto the window: invalid, so the slab move must roll back too.
            bus.execute(MoveElements([slab.id, door.id], 2.5, 0))
        assert slab.boundary == before and door.offset == 1.5

    def test_move_is_one_undo_step(self, setup):
        _, bus, _, wall, door, window, slab = setup
        bus.execute(MoveElements([wall.id, slab.id], 1, 1))
        bus.undo()
        assert wall.start == (0, 0) and slab.boundary[0] == (0, 0)
        assert bus.undo_label is None


class TestDirtyTracking:
    def test_saved_state(self, setup):
        _, bus, _, wall, *_ = setup
        assert not bus.is_dirty
        bus.execute(ModifyElement(wall.id, {"name": "A"}))
        assert bus.is_dirty
        bus.mark_saved()
        assert not bus.is_dirty
        bus.undo()
        assert bus.is_dirty
        bus.redo()
        assert not bus.is_dirty
        bus.mark_unsaved()
        assert bus.is_dirty


def test_delete_then_undo_restores_openings(setup):
    project, bus, _, wall, door, window, _ = setup
    bus.execute(RemoveElements([wall.id]))
    assert project.hosted_openings(wall.id) == []
    bus.undo()
    assert {o.id for o in project.hosted_openings(wall.id)} == {door.id, window.id}
