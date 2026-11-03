"""Main window: 3D viewport, project browser, detection review, properties, file and edit commands."""

from __future__ import annotations

import csv
import dataclasses
import re
import sys
import textwrap
import threading
import time
from pathlib import Path

import numpy as np
import vtkmodules.qt

vtkmodules.qt.PyQtImpl = "PySide6"

from PySide6.QtCore import QEvent, QEventLoop, QSettings, QStandardPaths, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QCursor, QDesktopServices, QKeySequence  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QDialog,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
)
from shapely.geometry import Polygon  # noqa: E402
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor  # noqa: E402
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera  # noqa: E402
from vtkmodules.vtkRenderingCore import vtkRenderer  # noqa: E402

from bimforge.app.commands import (  # noqa: E402
    AddElements,
    AddStorey,
    AddSystem,
    CommandBus,
    CompositeCommand,
    CopyElements,
    CopyToStorey,
    MergeProject,
    ModifyElement,
    ModifyStorey,
    MoveElements,
    RemoveElements,
    RemoveStorey,
    RemoveSystem,
    SetIssues,
    TransformElements,
    commit_conversion,
    flipped_stair,
    mirror_across,
    rotation_about,
)
from bimforge.app.events import ElementsChanged, EventBus  # noqa: E402
from bimforge.adapters.dwg_importer import ODA_DOWNLOAD  # noqa: E402
from bimforge.app.persistence import FILE_EXTENSION, ProjectFileError, load_project, save_project  # noqa: E402
from bimforge.conversion import ConversionResult, ConversionSettings, PlanConverter  # noqa: E402
from bimforge.conversion.rooms import rooms_from_walls  # noqa: E402
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
    Space,
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
from bimforge.geometry.mesh_ops import thin_lines  # noqa: E402
from bimforge.plugins.registry import PluginRegistry  # noqa: E402
from bimforge.plugins.sdk import DISPLAY_STYLES, Exporter, Importer, RenderStyle  # noqa: E402
from bimforge.render.vtk_backend import VtkRenderBackend  # noqa: E402
from bimforge.ui.coordination_panel import CoordinationPanel  # noqa: E402
from bimforge.ui.mep_panel import NewSystemDialog, MepPanel, PanelScheduleDialog, SCHEDULE_HEADERS, schedule_rows_for_csv  # noqa: E402
from bimforge.ui.edit_dialogs import ArrayDialog  # noqa: E402
from bimforge.ui.icons import icon  # noqa: E402
from bimforge.ui.notes_dialog import show_notes  # noqa: E402
from bimforge.ui.ribbon import Ribbon  # noqa: E402
from bimforge.ui.schedules_dialog import SchedulesDialog  # noqa: E402
from bimforge.ui.sheets import export_sheets  # noqa: E402
from bimforge.ui.theme import THEMES, apply_theme  # noqa: E402
from bimforge.ui.tools import (  # noqa: E402
    DEFAULT_SYSTEM_COLORS,
    BoxSelectTool,
    CopyTool,
    DrawRunTool,
    DrawSlabTool,
    DrawStairTool,
    DrawWallTool,
    MeasureTool,
    MoveTool,
    PlaceColumnTool,
    PlaceDoorTool,
    PlaceFixtureTool,
    PlaceWindowTool,
    Tool,
)
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
RECENT_FILES = 10
NUDGE = 0.1  # an arrow key slides the view by this share of its size
PANEL_WIDTHS = (220, 300)  # px: left and right panels when the app first opens
WHEEL_ZOOM = 1.2  # how much one wheel notch zooms
UNDERLAY_LIMIT = 150_000  # lines of the 2D drawing to display; detection still reads them all
UNDERLAY_SHORTEST = 0.03  # m; on a drawing above the limit, shorter lines aren't displayed
ROOM_LABEL_WIDTH = 14  # characters per line of a room's name in the 3D view
MULTISAMPLES = 4  # smooth edges (VTK's FXAA option turned the whole view black in testing)
LAYOUT_VERSION = 3  # raise when docks or toolbars change, so an old saved layout is ignored
SNAP_NAMES = {"end": "Snap: wall end", "middle": "Snap: wall middle", "wall": "Snap: on wall",
              "straight": "Snap: straight", "grid": "Snap: grid"}
