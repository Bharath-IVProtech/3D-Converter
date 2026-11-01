"""Rule-based model checks (code compliance), reported as issues."""

from __future__ import annotations

from bimforge.core.model import Issue, Project
from bimforge.disciplines import electrical, plumbing

CHECKS = {
    "Plumbing rules": plumbing.check,
    "Electrical rules": electrical.check,
}


def run_checks(project: Project) -> list[Issue]:
    issues: list[Issue] = []
    for check in CHECKS.values():
        issues += check(project)
    return issues
