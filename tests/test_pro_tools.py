"""Professional editing tools: snapping, rotate, mirror, copy, array, room detection, schedules."""

import csv
import math

import pytest

from bimforge.app.commands import (
    AddElements,
    CommandBus,
    CopyElements,
    TransformElements,
    mirror_across,
    rotation_about,
)
from bimforge.app.events import ElementsChanged, EventBus
from bimforge.conversion.rooms import rooms_from_walls
from bimforge.coordination.schedules import build_schedules, export_schedule_csv
from bimforge.core.model import (
    Column,
    Door,
    DuctSegment,
    Fixture,
    ImportedElement,
    PipeSegment,
    Project,
    Slab,
    Space,
    Stair,
    Wall,
    Window,
)
from bimforge.core.validation import ValidationError
from bimforge.geometry.builders import Tessellator
from bimforge.geometry.kernel import NumpyKernel
from bimforge.geometry.snapping import fit_opening, ids_in_box, nearest_wall, snap_point

tess = Tessellator(NumpyKernel())


class TestSnapping:
    WALL = ((0.0, 0.0), (4.0, 0.0))

    def test_wall_end_wins(self):
        snap = snap_point((3.9, 0.1), [self.WALL])
        assert (snap.kind, snap.point) == ("end", (4.0, 0.0))

    def test_wall_middle(self):
        snap = snap_point((2.1, 0.1), [self.WALL])
        assert (snap.kind, snap.point) == ("middle", (2.0, 0.0))

    def test_along_a_wall(self):
        snap = snap_point((1.0, 0.2), [self.WALL])
        assert (snap.kind, snap.point) == ("wall", (1.0, 0.0))

    def test_grid_when_nothing_is_near(self):
        snap = snap_point((7.03, 8.02), [self.WALL])
        assert (snap.kind, snap.point) == ("grid", (7.05, 8.0))

    def test_straight_line_lock(self):
        assert snap_point((3.02, 0.4), last=(1.0, 0.0), straight=True).point == (3.0, 0.0)
        assert snap_point((1.3, 2.52), last=(1.0, 0.0), straight=True).point == (1.0, 2.5)

    def test_straight_lock_still_lands_on_a_wall_end(self):
        snap = snap_point((3.9, 0.1), [self.WALL], last=(0.0, 5.0), straight=True)
        assert (snap.kind, snap.point) == ("end", (4.0, 0.0))


class TestPlacing:
    def test_nearest_wall_and_distance_along_it(self):
        near, far = Wall(start=(0, 0), end=(4, 0)), Wall(start=(0, 3), end=(4, 3))
        wall, along = nearest_wall((1.5, 0.4), [far, near])
        assert wall is near and along == pytest.approx(1.5)
        assert nearest_wall((1.5, 1.5), [far, near]) is None

    def test_opening_stays_inside_the_wall_and_finds_the_middle(self):
        assert fit_opening(0.1, 4.0, 1.0) == 0.5
        assert fit_opening(3.9, 4.0, 1.0) == 3.5
        assert fit_opening(2.1, 4.0, 1.0) == 2.0
        assert fit_opening(1.03, 4.0, 1.0) == pytest.approx(1.05)
        assert fit_opening(1.0, 0.8, 1.0) is None

    def test_box_select_inside_versus_touching(self):
        boxes = {"inside": (1, 2, 1, 2, 0, 3), "across": (3, 7, 1, 2, 0, 3), "away": (9, 10, 9, 10, 0, 3)}
        assert ids_in_box(boxes, (0, 0), (5, 5)) == ["inside"]
        assert ids_in_box(boxes, (5, 5), (0, 0)) == ["inside", "across"]


@pytest.fixture
def model():
    p = Project()
    bus = CommandBus(p, EventBus())
    changes: list[ElementsChanged] = []
    bus.events.subscribe(ElementsChanged, changes.append)
    wall = Wall(name="W", start=(0, 0), end=(4, 0), storey_id=p.default_storey.id)
    door = Door(name="D", host_id=wall.id, offset=1.0, storey_id=p.default_storey.id)
    bus.execute(AddElements([wall, door]))
    return p, bus, wall, door, changes


def test_drawn_elements_are_validated_and_a_refused_one_leaves_nothing(model):
    p, bus, wall, door, _ = model
    overlapping = Door(name="D2", host_id=wall.id, offset=1.2, storey_id=wall.storey_id)
    with pytest.raises(ValidationError, match="overlaps"):
        bus.execute(AddElements([overlapping], validate=True))
    assert overlapping.id not in p and len(p) == 2


