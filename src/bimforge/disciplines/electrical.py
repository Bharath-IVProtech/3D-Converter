"""Electrical engineering: circuits, loads, protective device and cable selection, panel schedules, SLD.

A circuit is a DistributionSystem (POWER or LIGHTING) whose properties hold
  panel_id     id of the distribution-board Fixture feeding it
  breaker_a    rated current of its protective device (chosen automatically if absent)
  cable_mm2    conductor size (chosen automatically if absent)
  length_m     cable length (estimated from geometry if absent)
Loads come from the fixtures assigned to the circuit (family load_va, overridable per fixture).

Indicative defaults (single-phase 230 V, copper PVC cable, reference method C,
two loaded conductors) from IEC 60364-5-52 Table B.52.4 and BS 7671 Table 4D1B.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape

from bimforge.core.model import DistributionSystem, Fixture, Issue, Project, SystemType
from bimforge.disciplines.catalog import family_of, fixture_value

CIRCUIT_TYPES = {SystemType.POWER, SystemType.LIGHTING}
VOLTAGE = 230.0
BREAKER_RATINGS = [6, 10, 16, 20, 25, 32, 40, 50, 63, 80, 100, 125]
# conductor mm2 -> (current-carrying capacity Iz A, voltage drop mV/A/m)
CABLES: list[tuple[float, float, float]] = [
    (1.5, 19.5, 29.0), (2.5, 27.0, 18.0), (4.0, 36.0, 11.0), (6.0, 46.0, 7.3),
    (10.0, 63.0, 4.4), (16.0, 85.0, 2.8), (25.0, 112.0, 1.75), (35.0, 138.0, 1.25),
]
VOLTAGE_DROP_LIMIT = {SystemType.LIGHTING: 0.03, SystemType.POWER: 0.05}
ROUTE_FACTOR = 1.2  # cable length allowance over the orthogonal distance


def circuits(project: Project, panel_id: str | None = None) -> list[DistributionSystem]:
    found = [s for s in project.systems.values() if s.system_type in CIRCUIT_TYPES]
    if panel_id is not None:
        found = [s for s in found if s.properties.get("panel_id") == panel_id]
    return sorted(found, key=lambda s: s.name)


def panels(project: Project) -> list[Fixture]:
    return [f for f in project.elements_of(Fixture) if (fam := family_of(f)) is not None and fam.is_panel]


def circuit_fixtures(project: Project, circuit: DistributionSystem) -> list[Fixture]:
    return [f for f in project.elements_of(Fixture) if f.system_id == circuit.id]


def breaker_for(current: float) -> int:
    for rating in BREAKER_RATINGS:
        if rating >= current:
            return rating
    raise ValueError(f"{current:.1f} A exceeds the largest tabulated breaker")


def cable_for(breaker_a: float) -> float:
    for mm2, iz, _ in CABLES:
        if iz >= breaker_a:
            return mm2
    raise ValueError(f"No tabulated cable carries {breaker_a} A")


def _mv_per_am(mm2: float) -> float:
    return next(mv for size, _, mv in CABLES if size == mm2)


def _world(project: Project, fixture: Fixture) -> tuple[float, float, float]:
    x, y, z = fixture.position
    return (x, y, z + project.storey_elevation(fixture))


def estimated_length(project: Project, circuit: DistributionSystem) -> float:
    """Orthogonal distance from the panel to the farthest point on the circuit, plus a routing allowance."""
    panel = project.get(str(circuit.properties.get("panel_id", "")))
    loads = circuit_fixtures(project, circuit)
    if not isinstance(panel, Fixture) or not loads:
        return 0.0
    px, py, pz = _world(project, panel)
    far = max(abs(x - px) + abs(y - py) + abs(z - pz) for x, y, z in (_world(project, f) for f in loads))
    return round(far * ROUTE_FACTOR, 1)


@dataclass
class CircuitRow:
    circuit_id: str
    name: str
    kind: str
    points: int
    load_va: float
    design_current: float
    breaker_a: float
    cable_mm2: float
    length_m: float
    voltage_drop: float  # fraction of nominal voltage
    ok: bool
    notes: str = ""


def evaluate(project: Project, circuit: DistributionSystem) -> CircuitRow:
    fixtures = circuit_fixtures(project, circuit)
    load = sum(fixture_value(f, "load_va") for f in fixtures)
    ib = load / VOLTAGE
    notes = []
    breaker = float(circuit.properties.get("breaker_a") or breaker_for(max(ib, 0.1)))
    cable = float(circuit.properties.get("cable_mm2") or cable_for(breaker))
    length = float(circuit.properties.get("length_m") or estimated_length(project, circuit))
    vd = _mv_per_am(cable) * ib * length / 1000 / VOLTAGE if cable in [c[0] for c in CABLES] else 0.0
    iz = next((i for size, i, _ in CABLES if size == cable), 0.0)
    if ib > breaker:
        notes.append(f"load {ib:.1f} A exceeds breaker {breaker:g} A")
    if breaker > iz:
        notes.append(f"breaker {breaker:g} A exceeds cable rating {iz:g} A")
    limit = VOLTAGE_DROP_LIMIT.get(circuit.system_type, 0.05)
    if vd > limit:
        notes.append(f"voltage drop {vd:.1%} exceeds {limit:.0%}")
    if not circuit.properties.get("panel_id"):
        notes.append("not connected to a panel")
    return CircuitRow(circuit.id, circuit.name, circuit.system_type.value, len(fixtures), load, ib, breaker, cable,
                      length, vd, not notes, "; ".join(notes))


def panel_schedule(project: Project, panel_id: str) -> list[CircuitRow]:
    return [evaluate(project, c) for c in circuits(project, panel_id)]


def check(project: Project) -> list[Issue]:
    issues = []
    for circuit in circuits(project):
        row = evaluate(project, circuit)
        if not row.ok:
            ids = tuple(f.id for f in circuit_fixtures(project, circuit))
            issues.append(Issue(title=f"Circuit {circuit.name}: {row.notes}", kind="code", element_ids=ids,
                                test="Electrical rules"))
    for fixture in project.elements_of(Fixture):
        family = family_of(fixture)
        if family and family.discipline.value == "electrical" and not family.is_panel and not fixture.system_id \
                and fixture_value(fixture, "load_va") > 0:
            issues.append(Issue(title=f"{fixture.name or family.name} is not on any circuit", kind="code",
                                element_ids=(fixture.id,), test="Electrical rules"))
    return issues


def single_line_diagram_svg(project: Project, panel_id: str) -> str:
    """A simple single-line diagram: incoming supply, the board busbar, one outgoing way per circuit."""
    panel = project.get(panel_id)
    rows = panel_schedule(project, panel_id)
    pitch, left, bus_y = 120, 60, 140
    width = max(left * 2 + pitch * max(len(rows), 1), 360)
    height = 420
    title = escape(panel.name if panel else "Panel")
    total = sum(r.load_va for r in rows)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'font-family="Segoe UI, Arial, sans-serif" font-size="12">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{left}" y="28" font-size="16" font-weight="bold">{title} - single line diagram</text>',
        f'<text x="{left}" y="48">Incoming supply {VOLTAGE:.0f} V 1~, connected load {total:,.0f} VA</text>',
        f'<line x1="{width / 2}" y1="60" x2="{width / 2}" y2="{bus_y}" stroke="black" stroke-width="2"/>',
        f'<line x1="{left - 20}" y1="{bus_y}" x2="{width - left + 20}" y2="{bus_y}" stroke="black" stroke-width="5"/>',
    ]
    for i, row in enumerate(rows):
        x = left + pitch * i + pitch / 2
        color = "black" if row.ok else "#c62828"
        parts += [
            f'<line x1="{x}" y1="{bus_y}" x2="{x}" y2="{bus_y + 40}" stroke="{color}" stroke-width="2"/>',
            # breaker symbol: a small square with a diagonal
            f'<rect x="{x - 8}" y="{bus_y + 40}" width="16" height="16" fill="white" stroke="{color}"/>',
            f'<line x1="{x - 8}" y1="{bus_y + 56}" x2="{x + 8}" y2="{bus_y + 40}" stroke="{color}"/>',
            f'<line x1="{x}" y1="{bus_y + 56}" x2="{x}" y2="{bus_y + 150}" stroke="{color}" stroke-width="2"/>',
            f'<circle cx="{x}" cy="{bus_y + 160}" r="10" fill="white" stroke="{color}"/>',
            f'<text x="{x + 14}" y="{bus_y + 53}">{row.breaker_a:g} A</text>',
            f'<text x="{x}" y="{bus_y + 190}" text-anchor="middle" font-weight="bold">{escape(row.name)}</text>',
            f'<text x="{x}" y="{bus_y + 206}" text-anchor="middle">{row.kind}, {row.points} pts</text>',
            f'<text x="{x}" y="{bus_y + 222}" text-anchor="middle">{row.load_va:,.0f} VA / {row.design_current:.1f} A</text>',
            f'<text x="{x}" y="{bus_y + 238}" text-anchor="middle">{row.cable_mm2:g} mm² · {row.length_m:g} m</text>',
            f'<text x="{x}" y="{bus_y + 254}" text-anchor="middle" fill="{color}">Vd {row.voltage_drop:.1%}</text>',
        ]
    parts.append("</svg>")
    return "\n".join(parts)


def circuit_color(index: int) -> tuple[float, float, float]:
    hue = (index * 0.61803) % 1.0
    return tuple(round(0.5 + 0.45 * math.cos(2 * math.pi * (hue + k / 3)), 3) for k in range(3))  # type: ignore[return-value]
