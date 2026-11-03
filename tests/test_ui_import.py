import importlib

import pytest


@pytest.mark.parametrize("module", ["main_window", "panels", "mep_panel", "coordination_panel", "tools", "ribbon", "theme", "icons", "sheets",
                                    "schedules_dialog", "edit_dialogs"])
def test_ui_modules_import(module):
    """Catches syntax and import errors in UI code, which the other tests never load."""
    pytest.importorskip("PySide6")
    pytest.importorskip("vtk")
    importlib.import_module(f"bimforge.ui.{module}")
