"""Light and dark appearance for the whole application."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class Theme:
    window: str
    panel: str
    field: str
    text: str
    muted: str
    border: str
    hover: str
    accent: str
    accent_text: str
    view_top: tuple[float, float, float]
    view_bottom: tuple[float, float, float]


THEMES = {
    "light": Theme("#eef0f3", "#fafbfc", "#ffffff", "#1f2933", "#66707c", "#d3d8df", "#e2e8f0", "#2563eb", "#ffffff",
                   (0.72, 0.76, 0.82), (0.93, 0.94, 0.96)),
    "dark": Theme("#1f2226", "#2a2e34", "#23262b", "#e6e8eb", "#9aa3ae", "#3c424a", "#3a4048", "#4c8dff", "#ffffff",
                  (0.11, 0.13, 0.16), (0.27, 0.30, 0.35)),
}


def _stylesheet(t: Theme) -> str:
    return f"""
    QToolBar#Ribbon {{ background: {t.panel}; border: none; border-bottom: 1px solid {t.border}; padding: 0; spacing: 0; }}
    QToolBar#ContextBar {{ background: {t.window}; border: none; border-bottom: 1px solid {t.border};
                           padding: 2px 6px; spacing: 6px; }}
    QTabWidget#RibbonTabs::pane {{ border: none; border-top: 1px solid {t.border}; background: {t.panel}; }}
    QTabWidget#RibbonTabs > QTabBar::tab {{ background: transparent; color: {t.muted}; padding: 5px 16px; border: none;
                                           border-bottom: 2px solid transparent; }}
    QTabWidget#RibbonTabs > QTabBar::tab:hover {{ color: {t.text}; }}
    QTabWidget#RibbonTabs > QTabBar::tab:selected {{ color: {t.accent}; border-bottom: 2px solid {t.accent};
                                                    font-weight: 600; }}
    QFrame#RibbonGroup {{ border: none; border-right: 1px solid {t.border}; background: transparent; }}
    QLabel#RibbonGroupTitle {{ color: {t.muted}; font-size: 8pt; }}
    QWidget#RibbonPage, QScrollArea#RibbonScroll {{ background: {t.panel}; border: none; }}
    QScrollArea#RibbonScroll QScrollBar:horizontal {{ height: 6px; background: transparent; border: none; }}
    QScrollArea#RibbonScroll QScrollBar::handle:horizontal {{ background: {t.border}; border-radius: 3px; min-width: 30px; }}
    QScrollArea#RibbonScroll QScrollBar::add-line:horizontal, QScrollArea#RibbonScroll QScrollBar::sub-line:horizontal {{ width: 0; }}
    QFrame#RibbonGroup QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px;
                                      padding: 2px; color: {t.text}; }}
    QFrame#RibbonGroup QToolButton:hover {{ background: {t.hover}; border-color: {t.border}; }}
    QFrame#RibbonGroup QToolButton:pressed {{ background: {t.border}; }}
    QFrame#RibbonGroup QToolButton:checked {{ background: {t.hover}; border-color: {t.accent}; }}
    QToolBar#ContextBar QToolButton {{ border: 1px solid transparent; border-radius: 3px; padding: 2px 6px; }}
    QToolBar#ContextBar QToolButton:hover {{ background: {t.hover}; }}
    QToolBar#ContextBar QToolButton:checked {{ background: {t.hover}; border-color: {t.accent}; }}
    QDockWidget {{ color: {t.text}; }}
    QDockWidget::title {{ background: {t.window}; padding: 5px 8px; border-bottom: 1px solid {t.border}; }}
    QMainWindow::separator {{ background: {t.border}; width: 1px; height: 1px; }}
    QStatusBar {{ background: {t.window}; color: {t.text}; border-top: 1px solid {t.border}; }}
    QStatusBar QLabel {{ color: {t.muted}; padding: 0 8px; border-left: 1px solid {t.border}; }}
    QTreeWidget, QTableWidget, QListWidget {{ background: {t.field}; alternate-background-color: {t.panel};
                                              border: 1px solid {t.border}; }}
    QHeaderView::section {{ background: {t.window}; color: {t.text}; padding: 4px 6px; border: none;
                            border-right: 1px solid {t.border}; border-bottom: 1px solid {t.border}; }}
    """


def apply_theme(app: QApplication, name: str) -> Theme:
    theme = THEMES.get(name, THEMES["light"])
    app.setStyle("Fusion")
    C = QPalette.ColorRole
    palette = QPalette()
    for role, value in ((C.Window, theme.window), (C.Base, theme.field), (C.AlternateBase, theme.panel),
                        (C.Button, theme.panel), (C.ToolTipBase, theme.panel), (C.WindowText, theme.text),
                        (C.Text, theme.text), (C.ButtonText, theme.text), (C.ToolTipText, theme.text),
                        (C.PlaceholderText, theme.muted), (C.Highlight, theme.accent),
                        (C.HighlightedText, theme.accent_text), (C.Link, theme.accent),
                        (C.Mid, theme.border), (C.Midlight, theme.hover), (C.Dark, theme.border)):
        palette.setColor(role, QColor(value))
    for role in (C.WindowText, C.Text, C.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(theme.muted))
    app.setPalette(palette)
    app.setStyleSheet(_stylesheet(theme))
    return theme
