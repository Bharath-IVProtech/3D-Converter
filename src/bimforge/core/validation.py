"""Model integrity rules checked before any edit is committed."""

from __future__ import annotations

from bimforge.core.model import Column, DuctSegment, Element, Opening, Point2, Project, Slab, Space, Stair, Wall

EPS = 1e-9


def _area(points: list[Point2]) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]))) / 2


class ValidationError(ValueError):
    pass


def validate(element: Element, project: Project) -> list[str]:
    label = element.name or type(element).__name__
    errors: list[str] = []
    if isinstance(element, Wall):
        if element.length <= 1e-6:
            errors.append(f"{label}: length must be positive")
        if element.thickness <= 0:
            errors.append(f"{label}: thickness must be positive")
        if element.height <= 0:
            errors.append(f"{label}: height must be positive")
    elif isinstance(element, Opening):
        host = project.get(element.host_id)
        if not isinstance(host, Wall):
            return [f"{label}: host wall not found"]
        if element.width <= 0 or element.height <= 0:
            errors.append(f"{label}: width and height must be positive")
        s0, s1 = element.span
        if s0 < -EPS or s1 > host.length + EPS:
            errors.append(f"{label}: extends beyond its host wall ({host.length:.3f} m long)")
        if element.sill_height < -EPS or element.sill_height + element.height > host.height + EPS:
            errors.append(f"{label}: does not fit within the wall height ({host.height:.3f} m)")
        for other in project.hosted_openings(host.id):
            o0, o1 = other.span
            if other.id != element.id and s0 < o1 - EPS and o0 < s1 - EPS:
                errors.append(f"{label}: overlaps {other.name or 'another opening'}")
    elif isinstance(element, Stair):
        if element.length <= 0.1 or element.width <= 0:
            errors.append(f"{label}: length and width must be positive")
        if element.risers < 2 or element.height <= 0:
            errors.append(f"{label}: needs at least 2 risers and a positive height")
    elif isinstance(element, DuctSegment):
        if element.width <= 0 or element.depth <= 0 or len(element.path) < 2:
            errors.append(f"{label}: needs a positive size and at least 2 path points")
    elif isinstance(element, Column):
        if min(element.width, element.depth, element.height) <= 0:
            errors.append(f"{label}: width, depth and height must be positive")
    elif isinstance(element, (Slab, Space)):
        if len(element.boundary) < 3 or _area(list(element.boundary)) <= 1e-6:
            errors.append(f"{label}: boundary must enclose an area")
        if isinstance(element, Slab) and element.thickness <= 0:
            errors.append(f"{label}: thickness must be positive")
        if isinstance(element, Space) and element.height <= 0:
            errors.append(f"{label}: height must be positive")
    if element.storey_id is not None and project.storey(element.storey_id) is None:
        errors.append(f"{label}: unknown storey")
    system_id = getattr(element, "system_id", None)
    if system_id is not None and system_id not in project.systems:
        errors.append(f"{label}: unknown system")
    return errors


def validate_with_dependents(element: Element, project: Project) -> list[str]:
    """Also re-checks openings hosted by a wall, since changing the wall can invalidate them."""
    errors = validate(element, project)
    if isinstance(element, Wall):
        for opening in project.hosted_openings(element.id):
            errors += validate(opening, project)
    return errors
