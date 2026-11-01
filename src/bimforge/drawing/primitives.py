"""2D primitives in drawing units.

DXF, DWG, DGN, vector PDF and vectorised raster importers all emit a
Drawing2D, so the 2D->3D pipeline is written once against this model.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field

from bimforge.core.model import Point2
from bimforge.core.units import LengthUnit


@dataclass(frozen=True, kw_only=True)
class Primitive:
    layer: str = "0"
    handle: str = ""  # source entity id, kept for traceability in the review UI
    block: str = ""  # name of the block this was exploded from ("" = drawn directly)


@dataclass(frozen=True, kw_only=True)
class Segment(Primitive):
    start: Point2
    end: Point2

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    def scaled(self, factor: float) -> Segment:
        return Segment(
            start=(self.start[0] * factor, self.start[1] * factor),
            end=(self.end[0] * factor, self.end[1] * factor),
            layer=self.layer,
            handle=self.handle,
            block=self.block,
        )


@dataclass(frozen=True, kw_only=True)
class Polyline(Primitive):
    points: tuple[Point2, ...]
    closed: bool = False
    width: float = 0.0  # constant line width; a wide polyline is often a wall drawn as its centreline
    filled: bool = False  # a solid-filled shape (poche wall, column, hatch boundary)

    def segments(self) -> Iterator[Segment]:
        pts = list(self.points) + ([self.points[0]] if self.closed and self.points else [])
        for a, b in zip(pts, pts[1:]):
            if a != b:
                yield Segment(start=a, end=b, layer=self.layer, handle=self.handle, block=self.block)


@dataclass(frozen=True, kw_only=True)
class Arc(Primitive):
    center: Point2
    radius: float
    start_angle: float = 0.0  # degrees, CCW
    end_angle: float = 360.0


@dataclass(frozen=True, kw_only=True)
class Text(Primitive):
    position: Point2
    text: str
    height: float = 0.0
    rotation: float = 0.0


@dataclass(frozen=True, kw_only=True)
class BlockRef(Primitive):
    """A block/cell/symbol instance: the main carrier of doors, windows and MEP symbols."""

    name: str
    position: Point2
    rotation: float = 0.0  # degrees
    scale: tuple[float, float] = (1.0, 1.0)
    extent: tuple[Point2, Point2] | None = None  # block-local bounding box, unscaled
    attributes: dict[str, str] = field(default_factory=dict)

    def to_world(self, local: Point2) -> Point2:
        x, y = local[0] * self.scale[0], local[1] * self.scale[1]
        c, s = math.cos(math.radians(self.rotation)), math.sin(math.radians(self.rotation))
        return (self.position[0] + x * c - y * s, self.position[1] + x * s + y * c)

    @property
    def world_center(self) -> Point2:
        if self.extent is None:
            return self.position
        (x0, y0), (x1, y1) = self.extent
        return self.to_world(((x0 + x1) / 2, (y0 + y1) / 2))

    @property
    def width(self) -> float | None:
        """Extent along the block's local x axis, which by convention runs along the host wall."""
        if self.extent is None:
            return None
        return abs((self.extent[1][0] - self.extent[0][0]) * self.scale[0])


@dataclass(frozen=True, kw_only=True)
class Dimension(Primitive):
    p1: Point2
    p2: Point2
    measurement: float
    text: str = ""


@dataclass(kw_only=True)
class Layer:
    name: str
    color: int = 7
    linetype: str = "Continuous"
    visible: bool = True


@dataclass(kw_only=True)
class Drawing2D:
    source: str = ""
    unit: LengthUnit | None = None  # None = unitless; the pipeline guesses or uses an override
    layers: dict[str, Layer] = field(default_factory=dict)
    primitives: list[Primitive] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def of_type(self, kind: type[Primitive]) -> list:
        return [p for p in self.primitives if isinstance(p, kind)]

    def is_visible(self, primitive: Primitive) -> bool:
        layer = self.layers.get(primitive.layer)
        return layer.visible if layer else True

    def segments(self) -> Iterator[Segment]:
        """All straight segments, exploding polylines."""
        for p in self.primitives:
            if isinstance(p, Segment):
                yield p
            elif isinstance(p, Polyline):
                yield from p.segments()

    def extent(self) -> tuple[Point2, Point2] | None:
        pts = [pt for s in self.segments() for pt in (s.start, s.end)]
        pts += [b.position for b in self.of_type(BlockRef)]
        if not pts:
            return None
        xs, ys = zip(*pts)
        return (min(xs), min(ys)), (max(xs), max(ys))
