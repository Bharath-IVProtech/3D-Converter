"""User-configurable defaults for 2D->3D conversion. All lengths in metres."""

from __future__ import annotations

from dataclasses import dataclass

from bimforge.core.units import LengthUnit


@dataclass
class ConversionSettings:
    # Generated element defaults
    wall_height: float = 3.0
    slab_thickness: float = 0.2
    door_height: float = 2.1
    default_door_width: float = 0.9
    window_sill_height: float = 0.9
    window_height: float = 1.2
    default_window_width: float = 1.2

    # Wall detection
    min_wall_thickness: float = 0.05
    max_wall_thickness: float = 0.6
    parallel_tolerance_deg: float = 2.0
    max_face_drift: float = 0.02  # max change in face-to-face distance along a wall
    min_wall_overlap: float = 0.3
    max_junction_gap: float = 0.5  # collinear walls closer than this are merged (T-junctions)
    max_opening_gap: float = 3.0
    min_room_area: float = 1.0

    # Layers chosen by the user as walls. Empty = detect from layer names.
    wall_layers: tuple[str, ...] = ()
    # Lines inside blocks smaller than this (furniture, symbols) are not wall candidates.
    min_wall_block_size: float = 2.5

    # Review
    accept_threshold: float = 0.5  # proposals at or above this start as accepted
    unit_override: LengthUnit | None = None
