from pathlib import Path

import pytest

from bimforge.adapters.dxf_importer import DxfImporter
from bimforge.app.commands import AddElements, CommandBus, RemoveElements
from bimforge.app.events import ElementsChanged, EventBus
from bimforge.conversion import PlanConverter
from bimforge.core.model import Door, Project, Wall
from bimforge.core.units import LengthUnit, from_dxf_insunits, guess_unit_from_extent
from bimforge.drawing.primitives import BlockRef, Dimension, Segment, Text
from bimforge.plugins.registry import PluginRegistry


class TestUnits:
    def test_dxf_codes(self):
        assert from_dxf_insunits(4) is LengthUnit.MILLIMETRE
        assert from_dxf_insunits(2) is LengthUnit.FOOT
        assert from_dxf_insunits(0) is None

    @pytest.mark.parametrize("extent, unit", [(12000, "mm"), (1200, "cm"), (12, "m")])
    def test_guess(self, extent, unit):
        assert guess_unit_from_extent(extent).symbol == unit

    def test_from_symbol(self):
        assert LengthUnit.from_symbol("FT") is LengthUnit.FOOT
        with pytest.raises(ValueError):
            LengthUnit.from_symbol("furlong")


class TestDxfImporter:
    def test_reads_layers_units_and_entities(self, sample_dxf):
        result = DxfImporter().import_file(sample_dxf)
        d = result.drawing
        assert d.unit is LengthUnit.MILLIMETRE
        assert {"A-WALL", "A-DOOR", "A-GLAZ"} <= set(d.layers)
        segments = d.of_type(Segment)
        assert len([x for x in segments if not x.block]) == 16
        # Symbol blocks are exploded too, each line tagged with its block (window: 3 lines, door: 1).
        assert sorted(x.block for x in segments if x.block) == ["DOOR_900"] + ["WINDOW_1200"] * 3
        assert len(d.of_type(Text)) == 1
        assert len(d.of_type(Dimension)) == 1

    def test_block_refs_carry_extent(self, sample_dxf):
        blocks = {b.name: b for b in DxfImporter().import_file(sample_dxf).drawing.of_type(BlockRef)}
        window = blocks["WINDOW_1200"]
        assert window.position == pytest.approx((2000, 0))
        assert window.width == pytest.approx(1200)
        assert window.world_center == pytest.approx((2600, 100))

    def test_missing_file_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError):
            DxfImporter().import_file(tmp_path / "nope.dxf")


def test_block_ref_rotation_transform():
    b = BlockRef(name="X", position=(10, 0), rotation=90, extent=((0, 0), (2, 1)))
    assert b.world_center == pytest.approx((9.5, 1.0))


class TestCommandBus:
    def setup_method(self):
        self.project = Project()
        self.events = EventBus()
        self.changes: list[ElementsChanged] = []
        self.events.subscribe(ElementsChanged, self.changes.append)
        self.bus = CommandBus(self.project, self.events)
        self.wall = Wall(start=(0, 0), end=(4, 0))
        self.door = Door(host_id=self.wall.id, offset=2)

    def test_add_undo_redo(self):
        self.bus.execute(AddElements([self.wall, self.door]))
        assert len(self.project) == 2
        assert self.bus.undo()
        assert len(self.project) == 0
        assert self.bus.redo()
        assert len(self.project) == 2
        assert [bool(c.added) for c in self.changes] == [True, False, True]
        assert not self.bus.redo()

    def test_remove_wall_takes_hosted_openings_and_undo_restores(self):
        self.bus.execute(AddElements([self.wall, self.door]))
        self.bus.execute(RemoveElements([self.wall.id]))
        assert len(self.project) == 0
        assert set(self.changes[-1].removed) == {self.wall.id, self.door.id}
        self.bus.undo()
        assert self.project.get(self.door.id) is self.door

    def test_failed_add_is_atomic(self):
        self.bus.execute(AddElements([self.wall]))
        with pytest.raises(ValueError):
            self.bus.execute(AddElements([self.door, self.wall]))  # duplicate wall id
        assert self.door.id not in self.project
        assert self.bus.undo_label == "Add elements"

    def test_new_command_clears_redo(self):
        self.bus.execute(AddElements([self.wall]))
        self.bus.undo()
        self.bus.execute(AddElements([Wall(start=(0, 0), end=(1, 0))]))
        assert self.bus.redo_label is None

    def test_unsubscribe(self):
        unsubscribe = self.events.subscribe(ElementsChanged, lambda e: pytest.fail("should be unsubscribed"))
        unsubscribe()
        self.bus.execute(AddElements([self.wall]))


def test_registry_resolves_builtin_plugins():
    registry = PluginRegistry.with_builtins(discover=False)
    assert isinstance(registry.importer_for(Path("plan.DXF")), DxfImporter)
    assert registry.kernel().supports("extrude")
    with pytest.raises(LookupError):
        registry.importer_for(Path("model.rvt"))


def test_ifc_export_roundtrip(sample_dxf, tmp_path):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    registry = PluginRegistry.with_builtins(discover=False)
    drawing = registry.importer_for(sample_dxf).import_file(sample_dxf).drawing
    project = Project("Sample")
    result = PlanConverter().run(drawing, project.default_storey)
    CommandBus(project, EventBus()).execute(AddElements(result.accepted_elements()))

    out = tmp_path / "model.ifc"
    assert registry.exporter_for(out).export(project, out) == []
    model = ifcopenshell.open(str(out))
    counts = {c: len(model.by_type(c)) for c in ("IfcWall", "IfcDoor", "IfcWindow", "IfcOpeningElement", "IfcSlab", "IfcSpace")}
    assert counts == {"IfcWall": 5, "IfcDoor": 1, "IfcWindow": 1, "IfcOpeningElement": 2, "IfcSlab": 1, "IfcSpace": 2}

    # Exported GUIDs are stable across exports.
    out2 = tmp_path / "model2.ifc"
    registry.exporter_for(out2).export(project, out2)
    guids = lambda f: sorted(e.GlobalId for e in ifcopenshell.open(str(f)).by_type("IfcWall"))  # noqa: E731
    assert guids(out) == guids(out2)
