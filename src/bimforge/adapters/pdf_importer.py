"""Vector PDF importer (pdfplumber/pdfminer, MIT).

A plan exported from CAD to PDF keeps its linework as vector paths but loses
layers, units and scale. So:
* coordinates are converted from paper points to model millimetres using the
  drawing scale, read from the sheet ("SCALE 1:100") or given by the user;
* line weight stands in for layers ("Line weight 0.70"): walls are usually the
  heaviest lines, and the user can tick those in Conversion settings;
* Bézier curves that are circular arcs (door swings) become Arc primitives.
Scanned PDFs (images only) are reported, not silently returned empty.
"""

from __future__ import annotations

import importlib.util
import math
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bimforge.core.model import Point2
from bimforge.core.units import LengthUnit
from bimforge.drawing.primitives import Arc, Drawing2D, Layer, Polyline, Primitive, Segment, Text
from bimforge.plugins.sdk import Importer, ImportResult, PluginInfo

MM_PER_PT = 25.4 / 72
DEFAULT_SCALE = 100
_SCALE_RE = re.compile(r"(?<![\d.])1\s*:\s*(\d{1,4})(?![\d.])")
PLAUSIBLE_SCALES = range(5, 5001)


def detect_scale(text: str) -> int | None:
    """Most frequent "1:N" on the sheet, ignoring implausible values."""
    found = [int(m) for m in _SCALE_RE.findall(text) if int(m) in PLAUSIBLE_SCALES]
    return Counter(found).most_common(1)[0][0] if found else None


COLOUR_NAMES = {
    (0, 0, 0): "black", (1, 0, 0): "red", (0, 1, 0): "green", (0, 0, 1): "blue",
    (0, 1, 1): "cyan", (1, 0, 1): "magenta", (1, 1, 0): "yellow", (1, 1, 1): "white",
}


def colour_name(colour: Any) -> str:
    """Readable name for a PDF stroke colour (gray, RGB or CMYK)."""
    if colour is None:
        return "black"
    values = list(colour) if isinstance(colour, (list, tuple)) else [colour]
    if len(values) == 1:
        rgb = (values[0],) * 3
    elif len(values) == 4:  # CMYK
        c, m, y, k = values
        rgb = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    else:
        rgb = tuple(values[:3])
    try:
        rgb = tuple(float(x) for x in rgb)
    except (TypeError, ValueError):
        return "pattern"
    if max(rgb) - min(rgb) < 0.06:  # neutral
        level = round(rgb[0] * 100)
        return "black" if level < 6 else "white" if level > 97 else f"grey {level}%"
    named = {k: v for k, v in COLOUR_NAMES.items()}
    nearest = min(named, key=lambda k: sum((a - b) ** 2 for a, b in zip(k, rgb)))
    if sum((a - b) ** 2 for a, b in zip(nearest, rgb)) < 0.05:
        return named[nearest]
    return "#" + "".join(f"{round(x * 255):02x}" for x in rgb)


def _structural_fill(obj: Mapping[str, Any]) -> bool:
    """Black or grey fills can be poche walls and columns. White fills are masks behind text
    (wipeouts); coloured fills are symbols, arrowheads and MEP markers."""
    if not obj.get("fill"):
        return False
    name = colour_name(obj.get("non_stroking_color"))
    return name == "black" or name.startswith("grey")


def weight_layer(linewidth: float | None, colour: Any = None) -> str:
    """Pseudo-layer for a PDF stroke: CAD exports keep weight and colour per layer."""
    return f"{round(float(linewidth or 0.0) * 20) / 20:.2f} pt {colour_name(colour)}"


def _bezier_point(p0, c1, c2, p3, t: float) -> Point2:
    u = 1 - t
    return tuple(u**3 * p0[i] + 3 * u * u * t * c1[i] + 3 * u * t * t * c2[i] + t**3 * p3[i] for i in range(2))  # type: ignore[return-value]


def _line_intersection(p, d, q, e) -> Point2 | None:
    denom = d[0] * e[1] - d[1] * e[0]
    if abs(denom) < 1e-12:
        return None
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / denom
    return (p[0] + d[0] * t, p[1] + d[1] * t)


