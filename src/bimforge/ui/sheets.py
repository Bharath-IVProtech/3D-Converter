"""Printable drawing sheets: an A3 PDF with a floor plan per level, then the schedules."""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

from PySide6.QtCore import QMarginsF, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPageLayout, QPageSize, QPainter, QPainterPath, QPdfWriter, QPen, QPolygonF
from shapely import MultiPoint
from shapely.geometry import Polygon

from bimforge.coordination.schedules import Schedule, build_schedules
from bimforge.core.model import (
    Column,
    Door,
    DuctSegment,
    Fixture,
    ImportedElement,
    MepSegment,
    Project,
    Slab,
    Space,
    Stair,
    Storey,
    Wall,
    Window,
)

RESOLUTION = 300  # dpi
K = RESOLUTION / 25.4  # device pixels per sheet millimetre
SHEET_W, SHEET_H = 420.0, 297.0  # A3 landscape, mm
MARGIN = 10.0
TITLE_W = 72.0
DIMENSION_SPACE = 16.0  # room around the plan for dimensions, mm
SCALES = (20, 50, 100, 150, 200, 250, 500, 1000, 2000, 5000)
SCHEDULES_ON_SHEETS = ("Doors", "Windows", "Rooms", "Stairs", "Columns")
ROW_H = 5.5  # schedule row, mm
ROOM_LINE = 4.0  # room name line, mm
MIN_LABEL_WIDTH = 18.0  # mm: a name in a narrower room may overlap its walls

INK = QColor(25, 28, 33)
WALL_FILL = QColor(70, 74, 80)
LIGHT = QColor(130, 135, 142)
ACCENT = QColor(37, 99, 235)


def choose_scale(width_m: float, height_m: float, paper_w_mm: float, paper_h_mm: float) -> int:
    """The largest standard scale (1:N) at which the plan fits the paper."""
    for n in SCALES:
        if width_m * 1000 / n <= paper_w_mm and height_m * 1000 / n <= paper_h_mm:
            return n
    return SCALES[-1]


def _plan_points(element) -> list[tuple[float, float]]:
    if isinstance(element, Wall):
        return [element.start, element.end]
    if isinstance(element, (Slab, Space)):
        return list(element.boundary)
    if isinstance(element, Stair):
        points = []
        for a, b, width, _ in element.flights:
            nx, ny = _left(a, b, width / 2)
            points += [(a[0] + nx, a[1] + ny), (a[0] - nx, a[1] - ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny)]
        return points
    if isinstance(element, Column):
        return [element.position]
    if isinstance(element, MepSegment):
        return [(p[0], p[1]) for p in element.path]
    if isinstance(element, Fixture):
        return [element.position[:2]]
    if isinstance(element, ImportedElement):
        return [(p[0], p[1]) for p in element.vertices]
    return []


def _left(a, b, distance: float) -> tuple[float, float]:
    length = math.dist(a, b) or 1.0
    return (-(b[1] - a[1]) / length * distance, (b[0] - a[0]) / length * distance)


