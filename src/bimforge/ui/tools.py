"""Interactive viewport tools: draw walls and slabs, place openings and fixtures, draw MEP runs, measure.

The main window forwards clicks/moves (already converted to world points) to
the active tool. Tools build elements and hand them to the window as commands,
so everything they create is undoable and validated like any other edit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import numpy as np

from bimforge.app.commands import AddElements, CopyElements, MoveElements
from bimforge.conversion.ducts import AIR_SYSTEMS
from bimforge.core.model import (
    CableCarrierSegment,
    Column,
    Door,
    Fixture,
    PipeSegment,
    Point3,
    Slab,
    Stair,
    SystemType,
    Wall,
    Window,
)
from bimforge.disciplines.catalog import FAMILIES, connection_point
from bimforge.disciplines.plumbing import DRAINAGE_TYPES, apply_slope, min_drain_slope
from bimforge.geometry.snapping import GRID, SNAP_RADIUS, Snap, fit_opening, ids_in_box, nearest_wall, snap_point
from bimforge.plugins.sdk import RenderStyle

if TYPE_CHECKING:
    from bimforge.ui.main_window import MainWindow

PREVIEW = "tool:preview"
MARKER = 0.12  # m: half the size of the snap marker
PREVIEW_STYLE = RenderStyle(color=(0.95, 0.2, 0.6), category="tool")


def snap_to_grid(p: Point3) -> Point3:
    return (round(p[0] / GRID) * GRID, round(p[1] / GRID) * GRID, p[2])


@dataclass(frozen=True)
class Option:
    """A number the user can change while the tool runs, shown in the bar above the view."""

    attr: str
    label: str
    low: float
    high: float
    step: float = 1.0
    decimals: int = 0
    suffix: str = ""


class Tool:
    needs_surface = False  # True: points come from surfaces; False: from the active work plane
    hint = ""
    title = ""
    options: ClassVar[tuple[Option, ...]] = ()

    def __init__(self, window: MainWindow) -> None:
        self.w = window

    def work_height(self) -> float:
        return self.w.active_elevation()

    def hover(self, point: Point3 | None, shift: bool) -> None: ...

    def click(self, point: Point3, shift: bool) -> None: ...

    def finish(self) -> None:
        self.cancel()

    def cancel(self) -> None:
        self.w.view.clear(PREVIEW)
        self.w.view.render()

    def _preview(self, points: list[Point3]) -> None:
        self.w.view.clear(PREVIEW)
        if len(points) >= 2:
            segments = np.array([[a, b] for a, b in zip(points, points[1:])], dtype=np.float64)
            self.w.view.upsert_lines(PREVIEW, segments, PREVIEW_STYLE)
        self.w.view.render()


class PlanTool(Tool):
    """A tool that picks plan points, snapped to the walls of the active level."""

    def walls(self) -> list[Wall]:
        return [w for w in self.w.project.elements_of(Wall) if w.storey_id == self.w.active_storey_id]

    def extra_segments(self) -> list:
        return []

    def snap(self, point: Point3, shift: bool, last=None) -> Snap:
        segments = [(w.start, w.end) for w in self.walls()] + self.extra_segments()
        return snap_point(point[:2], segments, last=last, straight=shift)

    def plane(self, p) -> Point3:
        return (p[0], p[1], self.work_height() + 0.02)

    def draw(self, segments: list, snap: Snap | None = None) -> None:
        """Rubber-band lines plus a marker showing what the cursor has snapped to."""
        lines = [(self.plane(a), self.plane(b)) for a, b in segments]
        if snap is not None:
            lines += self._marker(snap)
            self.w.show_snap(snap.kind)
        self.w.view.clear(PREVIEW)
        if lines:
            self.w.view.upsert_lines(PREVIEW, np.array(lines, dtype=np.float64), PREVIEW_STYLE)
        self.w.view.render()

    def _marker(self, snap: Snap) -> list:
        x, y = snap.point
        m = MARKER
        if snap.kind == "end":
            ring = [(x - m, y - m), (x + m, y - m), (x + m, y + m), (x - m, y + m)]
        elif snap.kind == "middle":
            ring = [(x - m, y - m), (x + m, y - m), (x, y + m)]
        elif snap.kind == "wall":
            return [(self.plane((x - m, y - m)), self.plane((x + m, y + m))),
                    (self.plane((x - m, y + m)), self.plane((x + m, y - m)))]
        else:
            return [(self.plane((x - m / 2, y)), self.plane((x + m / 2, y))),
                    (self.plane((x, y - m / 2)), self.plane((x, y + m / 2)))]
        return [(self.plane(a), self.plane(b)) for a, b in zip(ring, ring[1:] + ring[:1])]

    def next_name(self, prefix: str) -> str:
        taken = {e.name for e in self.w.project.elements()}
        n = 1
        while f"{prefix}-{n}" in taken:
            n += 1
        return f"{prefix}-{n}"

    def cancel(self) -> None:
        self.w.show_snap("")
        super().cancel()


class DrawWallTool(PlanTool):
    title = "Wall"
    hint = "Click to start a wall, click again for each corner. Shift = straight. Enter or right-click ends the chain."
    options = (Option("thickness_mm", "Thickness", 50, 2000, 10, 0, " mm"),
               Option("height_m", "Height", 0.1, 100, 0.1, 2, " m"))
    thickness_mm = 200.0

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.height_m = window.active_storey().height
        self.first = None
        self.last = None

    def hover(self, point, shift):
        if point is None:
            return
        snap = self.snap(point, shift, self.last)
        self.draw([(self.last, snap.point)] if self.last else [], snap)
        if self.last:
            self.w.statusBar().showMessage(f"Wall length {math.dist(self.last, snap.point):.3f} m")

    def click(self, point, shift):
        at = self.snap(point, shift, self.last).point
        if self.last is None:
            self.first = self.last = at
            return
        if math.dist(self.last, at) < 0.01:
            return
        storey = self.w.active_storey()
        wall = Wall(name=self.next_name("W"), start=self.last, end=at, thickness=self.thickness_mm / 1000,
                    height=self.height_m, storey_id=storey.id)
        if not self.w._execute(AddElements([wall], label="Draw wall", validate=True)):
            return
        if at == self.first:  # the loop is closed
            self.finish()
        else:
            self.last = at

    def finish(self):
        self.first = self.last = None
        self.draw([])
        self.w.statusBar().showMessage(self.hint)

    def cancel(self):
        self.first = self.last = None
        super().cancel()


class PlaceOpeningTool(PlanTool):
    make: ClassVar[type]
    prefix = ""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.hint = f"Move over a wall and click to place a {self.title.lower()}. Esc to stop."

    def locate(self, point):
        """(wall, offset) where the opening would go, or None if no wall is near or wide enough."""
        found = nearest_wall(point[:2], self.walls())
        if found is None:
            return None
        wall, along = found
        offset = fit_opening(along, wall.length, self.width_mm / 1000)
        return (wall, offset) if offset is not None else None

    def hover(self, point, shift):
        spot = self.locate(point) if point is not None else None
        if spot is None:
            self.w.view.clear(PREVIEW)
            self.w.view.render()
            return
        wall, offset = spot
        half, base = self.width_mm / 2000, self.w.active_elevation() + wall.base_offset + self.sill()
        a, b = wall.point_at(offset - half), wall.point_at(offset + half)
        top = base + self.height_mm / 1000
        corners = [(a[0], a[1], base), (b[0], b[1], base), (b[0], b[1], top), (a[0], a[1], top), (a[0], a[1], base)]
        self._preview(corners)
        self.w.statusBar().showMessage(f"{wall.name or 'Wall'}: {offset - half:.3f} m from its start, "
                                       f"{wall.length - offset - half:.3f} m from its end")

    def sill(self) -> float:
        return 0.0

    def click(self, point, shift):
        spot = self.locate(point)
        if spot is None:
            self.w.statusBar().showMessage("Click on a wall of the active level that is wide enough")
            return
        wall, offset = spot
        element = self.make(name=self.next_name(self.prefix), host_id=wall.id, offset=round(offset, 4),
                            width=self.width_mm / 1000, height=self.height_mm / 1000, sill_height=self.sill(),
                            storey_id=wall.storey_id)
        self.w._execute(AddElements([element], label=f"Place {self.title.lower()}", validate=True))


class PlaceDoorTool(PlaceOpeningTool):
    title = "Door"
    make = Door
    prefix = "D"
    options = (Option("width_mm", "Width", 300, 6000, 50, 0, " mm"), Option("height_mm", "Height", 500, 6000, 50, 0, " mm"))
    width_mm = 900.0
    height_mm = 2100.0


class PlaceWindowTool(PlaceOpeningTool):
    title = "Window"
    make = Window
    prefix = "WN"
    options = (Option("width_mm", "Width", 200, 10000, 50, 0, " mm"), Option("height_mm", "Height", 200, 6000, 50, 0, " mm"),
               Option("sill_mm", "Sill", 0, 5000, 50, 0, " mm"))
    width_mm = 1200.0
    height_mm = 1200.0
    sill_mm = 900.0

    def sill(self) -> float:
        return self.sill_mm / 1000


class PlaceColumnTool(PlanTool):
    title = "Column"
    hint = "Click to place a column; it snaps to wall ends. Esc to stop."
    options = (Option("width_mm", "Width", 50, 5000, 50, 0, " mm"), Option("depth_mm", "Depth", 50, 5000, 50, 0, " mm"),
               Option("rotation", "Rotation", -180, 180, 15, 0, "°"), Option("height_m", "Height", 0.1, 100, 0.1, 2, " m"))
    width_mm = 300.0
    depth_mm = 300.0
    rotation = 0.0

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.height_m = window.active_storey().height

    def hover(self, point, shift):
        if point is None:
            return
        snap = self.snap(point, shift)
        x, y = snap.point
        a = math.radians(self.rotation)
        ux, uy = math.cos(a) * self.width_mm / 2000, math.sin(a) * self.width_mm / 2000
        vx, vy = -math.sin(a) * self.depth_mm / 2000, math.cos(a) * self.depth_mm / 2000
        ring = [(x + ux + vx, y + uy + vy), (x - ux + vx, y - uy + vy), (x - ux - vx, y - uy - vy), (x + ux - vx, y + uy - vy)]
        self.draw(list(zip(ring, ring[1:] + ring[:1])), snap)

    def click(self, point, shift):
        column = Column(name=self.next_name("C"), position=self.snap(point, shift).point, width=self.width_mm / 1000,
                        depth=self.depth_mm / 1000, rotation=self.rotation, height=self.height_m,
                        storey_id=self.w.active_storey_id)
        self.w._execute(AddElements([column], label="Place column", validate=True))


class DrawSlabTool(PlanTool):
    title = "Slab"
    hint = "Click the corners of the slab; click the first corner again, or press Enter, to finish. Shift = straight."
    options = (Option("thickness_mm", "Thickness", 20, 3000, 10, 0, " mm"),)
    thickness_mm = 200.0

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.points: list = []

    def extra_segments(self):
        return [(self.points[0], self.points[0])] if len(self.points) >= 3 else []

    def hover(self, point, shift):
        if point is None:
            return
        snap = self.snap(point, shift, self.points[-1] if self.points else None)
        chain = self.points + [snap.point]
        lines = list(zip(chain, chain[1:]))
        if len(chain) >= 3:
            lines.append((chain[-1], chain[0]))
        self.draw(lines, snap)

    def click(self, point, shift):
        at = self.snap(point, shift, self.points[-1] if self.points else None).point
        if len(self.points) >= 3 and at == self.points[0]:
            self.finish()
        elif not self.points or math.dist(self.points[-1], at) > 0.01:
            self.points.append(at)

    def finish(self):
        points, self.points = self.points, []
        self.draw([])
        if len(points) < 3:
            return
        slab = Slab(name=self.next_name("S"), boundary=points, thickness=self.thickness_mm / 1000,
                    storey_id=self.w.active_storey_id)
        self.w._execute(AddElements([slab], label="Draw slab", validate=True))

    def cancel(self):
        self.points = []
        super().cancel()


class DrawStairTool(PlanTool):
    title = "Stair"
    hint = "Click the foot of the stair, then the top of the flight. Shift = straight."
    options = (Option("width_mm", "Width", 300, 6000, 50, 0, " mm"), Option("risers", "Risers", 2, 100, 1, 0, ""),
               Option("height_m", "Rise", 0.1, 50, 0.1, 2, " m"))
    width_mm = 1000.0
    risers = 18.0

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.height_m = window.active_storey().height
        self.start = None

    def _outline(self, a, b) -> list:
        length = math.dist(a, b)
        if length == 0:
            return []
        nx, ny = -(b[1] - a[1]) / length * self.width_mm / 2000, (b[0] - a[0]) / length * self.width_mm / 2000
        ring = [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]
        return list(zip(ring, ring[1:] + ring[:1]))

    def hover(self, point, shift):
        if point is None:
            return
        snap = self.snap(point, shift, self.start)
        self.draw(self._outline(self.start, snap.point) if self.start else [], snap)
        if self.start:
            going = math.dist(self.start, snap.point) / max(self.risers, 1)
            self.w.statusBar().showMessage(f"Going {going * 1000:.0f} mm, riser {self.height_m / max(self.risers, 1) * 1000:.0f} mm")

    def click(self, point, shift):
        at = self.snap(point, shift, self.start).point
        if self.start is None:
            self.start = at
            return
        stair = Stair(name=self.next_name("ST"), start=self.start, end=at, width=self.width_mm / 1000,
                      risers=int(self.risers), height=self.height_m, storey_id=self.w.active_storey_id)
        self.start = None
        self.draw([])
        self.w._execute(AddElements([stair], label="Draw stair", validate=True))

    def cancel(self):
        self.start = None
        super().cancel()


class _BaseToTargetTool(PlanTool):
    """Pick a point on the selection, then where that point should go."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.base = None

    def hover(self, point, shift):
        if point is None:
            return
        snap = self.snap(point, shift, self.base)
        self.draw([(self.base, snap.point)] if self.base else [], snap)
        if self.base:
            dx, dy = snap.point[0] - self.base[0], snap.point[1] - self.base[1]
            self.w.statusBar().showMessage(f"ΔX {dx:+.3f}  ΔY {dy:+.3f}  distance {math.hypot(dx, dy):.3f} m")

    def click(self, point, shift):
        at = self.snap(point, shift, self.base).point
        if self.base is None:
            self.base = at
            return
        self.apply(at[0] - self.base[0], at[1] - self.base[1])

    def apply(self, dx: float, dy: float) -> None: ...

    def cancel(self):
        self.base = None
        super().cancel()


