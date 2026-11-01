"""Plumbing engineering: sizing tables, slopes, network sizing and code checks.

Tables are indicative defaults based on the IPC (International Plumbing Code):
  * Table 710.1(2) horizontal fixture branch capacity (DFU) by pipe size,
  * Section 704.1 minimum drainage slope,
  * Appendix E Table E103.3(3) demand (gpm) for flush-tank systems by WSFU.
Supply pipes are sized by velocity using EN 1057 copper tube bores.
Regional code packs (IS 1172 / BS EN 12056 / EN 806) can replace these tables.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass

from bimforge.core.model import Fixture, Issue, PipeSegment, Point3, Project, SystemType
from bimforge.disciplines.catalog import connection_point, family_of, fixture_value

DRAINAGE_TYPES = {SystemType.SANITARY, SystemType.STORMWATER}
SUPPLY_TYPES = {SystemType.COLD_WATER, SystemType.HOT_WATER}

# (nominal DN mm, max DFU on a horizontal branch) — IPC Table 710.1(2), inch sizes mapped to DN.
DRAIN_CAPACITY: list[tuple[int, float]] = [
    (32, 1), (40, 3), (50, 6), (65, 12), (80, 20), (100, 160), (125, 360), (150, 620), (200, 1400), (250, 2500),
    (300, 3900),
]

# WSFU -> demand in US gpm, IPC Table E103.3(3) (predominantly flush tanks).
_WSFU_GPM: list[tuple[float, float]] = [
    (1, 3.0), (2, 5.0), (3, 6.5), (4, 8.0), (5, 9.4), (6, 10.7), (8, 12.8), (10, 14.6), (12, 16.0), (15, 17.5),
    (20, 19.6), (25, 21.5), (30, 23.3), (40, 26.3), (50, 29.1), (60, 32.0), (80, 38.0), (100, 43.5),
]
GPM_TO_LPS = 0.0630902

# Copper tube EN 1057: (outside diameter mm, bore mm).
COPPER_TUBES: list[tuple[float, float]] = [
    (15, 13.6), (22, 20.2), (28, 26.2), (35, 32.6), (42, 39.6), (54, 51.6), (76.1, 73.1), (108, 105.0),
]
MAX_SUPPLY_VELOCITY = 2.0  # m/s
CONNECT_TOLERANCE = 0.05  # m, for joining pipes and fixtures into a network
SLOPE_TOLERANCE = 1e-4


def drain_size_for(dfu: float, min_dn: int = 0) -> int:
    for dn, capacity in DRAIN_CAPACITY:
        if dn >= min_dn and dfu <= capacity:
            return dn
    raise ValueError(f"{dfu} DFU exceeds the largest tabulated drain size")


def min_drain_slope(dn_mm: float) -> float:
    """IPC 704.1: 1/4 in/ft up to 2-1/2 in, 1/8 in/ft for 3-6 in, 1/16 in/ft for 8 in and larger (rounded)."""
    if dn_mm <= 65:
        return 0.02
    if dn_mm <= 150:
        return 0.01
    return 0.005


def supply_flow_lps(wsfu: float) -> float:
    if wsfu <= 0:
        return 0.0
    xs = [x for x, _ in _WSFU_GPM]
    if wsfu >= xs[-1]:
        gpm = _WSFU_GPM[-1][1] * math.sqrt(wsfu / xs[-1])  # gentle extrapolation beyond the table
    elif wsfu <= xs[0]:
        gpm = _WSFU_GPM[0][1] * wsfu / xs[0]
    else:
        i = bisect.bisect_left(xs, wsfu)
        (x0, y0), (x1, y1) = _WSFU_GPM[i - 1], _WSFU_GPM[i]
        gpm = y0 + (y1 - y0) * (wsfu - x0) / (x1 - x0)
    return gpm * GPM_TO_LPS


def supply_size_for(wsfu: float, max_velocity: float = MAX_SUPPLY_VELOCITY) -> tuple[float, float]:
    """Smallest copper tube whose velocity stays within the limit: (outside diameter mm, velocity m/s)."""
    flow = supply_flow_lps(wsfu) / 1000  # m3/s
    for od, bore in COPPER_TUBES:
        velocity = flow / (math.pi * (bore / 1000) ** 2 / 4)
        if velocity <= max_velocity:
            return od, velocity
    raise ValueError(f"{wsfu} WSFU exceeds the largest tabulated supply tube")


# --- geometry helpers ------------------------------------------------------------


def _horizontal(a: Point3, b: Point3) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def leg_slopes(path: Sequence[Point3]) -> list[float | None]:
    """Fall per unit horizontal length for each leg (positive = falling); None for vertical legs."""
    out: list[float | None] = []
    for a, b in zip(path, path[1:]):
        run = _horizontal(a, b)
        out.append(None if run < 1e-6 else (a[2] - b[2]) / run)
    return out


def apply_slope(path: Sequence[Point3], slope: float, start_z: float | None = None) -> list[Point3]:
    """Re-grade a run to fall at `slope` from its start; vertical legs keep their drop."""
    if not path:
        return []
    z = path[0][2] if start_z is None else start_z
    out = [(path[0][0], path[0][1], z)]
    for a, b in zip(path, path[1:]):
        run = _horizontal(a, b)
        z = z - slope * run if run >= 1e-6 else z + (b[2] - a[2])
        out.append((b[0], b[1], round(z, 6)))
    return out


def path_length(path: Sequence[Point3]) -> float:
    return sum(math.dist(a, b) for a, b in zip(path, path[1:]))


def _point_on_path(p: Point3, path: Sequence[Point3], tol: float = CONNECT_TOLERANCE) -> bool:
    for a, b in zip(path, path[1:]):
        ab = [b[i] - a[i] for i in range(3)]
        denom = sum(c * c for c in ab)
        t = 0.0 if denom == 0 else max(0.0, min(1.0, sum((p[i] - a[i]) * ab[i] for i in range(3)) / denom))
        if math.dist(p, [a[i] + ab[i] * t for i in range(3)]) <= tol:
            return True
    return False


# --- network sizing -----------------------------------------------------------------


@dataclass
class PipeSizing:
    pipe_id: str
    fixture_units: float
    size_mm: float
    note: str = ""


def _world_path(project: Project, pipe: PipeSegment) -> list[Point3]:
    z0 = project.storey_elevation(pipe)
    return [(x, y, z + z0) for x, y, z in pipe.path]


def size_network(project: Project, system_id: str) -> list[PipeSizing]:
    """Size every pipe in a system from the fixture units it carries.

    Flow direction follows each pipe's path: drainage runs from fixtures
    (start) to the stack (end); supply runs from the source (start) to the
    fixtures (end). A pipe carries the units of the fixtures attached at its
    upstream/downstream end plus those of every pipe joining it.
    """
    system = project.systems[system_id]
    drainage = system.system_type in DRAINAGE_TYPES
    pipes = [e for e in project.elements_of(PipeSegment) if e.system_id == system_id and len(e.path) >= 2]
    fixtures = [f for f in project.elements_of(Fixture) if f.system_id == system_id]
    paths = {p.id: _world_path(project, p) for p in pipes}
    ports = {f.id: connection_point(f, project.storey_elevation(f)) for f in fixtures}

    def fixture_end(pipe_id: str) -> Point3:
        return paths[pipe_id][0] if drainage else paths[pipe_id][-1]

    def joining_end(pipe_id: str) -> Point3:
        return paths[pipe_id][-1] if drainage else paths[pipe_id][0]

    # pipe id -> (fixture units carried, smallest permitted drain DN)
    memo: dict[str, tuple[float, int]] = {}

    def walk(pipe_id: str, stack: frozenset[str] = frozenset()) -> tuple[float, int]:
        if pipe_id in memo:
            return memo[pipe_id]
        if pipe_id in stack:
            return (0.0, 0)  # loop in the network; break it
        attached = [f for f in fixtures if math.dist(ports[f.id], fixture_end(pipe_id)) <= 3 * CONNECT_TOLERANCE]
        total = sum(fixture_value(f, "dfu" if drainage else "wsfu") for f in attached)
        floor = max((fam.min_drain_dn for f in attached if (fam := family_of(f))), default=0)
        for other in pipes:
            if other.id != pipe_id and _point_on_path(joining_end(other.id), paths[pipe_id]):
                units, other_floor = walk(other.id, stack | {pipe_id})
                total += units
                if drainage:
                    # IPC 704.2: drains never reduce in size in the direction of flow.
                    floor = max(floor, drain_size_for(units, other_floor))
        memo[pipe_id] = (total, floor)
        return memo[pipe_id]

    results = []
    for pipe in pipes:
        fu, floor = walk(pipe.id)
        if drainage:
            results.append(PipeSizing(pipe.id, fu, drain_size_for(fu, floor)))
        else:
            od, velocity = supply_size_for(fu) if fu > 0 else (COPPER_TUBES[0][0], 0.0)
            results.append(PipeSizing(pipe.id, fu, od, f"{velocity:.2f} m/s"))
    return results


# --- code checks -------------------------------------------------------------------


def check(project: Project) -> list[Issue]:
    issues: list[Issue] = []
    for pipe in project.elements_of(PipeSegment):
        system = project.systems.get(pipe.system_id) if pipe.system_id else None
        if system is None:
            issues.append(Issue(title=f"{pipe.name or 'Pipe'} is not assigned to a system",
                                kind="code", element_ids=(pipe.id,), test="Plumbing rules"))
            continue
        if system.system_type in DRAINAGE_TYPES:
            required = min_drain_slope(pipe.diameter * 1000)
            flat = [s for s in leg_slopes(pipe.path) if s is not None and s < required - SLOPE_TOLERANCE]
            if flat:
                worst = min(flat)
                issues.append(Issue(
                    title=f"{pipe.name or 'Drain'}: slope {worst:.1%} below minimum {required:.1%}",
                    kind="code", element_ids=(pipe.id,), test="Plumbing rules",
                    point=tuple(_world_path(project, pipe)[0]),
                ))
    paths: dict[str, list[list[Point3]]] = {}
    for pipe in project.elements_of(PipeSegment):
        if pipe.system_id and len(pipe.path) >= 2:
            paths.setdefault(pipe.system_id, []).append(_world_path(project, pipe))
    for fixture in project.elements_of(Fixture):
        family = family_of(fixture)
        if not family or family.discipline.value != "plumbing":
            continue
        label = fixture.name or family.name
        if not fixture.system_id:
            issues.append(Issue(title=f"{label} is not assigned to a drainage/supply system",
                                kind="code", element_ids=(fixture.id,), test="Plumbing rules"))
            continue
        port = connection_point(fixture, project.storey_elevation(fixture))
        if not any(_point_on_path(port, path, 3 * CONNECT_TOLERANCE) for path in paths.get(fixture.system_id, [])):
            issues.append(Issue(title=f"{label} is not connected to any pipe of its system",
                                kind="code", element_ids=(fixture.id,), test="Plumbing rules", point=port))
    return issues