class _Sheet:
    """Draws in sheet millimetres; `plan` maps metres in the model to the sheet."""

    def __init__(self, painter: QPainter) -> None:
        self.p = painter
        self.ox = self.oy = 0.0
        self.mm_per_m = 10.0

    def set_plan(self, min_x: float, min_y: float, height_m: float, left: float, top: float, scale: int) -> None:
        self.mm_per_m = 1000 / scale
        self.ox, self.oy = left - min_x * self.mm_per_m, top + (height_m + min_y) * self.mm_per_m

    def plan(self, point) -> QPointF:
        return QPointF((self.ox + point[0] * self.mm_per_m) * K, (self.oy - point[1] * self.mm_per_m) * K)

    def pen(self, color: QColor = INK, width: float = 0.25, style=Qt.PenStyle.SolidLine) -> None:
        pen = QPen(color, width * K, style)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        self.p.setPen(pen)
        self.p.setBrush(Qt.BrushStyle.NoBrush)

    def font(self, points: float, bold: bool = False) -> None:
        font = QFont("Segoe UI, Arial, sans-serif")
        font.setPointSizeF(points)
        font.setBold(bold)
        self.p.setFont(font)

    def sheet_line(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.p.drawLine(QPointF(x0 * K, y0 * K), QPointF(x1 * K, y1 * K))

    def text(self, x: float, y: float, w: float, h: float, value: str, align=Qt.AlignmentFlag.AlignLeft) -> None:
        self.p.drawText(QRectF(x * K, y * K, w * K, h * K), int(align | Qt.AlignmentFlag.AlignVCenter), value)

    def plan_line(self, a, b) -> None:
        self.p.drawLine(self.plan(a), self.plan(b))

    def plan_poly(self, points, fill: QColor | None = None) -> None:
        polygon = QPolygonF([self.plan(q) for q in points])
        self.p.setBrush(QBrush(fill) if fill is not None else Qt.BrushStyle.NoBrush)
        self.p.drawPolygon(polygon)
        self.p.setBrush(Qt.BrushStyle.NoBrush)


def _corner_extensions(wall: Wall, walls: list[Wall]) -> tuple[float, float]:
    """How far to run each end past its centre-line point, so outside corners are filled in."""
    def extension(end) -> float:
        others = [w for w in walls if w.id != wall.id and (math.dist(end, w.start) < 1e-3 or math.dist(end, w.end) < 1e-3)]
        return max((w.thickness / 2 for w in others), default=0.0)

    return extension(wall.start), extension(wall.end)


def _wall_rect(wall: Wall, s0: float, s1: float, half: float | None = None):
    half = wall.thickness / 2 if half is None else half
    a, b = wall.point_at(s0), wall.point_at(s1)
    nx, ny = wall.normal[0] * half, wall.normal[1] * half
    return [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]


def _draw_wall(sheet: _Sheet, wall: Wall, project: Project, walls: list[Wall]) -> None:
    openings = sorted(project.hosted_openings(wall.id), key=lambda o: o.span[0])
    before, after = _corner_extensions(wall, walls)
    position = -before
    sheet.p.setPen(Qt.PenStyle.NoPen)
    for opening in openings:
        s0, s1 = opening.span
        if s0 > position:
            sheet.plan_poly(_wall_rect(wall, position, s0), WALL_FILL)
        position = max(position, s1)
    if position < wall.length + after:
        sheet.plan_poly(_wall_rect(wall, position, wall.length + after), WALL_FILL)
    for opening in openings:
        _draw_opening(sheet, wall, opening)


def _draw_opening(sheet: _Sheet, wall: Wall, opening) -> None:
    s0, s1 = opening.span
    half = wall.thickness / 2
    if isinstance(opening, Door):
        sheet.pen(INK, 0.18)
        hinge = wall.point_at(s0)
        nx, ny = wall.normal
        tip = (hinge[0] + nx * opening.width, hinge[1] + ny * opening.width)
        sheet.plan_line(hinge, tip)
        centre, radius = sheet.plan(hinge), opening.width * sheet.mm_per_m * K
        start = math.degrees(math.atan2(wall.direction[1], wall.direction[0]))
        box = QRectF(centre.x() - radius, centre.y() - radius, 2 * radius, 2 * radius)
        path = QPainterPath()
        path.arcMoveTo(box, start)
        path.arcTo(box, start, 90)
        sheet.p.drawPath(path)
    elif isinstance(opening, Window):
        sheet.pen(INK, 0.18)
        sheet.plan_poly(_wall_rect(wall, s0, s1))
        sheet.pen(ACCENT, 0.25)
        sheet.plan_line(wall.point_at(s0), wall.point_at(s1))
    else:  # a plain opening or louvre, shown above the cut line
        sheet.pen(INK, 0.15, Qt.PenStyle.DashLine)
        sheet.plan_poly(_wall_rect(wall, s0, s1))
    sheet.pen(INK)
    sheet.font(5)
    if opening.name and isinstance(opening, (Door, Window)):  # duct holes and louvres stay untagged, to keep it legible
        at = wall.point_at(opening.offset)
        nx, ny = wall.normal
        label = sheet.plan((at[0] - nx * (half + 0.22), at[1] - ny * (half + 0.22)))
        sheet.text(label.x() / K - 12, label.y() / K - 2, 24, 4, opening.name, Qt.AlignmentFlag.AlignHCenter)


def _draw_stair(sheet: _Sheet, stair: Stair) -> None:
    sheet.pen(INK, 0.15)
    for a, b, width, risers in stair.flights:
        nx, ny = _left(a, b, width / 2)
        sheet.plan_poly([(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)])
        for i in range(1, risers):
            t = i / risers
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            sheet.plan_line((x + nx, y + ny), (x - nx, y - ny))
        sheet.pen(ACCENT, 0.2)
        sheet.plan_line(a, b)  # walking line, arrow towards the top of the flight
        back = _left(b, a, 0.12)
        length = math.dist(a, b) or 1.0
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        for side in (1, -1):
            sheet.plan_line(b, (b[0] - ux * 0.25 + back[0] * side, b[1] - uy * 0.25 + back[1] * side))
        sheet.pen(INK, 0.15)
    sheet.font(5)
    at = sheet.plan(stair.start)
    sheet.text(at.x() / K - 10, at.y() / K - 2, 20, 4, "UP", Qt.AlignmentFlag.AlignHCenter)


def _wrapped(text: str, metrics, width: float) -> list[str]:
    """Word-wrap to a width in device pixels. A single long word stays whole."""
    lines: list[str] = []
    for word in text.split():
        if lines and metrics.horizontalAdvance(f"{lines[-1]} {word}") <= width:
            lines[-1] += f" {word}"
        else:
            lines.append(word)
    return lines or [""]


def _draw_room_label(sheet: _Sheet, room: Space) -> None:
    if len(room.boundary) < 3:
        return
    polygon = Polygon(room.boundary)
    if not polygon.is_valid:
        return
    at = sheet.plan(polygon.representative_point().coords[0])
    x, y = at.x() / K, at.y() / K
    sheet.font(7, bold=True)
    metrics = sheet.p.fontMetrics()
    xmin, _, xmax, _ = polygon.bounds
    room_width = (xmax - xmin) * sheet.mm_per_m
    lines = _wrapped(room.name or "Room", metrics, max(room_width - 3, MIN_LABEL_WIDTH) * K)
    half = max(max(metrics.horizontalAdvance(line) for line in lines) / K / 2 + 1, 9)
    top = y - ROOM_LINE * len(lines)
    sheet.p.fillRect(QRectF((x - half) * K, top * K, 2 * half * K, (ROOM_LINE * len(lines) + 4) * K),
                     QColor(255, 255, 255, 215))  # legible over anything
    sheet.pen(INK)
    for i, line in enumerate(lines):
        sheet.text(x - 40, top + i * ROOM_LINE, 80, ROOM_LINE, line, Qt.AlignmentFlag.AlignHCenter)
    sheet.font(6)
    sheet.text(x - 25, y, 50, 4, f"{room.area:.2f} m²", Qt.AlignmentFlag.AlignHCenter)


def _draw_dimension(sheet: _Sheet, x0: float, y0: float, x1: float, y1: float, value_m: float) -> None:
    """A dimension line between two sheet points, with ticks and its length."""
    sheet.pen(INK, 0.13)
    sheet.sheet_line(x0, y0, x1, y1)
    for x, y in ((x0, y0), (x1, y1)):
        sheet.sheet_line(x - 1, y + 1, x + 1, y - 1)
    sheet.font(6)
    if y0 == y1:
        sheet.text((x0 + x1) / 2 - 15, y0 - 4.5, 30, 4, f"{value_m:.3f}", Qt.AlignmentFlag.AlignHCenter)
        return
    sheet.p.save()
    sheet.p.translate(x0 * K - 1.2 * K, (y0 + y1) / 2 * K)
    sheet.p.rotate(-90)
    sheet.text(-15, -3.5, 30, 4, f"{value_m:.3f}", Qt.AlignmentFlag.AlignHCenter)
    sheet.p.restore()


def _frame(sheet: _Sheet) -> None:
    sheet.pen(INK, 0.5)
    sheet.p.drawRect(QRectF(MARGIN * K, MARGIN * K, (SHEET_W - 2 * MARGIN) * K, (SHEET_H - 2 * MARGIN) * K))
    x = SHEET_W - MARGIN - TITLE_W
    sheet.sheet_line(x, MARGIN, x, SHEET_H - MARGIN)


def _title_block(sheet: _Sheet, project: Project, title: str, scale: str, number: str, date: dt.date) -> None:
    x = SHEET_W - MARGIN - TITLE_W
    rows = [("Project", project.name or "Untitled", 14), ("Drawing", title, 14), ("Scale", scale, 10),
            ("Date", date.isoformat(), 10), ("Sheet", number, 14)]
    y = SHEET_H - MARGIN - sum(h for *_, h in rows)
    for label, value, height in rows:
        sheet.pen(INK, 0.25)
        sheet.sheet_line(x, y, x + TITLE_W, y)
        sheet.pen(LIGHT)
        sheet.font(6)
        sheet.text(x + 3, y + 0.8, TITLE_W - 6, 3.5, label)
        sheet.pen(INK)
        sheet.font(10 if height > 10 else 8, bold=height > 10)
        sheet.p.drawText(QRectF((x + 3) * K, (y + 4.2) * K, (TITLE_W - 6) * K, (height - 4.6) * K),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap), value)
        y += height


