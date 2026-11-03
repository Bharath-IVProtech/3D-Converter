"""Names written on the drawing: room labels, door and window tags, the title block.

A PDF stores text as separate words, and a room label sits among dimensions and duct notes, so
words are first grouped into blocks the way a reader would see them. Only then is a block judged
to be a name. Anything unclear is left unnamed, because a default name is better than a wrong one.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from bimforge.core.model import Point2
from bimforge.drawing.primitives import Text

CHAR_WIDTH = 0.62  # a character's width, as a share of the text height
LINE_PITCH = 1.6  # the next line starts within this many text heights
WORD_GAP = 1.0  # words on a line are at most this many text heights apart
SAME_SIZE = 1.3  # text more different in height than this is separate
DEFAULT_HEIGHT = 0.2  # m, for text that gives no height

MAX_NAME_WORDS = 8
TAG_REACH = 1.5  # m from an opening or column to its tag
STAIR_REACH = 3.0  # m from a stair to its label
LOUVRE_REACH = 3.0  # m from a louvre to its note

_DOOR_TAG = re.compile(r"^(D|DR|DW|FD|MD|SD)[-\s.]?\d+[A-Z]?$", re.I)
_WINDOW_TAG = re.compile(r"^(W|WN|WD|FW|SW|V)[-\s.]?\d+[A-Z]?$", re.I)
_COLUMN_TAG = re.compile(r"^(C|COL)[-\s.]?\d+[A-Z]?$", re.I)
_STAIR_NAME = re.compile(r"\bSTAIR|\bSTAIRCASE\b|^ST[-\s]?\d+$", re.I)
_LOUVRE_NOTE = re.compile(r"\bLOUV(ER|RE)\b", re.I)
_PROJECT_HEADING = re.compile(r"^PROJECT(\s+(TITLE|NAME))?\s*:?$", re.I)
_LEVEL_IN_TITLE = re.compile(r"\bPLANS?\s+AT\s+(.+?\bLEVEL)\b", re.I)

# What makes a block a note rather than a name.
_ANNOTATION = re.compile(
    r"\d+\s*[xX]\s*\d+"  # sizes: 425x300, cutout:900x900
    r"|\bB\.?O\.?D\b|\bF\.?F\.?L\b|\bT\.?O\.?[SC]\b|\bCFM\b|\bVCD\b"  # levels and air notes
    r"|\b(CUTOUT|LOUV(ER|RE)|GRILLES?|DUCT|PLENUM|FANS?|DIFFUSER|FHC|PIPES?|NOTES?|SCALE|DETAIL|SECTION|ABOVE|BELOW|RUNGS?)\b",
    re.I,
)
_WORD = re.compile(r"[A-Za-z]{3,}")


@dataclass(frozen=True)
class TextBlock:
    text: str
    centre: Point2  # m
    height: float  # m
    box: tuple[float, float, float, float]  # xmin, ymin, xmax, ymax in m


def _clean(text: str) -> str:
    return " ".join(text.split())


def group_text(texts: Sequence[Text], k: float) -> list[TextBlock]:
    """Words into blocks: near each other along a line or in the next line, at the same size and angle."""
    words = []
    for t in texts:
        text = _clean(t.text)
        if not text:
            continue
        a = math.radians(t.rotation)
        c, s = math.cos(a), math.sin(a)
        x, y = t.position[0] * k, t.position[1] * k
        height = t.height * k if t.height > 0 else DEFAULT_HEIGHT
        # u runs along the text, v across it, so rotated labels group just like level ones.
        u, v = x * c + y * s, -x * s + y * c
        words.append((text, u, v, height, round(t.rotation) % 360, c, s, u + len(text) * height * CHAR_WIDTH))

    parent = list(range(len(words)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Sorting by angle and then v keeps only near neighbours in the inner loop.
    order = sorted(range(len(words)), key=lambda i: (words[i][4], words[i][2]))
    for position, i in enumerate(order):
        _, u0, v0, h0, angle0, _, _, end0 = words[i]
        for j in order[position + 1:]:
            _, u1, v1, h1, angle1, _, _, end1 = words[j]
            h = max(h0, h1)
            if angle1 != angle0 or v1 - v0 > LINE_PITCH * h:
                break
            if max(h0, h1) > SAME_SIZE * min(h0, h1):
                continue
            if v1 - v0 <= h / 2:
                joined = max(u0, u1) - min(end0, end1) <= WORD_GAP * h
            else:
                joined = u0 < end1 + h and u1 < end0 + h  # a line below, overlapping along the text
            if joined:
                parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for i in range(len(words)):
        groups.setdefault(find(i), []).append(i)
    return [_block([words[i] for i in members]) for members in groups.values()]


def _block(members: list) -> TextBlock:
    """Reading order: top line first, left to right. Lines of bare numbers around a label are dimensions."""
    height = max(m[3] for m in members)
    lines: list[list] = []
    for word in sorted(members, key=lambda m: -m[2]):
        if lines and abs(lines[-1][0][2] - word[2]) <= height / 2:
            lines[-1].append(word)
        else:
            lines.append([word])
    rendered = [" ".join(w[0] for w in sorted(line, key=lambda m: m[1])) for line in lines]
    while len(rendered) > 1 and not re.search(r"[A-Za-z]", rendered[-1]):
        rendered.pop()
    while len(rendered) > 1 and not re.search(r"[A-Za-z]", rendered[0]):
        rendered.pop(0)
    _, _, _, _, _, c, s, _ = members[0]
    corners = [(u * c - v * s, u * s + v * c) for m in members for u in (m[1], m[7]) for v in (m[2], m[2] + m[3])]
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    box = (min(xs), min(ys), max(xs), max(ys))
    return TextBlock(" ".join(rendered), ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), height, box)


def is_room_name(block: TextBlock) -> bool:
    """Words a person would call a room, not a size, a level, equipment or a sentence."""
    return (
        _WORD.search(block.text) is not None
        and _ANNOTATION.search(block.text) is None
        and len(block.text.split()) <= MAX_NAME_WORDS
    )


def room_name(polygon, blocks: Sequence[TextBlock]) -> str | None:
    """The label inside a room: the largest text, then the one closest to the middle."""
    from shapely.geometry import Point

    inside = [b for b in blocks if is_room_name(b) and polygon.contains(Point(b.centre))]
    if not inside:
        return None
    middle = polygon.representative_point()
    best = max(inside, key=lambda b: (round(b.height, 3), -middle.distance(Point(b.centre))))
    return best.text


def _nearest(point: Point2, blocks: Sequence[TextBlock], pattern: re.Pattern, reach: float) -> str | None:
    best, best_d = None, reach
    for block in blocks:
        if pattern.search(block.text):
            d = math.dist(point, block.centre)
            if d < best_d:
                best, best_d = block.text, d
    return best


def door_tag(point: Point2, blocks: Sequence[TextBlock]) -> str | None:
    return _nearest(point, blocks, _DOOR_TAG, TAG_REACH)


def window_tag(point: Point2, blocks: Sequence[TextBlock]) -> str | None:
    return _nearest(point, blocks, _WINDOW_TAG, TAG_REACH)


def column_tag(point: Point2, blocks: Sequence[TextBlock]) -> str | None:
    return _nearest(point, blocks, _COLUMN_TAG, TAG_REACH)


def stair_name(point: Point2, blocks: Sequence[TextBlock]) -> str | None:
    return _nearest(point, blocks, _STAIR_NAME, STAIR_REACH)


def louvre_note(point: Point2, blocks: Sequence[TextBlock]) -> str | None:
    note = _nearest(point, blocks, _LOUVRE_NOTE, LOUVRE_REACH)
    return note.rstrip(".") if note else None


def project_title(blocks: Sequence[TextBlock]) -> str | None:
    """The text right under a "PROJECT TITLE:" heading in the title block."""
    for heading in (b for b in blocks if _PROJECT_HEADING.match(b.text)):
        below = [b for b in blocks if b is not heading and b.centre[1] < heading.box[1]
                 and b.box[0] < heading.box[2] and b.box[2] > heading.box[0]
                 and heading.box[1] - b.box[3] <= 3 * heading.height and _WORD.search(b.text)]
        if below:
            return max(below, key=lambda b: b.centre[1]).text
    return None


def level_from_title(blocks: Sequence[TextBlock]) -> str | None:
    """"... PLAN AT INTERMEDIATE LEVEL" names the level the plan shows."""
    for block in sorted(blocks, key=lambda b: -b.height):
        match = _LEVEL_IN_TITLE.search(block.text)
        if match:
            return match[1]
    return None