class MoveTool(_BaseToTargetTool):
    title = "Move"
    hint = "Click a point to move from, then where it should go. Shift = straight."

    def apply(self, dx, dy):
        self.base = None
        self.draw([])
        self.w._execute(MoveElements(self.w.selection, dx, dy))
        self.w.cancel_tool()


class CopyTool(_BaseToTargetTool):
    title = "Copy"
    hint = "Click a point to copy from, then each place a copy should go. Enter or Esc to stop."

    def apply(self, dx, dy):
        command = CopyElements(self.w.selection, [(dx, dy)])
        self.w._execute(command)

    def finish(self):
        self.w.cancel_tool()


class BoxSelectTool(PlanTool):
    title = "Box select"
    hint = "Click two corners. Left to right takes what is fully inside; right to left takes what it touches."

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.first = None

    def _ring(self, a, b) -> list:
        ring = [a, (b[0], a[1]), b, (a[0], b[1])]
        return list(zip(ring, ring[1:] + ring[:1]))

    def hover(self, point, shift):
        if point is not None and self.first is not None:
            self.draw(self._ring(self.first, point[:2]))

    def click(self, point, shift):
        if self.first is None:
            self.first = point[:2]
            return
        boxes = self.w.visible_element_boxes()
        ids = ids_in_box(boxes, self.first, point[:2])
        self.first = None
        self.draw([])
        self.w.cancel_tool()
        self.w.select(ids)
        self.w.statusBar().showMessage(f"Selected {len(ids)} element(s)")

    def cancel(self):
        self.first = None
        super().cancel()


