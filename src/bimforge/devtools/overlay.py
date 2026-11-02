"""Diagnostic overlay: detected elements drawn on top of the source PDF page.

Used to check conversion quality against the original sheet:
    python -m bimforge.devtools.overlay plan.pdf out.png [--crop x0 y0 x1 y1] [--layers "0.50 pt grey 54%" ...]
Crop values are fractions of the page (0-1).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bimforge.adapters.pdf_importer import MM_PER_PT, PdfImporter
from bimforge.conversion import ConversionSettings, PlanConverter
from bimforge.core.model import Column, Door, DuctSegment, Opening, Project, Space, Stair, Wall, Window

COLOURS = {Wall: (230, 30, 30), Door: (30, 160, 30), Window: (30, 90, 230), Space: (255, 170, 0),
           Stair: (150, 40, 200), Column: (20, 20, 20), DuctSegment: (255, 0, 200)}


def render_overlay(pdf: Path, out: Path, crop=(0.0, 0.0, 1.0, 1.0), zoom: float = 2.0,
                   settings: ConversionSettings | None = None, page: int = 1) -> dict[str, int]:
    import pypdfium2 as pdfium
    from PIL import ImageDraw

    imported = PdfImporter().import_file(pdf, {"page": page})
    drawing = imported.drawing
    scale = next((int(w.split("1:")[1].split()[0]) for w in drawing.warnings if "scale 1:" in w), 100)
    result = PlanConverter(settings).run(drawing, Project().default_storey)

    document = pdfium.PdfDocument(str(pdf))
    pdf_page = document[page - 1]
    height_pt = pdf_page.get_height()
    image = pdf_page.render(scale=zoom).to_pil().convert("RGB")
    draw = ImageDraw.Draw(image, "RGBA")
    k = 1000 / (MM_PER_PT * scale)  # metres -> points

    def px(p):
        return (p[0] * k * zoom, (height_pt - p[1] * k) * zoom)

    by_id = {p.element.id: p.element for p in result.proposals}
    for proposal in result.proposals:
        e = proposal.element
        if isinstance(e, Wall):
            draw.line([px(e.start), px(e.end)], fill=COLOURS[Wall] + (200,), width=max(2, int(e.thickness * k * zoom)))
        elif isinstance(e, DuctSegment):
            draw.line([px(e.path[0][:2]), px(e.path[-1][:2])], fill=COLOURS[DuctSegment] + (150,),
                      width=max(2, int(e.width * k * zoom)))
        elif isinstance(e, Stair):
            ux, uy = (e.end[0] - e.start[0]) / e.length, (e.end[1] - e.start[1]) / e.length
            h = e.width / 2
            corners = [(e.start[0] - uy * h * s_, e.start[1] + ux * h * s_) for s_ in (1, -1)]
            corners += [(e.end[0] - uy * h * s_, e.end[1] + ux * h * s_) for s_ in (-1, 1)]
            draw.polygon([px(c) for c in corners], outline=COLOURS[Stair] + (255,), fill=COLOURS[Stair] + (70,))
            draw.line([px(e.start), px(e.end)], fill=COLOURS[Stair] + (255,), width=4)
        elif isinstance(e, Column):
            r = max(e.width, e.depth) / 2
            draw.rectangle([px((e.position[0] - r, e.position[1] + r)), px((e.position[0] + r, e.position[1] - r))],
                           fill=COLOURS[Column] + (200,))
        elif isinstance(e, Space):
            draw.polygon([px(p) for p in e.boundary], outline=COLOURS[Space] + (255,), fill=COLOURS[Space] + (40,))
    for proposal in result.proposals:
        e = proposal.element
        if isinstance(e, Opening):
            host = by_id[e.host_id]
            a, b = host.point_at(e.span[0]), host.point_at(e.span[1])
            draw.line([px(a), px(b)], fill=COLOURS.get(type(e), (0, 0, 0)) + (255,), width=6)
    w, h = image.size
    image.crop((int(crop[0] * w), int(crop[1] * h), int(crop[2] * w), int(crop[3] * h))).save(out)
    return result.summary()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--crop", type=float, nargs=4, default=(0, 0, 1, 1))
    parser.add_argument("--zoom", type=float, default=2.0)
    parser.add_argument("--layers", nargs="*")
    args = parser.parse_args()
    settings = ConversionSettings(wall_layers=tuple(args.layers)) if args.layers else None
    print(render_overlay(args.pdf, args.out, tuple(args.crop), args.zoom, settings))