def bezier_as_arc(p0, c1, c2, p3) -> tuple[Point2, float, float, float] | None:
    """(centre, radius, start_deg, sweep_deg) if the cubic Bézier is a circular arc, else None.

    The centre is where the normals at both ends meet; the curve midpoint must
    lie on the same circle (within 2 %).
    """
    t0 = (c1[0] - p0[0], c1[1] - p0[1])
    t1 = (p3[0] - c2[0], p3[1] - c2[1])
    if math.hypot(*t0) < 1e-9 or math.hypot(*t1) < 1e-9:
        return None
    centre = _line_intersection(p0, (-t0[1], t0[0]), p3, (-t1[1], t1[0]))
    if centre is None:
        return None
    r0, r1 = math.dist(centre, p0), math.dist(centre, p3)
    mid = _bezier_point(p0, c1, c2, p3, 0.5)
    if r0 < 1e-9 or abs(r0 - r1) > 0.02 * r0 or abs(math.dist(centre, mid) - r0) > 0.02 * r0:
        return None
    a0 = math.degrees(math.atan2(p0[1] - centre[1], p0[0] - centre[0]))
    a1 = math.degrees(math.atan2(p3[1] - centre[1], p3[0] - centre[0]))
    # Direction of travel decides the sign of the sweep.
    cross = t0[0] * (p0[1] - centre[1]) - t0[1] * (p0[0] - centre[0])
    sweep = (a1 - a0) % 360 if cross < 0 else -((a0 - a1) % 360)
    return centre, (r0 + r1) / 2, a0, sweep


