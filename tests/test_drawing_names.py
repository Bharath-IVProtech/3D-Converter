"""Elements take the names written on the drawing, and notes around them are not mistaken for names."""

import pytest
from shapely.geometry import box

from bimforge.conversion.labels import (
    TextBlock,
    column_tag,
    door_tag,
    group_text,
    is_room_name,
    level_from_title,
    louvre_note,
    project_title,
    room_name,
    stair_name,
    window_tag,
)
from bimforge.conversion.rooms import rooms_from_walls
from bimforge.core.model import Wall
from bimforge.drawing.primitives import Text

H = 0.26  # m, text height used on the real sheet


def words(*rows, height=H):
    """Each row is (x, y, text), in metres."""
    return [Text(position=(x, y), text=t, height=height, layer="Text") for x, y, t in rows]


def block(text, x=0.0, y=0.0, height=H):
    return TextBlock(text, (x, y), height, (x - 1, y - height / 2, x + 1, y + height / 2))


class TestGrouping:
    def test_a_label_over_three_lines_reads_in_order(self):
        # As on the real sheet: "Treated / Water Tank / 30 KL", each word stored separately.
        rows = words((42.54, 40.66, "Treated"), (42.54, 40.35, "Water"), (43.17, 40.35, "Tank"),
                     (42.54, 40.03, "30"), (42.84, 40.03, "KL"))
        (only,) = group_text(rows, 1.0)
        assert only.text == "Treated Water Tank 30 KL"

    def test_a_separate_note_in_the_same_room_stays_separate(self):
        rows = words((43.06, 41.22, "Rungs"), (42.54, 40.66, "Treated"), (42.54, 40.35, "Tank"))
        assert sorted(b.text for b in group_text(rows, 1.0)) == ["Rungs", "Treated Tank"]

    def test_different_text_sizes_do_not_join(self):
        rows = words((0, 0, "KITCHEN")) + words((0, -0.3, "SMALL"), height=0.1)
        assert len(group_text(rows, 1.0)) == 2

    def test_rotated_words_group_along_their_own_line(self):
        rows = [Text(position=(5.0, y), text=t, height=H, rotation=90.0, layer="Text")
                for y, t in ((1.0, "PUMP"), (1.8, "ROOM"))]
        (only,) = group_text(rows, 1.0)
        assert only.text == "PUMP ROOM"

    def test_numbers_around_a_label_are_dropped(self):
        rows = words((0, 0.3, "2850"), (0, 0, "STORE"))
        assert group_text(rows, 1.0)[0].text == "STORE"

    def test_scale_applies(self):
        (only,) = group_text(words((1000, 2000, "HALL")), 0.001)
        assert only.centre[0] == pytest.approx(1.0, abs=0.2) and only.centre[1] == pytest.approx(2.0, abs=0.2)


class TestWhatCountsAsARoomName:
    @pytest.mark.parametrize("text", ["SLUDGE HOLDING TANK 10KL", "MBBR TANK-1 CAP-10KL", "UF FEED TANK 10KL",
                                      "Kitchen", "PUMP ROOM"])
    def test_names(self, text):
        assert is_room_name(block(text))

    @pytest.mark.parametrize("text", ["425x300 EA B.O.D 4300", "Wall cutout:900x900", "2850", "ES-1", "UP", "Rungs",
                                      "EA LOUVER W/O VCD", "AXIAL FLOW FANS", "CUTOUT TO BE SEALED AFTER INST'LN OF DUCT",
                                      "THIS DRAWING MUST NOT BE COPIED OR USED WITHOUT THE WRITTEN AUTHORITY"])
    def test_not_names(self, text):
        assert not is_room_name(block(text))


class TestRoomNames:
    ROOM = box(0, 0, 6, 4)

    def test_label_inside_the_room_wins_over_notes_and_outside_text(self):
        blocks = [block("425x300 EA B.O.D 4300", 1, 1), block("Rungs", 5, 3), block("ANOXIC TANK", 3, 2), block("LOBBY", 20, 2)]
        assert room_name(self.ROOM, blocks) == "ANOXIC TANK"

    def test_no_label_means_no_name(self):
        assert room_name(self.ROOM, [block("425x300 EA", 1, 1), block("LOBBY", 20, 2)]) is None

    def test_larger_text_wins(self):
        blocks = [block("store", 3, 2, height=0.1), block("PLANT ROOM", 1, 1, height=0.3)]
        assert room_name(self.ROOM, blocks) == "PLANT ROOM"

    def test_detect_rooms_uses_the_label(self):
        corners = [(0, 0), (6, 0), (6, 4), (0, 4)]
        walls = [Wall(start=corners[i], end=corners[(i + 1) % 4]) for i in range(4)]
        (room,) = rooms_from_walls(walls, [], 1.0, 3.0, None, labels=[block("KITCHEN", 3, 2)])
        assert room.name == "KITCHEN"


