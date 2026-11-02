"""Building data model aligned with IFC concepts.

    Project -> Site -> Building -> Storey -> Element
                                          -> DistributionSystem (MEP)

All lengths are metres. Elements are placed in plan (x, y) relative to the
project origin and vertically relative to their storey elevation.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar, TypeVar

Point2 = tuple[float, float]
Point3 = tuple[float, float, float]


def new_id() -> str:
    return uuid.uuid4().hex


class Discipline(str, Enum):
    ARCHITECTURE = "architecture"
    STRUCTURE = "structure"
    PLUMBING = "plumbing"
    ELECTRICAL = "electrical"
    MECHANICAL = "mechanical"


@dataclass(kw_only=True)
class Element:
    id: str = field(default_factory=new_id)
    name: str = ""
    storey_id: str | None = None
    type_id: str | None = None
    properties: dict[str, object] = field(default_factory=dict)

    ifc_class: ClassVar[str] = "IfcBuildingElementProxy"
    discipline: ClassVar[Discipline] = Discipline.ARCHITECTURE


# --- Architecture --------------------------------------------------------------


@dataclass(kw_only=True)
class Wall(Element):
    """Straight wall defined by its plan centreline."""

    start: Point2
    end: Point2
    thickness: float = 0.2
    height: float = 3.0
    base_offset: float = 0.0  # above storey elevation

    ifc_class: ClassVar[str] = "IfcWall"

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def direction(self) -> Point2:
        length = self.length
        if length == 0:
            raise ValueError(f"Wall {self.id} has zero length")
        return ((self.end[0] - self.start[0]) / length, (self.end[1] - self.start[1]) / length)

    @property
    def normal(self) -> Point2:
        """Unit normal pointing to the left of the start->end direction."""
        dx, dy = self.direction
        return (-dy, dx)

    def point_at(self, distance: float) -> Point2:
        dx, dy = self.direction
        return (self.start[0] + dx * distance, self.start[1] + dy * distance)


@dataclass(kw_only=True)
class Opening(Element):
    """An opening hosted by a wall, positioned by its centre along the wall."""

    host_id: str
    offset: float  # distance from host wall start to opening centre
    width: float
    height: float
    sill_height: float = 0.0

    ifc_class: ClassVar[str] = "IfcOpeningElement"

    @property
    def span(self) -> tuple[float, float]:
        return (self.offset - self.width / 2, self.offset + self.width / 2)


@dataclass(kw_only=True)
class Door(Opening):
    width: float = 0.9
    height: float = 2.1
    ifc_class: ClassVar[str] = "IfcDoor"


@dataclass(kw_only=True)
class Window(Opening):
    width: float = 1.2
    height: float = 1.2
    sill_height: float = 0.9
    ifc_class: ClassVar[str] = "IfcWindow"


@dataclass(kw_only=True)
class Slab(Element):
    boundary: list[Point2]
    thickness: float = 0.2
    top_offset: float = 0.0  # top face relative to storey elevation

    ifc_class: ClassVar[str] = "IfcSlab"
    discipline: ClassVar[Discipline] = Discipline.STRUCTURE


@dataclass(kw_only=True)
class Space(Element):
    """A room. Not rendered as a solid; used for schedules, MEP zoning and tagging."""

    boundary: list[Point2]
    height: float = 3.0

    ifc_class: ClassVar[str] = "IfcSpace"

    @property
    def area(self) -> float:
        pts = self.boundary
        return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))) / 2


@dataclass(kw_only=True)
class Stair(Element):
    """Straight stair flight. Climbs from `start` to `end` (centres of the flight's ends, in plan)."""

    start: Point2
    end: Point2
    width: float = 1.0
    risers: int = 16
    height: float = 3.0  # total rise
    base_offset: float = 0.0

    ifc_class: ClassVar[str] = "IfcStair"

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def going(self) -> float:
        return self.length / self.risers

    @property
    def riser_height(self) -> float:
        return self.height / self.risers


@dataclass(kw_only=True)
class Column(Element):
    """Rectangular column standing on its level."""

    position: Point2  # centre in plan
    width: float = 0.3  # along local x
    depth: float = 0.3  # along local y
    rotation: float = 0.0  # degrees about z
    height: float = 3.0
    base_offset: float = 0.0

    ifc_class: ClassVar[str] = "IfcColumn"
    discipline: ClassVar[Discipline] = Discipline.STRUCTURE


@dataclass(kw_only=True)
class ImportedElement(Element):
    """Geometry-only element from an external model (column, stair, furniture, MEP part...).

    Kept for coordination, clash detection and IFC round-trip; not parametric.
    Vertices are world coordinates in metres.
    """

    source_class: str = "IfcBuildingElementProxy"
    source_id: str = ""  # e.g. the IFC GlobalId
    category: str = "architecture"  # render/visibility category: a Discipline value or "mechanical"
    vertices: list[Point3] = field(default_factory=list)
    faces: list[tuple[int, int, int]] = field(default_factory=list)


# --- MEP (Phase 2; defined now so the model and IFC mapping are stable) ----------


class SystemType(str, Enum):
    COLD_WATER = "cold_water"
    HOT_WATER = "hot_water"
    SANITARY = "sanitary"
    VENT = "vent"
    STORMWATER = "stormwater"
    POWER = "power"
    LIGHTING = "lighting"
    DATA = "data"
    SUPPLY_AIR = "supply_air"
    EXHAUST_AIR = "exhaust_air"
    FRESH_AIR = "fresh_air"
    RETURN_AIR = "return_air"


@dataclass(kw_only=True)
class DistributionSystem:
    """Groups connected MEP elements (IfcDistributionSystem)."""

    id: str = field(default_factory=new_id)
    name: str
    system_type: SystemType
    color: tuple[float, float, float] = (0.5, 0.5, 0.5)
    properties: dict[str, object] = field(default_factory=dict)  # e.g. circuit: panel_id, breaker_a, phase


@dataclass(kw_only=True)
class MepSegment(Element):
    """A run of pipe, conduit or tray along a 3D polyline (storey-relative z)."""

    path: list[Point3]
    system_id: str | None = None


@dataclass(kw_only=True)
class PipeSegment(MepSegment):
    diameter: float = 0.025
    slope: float = 0.0  # fall per unit length; enforced for drainage systems
    ifc_class: ClassVar[str] = "IfcPipeSegment"
    discipline: ClassVar[Discipline] = Discipline.PLUMBING


@dataclass(kw_only=True)
class CableCarrierSegment(MepSegment):
    carrier_type: str = "CABLETRAYSEGMENT"  # or "CONDUITSEGMENT" (round, diameter = width)
    width: float = 0.1
    depth: float = 0.05
    ifc_class: ClassVar[str] = "IfcCableCarrierSegment"
    discipline: ClassVar[Discipline] = Discipline.ELECTRICAL


@dataclass(kw_only=True)
class DuctSegment(MepSegment):
    """Rectangular HVAC duct. The path runs along the duct's centre (width across, depth vertical)."""

    width: float = 0.3
    depth: float = 0.3
    ifc_class: ClassVar[str] = "IfcDuctSegment"
    discipline: ClassVar[Discipline] = Discipline.MECHANICAL


@dataclass(kw_only=True)
class Fixture(Element):
    """Point-placed MEP component: WC, basin, socket, luminaire, panel..."""

    position: Point3
    rotation: float = 0.0  # degrees about z
    family: str = ""
    system_id: str | None = None


# --- Coordination --------------------------------------------------------------


class IssueStatus(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    RESOLVED = "resolved"
    APPROVED = "approved"  # accepted as-is (e.g. an intended penetration)


@dataclass(kw_only=True)
class Issue:
    """A coordination issue: a clash found by a clash test, or a manual note."""

    id: str = field(default_factory=new_id)
    title: str
    kind: str = "note"  # "hard", "clearance" or "note"
    element_ids: tuple[str, ...] = ()
    status: IssueStatus = IssueStatus.NEW
    point: Point3 | None = None  # world coordinates, for zooming to the issue
    distance: float = 0.0  # clearance clashes: actual gap (m)
    test: str = ""  # name of the clash test that found it
    assignee: str = ""
    comment: str = ""


# --- Spatial structure ---------------------------------------------------------


@dataclass(kw_only=True)
class Storey:
    id: str = field(default_factory=new_id)
    name: str
    elevation: float = 0.0
    height: float = 3.0


@dataclass(kw_only=True)
class Building:
    id: str = field(default_factory=new_id)
    name: str = "Building"
    storeys: list[Storey] = field(default_factory=list)


@dataclass(kw_only=True)
class Site:
    id: str = field(default_factory=new_id)
    name: str = "Site"
    buildings: list[Building] = field(default_factory=list)


E = TypeVar("E", bound=Element)


class Project:
    """Aggregate root. Mutate only through app.commands so undo and events stay consistent."""

    def __init__(self, name: str = "Untitled") -> None:
        self.id = new_id()
        self.name = name
        self.site = Site(buildings=[Building(storeys=[Storey(name="Level 0")])])
        self.systems: dict[str, DistributionSystem] = {}
        self.issues: dict[str, Issue] = {}
        self._elements: dict[str, Element] = {}

    @property
    def building(self) -> Building:
        return self.site.buildings[0]

    @property
    def storeys(self) -> list[Storey]:
        return self.building.storeys

    @property
    def default_storey(self) -> Storey:
        return self.storeys[0]

    def storey(self, storey_id: str | None) -> Storey | None:
        return next((s for s in self.storeys if s.id == storey_id), None)

    def storey_elevation(self, element: Element) -> float:
        storey = self.storey(element.storey_id)
        return storey.elevation if storey else 0.0

    # Element access
    def add(self, element: Element) -> None:
        if element.id in self._elements:
            raise ValueError(f"Duplicate element id {element.id}")
        if element.storey_id is not None and self.storey(element.storey_id) is None:
            raise ValueError(f"Unknown storey {element.storey_id}")
        self._elements[element.id] = element

    def remove(self, element_id: str) -> Element:
        return self._elements.pop(element_id)

    def get(self, element_id: str) -> Element | None:
        return self._elements.get(element_id)

    def __contains__(self, element_id: str) -> bool:
        return element_id in self._elements

    def __len__(self) -> int:
        return len(self._elements)

    def elements(self) -> Iterator[Element]:
        return iter(list(self._elements.values()))

    def elements_of(self, kind: type[E]) -> list[E]:
        return [e for e in self._elements.values() if isinstance(e, kind)]

    def hosted_openings(self, wall_id: str) -> list[Opening]:
        return [e for e in self._elements.values() if isinstance(e, Opening) and e.host_id == wall_id]
