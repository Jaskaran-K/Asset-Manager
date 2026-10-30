"""The Artbook board: an endless canvas of reference images for one page.

BoardView (a QGraphicsView) holds ImageItems and handles everything you do with them:
getting images in (drop, paste, from the asset grid), moving, scaling, rotating, flipping,
stacking order, selecting, panning and zooming. Every change goes on the page's undo stack;
`changed` fires after each one so the panel can autosave.

Images are decoded off the UI thread (ui.tag_widgets.run_job). An item appears once its
image is ready; while a saved page loads, its items show as placeholders until then.

Controls are registered in ui.shortcuts (the Artbook category). While the board has the
keyboard focus it takes its keys first, so Ctrl+Z undoes board changes, not library ones.
"""

from __future__ import annotations

import base64
import html as html_lib
import io
import math
import re
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QFont,
    QImage,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QSyntaxHighlighter,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTransform,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSlider,
    QStyle,
    QStyleOptionGraphicsItem,
    QWidget,
    QWidgetAction,
)

from PIL import Image

from core import artbook_store as store
from core import artbook_share, pur_import
from ui import animations, artbook_layout, shortcuts
from ui.artbook_note_bar import NoteBar
from ui.tag_widgets import run_job
from ui.themes import theme_color

ZOOM_RANGE = (0.02, 32.0)
EXPORT_MAX_SIDE = 16384  # pixels: an exported picture's longest side...
EXPORT_MAX_PIXELS = 150_000_000  # ...and its area (about 600 MB while it's made)
EXPORT_MARGIN = 0.02  # around what's exported, of its longest side
WHEEL_ZOOM_STEP = 1.2  # per wheel notch
ZOOM_MS = 140
FIT_MS = 260
FIT_MARGIN = 0.08
DROP_GAP = 24.0  # between images dropped together
HANDLE_PX = 9.0  # on screen, whatever the zoom
MIN_CROP_PX = 8.0  # a crop keeps at least this much of the image (image pixels)
MIN_OPACITY = 10  # percent: fainter images are too easy to lose
CROP_OUTSIDE_OPACITY = 0.3  # crop mode: how the part being cut away shows
NOTE_WIDTH = 260.0  # a new note's width (its text wraps), before scaling
MIN_NOTE_WIDTH = 60.0
NOTE_FONT_PT = 14
NOTE_PADDING = 12.0
HIGHLIGHT_RADIUS = 4.0  # a note's highlights have rounded corners, like the note
MAX_NOTE_TEXT = 20000
NOTE_BAR_GAP = 38  # px between the format bar and the note (room for the rotate handle)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)  # a note never loads pictures from its HTML
ROTATE_HANDLE_PX = 26.0  # how far above the image the rotate handle sits
SCENE_EXTENT = 1_000_000.0
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


# Resize cursors by the direction a corner scales in: 0, 45, 90, 135 degrees (y points down)
_RESIZE_CURSORS = (
    Qt.CursorShape.SizeHorCursor, Qt.CursorShape.SizeFDiagCursor,
    Qt.CursorShape.SizeVerCursor, Qt.CursorShape.SizeBDiagCursor,
)
_rotate_cursor: Optional[QCursor] = None


def rotate_cursor() -> QCursor:
    """A circular arrow (Qt has no rotate cursor), white with a dark outline so it shows on anything."""
    global _rotate_cursor
    if _rotate_cursor is None:
        size = 24
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        arc = QPainterPath()
        arc.arcMoveTo(QRectF(5, 5, 14, 14), 100)
        arc.arcTo(QRectF(5, 5, 14, 14), 100, 260)
        head = QPolygonF([QPointF(9.2, 1.8), QPointF(14.6, 5.4), QPointF(9.0, 9.6)])
        for color, width in ((QColor(0, 0, 0, 200), 4.6), (QColor("#ffffff"), 2.2)):
            pen = QPen(color, width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(arc)
            painter.setBrush(color)
            painter.drawPolygon(head)
        painter.end()
        _rotate_cursor = QCursor(pixmap, size // 2, size // 2)
    return _rotate_cursor


Crop = tuple[float, float, float, float]  # left, top, right, bottom, in the image's own pixels


@dataclass(frozen=True)
class ItemState:
    """Where an image sits: center position (of the whole image), uniform scale, rotation (degrees),
    flips, stacking; and how it shows: crop (None: all of it), opacity, grayscale."""
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    z: float = 0.0
    crop: Optional[Crop] = None
    opacity: float = 1.0
    gray: bool = False
    width: Optional[float] = None  # notes: the width their text wraps at


def _gray_image(image: QImage) -> QImage:
    """image in grayscale, keeping its transparency."""
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    if image.hasAlphaChannel():
        painter = QPainter(gray)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, image)
        painter.end()
    return gray


def _clean_crop(value: Any, natural: tuple[int, int]) -> Optional[Crop]:
    """A saved crop, checked against the image's size (None: no crop, or a damaged one)."""
    try:
        left, top, right, bottom = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    w, h = natural
    left, top = max(0.0, min(left, w)), max(0.0, min(top, h))
    right, bottom = max(0.0, min(right, w)), max(0.0, min(bottom, h))
    if right - left < 1 or bottom - top < 1 or (left, top, right, bottom) == (0.0, 0.0, float(w), float(h)):
        return None
    return (left, top, right, bottom)


class ImageItem(QGraphicsItem):
    """One reference image. Its origin is the center of the whole image; size is the image's full
    pixel size. A crop only hides part of it (the whole image is kept), so it can be undone any time."""

    def __init__(self, item_id: str, source: dict[str, Optional[str]], size: tuple[int, int]):
        super().__init__()
        self.item_id = item_id
        self.source = source  # {"path": linked file or None, "blob": file in the page's images/ or None}
        self.natural = (max(1, int(size[0])), max(1, int(size[1])))
        self.pixmap: Optional[QPixmap] = None
        self._gray_pixmap: Optional[QPixmap] = None
        self.crop: Optional[Crop] = None
        self.gray = False
        self.cropping = False  # crop mode: the whole image shows (the cut-away part faintly)
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsMovable)

    # ------------------ state ------------------
    def state(self) -> ItemState:
        flip = self.transform()
        return ItemState(
            self.pos().x(), self.pos().y(), self.scale(), self.rotation(),
            flip.m11() < 0, flip.m22() < 0, self.zValue(),
            self.crop, round(self.opacity(), 4), self.gray,
        )

    def apply_state(self, state: ItemState) -> None:
        self.setPos(state.x, state.y)
        self.setScale(state.scale)
        self.setRotation(state.rotation)
        self.setTransform(QTransform.fromScale(-1.0 if state.flip_h else 1.0, -1.0 if state.flip_v else 1.0))
        self.setZValue(state.z)
        self.set_crop(state.crop)
        self.setOpacity(state.opacity)
        if state.gray != self.gray:
            self.gray = state.gray
            self.update()

    def to_dict(self) -> dict[str, Any]:
        s = self.state()
        return {
            "id": self.item_id, "kind": "image", "source": self.source, "size": list(self.natural),
            "x": s.x, "y": s.y, "scale": s.scale, "rotation": s.rotation,
            "flip_h": s.flip_h, "flip_v": s.flip_v, "z": s.z,
            "crop": list(s.crop) if s.crop else None, "opacity": s.opacity, "gray": s.gray,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImageItem":
        item = cls(str(data.get("id") or uuid.uuid4().hex), dict(data.get("source") or {}), tuple(data.get("size") or (512, 512)))
        try:
            opacity = min(1.0, max(MIN_OPACITY / 100, float(data.get("opacity", 1.0))))
        except (TypeError, ValueError):
            opacity = 1.0
        item.apply_state(ItemState(
            float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("scale", 1)) or 1.0,
            float(data.get("rotation", 0)), bool(data.get("flip_h")), bool(data.get("flip_v")), float(data.get("z", 0)),
            _clean_crop(data.get("crop"), item.natural), opacity, bool(data.get("gray")),
        ))
        return item

    def set_image(self, image: QImage) -> None:
        self.pixmap = QPixmap.fromImage(image)
        self._gray_pixmap = None
        self.update()

    def image(self) -> Optional[QImage]:
        """The image as it shows on the board: cropped (not flipped, rotated or grayscale)."""
        if self.pixmap is None:
            return None
        return self.pixmap.toImage().copy(self._pixmap_rect(self.crop_rect()).toAlignedRect())

    # ------------------ crop ------------------
    def full_rect(self) -> QRectF:
        w, h = self.natural
        return QRectF(-w / 2.0, -h / 2.0, w, h)

    def crop_rect(self) -> QRectF:
        """The part that shows, in the item's own coordinates."""
        if self.crop is None:
            return self.full_rect()
        left, top, right, bottom = self.crop
        w, h = self.natural
        return QRectF(left - w / 2.0, top - h / 2.0, right - left, bottom - top)

    def set_crop(self, crop: Optional[Crop]) -> None:
        if crop != self.crop:
            self.prepareGeometryChange()
            self.crop = crop
            self.update()

    def set_cropping(self, cropping: bool) -> None:
        if cropping != self.cropping:
            self.prepareGeometryChange()
            self.cropping = cropping
            self.update()

    def crop_from_rect(self, rect: QRectF) -> Optional[Crop]:
        """A crop for a rect in the item's own coordinates (None if it's the whole image)."""
        w, h = self.natural
        return _clean_crop((rect.left() + w / 2.0, rect.top() + h / 2.0, rect.right() + w / 2.0, rect.bottom() + h / 2.0), self.natural)

    def _pixmap_rect(self, rect: QRectF) -> QRectF:
        """Where rect (item coordinates) is in the pixmap, which may be a smaller copy of the image."""
        w, h = self.natural
        sx, sy = self.pixmap.width() / w, self.pixmap.height() / h
        return QRectF((rect.left() + w / 2.0) * sx, (rect.top() + h / 2.0) * sy, rect.width() * sx, rect.height() * sy)

    # ------------------ painting ------------------
    def boundingRect(self) -> QRectF:
        return self.full_rect() if self.cropping else self.crop_rect()

    def _shown_pixmap(self) -> QPixmap:
        scene = self.scene()
        if self.gray or (scene is not None and getattr(scene, "gray_all", False)):
            if self._gray_pixmap is None:
                self._gray_pixmap = QPixmap.fromImage(_gray_image(self.pixmap.toImage()))
            return self._gray_pixmap
        return self.pixmap

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        shown = self.crop_rect()
        if self.pixmap is not None:
            pixmap = self._shown_pixmap()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            if self.cropping:
                # The part being cut away, faintly, so you can drag the crop back out over it
                painter.save()
                painter.setOpacity(painter.opacity() * CROP_OUTSIDE_OPACITY)
                full = self.full_rect()
                painter.drawPixmap(full, pixmap, self._pixmap_rect(full))
                painter.restore()
            painter.drawPixmap(shown, pixmap, self._pixmap_rect(shown))
        else:
            # Still loading (or its file is gone): a quiet placeholder of the right size
            placeholder = QColor(theme_color("text_faint"))
            placeholder.setAlpha(40)
            painter.fillRect(shown, placeholder)
        # The selection frame is drawn by the view (BoardView.drawForeground), not here


