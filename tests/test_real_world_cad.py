"""DXF/DWG inputs as they come out of real CAD offices, not just the clean sample."""

import time
from pathlib import Path

import ezdxf
import pytest

from bimforge.adapters.dwg_importer import DwgImporter
from bimforge.adapters.dxf_importer import DxfImporter
from bimforge.conversion import ConversionSettings, PlanConverter
from bimforge.core.model import Door, Project, Wall, Window
from bimforge.devtools.sample_plan import build_sample_plan
from bimforge.drawing.primitives import Drawing2D, Layer, Segment
from bimforge.core.units import LengthUnit


def convert(path: Path, settings: ConversionSettings | None = None):
    drawing = DxfImporter().import_file(path).drawing
    return drawing, PlanConverter(settings).run(drawing, Project().default_storey)


def kinds(result) -> dict[str, int]:
    return result.summary()


def test_whole_plan_inside_a_block(tmp_path):
    """Many offices insert the floor plan as one block (or a bound xref)."""
    sample = ezdxf.readfile(build_sample_plan(tmp_path / "sample.dxf"))
    doc = ezdxf.new("R2018")
    doc.units = ezdxf.units.MM
    for layer in sample.layers:
        if layer.dxf.name not in doc.layers:
            doc.layers.add(layer.dxf.name)
    from ezdxf.addons import Importer

    importer = Importer(sample, doc)
    importer.import_blocks(["DOOR_900", "WINDOW_1200"])
    plan = doc.blocks.new("FLOOR_PLAN")
    importer.import_entities(sample.modelspace().query("LINE INSERT"), plan)
    importer.finalize()
    doc.modelspace().add_blockref("FLOOR_PLAN", (1000, 500))  # offset, to check the transform
    path = tmp_path / "in_block.dxf"
    doc.saveas(path)

    _, result = convert(path)
    assert kinds(result) == {"Wall": 5, "Window": 1, "Door": 1, "Space": 2, "Slab": 1}
    door = next(p.element for p in result.proposals if isinstance(p.element, Door))
    host = next(p.element for p in result.proposals if p.element.id == door.host_id)
    assert host.point_at(door.offset) == pytest.approx((6.0, 3.95))  # (5.0, 3.45) shifted by (1.0, 0.5)


def test_walls_drawn_as_wide_polylines(tmp_path):
    doc = ezdxf.new("R2018")
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (6000, 0), (6000, 4000), (0, 4000)], close=True,
                       dxfattribs={"layer": "WALLS", "const_width": 200})
    msp.add_lwpolyline([(3000, 0), (3000, 4000)], dxfattribs={"layer": "WALLS", "const_width": 100})
    path = tmp_path / "wide.dxf"
    doc.saveas(path)
    _, result = convert(path)
    walls = [p.element for p in result.proposals if isinstance(p.element, Wall)]
    assert sorted(w.thickness for w in walls) == pytest.approx([0.1, 0.2, 0.2, 0.2, 0.2])
    assert kinds(result)["Space"] == 2
    assert any("wide polylines" in w for w in result.warnings)


def test_walls_drawn_as_multilines(tmp_path):
    doc = ezdxf.new("R2018")
    doc.units = ezdxf.units.MM
    mline = doc.modelspace().add_mline([(0, 0), (8000, 0)], dxfattribs={"layer": "A-WALL"})
    mline.dxf.scale_factor = 250  # STANDARD style: two lines at +/-0.5 x scale
    mline.update_geometry()
    path = tmp_path / "mline.dxf"
    doc.saveas(path)
    _, result = convert(path)
    (wall,) = [p.element for p in result.proposals if isinstance(p.element, Wall)]
    assert wall.thickness == pytest.approx(0.25) and wall.length == pytest.approx(8.0)


