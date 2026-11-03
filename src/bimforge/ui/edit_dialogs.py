"""Small dialogs for the editing tools."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QSpinBox


class ArrayDialog(QDialog):
    """Rows and columns of copies, spaced in metres."""

    def __init__(self, count: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Array {count} element(s)")
        self.columns, self.rows = QSpinBox(), QSpinBox()
        for box, value in ((self.columns, 3), (self.rows, 1)):
            box.setRange(1, 200)
            box.setValue(value)
        self.dx, self.dy = QDoubleSpinBox(), QDoubleSpinBox()
        for box, value in ((self.dx, 3.0), (self.dy, 3.0)):
            box.setRange(-1000, 1000)
            box.setDecimals(3)
            box.setSingleStep(0.1)
            box.setSuffix(" m")
            box.setValue(value)
        form = QFormLayout(self)
        form.addRow("Columns (along X)", self.columns)
        form.addRow("Spacing along X", self.dx)
        form.addRow("Rows (along Y)", self.rows)
        form.addRow("Spacing along Y", self.dy)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def offsets(self) -> list[tuple[float, float]]:
        """Where each copy goes, leaving out the original at (0, 0)."""
        return [(c * self.dx.value(), r * self.dy.value())
                for r in range(self.rows.value()) for c in range(self.columns.value()) if r or c]