NoteContent = tuple[str, Optional[str]]  # (the text as HTML, fill colour or None for the theme's)


class _NoSquareHighlights(QSyntaxHighlighter):
    """Hides the square boxes Qt draws behind highlighted text, so NoteItem.paint can draw rounded
    ones instead. It only changes how the text is drawn: the highlight stays in the note's
    formatting (and saved HTML), and nothing is added to undo."""

    CLEAR = QTextCharFormat()
    CLEAR.setBackground(QColor(0, 0, 0, 0))

    def highlightBlock(self, text: str) -> None:
        if text:
            self.setFormat(0, len(text), self.CLEAR)


def highlight_boxes(document: QTextDocument) -> list[tuple[QRectF, QColor]]:
    """Where a document's highlighted text is (one box per highlight colour per line, in document
    coordinates), and in which colour."""
    boxes: list[tuple[QRectF, QColor]] = []
    block = document.begin()
    while block.isValid():
        layout = block.layout()
        origin = layout.position()
        pieces = []  # (start, end, colour) within the block
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            if fmt.hasProperty(QTextFormat.Property.BackgroundBrush) and fmt.background().style() != Qt.BrushStyle.NoBrush:
                start = fragment.position() - block.position()
                pieces.append((start, start + fragment.length(), fmt.background().color()))
            it += 1
        for i in range(layout.lineCount()):
            line = layout.lineAt(i)
            line_start, line_end = line.textStart(), line.textStart() + line.textLength()
            for start, end, color in pieces:
                a, b = max(start, line_start), min(end, line_end)
                if a >= b:
                    continue
                x1, x2 = line.cursorToX(a)[0], line.cursorToX(b)[0]
                box = QRectF(min(x1, x2), line.y(), abs(x2 - x1), line.height()).translated(origin)
                last = boxes[-1] if boxes else None
                if last and last[1].rgba() == color.rgba() and abs(last[0].top() - box.top()) < 0.5 and abs(last[0].right() - box.left()) < 0.5:
                    boxes[-1] = (last[0].united(box), color)  # (the same highlight across bold, a new font...)
                else:
                    boxes.append((box, color))
        block = block.next()
    return boxes


class NoteItem(QGraphicsTextItem):
    """A text note on the board, in the theme's colours unless given its own. The text wraps at the
    note's width and can be formatted (colour, highlight, bold, italic, underline, size, font,
    alignment). It moves, scales, rotates, stacks, arranges and aligns like an image (its origin is
    its top-left); image-only tools skip it.

    Double-click (or T for a new one) to type; finishing (Esc, or clicking elsewhere) calls
    on_edit_finished, which records the change for undo."""

    def __init__(self, item_id: str, text: str = "", width: float = NOTE_WIDTH):
        super().__init__()
        self.item_id = item_id
        self.crop = None  # (image-only things: notes have none)
        self.gray = False
        self.cropping = False
        self.fill: Optional[str] = None  # None: the theme's surface colour
        self.on_edit_finished: Optional[Callable[["NoteItem"], None]] = None
        font = self.font()
        font.setPointSize(NOTE_FONT_PT)
        self.setFont(font)
        self.document().setDocumentMargin(NOTE_PADDING)
        self._highlights = _NoSquareHighlights(self.document())  # (rounded highlights: see paint)
        self._highlights.rehighlight()
        self.setTextWidth(max(MIN_NOTE_WIDTH, float(width)))
        self.setPlainText(text)
        self.apply_theme()
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    def apply_theme(self) -> None:
        """Text without a colour of its own takes the theme's (call when the theme changes)."""
        self.setDefaultTextColor(theme_color("text_strong"))
        self.update()

    # ------------------ state ------------------
    def state(self) -> ItemState:
        return ItemState(
            self.pos().x(), self.pos().y(), self.scale(), self.rotation(), False, False, self.zValue(),
            None, round(self.opacity(), 4), False, self.textWidth(),
        )

    def apply_state(self, state: ItemState) -> None:
        self.setPos(state.x, state.y)
        self.setScale(state.scale)
        self.setRotation(state.rotation)
        self.setZValue(state.z)
        self.setOpacity(state.opacity)
        if state.width is not None and state.width != self.textWidth():
            self.setTextWidth(max(MIN_NOTE_WIDTH, state.width))

    def content(self) -> NoteContent:
        return (self.toHtml(), self.fill)

    def set_content(self, content: NoteContent) -> None:
        html, fill = content
        self.setHtml(_IMG_TAG.sub("", html))
        self.fill = fill
        self.update()

    def to_dict(self) -> dict[str, Any]:
        s = self.state()
        return {
            "id": self.item_id, "kind": "note", "text": self.toPlainText(), "html": self.toHtml(),
            "fill": self.fill, "width": self.textWidth(),
            "x": s.x, "y": s.y, "scale": s.scale, "rotation": s.rotation, "z": s.z, "opacity": s.opacity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NoteItem":
        text, html, fill = data.get("text"), data.get("html"), data.get("fill")
        fit = "width" in data and data["width"] is None  # (as wide as its text: imported notes)
        try:
            width = NOTE_WIDTH if fit else float(data.get("width", NOTE_WIDTH))
        except (TypeError, ValueError):
            width = NOTE_WIDTH
        try:
            opacity = min(1.0, max(MIN_OPACITY / 100, float(data.get("opacity", 1.0))))
        except (TypeError, ValueError):
            opacity = 1.0
        note = cls(str(data.get("id") or uuid.uuid4().hex), text[:MAX_NOTE_TEXT] if isinstance(text, str) else "", width)
        if isinstance(html, str) and html:
            note.set_content((html, None))  # formatted text (the plain "text" is kept for search / older versions)
        if fit:
            note.setTextWidth(-1)
            width = max(MIN_NOTE_WIDTH, float(int(note.document().idealWidth()) + 2))
        note.fill = fill if isinstance(fill, str) and QColor(fill).isValid() else None
        note.apply_state(ItemState(
            float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("scale", 1)) or 1.0,
            float(data.get("rotation", 0)), z=float(data.get("z", 0)), opacity=opacity, width=width,
        ))
        return note

    def full_rect(self) -> QRectF:
        return self.boundingRect()

    def crop_rect(self) -> QRectF:
        return self.boundingRect()

    def image(self) -> None:
        return None

    # ------------------ editing ------------------
    def editing(self) -> bool:
        return self.textInteractionFlags() != Qt.TextInteractionFlag.NoTextInteraction

    def start_editing(self) -> None:
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)  # type on from the end
        self.setTextCursor(cursor)

    def stop_editing(self) -> None:
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.clearFocus()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        # Switching to another app (or a menu opening) isn't finishing: you're coming back to type
        if self.editing() and event.reason() not in (Qt.FocusReason.ActiveWindowFocusReason, Qt.FocusReason.PopupFocusReason):
            if self.on_edit_finished is not None:
                self.on_edit_finished(self)

    # ------------------ painting ------------------
    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        paper = QColor(self.fill) if self.fill else theme_color("surface")
        edge = paper.darker(125) if self.fill else theme_color("border")
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(edge, 1.5))
        painter.setBrush(paper)
        painter.drawRoundedRect(self.boundingRect().adjusted(0.75, 0.75, -0.75, -0.75), 6, 6)
        painter.setPen(Qt.PenStyle.NoPen)
        for box, color in highlight_boxes(self.document()):
            radius = min(HIGHLIGHT_RADIUS, box.height() / 3)
            painter.setBrush(color)
            painter.drawRoundedRect(box.adjusted(-2, 0, 2, 0), radius, radius)  # (a little room round the letters)
        painter.restore()
        plain = QStyleOptionGraphicsItem(option)  # no dashed focus / selection box: the view draws the frame
        plain.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus)
        super().paint(painter, plain, widget)


BOARD_ITEMS = (ImageItem, NoteItem)


