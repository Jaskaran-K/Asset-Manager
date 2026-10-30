"""Asset grid: a virtualized list model, the card delegate that draws favorite / rating badges,
and the grid view itself (cards glide to their new places when a resize changes the rows).

AssetListModel only does work for rows the view asks about, which are the visible
ones: thumbnails are requested (and decoded in the background) as cards scroll into
view, and tooltips are built when hovered. A 50,000 file library costs about the same
to show as a 50 file one.
"""

from __future__ import annotations

import math
from typing import Callable, Optional, Sequence

from PySide6.QtCore import (
    QAbstractListModel, QEasingCurve, QMimeData, QModelIndex, QPersistentModelIndex, QPointF, QRectF, QSize, Qt, QUrl,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from core.scanner import AssetItem
from core.tag_repository import Marks
from ui import animations

ASSET_ROLE = Qt.ItemDataRole.UserRole
GLIDE_MS = 220  # cards gliding to their new places when the rows change


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
        glide = getattr(option.widget, "glide_offset", None)
        offset = glide(index) if glide is not None else None
        if offset is None:
            self._paint_card(painter, option, index)
            return
        painter.save()
        painter.translate(offset)  # (on its way from where it was: GridView)
        self._paint_card(painter, option, index)
        painter.restore()

    def _paint_card(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
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


class GridView(QListView):
    """The asset grid (an icon-mode QListView with a fixed grid size). When a resize changes how
    many cards fit in a row, the cards glide from where they were to their new places (the card
    delegate draws them offset by glide_offset) rather than jumping. Follows the motion setting."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._columns = 0  # cards in a row, as last laid out
        self._cell = QSize()
        self._start: Optional[Callable[[int], QPointF]] = None  # where each row's card glides from
        self._glide = QVariantAnimation(self)
        self._glide.setDuration(GLIDE_MS)
        self._glide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._glide.setStartValue(0.0)
        self._glide.setEndValue(1.0)
        self._glide.valueChanged.connect(self._on_glide_step)
        self._glide.finished.connect(self._on_glide_done)

    def _count_columns(self) -> int:
        model = self.model()
        rows = model.rowCount() if model is not None else 0
        if not rows:
            return 0
        top = self.rectForIndex(model.index(0, 0)).top()
        columns = 1
        while columns < rows and self.rectForIndex(model.index(columns, 0)).top() == top:
            columns += 1
        return columns

    def _cell_at(self, row: int, columns: int) -> QPointF:
        """Where a row's card sits in a grid columns wide (from the first card)."""
        size = self.gridSize()
        return QPointF((row % columns) * size.width(), (row // columns) * size.height())

    def paintEvent(self, event) -> None:
        # Checked here, not in updateGeometries: finding where cards sit can lay the grid out, and
        # doing that from inside Qt's own layout (which calls updateGeometries) can crash Qt
        self._check_columns()
        super().paintEvent(event)

    def _check_columns(self) -> None:
        """Start a glide if the cards in a row changed since the last paint."""
        columns = self._count_columns()
        old, size = self._columns, self.gridSize()
        self._columns = columns
        resized = size == self._cell
        self._cell = size
        if not (old and columns and columns != old and resized and self.isVisible()):
            return
        if not animations.motion_enabled():
            return
        start = self._start
        if start is not None and self._glide.state() == QVariantAnimation.State.Running:
            done = self._glide.easingCurve().valueForProgress(self._glide.currentTime() / GLIDE_MS)

            def from_now(row: int, start=start, old=old, done=done) -> QPointF:  # (from where it is mid-glide)
                begin = start(row)
                return begin + (self._cell_at(row, old) - begin) * done

            self._start = from_now
        else:
            self._start = lambda row, old=old: self._cell_at(row, old)
        self._glide.stop()
        self._glide.start()

    def glide_offset(self, index: QModelIndex) -> Optional[QPointF]:
        """How far a card is drawn from its place while the cards glide (None: at its place)."""
        if self._start is None or not self._columns:
            return None
        row = index.row()
        left = 1.0 - self._glide.easingCurve().valueForProgress(min(1.0, self._glide.currentTime() / GLIDE_MS))
        offset = (self._start(row) - self._cell_at(row, self._columns)) * left
        return offset if offset.manhattanLength() >= 0.5 else None

    def _on_glide_step(self, _value) -> None:
        self.viewport().update()

    def _on_glide_done(self) -> None:
        self._start = None
        self.viewport().update()
