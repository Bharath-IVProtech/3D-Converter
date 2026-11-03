"""The ribbon's icons, drawn as small SVGs so they stay sharp and ship no image files."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

SLATE = "#7b8794"
BLUE = "#3b82f6"
ORANGE = "#f08c2e"
GREEN = "#2fa36b"
RED = "#e0524d"

_L = f'stroke="{SLATE}"'  # outlines
_A = f'stroke="{BLUE}"'  # the part that matters
_F = f'fill="{BLUE}" fill-opacity=".22"'
_G = f'fill="{SLATE}" fill-opacity=".25"'

_ICONS: dict[str, str] = {
    "new": f'<path {_L} d="M6 3h8l4 4v14H6z M14 3v4h4"/><path {_A} d="M12 11v6M9 14h6"/>',
    "open": f'<path {_L} d="M3 19V5h6l2 2h8v3"/><path {_A} {_F} d="M3 19l3-9h16l-3 9z"/>',
    "save": f'<path {_A} {_F} d="M4 4h13l3 3v13H4z"/><path {_L} d="M8 4v5h7V4 M7 20v-7h10v7"/>',
    "import": f'<path {_L} d="M4 15v5h16v-5"/><path {_A} d="M12 3v12M7 10l5 5 5-5"/>',
    "export": f'<path {_L} d="M4 15v5h16v-5"/><path {_A} d="M12 15V3M7 8l5-5 5 5"/>',
    "undo": f'<path {_A} d="M8 5L3 10l5 5"/><path {_L} d="M3 10h11a6 6 0 0 1 0 12h-3"/>',
    "redo": f'<path {_A} d="M16 5l5 5-5 5"/><path {_L} d="M21 10H10a6 6 0 0 0 0 12h3"/>',
    "settings": f'<path {_L} d="M4 7h9M18 7h2M4 17h2M11 17h9"/><circle {_A} cx="15.5" cy="7" r="2.5"/>'
                f'<circle {_A} cx="8.5" cy="17" r="2.5"/>',
    "level": f'<path {_L} d="M3 19h18M3 12h18M3 5h18"/><path {_A} {_F} d="M16 10l3 2-3 2z"/>',
    "level_only": f'<path {_L} d="M3 20h18M3 4h18" stroke-dasharray="2 2"/><path {_A} {_F} d="M3 9h18v6H3z"/>',
    "wall": f'<path {_A} {_F} d="M3 7h18v10H3z"/><path {_L} d="M3 12h18M9 7v5M15 7v5M6 12v5M12 12v5M18 12v5"/>',
    "door": f'<path {_L} d="M3 21h18M6 21V3h12v18"/><path {_A} {_F} d="M6 21V3l8 3v15z"/>',
    "window": f'<path {_A} {_F} d="M4 4h16v16H4z"/><path {_L} d="M12 4v16M4 12h16"/>',
    "column": f'<path {_L} d="M6 3h12M6 21h12"/><path {_A} {_F} d="M9 3h6v18H9z"/>',
    "slab": f'<path {_A} {_F} d="M2 13l6-6h14l-6 6z"/><path {_L} d="M2 13v4h14v-4M16 17l6-6V7"/>',
    "stair": f'<path {_A} {_F} d="M3 21v-4h4v-4h4V9h4V5h6v16z"/>',
    "room": f'<path {_L} d="M3 3h18v18H3z"/><path {_A} {_F} d="M6 6h12v12H6z"/>'
            f'<path {_A} d="M9 12h6M12 9v6"/>',
    "select": f'<path {_L} stroke-dasharray="3 2" d="M3 6h15v12H3z"/><path {_A} {_F} d="M12 11l9 4-4 1-1 4z"/>',
    "select_all": f'<path {_A} {_F} stroke-dasharray="3 2" d="M3 3h18v18H3z"/><path {_L} d="M8 12l3 3 5-6"/>',
    "clear": f'<path {_L} stroke-dasharray="3 2" d="M3 3h18v18H3z"/><path {_A} d="M8 8l8 8M16 8l-8 8"/>',
    "move": f'<path {_A} d="M12 2v20M2 12h20M12 2L9 5M12 2l3 3M12 22l-3-3M12 22l3-3M2 12l3-3M2 12l3 3M22 12l-3-3M22 12l-3 3"/>',
    "copy": f'<path {_L} d="M4 4h11v11H4z"/><path {_A} {_F} d="M9 9h11v11H9z"/>',
    "rotate": f'<path {_L} d="M6 15V9h6v6z"/><path {_A} d="M20 12a8 8 0 1 1-3-6.2M17 2v4h4"/>',
    "mirror_x": f'<path {_L} stroke-dasharray="3 2" d="M12 2v20"/><path {_A} {_F} d="M9 6v12L3 18z"/><path {_L} d="M15 6v12l6 0z"/>',
    "mirror_y": f'<path {_L} stroke-dasharray="3 2" d="M2 12h20"/><path {_A} {_F} d="M6 9h12L18 3z"/><path {_L} d="M6 15h12l0 6z"/>',
    "array": f'<path {_A} {_F} d="M3 3h5v5H3z"/><path {_L} d="M10 3h5v5h-5zM17 3h4v5h-4zM3 10h5v5H3zM10 10h5v5h-5zM17 10h4v5h-4zM3 17h5v4H3zM10 17h5v4h-5zM17 17h4v4h-4z"/>',
    "delete": f'<path {_L} d="M4 6h16M9 6V3h6v3"/><path stroke="{RED}" fill="{RED}" fill-opacity=".18" d="M6 6l1 15h10l1-15z"/>',
    "copy_level": f'<path {_L} d="M3 21h18M3 11h18"/><path {_A} {_F} d="M7 14h6v4H7z"/><path {_A} d="M10 14V5M7 8l3-3 3 3"/>',
    "flip": f'<path {_A} d="M4 8h14l-4-4M20 16H6l4 4"/>',
    "fit": f'<path {_A} d="M3 8V3h5M16 3h5v5M21 16v5h-5M8 21H3v-5"/><path {_L} {_G} d="M8 8h8v8H8z"/>',
    "zoom": f'<circle {_A} cx="10" cy="10" r="7"/><path {_L} d="M15 15l6 6M7 10h6M10 7v6"/>',
    "zoom_in": f'<circle {_A} cx="10" cy="10" r="7"/><path {_L} d="M15 15l6 6"/><path {_A} d="M7 10h6M10 7v6"/>',
    "zoom_out": f'<circle {_A} cx="10" cy="10" r="7"/><path {_L} d="M15 15l6 6"/><path {_A} d="M7 10h6"/>',
    "pan": f'<path {_A} {_F} d="M8 12V6a1.5 1.5 0 0 1 3 0v5V4.5a1.5 1.5 0 0 1 3 0V11V6a1.5 1.5 0 0 1 3 0v8c0 4-2.5 7-6 7-3 0-4.5-1.5-6-4l-2-4c-.8-1.6 1.5-2.7 2.6-1.2z"/>',
    "fullview": f'<path {_L} d="M3 4h18v16H3z"/><path {_A} d="M7 10V8h3M17 10V8h-3M7 14v2h3M17 14v2h-3"/>',
    "iso": f'<path {_A} {_F} d="M12 2l9 5v10l-9 5-9-5V7z"/><path {_L} d="M3 7l9 5 9-5M12 12v10"/>',
    "top": f'<path {_L} d="M12 2l9 5v10l-9 5-9-5V7z"/><path {_A} {_F} d="M12 2l9 5-9 5-9-5z"/>',
    "bottom": f'<path {_L} d="M12 2l9 5v10l-9 5-9-5V7z"/><path {_A} {_F} d="M12 12l9 5-9 5-9-5z"/>',
    "front": f'<path {_L} d="M12 2l9 5v10l-9 5-9-5V7z"/><path {_A} {_F} d="M3 7l9 5v10l-9-5z"/>',
    "back": f'<path {_L} d="M12 2l9 5v10l-9 5-9-5V7z"/><path {_A} {_F} d="M12 2v10l-9 5V7z"/>',
    "left": f'<path {_L} d="M12 2l9 5v10l-9 5-9-5V7z"/><path {_A} {_F} d="M3 7l9-5v10l-9 5z"/>',
    "right": f'<path {_L} d="M12 2l9 5v10l-9 5-9-5V7z"/><path {_A} {_F} d="M21 7v10l-9 5V12z"/>',
    "ortho": f'<path {_A} d="M4 8h16M4 16h16"/><path {_L} d="M4 4v16M20 4v16"/>',
    "shaded": f'<path {_G} {_L} d="M12 2l9 5v10l-9 5-9-5V7z"/><path fill="{BLUE}" fill-opacity=".35" d="M12 12l9-5v10l-9 5z"/>',
    "edges": f'<path {_A} {_F} d="M12 2l9 5v10l-9 5-9-5V7z"/><path {_A} d="M3 7l9 5 9-5M12 12v10"/>',
    "hidden": f'<path {_L} d="M12 2l9 5v10l-9 5-9-5V7zM3 7l9 5 9-5M12 12v10"/>',
    "wireframe": f'<path {_A} d="M12 2l9 5v10l-9 5-9-5V7zM3 7l9 5 9-5M12 12v10"/>'
                 f'<path {_L} stroke-dasharray="2 2" d="M12 2v10M3 17l9-5 9 5"/>',
    "xray": f'<path fill="{BLUE}" fill-opacity=".14" {_L} d="M12 2l9 5v10l-9 5-9-5V7z"/><path {_A} d="M8 9h8v6H8z"/>',
    "grid": f'<path {_L} d="M3 3h18v18H3zM9 3v18M15 3v18M3 9h18M3 15h18"/>',
    "hide": f'<path {_L} d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><path {_A} d="M4 4l16 16"/>',
    "isolate": f'<path {_L} stroke-dasharray="3 2" d="M3 3h18v18H3z"/><path {_A} {_F} d="M8 8h8v8H8z"/>',
    "show": f'<path {_L} d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle {_A} {_F} cx="12" cy="12" r="3"/>',
    "section": f'<path {_L} d="M3 8l5-5h13v13l-5 5H3zM3 8h13v13M16 8l5-5"/><path {_A} {_F} d="M7 12h5v5H7z"/>',
    "section_level": f'<path {_L} d="M4 4h16v16H4z"/><path {_A} {_F} d="M4 10h16v5H4z"/>',
    "section_clear": f'<path {_L} d="M3 8l5-5h13v13l-5 5H3zM3 8h13v13"/><path {_A} d="M7 12l5 5M12 12l-5 5"/>',
    "panel": f'<path {_L} d="M3 4h18v16H3zM15 4v16"/><path {_A} {_F} d="M15 4h6v16h-6z"/>',
    "system": f'<path {_A} d="M6 18V6h12M12 6v12"/><circle {_L} cx="6" cy="18" r="2.5"/><circle {_L} cx="18" cy="6" r="2.5"/>'
              f'<circle {_L} cx="12" cy="18" r="2.5"/>',
    "clash": f'<path {_L} {_G} d="M3 4h11v11H3z"/><path {_L} d="M10 10h11v11H10z"/><path stroke="{RED}" fill="{RED}" fill-opacity=".45" d="M10 10h4v5h-4z"/>',
    "check": f'<path {_L} d="M5 3h14v18H5z"/><path stroke="{GREEN}" d="M8 12l3 3 5-6"/>',
    "measure": f'<path {_A} {_F} d="M2 16L16 2l6 6L8 22z"/><path {_L} d="M6 12l2 2M9 9l3 3M12 6l2 2M15 3l3 3"/>',
    "issues": f'<path stroke="{ORANGE}" fill="{ORANGE}" fill-opacity=".2" d="M12 3l10 18H2z"/><path stroke="{ORANGE}" d="M12 10v5M12 18v.5"/>',
    "schedule": f'<path {_L} d="M3 4h18v16H3zM3 10h18M3 15h18M9 10v10M15 10v10"/><path {_A} {_F} d="M3 4h18v6H3z"/>',
    "sheet": f'<path {_L} d="M2 5h20v14H2zM16 5v14M16 14h6"/><path {_A} d="M5 8h8v8H5zM5 12h4v4"/>',
    "image": f'<path {_L} d="M3 4h18v16H3z"/><path {_A} {_F} d="M3 18l6-6 4 4 3-3 5 5"/><circle {_L} cx="16" cy="8" r="1.6"/>',
    "boq": f'<path {_L} d="M5 3h14v18H5zM8 8h8M8 12h8M8 16h4"/><path {_A} d="M14 16h3"/>',
    "theme": f'<circle {_L} cx="12" cy="12" r="8"/><path fill="{BLUE}" fill-opacity=".6" d="M12 4a8 8 0 0 1 0 16z"/>',
    "help": f'<circle {_L} cx="12" cy="12" r="9"/><path {_A} d="M9 9.5a3 3 0 1 1 4.5 2.6c-1 .6-1.5 1.1-1.5 2.4M12 17.5v.5"/>',
}


@lru_cache(maxsize=None)
def icon(name: str) -> QIcon:
    """The named icon; an unknown name gives an empty icon rather than a failure."""
    body = _ICONS.get(name)
    if body is None:
        return QIcon()
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke-width="1.6" '
           f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    result = QIcon()
    for size in (16, 24, 32, 48):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        result.addPixmap(pixmap)
    return result