class _ColorReadout(QWidget):
    """Colour picker: the colour under the cursor and its hex code, beside the cursor."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.color = QColor()
        self.resize(118, 30)

    def set_color(self, color: QColor) -> None:
        self.color = color
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme_color("border"), 1))
        painter.setBrush(theme_color("surface"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
        painter.setPen(QPen(QColor(255, 255, 255, 140), 1))
        painter.setBrush(self.color)
        painter.drawRoundedRect(QRectF(6, 6, 18, 18), 4, 4)
        painter.setPen(theme_color("text_strong"))
        painter.drawText(QRect(32, 0, self.width() - 36, self.height()), Qt.AlignmentFlag.AlignVCenter, self.color.name().upper())


# ----------------------------------------------------------------------
# Undo commands
# ----------------------------------------------------------------------
class _StateCommand(QUndoCommand):
    def __init__(self, board: "BoardView", before: dict[str, ItemState], after: dict[str, ItemState], text: str):
        super().__init__(text)
        self.board, self.before, self.after = board, before, after

    def redo(self) -> None:
        self.board.apply_states(self.after)

    def undo(self) -> None:
        self.board.apply_states(self.before)


class _AddCommand(QUndoCommand):
    def __init__(self, board: "BoardView", items: list[ImageItem], text: str):
        super().__init__(text)
        self.board, self.items = board, items  # kept alive here while they're off the board

    def redo(self) -> None:
        for item in self.items:
            self.board.attach(item)

    def undo(self) -> None:
        for item in self.items:
            self.board.detach(item)


class _RemoveCommand(_AddCommand):
    def redo(self) -> None:
        super().undo()

    def undo(self) -> None:
        super().redo()


class _TextCommand(QUndoCommand):
    """A note's text and formatting (NoteContent) changing. Commands with the same merge key (a live
    colour pick) add up to one step."""

    def __init__(self, note: "NoteItem", before: "NoteContent", after: "NoteContent", text: str = "edit note", merge: Optional[int] = None):
        super().__init__(text)
        self.note, self.before, self.after, self.merge = note, before, after, merge

    def id(self) -> int:
        return 1 if self.merge is not None else -1

    def mergeWith(self, other: QUndoCommand) -> bool:
        if not isinstance(other, _TextCommand) or other.merge != self.merge or other.note is not self.note:
            return False
        self.after = other.after
        return True

    def redo(self) -> None:
        self.note.set_content(self.after)

    def undo(self) -> None:
        self.note.set_content(self.before)


# ----------------------------------------------------------------------
# Preparing images (worker threads)
# ----------------------------------------------------------------------
def _prepare_file(page_id: str, path: Path) -> dict[str, Any]:
    """Decode a dropped file and keep a small copy of it in the page (in case the file moves)."""
    image, size = store.decode_file(path)
    data, ext = store.encode_fallback(image)
    blob = store.store_blob(page_id, data, ext)
    return {"image": image, "size": size, "source": {"path": str(path), "blob": blob}}


def _prepare_bytes(page_id: str, data: bytes, ext: str) -> dict[str, Any]:
    """Pasted / downloaded image data: stored in full (it has no file to link to)."""
    image, size = store.decode_bytes(data)
    blob = store.store_blob(page_id, data, ext)
    return {"image": image, "size": size, "source": {"path": None, "blob": blob}}


def _prepare_qimage(page_id: str, image: QImage) -> dict[str, Any]:
    return _prepare_bytes(page_id, store.encode_png(image), ".png")


_IMG_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_OG_IMAGE = re.compile(
    r"""<meta\b[^>]*?(?:property|name)\s*=\s*["'](?:og:image|twitter:image)["'][^>]*?content\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_DATA_URI = re.compile(r"data:image/([a-z0-9.+-]+);base64,(.+)", re.IGNORECASE | re.DOTALL)
_BROWSER_UA = (  # some image hosts refuse requests that don't look like a browser
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def web_image_sources(urls: list[str], html: str = "") -> list[str]:
    """Where a dragged / pasted web image can be fetched from, best first.

    Search results (Google Images and the like) hand over a link to a page, not the image;
    the dragged picture comes along as HTML. So, in order: the real image a result link
    wraps (?imgurl=), the HTML's <img> address, the image data embedded in the HTML (a
    smaller copy, but always there), then the links themselves.
    """
    sources: list[str] = []

    def add(url: str) -> None:
        url = html_lib.unescape(url.strip())
        if url and url not in sources and (_is_web_url(url) or url.startswith("data:image/")):
            sources.append(url)

    for url in urls:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        for key in ("imgurl", "mediaurl", "img_url", "image_url"):
            for value in query.get(key, []):
                add(value)
    embedded = []
    for src in _IMG_SRC.findall(html or ""):
        if _is_web_url(src):
            add(src)
        elif src.startswith("data:image/"):
            embedded.append(src)
    for src in embedded:
        add(src)
    for url in urls:
        add(url)
    return sources


def _download(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA, "Accept": "image/*,*/*;q=0.8"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 (http(s) only: web_image_sources)
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
        kind = response.headers.get_content_type()
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise store.PageError("That image is too big to download (over 64 MB).")
    return data, kind


def _image_ext(data: bytes) -> Optional[str]:
    """".png" etc. if data is an image Pillow can open (whatever the server called it), else None."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return "." + (im.format or "img").lower().replace("jpeg", "jpg")
    except Exception:  # pylint: disable=broad-except
        return None


def _fetch_image(url: str) -> tuple[bytes, str]:
    """(image data, extension) from a data: URI or a web address. A web page's preview image
    (og:image) stands in when the address is a page rather than an image."""
    match = _DATA_URI.match(url)
    if match:
        data = base64.b64decode(match.group(2))
    else:
        data, kind = _download(url)
        if _image_ext(data) is None and kind in ("text/html", "application/xhtml+xml"):
            page = data[:2_000_000].decode("utf-8", errors="replace")
            preview = _OG_IMAGE.search(page)
            if preview:
                data, _kind = _download(urllib.parse.urljoin(url, html_lib.unescape(preview.group(1))))
    ext = _image_ext(data)
    if ext is None:
        raise store.PageError("That link isn't an image.")
    return data, ext


def _prepare_web(page_id: str, sources: list[str]) -> dict[str, Any]:
    """The first source that gives a usable image, stored in full (it has no file to link to)."""
    error = "Nothing to download."
    for source in sources:
        try:
            data, ext = _fetch_image(source)
            return _prepare_bytes(page_id, data, ext)
        except Exception as exc:  # pylint: disable=broad-except
            error = str(exc) or type(exc).__name__
    raise store.PageError(f"Couldn't get the image: {error}")


def _decode_saved(page_id: str, source: dict[str, Optional[str]]) -> QImage:
    """A saved item's image: its linked file if it's still there, otherwise the copy in the page."""
    path = source.get("path")
    if path and Path(path).is_file():
        try:
            return store.decode_file(Path(path))[0]
        except store.PageError:
            pass
    blob = source.get("blob")
    if blob:
        return store.decode_file(store.blob_path(page_id, blob))[0]
    raise store.PageError("The image is missing.")


# ----------------------------------------------------------------------
# The board
# ----------------------------------------------------------------------
def _is_web_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


class BoardView(QGraphicsView):
    """The endless board of one page (see the module docstring)."""

    changed = Signal()  # the page's content changed (autosave)
    zoom_changed = Signal(float)
    boards_dropped = Signal(list)  # board files dropped on it (.artbook, PureRef .pur): to import as pages
    export_image_requested = Signal()  # Ctrl+E / the menu: the panel asks where, then calls export_image
    export_page_requested = Signal()  # the menu: share the page as an .artbook file

    # Keys the board handles itself while it has the focus (even ones the window also uses)
    KEYS = (
        "artbook.paste", "artbook.copy", "artbook.fit", "artbook.zoom_100", "artbook.flip_h", "artbook.flip_v",
        "artbook.front", "artbook.back", "artbook.reset", "artbook.delete", "artbook.select_all",
        "artbook.deselect", "artbook.undo", "artbook.redo", "artbook.pan_key", "artbook.arrange",
        "artbook.crop", "artbook.crop_apply", "artbook.gray", "artbook.gray_board", "artbook.note", "artbook.picker",
        "artbook.export_png",
    )

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("artbookBoard")
        self.setProperty("smoothScroll", False)  # the wheel zooms here (ui.views would scroll)
        self._scene = QGraphicsScene(-SCENE_EXTENT, -SCENE_EXTENT, 2 * SCENE_EXTENT, 2 * SCENE_EXTENT, self)
        self._scene.selectionChanged.connect(self._on_selection_changed)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)  # the selection frame
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)  # hover cursors over handles and images
        self.centerOn(0, 0)

        self.undo_stack = QUndoStack(self)
        self.undo_stack.indexChanged.connect(self._on_undo_index_changed)
        self.page_id: Optional[str] = None
        self._items: dict[str, ImageItem] = {}
        self._generation = 0  # bumps when another page loads: late image results are dropped
        self._space_down = False
        self._drag: Optional[dict[str, Any]] = None  # pan / scale / rotate / move in progress
        self._drop_batch: Optional[dict[str, Any]] = None
        self._loading = False
        # Separate View: makes the window's options submenu for the right-click menu (ui.view_modes)
        self.window_menu: Optional[Callable[[QMenu], QMenu]] = None
        self._crop: Optional[dict[str, Any]] = None  # crop mode: {"item", "before", "rect"}
        self._editing: Optional[dict[str, Any]] = None  # typing in a note: {"note", "new", "before"}
        self._picking = False  # colour picker on
        self._readout = _ColorReadout(self)
        self._readout.hide()
        self._note_bar: Optional[NoteBar] = None  # floats above the selected note (made when first needed:
        self._note_bar_queued = False  # listing the system's fonts for it is slow)
        self._live_merge: Optional[int] = None  # (see live_note_change)
        self._live_merges = 0
        self._scene.gray_all = False  # the whole board in grayscale (read by ImageItem.paint)

        self._zoom_anim = QVariantAnimation(self)
        self._zoom_anim.setDuration(ZOOM_MS)
        self._zoom_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._zoom_anim.valueChanged.connect(self._on_zoom_step)
        self._zoom_target = 1.0
        self._zoom_anchor = QPointF()
        self._view_anim = QVariantAnimation(self)
        self._view_anim.setDuration(FIT_MS)
        self._view_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._view_anim.valueChanged.connect(self._on_view_step)
        self._view_from = self._view_to = (1.0, QPointF())

        self.hint = QLabel(
            "Drop images here, paste them with Ctrl+V, or drag them in from the asset grid.\n"
            "Middle-drag (or Space+drag) to pan, scroll to zoom, F to frame everything.",
            self,
        )
        self.hint.setObjectName("boardHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.toast = QLabel(self)
        self.toast.setObjectName("boardToast")
        self.toast.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.toast.setWordWrap(True)  # (an import's summary can be long)
        self.toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(3500)
        self._toast_timer.timeout.connect(self.toast.hide)

    # ------------------ page ------------------
    def load_page(self, page: dict[str, Any]) -> None:
        """Show a page (its images fill in as they're decoded)."""
        self._generation += 1
        self.cancel_crop()
        self.finish_note_edit()
        self.stop_picker()
        self._loading = True  # clearing the undo stack isn't a change to save
        self.undo_stack.clear()
        self._loading = False
        self._scene.clearSelection()
        for item in list(self._items.values()):
            self.detach(item)
        self._items.clear()
        self.page_id = page["id"]
        generation = self._generation
        for data in page.get("items", []):
            if data.get("kind") == "note":
                self.attach(NoteItem.from_dict(data))
                continue
            if data.get("kind") != "image":
                continue
            item = ImageItem.from_dict(data)
            self.attach(item)
            page_id, source = self.page_id, dict(item.source)
            run_job(
                lambda page_id=page_id, source=source: _decode_saved(page_id, source),
                lambda image, item=item, generation=generation: self._on_saved_decoded(item, image, generation),
                lambda error, item=item, generation=generation: self._on_saved_failed(item, error, generation),
            )
        view = page.get("view")
        if isinstance(view, dict):
            self.set_view(float(view.get("zoom", 1.0)), QPointF(float(view.get("x", 0)), float(view.get("y", 0))))
        else:
            self.set_view(1.0, QPointF(0, 0))
        self.set_board_gray(isinstance(view, dict) and view.get("gray") is True)
        self._update_hint()

    def page_items(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in sorted(self._items.values(), key=lambda i: i.zValue())]

    def thumbnail(self, width: int = 160, height: int = 120) -> Optional[QImage]:
        """Everything on the page, fitted into width x height (transparent around it); None if empty."""
        if not self._items:
            return None
        bounds = QRectF()
        for item in self._items.values():
            bounds = bounds.united(item.sceneBoundingRect())
        margin = max(bounds.width(), bounds.height()) * 0.04
        bounds.adjust(-margin, -margin, margin, margin)
        scale = min(width / bounds.width(), height / bounds.height())
        target = QRectF(0, 0, bounds.width() * scale, bounds.height() * scale)
        target.moveCenter(QPointF(width / 2, height / 2))
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        gray_all, self._scene.gray_all = self._scene.gray_all, False  # the list shows pages in colour
        try:
            self._scene.render(painter, target, bounds)  # items only: the selection frame is the view's
        finally:
            self._scene.gray_all = gray_all
        painter.end()
        return image

    def request_export_image(self) -> None:
        if self._items:
            self.export_image_requested.emit()

    def export_image(self) -> Optional[QImage]:
        """The selection (or everything) as one picture, on the board's colour, with the detail its
        images were loaded at (within EXPORT_MAX_SIDE / EXPORT_MAX_PIXELS). None if nothing's there."""
        self.finish_note_edit()
        if self._crop:
            self.apply_crop()
        items = self.selected() or self.items_list()
        if not items:
            return None
        bounds = QRectF()
        for item in items:
            bounds = bounds.united(item.sceneBoundingRect())
        margin = max(bounds.width(), bounds.height()) * EXPORT_MARGIN
        bounds.adjust(-margin, -margin, margin, margin)
        density = 1.0  # output pixels per board unit: as sharp as the sharpest image shows
        for item in items:
            if isinstance(item, ImageItem) and item.pixmap is not None and item.scale() > 0:
                density = max(density, item.pixmap.width() / (item.natural[0] * item.scale()))
        density = min(
            density, EXPORT_MAX_SIDE / max(bounds.width(), bounds.height()),
            math.sqrt(EXPORT_MAX_PIXELS / (bounds.width() * bounds.height())),
        )
        size = QSize(max(1, round(bounds.width() * density)), max(1, round(bounds.height() * density)))
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(theme_color("grid_bg"))
        hidden = [i for i in self.items_list() if i not in items and i.isVisible()]  # (only what's exported shows)
        for item in hidden:
            item.setVisible(False)
        painter = QPainter(image)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.TextAntialiasing)
        try:
            self._scene.render(painter, QRectF(0, 0, size.width(), size.height()), bounds)
        finally:
            painter.end()
            for item in hidden:
                item.setVisible(True)
        return image

    def view_state(self) -> dict[str, Any]:
        center = self.view_center()
        return {"x": center.x(), "y": center.y(), "zoom": self.zoom(), "gray": self._scene.gray_all}

    def items_list(self) -> list[ImageItem]:
        return list(self._items.values())

    def item(self, item_id: str) -> Optional[ImageItem]:
        return self._items.get(item_id)

    def attach(self, item: ImageItem) -> None:
        if isinstance(item, NoteItem):
            item.on_edit_finished = self._on_note_edit_finished
        self._items[item.item_id] = item
        if item.scene() is None:
            self._scene.addItem(item)
        self._update_hint()

    def detach(self, item: ImageItem) -> None:
        self._items.pop(item.item_id, None)
        if item.scene() is self._scene:
            item.setSelected(False)
            self._scene.removeItem(item)
        self._update_hint()

    def apply_states(self, states: dict[str, ItemState]) -> None:
        for item_id, state in states.items():
            item = self._items.get(item_id)
            if item is not None:
                item.apply_state(state)
        self.viewport().update()

    def _on_saved_decoded(self, item: ImageItem, image: QImage, generation: int) -> None:
        if generation == self._generation:
            item.set_image(image)

    def _on_saved_failed(self, item: ImageItem, error: str, generation: int) -> None:
        if generation == self._generation:
            self.show_message(f"An image couldn't be loaded: {error}")

    def _on_undo_index_changed(self, _index: int) -> None:
        if not self._loading:
            self.changed.emit()

    def _update_hint(self) -> None:
        self.hint.setVisible(not self._items)

    # ------------------ messages ------------------
    def show_message(self, text: str) -> None:
        self.toast.setText(text)
        self.toast.adjustSize()
        self._place_overlays()
        self.toast.show()
        self.toast.raise_()
        self._toast_timer.start(max(3500, 60 * len(text)))  # (time to read a long one)

    def _place_overlays(self) -> None:
        self.hint.setGeometry(self.rect().adjusted(40, 0, -40, 0))
        width = max(40, min(self.fontMetrics().horizontalAdvance(self.toast.text()) + 48, 560, self.width() - 40))
        height = max(34, self.toast.heightForWidth(width) + 4)
        self.toast.setGeometry((self.width() - width) // 2, self.height() - 22 - height, width, height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_overlays()

    # ------------------ adding images ------------------
    def add_files(self, paths: list[Path], at: Optional[QPointF] = None) -> int:
        """Add image files (linked, with a fallback copy); returns how many were images."""
        images = [Path(p) for p in paths if Path(p).suffix.lower() in store.IMAGE_EXTENSIONS and Path(p).is_file()]
        if len(images) < len(paths):
            skipped = len(paths) - len(images)
            self.show_message(f"Only images go on a page; skipped {skipped} other file{'s' if skipped != 1 else ''}.")
        self._start_batch(at)
        for path in images:
            self._prepare(lambda page_id, path=path: _prepare_file(page_id, path))
        return len(images)

    def add_image(self, image: QImage, at: Optional[QPointF] = None) -> None:
        self._start_batch(at)
        copy = QImage(image)  # the clipboard's image may change before the worker reads it
        self._prepare(lambda page_id: _prepare_qimage(page_id, copy))

    def add_web_image(self, sources: list[str], at: Optional[QPointF] = None) -> None:
        """One image from the web: the first of `sources` (web_image_sources) that works."""
        self._start_batch(at)
        if any(_is_web_url(source) for source in sources):
            self.show_message("Downloading the image...")
        self._prepare(lambda page_id: _prepare_web(page_id, sources))

    def _start_batch(self, at: Optional[QPointF]) -> None:
        """Images added together line up left to right in the order given, starting at `at`
        (default: the view's center). They're decoded in parallel, so results wait their turn."""
        point = at if at is not None else self.view_center()
        self._drop_batch = {"x": point.x(), "y": point.y(), "first": True, "count": 0, "next": 0, "ready": {}}

    def _prepare(self, fn: Callable[[str], dict[str, Any]]) -> None:
        if self.page_id is None:
            return
        page_id, generation, batch = self.page_id, self._generation, self._drop_batch
        index = batch["count"]
        batch["count"] += 1
        run_job(
            lambda: fn(page_id),
            lambda result: self._on_batch_result(batch, index, result, generation),
            lambda error: self._on_batch_result(batch, index, None, generation, error),
        )

    def _on_batch_result(
        self, batch: dict[str, Any], index: int, result: Optional[dict[str, Any]], generation: int, error: str = ""
    ) -> None:
        if error:
            self.show_message(error)
        batch["ready"][index] = result
        while batch["next"] in batch["ready"]:
            ready = batch["ready"].pop(batch["next"])
            batch["next"] += 1
            if ready is not None:
                self._on_prepared(ready, generation, batch)

    def _on_prepared(self, result: dict[str, Any], generation: int, batch: dict[str, Any]) -> None:
        if generation != self._generation:
            return  # another page is showing now
        width, height = result["size"]
        item = ImageItem(uuid.uuid4().hex, result["source"], (width, height))
        item.set_image(result["image"])
        if batch["first"]:
            x = batch["x"]  # the first one is centered where it was dropped
            batch["first"] = False
        else:
            x = batch["x"] + DROP_GAP + width / 2.0  # the rest line up to its right
        batch["x"] = x + width / 2.0  # where the next one starts
        top = max((i.zValue() for i in self._items.values()), default=0.0)
        item.apply_state(ItemState(x=x, y=batch["y"], z=top + 1.0))
        self.undo_stack.push(_AddCommand(self, [item], "add image"))
        self._scene.clearSelection()
        item.setSelected(True)

    def paste(self, at: Optional[QPointF] = None) -> bool:
        """Ctrl+V: image files, image data, or an image link from the clipboard."""
        return self._take(QApplication.clipboard().mimeData(), at)

    @staticmethod
    def can_paste() -> bool:
        """Whether the clipboard holds something paste() can use (greys out Paste in the menu)."""
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return False
        if mime.hasUrls() or mime.hasImage():
            return True
        text = mime.text().strip() if mime.hasText() else ""
        return bool(text) and (_is_web_url(text) or Path(text.strip('"')).is_file())

    def _take(self, mime, at: Optional[QPointF]) -> bool:
        if mime is None:
            return False
        files = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()] if mime.hasUrls() else []
        if files:
            return self.add_files(files, at) > 0
        if mime.hasImage():
            image = QImage(mime.imageData())
            if not image.isNull():
                self.add_image(image, at)
                return True
        links = [u.toString() for u in mime.urls() if _is_web_url(u.toString())] if mime.hasUrls() else []
        text = mime.text().strip() if mime.hasText() else ""
        if not links and _is_web_url(text):
            links = [text]
        elif not links and text and Path(text.strip('"')).is_file():
            return self.add_files([Path(text.strip('"'))], at) > 0
        html = mime.html() if mime.hasHtml() else ""
        if len(links) > 1 and not html:
            for link in links[:20]:  # several copied image links: one image each
                self.add_web_image(web_image_sources([link]), at)
            return True
        sources = web_image_sources(links[:20], html)  # a dragged web image: its link plus the picture
        if sources:
            self.add_web_image(sources, at)
        return bool(sources)

    # ------------------ drag and drop ------------------
    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasImage() or mime.hasHtml() or (mime.hasText() and _is_web_url(mime.text().strip())):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dropEvent(self, event) -> None:
        at = self.mapToScene(event.position().toPoint())
        boards = [
            url.toLocalFile() for url in event.mimeData().urls()
            if url.isLocalFile() and url.toLocalFile().lower().endswith((pur_import.SUFFIX, artbook_share.SUFFIX))
        ]
        if boards:
            self.boards_dropped.emit(boards)
        if self._take(event.mimeData(), at) or boards:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.setFocus()
        else:
            event.ignore()

    # ------------------ view: zoom and pan ------------------
    def zoom(self) -> float:
        return self.transform().m11()

    def view_center(self) -> QPointF:
        return self.mapToScene(self.viewport().rect().center())

    def set_view(self, zoom: float, center: QPointF) -> None:
        zoom = max(ZOOM_RANGE[0], min(ZOOM_RANGE[1], zoom))
        self.setTransform(QTransform.fromScale(zoom, zoom))
        self.centerOn(center)
        self.zoom_changed.emit(zoom)

    def zoom_at(self, zoom: float, anchor: QPointF) -> None:
        """Zoom keeping the board point under `anchor` (a viewport position) where it is."""
        zoom = max(ZOOM_RANGE[0], min(ZOOM_RANGE[1], zoom))
        fixed = self.mapToScene(anchor.toPoint())
        self.setTransform(QTransform.fromScale(zoom, zoom))
        moved = self.mapFromScene(fixed) - anchor.toPoint()
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + moved.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + moved.y())
        self.zoom_changed.emit(zoom)

    def wheelEvent(self, event) -> None:
        notches = event.angleDelta().y() / 120.0
        if not notches:
            return
        anchor = event.position()
        running = self._zoom_anim.state() == QVariantAnimation.State.Running
        base = self._zoom_target if running else self.zoom()
        target = max(ZOOM_RANGE[0], min(ZOOM_RANGE[1], base * WHEEL_ZOOM_STEP ** notches))
        self._zoom_target = target
        self._view_anim.stop()
        if not animations.motion_enabled():
            self.zoom_at(target, anchor)
            return
        self._zoom_anchor = anchor
        self._zoom_anim.stop()
        self._zoom_anim.setStartValue(math.log(self.zoom()))
        self._zoom_anim.setEndValue(math.log(target))
        self._zoom_anim.start()
        event.accept()

    def _on_zoom_step(self, value) -> None:
        self.zoom_at(math.exp(float(value)), self._zoom_anchor)

    def animate_view(self, zoom: float, center: QPointF) -> None:
        zoom = max(ZOOM_RANGE[0], min(ZOOM_RANGE[1], zoom))
        self._zoom_anim.stop()
        if not animations.motion_enabled() or not self.isVisible():
            self.set_view(zoom, center)
            return
        self._view_from = (self.zoom(), self.view_center())
        self._view_to = (zoom, center)
        self._view_anim.stop()
        self._view_anim.setStartValue(0.0)
        self._view_anim.setEndValue(1.0)
        self._view_anim.start()

    def _on_view_step(self, value) -> None:
        t = float(value)
        (z0, c0), (z1, c1) = self._view_from, self._view_to
        zoom = math.exp(math.log(z0) + (math.log(z1) - math.log(z0)) * t)
        self.set_view(zoom, c0 + (c1 - c0) * t)

    def frame(self, items: Optional[list[ImageItem]] = None) -> None:
        """F: fit the selection (or everything) in view."""
        items = items if items is not None else (self.selected() or self.items_list())
        if not items:
            self.animate_view(1.0, QPointF(0, 0))
            return
        rect = QRectF()
        for item in items:
            rect = rect.united(item.sceneBoundingRect())
        viewport = self.viewport().rect()
        margin = 1.0 + 2 * FIT_MARGIN
        zoom = min(viewport.width() / (rect.width() * margin), viewport.height() / (rect.height() * margin))
        self.animate_view(zoom, rect.center())

    # ------------------ selection ------------------
    def selected(self) -> list[ImageItem]:
        """Selected images and notes."""
        return [i for i in self._scene.selectedItems() if isinstance(i, BOARD_ITEMS)]

    def selected_images(self) -> list[ImageItem]:
        return [i for i in self._scene.selectedItems() if isinstance(i, ImageItem)]

    def _on_selection_changed(self) -> None:
        self.viewport().update()

    def _image_at(self, pos: QPointF) -> Optional[ImageItem]:
        """The image or note under a point of the view."""
        for item in self.items(pos.toPoint()):
            if isinstance(item, BOARD_ITEMS):
                return item
        return None

    def _snapshot(self, items: list[ImageItem]) -> dict[str, ItemState]:
        return {item.item_id: item.state() for item in items}

    def _push_states(self, before: dict[str, ItemState], text: str) -> None:
        after = {item_id: self._items[item_id].state() for item_id in before if item_id in self._items}
        if after != before:
            self.undo_stack.push(_StateCommand(self, before, after, text))

    def _change_selection(
        self, text: str, change: Callable[[ImageItem, ItemState], ItemState], items: Optional[list[ImageItem]] = None,
    ) -> None:
        items = self.selected() if items is None else items
        if not items:
            return
        before = self._snapshot(items)
        after = {item.item_id: change(item, before[item.item_id]) for item in items}
        if after != before:
            self.undo_stack.push(_StateCommand(self, before, after, text))

    @staticmethod
    def _centered(item: ImageItem, state: ItemState) -> ItemState:
        """state, moved so the middle of what shows stays where it is on the board now."""
        middle = item.crop_rect().center()
        now = item.mapToScene(middle)
        shape = (
            QTransform.fromScale(-1.0 if state.flip_h else 1.0, -1.0 if state.flip_v else 1.0)
            * QTransform().scale(state.scale, state.scale)
            * QTransform().rotate(state.rotation)
        )
        position = now - shape.map(middle)
        return replace(state, x=position.x(), y=position.y())

    def flip(self, horizontal: bool) -> None:
        self._change_selection(
            "flip",
            lambda item, s: self._centered(item, replace(s, flip_h=not s.flip_h) if horizontal else replace(s, flip_v=not s.flip_v)),
            self.selected_images(),  # notes don't flip
        )

    def reset_transform(self) -> None:
        """R: rotation, size and flips back to how the image came in (crop kept)."""
        self._change_selection(
            "reset", lambda item, s: self._centered(item, replace(s, scale=1.0, rotation=0.0, flip_h=False, flip_v=False))
        )

    def reset_rotation(self) -> None:
        self._change_selection("reset rotation", lambda item, s: self._centered(item, replace(s, rotation=0.0)))

    def reset_size(self) -> None:
        self._change_selection("reset size", lambda item, s: self._centered(item, replace(s, scale=1.0)))

    def reset_flips(self) -> None:
        self._change_selection(
            "reset flips", lambda item, s: self._centered(item, replace(s, flip_h=False, flip_v=False)), self.selected_images()
        )

    def restack(self, to_front: bool) -> None:
        items = sorted(self.selected(), key=lambda i: i.zValue())
        others = [i.zValue() for i in self._items.values() if not i.isSelected()]
        if not items:
            return
        start = (max(others, default=0.0) + 1.0) if to_front else (min(others, default=0.0) - len(items))
        order = {item.item_id: start + n for n, item in enumerate(items)}
        self._change_selection("restack", lambda item, s: replace(s, z=order[item.item_id]))

    # ------------------ layout (ui.artbook_layout) ------------------
    def arrange_scope(self) -> list[ImageItem]:
        """What Auto-Arrange tidies: the selection if it has two or more images, else everything."""
        selected = self.selected()
        return selected if len(selected) >= 2 else self.items_list()

    def _move_by(self, moves: dict[str, QPointF], text: str) -> None:
        items = [self._items[item_id] for item_id in moves]
        before = self._snapshot(items)
        for item in items:
            item.setPos(item.pos() + moves[item.item_id])
        self._push_states(before, text)  # one undo step

    @staticmethod
    def _boxes(items: list[ImageItem]) -> list[tuple[str, QRectF]]:
        return [(item.item_id, item.sceneBoundingRect()) for item in items]

    def auto_arrange(self) -> None:
        """A: rows of about equal width, in reading order, sizes kept."""
        items = self.arrange_scope()
        if len(items) >= 2:
            self._move_by(artbook_layout.arrange(self._boxes(items)), "arrange")

    def align(self, edge: str) -> None:
        """Line the selection up on an edge: left, center, right, top, middle or bottom."""
        items = self.selected()
        if len(items) >= 2:
            self._move_by(artbook_layout.align(self._boxes(items), edge), "align")

    def distribute(self, horizontal: bool) -> None:
        """Equal gaps between the selected images (three or more)."""
        items = self.selected()
        if len(items) >= 3:
            self._move_by(artbook_layout.distribute(self._boxes(items), horizontal), "distribute")

    def same_size(self, height: bool) -> None:
        """Scale the selection to one height (or width): the middle one of their sizes."""
        items = self.selected()
        if len(items) < 2:
            return
        def side(item: ImageItem) -> float:  # what shows (a crop counts)
            rect = item.crop_rect()
            return rect.height() if height else rect.width()

        target = artbook_layout.same_size([(item.item_id, side(item) * item.scale()) for item in items])
        self._change_selection("same size", lambda item, s: replace(s, scale=target / side(item)))

    # ------------------ grayscale, opacity ------------------
    def toggle_gray(self) -> None:
        """G: grayscale on for the selection (off if it's all grayscale already)."""
        items = self.selected_images()
        gray = not all(item.gray for item in items)
        self._change_selection("grayscale", lambda _item, s: replace(s, gray=gray), items)

    def set_board_gray(self, gray: bool) -> None:
        """Ctrl+G: the whole board in grayscale, to judge values (a view setting: not an undo step)."""
        if gray != self._scene.gray_all:
            self._scene.gray_all = gray
            self.viewport().update()

    def toggle_board_gray(self) -> None:
        self.set_board_gray(not self._scene.gray_all)

    def set_opacity(self, percent: int, before: Optional[dict[str, ItemState]] = None) -> None:
        """Opacity of the selection. With before (the states when dragging started): one undo step."""
        opacity = max(MIN_OPACITY, min(100, int(percent))) / 100
        for item in self.selected():
            item.setOpacity(opacity)
        if before is not None:
            self._push_states(before, "opacity")

    def selection_opacity(self) -> int:
        items = self.selected()
        return round(100 * max(item.opacity() for item in items)) if items else 100

    # ------------------ notes ------------------
    def editing_note(self) -> Optional[NoteItem]:
        return self._editing["note"] if self._editing else None

    def add_note(self, at: Optional[QPointF] = None) -> NoteItem:
        """T: a new note at the cursor (or `at`, or the middle of the view), ready to type in."""
        self.finish_note_edit()
        if at is None:
            under = self.viewport().mapFromGlobal(QCursor.pos())
            at = self.mapToScene(under) if self.viewport().rect().contains(under) else self.view_center()
        note = NoteItem(uuid.uuid4().hex)
        note.setScale(min(8.0, max(0.05, 1.0 / self.zoom())))  # reads the same size on screen at any zoom
        box = note.boundingRect()
        note.setPos(at - QPointF(box.width(), box.height()) * note.scale() / 2)
        note.setZValue(max((i.zValue() for i in self._items.values()), default=0.0) + 1.0)
        self.attach(note)  # recorded for undo once it has text (see finish_note_edit)
        self._edit(note, new=True)
        return note

    def edit_note(self, note: NoteItem) -> None:
        """Double-click a note: type in it."""
        if self.editing_note() is not note:
            self.finish_note_edit()
            self._edit(note, new=False)

    def _edit(self, note: NoteItem, new: bool) -> None:
        self._editing = {"note": note, "new": new, "before": note.content()}
        self._scene.clearSelection()
        note.setSelected(True)
        self.setFocus()
        note.start_editing()
        self.viewport().update()

    def _on_note_edit_finished(self, note: NoteItem) -> None:
        if self.editing_note() is note:
            self.finish_note_edit()

    def finish_note_edit(self) -> None:
        """Esc or clicking elsewhere: keep the text (one undo step). An emptied note goes."""
        editing, self._editing = self._editing, None
        if editing is None:
            return
        note, before = editing["note"], editing["before"]
        note.stop_editing()
        text = note.toPlainText()
        if len(text) > MAX_NOTE_TEXT:
            note.setPlainText(text[:MAX_NOTE_TEXT])  # (drops its formatting: only absurdly long notes)
            text = note.toPlainText()
        if editing["new"]:
            if text.strip():
                self.undo_stack.push(_AddCommand(self, [note], "note"))
            else:
                self.detach(note)  # never typed in: as if it was never made
        elif not text.strip():
            note.set_content(before)  # deleting a note's text deletes the note (undo brings it back)
            self.undo_stack.push(_RemoveCommand(self, [note], "delete note"))
        elif note.content() != before:
            self.undo_stack.push(_TextCommand(note, before, note.content()))
        self.viewport().update()

    # ------------------ note formatting (the format bar) ------------------
    def note_bar_target(self) -> Optional[NoteItem]:
        """The note the format bar works on: the one being typed in, or the only selected item."""
        if self._editing:
            return self._editing["note"]
        items = self.selected()
        return items[0] if len(items) == 1 and isinstance(items[0], NoteItem) else None

    def _format(self, change: Callable[[QTextCursor], None], text: str) -> None:
        """Apply change to the selected text while typing, otherwise to the whole note (one undo step)."""
        note = self.note_bar_target()
        if note is None:
            return
        before = note.content()
        typing = note.editing()
        cursor = note.textCursor() if typing else QTextCursor(note.document())
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.Document)
        change(cursor)
        if typing and note.textCursor().hasSelection():
            note.setTextCursor(cursor)  # keep the selection showing
        if not typing and note.content() != before:
            self.undo_stack.push(_TextCommand(note, before, note.content(), text, self._live_merge))  # typing: recorded when done
        self._sync_note_bar()

    def live_note_change(self, on: bool) -> None:
        """A custom colour is being picked (changing live): its changes add up to one undo step."""
        if on:
            self._live_merges += 1
        self._live_merge = self._live_merges if on else None

    def _char_format(self, fmt: QTextCharFormat, text: str) -> None:
        self._format(lambda cursor: cursor.mergeCharFormat(fmt), text)

    def note_formats(self) -> dict[str, Any]:
        """The target note's formatting where the text cursor is (or at its start): for the bar."""
        note = self.note_bar_target()
        cursor = note.textCursor() if note.editing() else QTextCursor(note.document())
        if not note.editing():
            cursor.movePosition(QTextCursor.MoveOperation.NextCharacter)  # the first character's format
        fmt = cursor.charFormat()
        font = fmt.font()
        color = fmt.foreground().color() if fmt.hasProperty(QTextCharFormat.Property.ForegroundBrush) else note.defaultTextColor()
        highlight = fmt.background().color() if fmt.hasProperty(QTextCharFormat.Property.BackgroundBrush) else None
        return {
            "bold": fmt.fontWeight() >= QFont.Weight.DemiBold.value,
            "italic": fmt.fontItalic(),
            "underline": fmt.fontUnderline(),
            "size": fmt.fontPointSize() or note.font().pointSizeF(),
            "family": (fmt.fontFamilies() or [note.font().family()])[0] if fmt.hasProperty(QTextCharFormat.Property.FontFamilies) else note.font().family(),
            "color": color,
            "highlight": highlight,
            "fill": QColor(note.fill) if note.fill else theme_color("surface"),
            "align": cursor.blockFormat().alignment() & Qt.AlignmentFlag.AlignHorizontal_Mask,
        }

    def toggle_note_bold(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Normal if self.note_formats()["bold"] else QFont.Weight.Bold)
        self._char_format(fmt, "bold")

    def toggle_note_italic(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontItalic(not self.note_formats()["italic"])
        self._char_format(fmt, "italic")

    def toggle_note_underline(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontUnderline(not self.note_formats()["underline"])
        self._char_format(fmt, "underline")

    def set_note_text_color(self, color: Optional[str]) -> None:
        def change(cursor: QTextCursor) -> None:
            if color:
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(color))
                cursor.mergeCharFormat(fmt)
            else:  # the theme's: take away the text's own colour
                self._clear_char_property(cursor, QTextCharFormat.Property.ForegroundBrush)

        self._format(change, "text colour")

    def set_note_highlight(self, color: Optional[str]) -> None:
        def change(cursor: QTextCursor) -> None:
            if color:
                fmt = QTextCharFormat()
                fmt.setBackground(QColor(color))
                cursor.mergeCharFormat(fmt)
            else:
                self._clear_char_property(cursor, QTextCharFormat.Property.BackgroundBrush)

        self._format(change, "highlight")

    @staticmethod
    def _clear_char_property(cursor: QTextCursor, prop: QTextCharFormat.Property) -> None:
        """Remove one property from every piece of the selected text (keeping the rest of its formatting)."""
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        walker = QTextCursor(cursor.document())
        walker.setPosition(start)
        while walker.position() < end:
            walker.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
            fmt = walker.charFormat()
            fmt.clearProperty(prop)
            walker.setCharFormat(fmt)
            walker.clearSelection()

    def set_note_font_size(self, size: int) -> None:
        fmt = QTextCharFormat()
        fmt.setFontPointSize(float(size))
        self._char_format(fmt, "text size")

    def set_note_font_family(self, family: str) -> None:
        fmt = QTextCharFormat()
        fmt.setFontFamilies([family])
        self._char_format(fmt, "font")

    def set_note_alignment(self, alignment: Qt.AlignmentFlag) -> None:
        def change(cursor: QTextCursor) -> None:
            block = QTextBlockFormat()
            block.setAlignment(alignment)
            cursor.mergeBlockFormat(block)

        self._format(change, "alignment")

    def set_note_fill(self, color: Optional[str]) -> None:
        """The note's colour (None: the theme's)."""
        note = self.note_bar_target()
        if note is None or note.fill == color:
            return
        before = note.content()
        note.fill = color
        note.update()
        if not note.editing():
            self.undo_stack.push(_TextCommand(note, before, note.content(), "note colour", self._live_merge))
        self._sync_note_bar()

    def clear_note_format(self) -> None:
        """Plain text in the theme's colours (alignment and all formatting gone)."""
        note = self.note_bar_target()
        if note is None:
            return
        before = note.content()
        note.setPlainText(note.toPlainText())
        note.fill = None
        note.update()
        if not note.editing() and note.content() != before:
            self.undo_stack.push(_TextCommand(note, before, note.content(), "clear formatting"))
        self._sync_note_bar()

    @property
    def note_bar(self) -> NoteBar:
        if self._note_bar is None:
            self._note_bar = NoteBar(self)
        return self._note_bar

    def _sync_note_bar(self) -> None:
        if self.note_bar_target() is not None:
            self.note_bar.sync(self.note_formats())

    def _queue_note_bar(self) -> None:
        """Re-place the format bar after this paint (it follows moves, zooms and undo that way)."""
        if not self._note_bar_queued:
            self._note_bar_queued = True
            QTimer.singleShot(0, self._place_note_bar)

    def _place_note_bar(self) -> None:
        self._note_bar_queued = False
        note = self.note_bar_target()
        if note is None or self._crop or self._picking or note.scene() is not self._scene:
            if self._note_bar is not None:
                self._note_bar.hide()
                self._note_bar.close_pickers()  # (nothing left for an open colour picker to colour)
            return
        box = self.mapFromScene(note.sceneBoundingRect()).boundingRect()
        bar = self.note_bar
        bar.resize(bar.sizeHint())
        x = max(4, min(box.center().x() - bar.width() // 2, self.width() - bar.width() - 4))
        y = box.top() - bar.height() - NOTE_BAR_GAP
        if y < 4:
            y = min(box.bottom() + 12, self.height() - bar.height() - 4)  # no room above: below it
        if not bar.isVisible() or bar.property("note") != note.item_id:
            bar.setProperty("note", note.item_id)
            self.note_bar.sync(self.note_formats())
        bar.move(x, y)
        bar.show()
        bar.raise_()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            for item in self._items.values():
                if isinstance(item, NoteItem):
                    item.apply_theme()  # notes in the theme's colours follow it
            self._sync_note_bar()  # and the bar's colour swatches show the new theme's

    # ------------------ colour picker ------------------
    def picking(self) -> bool:
        return self._picking

    def start_picker(self) -> None:
        """I: the colour picker. Each click copies the colour under the cursor as a hex code."""
        self.cancel_crop()
        self.finish_note_edit()
        self._picking = True
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self.viewport().update()  # no selection frame while picking: it would be picked too
        self.show_message("Colour picker: click to copy a colour. Esc when done")
        self._show_readout(self.viewport().mapFromGlobal(QCursor.pos()))

    def stop_picker(self) -> None:
        if not self._picking:
            return
        self._picking = False
        self._readout.hide()
        self.viewport().unsetCursor()
        self.viewport().update()

    def color_at(self, pos: QPoint) -> QColor:
        """The colour on screen at a point of the view (what you see: theme, opacity, grayscale and all)."""
        return self.viewport().grab(QRect(pos, QSize(1, 1))).toImage().pixelColor(0, 0)

    def pick_color(self, pos: QPoint) -> str:
        code = self.color_at(pos).name().upper()
        QApplication.clipboard().setText(code)
        self.show_message(f"Copied {code}")
        return code

    def _show_readout(self, pos: QPoint) -> None:
        if not self.viewport().rect().contains(pos):
            self._readout.hide()
            return
        self._readout.set_color(self.color_at(pos))
        spot = self.viewport().mapTo(self, pos) + QPoint(18, 18)
        spot.setX(min(spot.x(), self.width() - self._readout.width() - 4))
        spot.setY(min(spot.y(), self.height() - self._readout.height() - 4))
        self._readout.move(spot)
        self._readout.show()
        self._readout.raise_()

    # ------------------ crop ------------------
    def crop_item(self) -> Optional[ImageItem]:
        return self._crop["item"] if self._crop else None

    def start_crop(self) -> bool:
        """C: crop mode for the one selected image (False if not exactly one is selected)."""
        items = self.selected()
        if len(items) != 1 or not isinstance(items[0], ImageItem):
            return False
        if self._crop:
            self.apply_crop()
        item = items[0]
        self._crop = {"item": item, "before": item.state(), "rect": item.crop_rect()}
        item.set_cropping(True)
        self.viewport().update()
        return True

    def apply_crop(self) -> None:
        """Enter (or C again, or clicking elsewhere): keep the crop, as one undo step."""
        crop, self._crop = self._crop, None
        if crop is None:
            return
        item, before = crop["item"], crop["before"]
        item.set_cropping(False)
        after = replace(before, crop=item.crop_from_rect(crop["rect"]))
        item.apply_state(before)
        if after != before:
            self.undo_stack.push(_StateCommand(self, {item.item_id: before}, {item.item_id: after}, "crop"))
        self.viewport().update()

    def cancel_crop(self) -> None:
        """Esc in crop mode: leave the image as it was."""
        crop, self._crop = self._crop, None
        if crop is None:
            return
        crop["item"].set_cropping(False)
        crop["item"].apply_state(crop["before"])
        self.viewport().update()

    def reset_crop(self) -> None:
        self.cancel_crop()
        self._change_selection("reset crop", lambda _item, s: replace(s, crop=None), self.selected_images())

    def _crop_handles(self) -> list[QPointF]:
        """Scene positions of the crop handles: corners (TL, TR, BR, BL), then edges (T, R, B, L)."""
        item, rect = self._crop["item"], self._crop["rect"]
        center = rect.center()
        local = [
            rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft(),
            QPointF(center.x(), rect.top()), QPointF(rect.right(), center.y()),
            QPointF(center.x(), rect.bottom()), QPointF(rect.left(), center.y()),
        ]
        return [item.mapToScene(point) for point in local]

    def _crop_handle_at(self, pos: QPointF) -> Optional[int]:
        for index, handle in enumerate(self._crop_handles()):
            point = self.mapFromScene(handle)
            if abs(point.x() - pos.x()) <= HANDLE_PX and abs(point.y() - pos.y()) <= HANDLE_PX:
                return index
        return None

    def _drag_crop(self, index: int, scene_point: QPointF) -> None:
        item = self._crop["item"]
        rect = QRectF(self._crop["rect"])
        local = item.mapFromScene(scene_point)
        full = item.full_rect()
        x = max(full.left(), min(full.right(), local.x()))
        y = max(full.top(), min(full.bottom(), local.y()))
        least = MIN_CROP_PX
        # Which edges this handle moves: corners two, edge handles one
        moves_left, moves_right = index in (0, 3, 7), index in (1, 2, 5)
        moves_top, moves_bottom = index in (0, 1, 4), index in (2, 3, 6)
        if moves_left:
            rect.setLeft(min(x, rect.right() - least))
        if moves_right:
            rect.setRight(max(x, rect.left() + least))
        if moves_top:
            rect.setTop(min(y, rect.bottom() - least))
        if moves_bottom:
            rect.setBottom(max(y, rect.top() + least))
        self._crop["rect"] = rect
        item.set_crop(item.crop_from_rect(rect))
        self.viewport().update()

    def delete_selection(self) -> None:
        items = self.selected()
        if items:
            self.undo_stack.push(_RemoveCommand(self, items, "delete"))

    def select_all(self) -> None:
        for item in self._items.values():
            item.setSelected(True)

    def copy_selection(self) -> bool:
        items = sorted(self.selected(), key=lambda i: i.zValue())
        if items and isinstance(items[-1], NoteItem):
            QApplication.clipboard().setText(items[-1].toPlainText())
            return True
        image = items[-1].image() if items else None
        if image is None:
            return False
        QApplication.clipboard().setImage(image)
        return True

    # ------------------ the selection frame ------------------
    def _frame_corners(self) -> Optional[list[QPointF]]:
        """Scene corners of the selection's frame: the item's own (rotated) outline for one image."""
        items = self.selected()
        if not items:
            return None
        if len(items) == 1:
            return list(items[0].mapToScene(items[0].boundingRect()))[:4]
        rect = QRectF()
        for item in items:
            rect = rect.united(item.sceneBoundingRect())
        return [rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()]

    @staticmethod
    def _frame_handles(corners: list[QPointF]) -> list[QPointF]:
        """The 8 scale handles: corners (TL, TR, BR, BL), then the middle of each side (T, R, B, L)."""
        return corners + [(corners[i] + corners[(i + 1) % 4]) / 2.0 for i in range(4)]

    @staticmethod
    def _opposite(index: int) -> int:
        return (index + 2) % 4 if index < 4 else 4 + (index - 2) % 4

    def _rotate_handle(self, corners: list[QPointF]) -> QPointF:
        top_mid = (corners[0] + corners[1]) / 2.0
        bottom_mid = (corners[3] + corners[2]) / 2.0
        up = top_mid - bottom_mid
        length = math.hypot(up.x(), up.y()) or 1.0
        return top_mid + up / length * (ROTATE_HANDLE_PX / self.zoom())

    def _handle_at(self, pos: QPointF) -> Optional[tuple[str, int]]:
        corners = self._frame_corners()
        if corners is None or self._crop or self._editing:
            return None
        if len(self.selected()) == 1:
            handle = self.mapFromScene(self._rotate_handle(corners))
            if math.hypot(handle.x() - pos.x(), handle.y() - pos.y()) <= HANDLE_PX:
                return ("rotate", -1)
        for index, handle in enumerate(self._frame_handles(corners)):
            point = self.mapFromScene(handle)
            if abs(point.x() - pos.x()) <= HANDLE_PX and abs(point.y() - pos.y()) <= HANDLE_PX:
                return ("scale", index)
        return None

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        self._queue_note_bar()
        if self._picking:
            return
        if self._crop:
            self._draw_crop_frame(painter)
            return
        corners = self._frame_corners()
        if corners is None:
            return
        accent = theme_color("accent_border")
        pen = QPen(accent, 1.5)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for item in self.selected():
            painter.drawPolygon(item.mapToScene(item.boundingRect()))
        if self._editing:
            return  # typing in a note: just its outline (the handles don't work meanwhile)
        painter.drawPolygon(QPolygonF(corners))
        size = HANDLE_PX / self.zoom()
        painter.setBrush(QBrush(QColor("#ffffff")))
        for handle in self._frame_handles(corners):
            painter.drawRect(QRectF(handle.x() - size / 2, handle.y() - size / 2, size, size))
        if len(self.selected()) == 1:
            handle = self._rotate_handle(corners)
            top_mid = (corners[0] + corners[1]) / 2.0
            painter.drawLine(top_mid, handle)
            painter.drawEllipse(handle, size * 0.6, size * 0.6)

    def _draw_crop_frame(self, painter: QPainter) -> None:
        item, crop_rect = self._crop["item"], self._crop["rect"]
        pen = QPen(theme_color("accent_border"), 1.5)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(item.mapToScene(crop_rect))
        dashed = QPen(QColor(255, 255, 255, 120), 1.0, Qt.PenStyle.DashLine)
        dashed.setCosmetic(True)
        painter.setPen(dashed)
        painter.drawPolygon(item.mapToScene(item.full_rect()))  # the whole image: how far it can go back out
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#ffffff")))
        size = HANDLE_PX / self.zoom()
        for handle in self._crop_handles():
            painter.drawRect(QRectF(handle.x() - size / 2, handle.y() - size / 2, size, size))

    # ------------------ mouse ------------------
    def mousePressEvent(self, event) -> None:
        self.setFocus()
        pos = event.position()
        button = event.button()
        if self._picking:
            if button == Qt.MouseButton.LeftButton:
                self.pick_color(pos.toPoint())
            else:
                self.stop_picker()  # right (or middle) click: done
            event.accept()
            return
        if self._editing:
            if self._image_at(pos) is self._editing["note"] and button == Qt.MouseButton.LeftButton:
                super().mousePressEvent(event)  # placing the text cursor, selecting text
                return
            self.finish_note_edit()
        if button == Qt.MouseButton.MiddleButton or (button == Qt.MouseButton.LeftButton and self._space_down):
            self._drag = {"mode": "pan", "last": pos}
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if button == Qt.MouseButton.LeftButton and self._crop:
            index = self._crop_handle_at(pos)
            if index is not None:
                self._drag = {"mode": "crop", "index": index}
                event.accept()
                return
            if self._image_at(pos) is self._crop["item"]:
                event.accept()  # inside the image being cropped: nothing to do (drag a handle)
                return
            self.apply_crop()  # clicking elsewhere keeps the crop, then does what it normally does
        if button == Qt.MouseButton.LeftButton:
            handle = self._handle_at(pos)
            if handle is not None:
                self._begin_transform(handle, pos)
                event.accept()
                return
            item = self._image_at(pos)
            modifiers = event.modifiers()
            if item is not None and modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
                item.setSelected(not item.isSelected())
                event.accept()
                return
            super().mousePressEvent(event)
            self._drag = {"mode": "move", "before": self._snapshot(self.selected())} if item is not None else None
            return
        super().mousePressEvent(event)

    def _begin_transform(self, handle: tuple[str, int], pos: QPointF) -> None:
        kind, index = handle
        items = self.selected()
        corners = self._frame_corners()
        drag = {"mode": kind, "before": self._snapshot(items), "items": items, "press": self.mapToScene(QPointF(pos).toPoint())}
        if kind == "scale" and index in (5, 7) and len(items) == 1 and isinstance(items[0], NoteItem):
            # A note's side handle: wider or narrower (the text re-wraps), the other side staying put
            note = items[0]
            drag.update(mode="note_width", note=note, side=index, width=note.textWidth(), start=note.sceneTransform())
        elif kind == "scale":
            handles = self._frame_handles(corners)
            drag["fixed"] = handles[self._opposite(index)]  # corners and sides: the opposite one stays put
            drag["grab"] = handles[index]
        else:
            # Around the middle of what shows (a cropped image doesn't swing round its hidden part)
            drag["center"] = items[0].mapToScene(items[0].crop_rect().center())
        self._drag = drag

    def mouseMoveEvent(self, event) -> None:
        drag = self._drag
        pos = event.position()
        if self._picking:
            self._show_readout(pos.toPoint())
            return
        if drag and drag["mode"] == "pan":
            delta = pos - drag["last"]
            drag["last"] = pos
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(delta.y()))
            return
        if drag and drag["mode"] == "scale":
            self._drag_scale(drag, self.mapToScene(pos.toPoint()))
            return
        if drag and drag["mode"] == "crop":
            if self._crop:
                self._drag_crop(drag["index"], self.mapToScene(pos.toPoint()))
            return
        if drag and drag["mode"] == "note_width":
            self._drag_note_width(drag, self.mapToScene(pos.toPoint()))
            return
        if drag and drag["mode"] == "rotate":
            snap = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._drag_rotate(drag, self.mapToScene(pos.toPoint()), snap)
            return
        super().mouseMoveEvent(event)
        if not event.buttons():
            self._update_hover_cursor(pos)  # after Qt's handling, which resets the cursor

    def _drag_scale(self, drag: dict[str, Any], point: QPointF) -> None:
        fixed, grab = drag["fixed"], drag["grab"]
        diagonal = grab - fixed
        length = diagonal.x() ** 2 + diagonal.y() ** 2
        if length <= 0:
            return
        moved = point - fixed
        factor = (moved.x() * diagonal.x() + moved.y() * diagonal.y()) / length
        smallest = min(state.scale for state in drag["before"].values())
        factor = max(factor, 0.01 / smallest)
        states = {}
        for item_id, state in drag["before"].items():
            center = fixed + (QPointF(state.x, state.y) - fixed) * factor
            states[item_id] = replace(state, x=center.x(), y=center.y(), scale=min(100.0, state.scale * factor))
        self.apply_states(states)

    def _drag_note_width(self, drag: dict[str, Any], point: QPointF) -> None:
        note, start_width = drag["note"], drag["width"]
        local = drag["start"].inverted()[0].map(point)  # in the note's own coordinates when the drag began
        if drag["side"] == 5:  # right side
            note.setTextWidth(max(MIN_NOTE_WIDTH, local.x()))
        else:  # left side: the right edge stays where it was
            width = max(MIN_NOTE_WIDTH, start_width - local.x())
            note.setTextWidth(width)
            note.setPos(drag["start"].map(QPointF(start_width - width, 0)))
        self.viewport().update()

    def _drag_rotate(self, drag: dict[str, Any], point: QPointF, snap: bool) -> None:
        center, press = drag["center"], drag["press"]
        start = math.degrees(math.atan2(press.y() - center.y(), press.x() - center.x()))
        now = math.degrees(math.atan2(point.y() - center.y(), point.x() - center.x()))
        (item_id, state), = drag["before"].items()
        angle = state.rotation + now - start
        if snap:
            angle = round(angle / 15.0) * 15.0
        # Turning by `turn` around center also moves the item's own origin around it
        turn = math.radians(angle - state.rotation)
        offset = QPointF(state.x, state.y) - center
        moved = QPointF(
            offset.x() * math.cos(turn) - offset.y() * math.sin(turn),
            offset.x() * math.sin(turn) + offset.y() * math.cos(turn),
        )
        position = center + moved
        self.apply_states({item_id: replace(state, rotation=angle % 360.0, x=position.x(), y=position.y())})

    def hover_cursor(self, pos: QPointF) -> QCursor:
        """What the cursor shows over pos: resize on corners (pointing the way that corner
        scales, rotation included), rotate on the round handle, move over images."""
        if self._space_down:
            return QCursor(Qt.CursorShape.OpenHandCursor)
        if self._crop:
            index = self._crop_handle_at(pos)
            if index is None:
                return QCursor(Qt.CursorShape.ArrowCursor)
            center = self.mapFromScene(self._crop["item"].mapToScene(self._crop["rect"].center()))
            handle_point = self.mapFromScene(self._crop_handles()[index])
            angle = math.degrees(math.atan2(handle_point.y() - center.y(), handle_point.x() - center.x())) % 180.0
            return QCursor(_RESIZE_CURSORS[int(((angle + 22.5) % 180.0) // 45.0)])
        handle = self._handle_at(pos)
        if handle is not None and handle[0] == "rotate":
            return rotate_cursor()
        if handle is not None:
            handles = [self.mapFromScene(h) for h in self._frame_handles(self._frame_corners())]
            corner, opposite = handles[handle[1]], handles[self._opposite(handle[1])]
            angle = math.degrees(math.atan2(corner.y() - opposite.y(), corner.x() - opposite.x())) % 180.0
            return QCursor(_RESIZE_CURSORS[int(((angle + 22.5) % 180.0) // 45.0)])
        if self._image_at(pos) is not None:
            return QCursor(Qt.CursorShape.SizeAllCursor)
        return QCursor(Qt.CursorShape.ArrowCursor)

    def _update_hover_cursor(self, pos: QPointF) -> None:
        self.viewport().setCursor(self.hover_cursor(pos))

    def mouseReleaseEvent(self, event) -> None:
        drag, self._drag = self._drag, None
        if drag and drag["mode"] == "pan":
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self._space_down else Qt.CursorShape.ArrowCursor)
            return
        if drag and drag["mode"] in ("scale", "rotate", "note_width"):
            self._push_states(drag["before"], "note width" if drag["mode"] == "note_width" else drag["mode"])
            return
        if drag and drag["mode"] == "crop":
            return  # applied with Enter (or C, or clicking elsewhere)
        super().mouseReleaseEvent(event)
        if drag and drag["mode"] == "move":
            self._push_states(drag["before"], "move")

    def mouseDoubleClickEvent(self, event) -> None:
        if self._picking:
            event.accept()
            return
        item = self._image_at(event.position())
        if isinstance(item, NoteItem) and event.button() == Qt.MouseButton.LeftButton:
            if self.editing_note() is item:
                super().mouseDoubleClickEvent(event)  # selects a word
            else:
                self.edit_note(item)
            return
        if item is not None and event.button() == Qt.MouseButton.LeftButton:
            self.frame([item])
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        if self._picking:
            event.accept()  # the right click that stops the picker doesn't also open the menu
            return
        menu = self.context_menu(self.mapToScene(event.pos()))
        menu.exec(event.globalPos())
        menu.deleteLater()  # its actions carry shortcuts: don't leave them around

    def context_menu(self, at: QPointF) -> QMenu:
        """The right-click menu: a short top level, image tools grouped under Images, board-wide ones
        under Canvas. What can't be done right now (e.g. nothing selected) is greyed out; submenus
        always open, so you can see what's in them."""
        menu = QMenu(self)
        selected = len(self.selected())
        has_selection = bool(selected)
        items = self.selected()
        images = self.selected_images()
        states = [item.state() for item in items]
        reset = [
            ("All", "artbook.reset", self.reset_transform, has_selection),
            None,
            ("Rotation", None, self.reset_rotation, any(s.rotation % 360 for s in states)),
            ("Size", None, self.reset_size, any(s.scale != 1.0 for s in states)),
            ("Flips", None, self.reset_flips, any(item.state().flip_h or item.state().flip_v for item in images)),
            ("Crop", None, self.reset_crop, any(item.crop for item in images)),
        ]
        selection = [
            ("Crop", "artbook.crop", self.start_crop, selected == 1 and len(images) == 1),
            ("Grayscale", "artbook.gray", self.toggle_gray, bool(images), bool(images) and all(item.gray for item in images)),
            ("Opacity", [self._opacity_action(menu, has_selection)]),
            None,
            ("Flip Horizontally", "artbook.flip_h", lambda: self.flip(True), bool(images)),
            ("Flip Vertically", "artbook.flip_v", lambda: self.flip(False), bool(images)),
            ("Reset", reset),
            None,
            ("Bring to Front", "artbook.front", lambda: self.restack(True), has_selection),
            ("Send to Back", "artbook.back", lambda: self.restack(False), has_selection),
            None,
            ("Delete", "artbook.delete", self.delete_selection, has_selection),
        ]
        arrange = [
            ("Auto-Arrange", "artbook.arrange", self.auto_arrange, len(self.arrange_scope()) >= 2),
        ]
        align = [
            ("Left", None, lambda: self.align("left"), selected >= 2),
            ("Centers", None, lambda: self.align("center"), selected >= 2),
            ("Right", None, lambda: self.align("right"), selected >= 2),
            ("Top", None, lambda: self.align("top"), selected >= 2),
            ("Middles", None, lambda: self.align("middle"), selected >= 2),
            ("Bottom", None, lambda: self.align("bottom"), selected >= 2),
            None,
            ("Distribute Horizontally", None, lambda: self.distribute(True), selected >= 3),
            ("Distribute Vertically", None, lambda: self.distribute(False), selected >= 3),
        ]
        match_size = [
            ("Same Height", None, lambda: self.same_size(True), selected >= 2),
            ("Same Width", None, lambda: self.same_size(False), selected >= 2),
        ]
        canvas = [
            ("Frame Selection" if has_selection else "Frame Everything", "artbook.fit", self.frame, bool(self._items)),
            ("Zoom to 100%", "artbook.zoom_100", lambda: self.animate_view(1.0, self.view_center()), True),
            ("Grayscale Board", "artbook.gray_board", self.toggle_board_gray, True, self._scene.gray_all),
            None,
            ("Export Selection as PNG..." if has_selection else "Export as PNG...", "artbook.export_png", self.request_export_image, bool(self._items)),
            ("Export Page (.artbook)...", None, self.export_page_requested.emit, True),
        ]
        images_menu = [
            ("Select All", "artbook.select_all", self.select_all, bool(self._items)),
            ("Selection", selection),
            ("Arrange", arrange),
            ("Align", align),
            ("Match Size", match_size),
        ]
        entries = [
            ("Undo", "artbook.undo", self.undo_stack.undo, self.undo_stack.canUndo()),
            ("Redo", "artbook.redo", self.undo_stack.redo, self.undo_stack.canRedo()),
            None,
            ("Copy Image", "artbook.copy", self.copy_selection, has_selection),
            ("Paste", "artbook.paste", lambda: self.paste(at), self.can_paste()),
            ("Note", "artbook.note", lambda: self.add_note(at), True),
            ("Colour Picker", "artbook.picker", self.start_picker, True),
            None,
            ("Canvas", canvas),
            ("Images", images_menu),
        ]
        self._fill_menu(menu, entries)
        if self.window_menu is not None:
            menu.addSeparator()
            menu.addMenu(self.window_menu(menu))
        return menu

    def _opacity_action(self, parent: QMenu, enabled: bool) -> QWidgetAction:
        """A slider for the selection's opacity: live while dragging, one undo step when let go."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(36, 4, 14, 4)  # lined up with the items' text (after the tick column)
        layout.setSpacing(8)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(MIN_OPACITY, 100)
        slider.setMinimumWidth(140)
        slider.setValue(self.selection_opacity())
        slider.setToolTip("How see-through the selected images are")
        label = QLabel(f"{slider.value()}%")
        label.setMinimumWidth(36)
        layout.addWidget(slider, 1)
        layout.addWidget(label)
        row.setEnabled(enabled)
        started: dict[str, Any] = {}

        def changed(value: int) -> None:
            label.setText(f"{value}%")
            if "before" not in started:
                started["before"] = self._snapshot(self.selected())
            if slider.isSliderDown():
                self.set_opacity(value)  # live; the undo step comes on release
            else:  # keys or a click on the groove: one step each
                self.set_opacity(value, started.pop("before"))

        def released() -> None:
            if "before" in started:
                self.set_opacity(slider.value(), started.pop("before"))

        slider.valueChanged.connect(changed)
        slider.sliderReleased.connect(released)
        action = QWidgetAction(parent)
        action.setDefaultWidget(row)
        return action

    def _fill_menu(self, menu: QMenu, entries: list) -> None:
        """entries: None (a separator), a QAction, (text, [entries]) (a submenu) or
        (text, shortcut id or None, handler, enabled[, checked])."""
        for entry in entries:
            if entry is None:
                menu.addSeparator()
            elif isinstance(entry, QAction):
                menu.addAction(entry)
            elif len(entry) == 2:
                text, sub_entries = entry
                self._fill_menu(menu.addMenu(text), sub_entries)  # always opens: greyed items show what's there
            else:
                text, shortcut_id, handler, enabled, *checked = entry
                action = menu.addAction(text)
                if checked:
                    action.setCheckable(True)
                    action.setChecked(checked[0])
                if shortcut_id is not None:
                    action.setShortcut(shortcuts.sequence(shortcut_id))  # shown as a hint; the board handles the key
                action.setEnabled(enabled)
                # Called with no arguments: triggered passes `checked`, which frame() would take as its items
                action.triggered.connect(lambda _checked=False, handler=handler: handler())

    # ------------------ keys ------------------
    def _key_id(self, event: QKeyEvent) -> Optional[str]:
        return next((key for key in self.KEYS if shortcuts.matches(key, event)), None)

    def event(self, event) -> bool:
        # Claim the board's keys before window shortcuts (e.g. the library's Ctrl+Z) see them;
        # while typing in a note, every key (Ctrl+Z undoes typing there)
        if event.type() == QEvent.Type.ShortcutOverride and (self._editing or self._key_id(event)):
            event.accept()
            return True
        return super().event(event)

    # Only while typing in a note (Ctrl+B is the Pages sidebar otherwise)
    TYPING_KEYS = {
        "artbook.note_bold": "toggle_note_bold",
        "artbook.note_italic": "toggle_note_italic",
        "artbook.note_underline": "toggle_note_underline",
    }

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = self._key_id(event)
        if self._editing:
            typing_key = next((k for k in self.TYPING_KEYS if shortcuts.matches(k, event)), None)
            if key == "artbook.deselect":
                self.finish_note_edit()  # Esc: done typing
            elif typing_key is not None:
                getattr(self, self.TYPING_KEYS[typing_key])()
            else:
                super().keyPressEvent(event)  # to the note
                self._sync_note_bar()
            event.accept()
            return
        if self._picking and key in ("artbook.deselect", "artbook.picker"):
            self.stop_picker()  # Esc, or I again
            event.accept()
            return
        if self._crop and key == "artbook.deselect":
            self.cancel_crop()  # Esc: leave the image as it was
            event.accept()
            return
        if self._crop and key in ("artbook.crop_apply", "artbook.crop"):
            self.apply_crop()  # Enter, or C again
            event.accept()
            return
        if self._crop and key not in (None, "artbook.pan_key", "artbook.fit", "artbook.zoom_100"):
            self.apply_crop()  # anything else (delete, undo, flip...) works on the finished crop
        if key == "artbook.pan_key":
            if not event.isAutoRepeat():
                self._space_down = True
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            return
        actions = {
            "artbook.paste": self.paste,
            "artbook.copy": self.copy_selection,
            "artbook.fit": self.frame,
            "artbook.zoom_100": lambda: self.animate_view(1.0, self.view_center()),
            "artbook.flip_h": lambda: self.flip(True),
            "artbook.flip_v": lambda: self.flip(False),
            "artbook.front": lambda: self.restack(True),
            "artbook.back": lambda: self.restack(False),
            "artbook.reset": self.reset_transform,
            "artbook.delete": self.delete_selection,
            "artbook.select_all": self.select_all,
            "artbook.deselect": self._scene.clearSelection,
            "artbook.undo": self.undo_stack.undo,
            "artbook.redo": self.undo_stack.redo,
            "artbook.arrange": self.auto_arrange,
            "artbook.crop": self.start_crop,
            "artbook.gray": self.toggle_gray,
            "artbook.note": self.add_note,
            "artbook.picker": self.start_picker,
            "artbook.gray_board": self.toggle_board_gray,
            "artbook.export_png": self.request_export_image,
        }
        if key in actions:
            actions[key]()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if self._key_id(event) == "artbook.pan_key" and not event.isAutoRepeat():
            self._space_down = False
            self.viewport().unsetCursor()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:
        self._space_down = False  # a released Space can't reach us any more
        super().focusOutEvent(event)
