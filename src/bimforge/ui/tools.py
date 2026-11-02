"""Interactive viewport tools: place fixtures, draw MEP runs, measure.

The main window forwards clicks/moves (already converted to world points) to
the active tool. Tools build elements and hand them to the window as commands,
so everything they create is undoable and validated like any other edit.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from bimforge.app.commands import AddElements
from bimforge.conversion.ducts import AIR_SYSTEMS
from bimforge.core.model import CableCarrierSegment, Fixture, PipeSegment, Point3, SystemType
from bimforge.disciplines.catalog import FAMILIES, connection_point
from bimforge.disciplines.plumbing import DRAINAGE_TYPES, apply_slope, min_drain_slope
from bimforge.plugins.sdk import RenderStyle

if TYPE_CHECKING:
    from bimforge.ui.main_window import MainWindow

PREVIEW = "tool:preview"
GRID = 0.05  # m
SNAP_RADIUS = 0.25  # m
PREVIEW_STYLE = RenderStyle(color=(0.95, 0.2, 0.6), category="tool")


def snap_to_grid(p: Point3) -> Point3:
    return (round(p[0] / GRID) * GRID, round(p[1] / GRID) * GRID, p[2])


class Tool:
    needs_surface = False  # True: points come from surfaces; False: from the active work plane
    hint = ""

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
