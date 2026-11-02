"""Main window: 3D viewport, project browser, AI review, properties, file and edit commands."""

from __future__ import annotations

import csv
import dataclasses
import sys
from pathlib import Path

import numpy as np
import vtkmodules.qt

vtkmodules.qt.PyQtImpl = "PySide6"

from PySide6.QtCore import QStandardPaths, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
)
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor  # noqa: E402
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera  # noqa: E402
from vtkmodules.vtkRenderingCore import vtkRenderer  # noqa: E402

from bimforge.app.commands import (  # noqa: E402
    AddElements,
    AddStorey,
    AddSystem,
    CommandBus,
    CompositeCommand,
    CopyToStorey,
    MergeProject,
    ModifyElement,
    ModifyStorey,
    MoveElements,
    RemoveElements,
    RemoveStorey,
    RemoveSystem,
    SetIssues,
    commit_conversion,
)
from bimforge.app.events import ElementsChanged, EventBus  # noqa: E402
from bimforge.adapters.dwg_importer import ODA_DOWNLOAD  # noqa: E402
from bimforge.app.persistence import FILE_EXTENSION, ProjectFileError, load_project, save_project  # noqa: E402
from bimforge.conversion import ConversionResult, ConversionSettings, PlanConverter  # noqa: E402
from bimforge.core.model import (  # noqa: E402
    CableCarrierSegment,
    Column,
    DistributionSystem,
    Door,
    DuctSegment,
    Element,
    Fixture,
    ImportedElement,
    Issue,
    Opening,
    PipeSegment,
    Project,
    Slab,
    Stair,
    Storey,
    SystemType,
    Wall,
    Window,
)
from bimforge.coordination.checks import CHECKS, run_checks  # noqa: E402
from bimforge.coordination.clash import ClashDetector, element_category, merge_issues, results_to_issues  # noqa: E402
from bimforge.coordination.quantities import EXPORTERS, take_off  # noqa: E402
from bimforge.core.validation import ValidationError  # noqa: E402
from bimforge.disciplines import electrical, plumbing  # noqa: E402
from bimforge.disciplines.catalog import FAMILIES  # noqa: E402
from bimforge.drawing.primitives import Drawing2D  # noqa: E402
from bimforge.geometry.builders import Tessellator  # noqa: E402
from bimforge.plugins.registry import PluginRegistry  # noqa: E402
from bimforge.plugins.sdk import Exporter, Importer, RenderStyle  # noqa: E402
from bimforge.render.vtk_backend import VtkRenderBackend  # noqa: E402
from bimforge.ui.coordination_panel import CoordinationPanel  # noqa: E402
from bimforge.ui.mep_panel import NewSystemDialog, MepPanel, PanelScheduleDialog, SCHEDULE_HEADERS, schedule_rows_for_csv  # noqa: E402
from bimforge.ui.tools import DEFAULT_SYSTEM_COLORS, DrawRunTool, MeasureTool, PlaceFixtureTool, Tool  # noqa: E402
from bimforge.ui.panels import (  # noqa: E402
    LevelsPanel,
    MoveDialog,
    ProjectBrowser,
    PropertiesPanel,
    ReviewPanel,
    SettingsDialog,
)

GHOST = "ghost:"
UNDERLAY = "underlay"
AUTOSAVE_SUFFIX = ".autosave"
AUTOSAVE_INTERVAL_MS = 2 * 60 * 1000
PROJECT_FILTER = f"BIMForge project (*{FILE_EXTENSION})"
STYLES: dict[type, RenderStyle] = {
    Wall: RenderStyle(color=(0.88, 0.86, 0.82)),
    Door: RenderStyle(color=(0.55, 0.36, 0.2)),
    Window: RenderStyle(color=(0.55, 0.78, 0.95), opacity=0.5),
    Slab: RenderStyle(color=(0.62, 0.63, 0.67), category="structure"),
    Stair: RenderStyle(color=(0.78, 0.72, 0.62)),
    Column: RenderStyle(color=(0.5, 0.52, 0.58), category="structure"),
}
CATEGORY_STYLES = {  # imported reference geometry, coloured by discipline
    "architecture": RenderStyle(color=(0.8, 0.78, 0.74)),
    "structure": RenderStyle(color=(0.6, 0.66, 0.74), category="structure"),
    "plumbing": RenderStyle(color=(0.2, 0.5, 0.9), category="plumbing"),
    "mechanical": RenderStyle(color=(0.3, 0.7, 0.45), category="mechanical"),
    "electrical": RenderStyle(color=(0.95, 0.7, 0.15), category="electrical"),
}
GHOST_STYLE = RenderStyle(color=(1.0, 0.55, 0.15), opacity=0.45, category="proposals")
UNDERLAY_STYLE = RenderStyle(color=(0.45, 0.5, 0.6), category=UNDERLAY)


def untitled_autosave_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    return Path(base) / "autosave"


