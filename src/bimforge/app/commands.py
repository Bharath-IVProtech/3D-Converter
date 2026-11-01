"""Every model mutation is a Command, which gives undo/redo, events and an audit trail.

UI tools, AI proposals and natural-language requests all end up here.
"""

from __future__ import annotations

import dataclasses
import math
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy

from bimforge.app.events import ElementsChanged, EventBus
from bimforge.core.model import (
    Column,
    DistributionSystem,
    Element,
    Fixture,
    ImportedElement,
    Issue,
    MepSegment,
    Opening,
    Project,
    Slab,
    Space,
    Stair,
    Storey,
    Wall,
    new_id,
)
from bimforge.core.validation import ValidationError, validate_with_dependents
from bimforge.geometry import vec2 as v


class Command(ABC):
    label: str = "Edit"

    @abstractmethod
    def apply(self, project: Project) -> ElementsChanged: ...

    @abstractmethod
    def revert(self, project: Project) -> ElementsChanged: ...


def _combine(changes: Iterable[ElementsChanged]) -> ElementsChanged:
    added, modified, removed = {}, {}, {}  # dicts keep order and dedupe
    flags = {"storeys": False, "systems": False, "issues": False}
    for c in changes:
        added.update(dict.fromkeys(c.added))
        modified.update(dict.fromkeys(c.modified))
        removed.update(dict.fromkeys(c.removed))
        for flag in flags:
            flags[flag] |= getattr(c, flag)
    return ElementsChanged(tuple(added), tuple(k for k in modified if k not in added), tuple(removed), **flags)


class AddElements(Command):
    def __init__(self, elements: Iterable[Element], label: str = "Add elements") -> None:
        self.elements = list(elements)
        self.label = label

    def apply(self, project: Project) -> ElementsChanged:
        added: list[str] = []
        try:
            for element in self.elements:
                project.add(element)
                added.append(element.id)
        except Exception:
            for element_id in added:  # keep the command atomic
                project.remove(element_id)
            raise
        return ElementsChanged(added=tuple(added))

    def revert(self, project: Project) -> ElementsChanged:
        for element in self.elements:
            project.remove(element.id)
        return ElementsChanged(removed=tuple(e.id for e in self.elements))


class RemoveElements(Command):
    """Removes elements and any openings hosted by them."""

    def __init__(self, element_ids: Iterable[str], label: str = "Delete") -> None:
        self.element_ids = list(element_ids)
        self.label = label
        self._removed: list[Element] = []

    def apply(self, project: Project) -> ElementsChanged:
        ids = [i for i in self.element_ids if i in project]
        hosted = [o.id for i in ids for o in project.hosted_openings(i) if o.id not in ids]
        self._removed = [project.remove(i) for i in hosted + ids]
        return ElementsChanged(removed=tuple(e.id for e in self._removed))

    def revert(self, project: Project) -> ElementsChanged:
        # Restore hosts before the openings they host.
        restored = sorted(self._removed, key=lambda e: isinstance(e, Opening))
        for element in restored:
            project.add(element)
        return ElementsChanged(added=tuple(e.id for e in restored))


class ModifyElement(Command):
    """Sets fields on one element; rejected (and rolled back) if the result is invalid."""

    def __init__(
        self, element_id: str, changes: Mapping[str, object], label: str = "Edit properties", validate: bool = True
    ) -> None:
        self.element_id = element_id
        self.changes = dict(changes)
        self.label = label
        self.validate = validate  # False when a parent command validates the combined result
        self._old: dict[str, object] = {}

    def apply(self, project: Project) -> ElementsChanged:
        element = project.get(self.element_id)
        if element is None:
            raise KeyError(f"Element {self.element_id} not found")
        allowed = {f.name for f in dataclasses.fields(element)} - {"id"}
        unknown = set(self.changes) - allowed
        if unknown:
            raise ValidationError(f"{type(element).__name__} has no editable field(s): {', '.join(sorted(unknown))}")
        self._old = {name: getattr(element, name) for name in self.changes}
        for name, value in self.changes.items():
            setattr(element, name, value)
        errors = validate_with_dependents(element, project) if self.validate else []
        if errors:
            for name, value in self._old.items():
                setattr(element, name, value)
            raise ValidationError("; ".join(errors))
        return ElementsChanged(modified=(self.element_id,))

    def revert(self, project: Project) -> ElementsChanged:
        element = project.get(self.element_id)
        for name, value in self._old.items():
            setattr(element, name, value)
        return ElementsChanged(modified=(self.element_id,))