def _north_and_scale_bar(sheet: _Sheet, scale: int) -> None:
    x = SHEET_W - MARGIN - TITLE_W + 6
    y = SHEET_H - MARGIN - 62 - 28
    sheet.pen(INK, 0.3)
    centre = x + 8
    sheet.p.drawEllipse(QPointF(centre * K, (y + 8) * K), 7 * K, 7 * K)
    sheet.p.setBrush(QBrush(INK))
    sheet.p.drawPolygon(QPolygonF([QPointF(centre * K, (y + 1) * K), QPointF((centre + 2.6) * K, (y + 13) * K),
                                   QPointF((centre - 2.6) * K, (y + 13) * K)]))
    sheet.p.setBrush(Qt.BrushStyle.NoBrush)
    sheet.font(7, bold=True)
    sheet.text(centre - 6, y - 4.5, 12, 4, "N", Qt.AlignmentFlag.AlignHCenter)
    # Scale bar: a round number of metres that is 20 to 40 mm long on paper.
    metres = next((m for m in (1, 2, 5, 10, 20, 50, 100, 200) if m * 1000 / scale >= 20), 500)
    length = metres * 1000 / scale
    bx, by = x + 22, y + 10
    if length > TITLE_W - 30:
        return
    sheet.p.setPen(Qt.PenStyle.NoPen)
    for i in range(4):
        sheet.p.setBrush(QBrush(INK if i % 2 == 0 else QColor(255, 255, 255)))
        sheet.p.drawRect(QRectF((bx + length * i / 4) * K, by * K, (length / 4) * K, 1.4 * K))
    sheet.p.setBrush(Qt.BrushStyle.NoBrush)
    sheet.pen(INK, 0.15)
    sheet.p.drawRect(QRectF(bx * K, by * K, length * K, 1.4 * K))
    sheet.font(6)
    sheet.text(bx - 5, by + 1.6, 10, 4, "0", Qt.AlignmentFlag.AlignHCenter)
    sheet.text(bx + length - 8, by + 1.6, 16, 4, f"{metres} m", Qt.AlignmentFlag.AlignHCenter)