STYLES: dict[type, RenderStyle] = {
    Wall: RenderStyle(color=(0.88, 0.86, 0.82)),
    Door: RenderStyle(color=(0.55, 0.36, 0.2)),
    Window: RenderStyle(color=(0.55, 0.78, 0.95), opacity=0.5),
    Slab: RenderStyle(color=(0.62, 0.63, 0.67), category="structure"),
    Stair: RenderStyle(color=(0.78, 0.72, 0.62)),
    Column: RenderStyle(color=(0.5, 0.52, 0.58), category="structure"),
    Space: RenderStyle(color=(0.45, 0.68, 0.95), opacity=0.25, category="rooms"),
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


def _underlay_lines(drawing: Drawing2D, k: float) -> np.ndarray:
    """The drawing's lines as (N, 2, 3) metres, without duplicates, and few enough to stay smooth."""
    ends = [(s.start[0] * k, s.start[1] * k, s.end[0] * k, s.end[1] * k) for s in drawing.segments()]
    if not ends:
        return np.zeros((0, 2, 3))
    xy = thin_lines(np.asarray(ends, dtype=np.float64), UNDERLAY_LIMIT, UNDERLAY_SHORTEST)
    lines = np.full((len(xy), 2, 3), 0.005)
    lines[:, 0, :2], lines[:, 1, :2] = xy[:, :2], xy[:, 2:]
    return lines


def _write_quantities(project: Project, exporter, path: Path) -> list:
    rows = take_off(project)
    exporter(rows, path)
    return rows


class _Cancelled(Exception):
    """The user cancelled a background job."""


class MainWindow(QMainWindow):
    def __init__(self, registry: PluginRegistry) -> None:
        super().__init__()
        self.resize(1400, 860)
        self.setAcceptDrops(True)
        self.prefs = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "BIMForge", "BIMForge")
        self.theme_name = str(self.prefs.value("view/theme", "light"))
        if self.theme_name not in THEMES:
            self.theme_name = "light"
        theme = apply_theme(QApplication.instance(), self.theme_name)
        self._grid_on = str(self.prefs.value("view/grid", "true")) == "true"
        self._grid_signature: tuple | None = None
        self._user_hidden: set[str] = set()
        self._docks_before_full = None
        self._docks_sized = False
        self._drawing_labels: list = []  # text blocks of the imported drawing, so Detect rooms can use them too
        self._labels_storey: str | None = None
        self._tool_defaults: dict[tuple[str, str], float] = {}
        self._option_widgets: list = []
        self._schedules_dialog: SchedulesDialog | None = None
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
        self._ghosts: dict[str, tuple] = {}  # preview key -> shape signature
        self.active_storey_id = self.project.default_storey.id
        self.active_level_only = False
        self.tool: Tool | None = None
        self.section_box_on = False
        self._host_of: dict[str, str] = {}  # opening id -> wall id, to rebuild hosts on removal

        self.viewport = QVTKRenderWindowInteractor(self)
        renderer = vtkRenderer()
        self.viewport.GetRenderWindow().SetMultiSamples(MULTISAMPLES)
        self.viewport.GetRenderWindow().AddRenderer(renderer)
        interactor = self.viewport.GetRenderWindow().GetInteractor()
        style = self._style = vtkInteractorStyleTrackballCamera()
        interactor.SetInteractorStyle(style)
        # An observer on the style replaces its own handling of that event.
        style.AddObserver("MouseWheelForwardEvent", lambda *_: self._on_wheel(WHEEL_ZOOM))
        style.AddObserver("MouseWheelBackwardEvent", lambda *_: self._on_wheel(1 / WHEEL_ZOOM))
        style.AddObserver("LeftButtonPressEvent", self._on_left_down)
        style.AddObserver("LeftButtonReleaseEvent", self._on_left_up)
        style.AddObserver("StartInteractionEvent", lambda *_: self._user_moved_view())
        self._pan_mode = False
        self._panning = False
        self._fitted = False  # the view shows the fit, so a resized window should fit again
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(120)
        self._resize_timer.timeout.connect(self._after_resize)
        self.viewport.installEventFilter(self)
        # VTK's built-in single-letter keys would otherwise fight the app: "e" and "q" exit, "w" wireframes.
        interactor.RemoveObservers("CharEvent")
        interactor.AddObserver("LeftButtonPressEvent", self._on_press)
        interactor.AddObserver("LeftButtonReleaseEvent", self._on_release)
        interactor.AddObserver("MouseMoveEvent", self._on_mouse_move)
        interactor.AddObserver("RightButtonPressEvent", self._on_right_press, 1.0)
        interactor.AddObserver("RightButtonReleaseEvent", self._on_right_release)
        self._press_pos = (0, 0)
        self._right_press_pos = (0, 0)
        self.view = VtkRenderBackend(renderer)
        self.view.set_background(theme.view_top, theme.view_bottom)
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
        self._dock("Detection review", self.review, Qt.DockWidgetArea.RightDockWidgetArea)
        self._dock("MEP", self.mep, Qt.DockWidgetArea.RightDockWidgetArea)
        self._dock("Coordination", self.coordination, Qt.DockWidgetArea.RightDockWidgetArea)
        docks = {d.objectName(): d for d in self.findChildren(QDockWidget)}
        self.tabifyDockWidget(docks["Detection review"], docks["MEP"])
        self.tabifyDockWidget(docks["MEP"], docks["Coordination"])
        docks["Detection review"].raise_()
        self._dock("Properties", self.properties, Qt.DockWidgetArea.RightDockWidgetArea)
        self.docks = {d.objectName(): d for d in self.findChildren(QDockWidget)}

        self._build_actions()
        self._build_status_bar()
        self.events.subscribe(ElementsChanged, self._on_elements_changed)
        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self.autosave)
        self.autosave_timer.start(AUTOSAVE_INTERVAL_MS)
        self.viewport.Initialize()
        self.view.show_axes(interactor)
        self._refresh_levels()
        self._update_grid()
        self._update_title()
        self._default_layout = self.saveState(LAYOUT_VERSION)
        geometry, layout = self.prefs.value("window/geometry"), self.prefs.value("window/layout")
        if geometry is not None:
            self.restoreGeometry(geometry)
        if layout is not None:
            self.restoreState(layout, LAYOUT_VERSION)
        self.statusBar().showMessage("Import a drawing (Ctrl+I), open a project (Ctrl+O), or draw a wall (W). F1 lists the shortcuts.")

    # --- setup -----------------------------------------------------------------

    def _dock(self, title: str, widget, area) -> None:
        dock = QDockWidget(title, self)
        dock.setObjectName(title)
        dock.setWidget(widget)
        self.addDockWidget(area, dock)

    def _action(self, menu, text: str, slot, shortcut=None, checkable: bool = False, icon_name: str | None = None,
                label: str | None = None, checked: bool = True) -> QAction:
        """A command for the menu; the ribbon shows the same object using its icon and short label."""
        action = QAction(text, self)
        if icon_name:
            action.setIcon(icon(icon_name))
        action.setIconText(label or text.removesuffix("…"))
        if shortcut:
            action.setShortcut(shortcut)
            keys = QKeySequence(shortcut).toString(QKeySequence.SequenceFormat.NativeText)
            action.setToolTip(f"{text.removesuffix('…')}  ({keys})")
        else:
            action.setToolTip(text.removesuffix("…"))
        action.setCheckable(checkable)
        if checkable:
            action.setChecked(checked)
            action.toggled.connect(slot)
        else:
            action.triggered.connect(slot)
        if menu is not None:
            menu.addAction(action)
        return action

    def _build_actions(self) -> None:
        bar = self.menuBar()
        file_menu, edit_menu, model_menu = bar.addMenu("&File"), bar.addMenu("&Edit"), bar.addMenu("&Model")
        view_menu, tools_menu, documents_menu = bar.addMenu("&View"), bar.addMenu("&Tools"), bar.addMenu("&Documents")
        help_menu = bar.addMenu("&Help")
        S = QKeySequence.StandardKey
        A = self._action

        # File
        new = A(file_menu, "New project", self.new_project, S.New, icon_name="new", label="New")
        open_ = A(file_menu, "Open…", self.open_project_dialog, S.Open, icon_name="open")
        self.recent_menu = file_menu.addMenu("Open recent")
        self.recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        save = A(file_menu, "Save", self.save, S.Save, icon_name="save")
        A(file_menu, "Save as…", self.save_as, S.SaveAs)
        file_menu.addSeparator()
        import_ = A(file_menu, "Import…", self.import_dialog, "Ctrl+I", icon_name="import")
        export = A(file_menu, "Export…", self.export_dialog, "Ctrl+E", icon_name="export")
        file_menu.addSeparator()
        A(file_menu, "Quit", self.close, S.Quit)

        # Edit
        undo = A(edit_menu, "Undo", self.undo, S.Undo, icon_name="undo")
        redo = A(edit_menu, "Redo", self.redo, S.Redo, icon_name="redo")
        edit_menu.addSeparator()
        box = A(edit_menu, "Box select", lambda: self.set_tool(BoxSelectTool(self)), "B", icon_name="select", label="Box\nselect")
        select_all = A(edit_menu, "Select all", lambda: self.select([e.id for e in self.project.elements()]), S.SelectAll,
                       icon_name="select_all", label="Select\nall")
        clear = A(edit_menu, "Clear selection", lambda: self.select([]), icon_name="clear", label="Clear")
        edit_menu.addSeparator()
        move = A(edit_menu, "Move", lambda: self._start_on_selection(MoveTool), "V", icon_name="move")
        A(edit_menu, "Move by distance…", self.move_dialog, "Ctrl+M")
        copy = A(edit_menu, "Copy", lambda: self._start_on_selection(CopyTool), "Ctrl+D", icon_name="copy")
        rotate = A(edit_menu, "Rotate…", self.rotate_selection, "R", icon_name="rotate")
        mirror_x = A(edit_menu, "Mirror left to right", lambda: self.mirror_selection("x"), icon_name="mirror_x", label="Mirror\nX")
        mirror_y = A(edit_menu, "Mirror front to back", lambda: self.mirror_selection("y"), icon_name="mirror_y", label="Mirror\nY")
        array = A(edit_menu, "Array…", self.array_selection, "Ctrl+Shift+A", icon_name="array")
        delete = A(edit_menu, "Delete", self.delete_selection, S.Delete, icon_name="delete")
        edit_menu.addSeparator()
        copy_level = A(edit_menu, "Copy to level…", self.copy_to_level_dialog, "Ctrl+Shift+C", icon_name="copy_level", label="Copy to\nlevel")
        flip = A(edit_menu, "Flip stair direction", self.flip_selected_stairs, icon_name="flip", label="Flip\nstair")
        edit_menu.addSeparator()
        settings = A(edit_menu, "Conversion settings…", self.edit_settings, icon_name="settings", label="Detect\nsettings")

        # Model
        wall = A(model_menu, "Wall", lambda: self.set_tool(DrawWallTool(self)), "W", icon_name="wall")
        door = A(model_menu, "Door", lambda: self.set_tool(PlaceDoorTool(self)), "D", icon_name="door")
        window = A(model_menu, "Window", lambda: self.set_tool(PlaceWindowTool(self)), "N", icon_name="window")
        column = A(model_menu, "Column", lambda: self.set_tool(PlaceColumnTool(self)), "C", icon_name="column")
        slab = A(model_menu, "Slab", lambda: self.set_tool(DrawSlabTool(self)), "S", icon_name="slab")
        stair = A(model_menu, "Stair", lambda: self.set_tool(DrawStairTool(self)), "T", icon_name="stair")
        model_menu.addSeparator()
        rooms = A(model_menu, "Detect rooms", self.detect_rooms, icon_name="room", label="Detect\nrooms")
        model_menu.addSeparator()
        add_level = A(model_menu, "Add level", self.add_level, icon_name="level", label="Add\nlevel")

        # View
        fit = A(view_menu, "Fit view", self._fit, "F", icon_name="fit", label="Fit")
        zoom = A(view_menu, "Zoom to selection", self.zoom_selection, "Z", icon_name="zoom", label="Zoom to\nselection")
        zoom_in = A(view_menu, "Zoom in", lambda: self._zoom_button(WHEEL_ZOOM ** 2), "=", icon_name="zoom_in", label="Zoom\nin")
        zoom_out = A(view_menu, "Zoom out", lambda: self._zoom_button(WHEEL_ZOOM ** -2), "-", icon_name="zoom_out", label="Zoom\nout")
        pan = A(view_menu, "Pan", self._set_pan_mode, "P", checkable=True, icon_name="pan", checked=False)
        full = A(view_menu, "Full view (hide panels)", self._set_full_view, "F11", checkable=True, icon_name="fullview",
                 label="Full\nview", checked=False)
        view_menu.addSeparator()
        presets = [A(view_menu, text, lambda _=False, p=preset: self._set_view(p), f"Ctrl+{i + 1}", icon_name=preset, label=label)
                   for i, (preset, text, label) in enumerate((
                       ("iso", "Isometric view", "Iso"), ("top", "Top view", "Top"), ("front", "Front view", "Front"),
                       ("back", "Back view", "Back"), ("left", "Left view", "Left"), ("right", "Right view", "Right"),
                       ("bottom", "Bottom view", "Bottom")))]
        ortho = A(view_menu, "Orthographic", self._set_orthographic, "O", checkable=True, icon_name="ortho", label="Ortho",
                  checked=False)
        view_menu.addSeparator()
        styles = QActionGroup(self)
        current_style = str(self.prefs.value("view/style", "edges"))
        self.view.set_display_style(current_style if current_style in DISPLAY_STYLES else "edges")
        style_labels = {"shaded": "Shaded", "edges": "Edges", "hidden": "Hidden\nline", "wireframe": "Wire-\nframe", "xray": "X-ray"}
        style_actions = []
        for key, text in DISPLAY_STYLES.items():
            action = A(view_menu, text, lambda on, k=key: on and self._set_display_style(k), checkable=True, icon_name=key,
                       label=style_labels[key], checked=key == current_style)
            styles.addAction(action)
            style_actions.append(action)
        view_menu.addSeparator()
        grid = A(view_menu, "Ground grid", self._set_grid_visible, "G", checkable=True, icon_name="grid", label="Grid",
                 checked=self._grid_on)
        hide = A(view_menu, "Hide selected", self.hide_selection, "H", icon_name="hide", label="Hide")
        isolate = A(view_menu, "Isolate selected", self.isolate_selection, "I", icon_name="isolate", label="Isolate")
        show_all = A(view_menu, "Show all", self.show_all, "Shift+H", icon_name="show", label="Show\nall")
        self.only_active = A(view_menu, "Active level only", self._set_active_level_only, "Ctrl+L", checkable=True,
                             icon_name="level_only", label="Active\nlevel only", checked=False)
        view_menu.addSeparator()
        box_selected = A(view_menu, "Section box: selection", self.section_box_selection, "Ctrl+B", icon_name="section", label="Section\nselection")
        box_level = A(view_menu, "Section box: active level", self.section_box_level, "Ctrl+Shift+B", icon_name="section_level", label="Section\nlevel")
        box_clear = A(view_menu, "Clear section box", lambda: self._set_section_box(None), icon_name="section_clear", label="Clear\nsection")
        view_menu.addSeparator()
        for category, label in (("architecture", "Architecture"), ("structure", "Structure"), ("rooms", "Rooms"),
                                ("plumbing", "Plumbing"), ("mechanical", "Mechanical"), ("electrical", "Electrical"),
                                ("proposals", "Detected proposals"), (UNDERLAY, "2D underlay")):
            A(view_menu, label, lambda on, c=category: self._toggle_category(c, on), checkable=True)
        view_menu.addSeparator()
        panels_menu = view_menu.addMenu("Panels")
        for dock in self.docks.values():
            panels_menu.addAction(dock.toggleViewAction())
        reset_layout = A(view_menu, "Reset window layout", self.reset_layout, icon_name="panel", label="Reset\nlayout")
        dark = A(view_menu, "Dark theme", self._set_dark_theme, checkable=True, icon_name="theme", label="Dark\ntheme",
                 checked=self.theme_name == "dark")

        # Tools
        measure = A(tools_menu, "Measure", lambda: self.set_tool(MeasureTool(self)), "M", icon_name="measure")
        tools_menu.addSeparator()
        clashes = A(tools_menu, "Run clash detection", lambda: self.run_clashes(None), icon_name="clash", label="Clash\ndetection")
        checks = A(tools_menu, "Run model checks", self.run_model_checks, icon_name="check", label="Model\nchecks")
        issues = A(tools_menu, "Issues panel", lambda: self._raise_dock("Coordination"), icon_name="issues", label="Issues")
        tools_menu.addSeparator()
        mep_panel = A(tools_menu, "MEP panel", lambda: self._raise_dock("MEP"), icon_name="panel", label="MEP\npanel")
        new_system = A(tools_menu, "New system…", self.new_system, icon_name="system", label="New\nsystem")

        # Documents
        schedules = A(documents_menu, "Schedules", self.show_schedules, "Ctrl+Shift+T", icon_name="schedule")
        sheets = A(documents_menu, "Export PDF plan sheets…", self.export_plan_sheets, icon_name="sheet", label="PDF\nsheets")
        quantities = A(documents_menu, "Export quantities (BOQ)…", self.export_quantities, icon_name="boq", label="Quantities\n(BOQ)")
        image = A(documents_menu, "Save view as image…", self.save_view_image, icon_name="image", label="Save\nimage")

        # Help
        A(help_menu, "Keyboard shortcuts", self.show_shortcuts, "F1", icon_name="help")
        A(help_menu, "About BIMForge", self.show_about)

        self.ribbon = Ribbon()
        home = self.ribbon.page("Home")
        home.group("Project", [new, open_, save])
        home.group("Exchange", [import_, export])
        home.group("History", [undo, redo])
        home.group("Navigate", [fit, pan, full])
        home.group("Detection", [settings])
        home.group("Levels", [add_level, self.only_active])
        home.group("Appearance", [dark, reset_layout])
        page = self.ribbon.page("Model")
        page.group("Architecture", [wall, door, window, stair])
        page.group("Structure", [column, slab])
        page.group("Spaces", [rooms])
        page = self.ribbon.page("Edit")
        page.group("Select", [box, select_all, clear])
        page.group("Modify", [move, copy, rotate, mirror_x, mirror_y, array])
        page.group("Remove", [delete])
        page.group("Other", [copy_level, flip])
        page = self.ribbon.page("View")
        page.group("Navigate", [fit, zoom, zoom_in, zoom_out, pan, full])
        page.group("Direction", presets + [ortho])
        page.group("Display", style_actions)
        page.group("Show", [grid, hide, isolate, show_all])
        page.group("Section", [box_selected, box_level, box_clear])
        page = self.ribbon.page("MEP")
        page.group("Systems", [mep_panel, new_system])
        page = self.ribbon.page("Coordinate")
        page.group("Check", [clashes, checks, issues])
        page.group("Inspect", [measure])
        page = self.ribbon.page("Documents")
        page.group("Tables", [schedules, quantities])
        page.group("Output", [sheets, image])

        ribbon_bar = QToolBar("Ribbon")
        ribbon_bar.setObjectName("Ribbon")
        ribbon_bar.setMovable(False)
        ribbon_bar.setFloatable(False)
        ribbon_bar.addWidget(self.ribbon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, ribbon_bar)
        self.addToolBarBreak()

        # The bar under the ribbon: always the level, plus whatever the running tool can change.
        self.context_bar = QToolBar("Options")
        self.context_bar.setObjectName("ContextBar")
        self.context_bar.setMovable(False)
        self.context_bar.setFloatable(False)
        self.context_bar.addWidget(QLabel("Level"))
        self.level_combo = QComboBox()
        self.level_combo.setMinimumContentsLength(14)
        self.level_combo.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.level_combo.activated.connect(lambda i: self.set_active_storey(self.level_combo.itemData(i)))
        self.context_bar.addWidget(self.level_combo)
        self.context_bar.addSeparator()
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.context_bar)

        self._context_commands = [move, copy, rotate, mirror_x, mirror_y, array, delete, None, hide, isolate, show_all, None,
                                  zoom, fit]

        # Viewport-only shortcuts, so they never steal keys from text fields.
        for text, slot, key in (("Finish", self.finish_tool, "Return"), ("Cancel tool", self._escape, "Escape"),
                                ("Pan left", lambda: self._nudge(-NUDGE, 0), "Left"), ("Pan right", lambda: self._nudge(NUDGE, 0), "Right"),
                                ("Pan up", lambda: self._nudge(0, NUDGE), "Up"), ("Pan down", lambda: self._nudge(0, -NUDGE), "Down")):
            action = QAction(text, self.viewport)
            action.setShortcut(key)
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.triggered.connect(slot)
            self.viewport.addAction(action)

    def _build_status_bar(self) -> None:
        self.coordinates = QLabel("X 0.000   Y 0.000 m")
        self.snap_label = QLabel()
        self.selection_label = QLabel("Nothing selected")
        for label in (self.snap_label, self.selection_label, self.coordinates):
            self.statusBar().addPermanentWidget(label)
        self.coordinates.setMinimumWidth(170)
        self.snap_label.hide()

    def show_snap(self, kind: str) -> None:
        text = SNAP_NAMES.get(kind, "")
        self.snap_label.setText(text)
        self.snap_label.setVisible(bool(text))

    def _raise_dock(self, name: str) -> None:
        dock = self.docks[name]
        dock.show()
        dock.raise_()

    def reset_layout(self) -> None:
        self.restoreState(self._default_layout, LAYOUT_VERSION)

    def _set_dark_theme(self, enabled: bool) -> None:
        self.theme_name = "dark" if enabled else "light"
        self.prefs.setValue("view/theme", self.theme_name)
        self._apply_viewport_theme(apply_theme(QApplication.instance(), self.theme_name))

    def _apply_viewport_theme(self, theme) -> None:
        self.view.set_background(theme.view_top, theme.view_bottom)
        self.view.render()

    def _remember_file(self, path: Path) -> None:
        names = [str(path)] + [n for n in self._recent_files() if n != str(path)]
        self.prefs.setValue("files/recent", names[:RECENT_FILES])

    def _recent_files(self) -> list[str]:
        value = self.prefs.value("files/recent", [])
        return [value] if isinstance(value, str) else list(value or [])

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        names = self._recent_files()
        if not names:
            self.recent_menu.addAction("No recent files").setEnabled(False)
        for name in names:
            self.recent_menu.addAction(name).triggered.connect(lambda _=False, n=name: self.open_path(Path(n)))

    def open_path(self, path: Path) -> None:
        """Open a project, or import anything else, from a recent file or a dropped file."""
        if not path.exists():
            self.prefs.setValue("files/recent", [n for n in self._recent_files() if n != str(path)])
            QMessageBox.warning(self, "File not found", f"{path} no longer exists.")
        elif path.suffix.lower() != FILE_EXTENSION:
            self.import_drawing(path)
        elif self._confirm_discard():
            self.open_project(path)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt override
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt override
        files = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if files:
            event.acceptProposedAction()
            QTimer.singleShot(0, lambda: self.open_path(Path(files[0])))  # not inside the drop itself

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
        self._ghosts = {}
        self._user_hidden.clear()
        self._drawing_labels, self._labels_storey = [], None
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
            loaded = self._run_busy(f"Opening {path.name}…", lambda: load_project(source))
        except _Cancelled:
            self.statusBar().showMessage("Open cancelled")
            return
        except ProjectFileError as exc:
            QMessageBox.critical(self, "Open failed", str(exc))
            return
        self._set_project(loaded.project, path)
        self._remember_file(path)
        if recovered:
            self.bus.mark_unsaved()
            self._update_title()
        if loaded.warnings:
            show_notes(self, "Project opened with warnings", loaded.warnings)
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
        project = self.project
        try:
            self._run_busy(f"Saving {path.name}…", lambda: save_project(project, path), cancellable=False)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return False
        self._discard_autosave()
        if self.project.name == "Untitled":
            self.project.name = path.stem
        self.project_path = path
        self._remember_file(path)
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
            self.prefs.setValue("window/geometry", self.saveGeometry())
            self.prefs.setValue("window/layout", self.saveState(LAYOUT_VERSION))
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

    def _run_busy(self, message: str, work, cancellable: bool = True):
        """Run slow work on a background thread while the window stays responsive.

        Returns its result or raises its exception; raises _Cancelled if the user cancels.
        The work may read the project but must not change it or touch Qt. The window is modal
        from the start, so the user cannot edit what the work is reading. A cancelled thread
        finishes unseen, so jobs that write files should pass cancellable=False.
        """
        outcome: dict = {}

        def target() -> None:
            try:
                outcome["value"] = work()
            except Exception as exc:  # noqa: BLE001 - re-raised on the UI thread below
                outcome["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        dialog = QProgressDialog(message, "Cancel", 0, 0, self)
        dialog.setWindowTitle("BIMForge")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        if not cancellable:
            dialog.setCancelButton(None)
        # Shown at once so it blocks input, but invisible at first: quick jobs never flash a dialog.
        dialog.setWindowOpacity(0.0)
        dialog.show()
        QTimer.singleShot(400, dialog, lambda: dialog.setWindowOpacity(1.0))
        started = time.monotonic()

        def show_elapsed() -> None:
            minutes, seconds = divmod(int(time.monotonic() - started), 60)
            dialog.setLabelText(f"{message}\n{minutes}:{seconds:02d} so far. Very large drawings can take several minutes.")

        clock = QTimer()
        clock.setInterval(1000)
        clock.timeout.connect(show_elapsed)
        clock.start()
        self.autosave_timer.stop()
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(30)
        timer.timeout.connect(lambda: loop.quit() if not thread.is_alive() else None)
        if cancellable:
            dialog.canceled.connect(loop.quit)
        timer.start()
        while True:
            loop.exec()
            if not thread.is_alive() or cancellable:
                break  # (a non-cancellable job ignores the dialog being closed)
        timer.stop()
        clock.stop()
        self.autosave_timer.start(AUTOSAVE_INTERVAL_MS)
        cancelled = thread.is_alive()
        if cancellable:
            dialog.canceled.disconnect(loop.quit)
        dialog.reset()
        dialog.hide()
        dialog.setParent(None)  # deleteLater would wait for this nested event loop's owner to return
        dialog.deleteLater()
        if cancelled:
            raise _Cancelled
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    def import_drawing(self, path: Path) -> None:
        options: dict = {}
        if path.suffix.lower() == ".pdf":
            options = self._pdf_options(path)
            if options is None:
                return
        try:
            importer = self.registry.importer_for(path)
            imported = self._run_busy(f"Reading {path.name}…", lambda: importer.import_file(path, options))
        except _Cancelled:
            self.statusBar().showMessage("Import cancelled")
            return
        except Exception as exc:  # noqa: BLE001 - surface any importer failure to the user
            if ODA_DOWNLOAD in str(exc):
                self._offer_dwg_converter(str(exc))
            else:
                QMessageBox.critical(self, "Import failed", str(exc))
            return
        self._remember_file(path)
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
            show_notes(self, "Import notes", warnings)

    def _convert(self) -> None:
        assert self.drawing is not None
        storey = self.project.storey(self.active_storey_id) or self.project.default_storey
        drawing, settings = self.drawing, self.settings

        def work():
            result = PlanConverter(settings).run(drawing, storey)
            return result, _underlay_lines(drawing, result.unit.to_metres)

        try:
            self.result, underlay = self._run_busy("Detecting walls, doors, stairs and ducts…", work)
        except _Cancelled:
            self.statusBar().showMessage("Conversion cancelled")
            return
        self._drawing_labels, self._labels_storey = self.result.labels, storey.id
        self.view.clear(UNDERLAY)
        if len(underlay):
            self.view.upsert_lines(UNDERLAY, underlay, UNDERLAY_STYLE)
        self.review.populate(self.result.proposals)
        self.view.clear(GHOST)
        self._ghosts = {}
        self._show_ghosts()
        self._update_grid()
        self._set_view("iso")
        counts = ", ".join(f"{n} {k}" for k, n in self.result.summary().items()) or "nothing"
        self.statusBar().showMessage(f"Detected {counts} (units: {self.result.unit.symbol}). Review, then commit.")
        notes = list(self.drawing.warnings) + self.result.warnings
        if notes:
            show_notes(self, "Import notes", notes)

    def _show_ghosts(self) -> None:
        """Update the orange previews, rebuilding only those whose shape changed.

        A wall's shape depends on which of its openings are accepted, so that is its signature.
        """
        wanted: dict[str, tuple] = {}
        preview = Project("preview")
        if self.result is not None:
            preview.site = self.project.site
            for element in self.result.accepted_elements():
                preview.add(element)
            for element in preview.elements():
                holes = preview.hosted_openings(element.id) if isinstance(element, Wall) else []
                wanted[GHOST + element.id] = tuple(sorted(o.id for o in holes))
        for key in self._ghosts.keys() - wanted.keys():
            self.view.remove(key)
        for key, signature in wanted.items():
            if self._ghosts.get(key) != signature:
                mesh = self.tessellator.mesh_for(preview.get(key.removeprefix(GHOST)), preview)
                if mesh is not None and not mesh.is_empty:
                    self.view.upsert_mesh(key, mesh, GHOST_STYLE)
        self._ghosts = wanted
        self.view.render()

    def commit_proposals(self) -> None:
        if self.result is None:
            return
        result = self.result
        elements = result.accepted_elements()
        self.result = None
        self.review.populate([])
        self.view.clear(GHOST)
        self._ghosts = {}
        command = commit_conversion(result, self.project)
        storey = self.active_storey()
        if result.level_name and re.fullmatch(r"Level \d+", storey.name):  # a name the user chose is kept
            command = CompositeCommand([command, ModifyStorey(storey.id, {"name": result.level_name}, label="Name level")],
                                       command.label)
        if result.project_name and self.project.name == "Untitled":
            self.project.name = result.project_name
        self.bus.execute(command)
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
            exporter = self.registry.exporter_for(Path(path))
            project = self.project
            warnings = self._run_busy(f"Exporting {Path(path).name}…", lambda: exporter.export(project, Path(path)),
                                      cancellable=False)
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

    def flip_selected_stairs(self) -> None:
        stairs = [e for i in self.selection if isinstance(e := self.project.get(i), Stair)]
        if not stairs:
            self.statusBar().showMessage("Select a stair to flip first")
            return
        self._execute(CompositeCommand([ModifyElement(s.id, flipped_stair(s)) for s in stairs], "Flip stair direction"))

    def move_dialog(self) -> None:
        if not self.selection:
            self.statusBar().showMessage("Select elements to move first")
            return
        dialog = MoveDialog(len(self.selection), self)
        if dialog.exec():
            dx, dy = dialog.offset()
            if dx or dy:
                self._execute(MoveElements(self.selection, dx, dy))

    def _selection_centre(self) -> tuple[float, float] | None:
        bounds = self.view.bounds(self.selection)
        if bounds is None:
            self.statusBar().showMessage("Select elements first")
            return None
        return ((bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2)

    def _start_on_selection(self, tool_class) -> None:
        if not self.selection:
            self.statusBar().showMessage("Select elements first")
            return
        self.set_tool(tool_class(self))

    def rotate_selection(self) -> None:
        centre = self._selection_centre()
        if centre is None:
            return
        angle, ok = QInputDialog.getDouble(self, "Rotate", "Angle anticlockwise, about the middle of the selection (°):",
                                           90.0, -360.0, 360.0, 2)
        if ok and angle:
            self._execute(TransformElements(self.selection, rotation_about(centre, angle), "Rotate"))

    def mirror_selection(self, axis: str) -> None:
        centre = self._selection_centre()
        if centre is not None:
            self._execute(TransformElements(self.selection, mirror_across(centre, axis), "Mirror"))

    def array_selection(self) -> None:
        if not self.selection:
            self.statusBar().showMessage("Select elements first")
            return
        dialog = ArrayDialog(len(self.selection), self)
        if dialog.exec() and (offsets := dialog.offsets()):
            command = CopyElements(self.selection, offsets, "Array")
            if self._execute(command):
                self.statusBar().showMessage(f"Added {len(command.created)} element(s). Ctrl+Z to undo.")

    def detect_rooms(self) -> None:
        storey = self.active_storey()
        walls = [w for w in self.project.elements_of(Wall) if w.storey_id == storey.id]
        spaces = [s for s in self.project.elements_of(Space) if s.storey_id == storey.id]
        labels = self._drawing_labels if self._labels_storey == storey.id else ()
        rooms = rooms_from_walls(walls, spaces, self.settings.min_room_area, storey.height, storey.id,
                                 [s.name for s in self.project.elements_of(Space)], labels)
        if rooms and not self._execute(AddElements(rooms, label="Detect rooms")):
            return
        self.statusBar().showMessage(f"Found {len(rooms)} new room(s) on {storey.name}" if rooms else
                                     f"No new rooms on {storey.name}: rooms need walls that close a loop")

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
        self._update_grid()
        self.statusBar().showMessage(f"Active level: {self.project.storey(storey_id).name} (imports go here)")

    def _set_active_level_only(self, enabled: bool) -> None:
        self.active_level_only = enabled
        self._apply_level_filter()

    def _apply_level_filter(self) -> None:
        hidden = set(self._user_hidden)
        if self.active_level_only:
            hidden.update(e.id for e in self.project.elements() if e.storey_id != self.active_storey_id)
        self.view.set_hidden(hidden)
        self.view.render()

    def hide_selection(self) -> None:
        self._user_hidden.update(self.selection)
        self.select([])
        self._apply_level_filter()

    def isolate_selection(self) -> None:
        if self.selection:
            self._user_hidden = {e.id for e in self.project.elements()} - set(self.selection)
            self._apply_level_filter()

    def show_all(self) -> None:
        self._user_hidden.clear()
        self._apply_level_filter()

    def visible_element_boxes(self) -> dict[str, tuple]:
        return {key: box for key, box in self.view.visible_bounds().items() if key in self.project}

    def _update_grid(self) -> None:
        """A ground grid around the model at the active level, with spacing to suit its size."""
        if not self._grid_on:
            self.view.set_grid(None)
            self._grid_signature = None
            return
        # The grid belongs where the building is: previews count, stray far-off objects do not.
        frame = self.view.frame_bounds()
        if frame is not None:
            xmin, xmax, ymin, ymax = frame[:4]
        else:
            xmin, xmax, ymin, ymax = -10.0, 10.0, -10.0, 10.0
        span = max(xmax - xmin, ymax - ymin, 10.0)
        minor, major = (1, 5) if span < 80 else (5, 25) if span < 400 else (25, 125)
        pad = max(span * 0.15, minor * 2)
        x0, x1 = np.floor((xmin - pad) / minor) * minor, np.ceil((xmax + pad) / minor) * minor
        y0, y1 = np.floor((ymin - pad) / minor) * minor, np.ceil((ymax + pad) / minor) * minor
        z = self.active_elevation() - 0.01
        signature = (x0, x1, y0, y1, z, minor)
        if signature == self._grid_signature:
            return
        self._grid_signature = signature
        lines: dict[bool, list] = {True: [], False: []}
        for x in np.arange(x0, x1 + minor / 2, minor):
            lines[bool(round(x / minor) % (major / minor) == 0)].append(((x, y0, z), (x, y1, z)))
        for y in np.arange(y0, y1 + minor / 2, minor):
            lines[bool(round(y / minor) % (major / minor) == 0)].append(((x0, y, z), (x1, y, z)))
        self.view.set_grid(np.array(lines[False], dtype=np.float64).reshape(-1, 2, 3),
                           np.array(lines[True], dtype=np.float64).reshape(-1, 2, 3))

    def _set_grid_visible(self, enabled: bool) -> None:
        self._grid_on = enabled
        self.prefs.setValue("view/grid", "true" if enabled else "false")
        self._update_grid()
        self.view.render()

    def _set_display_style(self, style: str) -> None:
        self.prefs.setValue("view/style", style)
        self.view.set_display_style(style)
        self.view.render()

    def _set_orthographic(self, enabled: bool) -> None:
        self.view.set_orthographic(enabled)
        self.view.render()

    def zoom_selection(self) -> None:
        bounds = self.view.bounds(self.selection)
        if bounds is None:
            self.statusBar().showMessage("Select elements first")
            return
        self._user_moved_view()
        self.view.zoom_to(bounds)
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
        self._show_tool_options(tool)
        if tool is not None:
            self.viewport.setFocus()
            self.statusBar().showMessage(tool.hint)

    def _show_tool_options(self, tool: Tool | None) -> None:
        """Put the tool's settings in the bar under the ribbon; each remembers its last value."""
        for action, widget in self._option_widgets:
            self.context_bar.removeAction(action)
            widget.deleteLater()
        self._option_widgets = []

        def add(widget) -> None:
            self._option_widgets.append((self.context_bar.addWidget(widget), widget))

        if tool is None or not tool.title:
            return
        title = QLabel(tool.title)
        title.setStyleSheet("font-weight: 600;")
        add(title)
        for option in tool.options:
            remembered = (type(tool).__name__, option.attr)
            if remembered in self._tool_defaults:
                setattr(tool, option.attr, self._tool_defaults[remembered])
            add(QLabel(option.label))
            box = QDoubleSpinBox()
            box.setRange(option.low, option.high)
            box.setSingleStep(option.step)
            box.setDecimals(option.decimals)
            box.setSuffix(option.suffix)
            box.setValue(getattr(tool, option.attr))
            box.setKeyboardTracking(False)

            def changed(value: float, o=option, r=remembered, t=tool) -> None:
                setattr(t, o.attr, value)
                self._tool_defaults[r] = value

            box.valueChanged.connect(changed)
            box.editingFinished.connect(lambda: self.viewport.setFocus())
            add(box)

    def finish_tool(self) -> None:
        if self.tool is not None:
            self.tool.finish()

    def cancel_tool(self) -> None:
        if self.tool is not None:
            self.tool.cancel()
            self.tool = None
            self._show_tool_options(None)
            self.statusBar().showMessage("Tool stopped")

    def _escape(self) -> None:
        if self.tool is not None:
            self.cancel_tool()
        else:
            self.select([])

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
        project, tessellator = self.project, self.tessellator

        def work():
            detector = ClashDetector(project, tessellator)
            return [r for test in tests for r in detector.run(test)]

        try:
            results = self._run_busy("Checking for clashes…", work)
        except _Cancelled:
            self.statusBar().showMessage("Clash detection cancelled")
            return
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

    def show_schedules(self) -> None:
        if self._schedules_dialog is None:
            self._schedules_dialog = SchedulesDialog(lambda: self.project, self.select, self)
        self._schedules_dialog.rebuild()
        self._schedules_dialog.show()
        self._schedules_dialog.raise_()

    def export_plan_sheets(self) -> None:
        suggested = (self.project_path or Path(self.project.name)).with_suffix(".pdf").name
        path, _ = QFileDialog.getSaveFileName(self, "Export plan sheets", suggested, "PDF (*.pdf)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            count = export_sheets(self.project, Path(path))
        except OSError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QApplication.restoreOverrideCursor()
        if count:
            self.statusBar().showMessage(f"Exported {count} sheet(s) to {path}")
        else:
            QMessageBox.information(self, "Plan sheets", "The model is empty, so there is nothing to draw.")

    def save_view_image(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save view as image", "view.png", "PNG image (*.png)")
        if path:
            self.view.save_image(Path(path))
            self.statusBar().showMessage(f"Saved {path}")

    def show_shortcuts(self) -> None:
        rows = sorted({(a.toolTip().rsplit("  (", 1)[0], a.shortcut().toString(QKeySequence.SequenceFormat.NativeText))
                       for a in self.findChildren(QAction) if not a.shortcut().isEmpty() and a.parent() is self})
        rows += [("Orbit", "Left drag"), ("Pan", "Middle drag, Shift + left drag, or arrow keys"),
                 ("Zoom towards the cursor", "Wheel"), ("Zoom", "Right drag"),
                 ("Add to selection", "Ctrl + click"), ("End a tool", "Enter, or right-click"), ("Stop a tool", "Esc")]
        dialog = QDialog(self)
        dialog.setWindowTitle("Keyboard shortcuts")
        dialog.resize(420, 560)
        table = QTableWidget(len(rows), 2, dialog)
        table.setHorizontalHeaderLabels(["Command", "Keys"])
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(text))
        QVBoxLayout(dialog).addWidget(table)
        dialog.exec()

    def show_about(self) -> None:
        QMessageBox.about(self, "About BIMForge",
                          "<b>BIMForge</b><br>Turns 2D drawings (DXF, DWG, PDF) into editable 3D building models, "
                          "with MEP tools, coordination checks, schedules and IFC export.")

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
        project = self.project
        try:
            rows = self._run_busy("Counting quantities…", lambda: _write_quantities(project, exporter, path),
                                  cancellable=False)
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
        count = len(self.selection)
        self.selection_label.setText(f"{count} selected" if count else "Nothing selected")
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
        self._update_grid()
        removed = set(change.removed)
        if removed & set(self.selection):
            self.select([i for i in self.selection if i not in removed])
        elif set(self.selection) & affected:
            self.select(self.selection)  # refresh properties and re-apply highlight to new actors
        self._update_title()
        self.view.render()

    def _render_room(self, room: Space) -> None:
        """A room shows as a tinted floor with its name; it has no solid of its own in the model."""
        floor = Slab(boundary=room.boundary, thickness=0.01, top_offset=0.02, storey_id=room.storey_id)
        try:
            mesh = self.tessellator.mesh_for(floor, self.project)
        except ValueError:
            mesh = None
        if mesh is None or mesh.is_empty:
            self.view.remove(room.id)
            return
        polygon = Polygon(room.boundary)
        x, y = (polygon.representative_point() if polygon.is_valid else polygon.centroid).coords[0]
        name = "\n".join(textwrap.wrap(room.name or "Room", ROOM_LABEL_WIDTH))
        self.view.set_label(room.id, f"{name}\n{room.area:.1f} m²",
                            (x, y, self.project.storey_elevation(room) + room.height + 0.1))  # above the walls, which would hide it
        self.view.upsert_mesh(room.id, mesh, STYLES[Space])

    def _render_element(self, element: Element) -> None:
        if isinstance(element, Space):
            self._render_room(element)
            return
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
                               category=category)
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
        x, y = interactor.GetEventPosition()
        if self.tool is not None:
            self.tool.hover(self._tool_point(x, y), bool(interactor.GetShiftKey()))
        at = self.view.pick_on_plane(x, y, self.active_elevation())
        if at is not None:
            self.coordinates.setText(f"X {at[0]:.3f}   Y {at[1]:.3f} m")

    def _on_wheel(self, factor: float) -> None:
        x, y = self.viewport.GetRenderWindow().GetInteractor().GetEventPosition()
        self._user_moved_view()
        self.view.zoom_at(x, y, self.active_elevation(), factor)
        self.view.render()

    def _on_left_down(self, *_) -> None:
        interactor = self.viewport.GetRenderWindow().GetInteractor()
        if interactor.GetControlKey():
            return  # Ctrl+click adds to the selection; dragging must not roll the view on its side
        if self._pan_mode:
            self._style.FindPokedRenderer(*interactor.GetEventPosition())
            self._style.StartPan()
            self._panning = True
        else:
            self._style.OnLeftButtonDown()

    def _on_left_up(self, *_) -> None:
        if self._panning:
            self._style.EndPan()
            self._panning = False
        else:
            self._style.OnLeftButtonUp()

    def _set_pan_mode(self, enabled: bool) -> None:
        self._pan_mode = enabled
        self.viewport.setCursor(Qt.CursorShape.OpenHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.statusBar().showMessage("Pan: drag to move the drawing. Press P to go back to orbiting." if enabled else "Orbit: drag to turn the view.")

    def _user_moved_view(self) -> None:
        self._fitted = False

    def _nudge(self, across: float, along: float) -> None:
        self._user_moved_view()
        self.view.pan_by(across, along)
        self.view.render()

    def _zoom_button(self, factor: float) -> None:
        self._user_moved_view()
        self.view.zoom_centre(factor)
        self.view.render()

    def _set_full_view(self, enabled: bool) -> None:
        """All of the window for the drawing; the panels come back as they were."""
        if enabled:
            self._docks_before_full = self.saveState(LAYOUT_VERSION)
            for dock in self.docks.values():
                dock.hide()
        elif self._docks_before_full is not None:
            self.restoreState(self._docks_before_full, LAYOUT_VERSION)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if watched is self.viewport and event.type() == QEvent.Type.Resize:
            self._resize_timer.start()
        return super().eventFilter(watched, event)

    def _after_resize(self) -> None:
        if self._fitted:
            self.view.fit_view()
            self.view.render()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if not self._docks_sized:
            self._docks_sized = True
            if self.prefs.value("window/layout") is None:  # the panels start narrow, so the drawing has the room
                self.resizeDocks([self.docks["Project browser"]], [PANEL_WIDTHS[0]], Qt.Orientation.Horizontal)
                self.resizeDocks([self.docks["Properties"]], [PANEL_WIDTHS[1]], Qt.Orientation.Horizontal)
                self._default_layout = self.saveState(LAYOUT_VERSION)

    def _on_right_press(self, interactor, _event) -> None:
        self._right_press_pos = interactor.GetEventPosition()
        if self.tool is not None:
            self.finish_tool()

    def _on_right_release(self, interactor, _event) -> None:
        x, y = interactor.GetEventPosition()
        if self.tool is None and abs(x - self._right_press_pos[0]) + abs(y - self._right_press_pos[1]) <= 3:
            QTimer.singleShot(0, lambda: self._context_menu(x, y))  # after VTK has finished with this click

    def _context_menu(self, x: int, y: int) -> None:
        key = self.view.pick(x, y)
        if key in self.project and key not in self.selection:
            self.select([key])
        menu = QMenu(self)
        for action in self._context_commands:
            menu.addSeparator() if action is None else menu.addAction(action)
        menu.exec(QCursor.pos())

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
        self._fitted = True
        self.view.render()

    def _set_view(self, preset: str) -> None:
        self.view.set_view(preset)
        self._fitted = True
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