class CompositeCommand(Command):
    """Runs sub-commands as one undo step; all succeed or none are applied."""

    def __init__(self, commands: Sequence[Command], label: str) -> None:
        self.commands = list(commands)
        self.label = label

    def apply(self, project: Project) -> ElementsChanged:
        done: list[Command] = []
        changes = []
        try:
            for command in self.commands:
                changes.append(command.apply(project))
                done.append(command)
        except Exception:
            for command in reversed(done):
                command.revert(project)
            raise
        return _combine(changes)

    def revert(self, project: Project) -> ElementsChanged:
        return _combine([c.revert(project) for c in reversed(self.commands)])


def translation_changes(element: Element, dx: float, dy: float, project: Project) -> dict[str, object]:
    """Field changes that move an element by (dx, dy) in plan.

    Openings slide along their host wall (the perpendicular part is ignored)
    and are clamped to stay inside it.
    """

    def shift(p):
        return (p[0] + dx, p[1] + dy, *p[2:])

    if isinstance(element, (Wall, Stair)):
        return {"start": shift(element.start), "end": shift(element.end)}
    if isinstance(element, Column):
        return {"position": shift(element.position)}
    if isinstance(element, Opening):
        host = project.get(element.host_id)
        if not isinstance(host, Wall):
            return {}
        ux, uy = host.direction
        half = element.width / 2
        offset = min(max(element.offset + dx * ux + dy * uy, half), host.length - half)
        return {"offset": offset}
    if isinstance(element, (Slab, Space)):
        return {"boundary": [shift(p) for p in element.boundary]}
    if isinstance(element, MepSegment):
        return {"path": [shift(p) for p in element.path]}
    if isinstance(element, Fixture):
        return {"position": shift(element.position)}
    return {}


JOIN_TOLERANCE = 0.01


def joined_wall_changes(
    project: Project, moved: Sequence[Wall], dx: float, dy: float, exclude: set[str]
) -> list[ModifyElement]:
    """Keep walls joined to moving walls connected.

    Corner joins (endpoint on a moving wall's endpoint) follow the full move.
    T-joins (endpoint on a moving wall's side) slide only perpendicular to that
    wall, so the stem stays on it. Openings in stretched walls keep their world
    position.
    """
    commands: list[ModifyElement] = []
    for other in project.elements_of(Wall):
        if other.id in exclude:
            continue
        changes: dict[str, object] = {}
        for attr in ("start", "end"):
            p = getattr(other, attr)
            for wall in moved:
                if wall.storey_id != other.storey_id:
                    continue
                if min(math.dist(p, wall.start), math.dist(p, wall.end)) <= JOIN_TOLERANCE:
                    changes[attr] = (p[0] + dx, p[1] + dy)
                    break
                if v.point_segment_distance(p, wall.start, wall.end) <= JOIN_TOLERANCE:
                    nx, ny = wall.normal
                    d = dx * nx + dy * ny
                    changes[attr] = (p[0] + nx * d, p[1] + ny * d)
                    break
        if not changes:
            continue
        commands.append(ModifyElement(other.id, changes, validate=False))
        new_start = changes.get("start", other.start)
        new_end = changes.get("end", other.end)
        if math.dist(new_start, new_end) == 0:
            continue  # degenerate; validation will reject the move
        u = v.unit(v.sub(new_end, new_start))
        for opening in project.hosted_openings(other.id):
            centre = other.point_at(opening.offset)
            commands.append(ModifyElement(opening.id, {"offset": v.dot(v.sub(centre, new_start), u)}, validate=False))
    return commands


