"""MEP dock: systems and circuits, fixture placement, run drawing; plus the panel schedule dialog."""

from __future__ import annotations

from dataclasses import astuple, fields

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bimforge.core.model import Project, SystemType
from bimforge.disciplines.catalog import FAMILIES
from bimforge.disciplines.electrical import CIRCUIT_TYPES, CircuitRow, panels

ID_ROLE = Qt.ItemDataRole.UserRole
RUN_SIZES = {"pipe": (15, 22, 28, 32, 40, 50, 65, 80, 100, 150), "conduit": (20, 25, 32), "tray": (100, 150, 300, 450, 600)}


class MepPanel(QWidget):
    new_system_requested = Signal()
    delete_system_requested = Signal(str)
    assign_requested = Signal(str)
    size_requested = Signal(str)
    schedule_requested = Signal(str)
    place_requested = Signal(str, float)  # family key, rotation
    draw_requested = Signal(str, float, float)  # kind, size mm, offset m
    system_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        systems = QGroupBox("Systems and circuits")
        sl = QVBoxLayout(systems)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "Members"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection)
        sl.addWidget(self.table)
        row1, row2 = QHBoxLayout(), QHBoxLayout()
        for text, signal, row in (("New…", None, row1), ("Delete", self.delete_system_requested, row1),
                                  ("Assign selection", self.assign_requested, row1),
                                  ("Size pipes", self.size_requested, row2),
                                  ("Panel schedule…", self.schedule_requested, row2)):
            button = QPushButton(text)
            if signal is None:
                button.clicked.connect(self.new_system_requested)
            else:
                button.clicked.connect(lambda _=False, s=signal: self.current_system() and s.emit(self.current_system()))
            row.addWidget(button)
        sl.addLayout(row1)
        sl.addLayout(row2)
        layout.addWidget(systems)

        place = QGroupBox("Place fixture")
        pf = QFormLayout(place)
        self.family = QComboBox()
        for family in FAMILIES.values():
            self.family.addItem(f"{family.name} ({family.discipline.value})", family.key)
        self.rotation = QDoubleSpinBox()
        self.rotation.setRange(-360, 360)
        self.rotation.setSingleStep(90)
        self.rotation.setSuffix("°")
        place_button = QPushButton("Place (click in view)")
        place_button.clicked.connect(lambda: self.place_requested.emit(self.family.currentData(), self.rotation.value()))
        pf.addRow("Family", self.family)
        pf.addRow("Rotation", self.rotation)
        pf.addRow(place_button)
        layout.addWidget(place)

        draw = QGroupBox("Draw run")
        df = QFormLayout(draw)
        self.kind = QComboBox()
        for label, key in (("Pipe", "pipe"), ("Conduit", "conduit"), ("Cable tray", "tray")):
            self.kind.addItem(label, key)
        self.size = QComboBox()
        self.kind.currentIndexChanged.connect(self._fill_sizes)
        self._fill_sizes()
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-5, 20)
        self.offset.setDecimals(3)
        self.offset.setSingleStep(0.1)
        self.offset.setValue(-0.3)
        self.offset.setSuffix(" m")
        self.offset.setToolTip("Height of the run above the active level (negative = below the floor)")
        draw_button = QPushButton("Draw (click in view)")
        draw_button.clicked.connect(
            lambda: self.draw_requested.emit(self.kind.currentData(), float(self.size.currentData()), self.offset.value())
        )
        df.addRow("Type", self.kind)
        df.addRow("Size", self.size)
        df.addRow("Offset", self.offset)
        df.addRow(draw_button)
        layout.addWidget(draw)
        layout.addStretch()

    def _fill_sizes(self) -> None:
        kind = self.kind.currentData()
        self.size.clear()
        for size in RUN_SIZES[kind]:
            self.size.addItem(f"{size} mm", size)
        self.size.setCurrentIndex(min(1, self.size.count() - 1))
        if hasattr(self, "offset"):  # not yet created during __init__
            # Containment usually runs at ceiling level; pipes below the floor.
            self.offset.setValue(2.7 if kind in ("conduit", "tray") else -0.3)

    def _on_selection(self) -> None:
        system_id = self.current_system()
        if system_id:
            self.system_selected.emit(system_id)

    def current_system(self) -> str | None:
        items = self.table.selectedItems()
        return items[0].data(ID_ROLE) if items else None

    def rebuild(self, project: Project) -> None:
        current = self.current_system()
        counts: dict[str, int] = {}
        for element in project.elements():
            sid = getattr(element, "system_id", None)
            if sid:
                counts[sid] = counts.get(sid, 0) + 1
        systems = sorted(project.systems.values(), key=lambda s: (s.system_type.value, s.name))
        self.table.blockSignals(True)
        self.table.setRowCount(len(systems))
        for row, system in enumerate(systems):
            name = QTableWidgetItem(system.name)
            name.setData(ID_ROLE, system.id)
            name.setForeground(QColor.fromRgbF(*[c * 0.8 for c in system.color]))
            cells = [name, QTableWidgetItem(system.system_type.value.replace("_", " ")),
                     QTableWidgetItem(str(counts.get(system.id, 0)))]
            for col, item in enumerate(cells):
                self.table.setItem(row, col, item)
            if system.id == current:
                self.table.selectRow(row)
        self.table.blockSignals(False)


