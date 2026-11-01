"""Length units. The building model is always stored in metres; units only matter at the edges."""

from __future__ import annotations

from enum import Enum


class LengthUnit(Enum):
    MILLIMETRE = ("mm", 0.001)
    CENTIMETRE = ("cm", 0.01)
    METRE = ("m", 1.0)
    INCH = ("in", 0.0254)
    FOOT = ("ft", 0.3048)

    def __init__(self, symbol: str, to_metres: float) -> None:
        self.symbol = symbol
        self.to_metres = to_metres

    @classmethod
    def from_symbol(cls, symbol: str) -> LengthUnit:
        for unit in cls:
            if unit.symbol == symbol.lower():
                return unit
        raise ValueError(f"Unknown length unit '{symbol}'")


# DXF $INSUNITS header codes -> unit (0 = unitless, handled by guessing).
_DXF_INSUNITS = {
    1: LengthUnit.INCH,
    2: LengthUnit.FOOT,
    4: LengthUnit.MILLIMETRE,
    5: LengthUnit.CENTIMETRE,
    6: LengthUnit.METRE,
}


def from_dxf_insunits(code: int) -> LengthUnit | None:
    return _DXF_INSUNITS.get(int(code))


def guess_unit_from_extent(extent: float) -> LengthUnit:
    """Guess drawing units from the largest plan dimension of a unitless drawing.

    Assumes a single building between roughly 3 m and 500 m across. Metres and
    feet overlap in that range, so this is a fallback that the user must be able
    to override (ConversionSettings.unit_override).
    """
    if extent > 5000:
        return LengthUnit.MILLIMETRE
    if extent > 500:
        return LengthUnit.CENTIMETRE
    return LengthUnit.METRE
