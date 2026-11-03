"""The tabbed command ribbon. Buttons show the same QActions as the menus, so they stay in step."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

ICON_SIZE = 26
BUTTON_WIDTH = 56
BUTTON_HEIGHT = 66


class RibbonPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("RibbonPage")
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(4, 2, 4, 0)
        self._row.setSpacing(0)
        self._row.addStretch(1)

    def group(self, title: str, items: Sequence[QAction | QWidget]) -> None:
        frame = QFrame()
        frame.setObjectName("RibbonGroup")
        column = QVBoxLayout(frame)
        column.setContentsMargins(4, 0, 4, 0)
        column.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(1)
        for item in items:
            row.addWidget(_button(item) if isinstance(item, QAction) else item)
        column.addLayout(row)
        caption = QLabel(title)
        caption.setObjectName("RibbonGroupTitle")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(caption)
        self._row.insertWidget(self._row.count() - 1, frame)


def _button(action: QAction) -> QToolButton:
    button = QToolButton()
    button.setDefaultAction(action)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
    button.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
    button.setFixedSize(BUTTON_WIDTH, BUTTON_HEIGHT)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keyboard focus stays in the 3D view for Esc and Enter
    return button


class Ribbon(QTabWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("RibbonTabs")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setDocumentMode(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def page(self, title: str) -> RibbonPage:
        page = RibbonPage()
        scroll = QScrollArea()  # a narrow window scrolls the ribbon rather than squeezing it
        scroll.setObjectName("RibbonScroll")
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setMinimumHeight(BUTTON_HEIGHT + 32)  # includes the thin scroll bar a narrow window needs
        self.addTab(scroll, title)
        return page
