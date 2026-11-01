"""Writes the sample two-room plan as a vector PDF sheet (as CAD programs plot it), for demos and tests.

Walls are thick strokes, the door is a leaf line plus a Bézier swing arc,
the window is three thin glazing lines, there is some thin furniture, and a
"SCALE 1:100" note. Written with a tiny built-in PDF writer (no dependencies).
"""

from __future__ import annotations

from pathlib import Path

MM_PER_PT = 25.4 / 72
KAPPA = 0.5522847  # Bézier control distance for a quarter circle


def _pt(mm: float, scale: int) -> float:
    return mm / scale / MM_PER_PT


def build_sample_pdf(path: Path, scale: int = 100, margin_pt: float = 60) -> Path:
    def p(x: float, y: float) -> str:
        return f"{margin_pt + _pt(x, scale):.3f} {margin_pt + _pt(y, scale):.3f}"

    ops: list[str] = []

    def lines(width: float, segments) -> None:
        ops.append(f"{width} w")
        for (x0, y0), (x1, y1) in segments:
            ops.append(f"{p(x0, y0)} m {p(x1, y1)} l S")

    lines(0.7, [  # wall faces, same geometry as devtools.sample_plan (mm)
        ((0, 0), (2000, 0)), ((3200, 0), (10000, 0)),
        ((200, 200), (2000, 200)), ((3200, 200), (4950, 200)), ((5050, 200), (9800, 200)),
        ((0, 8000), (10000, 8000)), ((200, 7800), (4950, 7800)), ((5050, 7800), (9800, 7800)),
        ((0, 0), (0, 8000)), ((200, 200), (200, 7800)), ((10000, 0), (10000, 8000)), ((9800, 200), (9800, 7800)),
        ((4950, 200), (4950, 3000)), ((5050, 200), (5050, 3000)), ((4950, 3900), (4950, 7800)),
        ((5050, 3900), (5050, 7800)),
    ])
    lines(0.18, [((2000, y), (3200, y)) for y in (0, 100, 200)])  # window glazing
    lines(0.18, [((5050, 3000), (5050, 3900))])  # door leaf, drawn closed against the jamb
    # Door swing: quarter circle about the hinge (5050, 3000), radius 900, from 0° to 90°.
    r, (cx, cy) = 900, (5050, 3000)
    ops.append(f"{p(cx + r, cy)} m {p(cx + r, cy + r * KAPPA)} {p(cx + r * KAPPA, cy + r)} {p(cx, cy + r)} c S")
    lines(0.18, [((600, 5000), (2600, 5000)), ((600, 6600), (2600, 6600)),  # a bed
                 ((600, 5000), (600, 6600)), ((2600, 5000), (2600, 6600))])
    ops.append(f"BT /F1 10 Tf {p(0, -1200)} Td (GROUND FLOOR PLAN   SCALE 1:{scale}) Tj ET")
    ops.append(f"BT /F1 8 Tf {p(1500, 4000)} Td (ROOM 1) Tj ET")

    content = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1190.55 841.89] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


if __name__ == "__main__":
    import sys

    print(f"Wrote {build_sample_pdf(Path(sys.argv[1] if len(sys.argv) > 1 else 'samples/two_rooms.pdf'))}")
