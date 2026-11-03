"""Import notes in their own window: it scrolls, always fits the screen, and doesn't block the model."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QPushButton, QVBoxLayout


class NotesDialog(QDialog):
    def __init__(self, title: str, notes: Sequence[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)  # the keyboard stays with the 3D view
        self.text = QPlainTextEdit("\n\n".join(notes))
        self.text.setReadOnly(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.text.toPlainText()))
        buttons.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        layout.addWidget(buttons)
        screen = (parent.screen() if parent is not None else QGuiApplication.primaryScreen()).availableGeometry()
        self.resize(min(620, int(screen.width() * 0.8)), min(340, int(screen.height() * 0.6)))
        if parent is not None:  # in a corner, off the middle of the drawing
            area = parent.frameGeometry()
            self.move(area.right() - self.width() - 340, area.bottom() - self.height() - 60)


def show_notes(parent, title: str, notes: Sequence[str]) -> NotesDialog:
    dialog = NotesDialog(title, notes, parent)
    dialog.show()
    dialog.raise_()
    return dialog