class PlaceFixtureTool(Tool):
    def __init__(self, window: MainWindow, family_key: str, rotation: float, system_id: str | None) -> None:
        super().__init__(window)
        self.family = FAMILIES[family_key]
        self.rotation = rotation
        self.system_id = system_id
        self.hint = f"Click to place {self.family.name} (rotation {rotation:g}°). Esc to stop."

    def hover(self, point, shift):
        if point is None:
            return
        x, y, z = snap_to_grid(point)
        h = self.family.size[0] / 2
        a = math.radians(self.rotation)
        dx, dy = h * math.cos(a), h * math.sin(a)
        self._preview([(x - dx, y - dy, z + 0.01), (x + dx, y + dy, z + 0.01)])

    def click(self, point, shift):
        x, y, _ = snap_to_grid(point)
        storey = self.w.active_storey()
        count = sum(1 for f in self.w.project.elements_of(Fixture) if f.family == self.family.key) + 1
        fixture = Fixture(
            name=f"{self.family.key.upper()}-{count}",
            family=self.family.key,
            position=(round(x, 4), round(y, 4), self.family.mount_height),
            rotation=self.rotation,
            storey_id=storey.id,
            system_id=self.system_id,
        )
        self.w._execute(AddElements([fixture], label=f"Place {self.family.name}"))


