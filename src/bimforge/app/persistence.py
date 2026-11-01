"""Native project file (.bimx): a single SQLite database.

Tables
    meta(key, value)                     schema_version, app_version, project id/name, site JSON
    systems(id, data)                    DistributionSystem JSON
    elements(id, class, storey_id, data) element fields as JSON

Saves are atomic: the database is written to a temporary file that then
replaces the target, so a crash mid-save never corrupts the previous file.
Missing fields load with their defaults and unknown fields are ignored,
so older files keep opening as the model grows.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from bimforge import __version__
from bimforge.core.model import (
    Building,
    CableCarrierSegment,
    Column,
    DistributionSystem,
    Door,
    Element,
    Fixture,
    ImportedElement,
    Issue,
    IssueStatus,
    Opening,
    PipeSegment,
    Project,
    Site,
    Slab,
    Space,
    Stair,
    Storey,
    SystemType,
    Wall,
    Window,
)

FILE_EXTENSION = ".bimx"
SCHEMA_VERSION = 2  # 2: issues table

ELEMENT_TYPES: dict[str, type[Element]] = {
    cls.__name__: cls
    for cls in (Wall, Opening, Door, Window, Slab, Space, Stair, Column, PipeSegment, CableCarrierSegment, Fixture,
                ImportedElement)
}


def register_element_type(cls: type[Element]) -> None:
    """Plugins call this so their element classes can be saved and loaded."""
    ELEMENT_TYPES[cls.__name__] = cls


class ProjectFileError(Exception):
    pass


@dataclass
class LoadResult:
    project: Project
    warnings: list[str] = field(default_factory=list)


_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE systems (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE issues (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE elements (id TEXT PRIMARY KEY, class TEXT NOT NULL, storey_id TEXT, data TEXT NOT NULL);
CREATE INDEX elements_by_storey ON elements(storey_id);
"""


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Cannot serialise {type(value).__name__}")


def _dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, separators=(",", ":"))


def _tuplify(value: Any) -> Any:
    """JSON turns tuples into lists; restore coordinate tuples (lists of plain numbers)."""
    if isinstance(value, list):
        if value and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value):
            return tuple(value)
        return [_tuplify(x) for x in value]
    return value


def _decode(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: (v if k == "properties" else _tuplify(v)) for k, v in data.items() if k in names}


def save_project(project: Project, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    with contextlib.closing(sqlite3.connect(tmp)) as con:
        with con:
            con.executescript(_SCHEMA)
            meta = {
                "schema_version": str(SCHEMA_VERSION),
                "app_version": __version__,
                "project_id": project.id,
                "project_name": project.name,
                "site": _dumps(dataclasses.asdict(project.site)),
            }
            con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
            con.executemany(
                "INSERT INTO systems VALUES (?, ?)",
                [(s.id, _dumps(dataclasses.asdict(s))) for s in project.systems.values()],
            )
            con.executemany(
                "INSERT INTO issues VALUES (?, ?)",
                [(i.id, _dumps(dataclasses.asdict(i))) for i in project.issues.values()],
            )
            con.executemany(
                "INSERT INTO elements VALUES (?, ?, ?, ?)",
                [
                    (e.id, type(e).__name__, e.storey_id, _dumps(dataclasses.asdict(e)))
                    for e in project.elements()
                ],
            )
    os.replace(tmp, path)


def load_project(path: Path) -> LoadResult:
    path = Path(path)
    if not path.is_file():
        raise ProjectFileError(f"File not found: {path}")
    try:
        with contextlib.closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as con:
            meta = dict(con.execute("SELECT key, value FROM meta"))
            systems = con.execute("SELECT data FROM systems").fetchall()
            rows = con.execute("SELECT id, class, data FROM elements").fetchall()
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            issues = con.execute("SELECT data FROM issues").fetchall() if "issues" in tables else []
    except sqlite3.DatabaseError as exc:
        raise ProjectFileError(f"Not a valid {FILE_EXTENSION} project: {exc}") from exc

    version = int(meta.get("schema_version", "0"))
    if version > SCHEMA_VERSION:
        raise ProjectFileError(f"Project was saved by a newer version (schema {version}); please update")

    project = Project(meta.get("project_name", path.stem))
    project.id = meta.get("project_id", project.id)
    project.site = _site_from(json.loads(meta["site"]))
    for (data,) in systems:
        raw = json.loads(data)
        raw["system_type"] = SystemType(raw["system_type"])
        system = DistributionSystem(**_decode(DistributionSystem, raw))
        project.systems[system.id] = system

    for (data,) in issues:
        raw = json.loads(data)
        raw["status"] = IssueStatus(raw.get("status", "new"))
        raw["element_ids"] = tuple(raw.get("element_ids", ()))
        issue = Issue(**_decode(Issue, raw))
        project.issues[issue.id] = issue

    result = LoadResult(project)
    skipped: dict[str, int] = {}
    elements = []
    for element_id, class_name, data in rows:
        cls = ELEMENT_TYPES.get(class_name)
        if cls is None:
            skipped[class_name] = skipped.get(class_name, 0) + 1
            continue
        try:
            elements.append(cls(**_decode(cls, json.loads(data))))
        except (TypeError, ValueError) as exc:
            result.warnings.append(f"Element {element_id} ({class_name}) could not be loaded: {exc}")
    # Hosts first, so openings always find their wall.
    for element in sorted(elements, key=lambda e: isinstance(e, Opening)):
        try:
            project.add(element)
        except ValueError as exc:  # e.g. unknown storey or duplicate id: skip the element, keep the file
            result.warnings.append(f"Element {element.id} ({type(element).__name__}) could not be loaded: {exc}")
    for class_name, count in skipped.items():
        result.warnings.append(f"{count} '{class_name}' elements skipped (plugin not installed?)")
    return result


def _site_from(raw: dict[str, Any]) -> Site:
    buildings = [
        Building(
            id=b["id"],
            name=b["name"],
            storeys=[Storey(**_decode(Storey, s)) for s in b.get("storeys", [])],
        )
        for b in raw.get("buildings", [])
    ]
    return Site(id=raw["id"], name=raw["name"], buildings=buildings)
