"""The pieces of the Course View window (ui.course_window): the lesson list, the lists of files and notes, the bar
that cuts a long video into lessons, and the course cards on the Courses page. Each is painted in the theme's colours
(looked up as it paints, so it follows the theme)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImageReader, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QListWidget, QListWidgetItem, QMenu, QStyle, QStyledItemDelegate, QWidget,
)

from core import apps, courses
from core.artbook_store import IMAGE_EXTENSIONS
from core.scanner import VIDEO_EXTENSIONS
from ui import shortcuts
from ui.duotone import duotone_icon
from ui.themes import theme_color
from ui.video_player import format_time

KIND = Qt.ItemDataRole.UserRole  # a row: "chapter", "lesson", "heading" (a list's section), "file", "note"
ID = Qt.ItemDataRole.UserRole + 1  # its id (a file's: its path)
DATA = Qt.ItemDataRole.UserRole + 2  # the lesson / note dict, or a file's details
FILES_MIME = "application/x-course-files"  # files dragged out of a course's lists (to put them on a lesson)
THUMB = QSize(48, 36)
MAX_THUMBS = 40  # pictures shown small in one list (the rest get an icon)
TYPE_NAMES = {
    ".ztl": "ZBrush tool", ".zpr": "ZBrush project", ".zbp": "ZBrush brush", ".zmt": "ZBrush material",
    ".ma": "Maya scene", ".mb": "Maya scene", ".blend": "Blender file", ".fbx": "FBX model", ".obj": "OBJ model",
    ".spp": "Painter project", ".sbsar": "Substance material", ".pdf": "PDF", ".abr": "Photoshop brushes",
    ".psd": "Photoshop file", ".txt": "Text", ".mp4": "Video", ".mov": "Video", ".mkv": "Video",
}
ICONS = {".pdf": "page", ".txt": "page", ".rtf": "page", ".md": "page", ".html": "web", ".htm": "web",
         ".zbp": "tools", ".abr": "tools", ".sbsar": "material", ".spp": "material"}


def _font(base: QFont, scale: float = 1.0, weight=QFont.Weight.Normal) -> QFont:
    """base, scale times as big. (The app's stylesheet sizes text in pixels, so a font's point size can be -1: the size
    it has, points or pixels, is the one scaled.)"""
    font = QFont(base)
    if base.pointSizeF() > 0:
        font.setPointSizeF(base.pointSizeF() * scale)
    elif base.pixelSize() > 0:
        font.setPixelSize(max(1, round(base.pixelSize() * scale)))
    font.setWeight(weight)
    return font


def _alpha(color: QColor, a: float) -> QColor:
    color = QColor(color)
    color.setAlphaF(color.alphaF() * a)
    return color


def size_text(count: int) -> str:
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if count >= scale:
            return f"{count / scale:.1f} {unit}" if scale > 1024 else f"{count // scale} {unit}"
    return f"{count} bytes"


def main_app(path: Path) -> Optional[apps.App]:
    """The app a file opens in: Maya first (the main app), then Open In's order."""
    choices = sorted(apps.apps_for(path.suffix.lower()), key=lambda app: app.id != "maya")
    return choices[0] if choices else None


def file_details(path_text: str) -> dict[str, Any]:
    """What a file's row shows: its name, what it is, its size, the app button, its picture (small)."""
    path = Path(path_text)
    ext = path.suffix.lower()
    details: dict[str, Any] = {"path": path_text, "name": path.name or path_text, "missing": False, "button": "",
                               "app": "", "icon": ICONS.get(ext, "cube"), "picture": ext in IMAGE_EXTENSIONS}
    if path.is_dir():
        try:
            count = sum(1 for p in path.rglob("*") if p.is_file())
        except OSError:
            count = 0
        details.update(info=f"Folder · {count} file{'s' if count != 1 else ''}", icon="folder", button="Show")
        return details
    if not path.is_file():
        details.update(info="Missing: Relink... finds it", missing=True, icon="warning")
        return details
    try:
        size = size_text(path.stat().st_size)
    except OSError:
        size = ""
    kind = "Picture" if details["picture"] else TYPE_NAMES.get(ext, f"{ext[1:].upper()} file" if ext else "File")
    details["info"] = " · ".join(p for p in (kind, size) if p)
    if ext in VIDEO_EXTENSIONS:
        details["icon"] = "play"
    app = main_app(path)
    if app is not None:
        details.update(button=app.name, app=app.id)
    elif details["picture"]:
        details["info"] += " · drag onto the Artbook"
    else:
        details["button"] = "Open"
    return details


