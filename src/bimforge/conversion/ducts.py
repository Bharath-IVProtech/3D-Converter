"""HVAC ducts from their outlines and size labels.

A duct is drawn as two parallel outline lines the duct's width apart, usually
in a discipline colour, with a label such as "800x300 EA" and "B.O.D 4150"
(bottom of duct, mm above the level). Labels are the key evidence: an outline
pair counts as a duct when its spacing matches a labelled width.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace

from bimforge.conversion.settings import ConversionSettings
from bimforge.conversion.walls import WallCandidate, merge_collinear, pair_wall_faces
from bimforge.core.model import Point2, SystemType
from bimforge.drawing.primitives import Segment, Text
from bimforge.geometry import vec2 as v

WIDTH_TOLERANCE = 0.02  # m, between outline spacing and labelled width
MIN_RUN = 0.3  # m
FAN_GAP = 1.0  # m: fans and dampers split a duct's outline; runs on one line are joined across them
LONG_RUN = 1.0  # m
MIN_STYLE_LENGTH = 1.5  # m of long runs (after joining pieces) before a line style is believed
ELBOW_REACH = 1.5  # widths: how far a run may be extended to meet another at a corner
BOD_REACH = 1.0  # m, from a size label to its bottom-of-duct note
DEFAULT_DEPTH = 0.3
DEFAULT_BOTTOM = 2.7
NEUTRAL = ("black", "grey", "white", "blue")  # dimensions and text, not discipline colours

_SIZE = re.compile(r"(?<![\dxX×:.])(\d{2,4})\s*[xX×]\s*(\d{2,4})(?![\dxX×])")
_KIND = re.compile(r"\b(SA|EA|FA|RA)\b")
_BOD = re.compile(r"\bB\.?\s?O\.?\s?D\.?\b", re.I)
_NUMBER = re.compile(r"\b(\d{3,5})\b")


# Label word -> (system type, system name, display colour)
AIR_SYSTEMS = {
    "SA": (SystemType.SUPPLY_AIR, "Supply air", (0.2, 0.75, 0.3)),
    "EA": (SystemType.EXHAUST_AIR, "Exhaust air", (0.1, 0.75, 0.85)),
    "FA": (SystemType.FRESH_AIR, "Fresh air", (0.55, 0.85, 0.3)),
    "RA": (SystemType.RETURN_AIR, "Return air", (0.75, 0.45, 0.8)),
}


@dataclass
class DuctLabel:
    position: Point2  # metres
    width: float
    depth: float
    kind: str  # SA, EA, FA, RA or ""
    bottom: float | None = None


@dataclass
class DuctCandidate:
    start: Point2
    end: Point2
    width: float
    depth: float
    bottom: float
    kind: str
    confidence: float
    labelled: bool


def parse_labels(texts: Sequence[Text], scale: float) -> list[DuctLabel]:
    """Duct size labels. Text may be one string ("800x300 EA") or separate words (PDF).

    A size needs an air-kind word beside it, which rules out cutouts and grilles.
    """
    words = [(t.text, (t.position[0] * scale, t.position[1] * scale), t.height * scale) for t in texts]
    labels: list[DuctLabel] = []
    for text, pos, height in words:
        match = _SIZE.search(text)
        if match is None:
            continue
        line = max(height, 0.05) * 1.2
        beside = [w for w, p, _ in words if abs(p[1] - pos[1]) <= line and 0 <= p[0] - pos[0] <= 2.5]
        kind = _KIND.search(" ".join([text] + beside[:3]))
        if kind is None:
            continue
        label = DuctLabel(pos, int(match[1]) / 1000, int(match[2]) / 1000, kind[1])
        label.bottom = _bottom_near(text, pos, words)
        labels.append(label)
    return labels


def _bottom_near(text: str, pos: Point2, words: list) -> float | None:
    """The number after "B.O.D", in the same text or on a nearby line."""
    inline = _BOD.search(text)
    if inline and (number := _NUMBER.search(text, inline.end())):
        return int(number[1]) / 1000
    notes = [(math.dist(pos, p), p) for w, p, _ in words if _BOD.search(w) and math.dist(pos, p) <= BOD_REACH]
    if not notes:
        return None
    _, note = min(notes, key=lambda n: n[0])
    after = sorted((p[0], w) for w, p, _ in words if abs(p[1] - note[1]) <= 0.1 and 0 < p[0] - note[0] <= 2.0)
    for _, word in after:
        if number := _NUMBER.search(word):
            return int(number[1]) / 1000
    return None


def _weight_of(style: str) -> float:
    try:
        return float(style.split(" pt ", 1)[0]) if " pt " in style else 0.0
    except ValueError:
        return 0.0


def _colour_of(style: str) -> str:
    return style.split(" pt ", 1)[1] if " pt " in style else style


def detect_ducts(
    segments: Sequence[Segment], labels: Sequence[DuctLabel], settings: ConversionSettings,
    duct_layers: Sequence[str] = (),
) -> list[DuctCandidate]:
    """Segments in metres. Without labels, only layers named as ducts are used."""
    pairing = replace(settings, min_wall_thickness=0.1, max_wall_thickness=2.0, min_wall_overlap=MIN_RUN,
                      wall_layers=())
    widths = sorted({label.width for label in labels})
    by_style: dict[str, dict[tuple, Segment]] = defaultdict(dict)
    for seg in segments:
        named = seg.layer in duct_layers
        coloured = " pt " in seg.layer and not _colour_of(seg.layer).startswith(NEUTRAL)
        if (named or (widths and coloured)) and seg.length >= MIN_RUN:
            a, b = sorted(((round(seg.start[0], 3), round(seg.start[1], 3)), (round(seg.end[0], 3), round(seg.end[1], 3))))
            by_style[seg.layer][(a, b)] = seg  # each outline is often drawn twice

    # The outline style of each colour is its heaviest line weight with long runs at labelled widths.
    # Total length alone would favour inner lines (flanges, grilles) and thin key-plan outlines.
    best: dict[str, tuple[tuple[float, float], list[WallCandidate]]] = {}
    for style, unique in by_style.items():
        lines = list(unique.values())
        if widths:
            # One pass per labelled width: nearest-first pairing across all widths would let an
            # outline pair with the dashed centre line beside it instead of its true partner.
            runs = [r for w in widths for r in pair_wall_faces(
                lines, replace(pairing, min_wall_thickness=w - WIDTH_TOLERANCE, max_wall_thickness=w + WIDTH_TOLERANCE),
                longest_first=True)]
        else:
            runs = pair_wall_faces(lines, pairing)
        runs = merge_collinear(runs, FAN_GAP, pairing)
        total = sum(r.length for r in runs if r.length >= LONG_RUN)
        colour = _colour_of(style)
        rank = (_weight_of(style), total)
        if total >= MIN_STYLE_LENGTH and (colour not in best or rank > best[colour][0]):
            best[colour] = (rank, runs)

    found: list[DuctCandidate] = []
    for _, runs in best.values():
        merged = _drop_nested(runs)
        size = _sizer(labels, merged)
        for run in merged:
            found.append(size(run))
    return _join_elbows(found)


def _drop_nested(runs: list[WallCandidate]) -> list[WallCandidate]:
    """Remove stubs shorter than their own width, and short runs lying inside a longer duct (grilles, flanges)."""
    kept = [r for r in runs if r.length >= r.thickness]
    def inside(r: WallCandidate, o: WallCandidate) -> bool:
        mid = ((r.start[0] + r.end[0]) / 2, (r.start[1] + r.end[1]) / 2)
        return o.length > r.length and v.point_segment_distance(mid, o.start, o.end) <= o.thickness / 2
    return [r for r in kept if not any(inside(r, o) for o in kept if o is not r)]


def _sizer(labels: Sequence[DuctLabel], runs: Sequence[WallCandidate]):
    """Takes depth, kind and level from the nearest label of the same width."""
    def distance(label: DuctLabel, run: WallCandidate) -> float:
        return v.point_segment_distance(label.position, run.start, run.end)

    # Labels of this colour tell an unlabelled run its likely kind and level.
    nearby = [min(labels, key=lambda lb: distance(lb, r)) for r in runs if labels]
    kinds = Counter(lb.kind for lb in nearby if lb.kind)
    usual_kind = kinds.most_common(1)[0][0] if kinds else ""
    bottoms = [lb.bottom for lb in nearby if lb.bottom is not None]
    usual_bottom = statistics.median(bottoms) if bottoms else DEFAULT_BOTTOM

    def size(run: WallCandidate) -> DuctCandidate:
        same = [lb for lb in labels if abs(lb.width - run.thickness) <= WIDTH_TOLERANCE]
        if not same:
            return DuctCandidate(run.start, run.end, round(run.thickness, 3), DEFAULT_DEPTH, usual_bottom, usual_kind,
                                 0.6, False)
        label = min(same, key=lambda lb: distance(lb, run))
        bottom = label.bottom if label.bottom is not None else usual_bottom
        return DuctCandidate(run.start, run.end, label.width, label.depth, bottom, label.kind or usual_kind, 0.85, True)

    return size


def _join_elbows(ducts: list[DuctCandidate]) -> list[DuctCandidate]:
    """Extend runs of one air system to meet at corners (elbows are modelled as square corners)."""
    for a in ducts:
        for b in ducts:
            if a is b or a.kind != b.kind:
                continue
            x = v.line_intersection(a.start, v.sub(a.end, a.start), b.start, v.sub(b.end, b.start))
            if x is None:
                continue
            reach = ELBOW_REACH * max(a.width, b.width)
            if min(math.dist(b.start, x), math.dist(b.end, x)) > reach:
                continue
            if math.dist(a.start, x) <= reach and math.dist(a.start, x) <= math.dist(a.end, x):
                a.start = x
            elif math.dist(a.end, x) <= reach:
                a.end = x
    return ducts
