"""Clash detection between element sets.

Broad phase: axis-aligned bounding boxes (vectorised NumPy overlap test).
Narrow phase: FCL (BSD) triangle-mesh collision and distance queries, plus a
ray-parity containment test, because surface collision alone misses an
element sitting entirely inside another (a pipe buried in a slab).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from bimforge.core.model import (
    Element,
    Fixture,
    ImportedElement,
    Issue,
    IssueStatus,
    Opening,
    Project,
    Slab,
    Space,
    Wall,
)
from bimforge.disciplines.catalog import family_of
from bimforge.geometry.builders import Tessellator
from bimforge.geometry.mesh import Mesh

CLASH_KINDS = ("hard", "clearance")


def element_category(element: Element) -> str:
    if isinstance(element, ImportedElement):
        return element.category
    if isinstance(element, Fixture):
        family = family_of(element)
        return family.discipline.value if family else "architecture"
    return element.discipline.value


@dataclass(frozen=True)
class ClashTest:
    name: str
    set_a: tuple[str, ...]  # element categories
    set_b: tuple[str, ...]
    clearance: float = 0.0  # > 0 also reports elements closer than this (m)
    default: bool = True

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


DEFAULT_TESTS: tuple[ClashTest, ...] = (
    ClashTest("Plumbing vs Structure", ("plumbing",), ("structure",)),
    ClashTest("Electrical vs Structure", ("electrical",), ("structure",)),
    ClashTest("Plumbing vs Electrical", ("plumbing",), ("electrical",), clearance=0.1),
    ClashTest("MEP vs Mechanical", ("plumbing", "electrical"), ("mechanical",)),
    ClashTest("Mechanical vs Structure", ("mechanical",), ("structure",)),
    # Off by default: every pipe through a wall is reported until sleeves are modelled.
    ClashTest("MEP vs Architecture", ("plumbing", "electrical", "mechanical"), ("architecture",), default=False),
)


@dataclass
class ClashResult:
    test: str
    a_id: str
    b_id: str
    kind: str  # "hard" or "clearance"
    point: tuple[float, float, float]
    distance: float = 0.0


def _excluded(a: Element, b: Element) -> bool:
    """Pairs that touch by design."""
    if isinstance(a, Opening) and a.host_id == b.id or isinstance(b, Opening) and b.host_id == a.id:
        return True
    # Fixtures are mounted on walls and stand on slabs.
    return isinstance(a, Fixture) and isinstance(b, (Wall, Slab)) or isinstance(b, Fixture) and isinstance(a, (Wall, Slab))


def _ray_hits(origin: np.ndarray, direction: np.ndarray, tris: np.ndarray) -> int:
    """Möller-Trumbore, vectorised over triangles: number of hits in front of the origin."""
    v0, v1, v2 = tris[:, 0], tris[:, 1], tris[:, 2]
    e1, e2 = v1 - v0, v2 - v0
    p = np.cross(direction, e2)
    det = np.einsum("ij,ij->i", e1, p)
    ok = np.abs(det) > 1e-12
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    t_vec = origin - v0
    u = np.einsum("ij,ij->i", t_vec, p) * inv
    q = np.cross(t_vec, e1)
    w = np.einsum("j,ij->i", direction, q) * inv
    t = np.einsum("ij,ij->i", e2, q) * inv
    return int(np.count_nonzero(ok & (u >= 0) & (w >= 0) & (u + w <= 1) & (t > 1e-9)))


def point_inside(point: np.ndarray, mesh: Mesh) -> bool:
    # An irrational-ish direction avoids grazing edges and vertices of axis-aligned geometry.
    direction = np.array([0.5773, 0.5772, 0.5775])
    return _ray_hits(point, direction / np.linalg.norm(direction), mesh.vertices[mesh.faces]) % 2 == 1


class ClashDetector:
    def __init__(self, project: Project, tessellator: Tessellator) -> None:
        import fcl  # optional dependency, imported lazily

        self._fcl = fcl
        self.project = project
        self.tessellator = tessellator
        self._meshes: dict[str, Mesh | None] = {}
        self._objects: dict[str, object] = {}

    def mesh(self, element: Element) -> Mesh | None:
        if element.id not in self._meshes:
            mesh = None if isinstance(element, Space) else self.tessellator.mesh_for(element, self.project)
            self._meshes[element.id] = mesh if mesh is not None and not mesh.is_empty else None
        return self._meshes[element.id]

    def _object(self, element_id: str, mesh: Mesh):
        if element_id not in self._objects:
            fcl = self._fcl
            model = fcl.BVHModel()
            model.beginModel(len(mesh.vertices), len(mesh.faces))
            model.addSubModel(mesh.vertices.astype(np.float64), mesh.faces.astype(np.int32))
            model.endModel()
            self._objects[element_id] = fcl.CollisionObject(model, fcl.Transform())
        return self._objects[element_id]

    def _members(self, categories: Sequence[str]) -> list[tuple[Element, Mesh]]:
        out = []
        for element in self.project.elements():
            if element_category(element) in categories and (mesh := self.mesh(element)) is not None:
                out.append((element, mesh))
        return out

    def run(self, test: ClashTest) -> list[ClashResult]:
        fcl = self._fcl
        set_a, set_b = self._members(test.set_a), self._members(test.set_b)
        if not set_a or not set_b:
            return []
        b_lo = np.array([m.vertices.min(axis=0) for _, m in set_b])
        b_hi = np.array([m.vertices.max(axis=0) for _, m in set_b])
        margin = test.clearance
        results: list[ClashResult] = []
        seen: set[frozenset[str]] = set()
        for a, mesh_a in set_a:
            a_lo, a_hi = mesh_a.vertices.min(axis=0), mesh_a.vertices.max(axis=0)
            candidates = np.nonzero(np.all(b_lo <= a_hi + margin, axis=1) & np.all(b_hi >= a_lo - margin, axis=1))[0]
            for index in candidates:
                b, mesh_b = set_b[index]
                pair = frozenset((a.id, b.id))
                if a.id == b.id or pair in seen or _excluded(a, b):
                    continue
                seen.add(pair)
                oa, ob = self._object(a.id, mesh_a), self._object(b.id, mesh_b)
                request = fcl.CollisionRequest(num_max_contacts=1, enable_contact=True)
                result = fcl.CollisionResult()
                if fcl.collide(oa, ob, request, result):
                    # FCL's mesh-mesh contact position is an arbitrary triangle point; the centre of the
                    # bounding-box overlap is a stable, meaningful place to zoom to.
                    lo = np.maximum(a_lo, b_lo[index])
                    hi = np.minimum(a_hi, b_hi[index])
                    point = tuple(float(c) for c in (lo + hi) / 2)
                    results.append(ClashResult(test.name, a.id, b.id, "hard", point))
                    continue
                inside = self._contained(mesh_a, mesh_b)
                if inside is not None:
                    results.append(ClashResult(test.name, a.id, b.id, "hard", inside))
                    continue
                if margin > 0:
                    d_request = fcl.DistanceRequest(enable_nearest_points=True)
                    d_result = fcl.DistanceResult()
                    distance = fcl.distance(oa, ob, d_request, d_result)
                    if distance < margin:
                        p, q = (np.asarray(x, dtype=float) for x in d_result.nearest_points)
                        mid = tuple(float(c) for c in (p + q) / 2)
                        results.append(ClashResult(test.name, a.id, b.id, "clearance", mid, round(float(distance), 4)))
        return results

    @staticmethod
    def _contained(a: Mesh, b: Mesh) -> tuple[float, float, float] | None:
        for inner, outer in ((a, b), (b, a)):
            lo, hi = outer.vertices.min(axis=0), outer.vertices.max(axis=0)
            if np.all(inner.vertices.min(axis=0) >= lo) and np.all(inner.vertices.max(axis=0) <= hi):
                centre = inner.vertices.mean(axis=0)
                if point_inside(centre, outer):
                    return tuple(float(c) for c in centre)
        return None


def _issue_key(issue: Issue) -> tuple:
    title = issue.title if issue.kind not in CLASH_KINDS else ""
    return (issue.test, frozenset(issue.element_ids), issue.kind in CLASH_KINDS, title)


def results_to_issues(project: Project, results: Iterable[ClashResult]) -> list[Issue]:
    def label(element_id: str) -> str:
        e = project.get(element_id)
        return (e.name or type(e).__name__) if e else element_id[:8]

    issues = []
    for r in results:
        what = "clearance" if r.kind == "clearance" else "clash"
        extra = f" ({r.distance * 1000:.0f} mm)" if r.kind == "clearance" else ""
        issues.append(Issue(title=f"{label(r.a_id)} × {label(r.b_id)} {what}{extra}", kind=r.kind,
                            element_ids=(r.a_id, r.b_id), point=r.point, distance=r.distance, test=r.test))
    return issues


def merge_issues(existing: dict[str, Issue], found: Iterable[Issue], tests_run: Iterable[str]) -> dict[str, Issue]:
    """Update the issue list after re-running tests, keeping user status and comments.

    Found again: keeps id/status/comment (a resolved issue reopens as active, new becomes active).
    Not found any more: open issues from the re-run tests become resolved.
    Returns a new dict; `existing` is not modified (so the change can be undone).
    """
    out = {i: dataclasses.replace(issue) for i, issue in existing.items()}
    by_key = {_issue_key(issue): issue.id for issue in out.values()}
    seen: set[str] = set()
    for issue in found:
        match = by_key.get(_issue_key(issue))
        if match is None:
            out[issue.id] = issue
            seen.add(issue.id)
            continue
        current = out[match]
        seen.add(match)
        current.point, current.distance, current.title = issue.point, issue.distance, issue.title
        if current.status in (IssueStatus.NEW, IssueStatus.RESOLVED):
            current.status = IssueStatus.ACTIVE
    tests = set(tests_run)
    for issue in out.values():
        if issue.test in tests and issue.id not in seen and issue.status in (IssueStatus.NEW, IssueStatus.ACTIVE):
            issue.status = IssueStatus.RESOLVED
    return out