def _draw_plan_page(sheet: _Sheet, project: Project, storey: Storey, elements: list, number: str, date: dt.date) -> None:
    points = [q for e in elements for q in _plan_points(e)]
    xs, ys = [q[0] for q in points], [q[1] for q in points]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    width, height = max(max_x - min_x, 0.1), max(max_y - min_y, 0.1)
    paper_w = SHEET_W - 2 * MARGIN - TITLE_W - 2 * DIMENSION_SPACE
    paper_h = SHEET_H - 2 * MARGIN - 2 * DIMENSION_SPACE
    scale = choose_scale(width, height, paper_w, paper_h)
    drawn_w, drawn_h = width * 1000 / scale, height * 1000 / scale
    left = MARGIN + DIMENSION_SPACE + (paper_w - drawn_w) / 2
    top = MARGIN + DIMENSION_SPACE + (paper_h - drawn_h) / 2
    sheet.set_plan(min_x, min_y, height, left, top, scale)
    _frame(sheet)

    for slab in (e for e in elements if isinstance(e, Slab)):
        sheet.pen(LIGHT, 0.13, Qt.PenStyle.DashLine)
        sheet.plan_poly(slab.boundary)
    for imported in (e for e in elements if isinstance(e, ImportedElement) and e.vertices):
        hull = MultiPoint([(p[0], p[1]) for p in imported.vertices]).convex_hull
        if hull.geom_type == "Polygon":
            sheet.pen(LIGHT, 0.15)
            sheet.plan_poly(list(hull.exterior.coords)[:-1])
    for run in (e for e in elements if isinstance(e, MepSegment)):
        system = project.systems.get(run.system_id) if run.system_id else None
        color = QColor.fromRgbF(*system.color) if system else LIGHT
        sheet.pen(color, 0.25, Qt.PenStyle.DashLine)
        if isinstance(run, DuctSegment):
            sheet.pen(color, max(run.width * sheet.mm_per_m, 0.25))
            color.setAlphaF(0.3)
            sheet.pen(color, max(run.width * sheet.mm_per_m, 0.25))
        for a, b in zip(run.path, run.path[1:]):
            sheet.plan_line(a[:2], b[:2])
    for stair in (e for e in elements if isinstance(e, Stair)):
        _draw_stair(sheet, stair)
    walls = [e for e in elements if isinstance(e, Wall)]
    for wall in walls:
        _draw_wall(sheet, wall, project, walls)
    for column in (e for e in elements if isinstance(e, Column)):
        a = math.radians(column.rotation)
        ux, uy = math.cos(a) * column.width / 2, math.sin(a) * column.width / 2
        vx, vy = -math.sin(a) * column.depth / 2, math.cos(a) * column.depth / 2
        cx, cy = column.position
        sheet.p.setPen(Qt.PenStyle.NoPen)
        sheet.plan_poly([(cx + ux + vx, cy + uy + vy), (cx - ux + vx, cy - uy + vy),
                         (cx - ux - vx, cy - uy - vy), (cx + ux - vx, cy + uy - vy)], INK)
    for fixture in (e for e in elements if isinstance(e, Fixture)):
        sheet.pen(INK, 0.15)
        centre = sheet.plan(fixture.position[:2])
        sheet.p.drawRect(QRectF(centre.x() - 0.8 * K, centre.y() - 0.8 * K, 1.6 * K, 1.6 * K))
    for room in (e for e in elements if isinstance(e, Space)):
        _draw_room_label(sheet, room)

    bottom = top + drawn_h + 8
    _draw_dimension(sheet, left, bottom, left + drawn_w, bottom, width)
    _draw_dimension(sheet, left - 8, top, left - 8, top + drawn_h, height)
    _north_and_scale_bar(sheet, scale)
    _title_block(sheet, project, f"{storey.name} floor plan ({storey.elevation:+.2f} m)", f"1 : {scale} at A3", number, date)


