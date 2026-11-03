"""Schedules: the tables of doors, windows, rooms and so on that accompany a drawing set."""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bimforge.core.model import Column, Door, DuctSegment, Project, Space, Stair, Wall, Window
from bimforge.disciplines.plumbing import path_length


@dataclass
class Schedule:
    name: str
    headers: list[str]
    rows: list[list[str]]
    element_ids: list[str]  # one per row, to select the element from its row

    @property
    def totals(self) -> str:
        return f"{len(self.rows)} item{'s' if len(self.rows) != 1 else ''}"


def _mm(metres: float) -> str:
    return f"{metres * 1000:.0f}"


def build_schedules(project: Project) -> list[Schedule]:
    level = {s.id: s.name for s in project.storeys}

    def on(element) -> str:
        return level.get(element.storey_id, "")

    def table(name: str, headers: list[str], elements: list, row: Callable) -> Schedule:
        ordered = sorted(elements, key=lambda e: (on(e), e.name))
        return Schedule(name, headers, [row(e) for e in ordered], [e.id for e in ordered])

    def host(opening) -> str:
        wall = project.get(opening.host_id)
        return wall.name if wall else ""

    return [
        table("Doors", ["Mark", "Level", "Width (mm)", "Height (mm)", "Wall"], project.elements_of(Door),
              lambda e: [e.name, on(e), _mm(e.width), _mm(e.height), host(e)]),
        table("Windows", ["Mark", "Level", "Width (mm)", "Height (mm)", "Sill (mm)", "Wall"],
              project.elements_of(Window),
              lambda e: [e.name, on(e), _mm(e.width), _mm(e.height), _mm(e.sill_height), host(e)]),
        table("Rooms", ["Name", "Level", "Area (m²)", "Height (m)"], project.elements_of(Space),
              lambda e: [e.name, on(e), f"{e.area:.2f}", f"{e.height:.2f}"]),
        table("Walls", ["Mark", "Level", "Length (m)", "Thickness (mm)", "Height (m)", "Openings"],
              project.elements_of(Wall),
              lambda e: [e.name, on(e), f"{e.length:.2f}", _mm(e.thickness), f"{e.height:.2f}",
                         str(len(project.hosted_openings(e.id)))]),
        table("Stairs", ["Mark", "Level", "Flights", "Risers", "Riser (mm)", "Width (mm)", "Rise (m)"],
              project.elements_of(Stair),
              lambda e: [e.name, on(e), str(len(e.flights)), str(e.total_risers), _mm(e.riser_height), _mm(e.width),
                         f"{e.height:.2f}"]),
        table("Columns", ["Mark", "Level", "Width (mm)", "Depth (mm)", "Height (m)"], project.elements_of(Column),
              lambda e: [e.name, on(e), _mm(e.width), _mm(e.depth), f"{e.height:.2f}"]),
        table("Ducts", ["Mark", "Level", "System", "Size (mm)", "Length (m)"], project.elements_of(DuctSegment),
              lambda e: [e.name, on(e), project.systems[e.system_id].name if e.system_id in project.systems else "",
                         f"{_mm(e.width)} x {_mm(e.depth)}", f"{path_length(e.path):.2f}"]),
    ]


def export_schedule_csv(schedule: Schedule, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(schedule.headers)
        writer.writerows(schedule.rows)