def test_furniture_and_symbol_blocks_do_not_become_walls(tmp_path):
    doc = ezdxf.new("R2018")
    doc.units = ezdxf.units.MM
    bed = doc.blocks.new("BLK_42")  # unnamed furniture: parallel lines 150 mm apart
    for y in (0, 150, 1600, 1750):
        bed.add_line((0, y), (2000, y))
    doc.modelspace().add_blockref("BLK_42", (0, 0))
    path = tmp_path / "furniture.dxf"
    doc.saveas(path)
    _, result = convert(path)
    assert not [p for p in result.proposals if isinstance(p.element, Wall)]


def test_chosen_wall_layers_override_names(tmp_path):
    doc = ezdxf.new("R2018")
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    for y in (0, 200):  # walls on a layer whose name says nothing
        msp.add_line((0, y), (5000, y), dxfattribs={"layer": "L-07"})
    for y in (2000, 2150):  # parallel lines on another unnamed layer (e.g. a counter top)
        msp.add_line((0, y), (5000, y), dxfattribs={"layer": "L-12"})
    path = tmp_path / "layers.dxf"
    doc.saveas(path)
    _, auto = convert(path)
    assert len([p for p in auto.proposals if isinstance(p.element, Wall)]) == 2  # both look like walls
    _, chosen = convert(path, ConversionSettings(wall_layers=("L-07",)))
    (wall,) = [p.element for p in chosen.proposals if isinstance(p.element, Wall)]
    assert wall.start[1] == pytest.approx(0.1)
    (proposal,) = [p for p in chosen.proposals if p.element is wall]
    assert proposal.confidence == pytest.approx(1.0)


def test_no_walls_hint_lists_busiest_layers():
    drawing = Drawing2D(unit=LengthUnit.MILLIMETRE, layers={"X": Layer(name="X")},
                        primitives=[Segment(start=(0, i * 1000), end=(5000, i * 1000), layer="X") for i in range(5)])
    result = PlanConverter().run(drawing, Project().default_storey)
    (hint,) = [w for w in result.warnings if w.startswith("No walls detected")]
    assert "X (5)" in hint and "Conversion settings" in hint and "mm" in hint


def test_paper_space_only_drawing(tmp_path):
    doc = ezdxf.new("R2018")
    doc.units = ezdxf.units.MM
    layout = doc.layouts.get("Layout1")
    for y in (0, 200):
        layout.add_line((0, y), (5000, y), dxfattribs={"layer": "A-WALL"})
    path = tmp_path / "paper.dxf"
    doc.saveas(path)
    drawing, result = convert(path)
    assert any("paper-space" in w for w in drawing.warnings)
    assert len([p for p in result.proposals if isinstance(p.element, Wall)]) == 1


