"""Asset grid: a virtualized list model, and the card delegate that draws favorite / rating badges.

AssetListModel only does work for rows the view asks about, which are the visible
ones: thumbnails are requested (and decoded in the background) as cards scroll into
view, and tooltips are built when hovered. A 50,000 file library costs about the same
to show as a 50 file one.
"""

from __future__ import annotations

import math
from typing import Callable, Optional, Sequence

from PySide6.QtCore import QAbstractListModel, QMimeData, QModelIndex, QPersistentModelIndex, QPointF, QRectF, QSize, Qt, QUrl
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from core.scanner import AssetItem
from core.tag_repository import Marks

ASSET_ROLE = Qt.ItemDataRole.UserRole


class AssetListModel(QAbstractListModel):
    def __init__(
        self,
        thumbnail_for: Callable[[AssetItem], object],
        tooltip_for: Callable[[AssetItem], str],
        parent=None,
    ):
        super().__init__(parent)
        self._thumbnail_for = thumbnail_for
        self._tooltip_for = tooltip_for
        self._assets: list[AssetItem] = []
        self._rows: dict[str, int] = {}

    # Contents --------------------------------------------------------------
    def set_assets(self, assets: Sequence[AssetItem]) -> None:
        self.beginResetModel()
        self._assets = list(assets)
        self._rows = {str(a.path): row for row, a in enumerate(self._assets)}
        self.endResetModel()

    def asset_at(self, row: int) -> Optional[AssetItem]:
        return self._assets[row] if 0 <= row < len(self._assets) else None

    def row_of(self, path_str: str) -> Optional[int]:
        return self._rows.get(path_str)

    def assets(self) -> list[AssetItem]:
        return list(self._assets)

    def thumbnail_changed(self, path_str: str) -> None:
        row = self._rows.get(path_str)
        if row is not None:
            index = self.index(row)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])

    def refresh_all(self) -> None:
        """Repaint every card (e.g. after tags, favorites or ratings changed)."""
        if self._assets:
            self.dataChanged.emit(self.index(0), self.index(len(self._assets) - 1))

    # QAbstractListModel ------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._assets)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        asset = self.asset_at(index.row()) if index.isValid() else None
        if asset is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return asset.name
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbnail_for(asset)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip_for(asset)
        if role == ASSET_ROLE:
            return asset
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled

    # Dragging files out (to Explorer, Blender, Photoshop, ...) -----------------
    def mimeTypes(self) -> list[str]:
        return ["text/uri-list"]

    def mimeData(self, indexes: Sequence[QModelIndex]) -> QMimeData:
        rows = sorted({i.row() for i in indexes if i.isValid()})
        mime = QMimeData()
        # File URLs: Windows hands these to other apps as an ordinary file drop (CF_HDROP)
        mime.setUrls([QUrl.fromLocalFile(str(self._assets[r].path)) for r in rows])
        return mime

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction  # never move: the library keeps its files

STAR_COLOR = QColor("#fbbf24")


def star_polygon(center: QPointF, outer: float, inner_ratio: float = 0.45) -> QPolygonF:
    points = []
    for i in range(10):
        radius = outer if i % 2 == 0 else outer * inner_ratio
        angle = math.radians(-90 + i * 36)
        points.append(QPointF(center.x() + radius * math.cos(angle), center.y() + radius * math.sin(angle)))
    return QPolygonF(points)


def rating_text(rating: int) -> str:
    return "★" * rating + "☆" * (5 - rating)  # glyph-ok: rating text


class AssetCardDelegate(QStyledItemDelegate):
    """Default item painting plus a gold star (favorite) and rating pill on the thumbnail."""

    def __init__(self, marks_for: Callable[[QModelIndex], Marks], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._marks_for = marks_for

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        marks = self._marks_for(index)
        if not (marks.favorite or marks.rating):
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = option.widget
        style = widget.style() if widget is not None else None
        icon = (
            style.subElementRect(QStyle.SubElement.SE_ItemViewItemDecoration, opt, widget)
            if style is not None else option.rect
        )
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if marks.favorite:
            size = max(6.0, min(9.0, icon.width() * 0.07))
            center = QPointF(icon.left() + size + 7, icon.top() + size + 7)
            painter.setPen(QPen(QColor(0, 0, 0, 170), 1.2))
            painter.setBrush(STAR_COLOR)
            painter.drawPolygon(star_polygon(center, size))
        if marks.rating:
            text = "★" * marks.rating  # glyph-ok: rating text
            font = QFont("Segoe UI", 7, QFont.Weight.Bold)
            painter.setFont(font)
            width = painter.fontMetrics().horizontalAdvance(text) + 8
            pill = QRectF(icon.right() - width - 5, icon.top() + 6, width, 14)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 160))
            painter.drawRoundedRect(pill, 7, 7)
            painter.setPen(STAR_COLOR)
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()