class DrawRunTool(Tool):
    """Draw a pipe, conduit or cable tray as a polyline on the work plane (level + offset).

    Snaps to the grid, to fixture connection points and to existing run vertices;
    Shift constrains to X/Y. Drainage runs are graded to the code minimum slope.
    """

    def __init__(self, window: MainWindow, kind: str, size_mm: float, offset: float, system_id: str | None,
                 tray_depth_mm: float = 100) -> None:
        super().__init__(window)
        self.kind = kind  # "pipe" | "conduit" | "tray"
        self.size = size_mm / 1000
        self.offset = offset
        self.system_id = system_id
        self.tray_depth = tray_depth_mm / 1000
        self.points: list[Point3] = []
        self.hint = f"Click points for the {kind} (Shift = orthogonal). Enter or right-click to finish, Esc to cancel."

    def work_height(self) -> float:
        return self.w.active_elevation() + self.offset

    def _snap(self, point: Point3, shift: bool) -> tuple[Point3, bool]:
        """Returns (point, snapped_to_port)."""
        x, y, z = point
        best, best_d, is_port = None, SNAP_RADIUS, False
        for target, port in self._targets():
            d = math.hypot(target[0] - x, target[1] - y)
            if d < best_d:
                best, best_d, is_port = target, d, port
        if best is not None:
            return best, is_port
        x, y, z = snap_to_grid(point)
        if shift and self.points:
            lx, ly, _ = self.points[-1]
            if abs(x - lx) >= abs(y - ly):
                y = ly
            else:
                x = lx
        return (x, y, z), False

    def _targets(self):
        p = self.w.project
        discipline = "electrical" if self.kind in ("conduit", "tray") else "plumbing"
        for f in p.elements_of(Fixture):
            family = FAMILIES.get(f.family)
            if family and family.discipline.value == discipline:
                yield connection_point(f, p.storey_elevation(f)), True
        kind = CableCarrierSegment if discipline == "electrical" else PipeSegment
        for run in p.elements_of(kind):
            base = p.storey_elevation(run)
            for vx, vy, vz in (run.path[0], run.path[-1]):
                yield (vx, vy, vz + base), False

    def hover(self, point, shift):
        if point is None:
            return
        snapped, _ = self._snap(point, shift)
        self._preview(self.points + [snapped])

    def click(self, point, shift):
        snapped, is_port = self._snap(point, shift)
        h = self.work_height()
        if is_port and abs(snapped[2] - h) > 1e-6:
            # Connect at the port's height with a vertical leg to/from the work plane.
            on_plane = (snapped[0], snapped[1], h)
            self.points += [snapped, on_plane] if not self.points else [on_plane, snapped]
        else:
            self.points.append((snapped[0], snapped[1], h if not is_port else snapped[2]))
        if is_port and len(self.points) > 2:
            self.finish()
            return
        self._preview(self.points)

    def finish(self):
        points = [p for i, p in enumerate(self.points) if i == 0 or math.dist(p, self.points[i - 1]) > 1e-6]
        self.points = []
        self.cancel()
        if len(points) < 2:
            return
        storey = self.w.active_storey()
        path = [(round(x, 4), round(y, 4), round(z - storey.elevation, 4)) for x, y, z in points]
        system = self.w.project.systems.get(self.system_id) if self.system_id else None
        common = {"storey_id": storey.id, "system_id": self.system_id, "path": path}
        if self.kind == "pipe":
            if system is not None and system.system_type in DRAINAGE_TYPES:
                path = apply_slope(path, min_drain_slope(self.size * 1000))
                common["path"] = path
            count = len(self.w.project.elements_of(PipeSegment)) + 1
            element = PipeSegment(name=f"P-{count}", diameter=self.size, **common)
        else:
            count = len(self.w.project.elements_of(CableCarrierSegment)) + 1
            carrier = "CONDUITSEGMENT" if self.kind == "conduit" else "CABLETRAYSEGMENT"
            depth = self.size if self.kind == "conduit" else self.tray_depth
            prefix = "CD" if self.kind == "conduit" else "CT"
            element = CableCarrierSegment(name=f"{prefix}-{count}", carrier_type=carrier, width=self.size,
                                          depth=depth, **common)
        self.w._execute(AddElements([element], label=f"Draw {self.kind}"))

    def cancel(self):
        self.points = []
        super().cancel()


