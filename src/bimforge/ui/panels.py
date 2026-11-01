"""Dockable panels and dialogs: AI review, project browser, properties, settings, move."""

from __future__ import annotations

import dataclasses
import enum
from collections import defaultdict

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bimforge.conversion import ConversionSettings, Proposal
from bimforge.core.model import Element, Project, Space, Wall
from bimforge.core.units import LengthUnit

ID_ROLE = Qt.ItemDataRole.UserRole


def confidence_color(confidence: float) -> QColor:
    if confidence >= 0.8:
        return QColor(46, 160, 67, 60)
    if confidence >= 0.5:
        return QColor(210, 153, 34, 70)
    return QColor(218, 54, 51, 70)


class ReviewPanel(QWidget):
    """Lists AI/rule proposals with confidence; the user accepts or rejects each before commit."""

    acceptance_changed = Signal()
    selection_changed = Signal(list)  # element ids
    commit_requested = Signal()

    COLUMNS = ("", "Type", "Name", "Confidence", "Note")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._proposals: list[Proposal] = []
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection)

        accept_all, reject_all = QPushButton("Accept all"), QPushButton("Reject all")
        self.commit_button = QPushButton("Commit to model")
        self.commit_button.setDefault(True)
        accept_all.clicked.connect(lambda: self._set_all(True))
        reject_all.clicked.connect(lambda: self._set_all(False))
        self.commit_button.clicked.connect(self.commit_requested)

        buttons = QHBoxLayout()
        for b in (accept_all, reject_all):
            buttons.addWidget(b)
        buttons.addStretch()
        buttons.addWidget(self.commit_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self.populate([])

    def populate(self, proposals: list[Proposal]) -> None:
        self._proposals = proposals
        self.table.blockSignals(True)
        self.table.setRowCount(len(proposals))
        for row, p in enumerate(proposals):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check.setCheckState(Qt.CheckState.Checked if p.accepted else Qt.CheckState.Unchecked)
            cells = [check, QTableWidgetItem(p.kind), QTableWidgetItem(p.element.name),
                     QTableWidgetItem(f"{p.confidence:.0%}"), QTableWidgetItem(p.note)]
            cells[3].setBackground(confidence_color(p.confidence))
            for col, item in enumerate(cells):
                self.table.setItem(row, col, item)
        self.table.blockSignals(False)
        self.commit_button.setEnabled(bool(proposals))

    def select_element(self, element_id: str) -> None:
        for row, p in enumerate(self._proposals):
            if p.element.id == element_id:
                self.table.selectRow(row)
                return

    def _set_all(self, accepted: bool) -> None:
        for p in self._proposals:
            p.accepted = accepted
        self.populate(self._proposals)
        self.acceptance_changed.emit()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._proposals[item.row()].accepted = item.checkState() == Qt.CheckState.Checked
            self.acceptance_changed.emit()

    def _on_selection(self) -> None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        self.selection_changed.emit([self._proposals[r].element.id for r in sorted(rows)])


class ProjectBrowser(QTreeWidget):
    """Storey > element type > element tree. Rebuilt on change; fine up to a few thousand elements."""

    selection_changed = Signal(list)  # element ids

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.itemSelectionChanged.connect(self._on_selection)

    def rebuild(self, project: Project) -> None:
        selected = set(self.selected_ids())
        collapsed = {self.topLevelItem(i).text(0) for i in range(self.topLevelItemCount())
                     if not self.topLevelItem(i).isExpanded()}
        self.blockSignals(True)
        self.clear()
        grouped: dict[str | None, dict[str, list[Element]]] = defaultdict(lambda: defaultdict(list))
        for element in project.elements():
            grouped[element.storey_id][type(element).__name__].append(element)
        storeys = [(s.id, s.name) for s in project.storeys] + [(None, "Unassigned")]
        for storey_id, storey_name in storeys:
            if storey_id not in grouped:
                continue
            count = sum(len(v) for v in grouped[storey_id].values())
            storey_item = QTreeWidgetItem([f"{storey_name} ({count})"])
            self.addTopLevelItem(storey_item)
            for kind in sorted(grouped[storey_id]):
                elements = grouped[storey_id][kind]
                kind_item = QTreeWidgetItem([f"{kind}s ({len(elements)})"])
                storey_item.addChild(kind_item)
                for element in sorted(elements, key=lambda e: e.name):
                    item = QTreeWidgetItem([element.name or element.id[:8]])
                    item.setData(0, ID_ROLE, element.id)
                    kind_item.addChild(item)
                    item.setSelected(element.id in selected)
            storey_item.setExpanded(storey_item.text(0) not in collapsed)
        self.blockSignals(False)

    def selected_ids(self) -> list[str]:
        return [i.data(0, ID_ROLE) for i in self.selectedItems() if i.data(0, ID_ROLE)]

    def select_ids(self, ids: list[str]) -> None:
        wanted = set(ids)
        self.blockSignals(True)
        self.clearSelection()
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop()
            stack.extend(item.child(i) for i in range(item.childCount()))
            if item.data(0, ID_ROLE) in wanted:
                item.setSelected(True)
                self.scrollToItem(item)
        self.blockSignals(False)

    def _on_selection(self) -> None:
        self.selection_changed.emit(self.selected_ids())


class PropertiesPanel(QTreeWidget):
    """Shows an element's fields; double-click a value to edit it."""

    edit_requested = Signal(str, str, object)  # element id, field name, parsed value

    READ_ONLY = {"id", "storey_id", "type_id", "host_id", "system_id", "properties", "boundary", "path"}
    GREY = QBrush(QColor(128, 128, 128))

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderLabels(["Property", "Value"])
        self.setRootIsDecorated(False)
        self.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.itemDoubleClicked.connect(self._begin_edit)
        self.itemChanged.connect(self._on_changed)
        self._element: Element | None = None

    @staticmethod
    def _format(value: object) -> str:
        if isinstance(value, float):
            return f"{value:.3f}"
        if isinstance(value, tuple) and all(isinstance(x, (int, float)) for x in value):
            return ", ".join(f"{x:.3f}" for x in value)
        if isinstance(value, list):
            return f"{len(value)} points"
        return "" if value is None else str(value)

    def _row(self, name: str, text: str, editable: bool, field: str | None = None) -> None:
        item = QTreeWidgetItem([name, text])
        item.setData(1, ID_ROLE, field)
        if editable:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        else:
            item.setForeground(1, self.GREY)
        self.addTopLevelItem(item)

    def show_element(self, element: Element | None, editable: bool = True) -> None:
        self.blockSignals(True)
        self.clear()
        self._element = element
        if element is not None:
            self._row("Class", f"{type(element).__name__} ({element.ifc_class})", False)
            if isinstance(element, Wall):
                self._row("Length", f"{element.length:.3f}", False)
            if isinstance(element, Space):
                self._row("Area", f"{element.area:.2f} m²", False)
            for f in dataclasses.fields(element):
                value = getattr(element, f.name)
                can_edit = editable and f.name not in self.READ_ONLY and isinstance(value, (str, float, int, tuple))
                self._row(f.name, self._format(value), can_edit, f.name)
            self.resizeColumnToContents(0)
        self.blockSignals(False)

    def show_message(self, text: str) -> None:
        self.show_element(None)
        self._row(text, "", False)

    def refresh(self) -> None:
        self.show_element(self._element)

    def _begin_edit(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.flags() & Qt.ItemFlag.ItemIsEditable:
            self.editItem(item, 1)

    def _on_changed(self, item: QTreeWidgetItem, column: int) -> None:
        field = item.data(1, ID_ROLE)
        if column != 1 or self._element is None or not field:
            return
        current = getattr(self._element, field)
        text = item.text(1).strip()
        try:
            if isinstance(current, bool):
                value: object = text.lower() in ("1", "true", "yes")
            elif isinstance(current, (int, float)):
                value = type(current)(float(text))
            elif isinstance(current, tuple):
                value = tuple(float(p) for p in text.replace(";", ",").split(","))
                if len(value) != len(current):
                    raise ValueError(f"expected {len(current)} values")
            else:
                value = text
        except ValueError:
            self.refresh()
            return
        self.edit_requested.emit(self._element.id, field, value)


class SettingsDialog(QDialog):
    """Edits ConversionSettings element defaults and detection parameters."""

    FIELDS = (
        ("wall_height", "Wall height (m)", 0.5, 20.0),
        ("slab_thickness", "Slab thickness (m)", 0.05, 2.0),
        ("door_height", "Door height (m)", 1.5, 5.0),
        ("window_sill_height", "Window sill height (m)", 0.0, 3.0),
        ("window_height", "Window height (m)", 0.2, 5.0),
        ("min_wall_thickness", "Min wall thickness (m)", 0.01, 1.0),
        ("max_wall_thickness", "Max wall thickness (m)", 0.05, 2.0),
        ("accept_threshold", "Auto-accept confidence", 0.0, 1.0),
    )

    def __init__(self, settings: ConversionSettings, parent: QWidget | None = None,
                 layer_counts: dict[str, int] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Conversion settings")
        self._settings = settings
        form = QFormLayout(self)
        self._spins: dict[str, QDoubleSpinBox] = {}
        for name, label, lo, hi in self.FIELDS:
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setDecimals(3)
            spin.setSingleStep(0.05)
            spin.setValue(getattr(settings, name))
            form.addRow(label, spin)
            self._spins[name] = spin
        self._unit = QComboBox()
        self._unit.addItem("From drawing", None)
        for unit in LengthUnit:
            self._unit.addItem(unit.symbol, unit)
        if settings.unit_override:
            self._unit.setCurrentIndex(self._unit.findData(settings.unit_override))
        form.addRow("Drawing units", self._unit)
        self._layers = QListWidget()
        self._layers.setToolTip("Tick the layers that hold walls. Leave all unticked to detect walls from layer names.")
        for name, count in sorted((layer_counts or {}).items(), key=lambda kv: (-kv[1], kv[0])):
            item = QListWidgetItem(f"{name}  ({count} lines)")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in settings.wall_layers else Qt.CheckState.Unchecked)
            self._layers.addItem(item)
        if layer_counts:
            self._layers.setMinimumHeight(160)
            form.addRow("Wall layers", self._layers)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def result_settings(self) -> ConversionSettings:
        values = {name: spin.value() for name, spin in self._spins.items()}
        unit = self._unit.currentData()
        items = [self._layers.item(i) for i in range(self._layers.count())]
        walls = tuple(i.data(Qt.ItemDataRole.UserRole) for i in items if i.checkState() == Qt.CheckState.Checked)
        if not items:  # no drawing loaded: keep the previous choice
            walls = self._settings.wall_layers
        return dataclasses.replace(
            self._settings, **values, unit_override=unit if isinstance(unit, enum.Enum) else None, wall_layers=walls
        )


class MoveDialog(QDialog):
    """Move the selection by an exact plan offset."""

    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Move {count} element{'s' if count != 1 else ''}")
        form = QFormLayout(self)
        self.dx, self.dy = QDoubleSpinBox(), QDoubleSpinBox()
        for label, spin in (("ΔX (m)", self.dx), ("ΔY (m)", self.dy)):
            spin.setRange(-10000, 10000)
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            form.addRow(label, spin)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def offset(self) -> tuple[float, float]:
        return self.dx.value(), self.dy.value()


class LevelsPanel(QWidget):
    """Levels table (highest first, as in a section). Double-click a cell to edit; select a row to activate."""

    activated = Signal(str)  # storey id
    edit_requested = Signal(str, str, object)  # storey id, field, value
    add_requested = Signal()
    duplicate_requested = Signal(str)
    delete_requested = Signal(str)

    COLUMNS = (("name", "Level"), ("elevation", "Elevation (m)"), ("height", "Height (m)"))

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([label for _, label in self.COLUMNS])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed)
        self.table.itemChanged.connect(self._on_changed)
        self.table.itemSelectionChanged.connect(self._on_selection)
        add, duplicate, delete = QPushButton("Add"), QPushButton("Duplicate"), QPushButton("Delete")
        add.setToolTip("Add a level above the top level")
        duplicate.setToolTip("Add a level above the top level with a copy of this level's elements")
        add.clicked.connect(self.add_requested)
        duplicate.clicked.connect(lambda: self._emit_for_current(self.duplicate_requested))
        delete.clicked.connect(lambda: self._emit_for_current(self.delete_requested))
        buttons = QHBoxLayout()
        for b in (add, duplicate, delete):
            buttons.addWidget(b)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self._ids: list[str] = []

    def rebuild(self, project: Project, active_id: str) -> None:
        storeys = sorted(project.storeys, key=lambda s: -s.elevation)
        self._ids = [s.id for s in storeys]
        self._texts: dict[tuple[int, int], str] = {}
        self.table.blockSignals(True)
        self.table.setRowCount(len(storeys))
        for row, storey in enumerate(storeys):
            for col, (name, _) in enumerate(self.COLUMNS):
                value = getattr(storey, name)
                text = f"{value:.3f}" if isinstance(value, float) else value
                self._texts[(row, col)] = text
                self.table.setItem(row, col, QTableWidgetItem(text))
            if storey.id == active_id:
                self.table.selectRow(row)
        self.table.resizeColumnsToContents()
        self.table.blockSignals(False)

    def _current_id(self) -> str | None:
        row = self.table.currentRow()
        return self._ids[row] if 0 <= row < len(self._ids) else None

    def _emit_for_current(self, signal) -> None:
        storey_id = self._current_id()
        if storey_id:
            signal.emit(storey_id)

    def _on_selection(self) -> None:
        storey_id = self._current_id()
        if storey_id:
            self.activated.emit(storey_id)

    def _on_changed(self, item: QTableWidgetItem) -> None:
        name = self.COLUMNS[item.column()][0]
        text = item.text().strip()
        try:
            value: object = text if name == "name" else float(text)
        except ValueError:
            value = None
        if value in (None, ""):
            self.table.blockSignals(True)
            item.setText(self._texts[(item.row(), item.column())])  # restore the previous value
            self.table.blockSignals(False)
            return
        self.edit_requested.emit(self._ids[item.row()], name, value)