class MainWindow(QMainWindow):
    def __init__(self, registry: PluginRegistry) -> None:
        super().__init__()
        self.resize(1400, 860)
        self.registry = registry
        self.events = EventBus()
        self.tessellator = Tessellator(registry.kernel())
        self.settings = ConversionSettings()
        self.project = Project("Untitled")
        self.bus = CommandBus(self.project, self.events)
        self.project_path: Path | None = None
        self.drawing: Drawing2D | None = None
        self.result: ConversionResult | None = None
        self.selection: list[str] = []
        self.active_storey_id = self.project.default_storey.id
        self.active_level_only = False
        self.tool: Tool | None = None
        self.section_box_on = False
        self._host_of: dict[str, str] = {}  # opening id -> wall id, to rebuild hosts on removal

        self.viewport = QVTKRenderWindowInteractor(self)
        renderer = vtkRenderer()
        renderer.GradientBackgroundOn()
        renderer.SetBackground(0.93, 0.94, 0.96)
        renderer.SetBackground2(0.72, 0.76, 0.82)
        self.viewport.GetRenderWindow().AddRenderer(renderer)
        interactor = self.viewport.GetRenderWindow().GetInteractor()
        interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())
        interactor.AddObserver("LeftButtonPressEvent", self._on_press)
        interactor.AddObserver("LeftButtonReleaseEvent", self._on_release)
        interactor.AddObserver("MouseMoveEvent", self._on_mouse_move)
        interactor.AddObserver("RightButtonPressEvent", self._on_right_press, 1.0)
        self._press_pos = (0, 0)
        self.view = VtkRenderBackend(renderer)
        self.setCentralWidget(self.viewport)

        self.browser = ProjectBrowser()
        self.browser.selection_changed.connect(lambda ids: self.select(ids, source=self.browser))
        self.review = ReviewPanel()
        self.review.acceptance_changed.connect(self._show_ghosts)
        self.review.selection_changed.connect(lambda ids: self._highlight([GHOST + i for i in ids]))
        self.review.commit_requested.connect(self.commit_proposals)
        self.properties = PropertiesPanel()
        self.properties.edit_requested.connect(self.edit_property)
        self.levels = LevelsPanel()
        self.levels.activated.connect(self.set_active_storey)
        self.levels.edit_requested.connect(
            lambda sid, field, value: self._execute(ModifyStorey(sid, {field: value}, label=f"Edit level {field}"))
        )
        self.levels.add_requested.connect(self.add_level)
        self.levels.duplicate_requested.connect(self.duplicate_level)
        self.levels.delete_requested.connect(lambda sid: self._execute(RemoveStorey(sid)))
        self._dock("Project browser", self.browser, Qt.DockWidgetArea.LeftDockWidgetArea)
        self._dock("Levels", self.levels, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.mep = MepPanel()
        self.mep.new_system_requested.connect(self.new_system)
        self.mep.delete_system_requested.connect(lambda sid: self._execute(RemoveSystem(sid)))
        self.mep.assign_requested.connect(self.assign_selection_to_system)
        self.mep.size_requested.connect(self.size_system)
        self.mep.schedule_requested.connect(self.show_panel_schedule)
        self.mep.place_requested.connect(self.start_place)
        self.mep.draw_requested.connect(self.start_draw)
        self.mep.system_selected.connect(self._select_system_members)
        self.coordination = CoordinationPanel()
        self.coordination.run_clashes_requested.connect(self.run_clashes)
        self.coordination.run_checks_requested.connect(self.run_model_checks)
        self.coordination.status_changed.connect(self.set_issue_status)
        self.coordination.comment_requested.connect(self.comment_issue)
        self.coordination.issue_activated.connect(self.zoom_to_issue)
        self._dock("AI review", self.review, Qt.DockWidgetArea.RightDockWidgetArea)
        self._dock("MEP", self.mep, Qt.DockWidgetArea.RightDockWidgetArea)
        self._dock("Coordination", self.coordination, Qt.DockWidgetArea.RightDockWidgetArea)
        docks = {d.objectName(): d for d in self.findChildren(QDockWidget)}
        self.tabifyDockWidget(docks["AI review"], docks["MEP"])
        self.tabifyDockWidget(docks["MEP"], docks["Coordination"])
        docks["AI review"].raise_()
        self._dock("Properties", self.properties, Qt.DockWidgetArea.RightDockWidgetArea)

        self._build_actions()
        self.events.subscribe(ElementsChanged, self._on_elements_changed)
        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self.autosave)
        self.autosave_timer.start(AUTOSAVE_INTERVAL_MS)
        self.viewport.Initialize()
        self._refresh_levels()
        self._update_title()
        self.statusBar().showMessage("Import a DXF plan (Ctrl+I) or open a project (Ctrl+O)")

    # --- setup -----------------------------------------------------------------

    def _dock(self, title: str, widget, area) -> None:
        dock = QDockWidget(title, self)
        dock.setObjectName(title)
        dock.setWidget(widget)
        self.addDockWidget(area, dock)

    def _action(self, menu, text: str, slot, shortcut=None, checkable: bool = False) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.setCheckable(checkable)
        if checkable:
            action.setChecked(True)
            action.toggled.connect(slot)
        else:
            action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _build_actions(self) -> None:
        bar = self.menuBar()
        toolbar = self.addToolBar("Main")
        toolbar.setObjectName("Main")
        file_menu, edit_menu, view_menu = bar.addMenu("&File"), bar.addMenu("&Edit"), bar.addMenu("&View")
        tools_menu = bar.addMenu("&Tools")
        S = QKeySequence.StandardKey

        self._action(file_menu, "New project", self.new_project, S.New)
        toolbar.addAction(self._action(file_menu, "Open…", self.open_project_dialog, S.Open))
        toolbar.addAction(self._action(file_menu, "Save", self.save, S.Save))
        self._action(file_menu, "Save as…", self.save_as, S.SaveAs)
        file_menu.addSeparator()
        toolbar.addAction(self._action(file_menu, "Import…", self.import_dialog, "Ctrl+I"))
        toolbar.addAction(self._action(file_menu, "Export…", self.export_dialog, "Ctrl+E"))
        self._action(file_menu, "Export quantities (BOQ)…", self.export_quantities)
        file_menu.addSeparator()
        self._action(file_menu, "Quit", self.close, S.Quit)

        toolbar.addSeparator()
        toolbar.addAction(self._action(edit_menu, "Undo", self.undo, S.Undo))
        toolbar.addAction(self._action(edit_menu, "Redo", self.redo, S.Redo))
        edit_menu.addSeparator()
        toolbar.addAction(self._action(edit_menu, "Move…", self.move_dialog, "Ctrl+M"))
        toolbar.addAction(self._action(edit_menu, "Delete", self.delete_selection, S.Delete))
        self._action(edit_menu, "Copy to level…", self.copy_to_level_dialog, "Ctrl+Shift+C")
        self._action(edit_menu, "Select all", lambda: self.select([e.id for e in self.project.elements()]), S.SelectAll)
        self._action(edit_menu, "Clear selection", lambda: self.select([]))
        edit_menu.addSeparator()
        toolbar.addAction(self._action(edit_menu, "Conversion settings…", self.edit_settings))

        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" Level: "))
        self.level_combo = QComboBox()
        self.level_combo.setMinimumContentsLength(12)
        self.level_combo.activated.connect(lambda i: self.set_active_storey(self.level_combo.itemData(i)))
        toolbar.addWidget(self.level_combo)
        only_active = QAction("Active level only", self, checkable=True)
        only_active.setShortcut("Ctrl+L")
        only_active.toggled.connect(self._set_active_level_only)
        view_menu.addAction(only_active)
        toolbar.addAction(only_active)
        toolbar.addSeparator()
        toolbar.addAction(self._action(view_menu, "Fit view", self._fit, "F"))
        toolbar.addAction(self._action(view_menu, "Iso", lambda: self._set_view("iso"), "Ctrl+1"))
        toolbar.addAction(self._action(view_menu, "Top", lambda: self._set_view("top"), "Ctrl+2"))
        view_menu.addSeparator()
        self._action(view_menu, "Section box: selection", self.section_box_selection, "Ctrl+B")
        self._action(view_menu, "Section box: active level", self.section_box_level, "Ctrl+Shift+B")
        self._action(view_menu, "Clear section box", lambda: self._set_section_box(None))
        view_menu.addSeparator()
        for category, label in (("architecture", "Architecture"), ("structure", "Structure"),
                                ("plumbing", "Plumbing"), ("mechanical", "Mechanical"), ("electrical", "Electrical"),
                                ("proposals", "AI proposals"), (UNDERLAY, "2D underlay")):
            self._action(view_menu, label, lambda on, c=category: self._toggle_category(c, on), checkable=True)

        self._action(tools_menu, "Measure", lambda: self.set_tool(MeasureTool(self)), "M")
        tools_menu.addSeparator()
        self._action(tools_menu, "Run clash detection", lambda: self.run_clashes(None))
        self._action(tools_menu, "Run model checks", self.run_model_checks)
        # Viewport-only shortcuts, so they never steal keys from text fields.
        for text, slot, key in (("Finish", self.finish_tool, "Return"), ("Cancel tool", self.cancel_tool, "Escape")):
            action = QAction(text, self.viewport)
            action.setShortcut(key)
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.triggered.connect(slot)
            self.viewport.addAction(action)

    # --- project files -------------------------------------------------------------

    def _update_title(self) -> None:
        name = self.project_path.name if self.project_path else "Untitled"
        self.setWindowTitle(f"{name}{'*' if self.bus.is_dirty else ''} - BIMForge")

    def _set_project(self, project: Project, path: Path | None) -> None:
        self._discard_autosave()
        self.project = project
        self.bus = CommandBus(project, self.events)
        self.project_path = path
        self.drawing = self.result = None
        self.selection = []
        self.active_storey_id = project.default_storey.id
        self._host_of.clear()
        self.view.clear()
        self.review.populate([])
        self.properties.show_element(None)
        self.cancel_tool()
        self._on_elements_changed(ElementsChanged(
            added=tuple(e.id for e in project.elements()), storeys=True, systems=True, issues=True))
        self._set_view("iso")
        self._update_title()

    def _confirm_discard(self) -> bool:
        """True if it is safe to replace or close the current project."""
        if not self.bus.is_dirty:
            return True
        B = QMessageBox.StandardButton
        answer = QMessageBox.question(
            self, "Unsaved changes", "Save changes to the current project?", B.Save | B.Discard | B.Cancel
        )
        if answer == B.Save:
            return self.save()
        return answer == B.Discard

    def new_project(self) -> None:
        if self._confirm_discard():
            self._set_project(Project("Untitled"), None)

    def open_project_dialog(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", "", PROJECT_FILTER)
        if path:
            self.open_project(Path(path))

    def open_project(self, path: Path) -> None:
        source, recovered = path, False
        autosave = self._autosave_path_for(path)
        if autosave.exists() and autosave.stat().st_mtime > path.stat().st_mtime:
            B = QMessageBox.StandardButton
            if QMessageBox.question(self, "Recover", f"{path.name} has newer autosaved changes. Recover them?",
                                    B.Yes | B.No) == B.Yes:
                source, recovered = autosave, True
            else:
                autosave.unlink(missing_ok=True)
        try:
            loaded = load_project(source)
        except ProjectFileError as exc:
            QMessageBox.critical(self, "Open failed", str(exc))
            return
        self._set_project(loaded.project, path)
        if recovered:
            self.bus.mark_unsaved()
            self._update_title()
        if loaded.warnings:
            QMessageBox.warning(self, "Project opened with warnings", "\n".join(loaded.warnings))
        self.statusBar().showMessage(f"Opened {path} ({len(loaded.project)} elements)")

    def save(self) -> bool:
        if self.project_path is None:
            return self.save_as()
        return self._save_to(self.project_path)

    def save_as(self) -> bool:
        suggested = str(self.project_path or Path(f"{self.project.name}{FILE_EXTENSION}"))
        path, _ = QFileDialog.getSaveFileName(self, "Save project", suggested, PROJECT_FILTER)
        if not path:
            return False
        path = Path(path)
        if path.suffix.lower() != FILE_EXTENSION:
            path = path.with_suffix(FILE_EXTENSION)
        return self._save_to(path)

    def _save_to(self, path: Path) -> bool:
        try:
            save_project(self.project, path)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return False
        self._discard_autosave()
        if self.project.name == "Untitled":
            self.project.name = path.stem
        self.project_path = path
        self.bus.mark_saved()
        self._update_title()
        self.statusBar().showMessage(f"Saved {path}")
        return True

    def _autosave_path_for(self, path: Path | None) -> Path:
        if path is not None:
            return path.with_name(path.name + AUTOSAVE_SUFFIX)
        return untitled_autosave_dir() / f"untitled-{self.project.id}{FILE_EXTENSION}"

    def autosave(self) -> None:
        if not self.bus.is_dirty or len(self.project) == 0:
            return
        try:
            save_project(self.project, self._autosave_path_for(self.project_path))
        except OSError as exc:
            self.statusBar().showMessage(f"Autosave failed: {exc}")
            return
        self.statusBar().showMessage("Autosaved", 3000)

    def _discard_autosave(self) -> None:
        self._autosave_path_for(self.project_path).unlink(missing_ok=True)

    def offer_untitled_recovery(self) -> None:
        candidates = sorted(untitled_autosave_dir().glob(f"untitled-*{FILE_EXTENSION}"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return
        B = QMessageBox.StandardButton
        answer = QMessageBox.question(self, "Recover", "An unsaved project from a previous session was found. Recover it?",
                                      B.Yes | B.No)
        if answer == B.Yes:
            try:
                loaded = load_project(candidates[0])
            except ProjectFileError as exc:
                QMessageBox.critical(self, "Recovery failed", str(exc))
            else:
                self._set_project(loaded.project, None)
                self.bus.mark_unsaved()
                self._update_title()
        for path in candidates:
            if answer == B.No or path != candidates[0]:
                path.unlink(missing_ok=True)
            # The recovered file is kept until the user saves (then it is discarded).

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self._confirm_discard():
            self._discard_autosave()
            event.accept()
        else:
            event.ignore()

    # --- import / convert / commit ----------------------------------------------

    def import_dialog(self) -> None:
        exts = " ".join(f"*{e}" for imp in self.registry.all(Importer) for e in imp.extensions)
        path, _ = QFileDialog.getOpenFileName(self, "Import drawing or model", "", f"Drawings and models ({exts})")
        if path:
            self.import_drawing(Path(path))

    def _offer_dwg_converter(self, message: str) -> None:
        box = QMessageBox(QMessageBox.Icon.Information, "DWG converter needed", message, parent=self)
        download = box.addButton("Open download page", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is download:
            QDesktopServices.openUrl(QUrl(ODA_DOWNLOAD))

    def _pdf_options(self, path: Path) -> dict | None:
        """Ask which page and at what scale to read a PDF; None if the user cancels."""
        from bimforge.adapters.pdf_importer import DEFAULT_SCALE, PdfImporter

        try:
            scales = PdfImporter.inspect(path)
        except Exception:  # noqa: BLE001 - let the importer report unreadable files
            return {}
        page = 1
        if len(scales) > 1:
            items = [f"Page {i + 1}" + (f"  (scale 1:{s})" if s else "") for i, s in enumerate(scales)]
            choice, ok = QInputDialog.getItem(self, "Import PDF", "Which page is the floor plan?", items, 0, False)
            if not ok:
                return None
            page = items.index(choice) + 1
        detected = scales[page - 1]
        note = f"Scale read from the sheet: 1:{detected}" if detected else "No scale found on the sheet."
        scale, ok = QInputDialog.getInt(self, "Import PDF", f"{note}\nDrawing scale 1 :", detected or DEFAULT_SCALE,
                                        1, 5000)
        return {"page": page, "scale": scale} if ok else None

    def import_drawing(self, path: Path) -> None:
        options: dict = {}
        if path.suffix.lower() == ".pdf":
            options = self._pdf_options(path)
            if options is None:
                return
        try:
            importer = self.registry.importer_for(path)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                imported = importer.import_file(path, options)
            finally:
                QApplication.restoreOverrideCursor()
        except Exception as exc:  # noqa: BLE001 - surface any importer failure to the user
            if ODA_DOWNLOAD in str(exc):
                self._offer_dwg_converter(str(exc))
            else:
                QMessageBox.critical(self, "Import failed", str(exc))
            return
        if imported.project is not None:
            self._import_model(imported.project, imported.warnings, path)
        elif imported.drawing is not None:
            self.drawing = imported.drawing
            self._convert()
        else:
            QMessageBox.information(self, "Import", "Nothing to import from this file.")

    def _import_model(self, source: Project, warnings: list[str], path: Path) -> None:
        was_empty = len(self.project) == 0
        count = len(source)
        if was_empty and self.project.name == "Untitled":
            self.project.name = source.name
        if not self._execute(MergeProject(source, label=f"Import {path.name}")):
            return
        if was_empty:
            self._set_view("iso")
        self.statusBar().showMessage(f"Imported {count} elements from {path.name}. Ctrl+Z to undo.")
        if warnings:
            QMessageBox.warning(self, "Import notes", "\n".join(warnings))

    def _convert(self) -> None:
        assert self.drawing is not None
        storey = self.project.storey(self.active_storey_id) or self.project.default_storey
        self.result = PlanConverter(self.settings).run(self.drawing, storey)
        k = self.result.unit.to_metres
        segs = [((s.start[0] * k, s.start[1] * k, 0.005), (s.end[0] * k, s.end[1] * k, 0.005))
                for s in self.drawing.segments()]
        self.view.clear(UNDERLAY)
        if segs:
            self.view.upsert_lines(UNDERLAY, np.asarray(segs), UNDERLAY_STYLE)
        self.review.populate(self.result.proposals)
        self._show_ghosts()
        self._set_view("iso")
        counts = ", ".join(f"{n} {k}" for k, n in self.result.summary().items()) or "nothing"
        self.statusBar().showMessage(f"Detected {counts} (units: {self.result.unit.symbol}). Review, then commit.")
        notes = list(self.drawing.warnings) + self.result.warnings
        if notes:
            QMessageBox.warning(self, "Import notes", "\n\n".join(notes))

    def _show_ghosts(self) -> None:
        self.view.clear(GHOST)
        if self.result is not None:
            preview = Project("preview")
            preview.site = self.project.site
            for element in self.result.accepted_elements():
                preview.add(element)
            for element in preview.elements():
                mesh = self.tessellator.mesh_for(element, preview)
                if mesh is not None and not mesh.is_empty:
                    self.view.upsert_mesh(GHOST + element.id, mesh, GHOST_STYLE)
        self.view.render()

    def commit_proposals(self) -> None:
        if self.result is None:
            return
        result = self.result
        elements = result.accepted_elements()
        self.result = None
        self.review.populate([])
        self.view.clear(GHOST)
        self.bus.execute(commit_conversion(result, self.project))
        self.statusBar().showMessage(f"Committed {len(elements)} elements. Ctrl+Z to undo.")

    def edit_settings(self) -> None:
        counts: dict[str, int] = {}
        if self.drawing is not None:
            for seg in self.drawing.segments():
                counts[seg.layer] = counts.get(seg.layer, 0) + 1
        dialog = SettingsDialog(self.settings, self, counts)
        if dialog.exec():
            self.settings = dialog.result_settings()
            if self.drawing is not None and self.result is not None:
                self._convert()

    def export_dialog(self) -> None:
        if len(self.project) == 0:
            QMessageBox.information(self, "Export", "The model is empty. Commit some elements first.")
            return
        filters = ";;".join(f"{e.info.name} (*{' *'.join(e.extensions)})" for e in self.registry.all(Exporter))
        suggested = (self.project_path or Path(self.project.name)).with_suffix(".ifc").name
        path, _ = QFileDialog.getSaveFileName(self, "Export model", suggested, filters)
        if not path:
            return
        try:
            warnings = self.registry.exporter_for(Path(path)).export(self.project, Path(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.statusBar().showMessage(f"Exported {path}" + (f" ({len(warnings)} warnings)" if warnings else ""))

    # --- editing -------------------------------------------------------------------

    def _execute(self, command) -> bool:
        """Run a command, reporting validation failures instead of raising."""
        try:
            self.bus.execute(command)
        except ValidationError as exc:
            QMessageBox.warning(self, f"Cannot {command.label.lower()}", str(exc))
            self.properties.refresh()
            return False
        return True

    def edit_property(self, element_id: str, field: str, value: object) -> None:
        self._execute(ModifyElement(element_id, {field: value}, label=f"Edit {field}"))

    def delete_selection(self) -> None:
        if self.selection:
            count = len(self.selection)
            if self._execute(RemoveElements(self.selection, label="Delete")):
                self.statusBar().showMessage(f"Deleted {count} element(s). Ctrl+Z to undo.")

    def move_dialog(self) -> None:
        if not self.selection:
            self.statusBar().showMessage("Select elements to move first")
            return
        dialog = MoveDialog(len(self.selection), self)
        if dialog.exec():
            dx, dy = dialog.offset()
            if dx or dy:
                self._execute(MoveElements(self.selection, dx, dy))

    def undo(self) -> None:
        label = self.bus.undo_label
        self.statusBar().showMessage(f"Undid: {label}" if self.bus.undo() else "Nothing to undo")

    def redo(self) -> None:
        label = self.bus.redo_label
        self.statusBar().showMessage(f"Redid: {label}" if self.bus.redo() else "Nothing to redo")

    # --- levels ------------------------------------------------------------------

    def _refresh_levels(self) -> None:
        if self.project.storey(self.active_storey_id) is None:
            self.active_storey_id = self.project.default_storey.id
        self.levels.rebuild(self.project, self.active_storey_id)
        self.level_combo.blockSignals(True)
        self.level_combo.clear()
        for storey in sorted(self.project.storeys, key=lambda s: -s.elevation):
            self.level_combo.addItem(f"{storey.name}  ({storey.elevation:+.2f})", storey.id)
        self.level_combo.setCurrentIndex(self.level_combo.findData(self.active_storey_id))
        self.level_combo.blockSignals(False)

    def set_active_storey(self, storey_id: str) -> None:
        if storey_id == self.active_storey_id or self.project.storey(storey_id) is None:
            return
        self.active_storey_id = storey_id
        self._refresh_levels()
        self._apply_level_filter()
        self.statusBar().showMessage(f"Active level: {self.project.storey(storey_id).name} (imports go here)")

    def _set_active_level_only(self, enabled: bool) -> None:
        self.active_level_only = enabled
        self._apply_level_filter()

    def _apply_level_filter(self) -> None:
        hidden = []
        if self.active_level_only:
            hidden = [e.id for e in self.project.elements() if e.storey_id != self.active_storey_id]
        self.view.set_hidden(hidden)
        self.view.render()

    def _new_level_above(self) -> Storey:
        top = max(self.project.storeys, key=lambda s: s.elevation)
        names = {s.name for s in self.project.storeys}
        n = len(self.project.storeys)
        while f"Level {n}" in names:
            n += 1
        return Storey(name=f"Level {n}", elevation=round(top.elevation + top.height, 6), height=top.height)

    def add_level(self) -> None:
        storey = self._new_level_above()
        if self._execute(AddStorey(storey)):
            self.set_active_storey(storey.id)

    def duplicate_level(self, storey_id: str) -> None:
        storey = self._new_level_above()
        source_ids = [e.id for e in self.project.elements() if e.storey_id == storey_id]
        command = CompositeCommand([AddStorey(storey), CopyToStorey(source_ids, storey.id)], "Duplicate level")
        if self._execute(command):
            self.set_active_storey(storey.id)

    def copy_to_level_dialog(self) -> None:
        if not self.selection:
            self.statusBar().showMessage("Select elements to copy first")
            return
        storeys = sorted(self.project.storeys, key=lambda s: -s.elevation)
        names = [s.name for s in storeys]
        name, ok = QInputDialog.getItem(self, "Copy to level", "Target level:", names, 0, False)
        if ok:
            self._execute(CopyToStorey(self.selection, storeys[names.index(name)].id))

    # --- tools ---------------------------------------------------------------------

    def active_storey(self) -> Storey:
        return self.project.storey(self.active_storey_id) or self.project.default_storey

    def active_elevation(self) -> float:
        return self.active_storey().elevation

    def set_tool(self, tool: Tool | None) -> None:
        if self.tool is not None:
            self.tool.cancel()
        self.tool = tool
        if tool is not None:
            self.viewport.setFocus()
            self.statusBar().showMessage(tool.hint)

    def finish_tool(self) -> None:
        if self.tool is not None:
            self.tool.finish()

    def cancel_tool(self) -> None:
        if self.tool is not None:
            self.tool.cancel()
            self.tool = None
            self.statusBar().showMessage("Tool stopped")

    def _system_for(self, discipline: str) -> str | None:
        """The system selected in the MEP panel, if it belongs to the given discipline."""
        system = self.project.systems.get(self.mep.current_system() or "")
        if system is None:
            return None
        if system.system_type in plumbing.DRAINAGE_TYPES | plumbing.SUPPLY_TYPES:
            return system.id if discipline == "plumbing" else None
        electrical_types = electrical.CIRCUIT_TYPES | {SystemType.DATA}
        return system.id if discipline == "electrical" and system.system_type in electrical_types else None

    def start_place(self, family_key: str, rotation: float) -> None:
        discipline = FAMILIES[family_key].discipline.value
        tool = PlaceFixtureTool(self, family_key, rotation, self._system_for(discipline))
        self.set_tool(tool)

    def start_draw(self, kind: str, size_mm: float, offset: float) -> None:
        system_id = self._system_for("plumbing" if kind == "pipe" else "electrical")
        self.set_tool(DrawRunTool(self, kind, size_mm, offset, system_id))
        if system_id is None:
            self.statusBar().showMessage(f"{self.tool.hint}  (No matching system selected: the run is unassigned.)")

    # --- MEP systems -------------------------------------------------------------------

    def new_system(self) -> None:
        dialog = NewSystemDialog(self.project, self)
        if not dialog.exec():
            return
        system_type = dialog.type.currentData()
        same_type = [s for s in self.project.systems.values() if s.system_type == system_type]
        name = dialog.name.text().strip() or f"{system_type.value.upper()[:3]}-{len(same_type) + 1}"
        properties = {}
        if system_type in electrical.CIRCUIT_TYPES:
            if dialog.panel.currentData():
                properties["panel_id"] = dialog.panel.currentData()
            color = electrical.circuit_color(len(electrical.circuits(self.project)))
        else:
            color = DEFAULT_SYSTEM_COLORS.get(system_type, (0.5, 0.5, 0.5))
        system = DistributionSystem(name=name, system_type=system_type, color=color, properties=properties)
        self._execute(AddSystem(system))

    def assign_selection_to_system(self, system_id: str) -> None:
        targets = [e for i in self.selection if (e := self.project.get(i)) is not None and hasattr(e, "system_id")]
        if not targets:
            self.statusBar().showMessage("Select fixtures, pipes or containment to assign")
            return
        commands = [ModifyElement(e.id, {"system_id": system_id}) for e in targets]
        if self._execute(CompositeCommand(commands, f"Assign to {self.project.systems[system_id].name}")):
            self.statusBar().showMessage(f"Assigned {len(targets)} element(s)")

    def _select_system_members(self, system_id: str) -> None:
        self.select([e.id for e in self.project.elements() if getattr(e, "system_id", None) == system_id])

    def size_system(self, system_id: str) -> None:
        system = self.project.systems[system_id]
        if system.system_type not in plumbing.DRAINAGE_TYPES | plumbing.SUPPLY_TYPES:
            QMessageBox.information(self, "Size pipes", "Pipe sizing applies to water supply and drainage systems.")
            return
        try:
            results = plumbing.size_network(self.project, system_id)
        except ValueError as exc:
            QMessageBox.warning(self, "Size pipes", str(exc))
            return
        commands = [ModifyElement(r.pipe_id, {"diameter": r.size_mm / 1000}) for r in results
                    if abs(self.project.get(r.pipe_id).diameter - r.size_mm / 1000) > 1e-9]
        if commands and not self._execute(CompositeCommand(commands, f"Size {system.name}")):
            return
        lines = [f"{self.project.get(r.pipe_id).name}: {r.fixture_units:g} FU -> {r.size_mm:g} mm {r.note}"
                 for r in results]
        QMessageBox.information(self, f"Sized {system.name}", "\n".join(lines) or "No pipes in this system.")

    def show_panel_schedule(self, system_id: str) -> None:
        system = self.project.systems[system_id]
        panel_id = system.properties.get("panel_id") if system.system_type in electrical.CIRCUIT_TYPES else None
        if not panel_id:
            QMessageBox.information(self, "Panel schedule", "Select a power or lighting circuit fed from a panel.")
            return
        panel = self.project.get(str(panel_id))
        rows = electrical.panel_schedule(self.project, str(panel_id))
        dialog = PanelScheduleDialog(panel.name if panel else "Panel", rows, self)

        def export_csv() -> None:
            path, _ = QFileDialog.getSaveFileName(dialog, "Export schedule", "panel_schedule.csv", "CSV (*.csv)")
            if path:
                with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(SCHEDULE_HEADERS)
                    writer.writerows(schedule_rows_for_csv(rows))

        def export_sld() -> None:
            path, _ = QFileDialog.getSaveFileName(dialog, "Export single-line diagram", "sld.svg", "SVG (*.svg)")
            if path:
                Path(path).write_text(electrical.single_line_diagram_svg(self.project, str(panel_id)), encoding="utf-8")

        dialog.export_csv_requested.connect(export_csv)
        dialog.export_sld_requested.connect(export_sld)
        dialog.exec()

    # --- coordination ------------------------------------------------------------------

    def run_clashes(self, tests) -> None:
        from bimforge.coordination.clash import DEFAULT_TESTS

        tests = list(tests) if tests is not None else [t for t in DEFAULT_TESTS if t.default]
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            detector = ClashDetector(self.project, self.tessellator)
            results = [r for test in tests for r in detector.run(test)]
        finally:
            QApplication.restoreOverrideCursor()
        issues = merge_issues(self.project.issues, results_to_issues(self.project, results), [t.name for t in tests])
        self._execute(SetIssues(issues, "Run clash detection"))
        self.statusBar().showMessage(f"Clash detection: {len(results)} clash(es) in {len(tests)} test(s)")

    def run_model_checks(self) -> None:
        found = run_checks(self.project)
        self._execute(SetIssues(merge_issues(self.project.issues, found, CHECKS), "Run model checks"))
        self.statusBar().showMessage(f"Model checks: {len(found)} issue(s)")

    def _update_issue(self, issue_id: str, **changes) -> None:
        issues = {i: dataclasses.replace(issue) for i, issue in self.project.issues.items()}
        issues[issue_id] = dataclasses.replace(issues[issue_id], **changes)
        self._execute(SetIssues(issues, "Edit issue"))

    def set_issue_status(self, issue_id: str, status) -> None:
        if self.project.issues[issue_id].status != status:
            self._update_issue(issue_id, status=status)

    def comment_issue(self, issue_id: str) -> None:
        issue = self.project.issues[issue_id]
        text, ok = QInputDialog.getMultiLineText(self, "Issue comment", issue.title, issue.comment)
        if ok:
            self._update_issue(issue_id, comment=text)

    def zoom_to_issue(self, issue_id: str) -> None:
        issue: Issue = self.project.issues[issue_id]
        ids = [i for i in issue.element_ids if i in self.project]
        self.select(ids)
        if issue.point is not None:
            x, y, z = issue.point
            r = 1.5
            self.view.zoom_to((x - r, x + r, y - r, y + r, z - r, z + r))
        elif (bounds := self.view.bounds(ids)) is not None:
            self.view.zoom_to(bounds)
        self.view.render()

    def export_quantities(self) -> None:
        if len(self.project) == 0:
            QMessageBox.information(self, "Quantities", "The model is empty.")
            return
        suggested = (self.project_path or Path(self.project.name)).with_suffix(".xlsx").name
        path, _ = QFileDialog.getSaveFileName(self, "Export quantities", suggested, "Excel (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        path = Path(path)
        exporter = EXPORTERS.get(path.suffix.lower())
        if exporter is None:
            QMessageBox.warning(self, "Quantities", "Choose a .xlsx or .csv file name.")
            return
        rows = take_off(self.project)
        try:
            exporter(rows, path)
        except (OSError, ImportError) as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.statusBar().showMessage(f"Exported {len(rows)} quantity lines to {path}")

    # --- section box ---------------------------------------------------------------------

    def _set_section_box(self, bounds) -> None:
        self.view.set_section_box(bounds)
        self.section_box_on = bounds is not None
        self.view.render()

    def section_box_selection(self) -> None:
        bounds = self.view.bounds(self.selection)
        if bounds is None:
            self.statusBar().showMessage("Select elements to box")
            return
        m = 0.5
        b = bounds
        self._set_section_box((b[0] - m, b[1] + m, b[2] - m, b[3] + m, b[4] - m, b[5] + m))
        self.view.zoom_to(bounds)
        self.view.render()

    def section_box_level(self) -> None:
        everything = self.view.bounds([e.id for e in self.project.elements()])
        if everything is None:
            return
        storey = self.active_storey()
        self._set_section_box((everything[0] - 1, everything[1] + 1, everything[2] - 1, everything[3] + 1,
                               storey.elevation - 0.6, storey.elevation + storey.height - 0.01))

    # --- selection ----------------------------------------------------------------

    def select(self, ids: list[str], source: object | None = None) -> None:
        self.selection = [i for i in dict.fromkeys(ids) if i in self.project]
        if source is not self.browser:
            self.browser.select_ids(self.selection)
        if len(self.selection) == 1:
            self.properties.show_element(self.project.get(self.selection[0]))
        elif self.selection:
            self.properties.show_message(f"{len(self.selection)} elements selected")
        else:
            self.properties.show_element(None)
        self._highlight(self.selection)

    # --- model -> view sync -------------------------------------------------------

    def _on_elements_changed(self, change: ElementsChanged) -> None:
        affected = set(change.added) | set(change.modified)
        for element_id in change.removed:
            self.view.remove(element_id)
            host = self._host_of.pop(element_id, None)
            if host:
                affected.add(host)
        for element_id in list(affected):
            element = self.project.get(element_id)
            if isinstance(element, Opening):
                self._host_of[element.id] = element.host_id
                affected.add(element.host_id)
            elif isinstance(element, Wall):  # opening fillers depend on their host's geometry
                affected.update(o.id for o in self.project.hosted_openings(element.id))
        for element_id in affected:
            element = self.project.get(element_id)
            if element is not None:
                self._render_element(element)

        self.browser.rebuild(self.project)
        if change.storeys:
            self._refresh_levels()
        if change.systems or change.added or change.removed or change.modified:
            self.mep.rebuild(self.project)
        if change.issues:
            self.coordination.rebuild(self.project)
        if self.active_level_only and (change.added or change.storeys):
            self._apply_level_filter()
        removed = set(change.removed)
        if removed & set(self.selection):
            self.select([i for i in self.selection if i not in removed])
        elif set(self.selection) & affected:
            self.select(self.selection)  # refresh properties and re-apply highlight to new actors
        self._update_title()
        self.view.render()

    def _render_element(self, element: Element) -> None:
        mesh = self.tessellator.mesh_for(element, self.project)
        if mesh is None or mesh.is_empty:
            self.view.remove(element.id)
            return
        self.view.upsert_mesh(element.id, mesh, self._style_for(element))

    def _style_for(self, element: Element) -> RenderStyle:
        if isinstance(element, ImportedElement):
            return CATEGORY_STYLES.get(element.category, CATEGORY_STYLES["architecture"])
        if isinstance(element, (PipeSegment, CableCarrierSegment, DuctSegment)):
            system = self.project.systems.get(element.system_id) if element.system_id else None
            return RenderStyle(color=system.color if system else (0.6, 0.6, 0.6), category=element_category(element))
        if isinstance(element, Fixture):
            category = element_category(element)
            return RenderStyle(color=(0.97, 0.97, 0.95) if category == "plumbing" else (0.98, 0.8, 0.3),
                               category=category, show_edges=True)
        return STYLES.get(type(element), RenderStyle())

    # --- interaction -------------------------------------------------------------

    def _on_press(self, interactor, _event) -> None:
        self._press_pos = interactor.GetEventPosition()

    def _tool_point(self, x: int, y: int):
        if self.tool is None:
            return None
        if self.tool.needs_surface:
            return self.view.pick_surface(x, y)
        return self.view.pick_on_plane(x, y, self.tool.work_height())

    def _on_mouse_move(self, interactor, _event) -> None:
        if self.tool is not None:
            x, y = interactor.GetEventPosition()
            self.tool.hover(self._tool_point(x, y), bool(interactor.GetShiftKey()))

    def _on_right_press(self, interactor, _event) -> None:
        if self.tool is not None:
            self.finish_tool()

    def _on_release(self, interactor, _event) -> None:
        x, y = interactor.GetEventPosition()
        if abs(x - self._press_pos[0]) + abs(y - self._press_pos[1]) > 3:
            return  # it was an orbit drag, not a click
        if self.tool is not None:
            point = self._tool_point(x, y)
            if point is not None:
                self.tool.click(point, bool(interactor.GetShiftKey()))
            return
        key = self.view.pick(x, y)
        if key and key.startswith(GHOST):
            element_id = key.removeprefix(GHOST)
            proposal = next((p for p in self.result.proposals if p.element.id == element_id), None) if self.result else None
            self.review.select_element(element_id)
            self.properties.show_element(proposal.element if proposal else None, editable=False)
            self._highlight([key])
            return
        if interactor.GetControlKey():
            toggled = [i for i in self.selection if i != key] if key in self.selection else self.selection + [key]
            self.select([i for i in toggled if i])
        else:
            self.select([key] if key else [])

    def _highlight(self, keys: list[str]) -> None:
        self.view.set_highlight(keys)
        self.view.render()

    def _toggle_category(self, category: str, visible: bool) -> None:
        self.view.set_category_visible(category, visible)
        self.view.render()

    def _fit(self) -> None:
        self.view.fit_view()
        self.view.render()

    def _set_view(self, preset: str) -> None:
        self.view.set_view(preset)
        self.view.render()


def run(path: Path | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("BIMForge")
    app.setOrganizationName("BIMForge")
    window = MainWindow(PluginRegistry.with_builtins())
    window.show()
    if path is None:
        window.offer_untitled_recovery()
    elif path.suffix.lower() == FILE_EXTENSION:
        window.open_project(path)
    else:
        window.import_drawing(path)
    return app.exec()