class MoveElements(Command):
    """Moves elements in plan. Openings whose host is also moving travel with it,
    and (by default) walls joined to moving walls stretch to stay connected."""

    def __init__(
        self, element_ids: Iterable[str], dx: float, dy: float, label: str = "Move", move_joined: bool = True
    ) -> None:
        self.element_ids = list(element_ids)
        self.dx, self.dy = dx, dy
        self.label = label
        self.move_joined = move_joined
        self._inner: CompositeCommand | None = None

    def apply(self, project: Project) -> ElementsChanged:
        first_run = self._inner is None
        if self._inner is None:
            ids = set(self.element_ids)
            commands: list[Command] = []
            moved_walls = []
            for element_id in self.element_ids:
                element = project.get(element_id)
                if element is None or (isinstance(element, Opening) and element.host_id in ids):
                    continue
                changes = translation_changes(element, self.dx, self.dy, project)
                if changes:
                    commands.append(ModifyElement(element_id, changes, validate=False))
                if isinstance(element, Wall):
                    moved_walls.append(element)
            if self.move_joined and moved_walls:
                commands += joined_wall_changes(project, moved_walls, self.dx, self.dy, ids)
            self._inner = CompositeCommand(commands, self.label)
        change = self._inner.apply(project)
        if first_run:
            errors = [err for i in change.modified if (e := project.get(i)) for err in validate_with_dependents(e, project)]
            if errors:
                self._inner.revert(project)
                self._inner = None
                raise ValidationError("; ".join(dict.fromkeys(errors)))
        # Hosted openings change position too, so views must redraw them.
        hosted = [o.id for i in change.modified for o in project.hosted_openings(i)]
        return _combine([change, ElementsChanged(modified=tuple(hosted))])

    def revert(self, project: Project) -> ElementsChanged:
        assert self._inner is not None
        change = self._inner.revert(project)
        hosted = [o.id for i in change.modified for o in project.hosted_openings(i)]
        return _combine([change, ElementsChanged(modified=tuple(hosted))])


# --- Levels ----------------------------------------------------------------------

STOREY_FIELDS = {"name", "elevation", "height"}


def _sort_storeys(project: Project) -> None:
    project.storeys.sort(key=lambda s: s.elevation)


class AddStorey(Command):
    def __init__(self, storey: Storey, label: str = "Add level") -> None:
        self.storey = storey
        self.label = label

    def apply(self, project: Project) -> ElementsChanged:
        if project.storey(self.storey.id) is not None:
            raise ValidationError(f"Level {self.storey.name} already exists")
        if self.storey.height <= 0:
            raise ValidationError("Level height must be positive")
        project.storeys.append(self.storey)
        _sort_storeys(project)
        return ElementsChanged(storeys=True)

    def revert(self, project: Project) -> ElementsChanged:
        project.storeys.remove(self.storey)
        return ElementsChanged(storeys=True)


class ModifyStorey(Command):
    """Rename or move a level; elements on it move with it (their heights are level-relative)."""

    def __init__(self, storey_id: str, changes: Mapping[str, object], label: str = "Edit level") -> None:
        self.storey_id = storey_id
        self.changes = dict(changes)
        self.label = label
        self._old: dict[str, object] = {}

    def apply(self, project: Project) -> ElementsChanged:
        storey = project.storey(self.storey_id)
        if storey is None:
            raise KeyError(f"Level {self.storey_id} not found")
        unknown = set(self.changes) - STOREY_FIELDS
        if unknown:
            raise ValidationError(f"Levels have no editable field(s): {', '.join(sorted(unknown))}")
        if float(self.changes.get("height", storey.height)) <= 0:
            raise ValidationError("Level height must be positive")
        self._old = {k: getattr(storey, k) for k in self.changes}
        for k, value in self.changes.items():
            setattr(storey, k, value)
        _sort_storeys(project)
        return self._change(project)

    def revert(self, project: Project) -> ElementsChanged:
        storey = project.storey(self.storey_id)
        for k, value in self._old.items():
            setattr(storey, k, value)
        _sort_storeys(project)
        return self._change(project)

    def _change(self, project: Project) -> ElementsChanged:
        on_level = tuple(e.id for e in project.elements() if e.storey_id == self.storey_id)
        return ElementsChanged(modified=on_level, storeys=True)