def _draw_schedule_pages(sheet: _Sheet, writer: QPdfWriter, project: Project, schedules: list[Schedule], first: int,
                         date: dt.date) -> int:
    """Lays the tables out in two columns, continuing onto further sheets. Returns sheets used."""
    column_w = (SHEET_W - 2 * MARGIN - TITLE_W - 18) / 2
    top, bottom = MARGIN + 8, SHEET_H - MARGIN - 8

    def start_page(number: int) -> None:
        _frame(sheet)
        _title_block(sheet, project, "Schedules", "Not applicable", f"A-{number:02d}", date)

    used = 1
    start_page(first)
    column = 0
    y = top

    def advance() -> None:
        nonlocal column, y, used
        column += 1
        y = top
        if column == 2:
            column = 0
            writer.newPage()
            used += 1
            start_page(first + used - 1)

    for schedule in schedules:
        if y + 3 * ROW_H + 8 > bottom:
            advance()
        share = column_w / (len(schedule.headers) + 1)  # the first column holds names, so it gets double
        starts = [0.0] + [share * (c + 2) for c in range(len(schedule.headers) - 1)]
        widths = [share * 2] + [share] * (len(schedule.headers) - 1)
        row_index = 0
        title_row = True
        while row_index < len(schedule.rows) or title_row:
            x = MARGIN + 6 + column * (column_w + 6)
            if title_row:
                sheet.pen(INK)
                sheet.font(9, bold=True)
                sheet.text(x, y, column_w, 6, f"{schedule.name}  ({schedule.totals})")
                y += 6.5
                sheet.font(6, bold=True)
                for c, header in enumerate(schedule.headers):
                    sheet.text(x + starts[c] + 0.8, y, widths[c] - 1.6, ROW_H, header)
                y += ROW_H
                sheet.pen(INK, 0.3)
                sheet.sheet_line(x, y, x + column_w, y)
                title_row = False
                continue
            sheet.pen(INK)
            sheet.font(6)
            for c, value in enumerate(schedule.rows[row_index]):
                clipped = sheet.p.fontMetrics().elidedText(value, Qt.TextElideMode.ElideRight, int((widths[c] - 1.6) * K))
                sheet.text(x + starts[c] + 0.8, y, widths[c] - 1.6, ROW_H, clipped)
            y += ROW_H
            sheet.pen(LIGHT, 0.1)
            sheet.sheet_line(x, y, x + column_w, y)
            row_index += 1
            if y + ROW_H > bottom and row_index < len(schedule.rows):
                advance()
                title_row = True
        y += 8
    return used