class TestRotateMirror:
    def test_rotate_wall_keeps_door_on_it(self, model):
        p, bus, wall, door, changes = model
        bus.execute(TransformElements([wall.id, door.id], rotation_about((0, 0), 90), "Rotate"))
        assert wall.end == pytest.approx((0, 4)) and wall.length == pytest.approx(4)
        assert door.offset == 1.0 and door.id in changes[-1].modified
        bus.undo()
        assert wall.end == (4, 0)

    def test_mirror_across_vertical_and_horizontal_lines(self, model):
        _, bus, wall, *_ = model
        bus.execute(TransformElements([wall.id], mirror_across((5, 5), "x")))
        assert wall.start == pytest.approx((10, 0)) and wall.end == pytest.approx((6, 0))
        bus.undo()
        bus.execute(TransformElements([wall.id], mirror_across((5, 5), "y")))
        assert wall.start == pytest.approx((0, 10))

    def test_every_element_type_transforms(self):
        p = Project()
        elements = [
            Column(position=(1, 0), rotation=0),
            Slab(boundary=[(0, 0), (1, 0), (1, 1)]),
            Space(boundary=[(0, 0), (1, 0), (1, 1)]),
            Stair(start=(0, 0), end=(2, 0), further_flights=[(2, 1, 0, 1, 1.0, 8)]),
            PipeSegment(path=[(1, 0, 0.5), (2, 0, 0.5)]),
            DuctSegment(path=[(1, 0, 4), (2, 0, 4)]),
            Fixture(family="wc", position=(1, 0, 0)),
        ]
        for e in elements:
            p.add(e)
        CommandBus(p, EventBus()).execute(TransformElements([e.id for e in elements], rotation_about((0, 0), 90)))
        column, slab, space, stair, pipe, duct, fixture = elements
        assert column.position == pytest.approx((0, 1)) and column.rotation == pytest.approx(90)
        assert slab.boundary[1] == pytest.approx((0, 1)) and space.area == pytest.approx(0.5)
        assert stair.end == pytest.approx((0, 2)) and stair.further_flights[0][:2] == pytest.approx((-1, 2))
        assert pipe.path[0] == pytest.approx((0, 1, 0.5)) and duct.path[1] == pytest.approx((0, 2, 4))
        assert fixture.position == pytest.approx((0, 1, 0)) and fixture.rotation == pytest.approx(90)

    def test_mirrored_imported_mesh_stays_solid(self):
        p = Project()
        verts = [(x, y, z) for z in (0, 1) for y in (0, 1) for x in (0, 1)]
        faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 5), (0, 5, 4),
                 (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
        cube = ImportedElement(vertices=verts, faces=faces)
        p.add(cube)
        CommandBus(p, EventBus()).execute(TransformElements([cube.id], mirror_across((5, 0), "x")))
        assert tess.mesh_for(cube, p).volume() == pytest.approx(1.0)

    def test_invalid_result_is_rejected_and_rolled_back(self):
        p = Project()
        slab = Slab(boundary=[(0, 0), (1, 0), (1, 1)])
        p.add(slab)
        bus = CommandBus(p, EventBus())
        with pytest.raises(ValidationError):
            bus.execute(TransformElements([slab.id], (0, 0, 0, 0, 0, 0)))  # collapses to a point
        assert slab.boundary == [(0, 0), (1, 0), (1, 1)] and bus.undo_label != "Transform"


class TestCopyArray:
    def test_copy_wall_with_its_door(self, model):
        p, bus, wall, door, changes = model
        command = CopyElements([wall.id, door.id], [(0, 3)])
        bus.execute(command)
        new_wall, new_door = command.created
        assert new_wall.start == (0, 3) and new_door.host_id == new_wall.id
        assert {new_wall.id, new_door.id}.isdisjoint({wall.id, door.id})
        assert new_door.id in changes[-1].added
        assert wall.start == (0, 0)
        bus.undo()
        assert len(p) == 2
        bus.redo()
        assert len(p) == 4

    def test_copying_a_wall_brings_its_door_and_names_stay_distinct(self, model):
        p, bus, wall, door, _ = model
        command = CopyElements([wall.id], [(0, 3), (0, 6)])
        bus.execute(command)
        doors = [e for e in command.created if isinstance(e, Door)]
        walls = [e for e in command.created if isinstance(e, Wall)]
        assert [d.host_id for d in doors] == [w.id for w in walls]
        names = [e.name for e in p.elements()]
        assert len(names) == len(set(names)) and "W (3)" in names

    def test_door_alone_is_not_copied(self, model):
        _, bus, _, door, _ = model
        command = CopyElements([door.id], [(1, 0)])
        bus.execute(command)
        assert command.created == []

    def test_array_makes_one_copy_per_offset_in_one_undo_step(self, model):
        p, bus, wall, *_ = model
        command = CopyElements([wall.id], [(0, 3 * i) for i in range(1, 5)], "Array")
        bus.execute(command)
        assert [e.start for e in command.created if isinstance(e, Wall)] == [(0, 3), (0, 6), (0, 9), (0, 12)]
        bus.undo()
        assert len(p) == 2

    def test_copies_drop_source_ifc_ids(self):
        p = Project()
        wall = Wall(start=(0, 0), end=(1, 0), properties={"ifc_guid": "abc", "fire": "EI60"})
        p.add(wall)
        command = CopyElements([wall.id], [(0, 1)])
        CommandBus(p, EventBus()).execute(command)
        assert command.created[0].properties == {"fire": "EI60"} and wall.properties["ifc_guid"] == "abc"


class TestRooms:
    @staticmethod
    def box(p):
        s = p.default_storey.id
        corners = [(0, 0), (6, 0), (6, 4), (0, 4)]
        return [Wall(start=corners[i], end=corners[(i + 1) % 4], storey_id=s) for i in range(4)]

    def test_closed_loop_makes_a_room(self):
        p = Project()
        (room,) = rooms_from_walls(self.box(p), [], 1.0, 3.0, p.default_storey.id)
        assert room.area == pytest.approx(24) and room.name == "Room 1" and room.height == 3.0

    def test_existing_rooms_are_not_duplicated(self):
        p = Project()
        walls = self.box(p)
        (room,) = rooms_from_walls(walls, [], 1.0, 3.0, None)
        assert rooms_from_walls(walls, [room], 1.0, 3.0, None) == []

    def test_a_new_room_never_repeats_a_name(self):
        p = Project()
        (room,) = rooms_from_walls(self.box(p), [], 1.0, 3.0, None, used_names=["Room 1", "Room 3"])
        assert room.name == "Room 2"

    def test_open_walls_make_no_room(self):
        p = Project()
        assert rooms_from_walls(self.box(p)[:3], [], 1.0, 3.0, None) == []


def test_schedules_and_csv(tmp_path):
    p = Project()
    s = p.default_storey.id
    wall = Wall(name="W1", start=(0, 0), end=(5, 0), storey_id=s)
    p.add(wall)
    p.add(Door(name="D1", host_id=wall.id, offset=1, width=0.9, height=2.1, storey_id=s))
    p.add(Window(name="WN1", host_id=wall.id, offset=3, storey_id=s))
    p.add(Space(name="Lobby", boundary=[(0, 0), (4, 0), (4, 3), (0, 3)], storey_id=s))
    p.add(Stair(name="ST1", start=(0, 1), end=(2.5, 1), risers=10, height=3.4,
                further_flights=[(2.5, 2.2, 0, 2.2, 1.0, 10)], storey_id=s))
    p.add(Column(name="C1", position=(1, 1), storey_id=s))
    schedules = {sch.name: sch for sch in build_schedules(p)}
    assert schedules["Doors"].rows == [["D1", "Level 0", "900", "2100", "W1"]]
    assert schedules["Doors"].element_ids == [p.elements_of(Door)[0].id]
    assert schedules["Rooms"].rows == [["Lobby", "Level 0", "12.00", "3.00"]]
    assert schedules["Walls"].rows[0][-1] == "2"
    assert schedules["Stairs"].rows == [["ST1", "Level 0", "2", "20", "170", "1000", "3.40"]]
    assert schedules["Windows"].totals == "1 item"
    path = tmp_path / "doors.csv"
    export_schedule_csv(schedules["Doors"], path)
    with open(path, encoding="utf-8-sig") as fh:
        assert list(csv.reader(fh))[1][0] == "D1"
    assert math.isclose(float(schedules["Rooms"].rows[0][2]), 12.0)


def test_scale_is_the_largest_that_fits():
    from bimforge.ui.sheets import choose_scale

    assert choose_scale(10, 8, 300, 250) == 50
    assert choose_scale(40, 20, 300, 250) == 150
    assert choose_scale(5000, 5000, 300, 250) == 5000


def test_pdf_sheets_have_a_plan_per_level_and_schedules(tmp_path):
    pypdfium2 = pytest.importorskip("pypdfium2")
    from PySide6.QtWidgets import QApplication

    from bimforge.app.commands import AddStorey
    from bimforge.core.model import Storey
    from bimforge.ui.sheets import export_sheets

    QApplication.instance() or QApplication([])
    p = Project("Demo")
    path = tmp_path / "sheets.pdf"
    assert export_sheets(p, path) == 0 and not path.exists()
    s = p.default_storey.id
    wall = Wall(name="W1", start=(0, 0), end=(10, 0), storey_id=s)
    p.add(wall)
    p.add(Door(name="D1", host_id=wall.id, offset=2, storey_id=s))
    p.add(Window(name="WN1", host_id=wall.id, offset=6, storey_id=s))
    p.add(Space(name="Hall", boundary=[(0, 0), (10, 0), (10, 6), (0, 6)], storey_id=s))
    p.add(Stair(name="ST1", start=(1, 1), end=(4, 1), storey_id=s))
    p.add(Column(name="C1", position=(5, 5), storey_id=s))
    upper = Storey(name="Level 1", elevation=3.0)
    CommandBus(p, EventBus()).execute(AddStorey(upper))
    p.add(Slab(name="S1", boundary=[(0, 0), (4, 0), (4, 4)], storey_id=upper.id))
    assert export_sheets(p, path) == 3
    document = pypdfium2.PdfDocument(str(path))
    assert len(document) == 3
    image = document[0].render(scale=0.5).to_pil().convert("L")
    assert image.getextrema()[0] < 90  # the walls were drawn
    if QApplication.instance().platformName() != "offscreen":  # that platform may have no fonts
        assert "Doors" in document[2].get_textpage().get_text_range()
    document.close()