class RemoveStorey(Command):
    def __init__(self, storey_id: str, label: str = "Delete level") -> None:
        self.storey_id = storey_id
        self.label = label
        self._index = 0
        self._storey: Storey | None = None

    def apply(self, project: Project) -> ElementsChanged:
        storey = project.storey(self.storey_id)
        if storey is None:
            raise KeyError(f"Level {self.storey_id} not found")
        if len(project.storeys) == 1:
            raise ValidationError("A project needs at least one level")
        if any(e.storey_id == self.storey_id for e in project.elements()):
            raise ValidationError(f"Level {storey.name} is not empty; delete or move its elements first")
        self._index = project.storeys.index(storey)
        self._storey = storey
        project.storeys.remove(storey)
        return ElementsChanged(storeys=True)

    def revert(self, project: Project) -> ElementsChanged:
        assert self._storey is not None
        project.storeys.insert(self._index, self._storey)
        return ElementsChanged(storeys=True)


def copy_elements(elements: Sequence[Element], storey_id: str) -> list[Element]:
    """Deep copies with fresh ids on another level. Openings are kept only if their host is copied too."""
    id_map = {e.id: new_id() for e in elements}
    copies: list[Element] = []
    for element in elements:
        if isinstance(element, Opening) and element.host_id not in id_map:
            continue
        copy = deepcopy(element)
        copy.id = id_map[element.id]
        copy.storey_id = storey_id
        if isinstance(copy, Opening):
            copy.host_id = id_map[element.host_id]
        copies.append(copy)
    return copies


class CopyToStorey(Command):
    """Copy elements to another level (e.g. repeat a typical floor)."""

    def __init__(self, element_ids: Iterable[str], storey_id: str, label: str = "Copy to level") -> None:
        self.element_ids = list(element_ids)
        self.storey_id = storey_id
        self.label = label
        self._inner: AddElements | None = None

    @property
    def created(self) -> list[Element]:
        return self._inner.elements if self._inner else []

    def apply(self, project: Project) -> ElementsChanged:
        if project.storey(self.storey_id) is None:
            raise KeyError(f"Level {self.storey_id} not found")
        if self._inner is None:
            sources = [e for i in self.element_ids if (e := project.get(i)) is not None]
            # Imported meshes are in world coordinates and do not follow level elevations.
            sources = [e for e in sources if not isinstance(e, ImportedElement)]
            self._inner = AddElements(copy_elements(sources, self.storey_id), self.label)
        return self._inner.apply(project)

    def revert(self, project: Project) -> ElementsChanged:
        assert self._inner is not None
        return self._inner.revert(project)


class MergeProject(Command):
    """Bring another project (e.g. an imported IFC) into this one as a single undo step.

    Levels within 1 cm of an existing level's elevation are merged into it;
    the rest are added.
    """

    LEVEL_MATCH = 0.01

    def __init__(self, source: Project, label: str = "Import model") -> None:
        self.source = source
        self.label = label
        self._inner: CompositeCommand | None = None

    def apply(self, project: Project) -> ElementsChanged:
        if self._inner is None:
            storey_map: dict[str, str] = {}
            commands: list[Command] = []
            for storey in self.source.storeys:
                match = next(
                    (s for s in project.storeys if abs(s.elevation - storey.elevation) <= self.LEVEL_MATCH), None
                )
                if match is None:
                    match = storey if project.storey(storey.id) is None else dataclasses.replace(storey, id=new_id())
                    commands.append(AddStorey(match, "Add imported level"))
                storey_map[storey.id] = match.id
            elements = list(self.source.elements())
            for element in elements:
                element.storey_id = storey_map.get(element.storey_id, project.default_storey.id)
            commands.append(AddElements(elements, self.label))
            self._inner = CompositeCommand(commands, self.label)
        return self._inner.apply(project)

    def revert(self, project: Project) -> ElementsChanged:
        assert self._inner is not None
        return self._inner.revert(project)


# --- MEP systems and coordination issues ------------------------------------------

SYSTEM_FIELDS = {"name", "system_type", "color", "properties"}


class AddSystem(Command):
    def __init__(self, system: DistributionSystem, label: str = "Add system") -> None:
        self.system = system
        self.label = label

    def apply(self, project: Project) -> ElementsChanged:
        if self.system.id in project.systems:
            raise ValidationError(f"System {self.system.name} already exists")
        project.systems[self.system.id] = self.system
        return ElementsChanged(systems=True)

    def revert(self, project: Project) -> ElementsChanged:
        del project.systems[self.system.id]
        return ElementsChanged(systems=True)