# ----------------------------------------------------------------------
# The lesson list
# ----------------------------------------------------------------------
class _LessonDelegate(QStyledItemDelegate):
    TICK = 16

    def __init__(self, view: "LessonList"):
        super().__init__(view)
        self.view = view

    def sizeHint(self, option, index) -> QSize:
        return QSize(100, 30 if index.data(KIND) == "chapter" else 38)

    def tick_rect(self, rect: QRect) -> QRect:
        return QRect(rect.left() + 10, rect.center().y() - self.TICK // 2, self.TICK, self.TICK)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(4, 1, -4, -1)
        kind, base = index.data(KIND), option.font
        if index.row() == self.view.drop_row:
            painter.setPen(QPen(theme_color("accent_border"), 1.5))
            painter.setBrush(_alpha(theme_color("accent"), 0.25))
            painter.drawRoundedRect(rect, 8, 8)
        if kind == "chapter":
            painter.setFont(_font(base, 0.9, QFont.Weight.Bold))
            painter.setPen(theme_color("text_muted"))
            painter.drawText(rect.adjusted(10, 0, -8, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                             (index.data(Qt.ItemDataRole.DisplayRole) or "").upper())
            painter.restore()
            return
        lesson = index.data(DATA) or {}
        current = lesson.get("id") == self.view.current_id
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if current or selected:
            painter.setPen(QPen(_alpha(theme_color("accent_border"), 0.6), 1))
            painter.setBrush(_alpha(theme_color("accent_soft"), 0.35 if current else 0.2))
            painter.drawRoundedRect(rect, 8, 8)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_alpha(theme_color("hover"), 0.18))
            painter.drawRoundedRect(rect, 8, 8)
        x = rect.left() + 8
        if self.view.editing:  # (drag dots)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme_color("text_muted"))
            for dy in (-3, 3):
                for dx in (0, 5):
                    painter.drawEllipse(QPointF(x + 4 + dx, rect.center().y() + dy), 1.4, 1.4)
            x += 18
        else:
            box = QRectF(self.tick_rect(option.rect))
            if lesson.get("done"):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme_color("accent"))
                painter.drawRoundedRect(box, 4, 4)
                painter.setPen(QPen(theme_color("on_accent"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawPolyline([box.topLeft() + QPointF(4, 8.5), box.topLeft() + QPointF(7, 11.5), box.topLeft() + QPointF(12.5, 5)])
            else:
                painter.setPen(QPen(theme_color("text_muted"), 1.2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            x = box.right() + 10
        length = courses.format_length(courses.lesson_length(lesson))
        painter.setFont(_font(base, 0.92))
        metrics = QFontMetrics(painter.font())
        right = rect.right() - 8
        if length:
            painter.setPen(theme_color("text_muted"))
            painter.drawText(QRectF(right - 70, rect.top(), 70, rect.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, length)
            right -= metrics.horizontalAdvance(length) + 10
        count = len(lesson.get("files") or [])
        if count and right - x > 150:  # (how many files it has, when there's room)
            files = f"{count} file{'s' if count != 1 else ''}"
            painter.setPen(theme_color("text_muted"))
            painter.drawText(QRectF(right - 70, rect.top(), 70, rect.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, files)
            right -= metrics.horizontalAdvance(files) + 12
        if current:
            painter.drawPixmap(int(right - 16), int(rect.center().y() - 8), duotone_icon("play").pixmap(16, 16))
            right -= 22
        if lesson.get("url") and not lesson.get("video"):  # (a web lesson: a globe before its name)
            painter.drawPixmap(int(x), int(rect.center().y() - 8), duotone_icon("web").pixmap(16, 16))
            x += 22
        if lesson.get("number"):
            painter.setFont(_font(base, 0.92, QFont.Weight.DemiBold))
            painter.setPen(theme_color("text_muted"))
            painter.drawText(QRectF(x, rect.top(), 30, rect.height()), Qt.AlignmentFlag.AlignVCenter, lesson["number"])
            x += 30
        painter.setFont(_font(base, 1.0, QFont.Weight.DemiBold if current else QFont.Weight.Normal))
        painter.setPen(theme_color("text_strong") if current else theme_color("text"))
        name = QFontMetrics(painter.font()).elidedText(lesson.get("name", ""), Qt.TextElideMode.ElideRight, int(right - x - 6))
        painter.drawText(QRectF(x, rect.top(), right - x, rect.height()), Qt.AlignmentFlag.AlignVCenter, name)
        watched = float(lesson.get("watched") or 0)
        if not lesson.get("done") and watched > 0.02:  # (part watched: a thin bar along its bottom)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme_color("accent_hover"))
            painter.drawRoundedRect(QRectF(x, rect.bottom() - 4, (rect.right() - 8 - x) * watched, 2.5), 1.2, 1.2)
        painter.restore()


class LessonList(QListWidget):
    """Chapters and their lessons. Click a lesson to play it, its box to tick it. Editing: drag lessons about,
    double-click to rename, drop files on a lesson (its files) or videos anywhere (new lessons)."""

    lesson_clicked = Signal(str)
    tick_clicked = Signal(str)
    order_changed = Signal()
    files_dropped = Signal(str, str, list)  # the row's kind ("lesson" / "chapter" / ""), its id, the paths
    links_dropped = Signal(str, str, list)  # (the same, for web addresses dragged in from a browser)
    renamed = Signal(str, str, str)  # kind, id, the new name

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("courseLessons")
        self.editing = False
        self.current_id: Optional[str] = None
        self.drop_row = -1
        self._filling = False
        self.setItemDelegate(_LessonDelegate(self))
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.itemClicked.connect(self._on_clicked)
        self.itemChanged.connect(self._on_changed)
        self.setToolTip("Click a lesson to play it; its box marks it done")

    def fill(self, course: dict[str, Any]) -> None:
        self._filling = True
        selected = self.selected_id()
        self.clear()
        for chapter in course["chapters"]:
            row = QListWidgetItem(chapter["name"])
            row.setData(KIND, "chapter")
            row.setData(ID, chapter["id"])
            flags = Qt.ItemFlag.ItemIsEnabled
            if self.editing:
                flags |= Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled
            row.setFlags(flags)
            self.addItem(row)
            for lesson in chapter["lessons"]:
                item = QListWidgetItem(lesson["name"])
                item.setData(KIND, "lesson")
                item.setData(ID, lesson["id"])
                item.setData(DATA, lesson)
                item.setToolTip(lesson.get("url") or lesson.get("video") or "No video")
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if self.editing:
                    flags |= Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled
                item.setFlags(flags)
                self.addItem(item)
                if lesson["id"] == selected:
                    item.setSelected(True)
        self._filling = False

    def set_editing(self, on: bool) -> None:
        self.editing = on
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove if on else QAbstractItemView.DragDropMode.NoDragDrop)
        self.setAcceptDrops(on)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked if on else QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setToolTip("Drag lessons to reorder them, double-click to rename, drop files on a lesson" if on
                        else "Click a lesson to play it; its box marks it done")

    def selected_id(self) -> Optional[str]:
        items = self.selectedItems()
        return items[0].data(ID) if items else None

    def select(self, lesson_id: Optional[str]) -> None:
        for n in range(self.count()):
            item = self.item(n)
            if item.data(ID) == lesson_id:
                self.setCurrentItem(item)
                self.scrollToItem(item)
                return

    def order(self) -> list[tuple[str, str, str]]:
        """(kind, id, text) row by row, as they are now."""
        return [(self.item(n).data(KIND), self.item(n).data(ID), self.item(n).text()) for n in range(self.count())]

    def refresh_lesson(self, lesson: dict[str, Any]) -> None:
        for n in range(self.count()):
            if self.item(n).data(ID) == lesson["id"]:
                self._filling = True
                self.item(n).setData(DATA, lesson)
                self._filling = False
                return

    # ------------------ the mouse ------------------
    def mousePressEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        if (item is not None and item.data(KIND) == "lesson" and not self.editing
                and event.button() == Qt.MouseButton.LeftButton):
            box = self.itemDelegate().tick_rect(self.visualItemRect(item)).adjusted(-4, -4, 4, 4)
            if box.contains(event.position().toPoint()):  # (course.tick)
                self.tick_clicked.emit(item.data(ID))
                event.accept()
                return
        super().mousePressEvent(event)

    def _on_clicked(self, item: QListWidgetItem) -> None:
        if item.data(KIND) == "lesson" and not self.editing:  # (course.lesson_click)
            self.lesson_clicked.emit(item.data(ID))

    def _on_changed(self, item: QListWidgetItem) -> None:
        if not self._filling and item.text().strip():  # (course.lesson_rename)
            self.renamed.emit(item.data(KIND), item.data(ID), item.text().strip())

    # ------------------ dropping ------------------
    @staticmethod
    def _links(mime) -> list[str]:
        links = [u.toString() for u in mime.urls() if not u.isLocalFile() and u.scheme() in ("http", "https")]
        if not links and not mime.hasUrls() and mime.hasText():
            links = [line.strip() for line in mime.text().splitlines() if line.strip().lower().startswith(("http://", "https://"))]
        return links

    def dragEnterEvent(self, event) -> None:
        if self.editing and event.source() is not self and not event.mimeData().hasUrls() and self._links(event.mimeData()):
            event.acceptProposedAction()
            return
        if event.source() is self or (self.editing and event.mimeData().hasUrls()):
            super().dragEnterEvent(event) if event.source() is self else event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.source() is self:
            self.drop_row = -1
            super().dragMoveEvent(event)
            return
        if not (self.editing and (event.mimeData().hasUrls() or self._links(event.mimeData()))):
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        row = self.row(item) if item is not None else -1
        if row != self.drop_row:
            self.drop_row = row
            self.viewport().update()
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self.drop_row = -1
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self.drop_row = -1
        self.viewport().update()
        if event.source() is self:  # (course.lesson_drag)
            super().dropEvent(event)
            self.order_changed.emit()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        links = self._links(event.mimeData())
        if not (self.editing and (paths or links)):
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        kind, target = (item.data(KIND), item.data(ID)) if item is not None else ("", "")
        event.acceptProposedAction()
        if paths:
            self.files_dropped.emit(kind, target, paths)  # (course.file_drop)
        if links:
            self.links_dropped.emit(kind, target, links)  # (course.link_drop)


# ----------------------------------------------------------------------
# Files (a lesson's, the whole course's, the Course files tray)
# ----------------------------------------------------------------------
class _FileDelegate(QStyledItemDelegate):
    def __init__(self, view: "FileList"):
        super().__init__(view)
        self.view = view

    def sizeHint(self, option, index) -> QSize:
        if index.data(KIND) == "heading":
            return QSize(100, 30)
        if index.data(KIND) == "empty":
            return QSize(100, 64)
        return QSize(100, 58)

    def button_rect(self, rect: QRect, details: dict) -> QRect:
        if not details.get("button"):
            return QRect()
        width = QFontMetrics(_font(self.view.font(), 0.9, QFont.Weight.DemiBold)
                             ).horizontalAdvance(details["button"]) + 22
        return QRect(rect.right() - 10 - width, rect.center().y() - 14, width, 28)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        base = option.font
        rect = QRectF(option.rect).adjusted(6, 3, -6, -3)
        kind = index.data(KIND)
        if kind in ("heading", "empty"):
            if kind == "heading":
                painter.setFont(_font(base, 0.85, QFont.Weight.Bold))
                painter.setPen(theme_color("text_muted"))
                painter.drawText(rect.adjusted(6, 0, 0, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                                 index.data(Qt.ItemDataRole.DisplayRole).upper())
            else:
                painter.setFont(_font(base, 0.92))
                painter.setPen(theme_color("text_muted"))
                painter.drawText(rect.adjusted(8, 0, -8, 0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                                 | int(Qt.TextFlag.TextWordWrap), index.data(Qt.ItemDataRole.DisplayRole))
            painter.restore()
            return
        details = index.data(DATA) or {}
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setPen(QPen(theme_color("accent_border") if option.state & QStyle.StateFlag.State_Selected
                            else _alpha(theme_color("border"), 0.4), 1))
        painter.setBrush(_alpha(theme_color("hover"), 0.15) if hovered else theme_color("card"))
        painter.drawRoundedRect(rect, 9, 9)
        icon_box = QRectF(rect.left() + 8, rect.center().y() - 18, 48 if details.get("thumb") is not None else 36, 36)
        picture = details.get("thumb")
        if isinstance(picture, QPixmap) and not picture.isNull():
            clip = QPainterPath()
            clip.addRoundedRect(icon_box, 5, 5)
            painter.setClipPath(clip)
            fitted = picture.scaled(icon_box.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(int(icon_box.center().x() - fitted.width() / 2), int(icon_box.center().y() - fitted.height() / 2), fitted)
            painter.setClipping(False)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme_color("raised"))
            painter.drawRoundedRect(icon_box, 8, 8)
            tone = "warning" if details.get("missing") else "accent"
            painter.drawPixmap(int(icon_box.center().x() - 9), int(icon_box.center().y() - 9),
                               duotone_icon(details.get("icon", "cube"), tone).pixmap(18, 18))
        text_left = icon_box.right() + 10
        button = self.button_rect(option.rect, details)
        right = (button.left() - 8) if not button.isNull() else rect.right() - 8
        painter.setFont(_font(base, 1.0, QFont.Weight.DemiBold))
        painter.setPen(theme_color("text_strong"))
        name = QFontMetrics(painter.font()).elidedText(details.get("name", ""), Qt.TextElideMode.ElideMiddle, int(right - text_left))
        painter.drawText(QRectF(text_left, rect.top() + 6, right - text_left, 22), Qt.AlignmentFlag.AlignVCenter, name)
        painter.setFont(_font(base, 0.88))
        painter.setPen(theme_color("danger") if details.get("missing") else theme_color("text_muted"))
        info = QFontMetrics(painter.font()).elidedText(details.get("info", ""), Qt.TextElideMode.ElideRight, int(right - text_left))
        painter.drawText(QRectF(text_left, rect.top() + 27, right - text_left, 20), Qt.AlignmentFlag.AlignVCenter, info)
        if not button.isNull():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme_color("accent_hover") if hovered else theme_color("accent"))
            painter.drawRoundedRect(QRectF(button), 8, 8)
            painter.setFont(_font(base, 0.9, QFont.Weight.DemiBold))
            painter.setPen(theme_color("on_accent"))
            painter.drawText(QRectF(button), Qt.AlignmentFlag.AlignCenter, details["button"])
        painter.restore()


class FileList(QListWidget):
    """Files in sections ("This lesson", "The whole course"...). Its button opens a file in its app (Maya first), a
    folder in Explorer; double-click does the same; drag one out onto the Artbook or another app (or, editing, onto a
    lesson)."""

    open_requested = Signal(str, str)  # path, app id ("" : its default way)
    remove_requested = Signal(str, str)  # section id, path
    reveal_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("courseFiles")
        self.setItemDelegate(_FileDelegate(self))
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.itemDoubleClicked.connect(self._on_double_clicked)
        self.setToolTip("Double-click (or its button) to open a file; drag one onto the Artbook")

    def fill(self, sections: list[tuple[str, str, list[str]]], empty: str = "") -> None:
        """sections: (its id, its heading, the paths in it)."""
        self.clear()
        thumbs = 0
        for section_id, heading, paths in sections:
            if not paths:
                continue
            row = QListWidgetItem(heading)
            row.setData(KIND, "heading")
            row.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.addItem(row)
            for path in paths:
                details = file_details(path)
                details["section"] = section_id
                if details["picture"] and not details["missing"] and thumbs < MAX_THUMBS:
                    reader = QImageReader(path)
                    size = reader.size()
                    if size.isValid():
                        reader.setScaledSize(size.scaled(THUMB * 2, Qt.AspectRatioMode.KeepAspectRatioByExpanding))
                    image = reader.read()
                    details["thumb"] = QPixmap.fromImage(image) if not image.isNull() else None
                    thumbs += 1
                item = QListWidgetItem(details["name"])
                item.setData(KIND, "file")
                item.setData(ID, path)
                item.setData(DATA, details)
                item.setToolTip(path)
                self.addItem(item)
        if self.count() == 0 and empty:
            row = QListWidgetItem(empty)
            row.setData(KIND, "empty")
            row.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.addItem(row)

    def paths(self) -> list[str]:
        return [self.item(n).data(ID) for n in range(self.count()) if self.item(n).data(KIND) == "file"]

    def mousePressEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        if item is not None and item.data(KIND) == "file" and event.button() == Qt.MouseButton.LeftButton:
            details = item.data(DATA)
            if self.itemDelegate().button_rect(self.visualItemRect(item), details).contains(event.position().toPoint()):
                self.open_requested.emit(details["path"], details.get("app", ""))  # (course.file_open)
                event.accept()
                return
        super().mousePressEvent(event)

    def _on_double_clicked(self, item: QListWidgetItem) -> None:
        if item.data(KIND) == "file":
            details = item.data(DATA)
            self.open_requested.emit(details["path"], details.get("app", ""))

    def mimeData(self, items) -> QMimeData:  # (course.file_drag)
        paths = [item.data(ID) for item in items if item.data(KIND) == "file"]
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        mime.setData(FILES_MIME, json.dumps(paths).encode("utf-8"))
        return mime

    def contextMenuEvent(self, event) -> None:
        item = self.itemAt(event.pos())
        if item is None or item.data(KIND) != "file":
            return
        details = item.data(DATA)
        menu = QMenu(self)
        menu.addAction("Open", lambda: self.open_requested.emit(details["path"], details.get("app", "")))
        menu.addAction("Show in Explorer", lambda: self.reveal_requested.emit(details["path"]))
        menu.addAction("Copy Path", lambda: QApplication.clipboard().setText(details["path"]))
        menu.addSeparator()
        menu.addAction("Remove from the Course" if details["section"] != "lesson" else "Remove from the Lesson",
                       lambda: self.remove_requested.emit(details["section"], details["path"]))
        menu.exec(event.globalPos())
        menu.deleteLater()


# ----------------------------------------------------------------------
# Notes
# ----------------------------------------------------------------------
class _NoteDelegate(QStyledItemDelegate):
    PAD = 10

    def __init__(self, view: "NotesList"):
        super().__init__(view)
        self.view = view

    def _text_rect(self, width: int) -> QRect:
        return QRect(0, 0, max(60, width - 2 * self.PAD - 16), 10000)

    def sizeHint(self, option, index) -> QSize:
        note = index.data(DATA) or {}
        width = self.view.viewport().width()
        metrics = QFontMetrics(_font(option.font, 0.95))
        height = metrics.boundingRect(self._text_rect(width), int(Qt.TextFlag.TextWordWrap), note.get("text", "")).height()
        return QSize(width, height + 52)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        note = index.data(DATA) or {}
        rect = QRectF(option.rect).adjusted(6, 3, -6, -3)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setPen(QPen(_alpha(theme_color("border"), 0.4), 1))
        painter.setBrush(_alpha(theme_color("hover"), 0.15) if hovered else theme_color("card"))
        painter.drawRoundedRect(rect, 9, 9)
        stamp = format_time(note["at"]) if note.get("at") is not None else "Note"
        chip_font = _font(option.font, 0.85, QFont.Weight.DemiBold)
        painter.setFont(chip_font)
        chip = QRectF(rect.left() + self.PAD, rect.top() + 8, QFontMetrics(chip_font).horizontalAdvance(stamp) + 14, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme_color("note_bg"))
        painter.drawRoundedRect(chip, 9, 9)
        painter.setPen(theme_color("note_text"))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, stamp)
        painter.setFont(_font(option.font, 0.95))
        painter.setPen(theme_color("text_faint") if theme_color("text_faint").isValid() else theme_color("text"))
        text_rect = QRectF(rect.left() + self.PAD, chip.bottom() + 6, rect.width() - 2 * self.PAD, rect.bottom() - chip.bottom() - 10)
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap),
                         note.get("text", ""))
        painter.restore()


class NotesList(QListWidget):
    """A lesson's notes, each stamped with its moment: click one to jump back there; right-click to delete."""

    note_clicked = Signal(int)
    delete_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("courseNotes")
        self.setItemDelegate(_NoteDelegate(self))
        self.setMouseTracking(True)
        self.setWordWrap(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.itemClicked.connect(self._on_clicked)
        self.setToolTip("Click a note to jump back to its moment")

    def fill(self, notes: list[dict[str, Any]]) -> None:
        self.clear()
        for note in notes:
            item = QListWidgetItem(note.get("text", ""))
            item.setData(KIND, "note")
            item.setData(ID, note.get("id"))
            item.setData(DATA, note)
            self.addItem(item)

    def _on_clicked(self, item: QListWidgetItem) -> None:  # (course.note_click)
        if item.data(DATA).get("at") is not None:
            self.note_clicked.emit(int(item.data(DATA)["at"]))

    def contextMenuEvent(self, event) -> None:
        item = self.itemAt(event.pos())
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction("Delete Note", lambda: self.delete_requested.emit(item.data(ID)))
        menu.exec(event.globalPos())
        menu.deleteLater()


# ----------------------------------------------------------------------
# A long video's parts (cutting it into lessons)
# ----------------------------------------------------------------------
class CutBar(QWidget):
    """The parts of the video playing, each a lesson: K cuts at the moment playing (the window's key), drag a cut to
    move it, click a part to play it, double-click to name it, Delete joins the part picked with the next."""

    part_clicked = Signal(str)
    part_rename = Signal(str)
    cut_moved = Signal(str, int)  # the part before the cut, its new end (ms)
    join_requested = Signal(str)

    GRAB = 6

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("courseCuts")
        self.setMinimumHeight(56)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.parts: list[tuple[str, int, int, str]] = []  # (lesson id, start, end, name)
        self.length = 0
        self.position = 0
        self.current: Optional[str] = None
        self._drag: Optional[tuple[str, int]] = None  # (the part before the cut, where it is now)
        self.setToolTip("K (playing) cuts here · drag a cut to move it · double-click a part to name it · "
                        "Delete joins the part picked with the next")

    def set_parts(self, parts: list[tuple[str, int, int, str]], length: int, current: Optional[str]) -> None:
        self.parts, self.length, self.current = parts, max(1, length), current
        self.update()

    def set_position(self, position: int) -> None:
        self.position = position
        self.update()

    def _x(self, ms: int) -> float:
        return 4 + (self.width() - 8) * ms / max(1, self.length)

    def _ms(self, x: float) -> int:
        return int(max(0, min(self.length, (x - 4) / max(1, self.width() - 8) * self.length)))

    def _cut_at(self, x: float) -> Optional[str]:
        for lesson_id, _start, end, _name in self.parts[:-1]:
            if abs(self._x(end) - x) <= self.GRAB:
                return lesson_id
        return None

    def _part_at(self, x: float) -> Optional[str]:
        ms = self._ms(x)
        return next((p[0] for p in self.parts if p[1] <= ms < p[2]), None)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        top, height = 4.0, self.height() - 8.0
        for n, (lesson_id, start, end, name) in enumerate(self.parts):
            if self._drag is not None:
                if lesson_id == self._drag[0]:
                    end = self._drag[1]
                elif n > 0 and self.parts[n - 1][0] == self._drag[0]:
                    start = self._drag[1]
            box = QRectF(self._x(start) + 1, top, max(4.0, self._x(end) - self._x(start) - 2), height)
            on = lesson_id == self.current
            painter.setPen(QPen(_alpha(theme_color("accent_border"), 0.85 if on else 0.3), 1))
            painter.setBrush(_alpha(theme_color("accent_soft"), 0.55 if on else 0.25))
            painter.drawRoundedRect(box, 7, 7)
            painter.setPen(theme_color("text_strong"))
            painter.setFont(_font(self.font(), 0.9, QFont.Weight.DemiBold))
            label = QFontMetrics(painter.font()).elidedText(name, Qt.TextElideMode.ElideRight, int(box.width() - 12))
            painter.drawText(box.adjusted(8, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter, label)
        x = self._x(self.position)
        painter.setPen(QPen(theme_color("text_strong"), 2))
        painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        painter.end()

    def mousePressEvent(self, event) -> None:
        x = event.position().x()
        cut = self._cut_at(x)
        if cut is not None and event.button() == Qt.MouseButton.LeftButton:  # (course.cut_drag)
            self._drag = (cut, self._ms(x))
            return
        part = self._part_at(x)
        if part is not None:
            self.current = part
            self.update()
            self.part_clicked.emit(part)

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        if self._drag is not None:
            self._drag = (self._drag[0], self._ms(x))
            self.update()
            return
        self.setCursor(Qt.CursorShape.SizeHorCursor if self._cut_at(x) is not None else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is not None:
            lesson_id, ms = self._drag
            self._drag = None
            self.update()
            self.cut_moved.emit(lesson_id, ms)

    def mouseDoubleClickEvent(self, event) -> None:
        part = self._part_at(event.position().x())
        if part is not None:  # (course.part_rename)
            self.part_rename.emit(part)

    def keyPressEvent(self, event) -> None:
        if shortcuts.matches("course.remove", event) and self.current:
            self.join_requested.emit(self.current)
            return
        super().keyPressEvent(event)


# ----------------------------------------------------------------------
# The Courses page: a card per course
# ----------------------------------------------------------------------
class _CardDelegate(QStyledItemDelegate):
    SIZE = QSize(300, 292)

    def __init__(self, view: "CourseCards"):
        super().__init__(view)
        self.view = view

    def sizeHint(self, option, index) -> QSize:
        return self.SIZE + QSize(16, 16)

    def card_rect(self, rect: QRect) -> QRectF:
        return QRectF(rect.left() + 8, rect.top() + 8, self.SIZE.width(), self.SIZE.height())

    def button_rect(self, rect: QRect) -> QRectF:
        card = self.card_rect(rect)
        return QRectF(card.left() + 14, card.bottom() - 44, card.width() - 28, 30)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        course = index.data(DATA) or {}
        card = self.card_rect(option.rect)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setPen(QPen(theme_color("accent_border") if hovered else _alpha(theme_color("border"), 0.4), 1))
        painter.setBrush(theme_color("card"))
        painter.drawRoundedRect(card, 12, 12)
        cover = QRectF(card.left() + 1, card.top() + 1, card.width() - 2, 166)
        clip = QPainterPath()
        clip.addRoundedRect(cover, 11, 11)
        painter.setClipPath(clip)
        picture = self.view.covers.get(course.get("id"))
        if isinstance(picture, QPixmap) and not picture.isNull():
            fitted = picture.scaled(cover.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(int(cover.center().x() - fitted.width() / 2), int(cover.center().y() - fitted.height() / 2), fitted)
        else:
            painter.fillRect(cover, theme_color("raised"))
            painter.drawPixmap(int(cover.center().x() - 24), int(cover.center().y() - 24), duotone_icon("book").pixmap(48, 48))
        painter.setClipping(False)
        done, total, left = courses.progress(course)
        base = option.font
        painter.setFont(_font(base, 1.12, QFont.Weight.Bold))
        painter.setPen(theme_color("text_strong"))
        name = QFontMetrics(painter.font()).elidedText(course.get("name", ""), Qt.TextElideMode.ElideRight, int(card.width() - 28))
        painter.drawText(QRectF(card.left() + 14, cover.bottom() + 8, card.width() - 28, 22), Qt.AlignmentFlag.AlignVCenter, name)
        if done >= total and total:
            info = f"Done · {total} lessons"
        elif done == 0 and not any(float(l.get("watched") or 0) > 0 for l in courses.lessons(course)):
            info = f"Not started · {total} lesson{'s' if total != 1 else ''}"
        else:
            info = f"{done} of {total} lessons" + (f" · {courses.format_length(left)} left" if left else "")
        painter.setFont(_font(base, 0.9))
        painter.setPen(theme_color("text_muted"))
        painter.drawText(QRectF(card.left() + 14, cover.bottom() + 30, card.width() - 28, 18), Qt.AlignmentFlag.AlignVCenter, info)
        bar = QRectF(card.left() + 14, cover.bottom() + 54, card.width() - 28, 4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_alpha(theme_color("text_muted"), 0.25))
        painter.drawRoundedRect(bar, 2, 2)
        if total:
            painter.setBrush(theme_color("accent_hover"))
            painter.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * done / total, 4), 2, 2)
        button = self.button_rect(option.rect)
        going = 0 < done < total or (done == 0 and total and any(float(l.get("watched") or 0) > 0 for l in courses.lessons(course)))
        painter.setBrush(theme_color("accent") if going else theme_color("raised"))
        painter.drawRoundedRect(button, 8, 8)
        nxt = courses.next_up(course)
        if done >= total and total:
            label = "Watch again"
        elif nxt is None:
            label = "Add lessons"
        else:
            label = f"{'Continue' if going else 'Start'}: {(nxt.get('number') + ' ') if nxt.get('number') else ''}{nxt['name']}"
        painter.setFont(_font(base, 0.92, QFont.Weight.DemiBold))
        painter.setPen(theme_color("on_accent") if going else theme_color("text"))
        label = QFontMetrics(painter.font()).elidedText(label, Qt.TextElideMode.ElideRight, int(button.width() - 16))
        painter.drawText(button, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()


class CourseCards(QListWidget):
    """The Courses page: a card per course (its cover, how far you are, one button to carry on)."""

    open_course = Signal(str, bool)  # its id, carry on playing (the card's button)
    menu_requested = Signal(str, QPoint)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("courseCards")
        self.covers: dict[str, QPixmap] = {}
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setSpacing(4)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setItemDelegate(_CardDelegate(self))
        self.setToolTip("Click a course to open it; its button carries on where you left off")

    def fill(self, items: list[dict[str, Any]]) -> None:
        self.clear()
        for course in items:
            item = QListWidgetItem(course["name"])
            item.setData(ID, course["id"])
            item.setData(DATA, course)
            item.setSizeHint(_CardDelegate.SIZE + QSize(16, 16))
            self.addItem(item)

    def set_cover(self, course_id: str, picture: QPixmap) -> None:
        self.covers[course_id] = picture
        self.viewport().update()

    def mouseReleaseEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        if item is not None and event.button() == Qt.MouseButton.LeftButton:
            on_button = self.itemDelegate().button_rect(self.visualItemRect(item)).contains(event.position())
            self.open_course.emit(item.data(ID), on_button)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        item = self.itemAt(event.pos())
        if item is not None:
            self.menu_requested.emit(item.data(ID), event.globalPos())


def is_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def exists(path: str) -> bool:
    return os.path.exists(path)