class PdfImporter(Importer):
    info = PluginInfo(id="bimforge.import.pdf", name="PDF (vector)", version="0.1.0", license="MIT")
    extensions = (".pdf",)

    @classmethod
    def available(cls) -> bool:
        return importlib.util.find_spec("pdfplumber") is not None

    @staticmethod
    def inspect(path: Path) -> list[int | None]:
        """Detected scale per page (None where no "1:N" note was found).

        Uses PDFium's native text reader: on a large sheet it takes a fraction of a second,
        where pdfplumber would parse every line on the page (several seconds) just to read a note.
        """
        import pypdfium2

        document = pypdfium2.PdfDocument(str(path))
        try:
            return [detect_scale(page.get_textpage().get_text_range() or "") for page in document]
        finally:
            document.close()

    def import_file(self, path: Path, options: Mapping[str, Any] | None = None) -> ImportResult:
        import pdfplumber
        from pdfminer.pdfparser import PDFSyntaxError

        options = options or {}
        page_number = int(options.get("page", 1))
        try:
            pdf = pdfplumber.open(str(path))
        except (OSError, PDFSyntaxError, Exception) as exc:  # noqa: BLE001 - pdfminer raises many types
            raise ValueError(f"Cannot read PDF '{path}': {exc}") from exc
        with pdf:
            if not 1 <= page_number <= len(pdf.pages):
                raise ValueError(f"The PDF has {len(pdf.pages)} page(s); page {page_number} does not exist")
            page = pdf.pages[page_number - 1]
            return self._read_page(page, Path(path), page_number, len(pdf.pages), options)

    def _read_page(self, page: Any, path: Path, number: int, count: int, options: Mapping[str, Any]) -> ImportResult:
        text = page.extract_text() or ""
        detected = detect_scale(text)
        scale = int(options.get("scale") or detected or DEFAULT_SCALE)
        drawing = Drawing2D(source=f"{path}#page={number}", unit=LengthUnit.MILLIMETRE)
        if options.get("scale"):
            drawing.warnings.append(f"PDF read at scale 1:{scale} (as entered)")
        elif detected:
            drawing.warnings.append(f"PDF scale 1:{scale} read from the sheet")
        else:
            drawing.warnings.append(f"No scale found on the sheet; assumed 1:{scale}. Re-import with the right scale "
                                    "if sizes look wrong.")
        if count > 1:
            drawing.warnings.append(f"Read page {number} of {count}")

        k = MM_PER_PT * scale
        height = float(page.height)

        def model(x: float, top: float) -> Point2:
            return (round(x * k, 3), round((height - top) * k, 3))

        out: list[Primitive] = []
        weights: Counter[str] = Counter()

        def segment(a: Point2, b: Point2, layer: str) -> None:
            if a != b:
                out.append(Segment(start=a, end=b, layer=layer))
                weights[layer] += 1

        for line in page.lines:
            layer = weight_layer(line.get("linewidth"), line.get("stroking_color"))
            pts = [model(x, y) for x, y in line["pts"]]
            for a, b in zip(pts, pts[1:]):
                segment(a, b, layer)
        for rect in page.rects:
            layer = weight_layer(rect.get("linewidth"), rect.get("stroking_color"))
            corners = [model(rect["x0"], rect["top"]), model(rect["x1"], rect["top"]),
                       model(rect["x1"], rect["bottom"]), model(rect["x0"], rect["bottom"])]
            for a, b in zip(corners, corners[1:] + corners[:1]):
                segment(a, b, layer)
            if _structural_fill(rect):
                out.append(Polyline(points=tuple(corners), closed=True, filled=True,
                                    layer=f"fill {colour_name(rect.get('non_stroking_color'))}"))
        arcs = 0
        for curve in page.curves:
            layer = weight_layer(curve.get("linewidth"), curve.get("stroking_color"))
            arcs += self._read_path(curve.get("path") or [], model, layer, out, segment)
            if _structural_fill(curve) and len(curve.get("pts") or []) >= 3:
                out.append(Polyline(points=tuple(model(x, y) for x, y in curve["pts"]), closed=True, filled=True,
                                    layer=f"fill {colour_name(curve.get('non_stroking_color'))}"))
        for word in page.extract_words(extra_attrs=["upright"], return_chars=True):
            text, rotation, size = word["text"], 0.0, word["bottom"] - word["top"]
            if not word["upright"]:
                # Sideways words come back sorted top to bottom, so text running up the page reads backwards.
                upward = word["chars"][0]["matrix"][1] > 0
                text, rotation = (text[::-1], 90.0) if upward else (text, -90.0)
                size = word["x1"] - word["x0"]
            out.append(Text(position=model(word["x0"], word["bottom"]), text=text, height=size * k, rotation=rotation,
                            layer="Text"))

        if not weights and not arcs:
            if page.images:
                raise ValueError(
                    "This PDF page is a scanned image, with no vector linework to read. Scanned drawings are not "
                    "supported yet: export the plan from CAD as a vector PDF or DXF, or trace it in a CAD program."
                )
            raise ValueError("No linework found on this PDF page")

        drawing.primitives = out
        drawing.layers = {name: Layer(name=name) for name in weights} | {"Text": Layer(name="Text")}
        drawing.warnings.append(
            f"PDFs have no layers; the {len(weights)} line styles (weight + colour) are used as layers instead. "
            "If walls are missed or wrong, tick the wall line styles in Edit > Conversion settings."
        )
        return ImportResult(drawing=drawing, warnings=list(drawing.warnings))

    @staticmethod
    def _read_path(path_ops: list, model, layer: str, out: list[Primitive], segment) -> int:
        """Walk a PDF path: straight parts become segments, circular Bézier parts become arcs."""
        arcs = 0
        start = current = None
        pending: list = []  # consecutive arc pieces on the same circle, merged into one arc

        def flush() -> None:
            nonlocal arcs
            if pending:
                centre, radius, a0, sweep = pending[0][0], pending[0][1], pending[0][2], sum(p[3] for p in pending)
                lo = a0 if sweep >= 0 else a0 + sweep
                out.append(Arc(center=centre, radius=radius, start_angle=lo % 360, end_angle=(lo + abs(sweep)) % 360,
                               layer=layer))
                arcs += 1
                pending.clear()

        for op in path_ops:
            kind, points = op[0], [model(x, y) for x, y in op[1:]]
            if kind == "m":
                flush()
                start = current = points[0]
            elif kind == "l" and current is not None:
                flush()
                segment(current, points[0], layer)
                current = points[0]
            elif kind == "c" and current is not None:
                c1, c2, p3 = points
                arc = bezier_as_arc(current, c1, c2, p3)
                if arc is None:
                    flush()
                    previous = current
                    for t in (0.25, 0.5, 0.75, 1.0):  # gentle curves: short chords
                        point = _bezier_point(current, c1, c2, p3, t)
                        segment(previous, point, layer)
                        previous = point
                else:
                    if pending and (math.dist(pending[-1][0], arc[0]) > 0.02 * arc[1]
                                    or abs(pending[-1][1] - arc[1]) > 0.02 * arc[1]):
                        flush()
                    pending.append(arc)
                current = p3
            elif kind == "h" and current is not None and start is not None:
                flush()
                segment(current, start, layer)
                current = start
        flush()
        return arcs