def test_large_drawing_converts_quickly(tmp_path):
    """6000 lines (a busy floor plan) should take seconds, not minutes."""
    segments = []
    for i in range(1500):
        x = (i % 50) * 3.0
        y = (i // 50) * 3.0
        segments += [Segment(start=(x, y), end=(x + 2.5, y), layer="A-WALL"),
                     Segment(start=(x, y + 0.2), end=(x + 2.5, y + 0.2), layer="A-WALL"),
                     Segment(start=(x, y + 1.0), end=(x, y + 2.5), layer="A-FURN"),
                     Segment(start=(x + 1, y + 1.0), end=(x + 1, y + 2.5), layer="TEXT")]
    drawing = Drawing2D(unit=LengthUnit.METRE, primitives=segments,
                        layers={n: Layer(name=n) for n in ("A-WALL", "A-FURN", "TEXT")})
    started = time.perf_counter()
    result = PlanConverter().run(drawing, Project().default_storey)
    elapsed = time.perf_counter() - started
    assert result.summary()["Wall"] >= 1500 / 50  # rows of collinear pieces merge
    assert elapsed < 20, f"took {elapsed:.1f} s"


class TestDwg:
    def test_missing_converter_explains_what_to_do(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bimforge.adapters.dwg_importer.find_oda_converter", lambda: None)
        monkeypatch.setattr("bimforge.adapters.dwg_importer.find_libredwg", lambda: None)
        dwg = tmp_path / "plan.dwg"
        dwg.write_bytes(b"AC1032")
        with pytest.raises(ValueError, match="ODA File Converter") as info:
            DwgImporter().import_file(dwg)
        assert "Save As > DXF" in str(info.value)

    def test_converts_with_oda_and_reads_result(self, tmp_path, monkeypatch):
        sample = build_sample_plan(tmp_path / "source.dxf")
        dwg = tmp_path / "plan.dwg"
        dwg.write_bytes(b"AC1032")
        calls = []

        def fake_run(command):  # stands in for ODAFileConverter: writes <stem>.dxf into the output folder
            calls.append(command)
            Path(command[2], "plan.dxf").write_bytes(sample.read_bytes())

        monkeypatch.setattr("bimforge.adapters.dwg_importer.find_oda_converter", lambda: "ODAFileConverter.exe")
        monkeypatch.setattr(DwgImporter, "_run", staticmethod(fake_run))
        result = DwgImporter().import_file(dwg)
        assert calls[0][3:5] == ["ACAD2018", "DXF"]
        assert result.drawing.source == str(dwg)
        assert "ODA File Converter" in result.warnings[0]
        converted = PlanConverter().run(result.drawing, Project().default_storey)
        assert converted.summary()["Wall"] == 5

    def test_falls_back_to_libredwg(self, tmp_path, monkeypatch):
        sample = build_sample_plan(tmp_path / "source.dxf")
        dwg = tmp_path / "plan.dwg"
        dwg.write_bytes(b"AC1032")

        def fake_run(command):
            Path(command[command.index("-o") + 1]).write_bytes(sample.read_bytes())

        monkeypatch.setattr("bimforge.adapters.dwg_importer.find_oda_converter", lambda: None)
        monkeypatch.setattr("bimforge.adapters.dwg_importer.find_libredwg", lambda: "dwg2dxf")
        monkeypatch.setattr(DwgImporter, "_run", staticmethod(fake_run))
        assert "LibreDWG" in DwgImporter().import_file(dwg).warnings[0]


def test_registry_offers_dwg():
    from bimforge.plugins.registry import PluginRegistry

    registry = PluginRegistry.with_builtins(discover=False)
    assert isinstance(registry.importer_for(Path("plan.DWG")), DwgImporter)


def test_wall_layer_is_picked_automatically_among_unnamed_styles():
    """PDF-style input: many unnamed line styles; walls must win over table rows and text frames."""
    prims = []

    def box(x0, y0, x1, y1, t, layer):  # a closed room drawn as inner and outer face rectangles
        for (a, b), (c, d) in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
            prims.append(Segment(start=(a, b), end=(c, d), layer=layer))
        for (a, b), (c, d) in (((x0 + t, y0 + t), (x1 - t, y0 + t)), ((x1 - t, y0 + t), (x1 - t, y1 - t)),
                               ((x1 - t, y1 - t), (x0 + t, y1 - t)), ((x0 + t, y1 - t), (x0 + t, y0 + t))):
            prims.append(Segment(start=(a, b), end=(c, d), layer=layer))

    box(0, 0, 6000, 5000, 300, "0.50 pt grey 54%")
    box(6000, 0, 12000, 5000, 300, "0.50 pt grey 54%")
    for i in range(40):  # a revision table: long rows 200 mm apart (looks like walls to a naive pairer)
        prims.append(Segment(start=(20000, i * 200), end=(30000, i * 200), layer="0.25 pt black"))
    for n, layer in enumerate(("1.90 pt cyan", "0.35 pt grey 88%", "0.60 pt #ffc3c3")):
        prims.append(Segment(start=(0, 9000 + n * 1000), end=(500, 9000 + n * 1000), layer=layer))
    drawing = Drawing2D(unit=LengthUnit.MILLIMETRE, primitives=prims,
                        layers={p.layer: Layer(name=p.layer) for p in prims})
    result = PlanConverter().run(drawing, Project().default_storey)
    walls = [p.element for p in result.proposals if isinstance(p.element, Wall)]
    assert walls and all(w.thickness == pytest.approx(0.3) for w in walls)
    assert result.summary()["Space"] == 2
    assert any("'0.50 pt grey 54%'" in w for w in result.warnings)