class NewSystemDialog(QDialog):
    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New system / circuit")
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.type = QComboBox()
        for t in SystemType:
            self.type.addItem(t.value.replace("_", " "), t)
        self.panel = QComboBox()
        self.panel.addItem("(none)", None)
        for board in panels(project):
            self.panel.addItem(board.name or board.id[:8], board.id)
        self.type.currentIndexChanged.connect(self._update)
        form.addRow("Name", self.name)
        form.addRow("Type", self.type)
        form.addRow("Fed from panel", self.panel)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._update()

    def _update(self) -> None:
        self.panel.setEnabled(self.type.currentData() in CIRCUIT_TYPES)


class PanelScheduleDialog(QDialog):
    export_csv_requested = Signal()
    export_sld_requested = Signal()

    HEADERS = ["Circuit", "Type", "Points", "Load (VA)", "Ib (A)", "Breaker (A)", "Cable (mm²)", "Length (m)",
               "Vd", "OK", "Notes"]

    def __init__(self, title: str, rows: list[CircuitRow], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Panel schedule - {title}")
        self.resize(900, 360)
        layout = QVBoxLayout(self)
        table = QTableWidget(len(rows) + 1, len(self.HEADERS))
        table.setHorizontalHeaderLabels(self.HEADERS)
        table.verticalHeader().hide()
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for r, row in enumerate(rows):
            values = [row.name, row.kind, row.points, f"{row.load_va:,.0f}", f"{row.design_current:.1f}",
                      f"{row.breaker_a:g}", f"{row.cable_mm2:g}", f"{row.length_m:g}", f"{row.voltage_drop:.1%}",
                      "yes" if row.ok else "NO", row.notes]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if not row.ok:
                    item.setBackground(QColor(218, 54, 51, 50))
                table.setItem(r, c, item)
        total = sum(r.load_va for r in rows)
        table.setItem(len(rows), 0, QTableWidgetItem("TOTAL"))
        table.setItem(len(rows), 3, QTableWidgetItem(f"{total:,.0f}"))
        table.setItem(len(rows), 4, QTableWidgetItem(f"{total / 230:.1f}"))
        table.resizeColumnsToContents()
        layout.addWidget(table)
        buttons = QHBoxLayout()
        csv_button, sld_button = QPushButton("Export CSV…"), QPushButton("Export single-line diagram (SVG)…")
        csv_button.clicked.connect(self.export_csv_requested)
        sld_button.clicked.connect(self.export_sld_requested)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        for b in (csv_button, sld_button):
            buttons.addWidget(b)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)


SCHEDULE_HEADERS = [f.name for f in fields(CircuitRow)]


def schedule_rows_for_csv(rows: list[CircuitRow]) -> list[tuple]:
    return [astuple(r) for r in rows]