def export_sheets(project: Project, path: Path, date: dt.date | None = None) -> int:
    """Write the sheet set to a PDF and return how many sheets it has; 0 if there is nothing to draw."""
    date = date or dt.date.today()
    levels = []
    for storey in sorted(project.storeys, key=lambda s: s.elevation):
        elements = [e for e in project.elements() if e.storey_id == storey.id and _plan_points(e)]
        if elements:
            levels.append((storey, elements))
    schedules = [s for s in build_schedules(project) if s.name in SCHEDULES_ON_SHEETS and s.rows]
    if not levels and not schedules:
        return 0
    writer = QPdfWriter(str(path))
    writer.setResolution(RESOLUTION)
    writer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A3), QPageLayout.Orientation.Landscape,
                                     QMarginsF(0, 0, 0, 0)))
    writer.setTitle(f"{project.name} - drawings")
    writer.setCreator("BIMForge")
    painter = QPainter(writer)
    if not painter.isActive():
        raise OSError(f"Could not write {path}. If it is open in another program, close it and try again.")
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    sheet = _Sheet(painter)
    count = 0
    for storey, elements in levels:
        if count:
            writer.newPage()
        count += 1
        _draw_plan_page(sheet, project, storey, elements, f"A-{count:02d}", date)
    if schedules:
        if count:
            writer.newPage()
        count += _draw_schedule_pages(sheet, writer, project, schedules, count + 1, date)
    painter.end()
    return count
