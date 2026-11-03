"""Schedules window: one table per element kind. Clicking a row selects that element in the model."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
)

from bimforge.coordination.schedules import Schedule, export_schedule_csv
from bimforge.core.model import Project


class SchedulesDialog(QDialog):
    def __init__(self, project_source: Callable[[], Project], on_select: Callable[[list[str]], None], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Schedules")
        self.resize(760, 480)
        self._project_source = project_source
        self._on_select = on_select
        self._schedules: list[Schedule] = []
        self.tabs = QTabWidget()
        self.total = QLabel()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.rebuild)
        export = QPushButton("Export CSV…")
        export.clicked.connect(self.export_current)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.total)
        buttons.addStretch(1)
        for button in (refresh, export, close):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addLayout(buttons)
        self.tabs.currentChanged.connect(self._show_total)
        self.rebuild()

    def rebuild(self) -> None:
        from bimforge.coordination.schedules import build_schedules

        current = self.tabs.currentIndex()
        self.tabs.clear()
        self._schedules = build_schedules(self._project_source())
        for schedule in self._schedules:
            self.tabs.addTab(self._table(schedule), f"{schedule.name} ({len(schedule.rows)})")
        self.tabs.setCurrentIndex(max(current, 0))
        self._show_total()

    def _table(self, schedule: Schedule) -> QTableWidget:
        table = QTableWidget(len(schedule.rows), len(schedule.headers))
        table.setHorizontalHeaderLabels(schedule.headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for r, row in enumerate(schedule.rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem(value)
                if value.replace(".", "", 1).isdigit():
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(r, c, item)
        table.itemSelectionChanged.connect(lambda: self._on_select(
            [schedule.element_ids[i] for i in sorted({index.row() for index in table.selectedIndexes()})]))
        return table

    def _current(self) -> Schedule | None:
        index = self.tabs.currentIndex()
        return self._schedules[index] if 0 <= index < len(self._schedules) else None

    def _show_total(self) -> None:
        schedule = self._current()
        self.total.setText(schedule.totals if schedule else "")

    def export_current(self) -> None:
        schedule = self._current()
        if schedule is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export schedule", f"{schedule.name}.csv", "CSV (*.csv)")
        if path:
            export_schedule_csv(schedule, Path(path))
