"""Generates a small two-room floor plan DXF (millimetres) for demos and tests.

    +----------------------------+  10 m x 8 m outside
    |            |               |  200 mm exterior walls
    |   Room 1  door   Room 2    |  100 mm partition at x = 5 m with a 900 mm door
    |            |               |
    +----[win]-------------------+  1200 mm window in the south wall

Wall faces are drawn as separate lines, split at openings and T-junctions,
which is how most CAD plans are drawn.
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf import units

WALL_LAYER = "A-WALL"


def _box_lines(msp, faces: list[tuple[tuple[float, float], tuple[float, float]]]) -> None:
    for start, end in faces:
        msp.add_line(start, end, dxfattribs={"layer": WALL_LAYER})


def build_sample_plan(path: Path) -> Path:
    doc = ezdxf.new("R2018", setup=True)
    doc.units = units.MM
    doc.layers.add(WALL_LAYER, color=7)
    doc.layers.add("A-DOOR", color=3)
    doc.layers.add("A-GLAZ", color=4)
    doc.layers.add("A-ANNO-DIMS", color=2)

    door = doc.blocks.new("DOOR_900")
    door.add_line((0, 0), (0, 900))  # leaf, drawn open
    door.add_arc((0, 0), 900, 0, 90)  # swing
    window = doc.blocks.new("WINDOW_1200")
    for y in (0, 100, 200):
        window.add_line((0, y), (1200, y))

    msp = doc.modelspace()
    _box_lines(
        msp,
        [
            # South wall: outer face split by the window, inner face by window and partition.
            ((0, 0), (2000, 0)), ((3200, 0), (10000, 0)),
            ((200, 200), (2000, 200)), ((3200, 200), (4950, 200)), ((5050, 200), (9800, 200)),
            # North wall: inner face split by the partition.
            ((0, 8000), (10000, 8000)),
            ((200, 7800), (4950, 7800)), ((5050, 7800), (9800, 7800)),
            # West and east walls.
            ((0, 0), (0, 8000)), ((200, 200), (200, 7800)),
            ((10000, 0), (10000, 8000)), ((9800, 200), (9800, 7800)),
            # Partition at x = 5000, split by a door from y = 3000 to 3900.
            ((4950, 200), (4950, 3000)), ((5050, 200), (5050, 3000)),
            ((4950, 3900), (4950, 7800)), ((5050, 3900), (5050, 7800)),
        ],
    )
    msp.add_blockref("WINDOW_1200", (2000, 0), dxfattribs={"layer": "A-GLAZ"})
    msp.add_blockref("DOOR_900", (5050, 3000), dxfattribs={"layer": "A-DOOR"})
    msp.add_linear_dim(base=(0, -600), p1=(0, 0), p2=(10000, 0), dxfattribs={"layer": "A-ANNO-DIMS"}).render()
    msp.add_text("ROOM 1", height=250, dxfattribs={"layer": "A-ANNO-DIMS"}).set_placement((2000, 4000))

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


if __name__ == "__main__":
    import sys

    out = build_sample_plan(Path(sys.argv[1] if len(sys.argv) > 1 else "samples/two_rooms.dxf"))
    print(f"Wrote {out}")
