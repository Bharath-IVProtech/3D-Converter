import math
from pathlib import Path

import ezdxf
import pytest

from bimforge.adapters.dxf_importer import DxfImporter
from bimforge.conversion import PlanConverter
from bimforge.core.model import Door, Project, Wall, Window
from bimforge.devtools.sample_pdf import KAPPA, MM_PER_PT, build_sample_pdf

pytest.importorskip("pdfplumber")
from bimforge.adapters.pdf_importer import PdfImporter, bezier_as_arc, detect_scale  # noqa: E402

MARGIN_MM = 60 * MM_PER_PT * 100  # the sample sheet's 60 pt margin, in model mm at 1:100


@pytest.mark.parametrize("text, scale", [
    ("GROUND FLOOR PLAN   SCALE 1:100", 100),
    ("Scale 1 : 50 @ A1", 50),
    ("1:100 plan, 1:20 detail, 1:100 key", 100),
    ("Drawn 12.01.2025, rev 3", None),
    ("ratio 1:1", None),  # implausible for a plan
])
def test_detect_scale(text, scale):
    assert detect_scale(text) == scale


class TestBezierArcs:
    def test_quarter_circle_counter_clockwise(self):
        r = 10
        arc = bezier_as_arc((r, 0), (r, r * KAPPA), (r * KAPPA, r), (0, r))
        centre, radius, start, sweep = arc
        assert centre == pytest.approx((0, 0), abs=1e-6) and radius == pytest.approx(r)
        assert (start, sweep) == (pytest.approx(0), pytest.approx(90))

    def test_quarter_circle_clockwise(self):
        r = 10
        _, _, start, sweep = bezier_as_arc((0, r), (r * KAPPA, r), (r, r * KAPPA), (r, 0))
        assert (start, sweep) == (pytest.approx(90), pytest.approx(-90))

    def test_s_curve_is_not_an_arc(self):
        assert bezier_as_arc((0, 0), (5, 10), (5, -10), (10, 0)) is None


def convert_pdf(path, **options):
    drawing = PdfImporter().import_file(path, options).drawing
    return drawing, PlanConverter().run(drawing, Project().default_storey)


def test_sample_sheet_converts_like_the_dxf(tmp_path):
    drawing, result = convert_pdf(build_sample_pdf(tmp_path / "plan.pdf"))
    assert result.summary() == {"Wall": 5, "Door": 1, "Window": 1, "Space": 2, "Slab": 1}
    assert any("1:100 read from the sheet" in w for w in drawing.warnings)
    by_id = {p.element.id: p.element for p in result.proposals}
    door = next(e for e in by_id.values() if isinstance(e, Door))
    window = next(e for e in by_id.values() if isinstance(e, Window))
    m = MARGIN_MM / 1000
    assert by_id[door.host_id].point_at(door.offset) == pytest.approx((5.0 + m, 3.45 + m), abs=0.01)
    assert door.width == pytest.approx(0.9, abs=0.01)
    assert by_id[window.host_id].point_at(window.offset) == pytest.approx((2.6 + m, 0.1 + m), abs=0.01)
    assert window.width == pytest.approx(1.2, abs=0.01)
    exterior = [e for e in by_id.values() if isinstance(e, Wall) and e.thickness > 0.15]
    assert sorted(round(w.length, 2) for w in exterior) == [7.8, 7.8, 9.8, 9.8]


def test_given_scale_overrides_the_sheet(tmp_path):
    _, result = convert_pdf(build_sample_pdf(tmp_path / "plan.pdf"), scale=50)
    thicknesses = sorted({round(p.element.thickness, 3) for p in result.proposals if isinstance(p.element, Wall)})
    assert thicknesses == [0.05, 0.1]  # everything half size


def test_missing_page(tmp_path):
    with pytest.raises(ValueError, match="1 page"):
        PdfImporter().import_file(build_sample_pdf(tmp_path / "plan.pdf"), {"page": 3})


def test_inspect_reports_scale_per_page(tmp_path):
    assert PdfImporter.inspect(build_sample_pdf(tmp_path / "plan.pdf", scale=50)) == [50]


def _image_only_pdf(path: Path) -> Path:
    content = b"q 500 0 0 500 50 50 cm /Im1 Do Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 600] /Contents 4 0 R "
        b"/Resources << /XObject << /Im1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 "
        b"/Length 1 >>\nstream\n\x80\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref
    path.write_bytes(bytes(out))
    return path


def test_scanned_pdf_is_explained(tmp_path):
    with pytest.raises(ValueError, match="scanned image"):
        PdfImporter().import_file(_image_only_pdf(tmp_path / "scan.pdf"))


def test_not_a_pdf(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_text("hello")
    with pytest.raises(ValueError):
        PdfImporter().import_file(bad)


def test_raw_dxf_doors_and_windows_without_blocks(tmp_path):
    """Hand-drafted DXF: door as a plain ARC + leaf line, window as glazing lines in the gap."""
    doc = ezdxf.new("R2018")
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    for y in (0, 200):  # wall faces with a door gap (2000-2900) and a window gap (5000-6200)
        for x0, x1 in ((0, 2000), (2900, 5000), (6200, 8000)):
            msp.add_line((x0, y), (x1, y))
    msp.add_arc((2000, 200), 900, 0, 90)  # swing about the hinge at the jamb
    msp.add_line((2000, 200), (2000, 1100))
    for y in (50, 150):
        msp.add_line((5000, y), (6200, y))  # glazing
    path = tmp_path / "raw.dxf"
    doc.saveas(path)
    result = PlanConverter().run(DxfImporter().import_file(path).drawing, Project().default_storey)
    summary = result.summary()
    assert summary["Door"] == 1 and summary["Window"] == 1 and summary["Wall"] == 1
    door = next(p.element for p in result.proposals if isinstance(p.element, Door))
    assert door.width == pytest.approx(0.9)
    window = next(p.element for p in result.proposals if isinstance(p.element, Window))
    assert window.width == pytest.approx(1.2)


def test_registry_and_cli(tmp_path):
    from bimforge.__main__ import main
    from bimforge.app.persistence import load_project
    from bimforge.plugins.registry import PluginRegistry

    registry = PluginRegistry.with_builtins(discover=False)
    assert isinstance(registry.importer_for(Path("sheet.PDF")), PdfImporter)
    pdf = build_sample_pdf(tmp_path / "plan.pdf")
    assert main(["convert", str(pdf), str(tmp_path / "plan.bimx")]) == 0
    project = load_project(tmp_path / "plan.bimx").project
    assert len(project.elements_of(Wall)) == 5
    assert math.isclose(sum(w.length for w in project.elements_of(Wall)), 2 * 9.8 + 2 * 7.8 + 7.8, rel_tol=1e-3)
