"""Rule-based layer and block classification.

This is the baseline. An AIModel with task "layer_classification" can replace
it (e.g. a text classifier over layer names plus geometry statistics), and
its predictions feed the same LayerRole values.
"""

from __future__ import annotations

import re
from enum import Enum


class LayerRole(str, Enum):
    WALL = "wall"
    DOOR = "door"
    WINDOW = "window"
    ANNOTATION = "annotation"
    FURNITURE = "furniture"
    GRID = "grid"
    UNKNOWN = "unknown"


# Order matters: the first match wins. Covers AIA/NCS-style names and common local conventions.
_RULES: list[tuple[LayerRole, re.Pattern[str]]] = [
    (LayerRole.DOOR, re.compile(r"door|dr\b|porte|tuer|tür", re.I)),
    (LayerRole.WINDOW, re.compile(r"wind|glaz|fenster|fenetre|fenêtre|\bwin\b", re.I)),
    (LayerRole.WALL, re.compile(r"wall|wand|\bmur|a-wall|partition|masonry|brick", re.I)),
    (LayerRole.ANNOTATION, re.compile(r"dim|text|anno|note|tag|title|symb|hatch|defpoints", re.I)),
    (LayerRole.FURNITURE, re.compile(r"furn|fixt|equip|sanit|kitchen", re.I)),
    (LayerRole.GRID, re.compile(r"grid|axis|axes|centre|center", re.I)),
]

# How much a wall candidate on a layer of this role should be trusted.
WALL_LAYER_SCORE = {
    LayerRole.WALL: 1.0,
    LayerRole.UNKNOWN: 0.7,
}


def classify_name(name: str) -> LayerRole:
    for role, pattern in _RULES:
        if pattern.search(name):
            return role
    return LayerRole.UNKNOWN


def classify_block(block_name: str, layer: str) -> LayerRole:
    """Block name is more specific than layer name, so it wins when it is informative."""
    role = classify_name(block_name)
    return role if role is not LayerRole.UNKNOWN else classify_name(layer)
