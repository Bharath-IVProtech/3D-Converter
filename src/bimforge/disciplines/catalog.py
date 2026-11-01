"""Fixture families: the MEP components that can be placed in a model.

Dimensions are typical, not manufacturer-specific. Fixture units follow IPC
(Table 709.1 drainage DFU; Appendix E supply WSFU, private use); loads are
design allowances per point. All values are defaults a project can override
through element properties ("load_va", "dfu", "wsfu").
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from bimforge.core.model import Discipline, Fixture, Point3


@dataclass(frozen=True)
class FixtureFamily:
    key: str
    name: str
    discipline: Discipline
    ifc_class: str
    predefined_type: str
    size: tuple[float, float, float]  # width (local x), depth (local y), height (z), metres
    mount_height: float  # default base height above the level
    connection: tuple[float, float, float] = (0.0, 0.0, 0.0)  # local point where pipes/cables connect
    dfu: float = 0.0  # drainage fixture units
    wsfu: float = 0.0  # total water supply fixture units
    min_drain_dn: int = 0  # minimum drain/trap size, mm
    load_va: float = 0.0  # electrical design load per point
    is_panel: bool = False


PLUMBING, ELECTRICAL = Discipline.PLUMBING, Discipline.ELECTRICAL

FAMILIES: dict[str, FixtureFamily] = {
    f.key: f
    for f in (
        FixtureFamily("wc", "Water closet (cistern)", PLUMBING, "IfcSanitaryTerminal", "TOILETPAN",
                      (0.38, 0.7, 0.8), 0.0, (0.0, 0.1, 0.0), dfu=3, wsfu=2.2, min_drain_dn=80),
        FixtureFamily("basin", "Wash basin", PLUMBING, "IfcSanitaryTerminal", "WASHHANDBASIN",
                      (0.55, 0.45, 0.2), 0.65, (0.0, -0.15, 0.0), dfu=1, wsfu=0.7, min_drain_dn=40),
        FixtureFamily("sink", "Kitchen sink", PLUMBING, "IfcSanitaryTerminal", "SINK",
                      (0.8, 0.5, 0.25), 0.65, (0.0, -0.15, 0.0), dfu=2, wsfu=1.4, min_drain_dn=40),
        FixtureFamily("shower", "Shower tray", PLUMBING, "IfcSanitaryTerminal", "SHOWER",
                      (0.9, 0.9, 0.08), 0.0, (0.0, 0.0, 0.0), dfu=2, wsfu=1.4, min_drain_dn=50),
        FixtureFamily("bath", "Bathtub", PLUMBING, "IfcSanitaryTerminal", "BATH",
                      (1.7, 0.75, 0.55), 0.0, (0.7, 0.0, 0.0), dfu=2, wsfu=1.4, min_drain_dn=40),
        FixtureFamily("floor_drain", "Floor drain", PLUMBING, "IfcWasteTerminal", "FLOORTRAP",
                      (0.15, 0.15, 0.05), 0.0, (0.0, 0.0, 0.0), dfu=2, min_drain_dn=50),
        FixtureFamily("socket", "Socket outlet (double)", ELECTRICAL, "IfcOutlet", "POWEROUTLET",
                      (0.15, 0.05, 0.09), 0.3, (0.0, 0.0, 0.0), load_va=200),
        FixtureFamily("switch", "Light switch", ELECTRICAL, "IfcSwitchingDevice", "TOGGLESWITCH",
                      (0.09, 0.04, 0.09), 1.2, (0.0, 0.0, 0.0)),
        FixtureFamily("light", "Ceiling light (LED)", ELECTRICAL, "IfcLightFixture", "POINTSOURCE",
                      (0.3, 0.3, 0.08), 2.62, (0.0, 0.0, 0.08), load_va=20),
        FixtureFamily("water_heater", "Water heater", ELECTRICAL, "IfcElectricAppliance", "FREESTANDINGWATERHEATER",
                      (0.45, 0.45, 0.7), 1.6, (0.0, 0.0, 0.7), load_va=2000),
        FixtureFamily("db", "Distribution board", ELECTRICAL, "IfcElectricDistributionBoard", "DISTRIBUTIONBOARD",
                      (0.45, 0.12, 0.6), 1.4, (0.0, 0.0, 0.6), is_panel=True),
    )
}


def family_of(fixture: Fixture) -> FixtureFamily | None:
    return FAMILIES.get(fixture.family)


def fixture_value(fixture: Fixture, name: str) -> float:
    """A family value, overridable per instance through fixture.properties[name]."""
    if name in fixture.properties:
        return float(fixture.properties[name])  # type: ignore[arg-type]
    family = family_of(fixture)
    return float(getattr(family, name, 0.0)) if family else 0.0


def connection_point(fixture: Fixture, storey_elevation: float = 0.0) -> Point3:
    """World (or storey-relative when elevation is 0) position of the fixture's connection point."""
    family = family_of(fixture)
    cx, cy, cz = family.connection if family else (0.0, 0.0, 0.0)
    a = math.radians(fixture.rotation)
    x, y, z = fixture.position
    return (x + cx * math.cos(a) - cy * math.sin(a), y + cx * math.sin(a) + cy * math.cos(a), storey_elevation + z + cz)