class MeasureTool(Tool):
    needs_surface = True
    hint = "Click two points on the model to measure. Esc to stop."

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.first: Point3 | None = None

    def click(self, point, shift):
        if self.first is None:
            self.first = point
            self._preview([point, point])
            self.w.statusBar().showMessage("Click the second point")
            return
        a, b = self.first, point
        self._preview([a, b])
        dx, dy, dz = (b[i] - a[i] for i in range(3))
        self.w.statusBar().showMessage(
            f"Distance {math.dist(a, b):.3f} m   (ΔX {dx:+.3f}, ΔY {dy:+.3f}, ΔZ {dz:+.3f}, plan {math.hypot(dx, dy):.3f})"
        )
        self.first = None


DEFAULT_SYSTEM_COLORS = {
    SystemType.COLD_WATER: (0.2, 0.45, 0.95),
    SystemType.HOT_WATER: (0.9, 0.25, 0.2),
    SystemType.SANITARY: (0.55, 0.35, 0.15),
    SystemType.VENT: (0.35, 0.7, 0.35),
    SystemType.STORMWATER: (0.1, 0.6, 0.6),
    SystemType.POWER: (0.95, 0.55, 0.1),
    SystemType.LIGHTING: (0.95, 0.85, 0.2),
    SystemType.DATA: (0.6, 0.35, 0.85),
    **{system_type: colour for system_type, _, colour in AIR_SYSTEMS.values()},
}
