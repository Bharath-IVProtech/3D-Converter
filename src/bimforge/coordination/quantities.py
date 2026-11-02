"""Quantity takeoff (bill of quantities) and CSV/XLSX export."""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import astuple, dataclass, fields
from pathlib import Path

from bimforge.core.model import (
    CableCarrierSegment,
    Column,
    Door,
    DuctSegment,
    Element,
    Fixture,
    ImportedElement,
    Opening,
    PipeSegment,
    Project,
    Slab,
    Space,
    Stair,
    Wall,
    Window,
)
from bimforge.disciplines.catalog import family_of
from bimforge.disciplines.plumbing import path_length


@dataclass
class QuantityRow:
    category: str
    item: str
    level: str
    unit: str
    quantity: float
    count: int


def _area(points) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]))) / 2


def take_off(project: Project) -> list[QuantityRow]:
    level = {s.id: s.name for s in project.storeys}
    groups: dict[tuple[str, str, str, str], list[float]] = defaultdict(lambda: [0.0, 0])

    def add(category: str, item: str, element: Element, unit: str, quantity: float) -> None:
        key = (category, item, level.get(element.storey_id, ""), unit)
        groups[key][0] += quantity
        groups[key][1] += 1

    for wall in project.elements_of(Wall):
        openings = project.hosted_openings(wall.id)
        gross = wall.length * wall.height
        net = gross - sum(o.width * o.height for o in openings)
        item = f"Wall {wall.thickness * 1000:.0f} mm"
        add("Walls", f"{item} - length", wall, "m", wall.length)
        add("Walls", f"{item} - net area", wall, "m²", net)
        add("Walls", f"{item} - volume", wall, "m³", net * wall.thickness)
    for opening in project.elements_of(Door) + project.elements_of(Window):
        add(f"{type(opening).__name__}s", f"{opening.width * 1000:.0f} × {opening.height * 1000:.0f} mm",
            opening, "nr", 1)
    for opening in project.elements_of(Opening):
        if type(opening) is Opening:  # plain holes: duct openings and louvres
            add("Wall openings", f"{opening.name.split(' ')[0]} {opening.width * 1000:.0f} x "
                                 f"{opening.height * 1000:.0f} mm", opening, "nr", 1)
    for slab in project.elements_of(Slab):
        area = _area(list(slab.boundary))
        add("Slabs", f"Slab {slab.thickness * 1000:.0f} mm - area", slab, "m²", area)
        add("Slabs", f"Slab {slab.thickness * 1000:.0f} mm - volume", slab, "m³", area * slab.thickness)
    for stair in project.elements_of(Stair):
        flights = len(stair.flights)
        shape = f"{flights} flights, " if flights > 1 else ""
        add("Stairs", f"Stair {stair.width * 1000:.0f} mm wide, {shape}{stair.total_risers} risers", stair, "nr", 1)
    for column in project.elements_of(Column):
        item = f"Column {column.width * 1000:.0f} x {column.depth * 1000:.0f} mm"
        add("Columns", f"{item} - volume", column, "m³", column.width * column.depth * column.height)
    for space in project.elements_of(Space):
        add("Rooms", space.name or "Room", space, "m²", space.area)
    for pipe in project.elements_of(PipeSegment):
        system = project.systems.get(pipe.system_id) if pipe.system_id else None
        kind = system.system_type.value.replace("_", " ") if system else "unassigned"
        add("Pipes", f"{kind} Ø{pipe.diameter * 1000:.0f} mm", pipe, "m", path_length(pipe.path))
    for carrier in project.elements_of(CableCarrierSegment):
        if carrier.carrier_type == "CONDUITSEGMENT":
            item = f"Conduit Ø{carrier.width * 1000:.0f} mm"
        else:
            item = f"Cable tray {carrier.width * 1000:.0f} × {carrier.depth * 1000:.0f} mm"
        add("Electrical containment", item, carrier, "m", path_length(carrier.path))
    for duct in project.elements_of(DuctSegment):
        system = project.systems.get(duct.system_id) if duct.system_id else None
        kind = system.name if system else "unassigned"
        item = f"Duct {duct.width * 1000:.0f} x {duct.depth * 1000:.0f} mm, {kind}"
        if duct.end_width:
            item = f"Transition {duct.width * 1000:.0f} to {duct.end_width * 1000:.0f} x {duct.depth * 1000:.0f} mm, {kind}"
        length = path_length(duct.path)
        add("Ducts", f"{item} - length", duct, "m", length)
        mean_width = (duct.width + (duct.end_width or duct.width)) / 2
        add("Ducts", f"{item} - sheet area", duct, "m²", 2 * (mean_width + duct.depth) * length)
    for fixture in project.elements_of(Fixture):
        family = family_of(fixture)
        category = "Plumbing fixtures" if family and family.discipline.value == "plumbing" else "Electrical fixtures"
        add(category, family.name if family else fixture.family or "Fixture", fixture, "nr", 1)
    for imported in project.elements_of(ImportedElement):
        add("Imported elements", imported.source_class, imported, "nr", 1)

    rows = [QuantityRow(c, item, lvl, unit, round(q, 3), int(n)) for (c, item, lvl, unit), (q, n) in groups.items()]
    return sorted(rows, key=lambda r: (r.category, r.level, r.item))


HEADERS = [f.name.title() for f in fields(QuantityRow)]


def export_csv(rows: Sequence[QuantityRow], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM so Excel detects UTF-8 (m², ×)
        writer = csv.writer(fh)
        writer.writerow(HEADERS)
        writer.writerows(astuple(r) for r in rows)


def export_xlsx(rows: Sequence[QuantityRow], path: Path, title: str = "Bill of quantities") -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "BOQ"
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(HEADERS)
    for cell in ws[2]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append(list(astuple(row)))
    for column in ws.columns:
        width = max(len(str(c.value or "")) for c in column[1:])
        ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 8), 60)
    ws.freeze_panes = "A3"
    wb.save(path)


EXPORTERS: dict[str, Callable[[Sequence[QuantityRow], Path], None]] = {".csv": export_csv, ".xlsx": export_xlsx}
