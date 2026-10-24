"""Small icon helpers for sidebar categories."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap

# Fallback badge colors when a category has no saved color, cycled by position
MAIN_CATEGORY_COLORS = ["#3b82f6", "#10b981", "#ef4444", "#f59e0b", "#a855f7", "#06b6d4", "#ec4899"]
SUB_CATEGORY_COLORS = ["#60a5fa", "#f59e0b", "#10b981", "#a855f7", "#34d399", "#fbbf24", "#ef4444", "#c084fc"]


def resolve_category_colors(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of categories with a color filled in wherever none was saved."""
    result = copy.deepcopy(categories)
    sub_index = 0
    for main_index, main in enumerate(result):
        main.setdefault("color", MAIN_CATEGORY_COLORS[main_index % len(MAIN_CATEGORY_COLORS)])
        for sub in main.get("subcategories", []):
            sub.setdefault("color", SUB_CATEGORY_COLORS[sub_index % len(SUB_CATEGORY_COLORS)])
            sub_index += 1
    return result


def color_dot_icon(color: QColor | str, size: int = 16) -> QIcon:
    """Create a crisp circular color badge."""
    color = QColor(color)
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(color))
    p.setPen(QPen(color.lighter(130), 1))
    p.drawEllipse(2, 2, size - 4, size - 4)
    p.end()
    return QIcon(pix)


def load_icon_image(path: str | Path, size: int = 16) -> Optional[QIcon]:
    """Load a user-supplied icon image scaled to size, or None if it can't be read."""
    pix = QPixmap(str(path))
    if pix.isNull():
        return None
    scaled = pix.scaled(
        size * 2, size * 2,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return QIcon(scaled)


def category_icon(color: str, icon_path: Optional[str] = None, size: int = 16) -> QIcon:
    """Icon for a category: its custom image if set and readable, else its color dot."""
    if icon_path:
        icon = load_icon_image(icon_path, size)
        if icon is not None:
            return icon
    return color_dot_icon(color, size)


def color_swatch_icon(color: str, width: int = 28, height: int = 16) -> QIcon:
    """Rounded rectangle swatch used on color-picker buttons."""
    pix = QPixmap(width, height)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(color)
    p.setBrush(QBrush(c))
    p.setPen(QPen(c.darker(140), 1))
    p.drawRoundedRect(1, 1, width - 2, height - 2, 4, 4)
    p.end()
    return QIcon(pix)