class TestTags:
    def test_door_and_window_tags_are_told_apart(self):
        blocks = [block("D1", 1, 0), block("W2", 1.2, 0), block("DOOR SCHEDULE", 1, 0.1)]
        assert door_tag((1, 0.5), blocks) == "D1"
        assert window_tag((1, 0.5), blocks) == "W2"

    def test_a_far_tag_is_ignored(self):
        assert door_tag((0, 0), [block("D1", 9, 0)]) is None

    def test_grid_marks_are_not_tags(self):
        blocks = [block("ES-1", 0, 0), block("A", 0, 0), block("425x300", 0, 0)]
        assert door_tag((0, 0), blocks) is window_tag((0, 0), blocks) is column_tag((0, 0), blocks) is None

    def test_column_stair_and_louvre_text(self):
        assert column_tag((0, 0), [block("C12", 0.5, 0)]) == "C12"
        assert stair_name((0, 0), [block("STAIRCASE-2", 1, 1)]) == "STAIRCASE-2"
        assert stair_name((0, 0), [block("UP", 1, 1)]) is None
        assert louvre_note((0, 0), [block("FA LOUVER W/O VCD 1X2200X500H.", 1, 1)]) == "FA LOUVER W/O VCD 1X2200X500H"


class TestTitleBlock:
    def test_project_title_is_the_text_under_its_heading(self):
        blocks = [TextBlock("PROJECT TITLE:", (77, 16.1), 0.36, (75.9, 15.9, 78, 16.3)),
                  TextBlock("GODREJ HOUSING AT SECTOR-103, GURUGRAM", (78, 14.5), 0.6, (75.9, 14, 81, 15.2)),
                  TextBlock("DRAWING TITLE:", (77, 12.8), 0.36, (75.9, 12.6, 78, 13)),
                  TextBlock("ELSEWHERE", (30, 15), 0.6, (29, 14.7, 31, 15.3))]
        assert project_title(blocks) == "GODREJ HOUSING AT SECTOR-103, GURUGRAM"

    def test_no_heading_no_title(self):
        assert project_title([block("GROUND FLOOR")]) is None

    def test_level_comes_from_the_plan_title(self):
        assert level_from_title([block("EWS-STP UGT HVAC PLAN AT INTERMEDIATE LEVEL")]) == "INTERMEDIATE LEVEL"
        assert level_from_title([block("GROUND FLOOR PLAN")]) is None


def test_a_dxf_plan_uses_its_room_labels(tmp_path):
    """End to end: a label in a closed room reaches the proposal, and an unlabelled room keeps a number."""
    ezdxf = pytest.importorskip("ezdxf")
    from bimforge.adapters.dxf_importer import DxfImporter
    from bimforge.conversion import ConversionSettings, PlanConverter
    from bimforge.core.model import Project, Space

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6  # metres
    msp = doc.modelspace()
    t = 0.2
    for x0, x1 in ((0, 6), (6, 12)):
        for inset in (0.0, t):
            msp.add_lwpolyline([(x0 + inset, inset), (x1 - inset, inset), (x1 - inset, 5 - inset), (x0 + inset, 5 - inset)],
                               close=True, dxfattribs={"layer": "A-WALL"})
    msp.add_text("PUMP ROOM", dxfattribs={"height": 0.25}).set_placement((2, 2.5))
    msp.add_text("425x300 EA", dxfattribs={"height": 0.25}).set_placement((8, 2.5))
    path = tmp_path / "plan.dxf"
    doc.saveas(path)
    p = Project("t")
    result = PlanConverter(ConversionSettings()).run(DxfImporter().import_file(path, {}).drawing, p.default_storey)
    names = sorted(x.element.name for x in result.proposals if isinstance(x.element, Space))
    assert "PUMP ROOM" in names and any(n.startswith("Room ") for n in names)