class ModifySystem(Command):
    def __init__(self, system_id: str, changes: Mapping[str, object], label: str = "Edit system") -> None:
        self.system_id = system_id
        self.changes = dict(changes)
        self.label = label
        self._old: dict[str, object] = {}

    def apply(self, project: Project) -> ElementsChanged:
        system = project.systems.get(self.system_id)
        if system is None:
            raise KeyError(f"System {self.system_id} not found")
        unknown = set(self.changes) - SYSTEM_FIELDS
        if unknown:
            raise ValidationError(f"Systems have no editable field(s): {', '.join(sorted(unknown))}")
        self._old = {k: getattr(system, k) for k in self.changes}
        for k, value in self.changes.items():
            setattr(system, k, dict(value) if k == "properties" else value)  # type: ignore[call-overload]
        return self._change(project)

    def revert(self, project: Project) -> ElementsChanged:
        system = project.systems[self.system_id]
        for k, value in self._old.items():
            setattr(system, k, value)
        return self._change(project)

    def _change(self, project: Project) -> ElementsChanged:
        members = tuple(e.id for e in project.elements() if getattr(e, "system_id", None) == self.system_id)
        return ElementsChanged(modified=members, systems=True)


class RemoveSystem(Command):
    """Deletes a system and unassigns its members (they are kept)."""

    def __init__(self, system_id: str, label: str = "Delete system") -> None:
        self.system_id = system_id
        self.label = label
        self._system: DistributionSystem | None = None
        self._members: list[str] = []

    def apply(self, project: Project) -> ElementsChanged:
        self._system = project.systems.pop(self.system_id)
        members = [e for e in project.elements() if getattr(e, "system_id", None) == self.system_id]
        for element in members:
            element.system_id = None  # type: ignore[attr-defined]
        self._members = [e.id for e in members]
        return ElementsChanged(modified=tuple(self._members), systems=True)

    def revert(self, project: Project) -> ElementsChanged:
        assert self._system is not None
        project.systems[self.system_id] = self._system
        for element_id in self._members:
            project.get(element_id).system_id = self.system_id  # type: ignore[union-attr]
        return ElementsChanged(modified=tuple(self._members), systems=True)


class SetIssues(Command):
    """Replace the issue list (after a clash run or model check, or a status edit)."""

    def __init__(self, issues: Mapping[str, Issue], label: str = "Update issues") -> None:
        self.issues = dict(issues)
        self.label = label
        self._old: dict[str, Issue] = {}

    def apply(self, project: Project) -> ElementsChanged:
        self._old, project.issues = project.issues, dict(self.issues)
        return ElementsChanged(issues=True)

    def revert(self, project: Project) -> ElementsChanged:
        project.issues = self._old
        return ElementsChanged(issues=True)


_UNSAVED = object()  # sentinel: history position that matches no saved state


class CommandBus:
    def __init__(self, project: Project, events: EventBus, max_history: int = 500) -> None:
        self.project = project
        self.events = events
        self.max_history = max_history
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        self._saved_at: object | None = None  # top of the undo stack when last saved

    def execute(self, command: Command) -> None:
        change = command.apply(self.project)  # raises before touching history if invalid
        self._undo.append(command)
        del self._undo[: -self.max_history]
        self._redo.clear()
        self._publish(change)

    def undo(self) -> bool:
        if not self._undo:
            return False
        command = self._undo.pop()
        self._publish(command.revert(self.project))
        self._redo.append(command)
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        command = self._redo.pop()
        self._publish(command.apply(self.project))
        self._undo.append(command)
        return True

    @property
    def undo_label(self) -> str | None:
        return self._undo[-1].label if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1].label if self._redo else None

    @property
    def is_dirty(self) -> bool:
        return (self._undo[-1] if self._undo else None) is not self._saved_at

    def mark_saved(self) -> None:
        self._saved_at = self._undo[-1] if self._undo else None

    def mark_unsaved(self) -> None:
        """For state that differs from disk without history, e.g. a recovered autosave."""
        self._saved_at = _UNSAVED

    def _publish(self, change: ElementsChanged) -> None:
        if not change.is_empty:
            self.events.publish(change)
