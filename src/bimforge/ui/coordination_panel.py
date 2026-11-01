"""Coordination dock: clash tests, model checks and the issue list."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bimforge.core.model import IssueStatus, Project
from bimforge.coordination.clash import DEFAULT_TESTS, ClashTest

ID_ROLE = Qt.ItemDataRole.UserRole
STATUS_COLORS = {
    IssueStatus.NEW: QColor(218, 54, 51, 60),
    IssueStatus.ACTIVE: QColor(210, 153, 34, 70),
    IssueStatus.RESOLVED: QColor(46, 160, 67, 60),
    IssueStatus.APPROVED: QColor(80, 120, 200, 50),
}


class CoordinationPanel(QWidget):
    run_clashes_requested = Signal(list)  # list[ClashTest]
    run_checks_requested = Signal()
    status_changed = Signal(str, object)  # issue id, IssueStatus
    comment_requested = Signal(str)
    issue_activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        tests = QGroupBox("Clash tests")
        tl = QVBoxLayout(tests)
        self._checks: list[tuple[QCheckBox, ClashTest]] = []
        for test in DEFAULT_TESTS:
            label = test.name + (f" (clearance {test.clearance * 1000:.0f} mm)" if test.clearance else "")
            box = QCheckBox(label)
            box.setChecked(test.default)
            tl.addWidget(box)
            self._checks.append((box, test))
        buttons = QHBoxLayout()
        run_clash, run_checks = QPushButton("Run clash detection"), QPushButton("Run model checks")
        run_clash.clicked.connect(lambda: self.run_clashes_requested.emit([t for b, t in self._checks if b.isChecked()]))
        run_checks.clicked.connect(self.run_checks_requested)
        buttons.addWidget(run_clash)
        buttons.addWidget(run_checks)
        tl.addLayout(buttons)
        layout.addWidget(tests)

        filters = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItem("Open issues", "open")
        self.filter.addItem("All issues", "all")
        self.filter.currentIndexChanged.connect(lambda: self._project and self.rebuild(self._project))
        comment = QPushButton("Comment…")
        comment.clicked.connect(lambda: self._current() and self.comment_requested.emit(self._current()))
        filters.addWidget(self.filter)
        filters.addStretch()
        filters.addWidget(comment)
        layout.addLayout(filters)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Status", "Issue", "Test", "Comment"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda row, _: self.issue_activated.emit(self._ids[row]))
        layout.addWidget(self.table)
        self._ids: list[str] = []
        self._project: Project | None = None

    def _current(self) -> str | None:
        row = self.table.currentRow()
        return self._ids[row] if 0 <= row < len(self._ids) else None

    def rebuild(self, project: Project) -> None:
        self._project = project
        open_only = self.filter.currentData() == "open"
        issues = [i for i in project.issues.values()
                  if not open_only or i.status in (IssueStatus.NEW, IssueStatus.ACTIVE)]
        order = {IssueStatus.NEW: 0, IssueStatus.ACTIVE: 1, IssueStatus.APPROVED: 2, IssueStatus.RESOLVED: 3}
        issues.sort(key=lambda i: (order[i.status], i.test, i.title))
        self._ids = [i.id for i in issues]
        self.table.setRowCount(len(issues))
        for row, issue in enumerate(issues):
            status = QComboBox()
            for s in IssueStatus:
                status.addItem(s.value, s)
            status.setCurrentIndex(list(IssueStatus).index(issue.status))
            status.currentIndexChanged.connect(
                lambda _i, iid=issue.id, box=status: self.status_changed.emit(iid, box.currentData())
            )
            self.table.setCellWidget(row, 0, status)
            title = QTableWidgetItem(issue.title)
            title.setBackground(STATUS_COLORS[issue.status])
            title.setToolTip("Double-click to zoom to this issue")
            self.table.setItem(row, 1, title)
            self.table.setItem(row, 2, QTableWidgetItem(issue.test))
            self.table.setItem(row, 3, QTableWidgetItem(issue.comment))
