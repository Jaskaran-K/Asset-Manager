"""The Artbook board: an endless canvas of reference images for one page.

BoardView (a QGraphicsView) holds ImageItems and handles everything you do with them:
getting images in (drop, paste, from the asset grid), moving, scaling, rotating, flipping,
stacking order, selecting, panning and zooming. Every change goes on the page's undo stack;
`changed` fires after each one so the panel can autosave.

Images are decoded off the UI thread (ui.tag_widgets.run_job), at the detail they're seen at
(level of detail): every image keeps a small copy (BASE_SIDE), and the ones in or near the view
get a sharper copy for the zoom they're seen at, up to the full image when zoomed in. Sharper
copies are let go once an image is well out of view, so a big board doesn't fill the memory.
Pictures load from the middle of the view outwards; until its picture is in, an image shows as a
placeholder.

Controls are registered in ui.shortcuts (the Artbook category). While the board has the
keyboard focus it takes its keys first, so Ctrl+Z undoes board changes, not library ones.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QBuffer, QByteArray, QEasingCurve, QEvent, QIODevice, QLineF, QMarginsF, QMimeData, QPoint, QPointF, QRect, QRectF, QSize, QSizeF, Qt, QTimer, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QFontMetricsF,
    QImage,
    QImageReader,
    QPageSize,
    QPdfWriter,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QPolygonF,
    QSyntaxHighlighter,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextDocumentFragment,
    QTextFormat,
    QTextListFormat,
    QTextOption,
    QTransform,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QSlider,
    QSpinBox,
    QStyle,
    QStyleOptionGraphicsItem,
    QToolButton,
    QWidget,
    QWidgetAction,
)

import numpy as np
from PIL import Image

from core import artbook_store as store
from core import app_icons, apps, artbook_share, artbook_unsorted, pur_import, video_tools, web_images
from core.web_images import fetch_image as _fetch_image, is_web_url as _is_web_url, web_image_sources
from core.settings import (
    get_artbook_card_style, get_artbook_custom_grid, get_artbook_nav_hints, get_artbook_value_tones, get_last_pictures_dir,
    set_artbook_card_style, set_artbook_custom_grid, set_artbook_nav_hints, set_artbook_value_tones, set_last_pictures_dir,
)
from ui import animations, artbook_layout, file_actions, shortcuts
from ui.artbook_note_bar import SWATCHES, NoteBar, swatch_icon
from ui.artbook_rail import TOOL_MIME, TOOLS, ToolRail
from ui.artbook_draw_bar import DrawBar
from ui.artbook_group_bar import GroupBar
from ui.artbook_image_bar import ImageBar
from ui.artbook_image_list import ImageListPanel
from ui.artbook_nav_hints import NavHints
from ui.artbook_card_bar import CardBar
from ui.artbook_comments import CommentThread
from ui.artbook_tray import UNSORTED_MIME, TrayPanel
from ui.artbook_line_bar import LineBar
from ui.artbook_table_bar import StatusDialog, TableBar
from ui.controls import DialogFrame
from ui.tag_widgets import run_job
from ui.themes import theme_color

ZOOM_RANGE = (0.02, 32.0)
EXPORT_MAX_SIDE = 16384  # pixels: an exported picture's longest side...
EXPORT_MAX_PIXELS = 150_000_000  # ...and its area (about 600 MB while it's made)
EXPORT_MARGIN = 0.02  # around what's exported, of its longest side
EXPORT_AREAS = ("auto", "page", "selection", "view")  # what's exported (auto: the selection, or everything)
EXPORT_SIZES = ("1", "2", "4", "full")  # pixels per board unit (full: as sharp as its sharpest picture)
EXPORT_BACKGROUNDS = ("board", "white", "clear")
PDF_MAX_POINTS = 14400.0  # a PDF page is at most 200 inches a side
GROUP_PADDING = 0.025  # a group's backdrop reaches this far past its members (of its longest side)
GROUP_DEFAULT_ALPHA = 0.10  # a group's backdrop in the theme's accent colour, this see-through
CAPTION_SIZE = 0.045  # a caption's text height, of its image's longest side
MAX_CAPTION = 300
MAX_NAME = 200
OPEN_FILES_LIMIT = 10  # Open Source File opens at most this many at once
RELINK_SCAN_LIMIT = 300_000  # files looked at when relinking (a whole drive would take ages)
STROKE_STEP_PX = 2.0  # a stroke gets a new point every this many pixels the pen moves
ERASER_PX = 6  # the eraser's reach around the pointer
SMOOTH_FOLLOW = 0.35  # smoothing on: the line moves this far toward the pointer at each step (steadies a mouse)
SMOOTH_ROUNDS = 2  # smoothing on: corner-rounding passes over a finished stroke
MIN_STROKE_WIDTH = 0.001
MAX_STROKE_WIDTH = 100000.0
MAX_STROKE_POINTS = 50000
DRAW_BAR_TOP = 12
WHEEL_ZOOM_STEP = 1.2  # per wheel notch
ZOOM_MS = 140
FIT_MS = 260
FIT_MARGIN = 0.08
MOVE_MS = 220  # images gliding to where a layout (or an undo) puts them
FADE_MS = 180  # a picture fading in over its placeholder when it arrives
AREA_MS = 260  # the active area following the board's items as they spread out or gather
AREA_PAD_ITEM = 0.15  # the active area reaches this far past the items (of a typical item's longest side)...
AREA_PAD_BOARD = 0.02  # ...or this far (of the area's own longest side), whichever is more
AREA_RADIUS = 0.6  # its corners' rounding, of its padding
DROP_GAP = 24.0  # between images dropped together
HANDLE_PX = 9.0  # on screen, whatever the zoom
MIN_CROP_PX = 8.0  # a crop keeps at least this much of the image (image pixels)
MIN_OPACITY = 10  # percent: fainter images are too easy to lose
CROP_OUTSIDE_OPACITY = 0.3  # crop mode: how the part being cut away shows
NOTE_WIDTH = 260.0  # a new note's width (its text wraps), before scaling
MIN_NOTE_WIDTH = 60.0
NOTE_FONT_PT = 14
NOTE_PADDING = 12.0
NOTE_STYLES = ("card", "heading", "plain", "title")  # a note's look: a card, a title (a card with an accent line), text on
# the board, or the page's header ("title": the page's name, a line about it, what's on the page; one per page)
CARD_RADIUS = 10.0  # the corners of a card (a note's; the coming cards' too)
HEADING_PT = 26  # a heading note's text size (bold)
TITLE_PT, TITLE_ABOUT_PT, TITLE_STATS_PT = 30, 13, 10  # the page header's name (bold), the line under it, what's on the page
TITLE_GAP = 6.0
FIND_MARK = "#ffcf3d"  # find on this page: the matched words are marked in this
STRIP_COLORS = (("Red", "#e5484d"), ("Orange", "#f76b15"), ("Yellow", "#ffc53d"), ("Green", "#46a758"), ("Teal", "#12a594"),
                ("Blue", "#3e63dd"), ("Purple", "#8e4ec6"), ("Pink", "#d6409f"), ("Grey", "#8b8d98"))  # a card's colour
STRIP_WIDTH = 6.0  # a card's colour: a strip down its left edge
PACKAGE_MIME = "application/x-artbook-items"  # copied board items (Ctrl+C), to paste on this page or another
SNAP_PX = 6.0  # dragging: within this (on screen), a card snaps into line with another, or to an equal gap
NUDGE_PX, NUDGE_BIG_PX = 1.0, 10.0  # arrow keys move the selection this far on screen (Shift: the bigger)
NUDGE_MERGE = 7  # (nudges in a row: one undo step)
MIN_TITLE_WIDTH, MAX_TITLE_WIDTH = 360.0, 1400.0  # (it fits its words, within these)
MAX_TITLE_ABOUT = 300
PARAGRAPH_PT = {"h1": 20, "h2": 16}  # headings inside a note (bold); text and lists: NOTE_FONT_PT
PARAGRAPHS = ("text", "h1", "h2", "bullet", "number", "check")
MARKDOWN = {"#": "h1", "##": "h2", "-": "bullet", "*": "bullet", "1.": "number", "[]": "check"}  # typed at a line's start, then Space
RULE_WIDTH, RULE_HEIGHT, RULE_GAP = 48.0, 4.0, 8.0  # a heading's accent line, under its text
LIST_INDENT = 30  # how far a list's lines are in (room for a checklist's box)
BOX, BOX_GAP, GRIP_WIDTH = 15.0, 7.0, 8.0  # a checklist line's box, the gap to its text, the dots to drag it by
BAND = 14.0  # room under a to-do's title for its progress bar
COUNT_ROOM = 64.0  # room at the title's right for "2 of 4"
BAR_HEIGHT = 4.0
DONE_FADE = 0.55  # how far a done line fades into its card
COLUMN_WIDTH = 300.0  # a new column's width (its cards take it), before scaling
MIN_COLUMN_WIDTH, MAX_COLUMN_WIDTH = 160.0, 4000.0
COLUMN_PAD = 10.0  # between a column's outline and its cards
COLUMN_GAP = 8.0  # between its cards
COLUMN_RADIUS = 12.0
COLUMN_TITLE_PT = 18
COLUMN_TITLE_MARGIN = 4.0
COLUMN_EMPTY = 72.0  # an empty column's room for cards ("Drag cards here")
CHEVRON = 22.0  # the fold arrow's room, left of the title
CHIP_HEIGHT, CHIP_PAD = 20.0, 8.0  # the count chip at the title's right
CLOSE_RADIUS = 10.0  # the x on a hovered column's top-right corner
LIFTED_OPACITY = 0.7  # a card dragged over a column fades this much
COLUMN_EDGE_ALPHA = 0.6  # its dashed outline, in the theme's faint text colour this strong
COLUMN_Z_STEP = 0.001  # a column's cards stack just above it, in order
MAX_COLUMN_TITLE = 200
LINE_SHAPES = ("curve", "straight", "elbow")
LINE_ENDS = ("none", "end", "both")
LINE_WIDTHS = (1, 2, 3, 5)  # (as the line bar offers them)
LINE_GAP = 4.0  # between a card and a line's end
POINT_RADIUS = 8.0  # a point on a line (double-click a line): its ring's radius
POINT_SPOT_STEPS = 120  # where on a line a click is: the line tried at this many places (then finer near the best)
LINE_HIT = 9.0  # a click this near a line picks it
LINE_LABEL_PT = 10
LINE_LABEL_PAD = 8.0  # the label's pill, past its text at either side
ELBOW_RADIUS = 10.0  # an elbow line's rounded corners
MAX_LINE_LABEL = 200
LINE_DOT_PX, LINE_DOT_OFFSET_PX = 5.0, 14.0  # a hovered card's dots: their size and how far out (on screen)
LINE_OUTER_DOT_PX = 32.0  # a column's or group's dots: further out (clear of the dots of the cards in it)
MIN_LINE_PX = 8.0  # a drag from a dot shorter than this (on screen, to nowhere) makes no line
MAX_COLUMN_CARDS = 5000
HIGHLIGHT_RADIUS = 4.0  # a note's highlights have rounded corners, like the note
MAX_NOTE_TEXT = 20000
NOTE_BAR_GAP = 38  # px between the format bar and the note (room for the rotate handle)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)  # a note never loads pictures from its HTML
ROTATE_HANDLE_PX = 26.0  # how far above the image the rotate handle sits
SCENE_EXTENT = 1_000_000.0
BASE_SIDE = 160  # every image keeps a copy this big (longest side): what shows from far away
DETAIL_STEP = 1.15  # a sharper copy loads once an image shows this much bigger than the one it has
DETAIL_MAX_BYTES = 256 * 1024 * 1024  # one image's sharp copy at most (about 8000 x 8000)
DETAIL_BUDGET = 1024 * 1024 * 1024  # all the sharp copies together; past it, the farthest make do
NEAR_VIEW = 0.5  # images this far outside the view (of its size) count as in view
DETAIL_LOADS = 3  # decodes running at the same time
SMALL_BATCH = 24  # small copies read in one go (they're quick: one at a time, waiting is most of it)
DETAIL_TICK_MS = 50
EXPORT_DETAIL_SIDE = 4096  # an export is as sharp as its sharpest image at this size (or its own)


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


KIND_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}")  # an item's kind (kept even when this version doesn't know it)


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
    group: Optional[str] = None  # the group it's in (selected, moved and arranged together)
    caption: str = ""  # images: a line of text under it
    pixelated: bool = False  # images: hard pixel edges when zoomed in (no smoothing)
    values: int = 0  # images: shown as this many flat greys (0: off; VALUE_TONES)
    guides: str = ""  # images: lines drawn over it (GUIDES, or a custom grid "grid:<columns>:<rows>")
    title: str = ""  # columns: the title
    members: tuple = ()  # columns: their cards' ids, top to bottom
    folded: bool = False  # columns: folded to the title
    line: Optional[tuple] = None  # lines: (start, end, bend, shape, heads, colour, width, dashed, label, size)
    card: Optional[tuple] = None  # link / file / page cards: (style, its data as sorted (key, value) pairs)
    table: Optional[tuple] = None  # tables: (header row shown, columns (name, kind, width, align), rows of cells, statuses)
    strip: Optional[str] = None  # notes, tables, columns, cards: their colour (a strip down a card's edge; a column's outline)


def _gray_image(image: QImage) -> QImage:
    """image in grayscale, keeping its transparency."""
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    if image.hasAlphaChannel():
        painter = QPainter(gray)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, image)
        painter.end()
    return gray


VALUE_TONES = (2, 3, 4, 5)  # Values: how many flat greys an image can be shown in
GUIDES = {  # Guides: what's drawn over an image (a custom grid is "grid:<columns>:<rows>")
    "thirds": "Thirds", "grid:4:4": "Grid 4x4", "grid:8:8": "Grid 8x8", "golden": "Golden Ratio",
    "center": "Centre and Diagonals",
}
MAX_GRID = 32
GOLDEN = 0.381966  # 1 - 1/phi: where golden-ratio lines cut a side, from either end


def _values_image(image: QImage, tones: int) -> QImage:
    """image as `tones` flat greys, black to white (brightness split into equal bands), keeping its
    transparency: the light and shadow shapes without the detail."""
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    w, h, line = gray.width(), gray.height(), gray.bytesPerLine()
    rows = np.frombuffer(gray.constBits(), np.uint8, gray.sizeInBytes()).reshape(h, line)[:, :w]
    bands = (rows.astype(np.uint16) * tones) >> 8  # 0 .. tones - 1
    levels = np.ascontiguousarray((bands * 255 // (tones - 1)).astype(np.uint8))
    flat = QImage(levels.data, w, h, w, QImage.Format.Format_Grayscale8).copy()
    flat = flat.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    if image.hasAlphaChannel():
        painter = QPainter(flat)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, image)
        painter.end()
    return flat


def _clean_values(value: Any) -> int:
    return value if isinstance(value, int) and value in VALUE_TONES else 0


def custom_grid(guides: str) -> Optional[tuple[int, int]]:
    """(columns, rows) of a grid guide ("grid:6:4"), or None if it isn't one."""
    parts = guides.split(":") if isinstance(guides, str) else []
    if len(parts) == 3 and parts[0] == "grid" and all(p.isdigit() for p in parts[1:]):
        columns, rows = int(parts[1]), int(parts[2])
        if 1 <= columns <= MAX_GRID and 1 <= rows <= MAX_GRID:
            return columns, rows
    return None


def _clean_guides(value: Any) -> str:
    return value if isinstance(value, str) and (value in GUIDES or custom_grid(value)) else ""


def guide_lines(guides: str, rect: QRectF) -> list[QLineF]:
    """The lines of a guide over rect (an image's shown part)."""
    left, top, w, h = rect.left(), rect.top(), rect.width(), rect.height()

    def across(shares) -> list[QLineF]:  # vertical lines at these shares of the width, then horizontal
        lines = [QLineF(left + w * s, top, left + w * s, top + h) for s in shares[0]]
        return lines + [QLineF(left, top + h * s, left + w, top + h * s) for s in shares[1]]

    grid = custom_grid(guides)
    if guides == "thirds":
        return across(((1 / 3, 2 / 3), (1 / 3, 2 / 3)))
    if grid is not None:
        columns, rows = grid
        return across(([c / columns for c in range(1, columns)], [r / rows for r in range(1, rows)]))
    if guides == "golden":
        return across(((GOLDEN, 1 - GOLDEN), (GOLDEN, 1 - GOLDEN)))
    if guides == "center":
        return across(((0.5,), (0.5,))) + [QLineF(rect.topLeft(), rect.bottomRight()), QLineF(rect.topRight(), rect.bottomLeft())]
    return []


class CustomGridDialog(QDialog):
    """Guides > Custom Grid...: how many columns and rows."""

    def __init__(self, start: tuple[int, int], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Custom Grid")
        self.setMinimumWidth(380)
        frame = DialogFrame(self, "Custom Grid", "Guides that divide the selected images into equal parts.")
        row = QHBoxLayout()
        self.spin_columns, self.spin_rows = QSpinBox(self), QSpinBox(self)
        for spin, value, what in ((self.spin_columns, start[0], "Columns"), (self.spin_rows, start[1], "Rows")):
            spin.setRange(1, MAX_GRID)
            spin.setValue(value)
            spin.setToolTip(f"{what}: how many the image is divided into (1 to {MAX_GRID})")
            row.addWidget(QLabel(what, self))
            row.addWidget(spin)
            row.addSpacing(12)
        row.addStretch(1)
        frame.body.addLayout(row)
        frame.add_footer("OK", "Draw this grid over the selected images", cancel_tip="Leave the guides as they are")

    def grid(self) -> tuple[int, int]:
        return self.spin_columns.value(), self.spin_rows.value()


def _clean_group(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and 0 < len(value) <= 64 else None


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


def _clean_original(value: Any) -> Optional[dict[str, Any]]:
    """An upscaled image's original picture as saved, or None if it isn't a usable one."""
    if not isinstance(value, dict) or not isinstance(value.get("source"), dict):
        return None
    source = {key: value["source"].get(key) for key in ("path", "blob")}
    if not all(v is None or isinstance(v, str) for v in source.values()) or not any(source.values()):
        return None
    size = value.get("size")
    if not (isinstance(size, (list, tuple)) and len(size) == 2 and all(isinstance(v, int) and v > 0 for v in size)):
        return None
    name = value.get("name")
    return {"source": source, "size": [size[0], size[1]], "name": name[:MAX_NAME] if isinstance(name, str) else ""}


class ImageItem(QGraphicsItem):
    """One reference image. Its origin is the center of the whole image; size is the image's full
    pixel size. A crop only hides part of it (the whole image is kept), so it can be undone any time."""

    def __init__(self, item_id: str, source: dict[str, Optional[str]], size: tuple[int, int]):
        super().__init__()
        self.item_id = item_id
        self.source = source  # {"path": linked file or None, "blob": file in the page's images/ or None}
        self.natural = (max(1, int(size[0])), max(1, int(size[1])))
        self.base: Optional[QPixmap] = None  # the small copy (BASE_SIDE), kept while it's on the board
        self.base_ok = False  # the small copy is of its current picture
        self.detail: Optional[QPixmap] = None  # a sharper copy, while it's in view (see BoardView)
        self.detail_side = 0  # the sharper copy's longest side (0: none)
        self.loading = 0  # a picture being decoded for it: its longest side (-1: the small copy only)
        self.load_token = 0  # bumps when its picture changes: decodes still running are dropped
        self._gray_pixmap: Optional[QPixmap] = None
        self._gray_key = 0
        self.fade_from: Optional[float] = None  # when its picture started fading in (None: not fading)
        self.crop: Optional[Crop] = None
        self.gray = False
        self.cropping = False  # crop mode: the whole image shows (the cut-away part faintly)
        self.group: Optional[str] = None
        self.caption = ""
        self.pixelated = False
        self.values = 0  # (see ItemState)
        self.guides = ""
        self.name = ""  # what the image list calls it (empty: its file's name)
        # Upscaled: the picture it was before ({"source", "size", "name"}), for Revert to Original
        self.original: Optional[dict[str, Any]] = None
        self.load_failed = False  # its picture couldn't be read (shown as a placeholder)
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsMovable)

    # ------------------ state ------------------
    def state(self) -> ItemState:
        flip = self.transform()
        return ItemState(
            self.pos().x(), self.pos().y(), self.scale(), self.rotation(),
            flip.m11() < 0, flip.m22() < 0, self.zValue(),
            self.crop, round(self.opacity(), 4), self.gray,
            group=self.group, caption=self.caption, pixelated=self.pixelated, values=self.values, guides=self.guides,
        )

    def apply_state(self, state: ItemState) -> None:
        self.setPos(state.x, state.y)
        self.setScale(state.scale)
        self.setRotation(state.rotation)
        flipped_v = self.transform().m22() < 0
        if state.caption != self.caption or (self.caption and state.flip_v != flipped_v):
            self.prepareGeometryChange()  # (the caption: under the image, which a vertical flip turns over)
        self.setTransform(QTransform.fromScale(-1.0 if state.flip_h else 1.0, -1.0 if state.flip_v else 1.0))
        self.setZValue(state.z)
        self.set_crop(state.crop)
        self.setOpacity(state.opacity)
        self.group = state.group
        if (state.gray, state.caption, state.pixelated, state.values, state.guides) != (self.gray, self.caption, self.pixelated, self.values, self.guides):
            self.gray, self.caption, self.pixelated = state.gray, state.caption, state.pixelated
            self.values, self.guides = state.values, state.guides
            self.update()

    def to_dict(self) -> dict[str, Any]:
        s = self.state()
        return {
            "id": self.item_id, "kind": "image", "source": self.source, "size": list(self.natural),
            "x": s.x, "y": s.y, "scale": s.scale, "rotation": s.rotation,
            "flip_h": s.flip_h, "flip_v": s.flip_v, "z": s.z,
            "crop": list(s.crop) if s.crop else None, "opacity": s.opacity, "gray": s.gray,
            "group": s.group, "caption": s.caption, "pixelated": s.pixelated, "name": self.name,
            "values": s.values, "guides": s.guides,
            **({"original": self.original} if self.original else {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImageItem":
        item = cls(str(data.get("id") or uuid.uuid4().hex), dict(data.get("source") or {}), tuple(data.get("size") or (512, 512)))
        try:
            opacity = min(1.0, max(MIN_OPACITY / 100, float(data.get("opacity", 1.0))))
        except (TypeError, ValueError):
            opacity = 1.0
        caption = data.get("caption")
        item.apply_state(ItemState(
            float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("scale", 1)) or 1.0,
            float(data.get("rotation", 0)), bool(data.get("flip_h")), bool(data.get("flip_v")), float(data.get("z", 0)),
            _clean_crop(data.get("crop"), item.natural), opacity, bool(data.get("gray")),
            group=_clean_group(data.get("group")),
            caption=caption[:MAX_CAPTION] if isinstance(caption, str) else "",
            pixelated=data.get("pixelated") is True,
            values=_clean_values(data.get("values")), guides=_clean_guides(data.get("guides")),
        ))
        name = data.get("name")
        item.name = name[:MAX_NAME] if isinstance(name, str) else ""
        item.original = _clean_original(data.get("original"))
        return item

    # ------------------ its file ------------------
    def linked_path(self) -> Optional[Path]:
        """The file it's linked to, if it has one (whether or not it's still there)."""
        path = self.source.get("path")
        return Path(path) if isinstance(path, str) and path else None

    def file_missing(self) -> bool:
        """Linked to a file that isn't there any more (the copy kept in the page shows instead)."""
        path = self.linked_path()
        return path is not None and not path.is_file()

    def display_name(self) -> str:
        if self.name:
            return self.name
        path = self.linked_path()
        return path.name if path is not None else "Pasted image"

    @property
    def pixmap(self) -> Optional[QPixmap]:
        """What it's drawn with: the sharper copy if it has one, else the small one."""
        return self.detail if self.detail is not None else self.base

    def set_image(self, image: QImage) -> None:
        """A whole decoded picture (a new image): its small copy is made from it."""
        self.load_token += 1
        self.loading = 0
        self.set_pictures(QPixmap.fromImage(_base_image(image)), None if _fits_base(image) else QPixmap.fromImage(image))

    def set_pictures(self, base: Optional[QPixmap], detail: Optional[QPixmap]) -> None:
        if base is not None:
            self.base, self.base_ok = base, True
        self.detail = detail
        self.detail_side = max(detail.width(), detail.height()) if detail is not None else 0
        self.update()

    def drop_detail(self) -> None:
        if self.detail is not None:
            self.detail, self.detail_side = None, 0
            self.update()

    def pictures(self) -> tuple:
        """(small copy, whether it's current, sharper copy): for undo (see _ReplaceCommand)."""
        return self.base, self.base_ok, self.detail

    def restore_pictures(self, pictures: tuple) -> None:
        self.load_token += 1  # (a decode of the other picture still running is dropped)
        self.loading = 0
        base, base_ok, detail = pictures
        self.base = base
        self.set_pictures(None, detail)
        self.base_ok = base_ok and base is not None

    def picture_changed(self) -> None:
        """Its source changed (relinked): load it again (what it shows stays until then)."""
        self.load_token += 1
        self.loading = 0
        self.base_ok = False
        self.detail_side = 0  # (kept on screen, but no longer counts as sharp)
        self.load_failed = False

    def longest_side(self) -> int:
        return max(self.natural)

    def image(self, sharp: Optional[QImage] = None) -> Optional[QImage]:
        """The image as it shows on the board: cropped (not flipped, rotated or grayscale).
        sharp: a better decode of the whole picture to cut it from (default: what's loaded)."""
        if sharp is not None:
            w, h = self.natural
            shown = self.crop_rect()
            sx, sy = sharp.width() / w, sharp.height() / h
            return sharp.copy(QRectF((shown.left() + w / 2) * sx, (shown.top() + h / 2) * sy, shown.width() * sx, shown.height() * sy).toAlignedRect())
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

    # ------------------ caption ------------------
    def caption_layout(self) -> Optional[tuple[QRectF, QFont, float]]:
        """(where the caption goes, its font, its padding) in the item's own coordinates, or None.
        Its size follows the image's; it sits under what shows (over it, in item terms, when
        flipped upside down, so it still ends up underneath)."""
        if not self.caption:
            return None
        shown = self.crop_rect()
        size = max(2.0, max(shown.width(), shown.height()) * CAPTION_SIZE)
        font = QFont()
        font.setPixelSize(max(2, round(size)))
        text = QFontMetricsF(font).boundingRect(
            QRectF(0, 0, shown.width(), 1e9), int(Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap), self.caption,
        )
        pad = size * 0.4
        height = text.height() + 2 * pad
        top = shown.top() - height if self.transform().m22() < 0 else shown.bottom()
        return QRectF(shown.left(), top, shown.width(), height), font, pad

    # ------------------ painting ------------------
    def boundingRect(self) -> QRectF:
        rect = self.full_rect() if self.cropping else self.crop_rect()
        caption = self.caption_layout()
        return rect.united(caption[0]) if caption else rect

    def shown_values(self) -> int:
        """How many flat greys it shows as (its own Values, else the board's; 0: none)."""
        scene = self.scene()
        return self.values or (getattr(scene, "values_all", 0) if scene is not None else 0)

    def _shown_pixmap(self) -> QPixmap:
        scene = self.scene()
        pixmap = self.pixmap
        tones = self.shown_values()
        if tones:  # (Values first: they're grey too)
            key = (pixmap.cacheKey(), tones)
            if self._gray_pixmap is None or self._gray_key != key:
                self._gray_pixmap = QPixmap.fromImage(_values_image(pixmap.toImage(), tones))
                self._gray_key = key
            return self._gray_pixmap
        if self.gray or (scene is not None and getattr(scene, "gray_all", False)):
            if self._gray_pixmap is None or self._gray_key != pixmap.cacheKey():
                self._gray_pixmap = QPixmap.fromImage(_gray_image(pixmap.toImage()))
                self._gray_key = pixmap.cacheKey()
            return self._gray_pixmap
        self._gray_pixmap = None
        return pixmap

    def _paint_guides(self, painter: QPainter, shown: QRectF) -> None:
        """The guide lines over what shows: white on a dark edge (they show on any picture), one pixel
        wide however far it's zoomed."""
        lines = guide_lines(self.guides, shown)
        if not lines:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for color, width in ((QColor(0, 0, 0, 150), 3.0), (QColor(255, 255, 255, 220), 1.0)):
            pen = QPen(color, width)
            pen.setCosmetic(True)  # (screen pixels, not the image's)
            painter.setPen(pen)
            painter.drawLines(lines)
        painter.restore()

    def fade(self) -> float:
        """How far its picture has faded in (1: all the way)."""
        if self.fade_from is None:
            return 1.0
        done = (time.monotonic() - self.fade_from) * 1000 / FADE_MS
        if done >= 1.0:
            self.fade_from = None
            return 1.0
        return max(0.0, done)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        if getattr(self, "lifted", False):
            painter.setOpacity(painter.opacity() * LIFTED_OPACITY)  # (dragged over a column: its line shows through)
        shown = self.crop_rect()
        fade = self.fade()
        if self.pixmap is None or fade < 1.0:
            # Still loading (or its file is gone): a quiet placeholder of the right size
            placeholder = QColor(theme_color("text_faint"))
            placeholder.setAlpha(40)
            painter.fillRect(shown, placeholder)
        if self.pixmap is not None:
            painter.save()
            painter.setOpacity(painter.opacity() * fade)
            pixmap = self._shown_pixmap()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, not self.pixelated)
            if self.cropping:
                # The part being cut away, faintly, so you can drag the crop back out over it
                painter.save()
                painter.setOpacity(painter.opacity() * CROP_OUTSIDE_OPACITY)
                full = self.full_rect()
                painter.drawPixmap(full, pixmap, self._pixmap_rect(full))
                painter.restore()
            painter.drawPixmap(shown, pixmap, self._pixmap_rect(shown))
            painter.restore()
        if self.guides:
            self._paint_guides(painter, shown)
        caption = self.caption_layout()
        if caption:
            box, font, pad = caption
            painter.save()
            flip = self.transform()
            if flip.m11() < 0 or flip.m22() < 0:  # keep the words readable on a flipped image
                middle = box.center()
                painter.translate(middle)
                painter.scale(-1.0 if flip.m11() < 0 else 1.0, -1.0 if flip.m22() < 0 else 1.0)
                painter.translate(-middle)
            painter.setFont(font)
            painter.setPen(theme_color("text_strong"))
            painter.drawText(
                box.adjusted(0, pad, 0, -pad), int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap),
                self.caption,
            )
            painter.restore()
        # The selection frame is drawn by the view (BoardView.drawForeground), not here


NoteContent = tuple[str, Optional[str], str]  # (the text as HTML, fill colour or None for the theme's, style)


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


def clear_paragraph_backgrounds(document: QTextDocument, start: int = 0, end: Optional[int] = None) -> bool:
    """Remove the backgrounds of whole paragraphs (web pages' text brings them when pasted) from
    the paragraphs between start and end; True if there were any."""
    last = document.characterCount() - 1 if end is None else end
    stop = document.findBlock(max(start, last - 1))
    block = document.findBlock(start)
    found = False
    while block.isValid():
        fmt = block.blockFormat()
        if fmt.hasProperty(QTextFormat.Property.BackgroundBrush):
            fmt.clearProperty(QTextFormat.Property.BackgroundBrush)
            QTextCursor(block).setBlockFormat(fmt)
            found = True
        if block == stop:
            break
        block = block.next()
    return found


def pasted_html(html: str) -> QTextDocumentFragment:
    """Text pasted into a note: its formatting, without paragraph backgrounds or pictures."""
    document = QTextDocument()
    document.setHtml(_IMG_TAG.sub("", html))
    clear_paragraph_backgrounds(document)
    return QTextDocumentFragment(document)


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
        self.fill: Optional[str] = None  # None: the theme's card colour
        self.style = "card"  # NOTE_STYLES
        self.group: Optional[str] = None
        self.on_edit_finished: Optional[Callable[["NoteItem"], None]] = None
        self.on_paragraph: Optional[Callable[[str], None]] = None  # (typed # , ## , - or 1. then Space: BoardView)
        self.hover_row: Optional[int] = None  # (a checklist line the mouse is over: its dots show)
        self.drop_row: Optional[int] = None  # (a line being dragged: where it would go, as a line number)
        self.about = ""  # the page header: the line under the page's name (about_item shows it)
        self.stats = ""  # the page header: what's on the page (the board keeps it up to date)
        self.about_item: Optional[InlineText] = None
        font = self.font()
        font.setPointSize(NOTE_FONT_PT)
        self.setFont(font)
        self.document().setDocumentMargin(NOTE_PADDING)
        self.document().setIndentWidth(LIST_INDENT)
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
        if self.about_item is not None:
            self.about_item.setDefaultTextColor(theme_color("text_muted"))
        self.update()

    # ------------------ state ------------------
    def state(self) -> ItemState:
        return ItemState(
            self.pos().x(), self.pos().y(), self.scale(), self.rotation(), False, False, self.zValue(),
            None, round(self.opacity(), 4), False, self.textWidth(), group=self.group,
            title=self.about if self.style == "title" else "",
        )

    def apply_state(self, state: ItemState) -> None:
        self.setPos(state.x, state.y)
        self.setScale(state.scale)
        self.setRotation(state.rotation)
        self.setZValue(state.z)
        self.setOpacity(state.opacity)
        self.group = state.group
        if state.width is not None and state.width != self.textWidth():
            self.setTextWidth(max(MIN_NOTE_WIDTH, state.width))
        if self.style == "title" and state.title != self.about:
            self.set_about(state.title)

    def content(self) -> NoteContent:
        return (self.toHtml(), self.fill, self.style)

    def set_content(self, content: tuple) -> None:
        """(html, fill) or (html, fill, style)."""
        html, fill = content[0], content[1]
        style = content[2] if len(content) > 2 and content[2] in NOTE_STYLES else self.style
        self.style = style
        self.setFont(self.base_font())
        self.setHtml(_IMG_TAG.sub("", html))
        self.fill = fill
        self.lay_out()
        self.update()

    def base_font(self) -> QFont:
        """What typed text starts as: a heading's (and the page header's) big and bold, a card's or plain note's the
        usual size."""
        font = QFont(self.font())
        font.setPointSize({"heading": HEADING_PT, "title": TITLE_PT}.get(self.style, NOTE_FONT_PT))
        font.setWeight(QFont.Weight.Bold if self.style in ("heading", "title") else QFont.Weight.Normal)
        return font

    def lay_out(self) -> None:
        """Room under a heading's text for its accent line, under the page header's for its line, what's on the
        page and its accent line (setting the text resets the document's margins)."""
        frame = self.document().rootFrame()
        fmt = frame.frameFormat()
        if self.style == "title":
            bottom = self._title_room()
        else:
            bottom = NOTE_PADDING + (RULE_GAP + RULE_HEIGHT if self.style == "heading" else 0.0)
        if fmt.bottomMargin() != bottom:
            fmt.setBottomMargin(bottom)
            frame.setFrameFormat(fmt)
            self.prepareGeometryChange()
        self.document().setIndentWidth(LIST_INDENT)
        tidy_checklists(self.document())
        self._place_about()

    # ------------------ the page header ("title" style) ------------------
    def stats_font(self) -> QFont:
        font = QFont(self.font())
        font.setPointSize(TITLE_STATS_PT)
        font.setWeight(QFont.Weight.Normal)
        return font

    def _about_text(self) -> "InlineText":
        if self.about_item is None:
            self.about_item = InlineText(self, TITLE_ABOUT_PT, False)
            self.about_item.setDefaultTextColor(theme_color("text_muted"))
            self.about_item.setPlainText(self.about)
            self.about_item.document().contentsChanged.connect(self._on_about_typed)
            self.document().documentLayout().documentSizeChanged.connect(self._on_size_changed)
        return self.about_item

    def _on_about_typed(self) -> None:
        if self.style == "title":
            self.lay_out()  # (taller or shorter: what's under it moves)

    def _on_size_changed(self, _size) -> None:
        if self.style == "title":
            self._place_about()

    def about_shown(self) -> bool:
        return self.style == "title" and (bool(self.about) or (self.about_item is not None and self.about_item.editing()))

    def _title_room(self) -> float:
        """Under the page's name: the line about it, what's on the page, the accent line."""
        about = self._about_text()
        about.setTextWidth(max(40.0, self.textWidth() - 2 * NOTE_PADDING + 2 * COLUMN_TITLE_MARGIN))
        room = TITLE_GAP + (about.boundingRect().height() - 2 * COLUMN_TITLE_MARGIN if self.about_shown() else 0.0)
        return room + TITLE_GAP + QFontMetricsF(self.stats_font()).lineSpacing() + RULE_GAP + RULE_HEIGHT + NOTE_PADDING

    def text_bottom(self) -> float:
        """Where the page header's name ends (its line, what's on the page and the accent line are under it)."""
        document = self.document()
        return document.documentLayout().blockBoundingRect(document.lastBlock()).bottom()

    def _place_about(self) -> None:
        if self.about_item is None:
            return
        if self.style != "title":
            self.about_item.hide()
            return
        self.about_item.setVisible(self.about_shown())
        self.about_item.setPos(NOTE_PADDING - COLUMN_TITLE_MARGIN, self.text_bottom() + TITLE_GAP - COLUMN_TITLE_MARGIN)

    def about_rect(self) -> QRectF:
        """The page header's line (where it is, or would be): double-clicking there types it."""
        top = self.text_bottom()
        height = self.boundingRect().bottom() - top
        return QRectF(0, top, self.boundingRect().width(), height)

    def set_about(self, text: str) -> None:
        self.about = " ".join(str(text).split())[:MAX_TITLE_ABOUT]
        about = self._about_text()
        if about.toPlainText() != self.about:
            about.setPlainText(self.about)
        self.lay_out()
        self.update()

    def set_stats(self, text: str) -> None:
        if text != self.stats:
            self.stats = text
            self.update()

    def set_title_text(self, name: str) -> None:
        """The page header's name (the page was renamed)."""
        self.setFont(self.base_font())
        self.setPlainText(name)
        self.lay_out()

    def fit_title(self) -> None:
        """The page header as wide as its words (its name on one line, if it fits)."""
        document = QTextDocument()
        document.setDefaultFont(self.base_font())
        document.setDocumentMargin(NOTE_PADDING)
        document.setPlainText(self.toPlainText())
        width = document.idealWidth()
        about = QFontMetricsF(self._about_text().font()).horizontalAdvance(self.about) + 2 * NOTE_PADDING + 8
        stats = QFontMetricsF(self.stats_font()).horizontalAdvance(self.stats) + 2 * NOTE_PADDING + 8
        width = min(MAX_TITLE_WIDTH, max(MIN_TITLE_WIDTH, width + 4, about, stats))
        if abs(width - self.textWidth()) > 0.5:
            self.setTextWidth(width)
            self.lay_out()

    # ------------------ checklists (to-dos) ------------------
    def check_rows(self) -> list:
        """The checklist lines (QTextBlock), top to bottom."""
        rows, block = [], self.document().firstBlock()
        while block.isValid():
            if is_check(block):
                rows.append(block)
            block = block.next()
        return rows

    def progress(self) -> tuple[int, int]:
        """(done, all) of the checklist lines."""
        rows = self.check_rows()
        return sum(1 for row in rows if row.blockFormat().marker() == QTextBlockFormat.MarkerType.Checked), len(rows)

    def _first_line(self, block) -> QRectF:
        rect = self.document().documentLayout().blockBoundingRect(block)
        line = block.layout().lineAt(0)
        if not line.isValid():
            return QRectF(rect.x() + LIST_INDENT, rect.y(), 0, rect.height())
        return QRectF(rect.x() + line.x(), rect.y() + line.y(), line.naturalTextWidth(), line.height())

    def box_rect(self, block) -> QRectF:
        line = self._first_line(block)
        return QRectF(line.x() - BOX_GAP - BOX, line.y() + (line.height() - BOX) / 2, BOX, BOX)

    def grip_rect(self, block) -> QRectF:
        line, box = self._first_line(block), self.box_rect(block)
        return QRectF(box.left() - 3 - GRIP_WIDTH, line.y(), GRIP_WIDTH, line.height())

    def checkbox_at(self, pos: QPointF) -> Optional[int]:
        """The checklist line (its number) whose box is at pos (the note's own coordinates), with a little room."""
        for row in self.check_rows():
            if self.box_rect(row).adjusted(-4, -4, 4, 4).contains(pos):
                return row.blockNumber()
        return None

    def grip_at(self, pos: QPointF) -> Optional[int]:
        for row in self.check_rows():
            if self.grip_rect(row).adjusted(-3, 0, 2, 0).contains(pos):
                return row.blockNumber()
        return None

    def row_at(self, pos: QPointF) -> Optional[int]:
        """The checklist line (its number) at pos's height, anywhere along it."""
        for row in self.check_rows():
            rect = self.document().documentLayout().blockBoundingRect(row)
            if rect.top() <= pos.y() < rect.bottom():
                return row.blockNumber()
        return None

    def set_hover_row(self, row: Optional[int]) -> None:
        if row != self.hover_row:
            self.hover_row = row
            self.update()

    def to_dict(self) -> dict[str, Any]:
        s = self.state()
        return {
            "id": self.item_id, "kind": "note", "text": self.toPlainText(), "html": self.toHtml(),
            "fill": self.fill, "width": self.textWidth(), "group": self.group, "style": self.style,
            "x": s.x, "y": s.y, "scale": s.scale, "rotation": s.rotation, "z": s.z, "opacity": s.opacity,
            **({"about": self.about} if self.style == "title" else {}),
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
        style = data.get("style") if data.get("style") in NOTE_STYLES else "card"
        if isinstance(html, str) and html:
            note.set_content((html, None, style))  # formatted text (the plain "text" is kept for search / older versions)
        else:
            note.style = style
            note.lay_out()
        if fit:
            note.setTextWidth(-1)
            width = max(MIN_NOTE_WIDTH, float(int(note.document().idealWidth()) + 2))
        note.fill = fill if isinstance(fill, str) and QColor(fill).isValid() else None
        note.apply_state(ItemState(
            float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("scale", 1)) or 1.0,
            float(data.get("rotation", 0)), z=float(data.get("z", 0)), opacity=opacity, width=width,
            group=_clean_group(data.get("group")),
            title=" ".join(data["about"].split())[:MAX_TITLE_ABOUT] if style == "title" and isinstance(data.get("about"), str) else "",
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

    def keyPressEvent(self, event) -> None:
        if self.editing() and event.text() == " " and self._markdown():
            event.accept()
            return
        enter = self.editing() and event.text() in ("\r", "\n") and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if enter and self._into_first_row():
            event.accept()
            return
        super().keyPressEvent(event)
        if enter:
            self._new_row()
        tidy_checklists(self.document())

    def _into_first_row(self) -> bool:
        """Enter at the end of a to-do's title, its first line still empty: type there (not on a new line)."""
        cursor = self.textCursor()
        below = cursor.block().next()
        if cursor.hasSelection() or not cursor.atBlockEnd() or is_check(cursor.block()) or not below.isValid():
            return False
        if not is_check(below) or below.text():
            return False
        cursor.setPosition(below.position())
        self.setTextCursor(cursor)
        return True

    def _new_row(self) -> None:
        """A line Enter started in a checklist is a box to tick, above the done ones (they sit at the bottom)."""
        cursor = self.textCursor()
        block = cursor.block()
        if not is_check(block):
            return
        fmt = block.blockFormat()
        if fmt.marker() != QTextBlockFormat.MarkerType.Unchecked:
            fmt.setMarker(QTextBlockFormat.MarkerType.Unchecked)
            cursor.setBlockFormat(fmt)
        number = block.blockNumber()
        while True:
            above = self.document().findBlockByNumber(number).previous()
            if not (is_check(above) and above.textList() == block.textList()
                    and above.blockFormat().marker() == QTextBlockFormat.MarkerType.Checked):
                break
            swap_rows(self.document(), above.blockNumber(), number)
            number -= 1
        cursor = QTextCursor(self.document().findBlockByNumber(number))
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        self.setTextCursor(cursor)

    def _markdown(self) -> bool:
        """Space after # , ## , - , * or 1. at the start of a line: that line becomes a heading or a list."""
        cursor = self.textCursor()
        if cursor.hasSelection() or self.on_paragraph is None:
            return False
        typed = cursor.block().text()[:cursor.positionInBlock()]
        kind = MARKDOWN.get(typed)
        if kind is None or cursor.currentList() is not None:
            return False
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self.setTextCursor(cursor)
        self.on_paragraph(kind)
        return True

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        # Switching to another app (or a menu opening) isn't finishing: you're coming back to type
        if self.editing() and event.reason() not in (Qt.FocusReason.ActiveWindowFocusReason, Qt.FocusReason.PopupFocusReason):
            if self.on_edit_finished is not None:
                self.on_edit_finished(self)

    # ------------------ painting ------------------
    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        if getattr(self, "lifted", False):
            painter.setOpacity(painter.opacity() * LIFTED_OPACITY)  # (dragged over a column: its line shows through)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = self.boundingRect()
        if self.style == "title":
            self._paint_title(painter, box)
        if self.style not in ("plain", "title"):  # (a card, as the board's other cards: plain text has none)
            paper, edge = card_colors(self.fill)
            painter.setPen(QPen(edge, 1.2))
            painter.setBrush(paper)
            painter.drawRoundedRect(box.adjusted(0.6, 0.6, -0.6, -0.6), CARD_RADIUS, CARD_RADIUS)
            if self.style == "card":
                paint_strip(painter, box.adjusted(0.6, 0.6, -0.6, -0.6), self.strip)
        if self.style == "heading":  # its accent line, under the text, lined up with it
            align = self.document().firstBlock().blockFormat().alignment() & Qt.AlignmentFlag.AlignHorizontal_Mask
            if align & Qt.AlignmentFlag.AlignHCenter:
                left = (box.width() - RULE_WIDTH) / 2
            elif align & Qt.AlignmentFlag.AlignRight:
                left = box.width() - NOTE_PADDING - RULE_WIDTH
            else:
                left = NOTE_PADDING
            top = box.bottom() - NOTE_PADDING - RULE_HEIGHT
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.strip) if self.strip else theme_color("accent"))  # (its colour: the line)
            painter.drawRoundedRect(QRectF(left, top, RULE_WIDTH, RULE_HEIGHT), RULE_HEIGHT / 2, RULE_HEIGHT / 2)
        painter.setPen(Qt.PenStyle.NoPen)
        for box, color in highlight_boxes(self.document()):
            radius = min(HIGHLIGHT_RADIUS, box.height() / 3)
            painter.setBrush(color)
            painter.drawRoundedRect(box.adjusted(-2, 0, 2, 0), radius, radius)  # (a little room round the letters)
        painter.restore()
        plain = QStyleOptionGraphicsItem(option)  # no dashed focus / selection box: the view draws the frame
        plain.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus)
        super().paint(painter, plain, widget)
        rows = self.check_rows()
        if rows:
            self._paint_checklist(painter, rows)

    def _paint_title(self, painter: QPainter, box: QRectF) -> None:
        """Under the page's name (and its line): what's on the page, then the accent line."""
        top = self.text_bottom() + TITLE_GAP
        if self.about_shown() and self.about_item is not None:
            top += self.about_item.boundingRect().height() - 2 * COLUMN_TITLE_MARGIN + TITLE_GAP
        font = self.stats_font()
        line = QFontMetricsF(font).lineSpacing()
        painter.setFont(font)
        painter.setPen(theme_color("text_faint"))
        width = box.width() - 2 * NOTE_PADDING
        painter.drawText(QRectF(NOTE_PADDING, top, width, line), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         QFontMetricsF(font).elidedText(self.stats, Qt.TextElideMode.ElideRight, width))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.strip) if self.strip else theme_color("accent"))
        painter.drawRoundedRect(QRectF(NOTE_PADDING, top + line + RULE_GAP, RULE_WIDTH, RULE_HEIGHT), RULE_HEIGHT / 2, RULE_HEIGHT / 2)

    def _paint_checklist(self, painter: QPainter, rows: list) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paper = card_colors(self.fill)[0] if self.style != "plain" else None
        accent, faint = theme_color("accent"), theme_color("text_faint")
        done = 0
        for row in rows:
            checked = row.blockFormat().marker() == QTextBlockFormat.MarkerType.Checked
            done += checked
            box = self.box_rect(row)
            if checked:
                rect = self.document().documentLayout().blockBoundingRect(row)
                layout = row.layout()
                for i in range(layout.lineCount()):
                    line = layout.lineAt(i)
                    text = QRectF(rect.x() + line.x(), rect.y() + line.y(), line.naturalTextWidth(), line.height())
                    if paper is not None:  # faded into the card
                        veil = QColor(paper)
                        veil.setAlphaF(DONE_FADE)
                        painter.fillRect(text.adjusted(-1, 0, 1, 0), veil)
                    painter.setPen(QPen(faint, 1.4))
                    middle = text.top() + line.ascent() * 0.62
                    painter.drawLine(QPointF(text.left(), middle), QPointF(text.right(), middle))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(accent)
                painter.drawRoundedRect(box, 4, 4)
                tick = QPen(theme_color("on_accent"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                painter.setPen(tick)
                painter.drawPolyline([box.topLeft() + QPointF(3.7, 7.9), box.topLeft() + QPointF(6.5, 10.5),
                                      box.topLeft() + QPointF(11.5, 4.9)])
            else:
                painter.setPen(QPen(faint, 1.4))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(box.adjusted(0.7, 0.7, -0.7, -0.7), 4, 4)
            if self.hover_row == row.blockNumber():  # its dots: drag to move it
                grip = self.grip_rect(row)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(faint)
                for dx in (2.0, 6.0):
                    for dy in (-4.5, 0.0, 4.5):
                        painter.drawEllipse(QPointF(grip.left() + dx, grip.center().y() + dy), 1.2, 1.2)
        if self.drop_row is not None:
            target = self.document().findBlockByNumber(self.drop_row)
            if target.isValid():
                rect = self.document().documentLayout().blockBoundingRect(target)
                painter.setPen(QPen(accent, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawLine(QPointF(rect.left() + 4, rect.top()), QPointF(self.boundingRect().right() - NOTE_PADDING, rect.top()))
        # "2 of 4", at the title's right (or above the first line), and the bar under it
        first = rows[0]
        title = first.previous()
        count = f"{done} of {len(rows)}"
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, NOTE_FONT_PT * 0.72))
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        width = QFontMetricsF(font).horizontalAdvance(count)
        right = self.boundingRect().right() - NOTE_PADDING
        top = self._first_line(first).top()
        bar_y = top - BAND / 2 - BAR_HEIGHT / 2 - 1
        if title.isValid() and not is_check(title):
            line = self._first_line(title)
            painter.setPen(faint)
            painter.drawText(QRectF(right - width, line.top(), width, line.height()), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, count)
            bar_right = right
        else:
            painter.setPen(faint)
            painter.drawText(QRectF(right - width, bar_y - 8, width, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, count)
            bar_right = right - width - 8
        left = NOTE_PADDING
        if bar_right > left + 10:
            track = QColor(faint)
            track.setAlphaF(0.25)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(track)
            painter.drawRoundedRect(QRectF(left, bar_y, bar_right - left, BAR_HEIGHT), BAR_HEIGHT / 2, BAR_HEIGHT / 2)
            if done:
                painter.setBrush(accent)
                painter.drawRoundedRect(QRectF(left, bar_y, (bar_right - left) * done / len(rows), BAR_HEIGHT),
                                        BAR_HEIGHT / 2, BAR_HEIGHT / 2)
        painter.restore()


class StrokeItem(QGraphicsPathItem):
    """A freehand stroke (Draw, D). Its points are in its own coordinates (its origin is where it
    began); it moves, scales, rotates, stacks and groups like images and notes. Without a colour of
    its own it takes the theme's text colour (so it follows the theme, like notes)."""

    def __init__(self, item_id: str, points: list[QPointF], color: Optional[str], width: float):
        super().__init__()
        self.item_id = item_id
        self.color = color
        self.width = max(MIN_STROKE_WIDTH, float(width))
        self.points: list[QPointF] = []
        self.group: Optional[str] = None
        self.crop = None  # (image-only things: strokes have none)
        self.gray = False
        self.cropping = False
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.apply_theme()
        self.set_points(points)

    def apply_theme(self) -> None:
        pen = QPen(QColor(self.color) if self.color else theme_color("text_strong"), self.width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.setPen(pen)

    def set_points(self, points: list[QPointF]) -> None:
        """The stroke's line, smoothed through its points."""
        self.points = list(points)
        path = QPainterPath(self.points[0] if self.points else QPointF())
        if len(self.points) == 1:
            path.lineTo(self.points[0])  # (a dot: a round cap on nothing)
        for before, point in zip(self.points, self.points[1:]):
            path.quadTo(before, (before + point) / 2.0)
        if len(self.points) > 1:
            path.lineTo(self.points[-1])
        self.setPath(path)

    def state(self) -> ItemState:
        return ItemState(
            self.pos().x(), self.pos().y(), self.scale(), self.rotation(), False, False, self.zValue(),
            None, round(self.opacity(), 4), group=self.group,
        )

    def apply_state(self, state: ItemState) -> None:
        self.setPos(state.x, state.y)
        self.setScale(state.scale)
        self.setRotation(state.rotation)
        self.setZValue(state.z)
        self.setOpacity(state.opacity)
        self.group = state.group

    def to_dict(self) -> dict[str, Any]:
        s = self.state()
        return {
            "id": self.item_id, "kind": "stroke", "color": self.color, "width": self.width,
            "points": [[round(p.x(), 2), round(p.y(), 2)] for p in self.points],
            "x": s.x, "y": s.y, "scale": s.scale, "rotation": s.rotation, "z": s.z, "opacity": s.opacity, "group": s.group,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrokeItem":
        points = []
        raw = data.get("points")
        for point in raw[:MAX_STROKE_POINTS] if isinstance(raw, list) else []:
            if isinstance(point, (list, tuple)) and len(point) == 2 and all(isinstance(v, (int, float)) for v in point):
                points.append(QPointF(float(point[0]), float(point[1])))
        color = data.get("color")
        try:
            width = min(MAX_STROKE_WIDTH, float(data.get("width", 4.0)))
            opacity = min(1.0, max(MIN_OPACITY / 100, float(data.get("opacity", 1.0))))
        except (TypeError, ValueError):
            width, opacity = 4.0, 1.0
        stroke = cls(
            str(data.get("id") or uuid.uuid4().hex), points or [QPointF()],
            color if isinstance(color, str) and QColor(color).isValid() else None, width,
        )
        stroke.apply_state(ItemState(
            float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("scale", 1)) or 1.0,
            float(data.get("rotation", 0)), z=float(data.get("z", 0)), opacity=opacity, group=_clean_group(data.get("group")),
        ))
        return stroke

    def full_rect(self) -> QRectF:
        return self.boundingRect()

    def crop_rect(self) -> QRectF:
        return self.boundingRect()

    def image(self) -> None:
        return None

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        plain = QStyleOptionGraphicsItem(option)  # no dashed selection box: the view draws the frame
        plain.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        super().paint(painter, plain, widget)


class InlineText(QGraphicsTextItem):
    """Text typed in place on a board item, a child of it: a column's title, a line's label (BoardView._start_renaming)."""

    def __init__(self, parent: QGraphicsItem, point_size: int, bold: bool):
        super().__init__(parent)
        font = self.font()
        font.setPointSize(point_size)
        font.setWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
        self.setFont(font)
        self.document().setDocumentMargin(COLUMN_TITLE_MARGIN)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    def editing(self) -> bool:
        return self.textInteractionFlags() != Qt.TextInteractionFlag.NoTextInteraction

    def start_editing(self) -> None:
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        cursor = self.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)  # (typing replaces it)
        self.setTextCursor(cursor)

    def stop_editing(self) -> None:
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.clearFocus()

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        plain = QStyleOptionGraphicsItem(option)  # (no dashed focus box: what it's on shows that it's being typed in)
        plain.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus)
        super().paint(painter, plain, widget)


class ColumnItem(QGraphicsItem):
    """A column: a dashed outline with a big title (and the accent line under it) that cards stack in, top to
    bottom, at its width: notes, to-dos and pictures. The cards stay board items of their own (saved, selected and
    edited as ever); the column keeps their order (members: item ids) and BoardView.lay_out_columns puts them in
    place. Its chip counts them; folded, it's just its title and the chip. Hovering it shows an x on its top-right
    corner: delete it, with its cards or without them. Its origin is its top-left; it moves, scales and stacks
    like a note (it doesn't turn)."""

    def __init__(self, item_id: str, title: str = "", width: float = COLUMN_WIDTH):
        super().__init__()
        self.item_id = item_id
        self.crop = None  # (image-only things: columns have none)
        self.gray = False
        self.cropping = False
        self.group: Optional[str] = None
        self.members: list[str] = []  # its cards' ids, top to bottom (ids not on the page are skipped)
        self.folded = False
        self.width = max(MIN_COLUMN_WIDTH, float(width))
        self.height = 0.0  # (set by lay_out: its title, then its cards)
        self.counts = (0, 0)  # (cards, pictures) in it, for the chip
        self.hovered = False  # the mouse is over it: its x shows
        self.close_hot = False  # ...and over the x
        self.hot = False  # a card is being dragged over it
        self.drop_y: Optional[float] = None  # where that card would go (in its own coordinates: the board draws the line,
        # over the card being dragged)
        self.title_item = InlineText(self, COLUMN_TITLE_PT, True)
        self.title_item.setPlainText(title)
        self.apply_theme()
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.lay_out_header()
        self.set_height(self.header_height() + COLUMN_EMPTY)

    def apply_theme(self) -> None:
        self.title_item.setDefaultTextColor(theme_color("text_strong"))
        self.update()

    # ------------------ state ------------------
    def title(self) -> str:
        return self.title_item.toPlainText()

    def set_title(self, title: str) -> None:
        if title != self.title():
            self.title_item.setPlainText(title)
            self.lay_out_header()

    def state(self) -> ItemState:
        return ItemState(
            self.pos().x(), self.pos().y(), self.scale(), 0.0, False, False, self.zValue(), None,
            round(self.opacity(), 4), False, self.width, group=self.group, title=self.title(),
            members=tuple(self.members), folded=self.folded,
        )

    def apply_state(self, state: ItemState) -> None:
        self.setPos(state.x, state.y)
        self.setScale(state.scale)
        self.setZValue(state.z)
        self.setOpacity(state.opacity)
        self.group = state.group
        if state.width is not None:
            self.setTextWidth(state.width)
        self.set_title(state.title)
        self.members = list(state.members)
        self.folded = state.folded
        self.update()

    def to_dict(self) -> dict[str, Any]:
        s = self.state()
        return {
            "id": self.item_id, "kind": "column", "title": self.title(), "members": list(self.members),
            "width": self.width, "folded": self.folded, "group": self.group,
            "x": s.x, "y": s.y, "scale": s.scale, "z": s.z, "opacity": s.opacity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ColumnItem":
        title = data.get("title")
        try:
            width = float(data.get("width", COLUMN_WIDTH))
        except (TypeError, ValueError):
            width = COLUMN_WIDTH
        if not math.isfinite(width):
            width = COLUMN_WIDTH
        try:
            opacity = min(1.0, max(MIN_OPACITY / 100, float(data.get("opacity", 1.0))))
        except (TypeError, ValueError):
            opacity = 1.0
        members = data.get("members")
        column = cls(str(data.get("id") or uuid.uuid4().hex), title[:MAX_COLUMN_TITLE] if isinstance(title, str) else "",
                     min(width, MAX_COLUMN_WIDTH))
        column.apply_state(ItemState(
            float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("scale", 1)) or 1.0,
            z=float(data.get("z", 0)), opacity=opacity, width=column.width, group=_clean_group(data.get("group")),
            title=column.title(), folded=data.get("folded") is True,
            members=tuple(m for m in (members if isinstance(members, list) else [])[:MAX_COLUMN_CARDS] if isinstance(m, str) and 0 < len(m) <= 64),
        ))
        return column

    # ------------------ size ------------------
    def textWidth(self) -> float:  # (named as a note's, so the side handles widen it the same way)
        return self.width

    def setTextWidth(self, width: float) -> None:
        width = min(MAX_COLUMN_WIDTH, max(MIN_COLUMN_WIDTH, float(width)))
        if width != self.width:
            self.prepareGeometryChange()
            self.width = width
            self.lay_out_header()

    def set_height(self, height: float) -> None:
        if height != self.height:
            self.prepareGeometryChange()
            self.height = height

    def inner_width(self) -> float:
        """How wide its cards are (its own units)."""
        return self.width - 2 * COLUMN_PAD

    def set_counts(self, cards: int, pictures: int) -> None:
        if (cards, pictures) != self.counts:
            self.counts = (cards, pictures)
            self.lay_out_header()
            self.update()

    def chip_text(self) -> str:
        cards, pictures = self.counts
        parts = [f"{cards} card" + ("" if cards == 1 else "s")] if cards or not pictures else []
        if pictures:
            parts.append(f"{pictures} image" + ("" if pictures == 1 else "s"))
        return ", ".join(parts)

    def _chip_font(self) -> QFont:
        font = QFont(self.title_item.font())
        font.setPointSizeF(NOTE_FONT_PT * 0.72)
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def chip_rect(self) -> QRectF:
        width = QFontMetricsF(self._chip_font()).horizontalAdvance(self.chip_text()) + 2 * CHIP_PAD
        middle = self._title_middle()
        return QRectF(self.width - COLUMN_PAD - width, middle - CHIP_HEIGHT / 2, width, CHIP_HEIGHT)

    def _title_middle(self) -> float:
        """The middle of the title's first line (where the arrow and the chip sit)."""
        line = QFontMetricsF(self.title_item.font()).height()
        return self.title_item.y() + COLUMN_TITLE_MARGIN + line / 2

    def lay_out_header(self) -> None:
        """The title between the fold arrow and the chip, wrapping if it's long."""
        self.title_item.setPos(COLUMN_PAD + CHEVRON, COLUMN_PAD - COLUMN_TITLE_MARGIN)
        chip = QFontMetricsF(self._chip_font()).horizontalAdvance(self.chip_text()) + 2 * CHIP_PAD
        self.title_item.setTextWidth(max(40.0, self.width - COLUMN_PAD - CHEVRON - chip - 8 - COLUMN_PAD))
        self.update()

    def rule_rect(self) -> QRectF:
        """The accent line under the title."""
        title = self.title_item
        top = title.y() + title.boundingRect().height() - COLUMN_TITLE_MARGIN + RULE_GAP / 2
        return QRectF(title.x() + COLUMN_TITLE_MARGIN, top, RULE_WIDTH, RULE_HEIGHT)

    def header_height(self) -> float:
        return self.rule_rect().bottom() + COLUMN_PAD + 2

    def chevron_rect(self) -> QRectF:
        """The fold arrow's box (a click there folds or opens the column)."""
        middle = self._title_middle()
        return QRectF(COLUMN_PAD - 4, middle - CHEVRON / 2 - 2, CHEVRON + 2, CHEVRON + 4)

    def title_rect(self) -> QRectF:
        return self.title_item.mapRectToParent(self.title_item.boundingRect())

    def close_rect(self) -> QRectF:
        """The x on its top-right corner, on the outline's curve."""
        corner = COLUMN_RADIUS * (1 - math.sqrt(0.5))
        return QRectF(self.width - corner - CLOSE_RADIUS, corner - CLOSE_RADIUS, 2 * CLOSE_RADIUS, 2 * CLOSE_RADIUS)

    def set_hovered(self, hovered: bool, close_hot: bool = False) -> None:
        if (hovered, close_hot) != (self.hovered, self.close_hot):
            self.prepareGeometryChange()  # (its shape: the x comes and goes)
            self.hovered, self.close_hot = hovered, close_hot
            self.update()

    def full_rect(self) -> QRectF:
        return QRectF(0, 0, self.width, self.height)

    def crop_rect(self) -> QRectF:
        return self.full_rect()

    def image(self) -> None:
        return None

    def boundingRect(self) -> QRectF:
        return self.full_rect().united(self.close_rect()).adjusted(-1, -1, 1, 1)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(self.full_rect(), COLUMN_RADIUS, COLUMN_RADIUS)
        if self.hovered:
            path.addEllipse(self.close_rect())
        return path.simplified()

    # ------------------ painting ------------------
    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent, faint = (QColor(self.strip) if self.strip else theme_color("accent")), theme_color("text_faint")
        body = self.full_rect().adjusted(0.75, 0.75, -0.75, -0.75)
        if self.hot:
            painter.setPen(QPen(theme_color("accent_border"), 2.0))
        else:
            edge = QColor(self.strip) if self.strip else QColor(theme_color("text_faint"))
            edge.setAlphaF(0.85 if self.strip else COLUMN_EDGE_ALPHA)
            pen = QPen(edge, 1.5, Qt.PenStyle.DashLine)
            pen.setDashPattern([4.0, 3.0])
            painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(body, COLUMN_RADIUS, COLUMN_RADIUS)
        # the fold arrow: pointing down while open, right while folded
        arrow = self.chevron_rect()
        middle = arrow.center()
        painter.setPen(QPen(theme_color("text_muted"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if self.folded:
            points = [middle + QPointF(-2.0, -4.5), middle + QPointF(2.5, 0.0), middle + QPointF(-2.0, 4.5)]
        else:
            points = [middle + QPointF(-4.5, -2.0), middle + QPointF(0.0, 2.5), middle + QPointF(4.5, -2.0)]
        painter.drawPolyline(points)
        # the accent line under the title
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(self.rule_rect(), RULE_HEIGHT / 2, RULE_HEIGHT / 2)
        # the chip: how many cards (and pictures)
        chip = self.chip_rect()
        painter.setBrush(theme_color("accent_soft"))
        painter.drawRoundedRect(chip, chip.height() / 2, chip.height() / 2)
        painter.setFont(self._chip_font())
        painter.setPen(theme_color("card_selected_text"))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, self.chip_text())
        if not self.folded and not any(self.counts):
            hint = QFont(self.title_item.font())
            hint.setPointSizeF(NOTE_FONT_PT * 0.8)
            hint.setWeight(QFont.Weight.Normal)
            painter.setFont(hint)
            painter.setPen(faint)
            painter.drawText(QRectF(0, self.header_height(), self.width, self.height - self.header_height() - COLUMN_PAD),
                             Qt.AlignmentFlag.AlignCenter, "Drag cards here")
        if self.hovered:  # the x: delete it (BoardView asks: with its cards or without)
            box = self.close_rect()
            painter.setPen(QPen(theme_color("danger") if self.close_hot else theme_color("text_faint"), 1.2))
            painter.setBrush(theme_color("danger") if self.close_hot else theme_color("raised"))
            painter.drawEllipse(box.adjusted(0.6, 0.6, -0.6, -0.6))
            cross = QPen(theme_color("on_accent") if self.close_hot else theme_color("text_muted"), 1.8,
                         Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(cross)
            arm = CLOSE_RADIUS * 0.36
            c = box.center()
            painter.drawLine(c + QPointF(-arm, -arm), c + QPointF(arm, arm))
            painter.drawLine(c + QPointF(-arm, arm), c + QPointF(arm, -arm))
        painter.restore()


TABLE_KINDS = ("text", "number", "status")
TABLE_ALIGNS = ("", "left", "center", "right")  # a table column's alignment ("": its kind's: numbers right, the rest left)  # a table column's kind: typed text, numbers (a total under them), status chips
TABLE_PAD = 14.0  # between a table card's edge and its cells
TABLE_FONT_PT = 13  # its cells' text
TABLE_HEAD_PT = 9.5  # its header row (bold capitals)
TABLE_TITLE_PT = 16
CELL_PAD_X = 8.0
CELL_PAD_Y = 8.0
TABLE_RULE_ALPHA = 0.45  # the lines between rows: the theme's faint text this strong (clearer than its border colour)
TABLE_HEAD_RULE = 2.0  # the accent line under the header row
MIN_TABLE_COLUMN = 40.0
TEXT_COLUMN_EASY = 110.0  # a narrowed table: its text columns go down to this before its number and status columns narrow
MAX_TABLE_COLUMN = 4000.0
MAX_TABLE_ROWS = 1000
MAX_TABLE_COLUMNS = 40
MAX_CELL_TEXT = 2000
MAX_TABLE_TITLE = 200
MAX_STATUSES = 30
TABLE_PLUS = 18.0  # the + bars past the last row and the last column (hovering a table)
TABLE_PLUS_GAP = 6.0
TABLE_EDGE_PX = 5.0  # this near a column's edge (on screen), a drag sizes the column
DEFAULT_STATUSES = (("To do", "#8b8b9e"), ("In progress", "#f59e0b"), ("Done", "#16a34a"), ("Blocked", "#dc2626"))
STATUS_PALETTE = (  # (the colours a status can have)
    ("Grey", "#8b8b9e"), ("Amber", "#f59e0b"), ("Green", "#16a34a"), ("Red", "#dc2626"), ("Blue", "#3b82f6"),
    ("Purple", "#a855f7"), ("Pink", "#ec4899"), ("Teal", "#14b8a6"), ("Orange", "#f97316"), ("Indigo", "#6366f1"),
)
TABLE_HOVER_ALPHA = 0.07  # the row under the mouse: the theme's text colour this strong (reads on any theme)
_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def cell_number(text: str) -> Optional[tuple[float, str]]:
    """A number cell's value and its unit ("1.5 h" -> (1.5, "h")); None: no number in it."""
    match = _NUMBER.search(text)
    if match is None:
        return None
    try:
        value = float(match.group().replace(",", "."))
    except ValueError:
        return None
    return value, text[match.end():].strip()


def format_total(values: list[tuple[float, str]]) -> str:
    """The sum of a number column, with the unit its cells use most ("8.5 h")."""
    total = sum(value for value, _unit in values)
    units = [unit for _value, unit in values if unit]
    unit = max(units, key=units.count) if units else ""
    text = f"{round(total, 4):g}"
    return f"{text} {unit}" if unit else text


class TableItem(QGraphicsItem):
    """A table card: an optional title, a header row (column names in small capitals over the accent line), then
    rows of cells. A column is text, numbers (their total in a row at the bottom, in the unit they use) or status
    chips (picked from the table's statuses, which can be renamed, recoloured and added to). Hovering it shows a +
    past the last row and past the last column, and a grip on the row under the mouse (drag it up or down); a
    column's edge drags to size it. Its cells are typed in place (BoardView: Tab and Enter move on). Its origin is
    its top-left; it moves, scales and stacks like a note (it doesn't turn), and goes in columns."""

    def __init__(self, item_id: str, columns: Optional[list] = None, rows: Optional[list] = None):
        super().__init__()
        self.item_id = item_id
        self.crop = None  # (image-only things: tables have none)
        self.gray = False
        self.cropping = False
        self.group: Optional[str] = None
        self.columns: list[list] = [list(c) for c in (columns or [["Task", "text", 240.0, ""], ["Time", "number", 100.0, ""],
                                                                   ["Status", "status", 160.0, ""]])]  # [name, kind, width, align]
        self.rows: list[list[str]] = [list(r) for r in (rows if rows is not None else [[""] * len(self.columns) for _ in range(3)])]
        self.statuses: list[tuple[str, str]] = list(DEFAULT_STATUSES)  # (name, colour)
        self.header = True
        self.current: tuple[int, int] = (0, 0)  # the cell last clicked or typed in: the bar's Row and Column work on it
        self.editing_cell: Optional[tuple[int, int]] = None  # typing in it (row -1: the header)
        self.hovered = False  # the mouse is over it: its + bars show
        self.plus_hot: Optional[str] = None  # ...over one of them ("row" / "column")
        self.hover_row: Optional[int] = None  # its grip shows
        self.edge_hot: Optional[int] = None  # the column whose right edge is under the mouse (or being dragged)
        self.drop_row: Optional[int] = None  # a row being dragged: the line it would go above (len(rows): under the last)
        self.width = 0.0
        self.height = 0.0
        self._tops: list[float] = []
        self._heights: list[float] = []
        self._head = QRectF()
        self._total: Optional[QRectF] = None
        self.title_item = InlineText(self, TABLE_TITLE_PT, True)
        self.editor = InlineText(self, TABLE_FONT_PT, False)  # typing in a cell: over it
        self.editor.hide()
        self.editor.document().contentsChanged.connect(self._on_editor_changed)
        self.apply_theme()
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.normalize()
        self.lay_out()

    def apply_theme(self) -> None:
        self.title_item.setDefaultTextColor(theme_color("text_strong"))
        self.editor.setDefaultTextColor(theme_color("text"))
        self.update()

    # ------------------ fonts ------------------
    def cell_font(self) -> QFont:
        font = QFont(self.editor.font())
        font.setPointSizeF(TABLE_FONT_PT)
        font.setWeight(QFont.Weight.Normal)
        font.setCapitalization(QFont.Capitalization.MixedCase)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.0)  # (the editor's font: a header's had some)
        return font

    def head_font(self) -> QFont:
        font = QFont(self.editor.font())
        font.setPointSizeF(TABLE_HEAD_PT)
        font.setWeight(QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
        font.setCapitalization(QFont.Capitalization.AllUppercase)
        return font

    def chip_font(self) -> QFont:
        font = self.cell_font()
        font.setPointSizeF(TABLE_FONT_PT * 0.85)
        font.setWeight(QFont.Weight.DemiBold)
        return font

    # ------------------ state ------------------
    def title(self) -> str:
        return self.title_item.toPlainText()

    def set_title(self, title: str) -> None:
        if title != self.title():
            self.title_item.setPlainText(title)
        self.lay_out()

    def table_state(self) -> tuple:
        return (self.header, tuple(tuple(column) for column in self.columns),
                tuple(tuple(row) for row in self.rows), tuple(self.statuses))

    def set_table_state(self, table: tuple) -> None:
        header, columns, rows, statuses = table
        self.header = header
        self.columns = [list(column) for column in columns]
        self.rows = [list(row) for row in rows]
        self.statuses = list(statuses)
        self.normalize()
        self.lay_out()

    def normalize(self) -> None:
        """Every row as long as the columns; the current cell on the table."""
        if not self.columns:
            self.columns = [["Column 1", "text", 200.0, ""]]
        self.columns = [list(c[:3]) + [c[3] if len(c) > 3 and c[3] in TABLE_ALIGNS else ""] for c in self.columns]
        n = len(self.columns)
        self.rows = [(row + [""] * n)[:n] for row in self.rows]
        r, c = self.current
        self.current = (min(max(-1 if self.header else 0, r), len(self.rows) - 1), min(max(0, c), n - 1))

    def state(self) -> ItemState:
        return ItemState(
            self.pos().x(), self.pos().y(), self.scale(), 0.0, False, False, self.zValue(), None,
            round(self.opacity(), 4), False, self.width, group=self.group, title=self.title(), table=self.table_state(),
        )

    def apply_state(self, state: ItemState) -> None:
        self.setPos(state.x, state.y)
        self.setScale(state.scale)
        self.setZValue(state.z)
        self.setOpacity(state.opacity)
        self.group = state.group
        if state.table is not None:
            self.set_table_state(state.table)
        self.set_title(state.title)
        self.update()

    def to_dict(self) -> dict[str, Any]:
        s = self.state()
        return {
            "id": self.item_id, "kind": "table", "title": self.title(), "header": self.header,
            "columns": [{"name": name, "kind": kind, "width": width, "align": align} for name, kind, width, align in self.columns],
            "rows": [list(row) for row in self.rows],
            "statuses": [{"name": name, "color": color} for name, color in self.statuses],
            "group": self.group, "x": s.x, "y": s.y, "scale": s.scale, "z": s.z, "opacity": s.opacity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TableItem":
        def number(value: Any, default: float) -> float:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return default
            return value if math.isfinite(value) else default

        def text(value: Any, limit: int) -> str:
            return value[:limit] if isinstance(value, str) else ""

        columns = []
        for column in (data.get("columns") if isinstance(data.get("columns"), list) else [])[:MAX_TABLE_COLUMNS]:
            column = column if isinstance(column, dict) else {}
            kind = column.get("kind") if column.get("kind") in TABLE_KINDS else "text"
            width = min(MAX_TABLE_COLUMN, max(MIN_TABLE_COLUMN, number(column.get("width"), 160.0)))
            align = column.get("align") if column.get("align") in TABLE_ALIGNS else ""
            columns.append([text(column.get("name"), MAX_TABLE_TITLE), kind, width, align])
        rows = [[text(cell, MAX_CELL_TEXT) for cell in row[:MAX_TABLE_COLUMNS]]
                for row in (data.get("rows") if isinstance(data.get("rows"), list) else [])[:MAX_TABLE_ROWS] if isinstance(row, list)]
        table = cls(str(data.get("id") or uuid.uuid4().hex), columns or None, rows)
        statuses = []
        for status in (data.get("statuses") if isinstance(data.get("statuses"), list) else [])[:MAX_STATUSES]:
            status = status if isinstance(status, dict) else {}
            name, color = text(status.get("name"), MAX_TABLE_TITLE).strip(), status.get("color")
            if name and name not in [s[0] for s in statuses]:
                statuses.append((name, color if isinstance(color, str) and QColor(color).isValid() else DEFAULT_STATUSES[0][1]))
        opacity = min(1.0, max(MIN_OPACITY / 100, number(data.get("opacity"), 1.0)))
        table.apply_state(ItemState(
            number(data.get("x"), 0.0), number(data.get("y"), 0.0), number(data.get("scale"), 1.0) or 1.0,
            z=number(data.get("z"), 0.0), opacity=opacity, group=_clean_group(data.get("group")),
            title=text(data.get("title"), MAX_TABLE_TITLE),
            table=(data.get("header") is not False, tuple(tuple(c) for c in table.columns), tuple(tuple(r) for r in table.rows),
                   tuple(statuses) if isinstance(data.get("statuses"), list) else DEFAULT_STATUSES),
        ))
        return table

    def plain_text(self) -> str:
        """Its cells as tab-separated lines (the header first): for the clipboard."""
        lines = ["\t".join(column[0] for column in self.columns)] if self.header else []
        lines += ["\t".join(" ".join(cell.split()) for cell in row) for row in self.rows]
        return "\n".join(lines)

    # ------------------ size ------------------
    def textWidth(self) -> float:  # (named as a note's, so the side handles widen it the same way)
        return self.width

    def setTextWidth(self, width: float) -> None:
        """Wider: its columns share the room, as wide as they were to each other. Narrower: its text columns give up
        room first (their text wraps), then the number and status columns, down to what their cells need."""
        inner = sum(c[2] for c in self.columns)
        target = max(MIN_TABLE_COLUMN * len(self.columns), float(width) - 2 * TABLE_PAD)
        if inner <= 0 or abs(target - inner) <= 1e-6:
            if self._rebalance():
                self.lay_out()
            return
        if target > inner:
            factor = target / inner
            for column in self.columns:
                column[2] = min(MAX_TABLE_COLUMN, column[2] * factor)
        else:
            text = [c for c, column in enumerate(self.columns) if column[1] == "text"]
            others = [c for c in range(len(self.columns)) if c not in text]
            need = self._take(inner - target, ((text, lambda c: TEXT_COLUMN_EASY), (others, self.least_width),
                                               (text, lambda c: MIN_TABLE_COLUMN)))
            if need > 1e-9:  # (still too wide: everything narrower alike)
                factor = target / sum(c[2] for c in self.columns)
                for column in self.columns:
                    column[2] = max(MIN_TABLE_COLUMN, column[2] * factor)
        self._rebalance()
        self.lay_out()

    def _rebalance(self) -> bool:
        """Number and status columns too narrow for their cells take room from the text columns, if they have it
        (a table in a column: typed in after it went in). True: widths changed."""
        text = [c for c, column in enumerate(self.columns) if column[1] == "text"]
        short = {c: self.need_width(c) - column[2] for c, column in enumerate(self.columns) if column[1] != "text"}
        short = {c: gap for c, gap in short.items() if gap > 0.5}
        wanted = sum(short.values())
        if wanted <= 0.5:
            return False
        give = wanted - self._take(wanted, ((text, lambda c: TEXT_COLUMN_EASY), (text, lambda c: MIN_TABLE_COLUMN)))
        if give <= 0.5:
            return False
        for c, gap in short.items():
            self.columns[c][2] += give * gap / wanted
        return True

    def _take(self, need: float, stages) -> float:
        """Narrow columns by `need` in all: each stage's columns (columns, floor(column)) share it, down to their
        floors, before the next stage's. What's still needed after them all."""
        for group, floor in stages:
            room = {c: max(0.0, self.columns[c][2] - floor(c)) for c in group}
            total = sum(room.values())
            if need <= 1e-9 or total <= 0:
                continue
            take = min(need, total)
            for c in group:
                self.columns[c][2] -= take * room[c] / total
            need -= take
        return need

    def least_width(self, column: int) -> float:
        """How narrow a number or status column can go before its cells (and its name) don't fit."""
        return min(self.columns[column][2], self.need_width(column))

    def need_width(self, column: int) -> float:
        """How wide a column's cells (and its name) need it to be, unwrapped (text columns: the least they go to)."""
        name, kind = self.columns[column][:2]
        if kind == "text":
            return MIN_TABLE_COLUMN
        need = QFontMetricsF(self.head_font()).horizontalAdvance(name.upper())
        if kind == "number":
            bold = QFont(self.cell_font())
            bold.setWeight(QFont.Weight.DemiBold)
            metrics = QFontMetricsF(self.cell_font())
            need = max([need, QFontMetricsF(bold).horizontalAdvance(self.column_total(column))]
                       + [metrics.horizontalAdvance(row[column]) for row in self.rows])
        elif kind == "status":
            metrics = QFontMetricsF(self.chip_font())
            need = max([need] + [metrics.horizontalAdvance(row[column]) + 30 for row in self.rows if row[column]])
        return min(MAX_TABLE_COLUMN, max(MIN_TABLE_COLUMN, need + 2 * CELL_PAD_X + 2))

    def set_column_width(self, index: int, width: float) -> None:
        width = min(MAX_TABLE_COLUMN, max(MIN_TABLE_COLUMN, width))
        if 0 <= index < len(self.columns) and width != self.columns[index][2]:
            self.columns[index][2] = width
            self.lay_out()

    def column_left(self, index: int) -> float:
        return TABLE_PAD + sum(c[2] for c in self.columns[:index])

    def align_of(self, column: int) -> str:
        """"left", "center" or "right": the column's own, or its kind's (numbers right, the rest left)."""
        return self.columns[column][3] or ("right" if self.columns[column][1] == "number" else "left")

    def align_flag(self, column: int) -> Qt.AlignmentFlag:
        return {"left": Qt.AlignmentFlag.AlignLeft, "center": Qt.AlignmentFlag.AlignHCenter,
                "right": Qt.AlignmentFlag.AlignRight}[self.align_of(column)]

    def _aligned(self, box: QRectF, width: float, column: int) -> float:
        """The left of something width wide, put in box the way the column aligns."""
        align = self.align_of(column)
        if align == "center":
            return box.center().x() - width / 2
        return box.right() - width if align == "right" else box.left()

    def title_shown(self) -> bool:
        return bool(self.title()) or self.title_item.editing()

    def _text_height(self, text: str, font: QFont, width: float) -> float:
        box = QFontMetricsF(font).boundingRect(QRectF(0, 0, max(1.0, width), 1e7), int(Qt.TextFlag.TextWordWrap), text or " ")
        return max(box.height(), QFontMetricsF(font).height())

    def lay_out(self) -> None:
        """Where everything goes: the title, the header, each row (as tall as its tallest cell), the total row."""
        width = 2 * TABLE_PAD + sum(c[2] for c in self.columns)
        margin = COLUMN_TITLE_MARGIN
        self.title_item.setVisible(self.title_shown())
        self.title_item.setPos(TABLE_PAD - margin, TABLE_PAD - margin - 2)
        self.title_item.setTextWidth(max(40.0, width - 2 * TABLE_PAD + 2 * margin))
        y = TABLE_PAD
        if self.title_shown():
            y += self.title_item.boundingRect().height() - 2 * margin + 6
        inner = width - 2 * TABLE_PAD
        if self.header:
            head = QFontMetricsF(self.head_font()).height() + 2 * CELL_PAD_Y
            self._head = QRectF(TABLE_PAD, y, inner, head)
            y += head + TABLE_HEAD_RULE
        else:
            self._head = QRectF(TABLE_PAD, y, inner, 0)
        font = self.cell_font()
        chip = QFontMetricsF(self.chip_font()).height() + 6
        self._tops, self._heights = [], []
        for r, row in enumerate(self.rows):
            tallest = max(chip, QFontMetricsF(font).height())
            for c, (cell, column) in enumerate(zip(row, self.columns)):
                kind, w = column[1], column[2]
                if kind != "status":
                    if self.editing_cell == (r, c):
                        tallest = max(tallest, self.editor.boundingRect().height() - 2 * margin)
                    tallest = max(tallest, self._text_height(cell, font, w - 2 * CELL_PAD_X))
            self._tops.append(y)
            self._heights.append(tallest + 2 * CELL_PAD_Y)
            y += tallest + 2 * CELL_PAD_Y
        self._total = None
        if self.rows and any(column[1] == "number" for column in self.columns):
            total = QFontMetricsF(font).height() + 2 * CELL_PAD_Y
            self._total = QRectF(TABLE_PAD, y, inner, total)
            y += total
        height = y + TABLE_PAD * 0.6
        if (width, height) != (self.width, self.height):
            self.prepareGeometryChange()
            self.width, self.height = width, height
        self._place_editor()
        self.update()

    # ------------------ where things are (its own units) ------------------
    def cell_rect(self, row: int, column: int) -> QRectF:
        """A cell (row -1: the header's)."""
        left, width = self.column_left(column), self.columns[column][2]
        if row < 0:
            return QRectF(left, self._head.top(), width, self._head.height())
        return QRectF(left, self._tops[row], width, self._heights[row])

    def text_rect(self, row: int, column: int) -> QRectF:
        return self.cell_rect(row, column).adjusted(CELL_PAD_X, CELL_PAD_Y, -CELL_PAD_X, -CELL_PAD_Y)

    def body_rect(self) -> QRectF:
        """The header, rows and total together."""
        bottom = self._total.bottom() if self._total is not None else (self._tops[-1] + self._heights[-1] if self.rows else self._head.bottom())
        return QRectF(TABLE_PAD, self._head.top(), self.width - 2 * TABLE_PAD, bottom - self._head.top())

    def title_rect(self) -> QRectF:
        return QRectF(0, 0, self.width, self._head.top()) if self.title_shown() else QRectF()

    def cell_at(self, point: QPointF) -> Optional[tuple[int, int]]:
        """The cell (row, column) at a point (row -1: the header); None: none there."""
        if not (TABLE_PAD <= point.x() < self.width - TABLE_PAD):
            return None
        column = next((c for c in range(len(self.columns)) if point.x() < self.column_left(c + 1)), len(self.columns) - 1)
        if self.header and self._head.top() <= point.y() < self._head.bottom() + TABLE_HEAD_RULE:
            return -1, column
        for r, (top, height) in enumerate(zip(self._tops, self._heights)):
            if top <= point.y() < top + height:
                return r, column
        return None

    def row_at(self, y: float) -> Optional[int]:
        for r, (top, height) in enumerate(zip(self._tops, self._heights)):
            if top <= y < top + height:
                return r
        return None

    def drop_index(self, y: float) -> int:
        """Where a row let go at height y would go (the row it would go above; len(rows): under the last)."""
        return next((r for r, (top, height) in enumerate(zip(self._tops, self._heights)) if y < top + height / 2), len(self.rows))

    def chip_rect(self, row: int, column: int) -> Optional[QRectF]:
        """A status cell's chip (an empty one: a small one, to click)."""
        if row < 0 or self.columns[column][1] != "status":
            return None
        cell = self.cell_rect(row, column)
        name = self.rows[row][column]
        metrics = QFontMetricsF(self.chip_font())
        height = metrics.height() + 6
        width = (metrics.horizontalAdvance(name) + 28) if name else 34.0
        width = min(width, cell.width() - 2 * (CELL_PAD_X - 2))
        left = self._aligned(cell.adjusted(CELL_PAD_X - 2, 0, -(CELL_PAD_X - 2), 0), width, column)
        return QRectF(left, cell.center().y() - height / 2, width, height)

    def grip_rect(self, row: int) -> QRectF:
        """A row's grip, in the card's left margin."""
        return QRectF(1.0, self._tops[row] + self._heights[row] / 2 - 9, TABLE_PAD - 2, 18)

    def edge_at(self, point: QPointF, reach: float) -> Optional[int]:
        """The column whose right edge is within reach of a point (in the header and rows)."""
        body = self.body_rect()
        if not (body.top() <= point.y() <= body.bottom()):
            return None
        for c in range(len(self.columns)):
            if abs(point.x() - self.column_left(c + 1)) <= reach:
                return c
        return None

    def plus_rect(self, which: str) -> QRectF:
        """The + bar past the last row ("row") or the last column ("column")."""
        body = self.body_rect()
        if which == "row":
            return QRectF(TABLE_PAD, self.height + TABLE_PLUS_GAP, self.width - 2 * TABLE_PAD, TABLE_PLUS)
        return QRectF(self.width + TABLE_PLUS_GAP, body.top(), TABLE_PLUS, max(TABLE_PLUS, body.height()))

    def set_hover(self, hovered: bool, plus: Optional[str] = None, row: Optional[int] = None, edge: Optional[int] = None) -> None:
        if (hovered, plus, row, edge) != (self.hovered, self.plus_hot, self.hover_row, self.edge_hot):
            if hovered != self.hovered:
                self.prepareGeometryChange()  # (its shape: the + bars come and go)
            self.hovered, self.plus_hot, self.hover_row, self.edge_hot = hovered, plus, row, edge
            self.update()

    def full_rect(self) -> QRectF:
        return QRectF(0, 0, self.width, self.height)

    def crop_rect(self) -> QRectF:
        return self.full_rect()

    def image(self) -> None:
        return None

    def boundingRect(self) -> QRectF:
        return self.full_rect().united(self.plus_rect("row")).united(self.plus_rect("column")).adjusted(-2, -2, 2, 2)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(self.full_rect(), CARD_RADIUS, CARD_RADIUS)
        if self.hovered:  # (the + bars and the gap to them: the mouse crossing it stays on the table)
            row, column = self.plus_rect("row"), self.plus_rect("column")
            path.addRoundedRect(row.adjusted(0, -TABLE_PLUS_GAP - 1, 0, 0), TABLE_PLUS / 2, TABLE_PLUS / 2)
            path.addRoundedRect(column.adjusted(-TABLE_PLUS_GAP - 1, 0, 0, 0), TABLE_PLUS / 2, TABLE_PLUS / 2)
        return path.simplified()

    # ------------------ totals and statuses ------------------
    def column_total(self, column: int) -> str:
        values = [v for v in (cell_number(row[column]) for row in self.rows) if v is not None]
        return format_total(values) if values else ""

    def status_color(self, name: str) -> QColor:
        color = dict(self.statuses).get(name)
        return QColor(color) if color else theme_color("text_faint")

    def status_counts(self, column: int) -> list[tuple[str, int]]:
        """How many rows have each status (in the statuses' order; the ones in use)."""
        cells = [row[column] for row in self.rows]
        return [(name, cells.count(name)) for name, _color in self.statuses if cells.count(name)]

    # ------------------ typing in a cell ------------------
    def start_cell_edit(self, cell: tuple[int, int], select_all: bool) -> None:
        """Type in a cell (row -1: a column's name): the editor goes over it with its text."""
        row, column = cell
        self.editing_cell = None
        font = self.head_font() if row < 0 else self.cell_font()
        self.editor.setFont(font)
        option = QTextOption(self.align_flag(column))
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.editor.document().setDefaultTextOption(option)
        self.editor.setDefaultTextColor(theme_color("text_faint") if row < 0 else theme_color("text"))
        self.editor.setPlainText(self.columns[column][0] if row < 0 else self.rows[row][column])
        self.editing_cell = cell
        self.current = cell
        self.editor.show()
        self._place_editor()
        self.editor.start_editing()
        if not select_all:
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.editor.setTextCursor(cursor)
        self.lay_out()

    def stop_cell_edit(self) -> None:
        """Done typing: the cell keeps its text (trimmed; a column's name, on one line)."""
        cell, self.editing_cell = self.editing_cell, None
        self.editor.stop_editing()
        self.editor.hide()
        if cell is not None:
            row, column = cell
            if row < 0:
                self.columns[column][0] = " ".join(self.columns[column][0].split())[:MAX_TABLE_TITLE] or f"Column {column + 1}"
            elif 0 <= row < len(self.rows):
                self.rows[row][column] = self.rows[row][column].strip()
        self.lay_out()

    def _on_editor_changed(self) -> None:
        """Typing: the cell takes the text as it's typed (its row grows or shrinks to fit)."""
        if self.editing_cell is None:
            return
        row, column = self.editing_cell
        text = self.editor.toPlainText()[:MAX_CELL_TEXT]
        if row < 0:
            self.columns[column][0] = text.replace("\n", " ")[:MAX_TABLE_TITLE]
        elif 0 <= row < len(self.rows):
            self.rows[row][column] = text
        self.lay_out()

    def _place_editor(self) -> None:
        cell = self.editing_cell
        if cell is None or cell[1] >= len(self.columns) or cell[0] >= len(self.rows):
            return
        margin = COLUMN_TITLE_MARGIN
        box = self.text_rect(*cell) if cell[0] >= 0 else self.cell_rect(*cell).adjusted(CELL_PAD_X, CELL_PAD_Y, -CELL_PAD_X, -CELL_PAD_Y)
        self.editor.setTextWidth(max(10.0, box.width() + 2 * margin))
        self.editor.setPos(box.left() - margin, box.top() - margin)

    # ------------------ painting ------------------
    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        if getattr(self, "lifted", False):
            painter.setOpacity(painter.opacity() * LIFTED_OPACITY)  # (dragged over a column: its line shows through)
        live = widget is not None  # (on screen: not in an export or a page's thumbnail)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paper, edge = card_colors(None)
        painter.setPen(QPen(edge, 1.2))
        painter.setBrush(paper)
        painter.drawRoundedRect(self.full_rect().adjusted(0.6, 0.6, -0.6, -0.6), CARD_RADIUS, CARD_RADIUS)
        paint_strip(painter, self.full_rect().adjusted(0.6, 0.6, -0.6, -0.6), self.strip)
        rule = QColor(theme_color("text_faint"))
        rule.setAlphaF(TABLE_RULE_ALPHA)
        inner_left, inner_right = TABLE_PAD, self.width - TABLE_PAD
        # the header: the column names over the accent line
        if self.header:
            painter.setFont(self.head_font())
            painter.setPen(theme_color("text_faint"))
            for c, column in enumerate(self.columns):
                if self.editing_cell == (-1, c):
                    continue
                painter.drawText(self.cell_rect(-1, c).adjusted(CELL_PAD_X, 0, -CELL_PAD_X, 0),
                                 self.align_flag(c) | Qt.AlignmentFlag.AlignVCenter, column[0])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme_color("accent"))
            painter.drawRoundedRect(QRectF(inner_left, self._head.bottom(), inner_right - inner_left, TABLE_HEAD_RULE),
                                    TABLE_HEAD_RULE / 2, TABLE_HEAD_RULE / 2)
        # the rows
        font = self.cell_font()
        for r, row in enumerate(self.rows):
            top, height = self._tops[r], self._heights[r]
            if live and self.hover_row == r and self.drop_row is None:
                tint = QColor(theme_color("text"))
                tint.setAlphaF(TABLE_HOVER_ALPHA)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(tint)
                painter.drawRoundedRect(QRectF(inner_left, top + 1, inner_right - inner_left, height - 2), 6, 6)
            for c, (cell, column) in enumerate(zip(row, self.columns)):
                if column[1] == "status":
                    self._paint_chip(painter, r, c, live)
                    continue
                if self.editing_cell == (r, c):
                    continue
                painter.setFont(font)
                painter.setPen(theme_color("text"))
                painter.drawText(self.text_rect(r, c), int(self.align_flag(c) | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap), cell)
            if r < len(self.rows) - 1 or self._total is not None:
                painter.setPen(QPen(rule, 1.0))
                painter.drawLine(QPointF(inner_left, top + height), QPointF(inner_right, top + height))
        # the total row: the sums of the number columns, how many of each status
        if self._total is not None:
            bold = QFont(font)
            bold.setWeight(QFont.Weight.DemiBold)
            painter.setFont(bold)
            labelled = False
            for c, column in enumerate(self.columns):
                kind, w = column[1], column[2]
                box = QRectF(self.column_left(c) + CELL_PAD_X, self._total.top(), w - 2 * CELL_PAD_X, self._total.height())
                if kind == "number":
                    painter.setPen(theme_color("text_strong"))
                    painter.drawText(box, self.align_flag(c) | Qt.AlignmentFlag.AlignVCenter, self.column_total(c))
                elif kind == "status":
                    self._paint_counts(painter, c, box)
                    painter.setFont(bold)
                elif not labelled:
                    painter.setPen(theme_color("text_muted"))
                    painter.drawText(box, self.align_flag(c) | Qt.AlignmentFlag.AlignVCenter, "Total")
                labelled = True
        # the cell being typed in, or the one the bar works on
        if live and (self.editing_cell is not None or (self.isSelected() and self.current[0] < len(self.rows))):
            cell = self.editing_cell or self.current
            if cell[0] >= 0 or self.header:
                box = self.cell_rect(*cell).adjusted(2, 3, -2, -3)
                painter.setPen(QPen(theme_color("accent") if self.editing_cell else theme_color("accent_border"),
                                    1.6 if self.editing_cell else 1.2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(box, 5, 5)
        # dragging a column's edge (or about to): an accent line down it
        if live and self.edge_hot is not None and self.edge_hot < len(self.columns):
            x = self.column_left(self.edge_hot + 1)
            body = self.body_rect()
            painter.setPen(QPen(theme_color("accent"), 2.0))
            painter.drawLine(QPointF(x, body.top()), QPointF(x, body.bottom()))
        # a row being dragged: an accent line where it would go
        if self.drop_row is not None and self.rows:
            y = self._tops[self.drop_row] if self.drop_row < len(self.rows) else self._tops[-1] + self._heights[-1]
            painter.setPen(QPen(theme_color("accent"), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(inner_left, y), QPointF(inner_right, y))
        if live and self.hovered:
            if self.hover_row is not None and self.hover_row < len(self.rows):  # the row's grip (drag it up or down)
                grip = self.grip_rect(self.hover_row).center()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme_color("text_faint"))
                for dx in (-2.5, 2.5):
                    for dy in (-5.0, 0.0, 5.0):
                        painter.drawEllipse(grip + QPointF(dx, dy), 1.4, 1.4)
            for which in ("row", "column"):  # the + bars: a row, a column
                box = self.plus_rect(which)
                hot = self.plus_hot == which
                pen = QPen(theme_color("accent_border"), 1.2, Qt.PenStyle.SolidLine if hot else Qt.PenStyle.DashLine)
                painter.setPen(pen)
                fill = QColor(theme_color("accent"))
                fill.setAlphaF(0.22 if hot else 0.10)
                painter.setBrush(fill)
                painter.drawRoundedRect(box, TABLE_PLUS / 2, TABLE_PLUS / 2)
                c = box.center()
                painter.setPen(QPen(theme_color("accent"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawLine(c - QPointF(4.5, 0), c + QPointF(4.5, 0))
                painter.drawLine(c - QPointF(0, 4.5), c + QPointF(0, 4.5))
        painter.restore()

    def _paint_chip(self, painter: QPainter, row: int, column: int, live: bool) -> None:
        chip = self.chip_rect(row, column)
        name = self.rows[row][column]
        if chip is None:
            return
        if not name:  # (empty: a faint dashed chip, to click)
            if live and (self.hover_row == row or self.isSelected()):
                pen = QPen(theme_color("text_faint"), 1.0, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(chip.adjusted(0.5, 0.5, -0.5, -0.5), chip.height() / 2, chip.height() / 2)
            return
        color = self.status_color(name)
        fill = QColor(color)
        fill.setAlphaF(0.24)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(chip, chip.height() / 2, chip.height() / 2)
        painter.setBrush(color)
        painter.drawEllipse(QPointF(chip.left() + 10, chip.center().y()), 3.2, 3.2)
        painter.setFont(self.chip_font())
        painter.setPen(theme_color("text_strong"))
        metrics = QFontMetricsF(self.chip_font())
        text = metrics.elidedText(name, Qt.TextElideMode.ElideRight, chip.width() - 26)
        painter.drawText(chip.adjusted(18, 0, -6, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    def _paint_counts(self, painter: QPainter, column: int, box: QRectF) -> None:
        """A status column's total: a dot in each status's colour and how many rows have it."""
        font = self.chip_font()
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        counts = self.status_counts(column)
        whole = sum(10 + metrics.horizontalAdvance(str(count)) + 10 for _name, count in counts) - 10
        x = max(box.left(), self._aligned(box, whole, column))
        for name, count in counts:
            text = str(count)
            width = 10 + metrics.horizontalAdvance(text)
            if x + width > box.right():
                break
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.status_color(name))
            painter.drawEllipse(QPointF(x + 3.5, box.center().y()), 3.5, 3.5)
            painter.setPen(theme_color("text_muted"))
            painter.drawText(QRectF(x + 10, box.top(), width, box.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            x += width + 10


CARD_KINDS = ("link", "file", "page_card")  # a link to a web page, a library file, another Artbook page
CARD_STYLES = ("big", "compact")  # a picture card, or a compact row
CARD_WIDTH = {"big": 320.0, "compact": 420.0}  # a new card's width in each style
MIN_CARD_WIDTH = {"big": 200.0, "compact": 260.0}
MAX_CARD_WIDTH = 2000.0
CARD_PAD = 14.0
CARD_PICTURE_RATIO = {"link": 0.52, "file": 0.62, "page_card": 0.56}  # a big card's picture: its height, of its width
CARD_ROW_HEIGHT = {"link": 104.0, "file": 100.0, "page_card": 84.0}  # a compact card's height
CARD_TITLE_PT = 12.5
CARD_BODY_PT = 10.5
CARD_SMALL_PT = 9.5
CARD_BUTTON_HEIGHT = 30.0
CARD_PICTURE_SIDE = 720  # a link's (or file's) picture is kept this big at most
CARD_ICON_SIDE = 128  # a link's site icon (favicon) is kept this big at most
MAX_CARD_TEXT = 600
MAX_URL = 4000


def _wrapped(text: str, font: QFont, width: float, lines: int) -> list[str]:
    """text in at most `lines` lines of `width` (word by word; the last one cut short with an ellipsis)."""
    metrics = QFontMetricsF(font)
    words = text.split()
    out: list[str] = []
    while words and len(out) < lines:
        line = words.pop(0)
        while words and metrics.horizontalAdvance(line + " " + words[0]) <= width:
            line += " " + words.pop(0)
        if metrics.horizontalAdvance(line) > width or (len(out) == lines - 1 and words):
            line = metrics.elidedText(line + (" " + " ".join(words) if words else ""), Qt.TextElideMode.ElideRight, width)
            words = []
        out.append(line)
    return out


def card_size_text(size: int) -> str:
    """48 MB, 312 KB..."""
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") or value >= 10 else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class RefCardItem(QGraphicsItem):
    """A card that stands for something elsewhere: a web page (kind "link": its picture, title, a line about it,
    its site), a file in the library ("file": its thumbnail or its app's icon, name, type, size, folder) or another
    Artbook page ("page_card": its picture, name, what's on it). Each comes big (a picture card) or compact (a row);
    the board remembers the look last used for each kind. Double-clicking it opens what it stands for; hovering it
    shows its buttons (BoardView._card_control_at). Its origin is its top-left; it moves, scales and stacks like a
    note (it doesn't turn), and goes in columns."""

    def __init__(self, item_id: str, kind: str, style: str = "big", data: Optional[dict[str, Any]] = None):
        super().__init__()
        self.item_id = item_id
        self.kind = kind if kind in CARD_KINDS else "link"
        self.style = style if style in CARD_STYLES else "big"
        self.crop = None  # (image-only things: cards have none)
        self.gray = False
        self.cropping = False
        self.group: Optional[str] = None
        self.data: dict[str, Any] = {}  # url, title, about, site, favicon / path, name, size / page, name; picture (a stored image)
        self.width = CARD_WIDTH[self.style]
        self.height = 0.0
        self.image: Optional[QImage] = None  # its picture, as loaded (the board loads it)
        self.image_blob: Optional[str] = None  # ...from this stored picture (a different one saved: loaded again)
        self.icon: Optional[QImage] = None  # a file card without a picture: its app's icon (the board reads it)
        self.favicon: Optional[QImage] = None  # a link card: its site's icon, as loaded...
        self.favicon_blob: Optional[str] = None  # ...from this stored picture
        self.missing = False  # a file card whose file isn't there; a page card whose page is gone
        self.page_info = ""  # a page card: what's on the page ("24 images, 9 cards")
        self.hovered = False
        self.hot: Optional[str] = None  # the button under the mouse
        self.apps: list[tuple[str, str]] = []  # a file card: (app id, name) it opens in, the main one first
        self.set_data(data or {})
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.lay_out()

    def apply_theme(self) -> None:
        self.update()

    # ------------------ state ------------------
    def set_data(self, data: dict[str, Any]) -> None:
        clean: dict[str, Any] = {}
        for key in ("url", "title", "about", "site", "favicon", "path", "name", "page", "picture", "ext"):
            value = data.get(key)
            if isinstance(value, str) and value:
                clean[key] = value[:MAX_URL if key in ("url", "path") else MAX_CARD_TEXT]
        size = data.get("size")
        if isinstance(size, (int, float)) and not isinstance(size, bool) and math.isfinite(size) and size >= 0:
            clean["size"] = int(size)
        self.data = clean

    def state(self) -> ItemState:
        return ItemState(
            self.pos().x(), self.pos().y(), self.scale(), 0.0, False, False, self.zValue(), None,
            round(self.opacity(), 4), False, self.width, group=self.group,
            card=(self.style, tuple(sorted(self.data.items()))),
        )

    def apply_state(self, state: ItemState) -> None:
        self.setPos(state.x, state.y)
        self.setScale(state.scale)
        self.setZValue(state.z)
        self.setOpacity(state.opacity)
        self.group = state.group
        if state.card is not None:
            style, data = state.card
            self.style = style if style in CARD_STYLES else self.style
            self.set_data(dict(data))
        if state.width is not None:
            self.width = min(MAX_CARD_WIDTH, max(MIN_CARD_WIDTH[self.style], state.width))
        self.lay_out()

    def to_dict(self) -> dict[str, Any]:
        s = self.state()
        return {
            "id": self.item_id, "kind": self.kind, "style": self.style, "width": self.width, **self.data,
            "group": self.group, "x": s.x, "y": s.y, "scale": s.scale, "z": s.z, "opacity": s.opacity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RefCardItem":
        def number(value: Any, default: float) -> float:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return default
            return value if math.isfinite(value) else default

        card = cls(str(data.get("id") or uuid.uuid4().hex), str(data.get("kind")), str(data.get("style")), data)
        if card.data.get("url") and not _is_web_url(card.data["url"]):
            card.data.pop("url")  # (only web addresses open from a card)
        opacity = min(1.0, max(MIN_OPACITY / 100, number(data.get("opacity"), 1.0)))
        card.apply_state(ItemState(
            number(data.get("x"), 0.0), number(data.get("y"), 0.0), number(data.get("scale"), 1.0) or 1.0,
            z=number(data.get("z"), 0.0), opacity=opacity, group=_clean_group(data.get("group")),
            width=number(data.get("width"), CARD_WIDTH[card.style]), card=(card.style, tuple(sorted(card.data.items()))),
        ))
        return card

    # ------------------ what it says ------------------
    def title(self) -> str:
        if self.kind == "link":
            return self.data.get("title") or self.data.get("url", "")
        return self.data.get("name") or (Path(self.data["path"]).name if self.data.get("path") else "Page")

    def subtitle(self) -> str:
        """The faint line: a link's site, a file's folder, what's on a page."""
        if self.kind == "link":
            return self.data.get("site") or urllib.parse.urlsplit(self.data.get("url", "")).hostname or ""
        if self.kind == "file":
            return "File not found: Relink..." if self.missing else str(Path(self.data.get("path", "")).parent)
        return "This page is gone" if self.missing else self.page_info

    def chips(self) -> list[str]:
        """A file card's type and size."""
        if self.kind != "file":
            return []
        ext = (self.data.get("ext") or Path(self.data.get("path", "")).suffix).lstrip(".").upper()
        return [text for text in (ext, card_size_text(self.data["size"]) if "size" in self.data and not self.missing else "") if text]

    # ------------------ size ------------------
    def textWidth(self) -> float:  # (named as a note's, so the side handles widen it the same way)
        return self.width

    def setTextWidth(self, width: float) -> None:
        width = min(MAX_CARD_WIDTH, max(MIN_CARD_WIDTH[self.style], float(width)))
        if width != self.width:
            self.width = width
            self.lay_out()

    def set_style(self, style: str) -> None:
        """Big or compact (at that style's width)."""
        if style in CARD_STYLES and style != self.style:
            self.style = style
            self.width = CARD_WIDTH[style]
            self.lay_out()

    def fonts(self) -> tuple[QFont, QFont, QFont]:
        """(title, body, small)."""
        base = QFont(QApplication.font())
        title, body, small = QFont(base), QFont(base), QFont(base)
        title.setPointSizeF(CARD_TITLE_PT)
        title.setWeight(QFont.Weight.DemiBold)
        body.setPointSizeF(CARD_BODY_PT)
        small.setPointSizeF(CARD_SMALL_PT)
        return title, body, small

    def picture_rect(self) -> QRectF:
        if self.style == "compact":
            side = self.height - 16
            if self.kind == "link":  # (a link's small picture is on the right)
                return QRectF(self.width - 8 - side * 1.2, 8, side * 1.2, side)
            return QRectF(8, 8, side * (1.3 if self.kind == "page_card" else 1.0), side)
        if self.kind == "link":  # (a link's picture fills the card's top, edge to edge)
            return QRectF(0, 0, self.width, self.width * CARD_PICTURE_RATIO["link"])
        return QRectF(8, 8, self.width - 16, (self.width - 16) * CARD_PICTURE_RATIO[self.kind])

    def text_rect(self) -> QRectF:
        """Where its words go."""
        picture = self.picture_rect()
        if self.style == "compact":
            if self.kind == "link":
                return QRectF(CARD_PAD, 10, picture.left() - CARD_PAD - 10, self.height - 20)
            left = picture.right() + 12
            return QRectF(left, 10, self.width - left - (40 if self.kind == "page_card" else 46), self.height - 20)
        return QRectF(CARD_PAD, picture.bottom() + 10, self.width - 2 * CARD_PAD, 0)

    def lay_out(self) -> None:
        title, body, small = self.fonts()
        self.prepareGeometryChange()
        if self.style == "compact":
            height = CARD_ROW_HEIGHT[self.kind]
        else:  # (a big card's picture doesn't depend on its height)
            words = self.width - 2 * CARD_PAD
            height = self.picture_rect().bottom() + 10
            lines = len(_wrapped(self.title(), title, words, 2 if self.kind == "link" else 1))
            height += lines * QFontMetricsF(title).lineSpacing() + 6
            if self.kind == "link" and self.data.get("about"):
                height += len(_wrapped(self.data["about"], body, words, 2)) * QFontMetricsF(body).lineSpacing() + 6
            if self.kind == "file":
                height += 22 + 6
            height += QFontMetricsF(small).lineSpacing() + CARD_PAD
        self.height = height
        self.update()

    def full_rect(self) -> QRectF:
        return QRectF(0, 0, self.width, self.height)

    def crop_rect(self) -> QRectF:
        return self.full_rect()

    def boundingRect(self) -> QRectF:
        return self.full_rect().adjusted(-2, -2, 2, 2)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(self.full_rect(), CARD_RADIUS, CARD_RADIUS)
        return path

    # ------------------ its buttons (hovering it) ------------------
    def controls(self) -> list[tuple[str, QRectF, str]]:
        """(what, where, its label) for the buttons that show while it's hovered: "open" (a link, a page, a
        compact file card: the round button on its corner), a file card's "app:<id>" and "more", "relink"."""
        if self.kind == "file" and self.missing:
            box = self.picture_rect()
            return [("relink", QRectF(box.center().x() - 50, box.bottom() - CARD_BUTTON_HEIGHT - 8, 100, CARD_BUTTON_HEIGHT), "Relink...")]
        if self.kind == "file" and self.style == "big":
            _title, _body, small = self.fonts()
            bold = QFont(small)
            bold.setWeight(QFont.Weight.DemiBold)
            metrics = QFontMetricsF(bold)
            box = self.picture_rect()
            x, y = box.left() + 8, box.bottom() - CARD_BUTTON_HEIGHT - 8
            out = []
            for n, (app_id, name) in enumerate(self.apps[:2] or [("", "Open")]):
                label = f"Open in {name}" if n == 0 and app_id else name
                width = metrics.horizontalAdvance(label) + 24
                if x + width > box.right() - 8 - CARD_BUTTON_HEIGHT - 6:
                    break
                out.append((f"app:{app_id}", QRectF(x, y, width, CARD_BUTTON_HEIGHT), label))
                x += width + 6
            out.append(("more", QRectF(x, y, CARD_BUTTON_HEIGHT, CARD_BUTTON_HEIGHT), ""))
            return out
        side = 30.0
        return [("open", QRectF(self.width - side - 8, 8, side, side), "")]

    def control_under(self, point: QPointF) -> Optional[str]:
        """The button at a point, whether it shows or not."""
        return next((what for what, box, _label in self.controls() if box.contains(point)), None)

    def control_at(self, point: QPointF) -> Optional[str]:
        """The button at a point, if it's showing (the card is hovered)."""
        return self.control_under(point) if self.hovered else None

    def set_hover(self, hovered: bool, hot: Optional[str] = None) -> None:
        if (hovered, hot) != (self.hovered, self.hot):
            self.hovered, self.hot = hovered, hot
            self.update()

    # ------------------ painting ------------------
    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        if getattr(self, "lifted", False):
            painter.setOpacity(painter.opacity() * LIFTED_OPACITY)  # (dragged over a column: its line shows through)
        live = widget is not None  # (on screen: not in an export or a page's thumbnail)
        painter.save()
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        paper, edge = card_colors(None)
        pen = QPen(edge, 1.2, Qt.PenStyle.DashLine if self.missing else Qt.PenStyle.SolidLine)
        painter.setPen(pen)
        painter.setBrush(paper)
        painter.drawRoundedRect(self.full_rect().adjusted(0.6, 0.6, -0.6, -0.6), CARD_RADIUS, CARD_RADIUS)
        self._paint_picture(painter)
        paint_strip(painter, self.full_rect().adjusted(0.6, 0.6, -0.6, -0.6), self.strip)
        title_font, body_font, small_font = self.fonts()
        box = self.text_rect()
        if self.kind == "link" and self.style == "compact":
            self._paint_site_icon(painter, QRectF(box.left(), box.top() + 2, 16, 16))
            self._text(painter, small_font, "text_faint", QRectF(box.left() + 22, box.top(), box.width() - 22, 20), self.subtitle())
            y = box.top() + 24
            for line in _wrapped(self.title(), title_font, box.width(), 2):
                self._text(painter, title_font, "text_strong", QRectF(box.left(), y, box.width(), 24), line)
                y += QFontMetricsF(title_font).lineSpacing()
        elif self.style == "compact":
            y = box.top() + 2
            self._text(painter, title_font, "text_strong", QRectF(box.left(), y, box.width(), 24),
                       _wrapped(self.title(), title_font, box.width(), 1)[0] if self.title() else "")
            y += QFontMetricsF(title_font).lineSpacing() + 4
            if self.kind == "file":
                self._paint_chips(painter, QPointF(box.left(), y))
                y += 28
            self._text(painter, small_font, "text_faint", QRectF(box.left(), y, box.width(), 20),
                       QFontMetricsF(small_font).elidedText(self.subtitle(), Qt.TextElideMode.ElideMiddle, box.width()))
            if self.kind == "page_card":
                arrow = QPointF(self.width - 22, self.height / 2)
                painter.setPen(QPen(theme_color("text_muted"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.drawPolyline([arrow + QPointF(-3, -6), arrow + QPointF(3, 0), arrow + QPointF(-3, 6)])
        else:
            y = box.top()
            left = box.left()
            if self.kind == "page_card":
                self._paint_mark(painter, "page", QRectF(left, y + 4, 16, 16))
                left += 22
            for line in _wrapped(self.title(), title_font, box.right() - left, 2 if self.kind == "link" else 1):
                self._text(painter, title_font, "text_strong", QRectF(left, y, box.right() - left, 24), line)
                y += QFontMetricsF(title_font).lineSpacing()
            y += 6
            if self.kind == "link" and self.data.get("about"):
                for line in _wrapped(self.data["about"], body_font, box.width(), 2):
                    self._text(painter, body_font, "text_muted", QRectF(box.left(), y, box.width(), 22), line)
                    y += QFontMetricsF(body_font).lineSpacing()
                y += 6
            if self.kind == "file":
                self._paint_chips(painter, QPointF(box.left(), y))
                y += 28
            left = box.left()
            if self.kind == "link":
                self._paint_site_icon(painter, QRectF(left, y + 1, 15, 15))
                left += 21
            self._text(painter, small_font, "text_faint", QRectF(left, y, box.right() - left, 20),
                       QFontMetricsF(small_font).elidedText(self.subtitle(), Qt.TextElideMode.ElideMiddle, box.right() - left))
        if live and (self.hovered or (self.kind == "file" and self.missing)):
            self._paint_controls(painter)
        painter.restore()

    def _text(self, painter: QPainter, font: QFont, ink: str, box: QRectF, text: str) -> None:
        painter.setFont(font)
        painter.setPen(theme_color(ink))
        painter.drawText(box, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextSingleLine), text)

    def _paint_mark(self, painter: QPainter, icon: str, box: QRectF) -> None:
        from ui.duotone import duotone_icon

        painter.drawPixmap(box.toRect(), duotone_icon(icon).pixmap(QSize(64, 64)))

    def has_favicon(self) -> bool:
        return self.kind == "link" and self.favicon is not None and not self.favicon.isNull()

    def _paint_site_icon(self, painter: QPainter, box: QRectF) -> None:
        """A link's site icon (its favicon), else the web mark."""
        if not self.has_favicon():
            self._paint_mark(painter, "web", box)
            return
        side = min(box.width(), box.height())
        target = QRectF(0, 0, side, side)
        icon = self.favicon
        if icon.width() != icon.height():  # (kept in proportion)
            target.setSize(QSizeF(side, side * icon.height() / icon.width()) if icon.width() > icon.height()
                           else QSizeF(side * icon.width() / icon.height(), side))
        target.moveCenter(box.center())
        painter.drawImage(target, icon)

    def _paint_chips(self, painter: QPainter, at: QPointF) -> None:
        _title, _body, small = self.fonts()
        bold = QFont(small)
        bold.setWeight(QFont.Weight.DemiBold)
        painter.setFont(bold)
        metrics = QFontMetricsF(bold)
        x = at.x()
        for text in self.chips():
            box = QRectF(x, at.y(), metrics.horizontalAdvance(text) + 16, 22)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme_color("accent_soft"))
            painter.drawRoundedRect(box, 11, 11)
            painter.setPen(theme_color("card_selected_text"))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
            x = box.right() + 6

    def _paint_picture(self, painter: QPainter) -> None:
        box = self.picture_rect()
        radius = CARD_RADIUS if (self.kind == "link" and self.style == "big") else 7.0
        path = QPainterPath()
        if self.kind == "link" and self.style == "big":  # (rounded at the card's top corners only)
            path.addRoundedRect(box.adjusted(0.6, 0.6, -0.6, radius), radius, radius)
            square = QPainterPath()
            square.addRect(box)
            path = path.intersected(square)
        else:
            path.addRoundedRect(box, radius, radius)
        painter.save()
        painter.setClipPath(path)
        if self.image is not None and not self.image.isNull() and not self.missing:
            fill = self.kind == "link"  # (a web page's picture fills its box; a model's render, a page's picture: whole)
            if not fill:
                painter.fillRect(box, theme_color("grid_bg") if self.kind == "page_card" else theme_color("surface"))
            scaled = self.image.scaled(
                (box.size() * 2).toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding if fill else Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            target = QRectF(0, 0, scaled.width() / 2, scaled.height() / 2)
            target.moveCenter(box.center())
            painter.drawImage(target, scaled)
        else:
            shade = QColor(theme_color("text_faint"))
            shade.setAlphaF(0.12)
            painter.fillRect(box, shade)
            side = min(box.width(), box.height()) * (0.5 if self.style == "big" else 0.6)
            mark = QRectF(0, 0, side, side)
            mark.moveCenter(box.center() - QPointF(0, 10 if self.missing and self.style == "big" and self.kind == "file" else 0))
            if self.missing:
                self._paint_mark(painter, "warning", mark.adjusted(side * 0.2, side * 0.2, -side * 0.2, -side * 0.2))
            elif self.kind == "file" and self.icon is not None and not self.icon.isNull():
                painter.drawImage(mark, self.icon)  # (the installed app's own icon)
            elif self.has_favicon():  # (a link without a picture: its site's icon, at most twice its own size)
                small = min(side * 0.55, max(self.favicon.width(), self.favicon.height()) * 2.0, 72.0)
                icon_box = QRectF(0, 0, small, small)
                icon_box.moveCenter(mark.center())
                self._paint_site_icon(painter, icon_box)
            else:
                self._paint_mark(painter, {"link": "web", "file": "cube", "page_card": "page"}[self.kind],
                                 mark.adjusted(side * 0.15, side * 0.15, -side * 0.15, -side * 0.15))
        painter.restore()

    def _paint_controls(self, painter: QPainter) -> None:
        _title, _body, small = self.fonts()
        bold = QFont(small)
        bold.setWeight(QFont.Weight.DemiBold)
        controls = self.controls()
        for what, box, label in controls:
            hot = self.hot == what
            main = what == controls[0][0] and what.startswith("app:")  # (the first app: the filled button)
            if what == "open":
                painter.setPen(QPen(theme_color("accent_border") if hot else theme_color("border"), 1))
                painter.setBrush(theme_color("hover") if hot else theme_color("raised"))
                painter.drawEllipse(box)
                self._paint_mark(painter, "open_in", box.adjusted(7, 7, -7, -7))
                continue
            painter.setPen(QPen(theme_color("accent") if main else theme_color("border"), 1))
            fill = theme_color("accent_hover" if hot else "accent") if main else theme_color("hover" if hot else "raised")
            painter.setBrush(fill)
            painter.drawRoundedRect(box, 8, 8)
            if what == "more":
                self._paint_mark(painter, "more", box.adjusted(7, 7, -7, -7))
                continue
            painter.setFont(bold)
            painter.setPen(theme_color("on_accent") if main else theme_color("text"))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)


SIDES = {"left": QPointF(-1, 0), "right": QPointF(1, 0), "top": QPointF(0, -1), "bottom": QPointF(0, 1)}  # (outwards)


def _unit(vector: QPointF) -> QPointF:
    length = math.hypot(vector.x(), vector.y())
    return vector / length if length > 1e-9 else QPointF(1, 0)


def facing(box: QRectF, toward: QPointF) -> str:
    """The side of box that faces a point (a line leaves from its middle): across the wider gap past its edges, so
    a wide card beside another joins it from its side, one above another from its top or bottom."""
    d = toward - box.center()
    if abs(d.x()) - box.width() / 2 >= abs(d.y()) - box.height() / 2:
        return "right" if d.x() >= 0 else "left"
    return "bottom" if d.y() >= 0 else "top"


def side_point(box: QRectF, side: str, gap: float) -> QPointF:
    """The middle of a side of box, gap out from it."""
    return box.center() + QPointF(SIDES[side].x() * (box.width() / 2 + gap), SIDES[side].y() * (box.height() / 2 + gap))


def rounded_polyline(points: list[QPointF], radius: float) -> QPainterPath:
    """Straight runs through points, the corners rounded."""
    path = QPainterPath(points[0])
    for i in range(1, len(points) - 1):
        before, corner, after = points[i - 1], points[i], points[i + 1]
        d1 = math.hypot(corner.x() - before.x(), corner.y() - before.y())
        d2 = math.hypot(after.x() - corner.x(), after.y() - corner.y())
        r = min(radius, d1 / 2, d2 / 2)
        if r <= 0.01:
            path.lineTo(corner)
            continue
        path.lineTo(corner + (before - corner) * (r / d1))
        path.quadTo(corner, corner + (after - corner) * (r / d2))
    path.lineTo(points[-1])
    return path


def line_path(style: str, a: QPointF, side_a: str, b: QPointF, side_b: str, bend: QPointF, size: float) -> tuple[QPainterPath, QPointF]:
    """A line's path from a (leaving a's side) to b (arriving at b's side), bent by bend; and its middle (where
    the bend handle and the label sit)."""
    if style == "straight":
        mid = (a + b) / 2
        path = QPainterPath(a)
        if bend.isNull():
            path.lineTo(b)
        else:
            path.quadTo(mid + bend * 2, b)  # (its middle moves by bend)
        return path, mid + bend
    if style == "elbow":
        across = ("left", "right")
        if side_a in across and side_b in across:
            x = (a.x() + b.x()) / 2 + bend.x()
            points, mid = [a, QPointF(x, a.y()), QPointF(x, b.y()), b], QPointF(x, (a.y() + b.y()) / 2)
        elif side_a not in across and side_b not in across:
            y = (a.y() + b.y()) / 2 + bend.y()
            points, mid = [a, QPointF(a.x(), y), QPointF(b.x(), y), b], QPointF((a.x() + b.x()) / 2, y)
        elif side_a in across:
            points = [a, QPointF(b.x(), a.y()), b]
            mid = points[1]
        else:
            points = [a, QPointF(a.x(), b.y()), b]
            mid = points[1]
        return rounded_polyline(points, ELBOW_RADIUS * size), mid
    reach = max(24.0 * size, math.hypot(b.x() - a.x(), b.y() - a.y()) * 0.4)
    shift = bend * (4.0 / 3.0)  # (moves the curve's middle by bend)
    c1, c2 = a + SIDES[side_a] * reach + shift, b + SIDES[side_b] * reach + shift
    path = QPainterPath(a)
    path.cubicTo(c1, c2, b)
    return path, (a + c1 * 3 + c2 * 3 + b) / 8


def head_length(width: float, size: float) -> float:
    return (6.0 + 2.2 * width) * size


def line_geometry(
    style: str, heads: str, width: float, size: float, bend: QPointF,
    box_a: Optional[QRectF], point_a: QPointF, box_b: Optional[QRectF], point_b: QPointF,
    sides: tuple[Optional[str], Optional[str]] = (None, None), rings: tuple[float, float] = (0.0, 0.0),
) -> tuple[QPainterPath, QPointF, tuple[tuple[QPointF, QPointF], tuple[QPointF, QPointF]]]:
    """(path, middle, ((start tip, its arrow's direction), (end tip, its direction))) for a line from box_a (a card;
    None: a free end at point_a) to box_b (or point_b). The ends leave the sides facing each other (sides: the side
    an end must leave, at a point on a line); the path stops short of an arrowhead's tip, which the arrowhead fills,
    and rings out from an end at a point (its ring's radius)."""
    toward_b = box_b.center() if box_b is not None else point_b
    toward_a = box_a.center() if box_a is not None else point_a
    side_a = sides[0] or facing(box_a if box_a is not None else QRectF(point_a, point_a), toward_b)
    side_b = sides[1] or facing(box_b if box_b is not None else QRectF(point_b, point_b), toward_a)
    gap = LINE_GAP * size
    a = side_point(box_a, side_a, gap) if box_a is not None else QPointF(point_a)
    b = side_point(box_b, side_b, gap) if box_b is not None else QPointF(point_b)
    if rings[0] or rings[1]:  # (from the edge of a point's ring)
        if style == "straight":
            control = (a + b) / 2 + bend * 2
            a, b = a + _unit(control - a) * rings[0], b + _unit(control - b) * rings[1]
        else:
            a, b = a + SIDES[side_a] * rings[0], b + SIDES[side_b] * rings[1]
    head = head_length(width, size)
    trim_a = head * 0.7 if heads == "both" else 0.0
    trim_b = head * 0.7 if heads in ("end", "both") else 0.0
    if style == "straight":
        control = (a + b) / 2 + bend * 2
        into_a, into_b = _unit(a - control), _unit(b - control)
        path, mid = line_path(style, a - into_a * trim_a, side_a, b - into_b * trim_b, side_b, bend, size)
        return path, mid, ((a, into_a), (b, into_b))
    path, mid = line_path(style, a + SIDES[side_a] * trim_a, side_a, b + SIDES[side_b] * trim_b, side_b, bend, size)
    return path, mid, ((a, -SIDES[side_a]), (b, -SIDES[side_b]))


def paint_line(painter: QPainter, path: QPainterPath, tips, color: QColor, width: float, size: float,
               heads: str, dashed: bool) -> None:
    """A line and its arrowheads (shared by LineItem and the line being drawn)."""
    pen = QPen(color, width * size, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    if dashed:
        pen.setStyle(Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([3.0, 2.5])  # (in line widths)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    length = head_length(width, size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for n, (tip, direction) in enumerate(tips):
        if heads == "both" or (heads == "end" and n == 1):
            across = QPointF(-direction.y(), direction.x()) * (length * 0.45)
            base = tip - direction * length
            painter.drawPolygon(QPolygonF([tip, base + across, base - across]))


GROUP_REF = "group:"  # a line's end joined to a group: "group:<group id>" (else an item's id)


def point_side(centre: QPointF, far: QPointF, ends: list[tuple[bool, QPointF]]) -> str:
    """The side a line leaves a point on a line by, toward far (its other end). Every line at a point leaves it along
    one axis, so a line goes through it in one sweep: the axis from where the lines into it come from to where the
    lines out of it go (ends: (into it?, the other end) for each line at the point)."""
    ins = [p for into, p in ends if into]
    outs = [p for into, p in ends if not into]
    if ins and outs:
        d = sum(outs, QPointF()) / len(outs) - sum(ins, QPointF()) / len(ins)
    elif len(ends) >= 2:
        d = ends[1][1] - ends[0][1]
    else:
        d = far - centre
    if abs(d.x()) >= abs(d.y()):
        return "right" if far.x() >= centre.x() else "left"
    return "bottom" if far.y() >= centre.y() else "top"


def nearest_on_path(path: QPainterPath, at: QPointF) -> tuple[float, QPointF]:
    """(how far along, 0 to 1; the point) of path's point nearest at."""
    def distance(t: float) -> float:
        p = path.pointAtPercent(t)
        return math.hypot(p.x() - at.x(), p.y() - at.y())

    best = min((n / POINT_SPOT_STEPS for n in range(POINT_SPOT_STEPS + 1)), key=distance)
    step = 1.0 / POINT_SPOT_STEPS
    best = min((min(1.0, max(0.0, best + step * (n - 10) / 10)) for n in range(21)), key=distance)
    return best, path.pointAtPercent(best)


@dataclass(frozen=True)
class GroupTarget:
    """A group as one end of a line: the line ends round its panel (all its members)."""
    gid: str

    @property
    def item_id(self) -> str:
        return GROUP_REF + self.gid


class LineItem(QGraphicsItem):
    """A line between two things on the board (cards, pictures, columns), or from one to a free point: curved,
    straight or elbow, an arrow at its end (or none, or both), its colour (None: the theme's), thickness, dashed,
    and a label in a pill on its middle. Its ends are (item id, x, y): attached to an item (it follows it about;
    BoardView.lay_out_lines works out the path), or free (item id None) at a point of the board. bend moves its
    middle. It stays at the board's origin, its path in board coordinates; size scales its thickness and label,
    like a note made at that zoom."""

    def __init__(self, item_id: str, start: tuple, end: tuple, size: float = 1.0):
        super().__init__()
        self.item_id = item_id
        self.crop = None  # (image-only things: lines have none)
        self.gray = False
        self.cropping = False
        self.group: Optional[str] = None
        self.start, self.end = start, end
        self.bend = QPointF()
        self.style = "curve"  # LINE_SHAPES
        self.heads = "end"  # LINE_ENDS
        self.color: Optional[str] = None
        self.width = 2  # LINE_WIDTHS
        self.dashed = False
        self.size = size
        self.path = QPainterPath()
        self.mid = QPointF()
        self.tips = ((QPointF(), QPointF(1, 0)), (QPointF(), QPointF(1, 0)))
        self.holes: list[tuple[QPointF, float]] = []  # round the points it ends at: clicks there are theirs
        self.label_item = InlineText(self, LINE_LABEL_PT, False)
        self.apply_theme()
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)

    def apply_theme(self) -> None:
        self.label_item.setDefaultTextColor(theme_color("text_strong"))
        self.update()

    # ------------------ state ------------------
    def label(self) -> str:
        return self.label_item.toPlainText()

    def set_label(self, text: str) -> None:
        if text != self.label():
            self.label_item.setPlainText(text)
            self.place_label()

    def state(self) -> ItemState:
        return ItemState(z=self.zValue(), opacity=round(self.opacity(), 4), line=(
            self.start, self.end, (self.bend.x(), self.bend.y()), self.style, self.heads, self.color, self.width,
            self.dashed, self.label(), self.size,
        ))

    def apply_state(self, state: ItemState) -> None:
        self.setZValue(state.z)
        self.setOpacity(state.opacity)
        if state.line is not None:
            start, end, bend, style, heads, color, width, dashed, label, size = state.line
            self.start, self.end, self.bend = start, end, QPointF(*bend)
            self.style, self.heads, self.color, self.width, self.dashed, self.size = style, heads, color, width, dashed, size
            self.set_label(label)
        self.update()

    def to_dict(self) -> dict[str, Any]:
        def end(value: tuple) -> dict[str, Any]:
            if value[0] and value[0].startswith(GROUP_REF):
                return {"item": None, "group": value[0][len(GROUP_REF):], "x": value[1], "y": value[2]}
            return {"item": value[0], "x": value[1], "y": value[2]}

        return {
            "id": self.item_id, "kind": "line", "start": end(self.start), "end": end(self.end),
            "bend": [self.bend.x(), self.bend.y()], "shape": self.style, "heads": self.heads, "color": self.color,
            "width": self.width, "dashed": self.dashed, "label": self.label(), "size": self.size,
            "z": self.zValue(), "opacity": round(self.opacity(), 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LineItem":
        def number(value: Any, default: float = 0.0) -> float:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return default
            return value if math.isfinite(value) else default

        def end(value: Any) -> tuple:
            value = value if isinstance(value, dict) else {}
            group = _clean_group(value.get("group"))
            item = value.get("item")
            item = item if isinstance(item, str) and 0 < len(item) <= 64 and not item.startswith(GROUP_REF) else None
            return (GROUP_REF + group if group else item), number(value.get("x")), number(value.get("y"))

        size = min(1000.0, max(0.001, number(data.get("size"), 1.0)))
        line = cls(str(data.get("id") or uuid.uuid4().hex), end(data.get("start")), end(data.get("end")), size)
        bend = data.get("bend") if isinstance(data.get("bend"), list) and len(data["bend"]) == 2 else [0, 0]
        line.bend = QPointF(number(bend[0]), number(bend[1]))
        line.style = data.get("shape") if data.get("shape") in LINE_SHAPES else "curve"
        line.heads = data.get("heads") if data.get("heads") in LINE_ENDS else "end"
        color = data.get("color")
        line.color = color if isinstance(color, str) and QColor(color).isValid() else None
        line.width = data.get("width") if data.get("width") in LINE_WIDTHS else 2
        line.dashed = data.get("dashed") is True
        label = data.get("label")
        line.set_label(" ".join(label.split())[:MAX_LINE_LABEL] if isinstance(label, str) else "")
        line.setZValue(number(data.get("z")))
        line.setOpacity(min(1.0, max(MIN_OPACITY / 100, number(data.get("opacity"), 1.0))))
        return line

    def attached(self) -> set[str]:
        """The ids of the items its ends are attached to."""
        return {end[0] for end in (self.start, self.end) if end[0]}

    # ------------------ shape ------------------
    def set_geometry(self, path: QPainterPath, mid: QPointF, tips) -> None:
        self.prepareGeometryChange()
        self.path, self.mid, self.tips = path, mid, tips
        self.place_label()

    def has_pill(self) -> bool:
        return bool(self.label()) or self.label_item.editing()

    def place_label(self) -> None:
        """The label on the line's middle."""
        self.prepareGeometryChange()
        self.label_item.setScale(self.size)
        box = self.label_item.boundingRect()
        self.label_item.setPos(self.mid - QPointF(box.width(), box.height()) * self.size / 2)
        self.label_item.setVisible(self.has_pill())
        self.update()

    def pill_rect(self) -> QRectF:
        box = self.label_item.mapRectToParent(self.label_item.boundingRect())
        return box.adjusted(-LINE_LABEL_PAD * self.size, 0, LINE_LABEL_PAD * self.size, 0)

    def handle_point(self) -> QPointF:
        """The bend handle: on the middle, or just above the label."""
        if self.has_pill():
            return QPointF(self.mid.x(), self.pill_rect().top() - 9 * self.size)
        return self.mid

    def full_rect(self) -> QRectF:
        return self.boundingRect()

    def crop_rect(self) -> QRectF:
        return self.boundingRect()

    def image(self) -> None:
        return None

    def boundingRect(self) -> QRectF:
        margin = (self.width + LINE_HIT + head_length(self.width, 1.0) + 6) * self.size
        rect = self.path.controlPointRect().adjusted(-margin, -margin, margin, margin)
        return rect.united(self.pill_rect()) if self.has_pill() else rect

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(max(self.width, LINE_HIT * 2) * self.size)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        path = stroker.createStroke(self.path)
        if self.holes:
            holes = QPainterPath()
            for centre, radius in self.holes:
                holes.addEllipse(centre, radius, radius)
            path = path.subtracted(holes)
        if self.has_pill():
            path.addRoundedRect(self.pill_rect(), 6 * self.size, 6 * self.size)
        return path

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isSelected():  # a soft accent glow under it
            glow = QColor(theme_color("accent"))
            glow.setAlphaF(0.35)
            painter.setPen(QPen(glow, (self.width + 6) * self.size, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(self.path)
        ink = QColor(self.color) if self.color else theme_color("text_muted")
        paint_line(painter, self.path, self.tips, ink, self.width, self.size, self.heads, self.dashed)
        if self.has_pill():  # the label, in a pill on the line
            pill = self.pill_rect()
            painter.setPen(QPen(theme_color("accent_border") if self.isSelected() else theme_color("border"), self.size))
            painter.setBrush(theme_color("raised"))
            painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        painter.restore()


class PointItem(QGraphicsItem):
    """A point on a line (double-click a line): the line goes through it as two lines. Drag it to route them round
    things; other lines join it as they join a card; Delete joins its two lines back into one. A ring with a dot in
    it, in its lines' colour; its position is its middle, size scales it like a line made at that zoom."""

    def __init__(self, item_id: str, size: float = 1.0):
        super().__init__()
        self.item_id = item_id
        self.size = size
        self.ink: Optional[str] = None  # (its lines' colour; None: the theme's)
        self.group: Optional[str] = None
        self.crop = None  # (image-only things: points have none)
        self.gray = False
        self.cropping = False
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsMovable)

    def apply_theme(self) -> None:
        self.update()

    def radius(self) -> float:
        return POINT_RADIUS * self.size

    def state(self) -> ItemState:
        return ItemState(self.pos().x(), self.pos().y(), 1.0, 0.0, False, False, self.zValue(), None,
                         round(self.opacity(), 4), group=self.group)

    def apply_state(self, state: ItemState) -> None:
        self.setPos(state.x, state.y)
        self.setZValue(state.z)
        self.setOpacity(state.opacity)
        self.group = state.group

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.item_id, "kind": "point", "x": self.pos().x(), "y": self.pos().y(), "size": self.size,
                "z": self.zValue(), "opacity": round(self.opacity(), 4), "group": self.group}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PointItem":
        def number(value: Any, default: float = 0.0) -> float:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return default
            return value if math.isfinite(value) else default

        point = cls(str(data.get("id") or uuid.uuid4().hex), min(1000.0, max(0.001, number(data.get("size"), 1.0))))
        point.apply_state(ItemState(number(data.get("x")), number(data.get("y")), z=number(data.get("z")),
                                    opacity=min(1.0, max(MIN_OPACITY / 100, number(data.get("opacity"), 1.0))),
                                    group=_clean_group(data.get("group"))))
        return point

    def crop_rect(self) -> QRectF:
        r = self.radius()
        return QRectF(-r, -r, 2 * r, 2 * r)

    def full_rect(self) -> QRectF:
        return self.crop_rect()

    def image(self) -> None:
        return None

    def boundingRect(self) -> QRectF:
        margin = 5 * self.size
        return self.crop_rect().adjusted(-margin, -margin, margin, margin)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        reach = self.radius() + 3 * self.size  # (a little easier to grab than it looks)
        path.addEllipse(QPointF(), reach, reach)
        return path

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.radius()
        ink = QColor(self.ink) if self.ink else theme_color("text_muted")
        if self.isSelected():  # a soft accent glow round it, like a selected line
            glow = QColor(theme_color("accent"))
            glow.setAlphaF(0.35)
            painter.setPen(QPen(glow, 6 * self.size))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(), r, r)
            ink = theme_color("accent")
        painter.setPen(QPen(ink, 2 * self.size))
        painter.setBrush(theme_color("raised"))
        painter.drawEllipse(QPointF(), r - self.size, r - self.size)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        painter.drawEllipse(QPointF(), 3 * self.size, 3 * self.size)
        painter.restore()


BOARD_ITEMS = (ImageItem, NoteItem, StrokeItem, ColumnItem, LineItem, TableItem, RefCardItem, PointItem)


def _clean_strip(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and QColor.isValidColorName(value) else None


def _takes_a_colour(cls):
    """A kind of card that takes a colour (its "strip"): kept in its state (so undo has it) and saved with it."""
    cls.strip = None
    state, apply_state, to_dict, from_dict = cls.state, cls.apply_state, cls.to_dict, cls.from_dict.__func__

    def with_strip(self) -> ItemState:
        return replace(state(self), strip=self.strip)

    def apply_with_strip(self, new: ItemState) -> None:
        apply_state(self, new)
        if new.strip != self.strip:
            self.strip = new.strip
            self.update()

    def to_dict_with_strip(self) -> dict[str, Any]:
        data = to_dict(self)
        if self.strip:
            data["strip"] = self.strip
        return data

    def from_dict_with_strip(kind, data: dict[str, Any]):
        item = from_dict(kind, data)
        item.strip = _clean_strip(data.get("strip"))
        return item

    cls.state, cls.apply_state, cls.to_dict, cls.from_dict = with_strip, apply_with_strip, to_dict_with_strip, classmethod(from_dict_with_strip)
    return cls


for _kind in (NoteItem, TableItem, ColumnItem, RefCardItem):
    _takes_a_colour(_kind)


def paint_strip(painter: QPainter, box: QRectF, color: Optional[str], radius: float = CARD_RADIUS) -> None:
    """A card's colour: a strip down its left edge, inside its rounded corners."""
    if not color:
        return
    outline = QPainterPath()
    outline.addRoundedRect(box, radius, radius)
    painter.save()
    painter.setClipPath(outline)
    painter.fillRect(QRectF(box.left(), box.top(), STRIP_WIDTH, box.height()), QColor(color))
    painter.restore()


class ColourSwatch(QToolButton):
    """A colour to pick (right-click > Colour): a dot (none: the theme's, crossed out), ringed when it's the one."""

    def __init__(self, color: Optional[str], current: bool):
        super().__init__()
        self.color, self.current = color, current
        self.setFixedSize(QSize(24, 24))
        self.setAutoRaise(True)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        middle = QPointF(self.width() / 2, self.height() / 2)
        if self.current or self.underMouse():
            painter.setPen(QPen(theme_color("accent_border"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(middle, 10.5, 10.5)
        if self.color:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.color))
            painter.drawEllipse(middle, 7.5, 7.5)
        else:
            painter.setPen(QPen(theme_color("border"), 1.2))
            painter.setBrush(theme_color("raised"))
            painter.drawEllipse(middle, 7.5, 7.5)
            painter.setPen(QPen(theme_color("text_faint"), 1.4))
            painter.drawLine(middle + QPointF(-5, 5), middle + QPointF(5, -5))
        painter.end()


# ---------------------------------------------------------------- snapping while dragging (BoardView._snap_move)
def _seen_rect(item) -> QRectF:
    """What shows of an item, on the board (not the room round it for its hover buttons): what snapping lines up."""
    return item.mapToScene(item.crop_rect()).boundingRect()


def _span(rect: QRectF, horizontal: bool) -> tuple[float, float]:
    return (rect.left(), rect.right()) if horizontal else (rect.top(), rect.bottom())


def _across(rect: QRectF, horizontal: bool) -> tuple[float, float]:
    return (rect.top(), rect.bottom()) if horizontal else (rect.left(), rect.right())


def _snap_axis(rect: QRectF, others: list[QRectF], reach: float, horizontal: bool) -> tuple[float, Optional[tuple]]:
    """How far rect should move along one axis (within reach) to line up with another (its edges or middle), or to
    leave an equal gap (between its neighbours, or the same as theirs); and which gaps, if it's a gap."""
    lo, hi = _span(rect, horizontal)
    size, mid = hi - lo, (lo + hi) / 2
    best: Optional[tuple[float, float, Optional[tuple]]] = None

    def offer(delta: float, gaps: Optional[tuple] = None) -> None:
        nonlocal best
        if abs(delta) <= reach and (best is None or abs(delta) < best[0] - 1e-9):
            best = (abs(delta), delta, gaps)

    for other in others:
        olo, ohi = _span(other, horizontal)
        for a in (lo, mid, hi):
            for b in (olo, (olo + ohi) / 2, ohi):
                offer(b - a)
    clo, chi = _across(rect, horizontal)
    row = [o for o in others if _across(o, horizontal)[0] < chi and _across(o, horizontal)[1] > clo]
    before = max((o for o in row if _span(o, horizontal)[1] <= lo + reach), key=lambda o: _span(o, horizontal)[1], default=None)
    after = min((o for o in row if _span(o, horizontal)[0] >= hi - reach), key=lambda o: _span(o, horizontal)[0], default=None)
    if before is not None and after is not None:
        target = (_span(before, horizontal)[1] + _span(after, horizontal)[0] - size) / 2
        offer(target - lo, ("between", before, after))
    if before is not None:
        b_lo = _span(before, horizontal)[0]
        further = max((o for o in row if o is not before and _span(o, horizontal)[1] <= b_lo + 0.5),
                      key=lambda o: _span(o, horizontal)[1], default=None)
        if further is not None and b_lo - _span(further, horizontal)[1] > 0:
            offer(_span(before, horizontal)[1] + b_lo - _span(further, horizontal)[1] - lo, ("after", further, before))
    if after is not None:
        a_hi = _span(after, horizontal)[1]
        further = min((o for o in row if o is not after and _span(o, horizontal)[0] >= a_hi - 0.5),
                      key=lambda o: _span(o, horizontal)[0], default=None)
        if further is not None and _span(further, horizontal)[0] - a_hi > 0:
            offer(_span(after, horizontal)[0] - (_span(further, horizontal)[0] - a_hi) - hi, ("before", after, further))
    return (0.0, None) if best is None else (best[1], best[2])


def _gap_segment(a: QRectF, b: QRectF, horizontal: bool) -> tuple[QPointF, QPointF, float]:
    """The gap from a to b (b further along), across the middle of where they overlap."""
    start, end = _span(a, horizontal)[1], _span(b, horizontal)[0]
    middle = (max(_across(a, horizontal)[0], _across(b, horizontal)[0]) + min(_across(a, horizontal)[1], _across(b, horizontal)[1])) / 2
    if horizontal:
        return QPointF(start, middle), QPointF(end, middle), end - start
    return QPointF(middle, start), QPointF(middle, end), end - start


def _gaps_shown(rect: QRectF, others: list[QRectF], gaps: Optional[tuple], horizontal: bool) -> list[tuple[QPointF, QPointF, float]]:
    """The equal gaps a snap lined up (to show them, with their size)."""
    if not gaps:
        return []
    kind, first, second = gaps
    pairs = {"between": [(first, rect), (rect, second)], "after": [(first, second), (second, rect)],
             "before": [(rect, first), (first, second)]}[kind]
    segments = [_gap_segment(a, b, horizontal) for a, b in pairs]
    return segments if abs(segments[0][2] - segments[1][2]) < 0.6 else []


def _aligned_lines(rect: QRectF, others: list[QRectF], tolerance: float) -> list[tuple[QPointF, QPointF]]:
    """The lines rect is lined up on with others (edges or middles), each from end to end of the two."""
    out: list[tuple[QPointF, QPointF]] = []
    seen: set = set()
    for other in others:
        for a in (rect.left(), rect.center().x(), rect.right()):
            for b in (other.left(), other.center().x(), other.right()):
                if abs(a - b) < tolerance and ("x", round(a, 1)) not in seen:
                    seen.add(("x", round(a, 1)))
                    out.append((QPointF(a, min(rect.top(), other.top())), QPointF(a, max(rect.bottom(), other.bottom()))))
        for a in (rect.top(), rect.center().y(), rect.bottom()):
            for b in (other.top(), other.center().y(), other.bottom()):
                if abs(a - b) < tolerance and ("y", round(a, 1)) not in seen:
                    seen.add(("y", round(a, 1)))
                    out.append((QPointF(min(rect.left(), other.left()), a), QPointF(max(rect.right(), other.right()), a)))
    return out


def page_header_dict(page: dict[str, Any]) -> dict[str, Any]:
    """A header for a saved page that has none (as saved): its name, above what's on it, at the size it shows at."""
    view = page.get("view") if isinstance(page.get("view"), dict) else {}
    zoom = float(view.get("zoom") or 1.0) if isinstance(view.get("zoom"), (int, float)) else 1.0
    scale = max(0.05, min(8.0, 1.0 / zoom)) if zoom > 0 else 1.0
    note = NoteItem(uuid.uuid4().hex)
    note.set_content(("", None, "title"))
    note.set_title_text(str(page.get("name") or "Untitled"))
    note.set_stats(store.counts_text(store.content_counts(page.get("items", []))))
    note.fit_title()
    left: Optional[float] = None
    top: Optional[float] = None
    for d in page.get("items", []):
        if not isinstance(d, dict) or not isinstance(d.get("x"), (int, float)) or not isinstance(d.get("y"), (int, float)):
            continue
        if d.get("kind", "image") == "image":
            size = d.get("size") if isinstance(d.get("size"), list) and len(d["size"]) == 2 else [512, 512]
            item_scale = float(d.get("scale") or 1.0)
            x, y = d["x"] - size[0] * item_scale / 2, d["y"] - size[1] * item_scale / 2
        else:
            x, y = d["x"], d["y"]
        left, top = (x if left is None else min(left, x)), (y if top is None else min(top, y))
    note.setScale(scale)
    height = note.boundingRect().height() * scale
    note.setPos(left if left is not None else 0.0, (top - height - 24 * scale) if top is not None else 0.0)
    z = min((float(d.get("z", 0)) for d in page.get("items", []) if isinstance(d, dict)), default=1.0) - 1.0
    note.setZValue(z)
    return note.to_dict()


def _free_spot(items: list) -> tuple[float, float]:
    """Where things sent to a page go: to the right of what's on it (from its saved items: roughly)."""
    right: Optional[float] = None
    top: Optional[float] = None
    for d in items:
        if not isinstance(d, dict) or not isinstance(d.get("x"), (int, float)) or not isinstance(d.get("y"), (int, float)):
            continue
        scale = float(d.get("scale") or 1.0)
        if d.get("kind", "image") == "image":
            size = d.get("size") if isinstance(d.get("size"), list) and len(d["size"]) == 2 else [512, 512]
            width, height = size[0] * scale, size[1] * scale
            left, upper = d["x"] - width / 2, d["y"] - height / 2
        else:
            width = float(d.get("width") or 300.0) * scale
            left, upper = d["x"], d["y"]
        right = left + width if right is None else max(right, left + width)
        top = upper if top is None else min(top, upper)
    return (0.0, 0.0) if right is None else (right + 200.0, top)
COLUMN_KINDS = (NoteItem, ImageItem, TableItem, RefCardItem)  # what can go in a column
LINKABLE = (NoteItem, ImageItem, ColumnItem, TableItem, RefCardItem, PointItem)  # what lines join
TRASH_DAYS = 30  # what's deleted from a page stays in its Trash this long
MAX_TRASH = 300  # deletions kept (the oldest go first)
MAX_COMMENTS = 2000  # threads on a page
COMMENT_BADGE_PX = 13.0  # a comment badge's (or pin's) half-size on screen
PAGE_MIME = "application/x-artbook-page"  # a page dragged from the Pages list (its id): a page card


class _BoardScene(QGraphicsScene):
    """The board's scene. Behind everything, the active area (a lighter rounded panel around all
    the items: BoardView keeps it up to date), then each group's backdrop: a rounded panel around
    its members (in the scene, so exports and thumbnails show it too; the active area they leave out)."""

    def __init__(self, *args):
        super().__init__(*args)
        self.gray_all = False  # the whole board in grayscale (read by ImageItem.paint)
        self.values_all = 0  # the whole board as this many flat greys (0: off; ImageItem.shown_values)
        self.group_backdrops: Callable[[], list[tuple[QRectF, Optional[str]]]] = list
        self.active_area: Optional[tuple[QRectF, float]] = None  # (the panel, its corners' radius)
        self.show_area = True  # (off while exporting and making thumbnails)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        backdrops = self.group_backdrops()
        area = self.active_area if self.show_area else None
        if not backdrops and area is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if area is not None and area[0].intersects(rect):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme_color("board_area"))
            painter.drawRoundedRect(area[0], area[1], area[1])
        for box, fill in backdrops:
            if not box.intersects(rect):
                continue
            if fill:
                color = QColor(fill)
                edge = color.darker(120)
            else:
                color = QColor(theme_color("accent"))
                color.setAlphaF(GROUP_DEFAULT_ALPHA)
                edge = QColor(theme_color("accent"))
                edge.setAlphaF(0.35)
            pen = QPen(edge, 1.5)
            pen.setCosmetic(True)  # (a hairline at any zoom)
            painter.setPen(pen)
            painter.setBrush(color)
            radius = min(box.width(), box.height()) * 0.03
            painter.drawRoundedRect(box, radius, radius)
        painter.restore()


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
        self.board.apply_states(self.after, animate=True)

    def undo(self) -> None:
        self.board.apply_states(self.before, animate=True)


class _MergingStateCommand(_StateCommand):
    """States changing, adding up to one step with the next of the same merge key (a live colour pick)."""

    def __init__(self, board: "BoardView", before: dict[str, ItemState], after: dict[str, ItemState], text: str, merge: int):
        super().__init__(board, before, after, text)
        self.merge = merge

    def id(self) -> int:
        return 3

    def mergeWith(self, other: QUndoCommand) -> bool:
        if not isinstance(other, _MergingStateCommand) or other.merge != self.merge or other.after.keys() != self.after.keys():
            return False
        self.after = other.after
        return True


class _AddCommand(QUndoCommand):
    def __init__(self, board: "BoardView", items: list[ImageItem], text: str):
        super().__init__(text)
        self.board, self.items = board, items  # kept alive here while they're off the board

    def redo(self) -> None:
        for item in self.items:
            self.board.attach(item)
        self.board.lay_out_columns()

    def undo(self) -> None:
        for item in self.items:
            self.board.detach(item)
        self.board.lay_out_columns()


class _SendCommand(QUndoCommand):
    """Move / Copy to Page: things onto another page's file (and, moving, off this one). Undo takes them off that
    page again (and puts moved ones back here)."""

    def __init__(self, board: "BoardView", page_id: str, dicts: list, comments: list, groups: dict, items: list, text: str):
        super().__init__(text)
        self.board, self.page_id, self.dicts, self.comments, self.groups, self.items = board, page_id, dicts, comments, groups, items
        self.ids = {d["id"] for d in dicts}
        self.thread_ids = {c["id"] for c in comments}

    def redo(self) -> None:
        try:
            page = store.load_page(self.page_id)
            have = {d.get("id") for d in page.get("items", []) if isinstance(d, dict)}
            page["items"] = list(page.get("items", [])) + [json.loads(json.dumps(d)) for d in self.dicts if d["id"] not in have]
            threads = [t for t in page.get("comments") or [] if isinstance(t, dict)]
            page["comments"] = threads + [dict(t) for t in self.comments if t["id"] not in {x.get("id") for x in threads}]
            groups = dict(page.get("groups") or {})
            groups.update(self.groups)
            page["groups"] = groups
            store.save_page_now(page)
        except (store.PageError, OSError) as exc:
            self.board.show_message(f"Couldn't change that page: {exc}")
        for item in self.items:
            self.board.detach(item)
        self.board.lay_out_columns()
        self.board.lay_out_lines()

    def undo(self) -> None:
        try:
            page = store.load_page(self.page_id)
            page["items"] = [d for d in page.get("items", []) if not (isinstance(d, dict) and d.get("id") in self.ids)]
            page["comments"] = [t for t in page.get("comments") or [] if not (isinstance(t, dict) and t.get("id") in self.thread_ids)]
            store.save_page_now(page)
        except (store.PageError, OSError) as exc:
            self.board.show_message(f"Couldn't change that page: {exc}")
        for item in self.items:
            self.board.attach(item)
        self.board.lay_out_columns()
        self.board.lay_out_lines()


class _RemoveCommand(_AddCommand):
    """Items deleted: off the board, into the page's Trash (undo: back, and out of the Trash)."""

    def __init__(self, board: "BoardView", items: list, text: str):
        super().__init__(board, items, text)
        self.trash_id = uuid.uuid4().hex

    def redo(self) -> None:
        self.board.trash_add(self.trash_id, self.items)
        super().undo()

    def undo(self) -> None:
        self.board.trash_discard(self.trash_id)
        super().redo()


class _RestoreCommand(QUndoCommand):
    """Trash > Restore: a deletion's items back where they were (undo: into the Trash again)."""

    def __init__(self, board: "BoardView", entry: dict[str, Any], items: list):
        super().__init__("restore")
        self.board, self.entry, self.items = board, entry, items

    def redo(self) -> None:
        self.board.trash_discard(self.entry["id"])
        for gid, meta in self.entry.get("groups", {}).items():
            self.board._groups.setdefault(gid, dict(meta))
        for item in self.items:
            self.board.attach(item)
        self.board.lay_out_columns()

    def undo(self) -> None:
        for item in self.items:
            self.board.detach(item)
        self.board.trash_put(self.entry)
        self.board.lay_out_columns()


class _CommentsCommand(QUndoCommand):
    """The page's comments, before and after a change."""

    def __init__(self, board: "BoardView", before: list, after: list, text: str):
        super().__init__(text)
        self.board, self.before, self.after = board, before, after

    def redo(self) -> None:
        self.board.set_comments(self.after)

    def undo(self) -> None:
        self.board.set_comments(self.before)


def item_from_dict(data: dict[str, Any]):
    """A board item from its saved form (None: a kind this version doesn't make)."""
    kind = data.get("kind", "image")
    makers = {"note": NoteItem, "stroke": StrokeItem, "column": ColumnItem, "line": LineItem, "table": TableItem,
              "image": ImageItem, "point": PointItem}
    if kind in CARD_KINDS:
        return RefCardItem.from_dict(data)
    return makers[kind].from_dict(data) if kind in makers else None


def trash_label(data: dict[str, Any]) -> str:
    """What a deleted item was, in a few words (its row in the Trash)."""
    kind = data.get("kind", "image")

    def short(text: str, limit: int = 40) -> str:
        text = " ".join(str(text).split())
        return text if len(text) <= limit else text[:limit - 3] + "..."

    if kind == "image":
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        return short(data.get("name") or (Path(source["path"]).name if source.get("path") else "") or "Image")
    if kind == "note":
        text = short(data.get("text") or "")
        return f"Note: “{text}”" if text else "Note"
    if kind == "column":
        return f"Column: {short(data.get('title') or 'Untitled')}"
    if kind == "table":
        return f"Table: {short(data['title'])}" if data.get("title") else "Table"
    if kind in CARD_KINDS:
        name = data.get("title") or data.get("name") or (Path(data["path"]).name if data.get("path") else "") or data.get("url") or "Card"
        return short(name)
    return {"stroke": "Drawing", "line": "Line", "point": "Point on a line"}.get(kind, kind.title())


def clean_trash(value: Any, now: Optional[float] = None) -> list[dict[str, Any]]:
    """A page's Trash as saved, without what's older than TRASH_DAYS (or damaged)."""
    now = time.time() if now is None else now
    out = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("items"), list) or not entry["items"]:
            continue
        stamp = entry.get("time")
        if not isinstance(stamp, (int, float)) or now - stamp > TRASH_DAYS * 86400:
            continue
        items = [d for d in entry["items"] if isinstance(d, dict) and isinstance(d.get("id"), str)]
        groups = entry.get("groups") if isinstance(entry.get("groups"), dict) else {}
        if items:
            out.append({"id": str(entry.get("id") or uuid.uuid4().hex), "time": float(stamp), "items": items,
                        "groups": {g: dict(m) for g, m in groups.items() if _clean_group(g) and isinstance(m, dict)}})
    return out[-MAX_TRASH:]


def clean_comments(value: Any) -> list[dict[str, Any]]:
    """A page's comment threads as saved: on an item ("item": its id) or a pin ("x", "y"), each with its notes."""
    out = []
    for thread in (value if isinstance(value, list) else [])[:MAX_COMMENTS]:
        if not isinstance(thread, dict):
            continue
        notes = []
        for note in thread.get("notes") if isinstance(thread.get("notes"), list) else []:
            if isinstance(note, dict) and isinstance(note.get("text"), str) and note["text"].strip():
                stamp = note.get("time")
                notes.append({"text": note["text"][:2000], "time": float(stamp) if isinstance(stamp, (int, float)) else 0.0,
                              "done": note.get("done") is True})
        if not notes:
            continue
        item = thread.get("item") if isinstance(thread.get("item"), str) and 0 < len(thread["item"]) <= 64 else None
        try:
            x, y = float(thread.get("x", 0)), float(thread.get("y", 0))
        except (TypeError, ValueError):
            x = y = 0.0
        if not (math.isfinite(x) and math.isfinite(y)):
            x = y = 0.0
        out.append({"id": str(thread.get("id") or uuid.uuid4().hex)[:64], "item": item, "x": x, "y": y, "notes": notes})
    return out


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


class _ReplaceCommand(QUndoCommand):
    """An image's picture swapped for another (its place, size, crop and adjustments kept).
    before / after: {"source", "natural", "name", "pictures" (ImageItem.pictures), "state",
    "original" (ImageItem.original)}."""

    def __init__(self, item: "ImageItem", before: dict[str, Any], after: dict[str, Any], text: str = "replace image"):
        super().__init__(text)
        self.item, self.before, self.after = item, before, after

    @staticmethod
    def _apply(item: "ImageItem", picture: dict[str, Any]) -> None:
        item.prepareGeometryChange()
        item.source, item.natural, item.name = dict(picture["source"]), picture["natural"], picture["name"]
        item.original = picture.get("original")
        item.restore_pictures(picture["pictures"])
        item.load_failed = False
        item.crop = None  # (the state brings the crop that fits this picture)
        item.apply_state(picture["state"])
        item.update()

    def redo(self) -> None:
        self._apply(self.item, self.after)

    def undo(self) -> None:
        self._apply(self.item, self.before)


class _ReplaceManyCommand(QUndoCommand):
    """Several images' pictures swapped as one step ([(item, before, after)], as _ReplaceCommand).
    (Not child commands: Qt would keep those after Python had let go of them.)"""

    def __init__(self, swaps: list[tuple["ImageItem", dict[str, Any], dict[str, Any]]], text: str):
        super().__init__(text)
        self.swaps = swaps

    def redo(self) -> None:
        for item, _before, after in self.swaps:
            _ReplaceCommand._apply(item, after)  # pylint: disable=protected-access

    def undo(self) -> None:
        for item, before, _after in self.swaps:
            _ReplaceCommand._apply(item, before)  # pylint: disable=protected-access


class _SourcesCommand(QUndoCommand):
    """Images linked to other files ({item id: (source before, source after)}); each is read again."""

    def __init__(self, board: "BoardView", changes: dict[str, tuple[dict[str, Any], dict[str, Any]]]):
        super().__init__("relink images")
        self.board, self.changes = board, changes

    def redo(self) -> None:
        self.board.set_sources({item_id: after for item_id, (_before, after) in self.changes.items()})

    def undo(self) -> None:
        self.board.set_sources({item_id: before for item_id, (before, _after) in self.changes.items()})


class _GroupMetaCommand(QUndoCommand):
    """Groups' own settings changing ({group id: {"fill": colour, "locked": True}}, {} for none).
    Commands with the same merge key (a live colour pick) add up to one step."""

    def __init__(
        self, board: "BoardView", before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]],
        text: str, merge: Optional[int] = None,
    ):
        super().__init__(text)
        self.board, self.before, self.after, self.merge = board, before, after, merge

    def id(self) -> int:
        return 2 if self.merge is not None else -1

    def mergeWith(self, other: QUndoCommand) -> bool:
        if not isinstance(other, _GroupMetaCommand) or other.merge != self.merge or other.after.keys() != self.after.keys():
            return False
        self.after = other.after
        return True

    def redo(self) -> None:
        self.board.set_group_metas(self.after)

    def undo(self) -> None:
        self.board.set_group_metas(self.before)


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


def _card_picture_blob(page_id: str, image: Optional[QImage]) -> Optional[str]:
    """A card's picture kept in the page (as a JPEG, at most CARD_PICTURE_SIDE): its stored name."""
    if image is None or image.isNull():
        return None
    if max(image.width(), image.height()) > CARD_PICTURE_SIDE:
        image = image.scaled(CARD_PICTURE_SIDE, CARD_PICTURE_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    flat = QImage(image.size(), QImage.Format.Format_RGB32)
    flat.fill(QColor("#ffffff"))  # (a JPEG has no see-through parts)
    painter = QPainter(flat)
    painter.drawImage(0, 0, image)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    flat.save(buffer, "JPG", 88)
    return store.store_blob(page_id, bytes(buffer.data()), ".jpg")


def card_icon_image(data: Optional[bytes]) -> Optional[QImage]:
    """A site icon's data (a PNG, an .ico, an SVG...) as a picture at most CARD_ICON_SIDE (an SVG drawn at that
    size); None if it isn't one."""
    if not data:
        return None
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if size.isValid() and (reader.format() == b"svg" or max(size.width(), size.height()) > CARD_ICON_SIDE):
        reader.setScaledSize(size.scaled(CARD_ICON_SIDE, CARD_ICON_SIDE, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    buffer.close()
    return None if image.isNull() else image


def _card_icon_blob(page_id: str, image: Optional[QImage]) -> Optional[str]:
    """A link card's site icon kept in the page (a PNG: icons have see-through parts): its stored name."""
    if image is None or image.isNull():
        return None
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return store.store_blob(page_id, bytes(buffer.data()), ".png")


def _with_icon(page_id: str, prepared: dict[str, Any], icon: Optional[bytes]) -> dict[str, Any]:
    """A link card as prepared, with its site's icon (data, or None): stored, and its picture for the card."""
    image = card_icon_image(icon)
    blob = _card_icon_blob(page_id, image)
    if blob:
        prepared["card"]["favicon"] = blob
    prepared["favicon"] = image if blob else None
    return prepared


def link_name(url: str) -> str:
    """A page's name from its address, when the page can't be read: its last part ("artstation.com/danieljakobi":
    "danieljakobi"), or its site."""
    parts = urllib.parse.urlsplit(url)
    segments = [urllib.parse.unquote(part) for part in parts.path.split("/") if part]
    host = parts.hostname or url
    return " ".join(segments[-1].replace("_", " ").split()) if segments else (host[4:] if host.startswith("www.") else host)


def _prepare_link(page_id: str, url: str) -> dict[str, Any]:
    """A web address: its picture (an image's address), or a link card with the page's title, a line about it,
    its site and its preview picture. A page that can't be read still gets a card (just its address)."""
    try:
        kind, payload = web_images.read_link(url)
    except Exception:  # pylint: disable=broad-except
        kind, payload = "page", {}
    if kind == "image":
        return _prepare_bytes(page_id, *payload)
    image = QImage.fromData(payload["picture"]) if payload.get("picture") else None
    host = urllib.parse.urlsplit(url).hostname or ""
    data = {
        "kind": "link", "url": url, "title": payload.get("title") or link_name(url), "about": payload.get("about") or "",
        "site": payload.get("site") or (host[4:] if host.startswith("www.") else host),
        "picture": _card_picture_blob(page_id, image),
    }
    icon = payload.get("icon")
    return _with_icon(page_id, {"card": data, "image": image}, icon[0] if icon else None)


def file_card_picture(path: Path) -> Optional[QImage]:
    """A library file's picture: the asset grid's (from its cache), or one read now (a 3D model's render, a .blend's
    preview, a PDF's first page, a video's frame); None: there's none (its card shows its app's icon)."""
    from core import thumbnail_manager
    from core.scanner import AssetItem, determine_asset_group, determine_category_name
    from core.settings import get_thumbnail_size

    ext = path.suffix.lower()
    stat = path.stat()
    asset = AssetItem(path, path.name, ext, stat.st_size, stat.st_mtime, determine_asset_group(ext), determine_category_name(ext))
    cached = thumbnail_manager.disk_cache_path(asset, get_thumbnail_size())
    if cached is not None and cached.is_file():
        image = QImage(str(cached))
        if not image.isNull():
            return image
    try:
        if asset.is_app_file or ext == ".blend":
            return thumbnail_manager.load_app_file_picture(asset, 384)
        if asset.is_3d_model:
            return thumbnail_manager.load_model_picture(path, 384)
        if ext == ".pdf":
            return thumbnail_manager.load_pdf_thumbnail_image(path, 384)
        if asset.is_video:
            return thumbnail_manager.load_video_picture(path, 384)
    except Exception:  # pylint: disable=broad-except
        return None
    return None


def _prepare_link_entry(page_id: str, entry: dict[str, Any], picture: Optional[bytes], icon: Optional[bytes] = None) -> dict[str, Any]:
    """A link the browser extension read (its title, a line about it, its site, its preview's and its site icon's
    data): a link card."""
    image = QImage.fromData(picture) if picture else None
    url = entry.get("url", "")
    host = urllib.parse.urlsplit(url).hostname or ""
    data = {
        "kind": "link", "url": url, "title": entry.get("title") or link_name(url), "about": entry.get("about") or "",
        "site": entry.get("site") or (host[4:] if host.startswith("www.") else host),
        "picture": _card_picture_blob(page_id, image if image is not None and not image.isNull() else None),
    }
    return _with_icon(page_id, {"card": data, "image": image if image is not None and not image.isNull() else None}, icon)


def _prepare_file_card(page_id: str, path: Path) -> dict[str, Any]:
    """A file that isn't an image: a card linked to it, with its picture (file_card_picture)."""
    image = file_card_picture(path)
    if image is None:
        app_icons.icon_for(path.suffix.lower())  # (read now, off the UI thread: the card shows it)
    data = {"kind": "file", "path": str(path), "name": path.name, "ext": path.suffix.lower(), "size": path.stat().st_size,
            "picture": _card_picture_blob(page_id, image)}
    return {"card": data, "image": image}


def smooth_points(points: list[QPointF], rounds: int = SMOOTH_ROUNDS) -> list[QPointF]:
    """A line's corners rounded off (Chaikin's corner cutting), its ends kept where they are."""
    for _ in range(rounds):
        if len(points) < 3:
            return points
        rounded = [points[0]]
        for a, b in zip(points, points[1:]):
            rounded += [a * 0.75 + b * 0.25, a * 0.25 + b * 0.75]
        rounded.append(points[-1])
        points = rounded[: MAX_STROKE_POINTS]
    return points


def card_colors(fill: Optional[str]) -> tuple[QColor, QColor]:
    """A card's paper and edge: its own colour (a darker edge), or the theme's raised colour and border."""
    if fill:
        paper = QColor(fill)
        return paper, paper.darker(125)
    return theme_color("raised"), theme_color("border")


def is_check(block) -> bool:
    """A checklist line: in a list, with a box (ticked or not)."""
    return (block.isValid() and block.textList() is not None
            and block.blockFormat().marker() != QTextBlockFormat.MarkerType.NoMarker)


def tidy_checklists(document: QTextDocument) -> None:
    """Keep checklists drawn by the note (their lists without a bullet: reading a page back makes them bulleted),
    and the room their count and bar need: under the line above the first box, and at its right."""
    block = document.firstBlock()
    seen: set[int] = set()
    first_rows: set[int] = set()
    while block.isValid():
        listed = block.textList()
        if is_check(block) and listed is not None:
            if id(listed) not in seen:
                seen.add(id(listed))
                fmt = listed.format()
                if fmt.style() != QTextListFormat.Style.ListStyleUndefined:
                    fmt.setStyle(QTextListFormat.Style.ListStyleUndefined)
                    listed.setFormat(fmt)
            if not first_rows:
                first_rows.add(block.blockNumber())
        block = block.next()
    first = min(first_rows) if first_rows else None
    block = document.firstBlock()
    while block.isValid():
        number = block.blockNumber()
        titled = first is not None and number == first - 1 and not is_check(block)
        top = first is not None and number == first and not block.previous().isValid()
        fmt = block.blockFormat()
        want_bottom = BAND if titled else 0.0
        want_top = BAND + 4 if top else 0.0
        want_right = COUNT_ROOM if titled else 0.0
        if (fmt.bottomMargin(), fmt.topMargin(), fmt.rightMargin()) != (want_bottom, want_top, want_right):
            if fmt.bottomMargin() in (0.0, BAND) and fmt.topMargin() in (0.0, BAND + 4) and fmt.rightMargin() in (0.0, COUNT_ROOM):
                fmt.setBottomMargin(want_bottom)
                fmt.setTopMargin(want_top)
                fmt.setRightMargin(want_right)
                QTextCursor(block).setBlockFormat(fmt)
        block = block.next()


def swap_rows(document: QTextDocument, a: int, b: int) -> None:
    """Swap two lines' text (with its formatting) and their boxes; each line keeps its place in the list."""
    if a == b:
        return
    first, second = sorted((a, b))
    pieces = {}
    for number in (first, second):
        # The text piece by piece with its formatting (a copied whole line would bring its list item along: a new line)
        block = document.findBlockByNumber(number)
        runs, it = [], block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            runs.append((fragment.text(), fragment.charFormat()))
            it += 1
        pieces[number] = (runs, block.blockFormat().marker(), block.charFormat())
    for number, other in ((second, first), (first, second)):  # (the later one first: the earlier's place stays put)
        block = document.findBlockByNumber(number)
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        runs, marker, typing = pieces[other]
        cursor.removeSelectedText()
        cursor.setBlockCharFormat(typing)  # (what an empty line's typed text starts as)
        for text, fmt in runs:
            cursor.insertText(text, fmt)
        block = document.findBlockByNumber(number)
        fmt = block.blockFormat()
        fmt.setMarker(marker)
        QTextCursor(block).setBlockFormat(fmt)


def move_row(document: QTextDocument, row: int, to: int) -> int:
    """Move a checklist line to another line's place in its list (the ones between shift along); returns where it is."""
    step = 1 if to > row else -1
    while row != to:
        nxt = document.findBlockByNumber(row + step)
        here = document.findBlockByNumber(row)
        if not is_check(nxt) or nxt.textList() != here.textList():
            break
        swap_rows(document, row, row + step)
        row += step
    return row


def tick_row(document: QTextDocument, row: int) -> int:
    """Tick a checklist line (or untick it): a done one sinks to the bottom of its list, one ticked again comes back up
    above the done ones. Returns where it is now."""
    block = document.findBlockByNumber(row)
    fmt = block.blockFormat()
    done = fmt.marker() != QTextBlockFormat.MarkerType.Checked
    fmt.setMarker(QTextBlockFormat.MarkerType.Checked if done else QTextBlockFormat.MarkerType.Unchecked)
    QTextCursor(block).setBlockFormat(fmt)
    listed = block.textList()
    numbers = [listed.item(i).blockNumber() for i in range(listed.count())]
    if done:
        return move_row(document, row, numbers[-1])
    checked = [n for n in numbers if n != row and document.findBlockByNumber(n).blockFormat().marker() == QTextBlockFormat.MarkerType.Checked]
    above = [n for n in checked if n < row]
    return move_row(document, row, above[0]) if above else row


def paragraph_of(cursor: QTextCursor) -> str:
    """What the cursor's line is: a checklist, a list (bullet / number), a heading (h1 / h2) or text."""
    if is_check(cursor.block()):
        return "check"
    listed = cursor.currentList()
    if listed is not None:
        return "number" if listed.format().style() == QTextListFormat.Style.ListDecimal else "bullet"
    level = cursor.blockFormat().headingLevel()
    return {1: "h1", 2: "h2"}.get(level, "text")


def set_paragraph(cursor: QTextCursor, kind: str, style: str = "card") -> None:
    """Make the lines cursor touches a heading (h1 / h2), a list (bullet / number) or text again."""
    document = cursor.document()
    first = document.findBlock(cursor.selectionStart())
    last = document.findBlock(cursor.selectionEnd())
    blocks = [first]
    while blocks[-1] != last and blocks[-1].next().isValid():
        blocks.append(blocks[-1].next())
    edit = QTextCursor(document)
    edit.beginEditBlock()
    base = HEADING_PT if style == "heading" else NOTE_FONT_PT
    for block in blocks:
        listed = block.textList()
        if listed is not None and kind not in ("bullet", "number", "check"):
            listed.remove(block)
            fmt = block.blockFormat()
            fmt.setIndent(0)
            QTextCursor(block).setBlockFormat(fmt)
        fmt = block.blockFormat()
        marker = QTextBlockFormat.MarkerType.NoMarker
        if kind == "check":  # (keeps a tick it has)
            marker = fmt.marker() if fmt.marker() != QTextBlockFormat.MarkerType.NoMarker else QTextBlockFormat.MarkerType.Unchecked
        if fmt.marker() != marker:
            fmt.setMarker(marker)
            QTextCursor(block).setBlockFormat(fmt)
        line = QTextCursor(block)
        fmt = line.blockFormat()
        fmt.setHeadingLevel({"h1": 1, "h2": 2}.get(kind, 0))
        line.setBlockFormat(fmt)
        line.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        chars = QTextCharFormat()
        chars.setFontPointSize(float(PARAGRAPH_PT.get(kind, base)))
        chars.setFontWeight(QFont.Weight.Bold if kind in PARAGRAPH_PT or style == "heading" else QFont.Weight.Normal)
        line.mergeCharFormat(chars)
        line.clearSelection()
        line.mergeBlockCharFormat(chars)  # (what's typed on that line next)
    if kind in ("bullet", "number", "check"):
        # Lists don't read centred (a new note is): the whole note lines up on the left (More > Align brings it back)
        everything = QTextCursor(document)
        everything.select(QTextCursor.SelectionType.Document)
        left = QTextBlockFormat()
        left.setAlignment(Qt.AlignmentFlag.AlignLeft)
        everything.mergeBlockFormat(left)
        wanted = {"bullet": QTextListFormat.Style.ListDisc, "number": QTextListFormat.Style.ListDecimal,
                  "check": QTextListFormat.Style.ListStyleUndefined}[kind]
        listed = blocks[0].textList()
        if listed is not None and all(block.textList() == listed for block in blocks):
            fmt = listed.format()
            fmt.setStyle(wanted)
            listed.setFormat(fmt)
        else:
            span = QTextCursor(document)
            span.setPosition(blocks[0].position())
            span.setPosition(blocks[-1].position() + blocks[-1].length() - 1, QTextCursor.MoveMode.KeepAnchor)
            span.createList(wanted)
    tidy_checklists(document)
    edit.endEditBlock()


def _center_text(note: "NoteItem") -> None:
    """Center every line of a note (what new notes start with; Enter carries it to the next line)."""
    cursor = QTextCursor(note.document())
    cursor.select(QTextCursor.SelectionType.Document)
    block = QTextBlockFormat()
    block.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    cursor.mergeBlockFormat(block)


def _fits_base(image: QImage) -> bool:
    return max(image.width(), image.height()) <= BASE_SIDE


def _base_image(image: QImage) -> QImage:
    """The small copy every image keeps (the image itself if it's that small already)."""
    if _fits_base(image):
        return image
    return image.scaled(BASE_SIDE, BASE_SIDE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


def _decode_saved(page_id: str, source: dict[str, Optional[str]], side: int = store.MAX_DECODE_SIDE) -> QImage:
    """An image's picture, at most side big: from its linked file if it's still there, otherwise
    from the copy in the page. The copy is used for small sizes too, while it's as new as the file
    (it's quicker to read than a big original, and holds the same picture)."""
    path = source.get("path")
    blob = source.get("blob")
    copy = store.blob_path(page_id, blob) if blob else None
    if path and Path(path).is_file():
        try:
            if copy is not None and side <= store.FALLBACK_SIDE and copy.stat().st_mtime >= Path(path).stat().st_mtime:
                return store.decode_file(copy, side)[0]
        except (OSError, store.PageError):
            pass
        try:
            return store.decode_file(Path(path), side)[0]
        except store.PageError:
            pass
    if copy is not None:
        return store.decode_file(copy, side)[0]
    raise store.PageError("The image is missing.")


def _load_pictures(page_id: str, requests: list[tuple[dict, int, bool]]) -> list[tuple[Optional[tuple], str]]:
    """_load_picture for each (source, side, base): [(result or None, error)], one per request."""
    results = []
    for source, side, base in requests:
        try:
            results.append((_load_picture(page_id, source, side, base), ""))
        except Exception as exc:  # pylint: disable=broad-except
            results.append((None, str(exc) or type(exc).__name__))
    return results


def _small_copy_file(page_id: str, source: dict[str, Optional[str]]) -> Optional[Path]:
    """Where an image's small copy is cached (see video_tools.artbook_cache_dir): named after its
    file as it is now (its path, size and time), or the copy in the page (named by its content)."""
    path, blob = source.get("path"), source.get("blob")
    key = None
    if path:
        try:
            info = Path(path).stat()
            key = f"file|{os.path.normcase(os.path.abspath(path))}|{info.st_size}|{info.st_mtime_ns}"
        except OSError:
            key = None
    if key is None and blob and store.blob_path(page_id, blob).is_file():
        key = f"blob|{blob}"
    if key is None:
        return None
    name = hashlib.sha1(f"{key}|{BASE_SIDE}".encode("utf-8")).hexdigest()
    return video_tools.artbook_cache_dir() / f"{name}.png"


def _small_copy(page_id: str, source: dict[str, Optional[str]]) -> QImage:
    """An image's small copy (BASE_SIDE): from the cache if it's there, else decoded (and cached)."""
    cached = _small_copy_file(page_id, source)
    if cached is not None and cached.is_file():
        image = QImage(str(cached))
        if not image.isNull():
            return image
    image = _decode_saved(page_id, source, BASE_SIDE)
    _cache_small_copy(cached, image)
    return image


def _cache_small_copy(cached: Optional[Path], image: QImage) -> None:
    if cached is None or cached.is_file():
        return
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        temp = cached.with_name(f"{cached.name}.{uuid.uuid4().hex}.tmp")
        if image.save(str(temp), "PNG"):
            os.replace(temp, cached)
        else:
            temp.unlink(missing_ok=True)
    except OSError:
        pass  # (only a cache: it's decoded again next time)


def _load_picture(page_id: str, source: dict[str, Optional[str]], side: int, base: bool) -> tuple[Optional[QImage], Optional[QImage]]:
    """(sharper copy or None, small copy or None) for an image: side 0 is the small copy only."""
    if not side:
        return None, _small_copy(page_id, source)
    image = _decode_saved(page_id, source, side)
    if not base:
        return image, None
    small = _base_image(image)
    _cache_small_copy(_small_copy_file(page_id, source), small)
    return image, small


def export_pictures(page_id: Optional[str], requests: list[tuple[str, dict, int]]) -> dict[str, QImage]:
    """{item id: its picture decoded side big} for BoardView.picture_requests (images that can't be
    read are left out: they're drawn as loaded). Safe off the UI thread."""
    pictures = {}
    for item_id, source, side in requests:
        try:
            pictures[item_id] = _decode_saved(page_id or "", source, side)
        except store.PageError:
            pass
    return pictures


def detail_side(natural: tuple[int, int], on_screen: float) -> int:
    """How sharp a copy an image needs (its longest side), shown on_screen pixels big (longest
    side): 0 if the small copy will do. Powers of two (so zooming doesn't decode at every step),
    at most the whole image, and at most DETAIL_MAX_BYTES."""
    longest = max(natural)
    if longest <= BASE_SIDE or on_screen <= BASE_SIDE * DETAIL_STEP:
        return 0
    side = BASE_SIDE * 2
    while side < on_screen / DETAIL_STEP:
        side *= 2
    cap = int(longest * math.sqrt(DETAIL_MAX_BYTES / (4.0 * natural[0] * natural[1])))
    return min(side, longest, cap)


def _detail_bytes(natural: tuple[int, int], side: int) -> int:
    ratio = side / max(natural)
    return int(natural[0] * ratio * natural[1] * ratio * 4)


# ----------------------------------------------------------------------
# The board
# ----------------------------------------------------------------------
def find_files(folder: Path, names: set[str], limit: int = RELINK_SCAN_LIMIT) -> dict[str, str]:
    """Look through a folder (and the folders in it) for files with these names (any case).
    {lowercase name: the first such file found}. Safe off the UI thread."""
    wanted = {name.lower() for name in names}
    found: dict[str, str] = {}
    seen = 0
    for root, _dirs, files in os.walk(folder):
        for file_name in files:
            seen += 1
            key = file_name.lower()
            if key in wanted and key not in found:
                found[key] = str(Path(root) / file_name)
        if len(found) == len(wanted) or seen > limit:
            break
    return found


class BoardView(QGraphicsView):
    """The endless board of one page (see the module docstring)."""

    changed = Signal()  # the page's content changed (autosave)
    trash_changed = Signal()  # something went into the page's Trash or came out (the tray shows it)
    unsorted_changed = Signal()  # something went into Unsorted or came out (the tray and its button show it)
    tray_toggled = Signal(bool)  # the tray was shown or hidden
    zoom_changed = Signal(float)
    mirrored_changed = Signal(bool)  # Mirror Board turned on or off
    boards_dropped = Signal(list)  # board files dropped on it (.artbook, PureRef .pur): to import as pages
    export_requested = Signal(str, str)  # (format: png / jpg / pdf / copy / options, what: "" for the saved choice, or an area)
    export_page_requested = Signal()  # the menu: share the page as an .artbook file
    files_changed = Signal()  # an image's picture came in (or failed), or it was relinked: for the image list
    image_list_toggled = Signal(bool)  # the image list was shown / hidden

    # Keys the board handles itself while it has the focus (even ones the window also uses)
    KEYS = (
        "artbook.paste", "artbook.copy", "artbook.fit", "artbook.zoom_100", "artbook.flip_h", "artbook.flip_v",
        "artbook.front", "artbook.back", "artbook.reset", "artbook.delete", "artbook.select_all",
        "artbook.deselect", "artbook.undo", "artbook.redo", "artbook.pan_key", "artbook.arrange",
        "artbook.crop", "artbook.crop_apply", "artbook.gray", "artbook.gray_board", "artbook.values_board", "artbook.mirror_board", "artbook.note",
        "artbook.heading", "artbook.todo", "artbook.column", "artbook.table", "artbook.comment", "artbook.tray", "artbook.line", "artbook.picker",
        "artbook.export_png", "artbook.group", "artbook.ungroup", "artbook.image_list", "artbook.draw", "artbook.eraser",
        "artbook.find", "artbook.export_options", "artbook.copy_image", "artbook.duplicate",
        "artbook.nudge_left", "artbook.nudge_right", "artbook.nudge_up", "artbook.nudge_down",
        "artbook.nudge_left_big", "artbook.nudge_right_big", "artbook.nudge_up_big", "artbook.nudge_down_big",
    )

    page_open_requested = Signal(str)  # a page card was opened: show that page (ui.artbook: with a way back)
    show_in_library_requested = Signal(str)  # a file card's Show in Library: the asset browser shows that file
    page_rename_requested = Signal(str)  # the page header's name was typed (or undone): ui.artbook renames the page

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._loads = 0  # pictures being decoded (level of detail)
        self._queue: list[tuple[ImageItem, int]] = []  # what to decode next, most wanted first
        self._detail_timer = QTimer(self)  # (first: making the view scrolls it)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(DETAIL_TICK_MS)
        self._detail_timer.timeout.connect(self.update_detail)
        self.setObjectName("artbookBoard")
        self.setProperty("smoothScroll", False)  # the wheel zooms here (ui.views would scroll)
        self._scene = _BoardScene(-SCENE_EXTENT, -SCENE_EXTENT, 2 * SCENE_EXTENT, 2 * SCENE_EXTENT, self)
        self._scene.changed.connect(self._schedule_detail)  # (items moved, scaled, added)
        self._scene.group_backdrops = self.group_backdrops
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
        self.upscale_dialog = None  # the Upscale window, while it's open (ui.artbook_upscale)
        self.mirrored = False  # Mirror Board: the view flipped left to right (a check: nothing on the page changes)
        self._note_bar: Optional[NoteBar] = None  # floats above the selected note (made when first needed:
        self._note_bar_queued = False  # listing the system's fonts for it is slow)
        self._live_merge: Optional[int] = None  # (see live_change)
        self._live_merges = 0
        self._groups: dict[str, dict[str, Any]] = {}  # group id -> {"fill": colour}, for groups with one
        self._expanding = False  # (selecting the rest of a group)
        self._single_pick = False  # Alt+click: one member of a group, on its own
        self._group_bar: Optional[GroupBar] = None  # floats above the selected group (made when first needed)
        self._image_bar: Optional[ImageBar] = None  # floats under the selected images (made when first needed)
        self._image_list: Optional[ImageListPanel] = None  # every image on the page, at the right (L)
        self._tray: Optional[TrayPanel] = None  # Unsorted and Trash, at the right (U)
        self._trash: list[dict[str, Any]] = []  # what was deleted from the page: {"id", "time", "items", "groups"}
        self._comments: list[dict[str, Any]] = []  # threads: {"id", "item" (or None: a pin at "x", "y"), "notes"}
        self._comment_thread: Optional[CommentThread] = None  # the thread being read (made when first needed)
        self._open_thread: Optional[dict[str, Any]] = None  # ...its thread (a new one: not on the page until posted)
        self._comment_hover = None  # an item without comments, hovered: its faint bubble shows (click: comment)
        self._pen: dict[str, Any] = {"color": None, "width": 4, "eraser": False, "smooth": True}  # the pen (kept between sessions)
        self._draw_on = False  # draw mode (D)
        self._stroke: Optional[dict[str, Any]] = None  # a stroke being drawn (or strokes being erased)
        self._draw_bar: Optional[DrawBar] = None
        # Items of kinds this version doesn't know (a newer one's cards): kept as they are, saved back with the page
        self._kept: list[dict[str, Any]] = []
        self._renaming: Optional[dict[str, Any]] = None  # typing a column's title or a line's label: {"item", "text", "before"}
        self.page_name = ""  # the open page's name (its header shows it)
        self._title_timer = QTimer(self)
        self._title_timer.setSingleShot(True)
        self._title_timer.setInterval(0)
        self._title_timer.timeout.connect(self._after_change)
        self._find_query = ""  # find on this page (Ctrl+F): what's looked for
        self._find_matches: list[dict[str, Any]] = []  # each match, in reading order (see _find_all)
        self._find_index = -1  # the current one
        self._find_bar = None  # (ui.artbook_find.FindBar, made when first opened)
        self._guides: Optional[dict[str, Any]] = None  # dragging: the lines it lined up with, the equal gaps
        self._quick_add = None  # (ui.artbook_quick_add.QuickAdd: double-click empty space)
        self.undo_stack.indexChanged.connect(self._queue_title)
        self._line_tool = False  # the Line tool (Shift+L): drag from card to card
        self._dot_card = None  # the card whose dots show (hovered): drag one to draw a line
        self._line_spot: Optional[tuple[QPointF, float]] = None  # over a line: where a point would go (its middle, radius)
        self._line_bar: Optional[LineBar] = None  # floats above the selected lines (made when first needed)
        self._table_bar: Optional[TableBar] = None  # floats above the selected table
        self._card_bar: Optional[CardBar] = None  # floats above the selected link, file or page card
        self._folded_away: set[str] = set()  # cards hidden in folded columns
        self._column_drop: Optional[tuple["ColumnItem", int]] = None  # cards dragged over a column: where they'd go
        self.rail = ToolRail(self)  # what you can add, down the left edge (ui.artbook_rail)
        self.rail.reveal()
        # The Pages submenu of the right-click menu, from the panel (ui.artbook): its entries
        self.page_menu: Optional[Callable[[], list]] = None

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
        self._move_anim = QVariantAnimation(self)
        self._move_anim.setDuration(MOVE_MS)
        self._move_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._move_anim.setStartValue(0.0)
        self._move_anim.setEndValue(1.0)
        self._move_anim.valueChanged.connect(self._on_move_step)
        self._move_anim.finished.connect(self.finish_moves)
        self._moves: dict[ImageItem, tuple[ItemState, ItemState]] = {}  # gliding: item -> (from, to)
        self._area_anim = QVariantAnimation(self)  # the active area gliding to fit the items
        self._area_anim.setDuration(AREA_MS)
        self._area_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._area_anim.setStartValue(0.0)
        self._area_anim.setEndValue(1.0)
        self._area_anim.valueChanged.connect(self._on_area_step)
        self._area_from: Optional[tuple[QRectF, float]] = None
        self._area_target: Optional[tuple[QRectF, float]] = None
        self._scene.changed.connect(self.update_area)
        self._fade_timer = QTimer(self)  # repaints while pictures fade in
        self._fade_timer.setInterval(16)
        self._fade_timer.timeout.connect(self._on_fade_step)

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
        self.nav_hints_shown = get_artbook_nav_hints()
        self.nav_hints = NavHints(self)  # (the mouse controls, bottom right)

    # ------------------ page ------------------
    def load_page(self, page: dict[str, Any]) -> None:
        """Show a page (its images fill in as they're decoded)."""
        self._generation += 1
        if self._group_bar is not None:
            self._group_bar.close_pickers()
        self.cancel_crop()
        self.finish_note_edit()
        self.finish_renaming()
        self.stop_picker()
        self.stop_draw()
        self.stop_line_tool()
        self._dot_card = None
        self._loading = True  # clearing the undo stack isn't a change to save
        self.undo_stack.clear()
        self._loading = False
        self._scene.clearSelection()
        for item in list(self._items.values()):
            self.detach(item)
        self._items.clear()
        self._kept = []
        self.page_id = page["id"]
        self.page_name = str(page.get("name") or "")
        groups = page.get("groups")
        self._trash = clean_trash(page.get("trash"))
        self._comments = clean_comments(page.get("comments"))
        self.close_comments()
        self.trash_changed.emit()
        self._groups = {
            gid: {"fill": meta["fill"]} for gid, meta in (groups.items() if isinstance(groups, dict) else [])
            if _clean_group(gid) and isinstance(meta, dict) and isinstance(meta.get("fill"), str) and QColor(meta["fill"]).isValid()
        }
        self._loads = 0  # (results for the last page are dropped)
        self._queue = []
        self._move_anim.stop()  # (glides and fades on the last page end here)
        self._moves = {}
        self._fade_timer.stop()
        self._area_anim.stop()  # (the new page's area appears where it is, not gliding from the last one's)
        self._scene.active_area = self._area_target = None
        groups = page.get("groups")
        for gid, meta in (groups.items() if isinstance(groups, dict) else []):
            if gid in self._groups and isinstance(meta, dict) and meta.get("locked") is True:
                self._groups[gid]["locked"] = True
            elif _clean_group(gid) and isinstance(meta, dict) and meta.get("locked") is True:
                self._groups[gid] = {"locked": True}
        for data in page.get("items", []):
            if data.get("kind") == "note":
                self.attach(NoteItem.from_dict(data))
                continue
            if data.get("kind") == "stroke":
                self.attach(StrokeItem.from_dict(data))
                continue
            if data.get("kind") == "column":
                self.attach(ColumnItem.from_dict(data))
                continue
            if data.get("kind") == "line":
                self.attach(LineItem.from_dict(data))
                continue
            if data.get("kind") == "point":
                self.attach(PointItem.from_dict(data))
                continue
            if data.get("kind") == "table":
                self.attach(TableItem.from_dict(data))
                continue
            if data.get("kind") in CARD_KINDS:
                self.attach(RefCardItem.from_dict(data))
                continue
            if data.get("kind") != "image":
                if isinstance(data.get("kind"), str) and KIND_PATTERN.fullmatch(data["kind"]):
                    self._kept.append(dict(data))  # (a kind from a newer version: left as it is)
                continue
            self.attach(ImageItem.from_dict(data))
        self._folded_away = set()
        self._clean_columns()
        title = self.title_note()
        if title is not None and title.toPlainText() != self.page_name:
            title.set_title_text(self.page_name)  # (renamed elsewhere: the header follows)
        self.refresh_title()
        self.lay_out_columns()
        self._apply_locks()
        view = page.get("view")
        if isinstance(view, dict):
            self.set_view(float(view.get("zoom", 1.0)), QPointF(float(view.get("x", 0)), float(view.get("y", 0))))
        else:
            self.set_view(1.0, QPointF(0, 0))
        self.set_board_gray(isinstance(view, dict) and view.get("gray") is True)
        self.set_board_values(_clean_values(view.get("values")) if isinstance(view, dict) else 0)
        self._update_hint()
        if self._find_query:
            self.find(self._find_query)

    def page_items(self) -> list[dict[str, Any]]:
        """Every item, bottom to top, then the kept ones of kinds this version doesn't know."""
        self.finish_moves()
        known = [item.to_dict() for item in sorted(self._items.values(), key=lambda i: i.zValue())]
        for data in known:
            if data["kind"] == "column":  # (cards deleted from it, kept for undo: not saved)
                data["members"] = [m for m in data["members"] if isinstance(self._items.get(m), COLUMN_KINDS)]
        known = [data for data in known if data["kind"] != "line" or all(  # (a line to something no longer there)
            (isinstance(self._items.get(end["item"]), LINKABLE) if end["item"] is not None
             else not end.get("group") or bool(self.group_members(end["group"])))
            for end in (data["start"], data["end"]))]
        return known + [dict(data) for data in self._kept]

    def page_groups(self) -> dict[str, dict[str, Any]]:
        """The groups' own settings (their colour), for the groups still on the page."""
        used = {item.group for item in self._items.values() if item.group}
        used |= {data["group"] for data in self._kept if isinstance(data.get("group"), str)}
        return {gid: dict(meta) for gid, meta in self._groups.items() if gid in used}

    def thumbnail(self, width: int = 160, height: int = 120) -> Optional[QImage]:
        """Everything on the page, fitted into width x height (transparent around it); None if empty."""
        self.finish_moves()
        self.end_fades()
        if not self._items:
            return None
        bounds = QRectF()
        for item in self._items.values():
            if item.isVisible():  # (not the cards in folded columns)
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
        values_all, self._scene.values_all = self._scene.values_all, 0
        self._scene.show_area = False
        try:
            self._scene.render(painter, target, bounds)  # items only: the selection frame is the view's
        finally:
            self._scene.gray_all = gray_all
            self._scene.values_all = values_all
            self._scene.show_area = True
        painter.end()
        return image

    def request_export_image(self) -> None:
        """Ctrl+E: a PNG, with the last export settings."""
        if self._items:
            self.export_requested.emit("png", "")

    def request_export(self, fmt: str, area: str = "") -> None:
        if self._items or fmt == "options":
            self.export_requested.emit(fmt, area)

    def export_plan(self, area: str = "auto", size: str = "full", title: bool = True,
                    max_side: Optional[int] = None) -> Optional[tuple[list, QRectF, float, list[tuple[str, dict, int]]]]:
        """(items, the board area, output pixels per board unit, pictures to decode for it) for exporting; None if
        nothing's there. area: EXPORT_AREAS (auto: the selection, or everything); size: EXPORT_SIZES (full: as sharp as
        its sharpest image, at up to EXPORT_DETAIL_SIDE); title: with the page's header. Within EXPORT_MAX_SIDE /
        EXPORT_MAX_PIXELS (max_side: smaller still, for a preview)."""
        self.finish_note_edit()
        self.finish_moves()
        self.end_fades()
        if self._crop:
            self.apply_crop()
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        if area == "view":
            items = [i for i in self.items_list() if i.sceneBoundingRect().intersects(view)]
        elif area in ("auto", "selection") and self.selected():
            items = self.selected()
        else:
            items = self.items_list()
        for column in [i for i in items if isinstance(i, ColumnItem)]:
            items += [card for card in self.column_cards(column) if card not in items]  # (a column comes with its cards)
        items = [item for item in items if item.isVisible() and (title or not (isinstance(item, NoteItem) and item.style == "title"))]
        if not items:
            return None
        if area == "view":
            bounds = QRectF(view)
        else:
            bounds = QRectF()
            for item in items:
                bounds = bounds.united(item.sceneBoundingRect())
            margin = max(bounds.width(), bounds.height()) * EXPORT_MARGIN
            bounds.adjust(-margin, -margin, margin, margin)
        if size in ("1", "2", "4"):
            density = float(size)
        else:
            density = 1.0
            for item in items:
                if isinstance(item, ImageItem) and item.scale() > 0:
                    density = max(density, min(item.longest_side(), EXPORT_DETAIL_SIDE) / (item.longest_side() * item.scale()))
        density = min(
            density, EXPORT_MAX_SIDE / max(bounds.width(), bounds.height()),
            math.sqrt(EXPORT_MAX_PIXELS / (bounds.width() * bounds.height())),
        )
        if max_side is not None:
            density = min(density, max_side / max(bounds.width(), bounds.height()))
        requests = self.picture_requests(items, lambda item: math.ceil(item.longest_side() * abs(item.scale()) * density))
        return items, bounds, density, requests

    def export_threads(self, items: list, bounds: QRectF) -> list[tuple[int, QPointF, dict[str, Any]]]:
        """The comments an export shows, numbered: (number, where its pin goes on the board, the thread)."""
        out = []
        for thread in self.comment_threads():
            if thread.get("item") is not None and self._items.get(thread["item"]) not in items:
                continue
            anchor = self._comment_anchor(thread)
            if anchor is not None and bounds.contains(anchor):
                out.append((len(out) + 1, anchor, thread))
        return out

    def _render_export(self, painter: QPainter, target: QRectF, bounds: QRectF, items: list, pictures: dict[str, QImage],
                       background: str) -> None:
        """What's exported, drawn into target (the board area bounds): only those items, the sharper pictures
        swapped in while it's drawn, on the board's colour (white, or nothing)."""
        if background != "clear":
            painter.fillRect(target, QColor("#ffffff") if background == "white" else theme_color("grid_bg"))
        hidden = [i for i in self.items_list() if i not in items and i.isVisible()]  # (only what's exported shows)
        for item in hidden:
            item.setVisible(False)
        swapped = {}
        for item_id, picture in pictures.items():
            item = self._items.get(item_id)
            if isinstance(item, ImageItem):
                swapped[item] = item.detail
                item.detail = QPixmap.fromImage(picture)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.TextAntialiasing)
        self._scene.show_area = False  # (on the board's own colour)
        try:
            self._scene.render(painter, target, bounds)
        finally:
            self._scene.show_area = True
            for item in hidden:
                item.setVisible(True)
            for item, detail in swapped.items():
                item.detail = detail

    @staticmethod
    def _draw_export_pins(painter: QPainter, pins: list, to_target: Callable[[QPointF], QPointF], radius: float) -> None:
        font = QFont(QApplication.font())
        font.setPixelSize(max(8, round(radius * 1.15)))
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        for number, anchor, _thread in pins:
            center = to_target(anchor)
            painter.setPen(QPen(QColor("#ffffff"), max(1.0, radius / 6)))
            painter.setBrush(theme_color("accent"))
            painter.drawEllipse(center, radius, radius)
            painter.setPen(theme_color("on_accent"))
            painter.drawText(QRectF(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius), Qt.AlignmentFlag.AlignCenter, str(number))

    @staticmethod
    def export_comment_lines(pins: list) -> list[tuple[str, str]]:
        """The exported comments as (pin number, a line of text) rows: each comment, done ones marked."""
        from ui.artbook_comments import when_text

        rows = []
        for number, _anchor, thread in pins:
            for n, note in enumerate(thread["notes"]):
                text = " ".join(note["text"].split())
                rows.append((str(number) if n == 0 else "", f"{text}  ({when_text(note['time'])}{', done' if note['done'] else ''})"))
        return rows

    def _draw_comment_list(self, painter: QPainter, area: QRectF, rows: list[tuple[str, str]], ink: QColor) -> None:
        font = QFont(QApplication.font())
        font.setPixelSize(max(10, round(area.width() / 80)))
        painter.setFont(font)
        line = QFontMetricsF(font).lineSpacing() * 1.3
        y = area.top()
        painter.setPen(ink)
        for number, text in rows:
            if y + line > area.bottom():
                break
            painter.drawText(QRectF(area.left(), y, line * 1.6, line), int(Qt.AlignmentFlag.AlignVCenter), number)
            painter.drawText(QRectF(area.left() + line * 1.8, y, area.width() - line * 1.8, line), int(Qt.AlignmentFlag.AlignVCenter),
                             QFontMetricsF(font).elidedText(text, Qt.TextElideMode.ElideRight, area.width() - line * 1.8))
            y += line

    def export_image(self, pictures: Optional[dict[str, QImage]] = None, area: str = "auto", size: str = "full",
                     title: bool = True, background: str = "board", pins: bool = False,
                     max_side: Optional[int] = None) -> Optional[QImage]:
        """What's exported (see export_plan) as one picture, on the board's colour (white, or see-through), with the
        comments' numbered pins and a list of them under it (pins). pictures: the sharper pictures export_plan asked
        for (default: decoded here and now; {}: the ones loaded, for a preview)."""
        plan = self.export_plan(area, size, title, max_side)
        if plan is None:
            return None
        items, bounds, density, requests = plan
        if pictures is None:
            pictures = export_pictures(self.page_id, requests)
        size_px = QSize(max(1, round(bounds.width() * density)), max(1, round(bounds.height() * density)))
        threads = self.export_threads(items, bounds) if pins else []
        rows = self.export_comment_lines(threads)
        line = max(10, round(size_px.width() / 80)) * 1.6
        extra = round(len(rows) * line * 1.3 + 2 * line) if rows else 0
        image = QImage(QSize(size_px.width(), size_px.height() + extra), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            self._render_export(painter, QRectF(0, 0, size_px.width(), size_px.height()), bounds, items, pictures, background)
            if threads:
                to_target = lambda point: (point - bounds.topLeft()) * density
                self._draw_export_pins(painter, threads, to_target, max(9.0, size_px.width() / 120))
                list_area = QRectF(line, size_px.height(), size_px.width() - 2 * line, extra)
                if background != "clear":
                    painter.fillRect(QRectF(0, size_px.height(), size_px.width(), extra),
                                     QColor("#ffffff") if background == "white" else theme_color("grid_bg"))
                ink = QColor("#202124") if background == "white" else theme_color("text")
                self._draw_comment_list(painter, list_area.adjusted(0, line, 0, 0), rows, ink)
        finally:
            painter.end()
        return image

    def export_pdf(self, path: Path, pictures: dict[str, QImage], area: str = "auto", size: str = "full", title: bool = True,
                   background: str = "board", pins: bool = False, links: bool = True) -> bool:
        """What's exported (see export_plan) as a PDF page: text and shapes stay sharp at any zoom, link and file cards
        open what they stand for when clicked (links), the comments' pins on it and their list on a second page
        (pins). False if there's nothing to export."""
        plan = self.export_plan(area, size, title)
        if plan is None:
            return False
        items, bounds, _density, _requests = plan
        scale = min(1.0, PDF_MAX_POINTS / max(bounds.width(), bounds.height()))  # (a board unit is a point)
        writer = QPdfWriter(str(path))
        writer.setResolution(72)
        writer.setTitle(self.page_name or "Artbook page")
        writer.setCreator("Asset Manager")
        writer.setPageSize(QPageSize(QSizeF(bounds.width() * scale, bounds.height() * scale), QPageSize.Unit.Point))
        writer.setPageMargins(QMarginsF(0, 0, 0, 0))
        painter = QPainter(writer)
        if not painter.isActive():
            raise store.PageError(f"Couldn't write {Path(path).name}.")
        try:
            target = QRectF(0, 0, writer.width(), writer.height())
            self._render_export(painter, target, bounds, items, pictures, background)
            factor = target.width() / bounds.width()
            to_target = lambda point: (point - bounds.topLeft()) * factor
            if links:
                for card in (i for i in items if isinstance(i, RefCardItem)):
                    url = card.data.get("url") if card.kind == "link" else (
                        QUrl.fromLocalFile(card.data["path"]).toString() if card.kind == "file" and card.data.get("path") else "")
                    if url:
                        box = card.sceneBoundingRect()
                        self._pdf_link(painter, QRectF(to_target(box.topLeft()), to_target(box.bottomRight())), url)
            threads = self.export_threads(items, bounds) if pins else []
            if threads:
                self._draw_export_pins(painter, threads, to_target, max(7.0, target.width() / 120))
                writer.newPage()
                margin = target.width() / 20
                ink = QColor("#202124") if background != "board" else theme_color("text")
                if background == "board":
                    painter.fillRect(target, theme_color("grid_bg"))
                self._draw_comment_list(painter, target.adjusted(margin, margin, -margin, -margin), self.export_comment_lines(threads), ink)
        finally:
            painter.end()
        return True

    @staticmethod
    def _pdf_link(painter: QPainter, rect: QRectF, url: str) -> None:
        """A clickable area in a PDF (Qt's PDF writer makes a link of anchored text: an invisible one, stretched over
        rect)."""
        document = QTextDocument()
        document.setDocumentMargin(0)
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(url)
        fmt.setForeground(QColor(0, 0, 0, 0))
        font = QFont(QApplication.font())
        font.setPixelSize(100)
        fmt.setFont(font)
        QTextCursor(document).insertText("M", fmt)
        metrics = QFontMetricsF(font)
        natural = QSizeF(document.size().width(), metrics.ascent() + metrics.descent())  # (what the link covers)
        painter.save()
        painter.translate(rect.topLeft())
        painter.scale(rect.width() / max(1.0, natural.width()), rect.height() / max(1.0, natural.height()))
        document.drawContents(painter)
        painter.restore()

    def view_state(self) -> dict[str, Any]:
        center = self.view_center()
        return {"x": center.x(), "y": center.y(), "zoom": self.zoom(), "gray": self._scene.gray_all, "values": self._scene.values_all}

    def items_list(self) -> list[ImageItem]:
        return list(self._items.values())

    def item(self, item_id: str) -> Optional[ImageItem]:
        return self._items.get(item_id)

    def attach(self, item: ImageItem) -> None:
        if isinstance(item, NoteItem):
            item.on_edit_finished = self._on_note_edit_finished
            item.on_paragraph = self.set_note_paragraph
            if item.style == "title":
                if any(other is not item and isinstance(other, NoteItem) and other.style == "title" for other in self._items.values()):
                    item.set_content((item.toHtml(), item.fill, "heading"))  # (one header per page: this one's a heading now)
                else:
                    item.setToolTip("The page's name: double-click to rename the page (or the line under it, to change that)")
        if isinstance(item, (NoteItem, ColumnItem, LineItem, TableItem)) and not getattr(item, "_follows", False):
            item._follows = True  # (once: undo and redo attach it again)
            if isinstance(item, NoteItem):
                texts = [item]
            elif isinstance(item, TableItem):
                texts = [item.title_item, item.editor]
            else:
                texts = [item.title_item if isinstance(item, ColumnItem) else item.label_item]
            for text in texts:
                text.document().contentsChanged.connect(self._on_text_changed)  # (taller or shorter: the column follows)
        self._items[item.item_id] = item
        if isinstance(item, RefCardItem):
            self._load_card(item)
        if item.scene() is None:
            self._scene.addItem(item)
        if any(meta.get("locked") for meta in self._groups.values()):
            self._apply_locks()
        self._update_hint()

    def detach(self, item: ImageItem) -> None:
        self._items.pop(item.item_id, None)
        if item.scene() is self._scene:
            item.setSelected(False)
            self._scene.removeItem(item)
        self._update_hint()

    def apply_states(self, states: dict[str, ItemState], animate: bool = False) -> None:
        """Put items in these states. animate: they glide to their new place, size and turn (the
        rest changes at once); state() reads the glide's end straight away after finish_moves."""
        self.finish_moves()
        animate = animate and animations.motion_enabled() and self.isVisible()
        moves = {}
        for item_id, state in states.items():
            item = self._items.get(item_id)
            if item is None:
                continue
            start = item.state()
            item.apply_state(state)
            if isinstance(item, RefCardItem) and item.data.get("picture") != item.image_blob:
                self._load_card(item)  # (its picture changed back: undo, redo)
            if animate and start.crop == state.crop and (start.x, start.y, start.scale, start.rotation) != (state.x, state.y, state.scale, state.rotation):
                moves[item] = (start, state)
        if any(meta.get("locked") for meta in self._groups.values()):
            self._apply_locks()  # (a group's members may have changed)
        if moves:
            self._moves = moves
            self._on_move_step(0.0)
            self._move_anim.start()
        self.lay_out_columns()
        self.viewport().update()

    def _on_move_step(self, value) -> None:
        t = float(value)
        for item, (a, b) in self._moves.items():
            item.setPos(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t)
            if a.scale > 0 and b.scale > 0:
                item.setScale(a.scale * (b.scale / a.scale) ** t)  # (even steps, however big the change)
            turn = (b.rotation - a.rotation + 180.0) % 360.0 - 180.0  # the short way round
            item.setRotation(a.rotation + turn * t)
        self.lay_out_columns()  # (a gliding column's cards go with it)
        self.viewport().update()

    def finish_moves(self) -> None:
        """End any glide now, everything where it's going (before reading or changing states)."""
        if not self._moves:
            return
        moves, self._moves = self._moves, {}
        self._move_anim.stop()
        for item, (_start, end) in moves.items():
            item.setPos(end.x, end.y)
            item.setScale(end.scale)
            item.setRotation(end.rotation)
        self.lay_out_columns()
        self._place_note_bar()
        self.viewport().update()

    # ------------------ the active area ------------------
    def area_box(self) -> Optional[tuple[QRectF, float]]:
        """(the active area: around every item and group panel, with room to spare; its corners'
        radius), or None for an empty board."""
        boxes = [item.sceneBoundingRect() for item in self._items.values() if item.isVisible()]
        if not boxes:
            return None
        area = QRectF(boxes[0])
        for box in boxes[1:]:
            area = area.united(box)
        for box in self.group_backdrop_boxes().values():
            area = area.united(box)
        sides = sorted(max(box.width(), box.height()) for box in boxes)
        pad = max(sides[len(sides) // 2] * AREA_PAD_ITEM, max(area.width(), area.height()) * AREA_PAD_BOARD)
        return area.adjusted(-pad, -pad, pad, pad), pad * AREA_RADIUS

    def update_area(self, *_args) -> None:
        """Fit the active area to the items again: it glides there (or jumps, with motion off)."""
        target = self.area_box()
        if target == self._area_target:
            return
        self._area_target = target
        current = self._scene.active_area
        if target is None or current is None or not animations.motion_enabled() or not self.isVisible():
            self._area_anim.stop()
            self._scene.active_area = target
            self.viewport().update()
            return
        self._area_from = current
        self._area_anim.stop()
        self._area_anim.start()

    def _on_area_step(self, value) -> None:
        if self._area_from is None or self._area_target is None:
            return
        t = float(value)
        (a, ra), (b, rb) = self._area_from, self._area_target
        self._scene.active_area = (QRectF(
            a.left() + (b.left() - a.left()) * t, a.top() + (b.top() - a.top()) * t,
            a.width() + (b.width() - a.width()) * t, a.height() + (b.height() - a.height()) * t,
        ), ra + (rb - ra) * t)
        self.viewport().update()

    def _start_fade(self, item: ImageItem) -> None:
        if animations.motion_enabled() and self.isVisible():
            item.fade_from = time.monotonic()
            if not self._fade_timer.isActive():
                self._fade_timer.start()

    def _on_fade_step(self) -> None:
        fading = [item for item in self._items.values() if isinstance(item, ImageItem) and item.fade_from is not None]
        for item in fading:
            item.fade()  # (ends it once it's done)
            item.update()
        if all(item.fade_from is None for item in fading):
            self._fade_timer.stop()

    def end_fades(self) -> None:
        for item in self._items.values():
            if isinstance(item, ImageItem):
                item.fade_from = None

    # ------------------ level of detail ------------------
    def _schedule_detail(self, *_args) -> None:
        if not self._detail_timer.isActive():
            self._detail_timer.start()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        self._schedule_detail()

    def update_detail(self) -> None:
        """Give each image the picture it needs for how it's seen now (see the module docstring):
        sharper copies for the ones in or near the view, nearest the middle first and within
        DETAIL_BUDGET, small copies for the rest; sharper copies out of view are let go."""
        if self.page_id is None:
            return
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        near = view.adjusted(-view.width() * NEAR_VIEW, -view.height() * NEAR_VIEW, view.width() * NEAR_VIEW, view.height() * NEAR_VIEW)
        middle = view.center()
        ratio = self.zoom() * self.devicePixelRatioF()
        images = []
        for item in self._items.values():
            if isinstance(item, ImageItem):
                box = item.sceneBoundingRect()
                offset = box.center() - middle
                images.append((offset.x() ** 2 + offset.y() ** 2, item, box.intersects(near)))
        images.sort(key=lambda entry: entry[0])
        budget = DETAIL_BUDGET
        wanted: list[tuple[ImageItem, int]] = []  # what to decode, most wanted first
        small: list[tuple[ImageItem, int]] = []  # small copies for images out of view (after those)
        for _distance, item, in_view in images:
            if not in_view:
                item.drop_detail()
                if not item.base_ok and not item.load_failed:
                    small.append((item, 0))
                continue
            side = detail_side(item.natural, item.longest_side() * abs(item.scale()) * ratio)
            if side and _detail_bytes(item.natural, side) > budget:
                side = 0
            budget -= _detail_bytes(item.natural, side) if side else 0
            if not side:
                item.drop_detail()
                if not item.base_ok and not item.load_failed:
                    wanted.append((item, 0))
            elif not item.load_failed and not (side <= item.detail_side <= side * 4):
                wanted.append((item, side))
        self._queue = wanted + small
        self._load_next()

    def _load_next(self) -> None:
        """Start decodes from the queue update_detail made, up to DETAIL_LOADS at once."""
        while self._queue and self._loads < DETAIL_LOADS:
            batch: list[tuple[ImageItem, int]] = []
            while self._queue and len(batch) < SMALL_BATCH:
                item, side = self._queue[0]
                if item.loading or item.load_failed or self._items.get(item.item_id) is not item:
                    self._queue.pop(0)
                    continue
                if side and batch:
                    break  # (a sharper copy goes on its own)
                batch.append(self._queue.pop(0))
                if side:
                    break
            if batch:
                self._load(batch)

    def _load(self, batch: list[tuple[ImageItem, int]]) -> None:
        """Decode pictures in the background: one sharper copy, or a few small ones (quick to read)."""
        self._loads += 1
        page_id, generation = self.page_id, self._generation
        jobs = []
        for item, side in batch:
            item.loading = side or -1
            jobs.append((item, item.load_token, dict(item.source), side, not item.base_ok))
        requests = [(source, side, base) for _item, _token, source, side, base in jobs]
        run_job(
            lambda: _load_pictures(page_id, requests),
            lambda results: self._on_pictures_loaded(jobs, generation, results),
            lambda error: self._on_pictures_loaded(jobs, generation, [(None, error)] * len(jobs)),
        )

    def _on_pictures_loaded(self, jobs: list, generation: int, results: list) -> None:
        if generation != self._generation:
            return
        self._loads -= 1
        failures = []
        changed = False
        for (item, token, _source, _side, _base), (result, error) in zip(jobs, results):
            if token != item.load_token:
                continue  # (its picture changed meanwhile)
            item.loading = 0
            if self._items.get(item.item_id) is not item:
                continue
            if result is None:
                if not item.load_failed:
                    item.load_failed = True  # (not tried again until it's relinked or the page reopens)
                    failures.append(error)
                    changed = True
                continue
            detail, base = result
            if not item.base_ok:
                changed = True
                if item.pixmap is None:
                    self._start_fade(item)
            item.load_failed = False
            item.set_pictures(
                QPixmap.fromImage(base) if base is not None else None,
                QPixmap.fromImage(detail) if detail is not None else item.detail,
            )
        if changed:
            self.files_changed.emit()
        if failures:
            more = f" (and {len(failures) - 1} more)" if len(failures) > 1 else ""
            self.show_message(f"An image couldn't be loaded: {failures[0]}{more}")
        self._load_next()  # (the next straight away; the view's next tick redoes the list)

    def sharp_pictures(self, items: list, side_for: Callable[[ImageItem], int]) -> dict[str, QImage]:
        """Pictures of these images decoded side_for(item) big, where that's sharper than what's
        loaded (on this thread: see export_pictures for doing it in the background)."""
        return export_pictures(self.page_id, self.picture_requests(items, side_for))

    def picture_requests(self, items: list, side_for: Callable[[ImageItem], int]) -> list[tuple[str, dict, int]]:
        """(item id, source, side) for each image whose loaded picture is less sharp than side_for(item)."""
        requests = []
        for item in items:
            if isinstance(item, ImageItem) and not item.load_failed:
                side = min(side_for(item), item.longest_side())
                loaded = item.pixmap
                if loaded is None or max(loaded.width(), loaded.height()) < side:
                    requests.append((item.item_id, dict(item.source), side))
        return requests

    def _on_undo_index_changed(self, _index: int) -> None:
        self.lay_out_columns()  # (whatever changed, the columns fit their cards)
        if not self._loading:
            self.changed.emit()
            self._sync_note_bar()  # (an undone style or colour shows on the bar too)

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
        self.nav_hints.place()

    def toggle_nav_hints(self) -> None:
        """Right-click > Canvas > Navigation Hints: show / hide the mouse controls in the bottom-right corner."""
        self.nav_hints_shown = not self.nav_hints_shown
        set_artbook_nav_hints(self.nav_hints_shown)
        self.nav_hints.place()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_overlays()
        self._schedule_detail()
        if self._image_list is not None and self._image_list.isVisible():
            self._image_list.place()
        if self._tray is not None and self._tray.isVisible():
            self._tray.place()
        if self._find_bar is not None and self._find_bar.isVisible():
            self._find_bar.place()

    @property
    def image_list(self) -> ImageListPanel:
        if self._image_list is None:
            self._image_list = ImageListPanel(self)
        return self._image_list

    def toggle_image_list(self) -> None:
        """L: show / hide the list of every image on the page."""
        if self.tray_open():
            self.toggle_tray()  # (both sit on the right: one at a time)
        self.image_list.toggle()
        self.nav_hints.place()  # (left of the list while it's open)
        self.image_list_toggled.emit(self.image_list.isVisible())

    @property
    def tray(self) -> TrayPanel:
        if self._tray is None:
            self._tray = TrayPanel(self)
            self.unsorted_changed.connect(self._tray.refresh)
        return self._tray

    def tray_open(self) -> bool:
        return self._tray is not None and self._tray.isVisible()

    def toggle_tray(self, tab: Optional[str] = None) -> None:
        """U (or the tray button): show / hide Unsorted and the page's Trash (tab: show that one)."""
        if self.tray_open() and tab is None:
            self._tray.hide()
        else:
            if self.image_list_open():
                self.toggle_image_list()
            self.tray.place()
            self.tray.show()
            self.tray.raise_()
            self.tray.show_tab(tab or self.tray.tab)
        self.nav_hints.place()  # (left of the tray while it's open)
        self.tray_toggled.emit(self.tray_open())

    @staticmethod
    def trash_days() -> int:
        return TRASH_DAYS

    def place_unsorted(self, entry_ids: list[str], at: Optional[QPointF] = None) -> int:
        """Put things from Unsorted on this page, in a row from `at` (the view's middle); they leave Unsorted."""
        wanted = [entry_id for entry_id in entry_ids if entry_id]
        found = {entry["id"]: entry for entry in artbook_unsorted.entries() if entry["id"] in wanted}
        chosen = [found[entry_id] for entry_id in wanted if entry_id in found]
        if not chosen or self.page_id is None:
            return 0
        self._start_batch(at)
        placed, gone = 0, 0
        for entry in chosen:
            picture = entry.get("picture_path")
            data = picture.read_bytes() if picture is not None else None
            if entry["kind"] == "image" and data:
                ext, name = picture.suffix.lower(), entry.get("name", "")
                self._prepare(lambda page_id, d=data, e=ext, n=name: {**_prepare_bytes(page_id, d, e), "name": n})
            elif entry["kind"] == "link" and entry.get("url"):
                icon_path = entry.get("icon_path")
                icon = icon_path.read_bytes() if icon_path is not None else None
                self._prepare(lambda page_id, e=dict(entry), d=data, i=icon: _prepare_link_entry(page_id, e, d, i))
            elif entry["kind"] == "file" and entry.get("path") and Path(entry["path"]).is_file():
                path = Path(entry["path"])
                if path.suffix.lower() in store.IMAGE_EXTENSIONS:
                    self._prepare(lambda page_id, p=path: _prepare_file(page_id, p))
                else:
                    self._prepare(lambda page_id, p=path: _prepare_file_card(page_id, p))
            else:
                gone += 1
                continue
            placed += 1
            artbook_unsorted.remove(entry["id"])
        if gone:
            self.show_message(f"{gone} of them couldn't be placed (a file that isn't there any more): they stay in Unsorted.")
        self.unsorted_changed.emit()
        return placed

    def add_link_entries(self, links: list, at: Optional[QPointF] = None) -> None:
        """Pages the browser extension sent for this page, as (details, preview's data, site icon's data): their link
        cards (title, preview, icon, as the browser saw them), in a row."""
        self._start_batch(at)
        for entry, picture, *rest in links:
            icon = rest[0] if rest else None
            self._prepare(lambda page_id, e=dict(entry), d=picture, i=icon: _prepare_link_entry(page_id, e, d, i))

    def image_list_open(self) -> bool:
        return self._image_list is not None and self._image_list.isVisible()

    # ------------------ adding images ------------------
    def add_files(self, paths: list[Path], at: Optional[QPointF] = None) -> int:
        """Add files: images (linked, with a fallback copy), and a card for each other file (a 3D model, a ZBrush
        tool...: linked to it); returns how many went on the page."""
        files = [Path(p) for p in paths if Path(p).is_file() and not str(p).lower().endswith((pur_import.SUFFIX, artbook_share.SUFFIX))]
        self._start_batch(at)
        for path in files:
            if path.suffix.lower() in store.IMAGE_EXTENSIONS:
                self._prepare(lambda page_id, path=path: _prepare_file(page_id, path))
            else:
                self._prepare(lambda page_id, path=path: _prepare_file_card(page_id, path))
        return len(files)

    def add_web_link(self, url: str, at: Optional[QPointF] = None) -> None:
        """A web address: its picture if it's an image's, else a link card for the page."""
        self._start_batch(at)
        self.show_message("Reading the link...")
        self._prepare(lambda page_id: _prepare_link(page_id, url))

    def add_image(self, image: QImage, at: Optional[QPointF] = None, name: str = "") -> None:
        """A picture (pasted, or a PDF page sent here); name: what the image list calls it."""
        self._start_batch(at)
        copy = QImage(image)  # the clipboard's image may change before the worker reads it
        self._prepare(lambda page_id: {**_prepare_qimage(page_id, copy), "name": name})

    def add_pictures_data(self, pictures: list[tuple[bytes, str, str]], at: Optional[QPointF] = None) -> None:
        """Pictures as file data, each (data, extension, name), in a row: sent from the browser
        (core.browser_link). Stored in full: there's no file to link to."""
        self._start_batch(at)
        for data, ext, name in pictures:
            self._prepare(lambda page_id, d=data, e=ext, n=name: {**_prepare_bytes(page_id, d, e), "name": n})

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
        if "card" in result:
            self._place_new_card(result, batch)
            return
        width, height = result["size"]
        item = ImageItem(uuid.uuid4().hex, result["source"], (width, height))
        item.name = str(result.get("name") or "")[:MAX_NAME]
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
        """Ctrl+V: cards copied in the Artbook (from any page), image files, image data, or an image link."""
        mime = QApplication.clipboard().mimeData()
        return self.paste_package(mime, at) or self._take(mime, at)

    @staticmethod
    def can_paste() -> bool:
        """Whether the clipboard holds something paste() can use (greys out Paste in the menu)."""
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return False
        if mime.hasUrls() or mime.hasImage() or mime.hasFormat(PACKAGE_MIME):
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
        if links and set(web_image_sources(links[:20], html)) <= set(links):  # (no picture with them: each is an
            for link in links[:20]:                                              # image's address, or a page's)
                self.add_web_link(link, at)
            return True
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
        if mime.hasFormat(TOOL_MIME) or mime.hasFormat(PAGE_MIME) or mime.hasFormat(UNSORTED_MIME) or mime.hasUrls() or mime.hasImage() or mime.hasHtml() or (mime.hasText() and _is_web_url(mime.text().strip())):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dropEvent(self, event) -> None:
        at = self.mapToScene(event.position().toPoint())
        if event.mimeData().hasFormat(TOOL_MIME):  # a tool dragged off the rail: it goes where it was let go
            tool = bytes(event.mimeData().data(TOOL_MIME)).decode("ascii", "replace")
            if any(t is not None and t.tool_id == tool and t.draggable for t in TOOLS):
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
                self.setFocus()
                self.add_tool(tool, at)
            else:
                event.ignore()
            return
        if event.mimeData().hasFormat(UNSORTED_MIME):  # (artbook.unsorted_drag) from the tray: placed where let go
            ids = bytes(event.mimeData().data(UNSORTED_MIME)).decode("utf-8", "replace").split()
            if self.place_unsorted(ids, at):
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
                self.setFocus()
            else:
                event.ignore()
            return
        if event.mimeData().hasFormat(PAGE_MIME):  # (artbook.page_card_drag) a page from the Pages list: its card
            ids = bytes(event.mimeData().data(PAGE_MIME)).decode("utf-8", "replace").split()
            made = [self.add_page_card(page_id, at) for page_id in ids[:20]]
            if any(card is not None for card in made):
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
                self.setFocus()
            else:
                event.ignore()
            return
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
        return self.transform().m22()  # (m11 is negative while the board is mirrored)

    def _view_transform(self, zoom: float) -> QTransform:
        return QTransform.fromScale(-zoom if self.mirrored else zoom, zoom)

    def view_center(self) -> QPointF:
        return self.mapToScene(self.viewport().rect().center())

    def set_view(self, zoom: float, center: QPointF) -> None:
        zoom = max(ZOOM_RANGE[0], min(ZOOM_RANGE[1], zoom))
        self.setTransform(self._view_transform(zoom))
        self.centerOn(center)
        self.zoom_changed.emit(zoom)
        self._schedule_detail()

    def zoom_at(self, zoom: float, anchor: QPointF) -> None:
        """Zoom keeping the board point under `anchor` (a viewport position) where it is."""
        zoom = max(ZOOM_RANGE[0], min(ZOOM_RANGE[1], zoom))
        fixed = self.mapToScene(anchor.toPoint())
        self.setTransform(self._view_transform(zoom))
        moved = self.mapFromScene(fixed) - anchor.toPoint()
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + moved.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + moved.y())
        self.zoom_changed.emit(zoom)
        self._schedule_detail()

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
        if not self._expanding and not self._single_pick:
            groups = {item.group for item in self.selected() if item.group}
            missing = [i for i in self._items.values() if i.group in groups and not i.isSelected()] if groups else []
            if missing:
                self._expanding = True  # selecting one of a group selects all of it
                try:
                    for item in missing:
                        item.setSelected(True)
                finally:
                    self._expanding = False
        self.viewport().update()

    # ------------------ groups ------------------
    def group_members(self, group: Optional[str]) -> list[ImageItem]:
        return [i for i in self._items.values() if group and i.group == group]

    def group_backdrops(self) -> list[tuple[QRectF, Optional[str]]]:
        """Each group's panel (around its members, with a margin) and colour (None: the theme's)."""
        return [(box, self.group_fill(gid)) for gid, box in self.group_backdrop_boxes().items()]

    def group_backdrop_boxes(self) -> dict[str, QRectF]:
        """Each group's panel (groups of two or more), by group id."""
        boxes: dict[str, QRectF] = {}
        counts: dict[str, int] = {}
        for item in self._items.values():
            if item.group and item.isVisible():
                rect = item.sceneBoundingRect()
                boxes[item.group] = boxes[item.group].united(rect) if item.group in boxes else rect
                counts[item.group] = counts.get(item.group, 0) + 1
        result = {}
        for gid, box in boxes.items():
            if counts[gid] >= 2:
                pad = max(box.width(), box.height()) * GROUP_PADDING
                result[gid] = box.adjusted(-pad, -pad, pad, pad)
        return result

    def group_selection(self) -> None:
        """Ctrl+G: the selection becomes one group (its members leave any group they were in)."""
        items = [item for item in self.selected() if self.column_of(item) is None and not isinstance(item, LineItem)]
        if len(items) < 2:
            return
        gid = uuid.uuid4().hex
        self._change_selection("group", lambda _item, s: replace(s, group=gid), items)

    def ungroup_selection(self) -> None:
        """Ctrl+Shift+G: the selected groups come apart (everything stays where it is)."""
        items = [i for i in self.selected() if i.group]
        self._change_selection("ungroup", lambda _item, s: replace(s, group=None), items)

    # ------------------ drawing ------------------
    def drawing(self) -> bool:
        return self._draw_on

    @property
    def draw_bar(self) -> DrawBar:
        if self._draw_bar is None:
            self._draw_bar = DrawBar(self)
        return self._draw_bar

    def toggle_draw(self) -> None:
        """D: draw freehand strokes on the board (D again, Esc or Done to stop)."""
        if self._draw_on:
            self.stop_draw()
        else:
            self.start_draw()

    def start_draw(self) -> None:
        self.stop_line_tool()
        self.finish_note_edit()
        self.stop_picker()
        if self._crop:
            self.apply_crop()
        self._draw_on = True
        self._scene.clearSelection()
        self._sync_draw_bar()
        bar = self.draw_bar
        bar.resize(bar.sizeHint())
        bar.move((self.width() - bar.width()) // 2, DRAW_BAR_TOP)
        bar.show()
        bar.raise_()
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self.rail.sync()

    def stop_draw(self) -> None:
        if not self._draw_on:
            return
        self._finish_stroke()
        self._draw_on = False
        if self._draw_bar is not None:
            self._draw_bar.close_pickers()
            self._draw_bar.hide()
        self.viewport().unsetCursor()
        self.rail.sync()

    def set_pen_color(self, color: Optional[str]) -> None:
        self._pen["color"] = color
        self._sync_draw_bar()

    def set_pen_width(self, width: int) -> None:
        self._pen["width"] = max(1, int(width))
        self._sync_draw_bar()

    def toggle_smoothing(self) -> None:
        """Draw: smoothing on / off (steadies lines drawn with a mouse)."""
        self._pen["smooth"] = not self._pen["smooth"]
        self._sync_draw_bar()

    def toggle_eraser(self) -> None:
        """E (while drawing): drag over strokes to remove them."""
        self._pen["eraser"] = not self._pen["eraser"]
        self._sync_draw_bar()

    def _sync_draw_bar(self) -> None:
        if self._draw_bar is not None or self._draw_on:
            color = QColor(self._pen["color"]) if self._pen["color"] else theme_color("text_strong")
            self.draw_bar.sync(color, self._pen["width"], self._pen["eraser"], self._pen["smooth"])

    def _begin_stroke(self, pos: QPointF) -> None:
        if self._pen["eraser"]:
            self._stroke = {"erased": [], "last": pos}
            self._erase_at(pos)
            return
        start = self.mapToScene(pos.toPoint())
        item = StrokeItem(uuid.uuid4().hex, [QPointF()], self._pen["color"], self._pen["width"] / self.zoom())
        item.setPos(start)
        item.setZValue(max((i.zValue() for i in self._items.values()), default=0.0) + 1.0)
        self._scene.addItem(item)  # (on the board while it's drawn; added for good when it's done)
        self._stroke = {"item": item, "last": pos}

    def _extend_stroke(self, pos: QPointF) -> None:
        stroke = self._stroke
        if "erased" in stroke:
            # Along the whole way from the last point: a quick drag mustn't skip a stroke
            last = stroke["last"]
            steps = max(1, math.ceil(math.hypot(pos.x() - last.x(), pos.y() - last.y()) / ERASER_PX))
            for n in range(1, steps + 1):
                self._erase_at(last + (pos - last) * (n / steps))
            stroke["last"] = pos
            return
        stroke["end"] = self.mapToScene(pos.toPoint())  # (where the pointer is: smoothing trails behind it)
        if math.hypot(pos.x() - stroke["last"].x(), pos.y() - stroke["last"].y()) < STROKE_STEP_PX:
            return
        stroke["last"] = pos
        item = stroke["item"]
        point = self.mapToScene(pos.toPoint()) - item.pos()
        if self._pen["smooth"]:
            point = item.points[-1] + (point - item.points[-1]) * SMOOTH_FOLLOW  # (trails the pointer: no wobble)
        item.set_points(item.points + [point])

    def _erase_at(self, pos: QPointF) -> None:
        reach = ERASER_PX
        area = QRect(round(pos.x()) - reach, round(pos.y()) - reach, 2 * reach, 2 * reach)
        for item in self.items(area):
            if isinstance(item, StrokeItem) and item.isVisible() and item.item_id in self._items and not self.is_locked(item):
                item.setVisible(False)  # (gone for good when the drag ends: one undo step)
                self._stroke["erased"].append(item)

    def _finish_stroke(self) -> None:
        stroke, self._stroke = self._stroke, None
        if stroke is None:
            return
        if "erased" in stroke:
            for item in stroke["erased"]:
                item.setVisible(True)
            if stroke["erased"]:
                self.undo_stack.push(_RemoveCommand(self, stroke["erased"], "erase"))
            return
        item = stroke["item"]
        if self._pen["smooth"] and len(item.points) > 2:
            ends = [stroke["end"] - item.pos()] if "end" in stroke else []
            item.set_points(smooth_points(item.points + ends))  # (to where the pointer let go, corners rounded)
        self.undo_stack.push(_AddCommand(self, [item], "draw"))

    # ------------------ files: copy path, Explorer, open, replace, relink ------------------
    def _images_in_order(self) -> list[ImageItem]:
        return sorted(self.selected_images(), key=lambda item: item.zValue())  # (back to front: always the same)

    def selected_files(self) -> list[Path]:
        """The files the selected images are linked to (that are still there)."""
        return [path for item in self._images_in_order() if (path := item.linked_path()) is not None and path.is_file()]

    def copy_file_paths(self) -> bool:
        """Copy the selected images' file paths, one per line (their linked files, there or not)."""
        paths = [str(path) for item in self._images_in_order() if (path := item.linked_path()) is not None]
        if paths:
            QApplication.clipboard().setText("\n".join(paths))
        return bool(paths)

    def show_in_explorer(self) -> None:
        files = self.selected_files()
        if files:
            file_actions.reveal(files[0])

    def open_source_files(self) -> None:
        """Open the selected images' files in the app Windows uses for them (a few at most)."""
        for path in self.selected_files()[:OPEN_FILES_LIMIT]:
            file_actions.open_file(path)

    def replace_image(self, path: Optional[Path] = None) -> None:
        """Swap the one selected image's picture for another file (asks which). It keeps its place,
        its size on the board, its crop (as a share of the picture) and its adjustments."""
        images = self.selected_images()
        if len(images) != 1 or len(self.selected()) != 1 or self.page_id is None or self.is_locked(images[0]):
            return
        if path is None:
            extensions = " ".join(f"*{ext}" for ext in sorted(store.IMAGE_EXTENSIONS))
            chosen, _filter = QFileDialog.getOpenFileName(self, "Replace Image", "", f"Images ({extensions})")
            if not chosen:
                return
            path = Path(chosen)
        item, page_id, generation = images[0], self.page_id, self._generation
        self.show_message(f"Replacing with {Path(path).name}...")
        run_job(
            lambda: _prepare_file(page_id, Path(path)),
            lambda result: self._on_replacement(item, result, generation),
            self.show_message,
        )

    def _on_replacement(self, item: ImageItem, result: dict[str, Any], generation: int) -> None:
        if generation != self._generation or item.item_id not in self._items:
            return
        image = result["image"]
        pictures = (QPixmap.fromImage(_base_image(image)), True, None if _fits_base(image) else QPixmap.fromImage(image))
        before, after = self._picture_swap(item, result["source"], result["size"], "", pictures, None)
        self.undo_stack.push(_ReplaceCommand(item, before, after))
        self.viewport().update()

    @staticmethod
    def _picture_swap(
        item: ImageItem, source: dict[str, Any], size: tuple[int, int], name: str, pictures: tuple,
        original: Optional[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """(before, after) for _ReplaceCommand: the item showing another picture in the same place,
        at the same size on the board, with its crop (as a share of the picture) and adjustments."""
        state = item.state()
        old_w, old_h = item.natural
        new_w, new_h = max(1, int(size[0])), max(1, int(size[1]))
        crop = None
        if item.crop:
            left, top, right, bottom = item.crop
            crop = _clean_crop((left / old_w * new_w, top / old_h * new_h, right / old_w * new_w, bottom / old_h * new_h), (new_w, new_h))
        shown_old = item.crop_rect()
        shown_new_width = (crop[2] - crop[0]) if crop else new_w
        scale = state.scale * shown_old.width() / shown_new_width
        middle_now = item.mapToScene(shown_old.center())
        if crop:
            left, top, right, bottom = crop
            middle = QPointF((left + right) / 2 - new_w / 2, (top + bottom) / 2 - new_h / 2)
        else:
            middle = QPointF()
        shape = (
            QTransform.fromScale(-1.0 if state.flip_h else 1.0, -1.0 if state.flip_v else 1.0)
            * QTransform().scale(scale, scale) * QTransform().rotate(state.rotation)
        )
        position = middle_now - shape.map(middle)
        before = {
            "source": dict(item.source), "natural": item.natural, "name": item.name, "pictures": item.pictures(),
            "state": state, "original": item.original,
        }
        after = {
            "source": dict(source), "natural": (new_w, new_h), "name": name, "pictures": pictures,
            "state": replace(state, x=position.x(), y=position.y(), scale=scale, crop=crop), "original": original,
        }
        return before, after

    # ------------------ upscaling (ui.artbook_upscale) ------------------
    def upscale_candidates(self) -> list[ImageItem]:
        """The selected images that may be upscaled (not in a locked group)."""
        return [item for item in self._images_in_order() if not self.is_locked(item)] if self.page_id else []

    def upscale_selection(self, cutout: bool = False) -> None:
        """Open the Upscale window for the selected images (cutout: the Remove Background window)."""
        from ui.artbook_upscale import CutoutDialog, UpscaleDialog  # (they use this module's picture loading)

        items = self.upscale_candidates()
        if not items:
            return
        dialog = (CutoutDialog if cutout else UpscaleDialog)(self, items)
        dialog.finished.connect(dialog.deleteLater)
        dialog.open()
        self.upscale_dialog = dialog

    def apply_upscales(self, results: dict[str, dict[str, Any]], what: str = "upscale") -> int:
        """Show upscaled pictures ({item id: {"source", "size", "base" (a small QImage)}}): one undo
        step. Each image remembers the picture it had (Revert to Original). Returns how many changed."""
        items = [(self._items.get(item_id), result) for item_id, result in results.items()]
        items = [(item, result) for item, result in items if isinstance(item, ImageItem)]
        if not items:
            return 0
        count = len(items)
        swaps = []
        for item, result in items:
            original = item.original or {"source": dict(item.source), "size": list(item.natural), "name": item.name}
            pictures = (QPixmap.fromImage(result["base"]), True, None)
            swaps.append((item, *self._picture_swap(item, result["source"], result["size"], item.display_name(), pictures, original)))
        self.undo_stack.push(_ReplaceManyCommand(swaps, f"{what} {count} image{'s' if count != 1 else ''}"))
        self._schedule_detail()  # (the sharp copies load as usual)
        self.viewport().update()
        return count

    def revert_upscales(self) -> None:
        """The selected upscaled images back to their original pictures (one undo step)."""
        items = [item for item in self._images_in_order() if item.original and not self.is_locked(item)]
        if not items:
            return
        swaps = []
        for item in items:
            original = item.original
            pictures = (item.base, False, None)  # (shows meanwhile: the same picture; the original's is read again)
            swaps.append((item, *self._picture_swap(item, original["source"], tuple(original["size"]), original["name"], pictures, None)))
        self.undo_stack.push(_ReplaceManyCommand(swaps, "revert to original"))
        self._schedule_detail()
        self.viewport().update()

    def missing_images(self) -> list[ImageItem]:
        return [item for item in self._items.values() if isinstance(item, ImageItem) and item.file_missing()]

    def relink_missing(self, folder: Optional[Path] = None) -> None:
        """Find the images whose files moved: look for files with the same names in a folder
        (and the folders in it). One undo step; the ones not found stay as they are."""
        missing = self.missing_images()
        if not missing:
            self.show_message("No images are missing their files.")
            return
        if folder is None:
            chosen = QFileDialog.getExistingDirectory(self, "Relink Missing Images: where are the files now?")
            if not chosen:
                return
            folder = Path(chosen)
        names = {item.linked_path().name for item in missing}
        generation = self._generation
        self.show_message(f"Looking for {len(missing)} missing file{'s' if len(missing) != 1 else ''} in {folder.name or folder}...")
        run_job(
            lambda: find_files(folder, names),
            lambda found: self._on_relinked(found, generation),
            self.show_message,
        )

    def _on_relinked(self, found: dict[str, str], generation: int) -> None:
        if generation != self._generation:
            return
        missing = self.missing_images()
        changes = {}
        for item in missing:
            path = found.get(item.linked_path().name.lower())
            if path:
                changes[item.item_id] = (dict(item.source), {**item.source, "path": path})
        if changes:
            self.undo_stack.push(_SourcesCommand(self, changes))
        left = len(missing) - len(changes)
        message = f"Relinked {len(changes)} image{'s' if len(changes) != 1 else ''}."
        if left:
            message += f" {left} still missing (no file with {'its' if left == 1 else 'their'} name there)."
        self.show_message(message)

    def set_sources(self, sources: dict[str, dict[str, Any]]) -> None:
        """Point images at other files (or back), and read their pictures again."""
        for item_id, source in sources.items():
            item = self._items.get(item_id)
            if item is None:
                continue
            item.source = dict(source)
            item.picture_changed()
        self._schedule_detail()
        self.files_changed.emit()

    # ------------------ a group's colour and lock (and its bar) ------------------
    def selected_groups(self) -> list[str]:
        return sorted({item.group for item in self.selected() if item.group and len(self.group_members(item.group)) >= 2})

    def group_fill(self, group: str) -> Optional[str]:
        return self._groups.get(group, {}).get("fill")

    def group_locked(self, group: Optional[str]) -> bool:
        return bool(group) and self._groups.get(group, {}).get("locked") is True

    def is_locked(self, item: ImageItem) -> bool:
        return self.group_locked(item.group) and len(self.group_members(item.group)) >= 2

    def _unlocked(self, items: list[ImageItem]) -> list[ImageItem]:
        return [item for item in items if not self.is_locked(item)]

    def set_group_metas(self, metas: dict[str, dict[str, Any]]) -> None:
        """Set groups' own settings as they are ({} for none): no undo step (see set_group_fill)."""
        for gid, meta in metas.items():
            if meta:
                self._groups[gid] = dict(meta)
            else:
                self._groups.pop(gid, None)
        self._apply_locks()
        self._sync_group_bar()
        self.viewport().update()

    def _change_groups(self, groups: list[str], change: Callable[[dict[str, Any]], dict[str, Any]], text: str) -> None:
        before = {gid: dict(self._groups.get(gid, {})) for gid in groups if self.group_members(gid)}
        after = {gid: {k: v for k, v in change(dict(meta)).items() if v not in (None, False)} for gid, meta in before.items()}
        if before and before != after:
            self.undo_stack.push(_GroupMetaCommand(self, before, after, text, self._live_merge if text == "group colour" else None))

    def set_group_fill(self, fill: Optional[str], groups: Optional[list[str]] = None) -> None:
        """The selected groups' (or groups') colour, None for the theme's: one undo step (while a
        custom colour is picked live, its changes add up to one)."""
        self._change_groups(self.selected_groups() if groups is None else groups, lambda meta: {**meta, "fill": fill}, "group colour")

    def toggle_group_lock(self) -> None:
        """Lock the selected groups in place (or unlock them, if they're all locked)."""
        groups = self.selected_groups()
        locked = not all(self.group_locked(gid) for gid in groups)
        self._change_groups(groups, lambda meta: {**meta, "locked": locked}, "lock group" if locked else "unlock group")

    def _apply_locks(self) -> None:
        """A locked group's members can't be dragged."""
        counts: dict[str, int] = {}
        for item in self._items.values():
            if item.group:
                counts[item.group] = counts.get(item.group, 0) + 1
        for item in self._items.values():
            if isinstance(item, LineItem):
                continue  # (lines never move by dragging: their ends do)
            locked = self.group_locked(item.group) and counts.get(item.group, 0) >= 2
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not locked)

    @staticmethod
    def default_group_color() -> QColor:
        color = QColor(theme_color("accent"))
        color.setAlphaF(GROUP_DEFAULT_ALPHA)
        return color

    def _group_color_menu(self, enabled: bool) -> list:
        """Group Colour: the theme's, white, black, or Custom... (the same choices as a note's colours)."""
        entries: list = []
        for name, fill in (("Theme Default", None),) + SWATCHES:
            action = QAction(swatch_icon(fill), name, self)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _checked=False, fill=fill: self.set_group_fill(fill))
            entries.append(action)
        entries += [None, ("Custom...", None, self.pick_group_color, enabled)]
        return entries

    def pick_group_color(self) -> None:
        """Custom group colour: the group bar's colour picker (floating beside the bar and group)."""
        self._place_group_bar()
        if self._group_bar is not None and self._group_bar.isVisible():
            self._group_bar.btn_color.open_custom()

    @property
    def group_bar(self) -> GroupBar:
        if self._group_bar is None:
            self._group_bar = GroupBar(self)
        return self._group_bar

    def group_bar_target(self) -> Optional[str]:
        """The group the bar works on: when the selection is exactly one group (and nothing's being typed)."""
        groups = self.selected_groups()
        if len(groups) != 1 or self._editing or self._crop or self._picking:
            return None
        return groups[0] if all(item.group == groups[0] for item in self.selected()) else None

    def group_bar_box(self) -> Optional[QRect]:
        """The bar's group's panel, in view coordinates."""
        gid = self.group_bar_target()
        box = self.group_backdrop_boxes().get(gid) if gid else None
        return self.mapFromScene(box).boundingRect() if box is not None else None

    def _sync_group_bar(self) -> None:
        gid = self.group_bar_target()
        if gid is not None and self._group_bar is not None:
            fill = self.group_fill(gid)
            self._group_bar.sync(QColor(fill) if fill else self.default_group_color(), self.group_locked(gid))

    def _place_group_bar(self) -> None:
        box = self.group_bar_box()
        if box is None:
            if self._group_bar is not None:
                self._group_bar.hide()
                self._group_bar.close_pickers()  # (nothing left for an open colour picker to colour)
            return
        bar = self.group_bar
        bar.resize(bar.sizeHint())
        x = max(4, min(box.center().x() - bar.width() // 2, self.width() - bar.width() - 4))
        y = box.top() - bar.height() - NOTE_BAR_GAP
        if y < 4:
            y = min(box.bottom() + 12, self.height() - bar.height() - 4)  # no room above: below it
        gid = self.group_bar_target()
        if not bar.isVisible() or bar.property("group") != gid:
            bar.setProperty("group", gid)
            self._sync_group_bar()
        bar.move(x, y)
        bar.show()
        bar.raise_()

    @property
    def image_bar(self) -> ImageBar:
        if self._image_bar is None:
            self._image_bar = ImageBar(self)
        return self._image_bar

    def image_bar_shown(self) -> bool:
        """The image bar's for the selection now: images are selected, and nothing else is going on (typing,
        cropping, picking a colour, drawing); a single note has its own bar."""
        return bool(self.selected_images()) and not (self._editing or self._crop or self._picking or self._draw_on)

    def _place_image_bar(self) -> None:
        """Under the selection (above it when there's no room below), kept inside the board."""
        if not self.image_bar_shown():
            if self._image_bar is not None:
                self._image_bar.hide()
            return
        box = QRect()
        for item in self.selected():
            box = box.united(self.mapFromScene(item.sceneBoundingRect()).boundingRect())
        bar = self.image_bar
        key = tuple(sorted((item.item_id, item.group or "") for item in self.selected()))
        if not bar.isVisible() or bar.property("selection") != key:
            bar.setProperty("selection", key)
            bar.sync()
        bar.resize(bar.sizeHint())
        x = max(4, min(box.center().x() - bar.width() // 2, self.width() - bar.width() - 4))
        y = box.bottom() + 16  # (below the frame's handles)
        if y + bar.height() > self.height() - 4:
            above = box.top() - bar.height() - NOTE_BAR_GAP
            y = above if above >= 4 else self.height() - bar.height() - 4
        bar.move(x, y)
        bar.show()
        bar.raise_()

    def _group_at(self, scene_point: QPointF) -> Optional[str]:
        """The group whose panel is at a point (the smallest, where panels overlap)."""
        inside = [(box.width() * box.height(), gid) for gid, box in self.group_backdrop_boxes().items() if box.contains(scene_point)]
        return min(inside)[1] if inside else None

    def _units(self, items: list[ImageItem]) -> dict[str, list[ImageItem]]:
        """items in blocks: a group's members together, everything else on its own. If they're all
        one group, its members on their own (arranging a group tidies inside it)."""
        units: dict[str, list[ImageItem]] = {}
        items = [item for item in items if self.column_of(item) is None and not isinstance(item, (LineItem, PointItem))]  # (placed for them)
        for item in items:
            units.setdefault(item.group or item.item_id, []).append(item)
        if len(units) == 1 and len(items) > 1:
            units = {item.item_id: [item] for item in items}
        return units

    @staticmethod
    def _unit_boxes(units: dict[str, list[ImageItem]]) -> list[tuple[str, QRectF]]:
        boxes = []
        for key, members in units.items():
            rect = QRectF()
            for item in members:
                rect = rect.united(item.sceneBoundingRect())
            boxes.append((key, rect))
        return boxes

    def _move_units(self, units: dict[str, list[ImageItem]], moves: dict[str, QPointF], text: str) -> None:
        self._move_by({item.item_id: moves[key] for key, members in units.items() if key in moves for item in members}, text)

    def _image_at(self, pos: QPointF) -> Optional[ImageItem]:
        """The image or note under a point of the view."""
        for item in self.items(pos.toPoint()):
            if isinstance(item, BOARD_ITEMS):
                return item
        return None

    def _snapshot(self, items: list[ImageItem]) -> dict[str, ItemState]:
        self.finish_moves()
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
            self._unlocked(self.selected_images()),  # notes don't flip
        )

    def reset_transform(self) -> None:
        """R: rotation, size and flips back to how the image came in (crop kept)."""
        self._change_selection(
            "reset", lambda item, s: self._centered(item, replace(s, scale=1.0, rotation=0.0, flip_h=False, flip_v=False)),
            self._unlocked(self.selected()),
        )

    def reset_rotation(self) -> None:
        self._change_selection("reset rotation", lambda item, s: self._centered(item, replace(s, rotation=0.0)), self._unlocked(self.selected()))

    def reset_size(self) -> None:
        self._change_selection("reset size", lambda item, s: self._centered(item, replace(s, scale=1.0)), self._unlocked(self.selected()))

    def reset_flips(self) -> None:
        self._change_selection(
            "reset flips", lambda item, s: self._centered(item, replace(s, flip_h=False, flip_v=False)),
            self._unlocked(self.selected_images()),
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
        items = self._unlocked([self._items[item_id] for item_id in moves])  # (a locked group stays put)
        before = self._snapshot(items)
        after = {
            item.item_id: replace(before[item.item_id], x=before[item.item_id].x + moves[item.item_id].x(), y=before[item.item_id].y + moves[item.item_id].y())
            for item in items
        }
        if after != before:
            self.undo_stack.push(_StateCommand(self, before, after, text))  # one undo step (they glide there)

    @staticmethod
    def _boxes(items: list[ImageItem]) -> list[tuple[str, QRectF]]:
        return [(item.item_id, item.sceneBoundingRect()) for item in items]

    def auto_arrange(self) -> None:
        """A: rows of about equal width, in reading order, sizes kept (a group moves as one block)."""
        units = self._units(self.arrange_scope())
        if len(units) >= 2:
            self._move_units(units, artbook_layout.arrange(self._unit_boxes(units)), "arrange")

    def align(self, edge: str) -> None:
        """Line the selection up on an edge: left, center, right, top, middle or bottom."""
        units = self._units(self.selected())
        if len(units) >= 2:
            self._move_units(units, artbook_layout.align(self._unit_boxes(units), edge), "align")

    def distribute(self, horizontal: bool) -> None:
        """Equal gaps between the selected images (three or more)."""
        units = self._units(self.selected())
        if len(units) >= 3:
            self._move_units(units, artbook_layout.distribute(self._unit_boxes(units), horizontal), "distribute")

    def stack(self) -> None:
        """Pile the selection up where the back-most one is, each a little down and right of the last."""
        units = self._units(self.selected())
        if len(units) >= 2:
            boxes = sorted(self._unit_boxes(units), key=lambda box: min(i.zValue() for i in units[box[0]]))
            self._move_units(units, artbook_layout.stack(boxes), "stack")

    # ------------------ caption, pixels ------------------
    def edit_caption(self) -> None:
        """Ask for the selected images' caption (empty: none)."""
        images = self.selected_images()
        if not images:
            return
        text, ok = QInputDialog.getText(self, "Caption", "Caption under the image (empty for none):", text=images[0].caption)
        if ok:
            caption = " ".join(text.split())[:MAX_CAPTION]
            self._change_selection("caption", lambda _item, s: replace(s, caption=caption), images)

    def toggle_pixelated(self) -> None:
        """Hard pixel edges for the selected images (smooth again if they all have them)."""
        images = self.selected_images()
        pixelated = not all(item.pixelated for item in images)
        self._change_selection("pixelated", lambda _item, s: replace(s, pixelated=pixelated), images)

    def same_size(self, height: bool) -> None:
        """Scale the selection to one height (or width): the middle one of their sizes."""
        items = self.selected()
        if len(items) < 2:
            return
        def side(item: ImageItem) -> float:  # what shows (a crop counts)
            rect = item.crop_rect()
            return rect.height() if height else rect.width()

        target = artbook_layout.same_size([(item.item_id, side(item) * item.scale()) for item in items])
        self._change_selection("same size", lambda item, s: replace(s, scale=target / side(item)), self._unlocked(items))

    # ------------------ grayscale, opacity ------------------
    def toggle_gray(self) -> None:
        """G: grayscale on for the selection (off if it's all grayscale already)."""
        items = self.selected_images()
        gray = not all(item.gray for item in items)
        self._change_selection("grayscale", lambda _item, s: replace(s, gray=gray), items)

    def set_board_gray(self, gray: bool) -> None:
        """Shift+G: the whole board in grayscale, to judge values (a view setting: not an undo step)."""
        if gray != self._scene.gray_all:
            self._scene.gray_all = gray
            self.viewport().update()

    def toggle_board_gray(self) -> None:
        self.set_board_gray(not self._scene.gray_all)

    # ------------------ values, guides, mirror ------------------
    def set_values(self, tones: int) -> None:
        """The selected images as `tones` flat greys (0: off). One undo step."""
        tones = _clean_values(tones)
        self._change_selection("values", lambda _item, s: replace(s, values=tones), self.selected_images())

    def set_board_values(self, tones: int) -> None:
        """Values Board: everything as `tones` flat greys (0: off; a view setting, saved with the page)."""
        tones = _clean_values(tones)
        if tones != self._scene.values_all:
            self._scene.values_all = tones
            self.viewport().update()

    def toggle_board_values(self) -> None:
        """Shift+V: Values Board on (with the remembered number of tones) or off."""
        self.set_board_values(0 if self._scene.values_all else get_artbook_value_tones())

    def pick_board_tones(self, tones: int) -> None:
        set_artbook_value_tones(tones)
        self.set_board_values(tones)

    def set_guides(self, guides: str) -> None:
        """Guide lines over the selected images ("": none). One undo step."""
        guides = _clean_guides(guides)
        self._change_selection("guides", lambda _item, s: replace(s, guides=guides), self.selected_images())

    def ask_custom_grid(self) -> None:
        """Custom grid: ask for columns and rows (the last ones asked for to start with)."""
        dialog = CustomGridDialog(get_artbook_custom_grid(), self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                columns, rows = dialog.grid()
                set_artbook_custom_grid((columns, rows))
                self.set_guides(f"grid:{columns}:{rows}")
        finally:
            dialog.deleteLater()

    def set_mirrored(self, mirrored: bool) -> None:
        """Mirror Board: the whole board shown flipped left to right, to check proportions with fresh eyes.
        Only the view: the page, its images and undo stay as they are. The middle stays where it is."""
        if mirrored == self.mirrored:
            return
        center = self.view_center()
        self.mirrored = mirrored
        self.set_view(self.zoom(), center)
        self.show_message("Mirror Board: the board is shown flipped (nothing on it changed). Shift+H turns it back."
                          if mirrored else "Mirror Board off.")
        self.mirrored_changed.emit(mirrored)

    def toggle_mirror(self) -> None:
        self.set_mirrored(not self.mirrored)

    def _values_menu(self, images: list) -> list:
        current = {item.values for item in images}
        entries = [("Off", None, lambda: self.set_values(0), bool(images), bool(images) and current == {0})]
        for tones in VALUE_TONES:
            entries.append((f"{tones} Tones", None, lambda t=tones: self.set_values(t), bool(images), bool(images) and current == {tones}))
        return entries

    def _guides_menu(self, images: list) -> list:
        current = {item.guides for item in images}
        entries = [("Off", None, lambda: self.set_guides(""), bool(images), bool(images) and current == {""})]
        for key, name in GUIDES.items():
            entries.append((name, None, lambda k=key: self.set_guides(k), bool(images), bool(images) and current == {key}))
        shown = next(iter(current)) if len(current) == 1 else ""
        custom = custom_grid(shown) if shown not in GUIDES else None  # (a grid of its own, not one of the above)
        label = "Custom Grid..." if custom is None else f"Custom Grid ({custom[0]}x{custom[1]})..."
        entries += [None, (label, None, self.ask_custom_grid, bool(images), bool(custom))]
        return entries

    def _board_tones_menu(self) -> list:
        chosen = get_artbook_value_tones()
        return [(f"{tones} Tones", None, lambda t=tones: self.pick_board_tones(t), True, self._scene.values_all == tones or
                 (not self._scene.values_all and tones == chosen)) for tones in VALUE_TONES]

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
    # ------------------ lines ------------------
    def lines(self) -> list["LineItem"]:
        return [item for item in self._items.values() if isinstance(item, LineItem)]

    def lines_of(self, items: list) -> list["LineItem"]:
        """The lines attached to any of items, or to a group they're all of (not already among them)."""
        ids = {item.item_id for item in items}
        groups = {item.group for item in items if item.group}
        ids |= {GROUP_REF + gid for gid in groups if all(member in items for member in self.group_members(gid))}
        return [line for line in self.lines() if line not in items and line.attached() & ids]

    def _line_box(self, item) -> Optional[QRectF]:
        """Where a line attached to item ends: round it (a card hidden in a folded column: round the column; a group:
        round its panel)."""
        if isinstance(item, GroupTarget):
            return self.group_backdrop_boxes().get(item.gid)
        if not item.isVisible():
            column = self.column_of(item)
            if column is None or not column.isVisible():
                return None
            item = column
        return item.mapRectToScene(item.crop_rect())

    def _line_end(self, end: tuple) -> Optional[tuple[Optional[QRectF], QPointF]]:
        """(the box it ends round, or None for a free end; the free end's point). None: its item isn't on the page."""
        item_id, x, y = end
        if item_id is None:
            return None, QPointF(x, y)
        if item_id.startswith(GROUP_REF):
            box = self._line_box(GroupTarget(item_id[len(GROUP_REF):]))
            return None if box is None else (box, box.center())
        item = self._items.get(item_id)
        if not isinstance(item, LINKABLE):
            return None
        box = self._line_box(item)
        return None if box is None else (box, box.center())

    def lay_out_lines(self) -> None:
        """Each line's path, between where its ends are now (hidden while an item it joins is off the page). Lines at
        a point on a line meet it along one axis (so a line goes through in one sweep) and stop at its ring."""
        lines = self.lines()
        ends_of = {line: (self._line_end(line.start), self._line_end(line.end)) for line in lines}
        at_points: dict[str, list[tuple[bool, QPointF]]] = {}  # point id: (into it?, the line's other end) for each
        ink: dict[str, Optional[str]] = {}
        for line, ends in ends_of.items():
            if None in ends:
                continue
            for n, end in enumerate((line.start, line.end)):
                if isinstance(self._items.get(end[0]), PointItem):
                    at_points.setdefault(end[0], []).append((n == 1, ends[1 - n][1]))
                    if n == 1 or end[0] not in ink:  # (its colour: the line into it's, else the first's)
                        ink[end[0]] = line.color
        for line, ends in ends_of.items():
            if None in ends:
                if line.isVisible():
                    line.setSelected(False)
                    line.setVisible(False)
                continue
            (box_a, point_a), (box_b, point_b) = ends
            boxes, sides, rings, holes = [box_a, box_b], [None, None], [0.0, 0.0], []
            for n, end in enumerate((line.start, line.end)):
                point = self._items.get(end[0]) if end[0] else None
                if isinstance(point, PointItem):
                    centre = point.scenePos()
                    sides[n] = point_side(centre, ends[1 - n][1], at_points.get(end[0], []))
                    rings[n] = point.radius()
                    boxes[n] = None
                    holes.append((centre, point.radius() + 3 * point.size))
            line.holes = holes
            line.set_geometry(*line_geometry(line.style, line.heads, line.width, line.size, line.bend, boxes[0], point_a,
                                             boxes[1], point_b, (sides[0], sides[1]), (rings[0], rings[1])))
            if not line.isVisible():
                line.setVisible(True)
        for point_id, color in ink.items():
            point = self._items[point_id]
            if point.ink != color:
                point.ink = color
                point.update()

    # ------------------ points on lines ------------------
    def add_line_point(self, line: "LineItem", at: QPointF) -> Optional["PointItem"]:
        """Double-click a line (artbook.line_point): a point there, the line now two lines meeting at it (the arrow
        stays at the far end). Drag it to route them round things; other lines join it as they join a card; Delete
        joins them back into one. One undo step."""
        if self.is_locked(line) or not line.isVisible() or line.path.isEmpty():
            return None
        self.finish_note_edit()
        self.finish_renaming()
        along, spot = nearest_on_path(line.path, at)
        point = PointItem(uuid.uuid4().hex, line.size)
        point.setPos(spot)
        point.setZValue(max((i.zValue() for i in self._items.values()), default=0.0) + 1.0)
        here = (point.item_id, 0.0, 0.0)
        second = LineItem(uuid.uuid4().hex, here, line.end, line.size)
        second.style, second.color, second.width, second.dashed = line.style, line.color, line.width, line.dashed
        second.setZValue(line.zValue())
        second.setOpacity(line.opacity())
        second.heads = "end" if line.heads in ("end", "both") else "none"
        before = {line.item_id: line.state()}
        label = line.label()
        if line.heads == "both":  # (the start's arrow stays: that part runs from the point back to it)
            line.start, line.end, line.heads = here, line.start, "end"
        else:
            line.end, line.heads = here, "none"
        line.bend = QPointF()
        if label and along <= 0.5:  # (the label sits on the middle: on whichever part has it now)
            second.set_label(label)
            line.set_label("")
        after = {line.item_id: line.state()}
        line.apply_state(before[line.item_id])
        self.undo_stack.beginMacro("add point")
        self.undo_stack.push(_AddCommand(self, [point, second], "add point"))
        self.undo_stack.push(_StateCommand(self, before, after, "add point"))
        self.undo_stack.endMacro()
        self.lay_out_lines()
        self._line_spot = None
        self._scene.clearSelection()
        point.setSelected(True)
        return point

    def _rejoin_lines(self, points: list["PointItem"], going: list) -> list["LineItem"]:
        """Deleting points: where a point has two lines, they become one again (the first keeps going, to the other's
        far end; an arrow at either far end stays). Each change pushed; the lines that go with it returned."""
        dropped: list[LineItem] = []
        for point in points:
            here = point.item_id
            lines = [line for line in self.lines() if here in line.attached() and line not in going and line not in dropped]
            if len(lines) != 2:
                continue

            def far(line: "LineItem") -> tuple:
                return line.start if line.end[0] == here else line.end

            def arrow(line: "LineItem") -> bool:  # (an arrow at its far end)
                return line.heads in ("end", "both") if line.start[0] == here else line.heads == "both"

            keep, other = lines
            if far(keep)[0] == here or far(other)[0] == here or (far(keep)[0] and far(keep)[0] == far(other)[0]):
                continue  # (a loop back to the point, or both to one card: nothing to join)
            if arrow(keep) and not arrow(other):
                keep, other = other, keep  # (one arrow: at the end)
            before = {keep.item_id: keep.state()}
            start, end = far(keep), far(other)
            heads = "both" if arrow(keep) and arrow(other) else ("end" if arrow(other) else "none")
            label = keep.label() or other.label()
            keep.start, keep.end, keep.heads, keep.bend = start, end, heads, QPointF()
            keep.set_label(label)
            after = {keep.item_id: keep.state()}
            keep.apply_state(before[keep.item_id])
            self.undo_stack.push(_StateCommand(self, before, after, "delete"))
            dropped.append(other)
        return dropped

    def _hover_line_spot(self, pos: QPointF) -> None:
        """Over a line: a faint point where double-clicking would add one (not over its label)."""
        spot = None
        if self._dots_allowed() and self._dot_card is None:
            item = self._image_at(pos)
            if isinstance(item, LineItem) and not self.is_locked(item) and not item.path.isEmpty():
                at = self.mapToScene(pos.toPoint())
                if not (item.has_pill() and item.pill_rect().contains(at)):
                    spot = (nearest_on_path(item.path, at)[1], POINT_RADIUS * item.size)
        if spot != self._line_spot:
            self._line_spot = spot
            self.viewport().update()

    def _linkable_at(self, pos: QPointF):
        """The card, picture or column under a point of the view (lines and labels skipped)."""
        for item in self.items(pos.toPoint()):
            if isinstance(item, LINKABLE) and item.isVisible():
                return item
        return None

    def _line_target_at(self, pos: QPointF):
        """What a line would join at a point of the view: a card, picture or column, or a group's panel round them."""
        item = self._linkable_at(pos)
        if item is not None:
            return item
        group = self._group_at(self.mapToScene(pos.toPoint()))
        return GroupTarget(group) if group is not None else None

    def _containers(self, target) -> list:
        """What target sits in, innermost first: its column, its group (each can be joined by a line too)."""
        if isinstance(target, GroupTarget) or target is None:
            return []
        column = self.column_of(target)
        if column is not None:
            return [column] + self._containers(column)
        if target.group and target.group in self.group_backdrop_boxes():
            return [GroupTarget(target.group)]
        return []

    def _target_alive(self, target) -> bool:
        if isinstance(target, GroupTarget):
            return target.gid in self.group_backdrop_boxes()
        return target.scene() is self._scene and target.isVisible()

    def _dot_reach(self, target) -> QRectF:
        """Round target, as far as its dots (the mouse heading for one is still on it)."""
        box = self._line_box(target) or QRectF()
        reach = (self._dot_offset(target) + 2 * LINE_DOT_PX) / self.zoom()
        return box.adjusted(-reach, -reach, reach, reach)

    @staticmethod
    def _dot_offset(target) -> float:
        """How far out (on screen) target's dots sit: a column's or group's further than a card's."""
        return LINE_OUTER_DOT_PX if isinstance(target, (ColumnItem, GroupTarget, TableItem)) else LINE_DOT_OFFSET_PX

    def _dot_targets(self) -> list:
        """Whose dots show: the hovered card, then what it sits in (its column, its group)."""
        target = self._dot_card
        if target is None or not self._dots_allowed() or not self._target_alive(target):
            return []
        return [target] + self._containers(target)

    # drawing a line: a hovered card's dots, or the Line tool
    def line_tool_on(self) -> bool:
        return self._line_tool

    def toggle_line_tool(self) -> None:
        """Shift+L: the Line tool. Drag from a card to another to join them (Shift+L again or Esc to stop)."""
        if self._line_tool:
            self.stop_line_tool()
        else:
            self.start_line_tool()

    def start_line_tool(self) -> None:
        self.finish_note_edit()
        self.finish_renaming()
        self.stop_draw()
        self.stop_picker()
        if self._crop:
            self.apply_crop()
        self._line_tool = True
        self._dot_card = None
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self.show_message("Line: drag from a card to another. Esc when done")
        self.rail.sync()

    def stop_line_tool(self) -> None:
        if not self._line_tool:
            return
        self._line_tool = False
        self.viewport().unsetCursor()
        self.rail.sync()

    def _dots_allowed(self) -> bool:
        return not (self._draw_on or self._crop or self._picking or self._line_tool or self._editing or self._renaming
                    or self._drag is not None)

    def _line_dots(self, card) -> list[tuple[str, QPointF]]:
        """A hovered card's (or column's, or group's) dots: the middle of each side, a little way out (not the top
        one while a rotate handle is there)."""
        box = self._line_box(card)
        if box is None:
            return []
        out = self._dot_offset(card) / self.zoom()
        sides = ["left", "right", "top", "bottom"]
        if not isinstance(card, GroupTarget) and card.isSelected() and self._turnable() and self.selected() == [card]:
            sides.remove("top")
        return [(side, side_point(box, side, out)) for side in sides]

    def _hover_dots(self, pos: QPointF) -> None:
        """The card under the mouse shows its dots (and its column's and group's). Heading out for one of them, over
        the column or group it's in, it keeps them: the innermost one whose dots the mouse is near."""
        hovered = None
        if self._dots_allowed():
            under = self._line_target_at(pos)
            point = self.mapToScene(pos.toPoint())
            keep = self._dot_card
            chain = [keep] + self._containers(keep) if keep is not None and self._target_alive(keep) else []
            near = next((target for target in chain if self._dot_reach(target).contains(point)), None)
            hovered = near if near is not None and (under is None or under in chain) else under
        if hovered != self._dot_card:
            self._dot_card = hovered
            self.viewport().update()

    def _dot_at(self, pos: QPointF) -> Optional[tuple[Any, str]]:
        """(the card, column or group, its side) for the dot at a point of the view."""
        for target in self._dot_targets():
            for side, point in self._line_dots(target):
                spot = self.mapFromScene(point)
                if math.hypot(spot.x() - pos.x(), spot.y() - pos.y()) <= LINE_DOT_PX + 3:
                    return target, side
        return None

    def _begin_line(self, source, pos: QPointF) -> None:
        """Start drawing a line from source (a card; None: a free point, with the Line tool)."""
        self.finish_note_edit()
        point = self.mapToScene(pos.toPoint())
        self._drag = {"mode": "line_draw", "source": source, "press": point, "point": point, "target": None}
        self._dot_card = None
        self.viewport().update()

    def _drag_line(self, drag: dict[str, Any], pos: QPointF) -> None:
        drag["point"] = self.mapToScene(pos.toPoint())
        target = self._line_target_at(pos)
        drag["target"] = target if target != drag["source"] else None
        self.viewport().update()

    def _line_pairs(self, drag: dict[str, Any]) -> list[tuple[Any, Any]]:
        """(from, to) for each line the drag makes: one; or with several cards selected, one to each of them (let go
        on one of them), or one from each of them (drawn from one of them)."""
        source, target = drag["source"], drag["target"]
        chosen = [item for item in self.selected() if isinstance(item, LINKABLE)]
        sources, targets = [source], [target]
        if len(chosen) > 1 and target is not None and target in chosen and source not in chosen:
            targets = chosen
        elif len(chosen) > 1 and source is not None and source in chosen and target not in chosen:
            sources = chosen
        return [(a, b) for a in sources for b in targets if a != b or a is None]

    def _finish_line(self, drag: dict[str, Any]) -> list["LineItem"]:
        """Let go: the lines (one undo step), selected; nothing if it was hardly a drag."""
        self.viewport().update()
        press, point = drag["press"], drag["point"]
        if drag["target"] is None and math.hypot(point.x() - press.x(), point.y() - press.y()) * self.zoom() < MIN_LINE_PX:
            return []
        size = min(8.0, max(0.05, 1.0 / self.zoom()))
        z = max((i.zValue() for i in self._items.values()), default=0.0) + 1.0
        lines = []
        for a, b in self._line_pairs(drag):
            start = (a.item_id, 0.0, 0.0) if a is not None else (None, press.x(), press.y())
            end = (b.item_id, 0.0, 0.0) if b is not None else (None, point.x(), point.y())
            line = LineItem(uuid.uuid4().hex, start, end, size)
            line.setZValue(z)
            lines.append(line)
        if lines:
            self.undo_stack.push(_AddCommand(self, lines, "line" if len(lines) == 1 else "lines"))
            self._scene.clearSelection()
            for line in lines:
                line.setSelected(True)
        return lines

    def _draw_line_overlays(self, painter: QPainter) -> None:
        """A hovered card's dots; a line being drawn (to each card it will join, lit up); a selected line's handles."""
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        zoom = self.zoom()
        drag = self._drag
        if drag is not None and drag["mode"] == "line_draw":
            accent = theme_color("accent")
            size = 1.0 / zoom
            for a, b in self._line_pairs(drag):
                box_a = self._line_box(a) if a is not None else None
                box_b = self._line_box(b) if b is not None else None
                path, _mid, tips = line_geometry("curve", "end", 2, size, QPointF(), box_a, drag["press"], box_b, drag["point"])
                paint_line(painter, path, tips, accent, 2, size, "end", False)
                if box_b is not None:
                    pen = QPen(theme_color("accent_border"), 2.0)
                    pen.setCosmetic(True)
                    painter.setPen(pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    gap = 4 / zoom
                    if isinstance(b, PointItem):
                        painter.drawEllipse(box_b.center(), box_b.width() / 2 + gap, box_b.width() / 2 + gap)
                    else:
                        painter.drawRoundedRect(box_b.adjusted(-gap, -gap, gap, gap), CARD_RADIUS * size, CARD_RADIUS * size)
        pen = QPen(theme_color("accent_border"), 1.5)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(theme_color("raised"))
        for target in self._dot_targets():  # (the card's, then its column's and group's)
            for _side, point in self._line_dots(target):
                painter.drawEllipse(point, LINE_DOT_PX / zoom, LINE_DOT_PX / zoom)
        if self._line_spot is not None and drag is None:  # (double-click: a point here)
            spot, radius = self._line_spot
            faint = QColor(theme_color("accent"))
            faint.setAlphaF(0.6)
            pen = QPen(faint, 1.5)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(spot, radius, radius)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(faint)
            painter.drawEllipse(spot, radius * 0.38, radius * 0.38)
        line = self._selected_line()
        if line is not None:
            pen = QPen(theme_color("accent_border"), 1.5)
            pen.setCosmetic(True)
            painter.setPen(pen)
            radius = HANDLE_PX / 2 / zoom
            painter.setBrush(QColor("#ffffff"))
            for tip, _direction in line.tips:
                painter.drawEllipse(tip, radius, radius)
            painter.setBrush(theme_color("accent"))
            painter.drawEllipse(line.handle_point(), radius, radius)
        painter.restore()

    # a selected line: its ends (reconnect them) and its middle (bend it)
    def _selected_line(self) -> Optional["LineItem"]:
        items = self.selected()
        if len(items) == 1 and isinstance(items[0], LineItem) and items[0].isVisible() and not self._renaming:
            return items[0]
        return None

    def _line_handle_at(self, pos: QPointF) -> Optional[str]:
        """"start" / "end" on a selected line's ends, "bend" on its middle; else None."""
        line = self._selected_line()
        if line is None or self.is_locked(line):
            return None
        for which, point in (("bend", line.handle_point()), ("start", line.tips[0][0]), ("end", line.tips[1][0])):
            spot = self.mapFromScene(point)
            if math.hypot(spot.x() - pos.x(), spot.y() - pos.y()) <= HANDLE_PX:
                return which
        return None

    def _begin_line_handle(self, which: str, pos: QPointF) -> None:
        line = self._selected_line()
        self._drag = {"mode": "line_handle", "line": line, "which": which, "before": self._snapshot([line]),
                      "press": self.mapToScene(pos.toPoint()), "bend": QPointF(line.bend)}

    def _drag_line_handle(self, drag: dict[str, Any], pos: QPointF) -> None:
        line, point = drag["line"], self.mapToScene(pos.toPoint())
        if drag["which"] == "bend":
            line.bend = drag["bend"] + (point - drag["press"])
        else:
            target = self._line_target_at(pos)
            other = line.end if drag["which"] == "start" else line.start
            if target is not None and target.item_id != other[0]:
                end = (target.item_id, 0.0, 0.0)  # (joined to what's under the mouse)
            else:
                end = (None, point.x(), point.y())  # (free, where the mouse is)
            if drag["which"] == "start":
                line.start = end
            else:
                line.end = end
        self.lay_out_lines()
        self.viewport().update()

    # the line bar (and the right-click menu): the selected lines' look
    def selected_lines(self) -> list["LineItem"]:
        return [item for item in self.selected() if isinstance(item, LineItem)]

    def _change_lines(self, text: str, change: Callable[["LineItem"], None], merge: Optional[int] = None) -> None:
        lines = [line for line in self.selected_lines() if not self.is_locked(line)]
        if not lines:
            return
        before = self._snapshot(lines)
        for line in lines:
            change(line)
        self.lay_out_lines()
        after = {line.item_id: line.state() for line in lines}
        if after != before:
            self.undo_stack.push(_MergingStateCommand(self, before, after, text, merge) if merge is not None
                                 else _StateCommand(self, before, after, text))
        self._sync_line_bar()

    def set_line_shape(self, style: str) -> None:
        if style in LINE_SHAPES:
            self._change_lines("line shape", lambda line: setattr(line, "style", style))

    def set_line_heads(self, heads: str) -> None:
        if heads in LINE_ENDS:
            self._change_lines("line ends", lambda line: setattr(line, "heads", heads))

    def set_line_color(self, color: Optional[str]) -> None:
        self._change_lines("line colour", lambda line: setattr(line, "color", color), self._live_merge)

    def set_line_width(self, width: int) -> None:
        if width in LINE_WIDTHS:
            self._change_lines("line thickness", lambda line: setattr(line, "width", width))

    def toggle_line_dashed(self) -> None:
        dashed = not all(line.dashed for line in self.selected_lines())
        self._change_lines("dashed line", lambda line: setattr(line, "dashed", dashed))

    def reset_line_bend(self) -> None:
        self._change_lines("straighten line", lambda line: setattr(line, "bend", QPointF()))

    def reverse_lines(self) -> None:
        def reverse(line: "LineItem") -> None:
            line.start, line.end = line.end, line.start

        self._change_lines("reverse line", reverse)

    def _enter_on_selection(self) -> None:
        """Enter (out of crop mode): one line selected: type its label."""
        if self._selected_line() is not None:
            self.edit_line_label()

    def edit_line_label(self, line: Optional["LineItem"] = None) -> None:
        """Double-click a line's label, Enter on a selected line, or the bar's Label: type its label (Enter or Esc when
        done; empty: none)."""
        lines = self.selected_lines()
        line = line if isinstance(line, LineItem) else (lines[0] if len(lines) == 1 else None)
        if line is not None and not self.is_locked(line):
            self._start_renaming(line, line.label_item)
            line.place_label()

    def line_formats(self) -> dict[str, Any]:
        lines = self.selected_lines()
        first = lines[0]
        return {
            "shape": first.style, "heads": first.heads, "width": first.width,
            "color": QColor(first.color) if first.color else theme_color("text_muted"),
            "dashed": all(line.dashed for line in lines), "one": len(lines) == 1,
        }

    @property
    def line_bar(self) -> LineBar:
        if self._line_bar is None:
            self._line_bar = LineBar(self)
        return self._line_bar

    def _sync_line_bar(self) -> None:
        if self._line_bar is not None and self.selected_lines():
            self._line_bar.sync(self.line_formats())

    def _place_line_bar(self) -> None:
        items = self.selected()
        if not items or not all(isinstance(item, LineItem) for item in items) or self._crop or self._picking or self._draw_on:
            if self._line_bar is not None:
                self._line_bar.hide()
                self._line_bar.close_pickers()
            return
        area = QRectF()
        for line in items:
            area = area.united(line.mapRectToScene(line.path.controlPointRect()))
        box = self.mapFromScene(area).boundingRect()
        bar = self.line_bar
        bar.resize(bar.sizeHint())
        x = max(4, min(box.center().x() - bar.width() // 2, self.width() - bar.width() - 4))
        y = box.top() - bar.height() - NOTE_BAR_GAP
        if y < 4:
            y = min(box.bottom() + 12, self.height() - bar.height() - 4)  # no room above: below it
        key = ",".join(sorted(line.item_id for line in items))
        if not bar.isVisible() or bar.property("lines") != key:
            bar.setProperty("lines", key)
            bar.sync(self.line_formats())
        bar.move(x, y)
        bar.show()
        bar.raise_()

    # ------------------ columns ------------------
    def columns(self) -> list["ColumnItem"]:
        return [item for item in self._items.values() if isinstance(item, ColumnItem)]

    def column_of(self, item) -> Optional["ColumnItem"]:
        """The column a card is in (None: it's loose on the board)."""
        for column in self.columns():
            if item.item_id in column.members:
                return column
        return None

    def column_cards(self, column: "ColumnItem") -> list:
        """A column's cards on the page, top to bottom."""
        return [self._items[m] for m in column.members if isinstance(self._items.get(m), COLUMN_KINDS)]

    def column_at(self, scene_point: QPointF) -> Optional["ColumnItem"]:
        """The (top-most) column whose outline is around a point of the board."""
        for column in sorted(self.columns(), key=lambda c: -c.zValue()):
            if column.isVisible() and column.crop_rect().contains(column.mapFromScene(scene_point)):
                return column
        return None

    def _column_snapshot(self, columns: list["ColumnItem"]) -> dict[str, ItemState]:
        """The columns' states and their cards' (for one undo step)."""
        items = list(columns)
        for column in columns:
            items += self.column_cards(column)
        return self._snapshot(items)

    def _clean_columns(self) -> None:
        """A card is in one column at most (the first that lists it); columns don't go in columns."""
        seen: set[str] = set()
        for column in self.columns():
            kept = []
            for member in column.members:
                if member not in seen and not isinstance(self._items.get(member), (ColumnItem, StrokeItem)):
                    seen.add(member)
                    kept.append(member)
            column.members = kept

    def lay_out_columns(self) -> None:
        """Columns fit their cards, then lines follow what they join (called after each change: cheap)."""
        self._place_columns()
        self.lay_out_lines()

    def _place_columns(self) -> None:
        """Put each column's cards in place: stacked down it at its width (a picture as wide as a note), hidden
        while it's folded; the column grows or shrinks to fit them. Cheap to call: it's called after each change."""
        columns = self.columns()
        if not columns and not self._folded_away:
            return
        hidden: set[str] = set()
        placed: set[str] = set()
        for column in sorted(columns, key=lambda c: c.zValue()):
            cards = [card for card in self.column_cards(column) if card.item_id not in placed]
            placed.update(card.item_id for card in cards)
            pictures = sum(1 for card in cards if isinstance(card, ImageItem))
            column.set_counts(len(cards) - pictures, pictures)
            top = column.header_height()
            if column.folded:
                hidden.update(card.item_id for card in cards)
                column.set_height(top)
                continue
            scale = column.scale() or 1.0
            y = top
            for n, card in enumerate(cards):
                self._fit_card(card, column.inner_width(), scale)
                box = card.mapRectToScene(card.boundingRect())
                shift = column.mapToScene(QPointF(COLUMN_PAD, y)) - box.topLeft()
                if abs(shift.x()) > 1e-6 or abs(shift.y()) > 1e-6:
                    card.setPos(card.pos() + shift)
                z = column.zValue() + (n + 1) * COLUMN_Z_STEP
                if card.zValue() != z:
                    card.setZValue(z)
                y += box.height() / scale + COLUMN_GAP
            column.set_height(y - COLUMN_GAP + COLUMN_PAD if cards else top + COLUMN_EMPTY)
        for item in self._items.values():
            if isinstance(item, COLUMN_KINDS):
                shown = item.item_id not in hidden
                if item.isVisible() != shown:
                    if not shown:
                        item.setSelected(False)
                    item.setVisible(shown)
        self._folded_away = hidden

    @staticmethod
    def _fit_card(card, width: float, scale: float) -> None:
        """A card as wide as a column's inside (width, in the column's units, at its scale), upright."""
        if card.rotation():
            card.setRotation(0.0)
        if isinstance(card, (NoteItem, TableItem, RefCardItem)):
            if card.textWidth() != width or isinstance(card, TableItem):  # (a table: its columns may want rebalancing)
                card.setTextWidth(width)
            if card.scale() != scale:
                card.setScale(scale)
            return
        shown = card.crop_rect().width()
        if shown > 0 and abs(card.scale() - width * scale / shown) > 1e-9:
            card.setScale(width * scale / shown)

    def _on_text_changed(self) -> None:
        """A note's text (a column's title, a line's label) changed: its column fits it, lines follow."""
        self.lay_out_columns()

    def _insert_cards(self, column: "ColumnItem", cards: list, index: int) -> None:
        """Put cards in a column at index (counted among its other cards), out of any column they were in.
        A card in a column leaves its group."""
        ids = [card.item_id for card in cards]
        for other in self.columns():
            other.members = [m for m in other.members if m not in ids]
        stay = self.column_cards(column)
        at = column.members.index(stay[index].item_id) if index < len(stay) else len(column.members)
        column.members[at:at] = ids
        for card in cards:
            card.group = None

    def _drop_index(self, column: "ColumnItem", scene_point: QPointF, moving: list) -> int:
        """Where a card let go at scene_point would go among the column's other cards."""
        stay = [card for card in self.column_cards(column) if card not in moving]
        return next((n for n, card in enumerate(stay) if scene_point.y() < card.sceneBoundingRect().center().y()), len(stay))

    def _join_column_at(self, card, at: Optional[QPointF]) -> Optional[tuple["ColumnItem", ItemState]]:
        """A card made at `at`, over a column: it goes in there, among its cards. (the column, its state before)."""
        column = self.column_at(at) if at is not None else None
        if column is None or column.folded or self.is_locked(column):
            return None
        before = column.state()
        self._insert_cards(column, [card], self._drop_index(column, at, [card]))
        self.lay_out_columns()
        return column, before

    def _track_column_drop(self, pos: QPointF) -> None:
        """While dragging: cards over a column show where they'd go (a line); a column dragged takes its cards along."""
        selected = self.selected()
        if any(isinstance(item, ColumnItem) for item in selected):
            self.lay_out_columns()
            return
        target = None
        if selected and all(isinstance(item, COLUMN_KINDS) for item in selected) and not any(self.is_locked(i) for i in selected):
            point = self.mapToScene(pos.toPoint())
            column = self.column_at(point)
            if column is not None and not column.folded and not self.is_locked(column):
                target = (column, self._drop_index(column, point, selected))
        self._show_column_drop(target, selected)

    def _show_column_drop(self, target: Optional[tuple["ColumnItem", int]], moving: list) -> None:
        self._column_drop = target
        for item in self._items.values():
            lifted = target is not None and item in moving
            if getattr(item, "lifted", False) != lifted:
                item.lifted = lifted
                item.update()
        for column in self.columns():
            hot = target is not None and column is target[0]
            line = None
            if hot:
                stay = [card for card in self.column_cards(column) if card not in moving]
                index = target[1]
                if not stay:
                    line = column.header_height() + COLUMN_EMPTY / 2
                elif index < len(stay):
                    line = column.mapFromScene(stay[index].sceneBoundingRect().topLeft()).y() - COLUMN_GAP / 2
                else:
                    line = column.mapFromScene(stay[-1].sceneBoundingRect().bottomLeft()).y() + COLUMN_GAP / 2
            if (column.hot, column.drop_y) != (hot, line):
                column.hot, column.drop_y = hot, line
                column.update()
                self.viewport().update()

    def _finish_card_move(self, drag: dict[str, Any]) -> bool:
        """Cards let go: into the column under the pointer (where its line showed), or out of the column they were
        in (staying where they were let go). One undo step. False: neither (a plain move)."""
        target, self._column_drop = self._column_drop, None
        cards = [item for item in self.selected() if isinstance(item, COLUMN_KINDS)]
        if not cards or len(cards) != len(self.selected()):
            return False
        if all(card.state() == drag["before"].get(card.item_id) for card in cards):
            return False  # (a click, not a drag)
        sources = []
        for card in cards:
            column = self.column_of(card)
            if column is not None and column not in sources:
                sources.append(column)
        if target is None and not sources:
            return False
        columns = sources + ([target[0]] if target is not None and target[0] not in sources else [])
        before = self._column_snapshot(columns)
        before.update(drag["before"])  # (the cards as they were before the drag)
        cards.sort(key=lambda card: card.sceneBoundingRect().top())
        if target is not None:
            self._insert_cards(target[0], cards, target[1])
        else:
            ids = {card.item_id for card in cards}
            for column in sources:
                column.members = [m for m in column.members if m not in ids]
        self.lay_out_columns()
        self._push_states(before, "move into column" if target is not None else "move out of column")
        return True

    def _hover_columns(self, pos: QPointF) -> None:
        """The column under the mouse shows its x (top-right corner)."""
        point = self.mapToScene(pos.toPoint())
        hovered, close = None, False
        for column in sorted(self.columns(), key=lambda c: -c.zValue()):
            local = column.mapFromScene(point)
            on_close = column.hovered and column.close_rect().contains(local)
            if column.isVisible() and (column.crop_rect().contains(local) or on_close):
                hovered, close = column, on_close
                break
        for column in self.columns():
            mine = column is hovered and not self.is_locked(column)
            column.set_hovered(mine, mine and close)

    def leaveEvent(self, event) -> None:
        if self._comment_hover is not None:
            self._comment_hover = None
            self.viewport().update()
        for column in self.columns():
            column.set_hovered(False)
        for table in self.tables():
            table.set_hover(False)
        for card in self.cards():
            card.set_hover(False)
        super().leaveEvent(event)

    def _column_control_at(self, pos: QPointF) -> Optional[tuple["ColumnItem", str]]:
        """(a column, "close") on a hovered column's x, (a column, "fold") on its fold arrow; else None."""
        point = self.mapToScene(pos.toPoint())
        for column in sorted(self.columns(), key=lambda c: -c.zValue()):
            if not column.isVisible():
                continue
            local = column.mapFromScene(point)
            if column.hovered and column.close_rect().contains(local) and not self.is_locked(column):
                return column, "close"
            if column.chevron_rect().contains(local) and self._image_at(pos) is column:
                return column, "fold"
        return None

    def _inline_text_at(self, pos: QPointF):
        """The item whose title or label is under a point of the view (None: none)."""
        text = self._inline_text_item_at(pos)
        return text.parentItem() if text is not None else None

    def _inline_text_item_at(self, pos: QPointF) -> Optional[InlineText]:
        """The title, label or cell being typed in under a point of the view (None: none)."""
        for item in self.items(pos.toPoint()):
            if isinstance(item, InlineText):
                return item
        return None

    def add_column(self, at: Optional[QPointF] = None) -> "ColumnItem":
        """Shift+C: a new column at the cursor (or `at`, or the middle of the view), its title ready to type."""
        self.finish_note_edit()
        self.finish_renaming()
        if at is None:
            under = self.viewport().mapFromGlobal(QCursor.pos())
            at = self.mapToScene(under) if self.viewport().rect().contains(under) else self.view_center()
        column = ColumnItem(uuid.uuid4().hex, "Untitled")
        column.setScale(min(8.0, max(0.05, 1.0 / self.zoom())))  # reads the same size on screen at any zoom
        column.setPos(at - QPointF(column.width, column.header_height() + COLUMN_EMPTY) * column.scale() / 2)
        column.setZValue(max((i.zValue() for i in self._items.values()), default=0.0) + 1.0)
        self.undo_stack.push(_AddCommand(self, [column], "column"))
        self._scene.clearSelection()
        column.setSelected(True)
        self.edit_column_title(column)
        return column

    def toggle_fold(self, column: "ColumnItem") -> None:
        """Click a column's arrow: fold it to its title and count, or open it again (one undo step)."""
        self.finish_renaming()
        before = self._column_snapshot([column])
        column.folded = not column.folded
        self.lay_out_columns()
        self._push_states(before, "fold column" if column.folded else "open column")

    def edit_column_title(self, column: "ColumnItem") -> None:
        """Double-click a column's title: type a new one (Enter or Esc when done)."""
        self._start_renaming(column, column.title_item)

    # ------------------ the page header ------------------
    def title_note(self) -> Optional[NoteItem]:
        """The page's header (a note in the "title" style), if it has one."""
        return next((item for item in self._items.values() if isinstance(item, NoteItem) and item.style == "title"), None)

    def page_counts(self) -> tuple[int, int, int, int]:
        """(images, cards, to-do lines done, to-do lines) on the page (store.content_counts, from what's here)."""
        images = cards = done = total = 0
        for item in self._items.values():
            if isinstance(item, ImageItem):
                images += 1
            elif isinstance(item, NoteItem) and item.style != "title":
                cards += 1
                d, t = item.progress()
                done, total = done + d, total + t
            elif isinstance(item, (TableItem, RefCardItem)):
                cards += 1
        return images, cards, done, total

    def refresh_title(self) -> None:
        """The header's line of what's on the page, and the page's name if undo changed it."""
        title = self.title_note()
        if title is None:
            return
        title.set_stats(store.counts_text(self.page_counts()))
        if not title.editing():
            title.fit_title()
            name = " ".join(title.toPlainText().split())
            if name and name != self.page_name and not self._loading:
                self.page_name = name
                self.page_rename_requested.emit(name)

    def _queue_title(self, _index: int = 0) -> None:
        self._title_timer.start()  # (once things settle: after a whole undo step)

    def _after_change(self) -> None:
        """Something changed (an undo step): the header's counts and name, and what find shows."""
        self.refresh_title()
        if self._find_query:
            self.find(self._find_query, keep=True)

    # ------------------ find on this page ------------------
    @property
    def find_bar(self):
        if self._find_bar is None:
            from ui.artbook_find import FindBar

            self._find_bar = FindBar(self)
        return self._find_bar

    def find_open(self) -> bool:
        return self._find_bar is not None and self._find_bar.isVisible()

    def open_find(self) -> None:
        """Ctrl+F: the find bar (its box ready to type in; what was found last is selected)."""
        self.finish_note_edit()
        self.finish_renaming()
        self.find_bar.open()

    def close_find(self) -> None:
        if self._find_bar is not None:
            self._find_bar.hide()
        self.clear_find()
        self.setFocus()

    def clear_find(self) -> None:
        self._find_query, self._find_matches, self._find_index = "", [], -1
        self.viewport().update()

    def _find_all(self, query: str) -> list[dict[str, Any]]:
        """Every match of query on the page, in reading order (top to bottom, then left to right): each
        {"item", "text" (the text item it's in, or None), "start", "length", "cell" (a table's (row, column)),
        "thread" (a comment's)}."""
        from core.artbook_search import occurrences

        q = query.strip()
        if not q:
            return []
        out: list[dict[str, Any]] = []
        items = sorted((i for i in self._items.values() if i.isVisible()),
                       key=lambda i: (round(i.sceneBoundingRect().top() / 40), i.sceneBoundingRect().left()))

        def in_text(item, text_item) -> None:
            for start in occurrences(text_item.toPlainText(), q):
                out.append({"item": item, "text": text_item, "start": start, "length": len(q), "cell": None, "thread": None})

        def whole(item, texts) -> None:
            for text in texts:
                for _start in occurrences(text, q):
                    out.append({"item": item, "text": None, "start": 0, "length": 0, "cell": None, "thread": None})

        for item in items:
            if isinstance(item, NoteItem):
                in_text(item, item)
                if item.style == "title" and item.about and item.about_item is not None:
                    in_text(item, item.about_item)
            elif isinstance(item, ColumnItem):
                in_text(item, item.title_item)
            elif isinstance(item, LineItem):
                in_text(item, item.label_item)
            elif isinstance(item, TableItem):
                in_text(item, item.title_item)
                cells = [(-1, c, column[0]) for c, column in enumerate(item.columns) if item.header]
                cells += [(r, c, text) for r, row in enumerate(item.rows) for c, text in enumerate(row)]
                for r, c, text in cells:
                    for start in occurrences(text, q):
                        out.append({"item": item, "text": None, "start": start, "length": len(q), "cell": (r, c), "thread": None})
            elif isinstance(item, RefCardItem):
                whole(item, [item.title(), item.data.get("about", "") if item.kind == "link" else "", item.subtitle() if item.kind == "link" else ""])
            elif isinstance(item, ImageItem):
                whole(item, [item.name or "", item.caption or ""])
        for thread in self._comments:
            target = self._items.get(thread["item"]) if thread.get("item") else None
            if thread.get("item") and target is None:
                continue  # (on something deleted)
            for note in thread["notes"]:
                for _start in occurrences(note["text"], q):
                    out.append({"item": target, "text": None, "start": 0, "length": 0, "cell": None, "thread": thread["id"]})
        return out

    def find(self, query: str, keep: bool = False) -> int:
        """Show what matches query on the page (the rest fades) and go to the first match (keep: stay on the
        current one, as near as there is); returns how many there are."""
        self._find_query = query.strip()
        current = self._find_matches[self._find_index] if keep and 0 <= self._find_index < len(self._find_matches) else None
        self._find_matches = self._find_all(self._find_query)
        if not self._find_matches:
            self._find_index = -1
        elif current is not None:
            same = [n for n, m in enumerate(self._find_matches) if m["item"] is current["item"] and m["cell"] == current["cell"]]
            self._find_index = same[0] if same else min(self._find_index, len(self._find_matches) - 1)
        else:
            self._find_index = 0
            self._show_find_match()
        self.viewport().update()
        return len(self._find_matches)

    def find_step(self, step: int) -> None:
        """Enter / Shift+Enter in the find bar: the next (previous) match, the board moving to it."""
        if self._find_matches:
            self._find_index = (self._find_index + step) % len(self._find_matches)
            self._show_find_match()
            self.viewport().update()

    def find_go_to(self, item_id: Optional[str], thread_id: Optional[str] = None) -> bool:
        """A search result picked: its first match on this page."""
        for n, match in enumerate(self._find_matches):
            if (thread_id and match["thread"] == thread_id) or (not thread_id and match["item"] is not None
                                                                  and match["item"].item_id == item_id and match["thread"] is None):
                self._find_index = n
                self._show_find_match()
                self.viewport().update()
                return True
        return False

    def find_position(self) -> tuple[int, int]:
        """(the current match's number from 1, how many there are)."""
        return self._find_index + 1, len(self._find_matches)

    def _match_rect(self, match: dict[str, Any]) -> QRectF:
        """Where a match is on the board (its words, its table cell, the item, the comment's pin)."""
        if match["text"] is not None:
            rects = self._text_rects(match["text"], match["start"], match["length"])
            if rects:
                united = QRectF()
                for rect in rects:
                    united = united.united(rect)
                return united
        if match["cell"] is not None:
            return match["item"].mapRectToScene(match["item"].cell_rect(*match["cell"]))
        if match["item"] is not None:
            return match["item"].sceneBoundingRect()
        thread = next((t for t in self._comments if t["id"] == match["thread"]), None)
        return QRectF(thread["x"] - 12, thread["y"] - 12, 24, 24) if thread else QRectF()

    def _show_find_match(self) -> None:
        """Pan to the current match if it isn't in view (zooming out only if it's bigger than the view)."""
        if not 0 <= self._find_index < len(self._find_matches):
            return
        match = self._find_matches[self._find_index]
        target = match["item"].sceneBoundingRect() if match["item"] is not None else self._match_rect(match)
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        top = 70 / self.zoom()  # (below the find bar)
        inside = view.adjusted(20 / self.zoom(), top, -20 / self.zoom(), -20 / self.zoom())
        if inside.contains(target):
            return
        zoom = self.zoom()
        if target.width() > inside.width() or target.height() > inside.height():
            zoom = max(0.05, zoom * min(inside.width() / max(1.0, target.width()), inside.height() / max(1.0, target.height())) * 0.9)
        self.animate_view(zoom, target.center() - QPointF(0, top / 2 * self.zoom() / zoom))

    @staticmethod
    def _cell_word_rect(table: "TableItem", cell: tuple[int, int], start: int, length: int) -> Optional[QRectF]:
        """Where a match is in a table cell's text (on the board), if the text is on one line; else None."""
        r, c = cell
        if r < 0 or r >= len(table.rows) or c >= len(table.rows[r]):
            return None
        text = table.rows[r][c]
        metrics = QFontMetricsF(table.cell_font())
        box = table.text_rect(r, c)
        full = metrics.horizontalAdvance(text)
        if "\n" in text or full > box.width():
            return None
        left = table._aligned(box, full, c) + metrics.horizontalAdvance(text[:start])
        word = QRectF(left, box.center().y() - metrics.height() / 2, metrics.horizontalAdvance(text[start:start + length]), metrics.height())
        return table.mapRectToScene(word)

    @staticmethod
    def _text_rects(text_item, start: int, length: int) -> list[QRectF]:
        """A run of characters in a text item (a note, a title), line by line, on the board: around its letters."""
        out = []
        document = text_item.document()
        end = start + length
        block = document.findBlock(start)
        while block.isValid() and block.position() < end:
            layout = block.layout()
            origin = document.documentLayout().blockBoundingRect(block).topLeft()
            a, b = max(start, block.position()) - block.position(), min(end, block.position() + block.length() - 1) - block.position()
            for i in range(layout.lineCount()):
                line = layout.lineAt(i)
                lo, hi = max(a, line.textStart()), min(b, line.textStart() + line.textLength())
                if lo >= hi:
                    continue
                x1, x2 = line.cursorToX(lo)[0], line.cursorToX(hi)[0]
                glyphs = line.ascent() + line.descent()
                rect = QRectF(origin.x() + min(x1, x2), origin.y() + line.y() + line.height() - glyphs, abs(x2 - x1), glyphs)
                out.append(text_item.mapRectToScene(rect))
            block = block.next()
        return out

    def _draw_find(self, painter: QPainter) -> None:
        """Find on this page: everything but what matches fades; the matched words are marked (the current one more),
        the matched things outlined (the current one more)."""
        if not self._find_query:
            return
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        holes = QPainterPath()
        holes.setFillRule(Qt.FillRule.WindingFill)
        targets: list = []
        for match in self._find_matches:
            item = match["item"]
            if item is not None and item not in targets:
                targets.append(item)
        pad = 6 / self.zoom()
        for item in targets:
            holes.addRoundedRect(item.sceneBoundingRect().adjusted(-pad, -pad, pad, pad), 10 / self.zoom(), 10 / self.zoom())
        for match in self._find_matches:
            if match["item"] is None:
                rect = self._match_rect(match).adjusted(-pad, -pad, pad, pad)
                holes.addEllipse(rect)
        veil = QPainterPath()
        veil.addRect(view)
        veil = veil.subtracted(holes)
        fade = QColor(theme_color("grid_bg"))
        fade.setAlphaF(0.62)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fade)
        painter.drawPath(veil)
        current = self._find_matches[self._find_index] if 0 <= self._find_index < len(self._find_matches) else None
        mark = QColor(FIND_MARK)
        for match in self._find_matches:
            strong = match is current
            if match["text"] is not None:
                mark.setAlphaF(0.85 if strong else 0.42)
                painter.setBrush(mark)
                for rect in self._text_rects(match["text"], match["start"], match["length"]):
                    painter.drawRoundedRect(rect.adjusted(-2 / self.zoom(), 0, 2 / self.zoom(), 0), 3 / self.zoom(), 3 / self.zoom())
            elif match["cell"] is not None:
                word = self._cell_word_rect(match["item"], match["cell"], match["start"], match["length"])
                if word is not None:
                    mark.setAlphaF(0.85 if strong else 0.42)
                    painter.setBrush(mark)
                    painter.drawRoundedRect(word.adjusted(-2 / self.zoom(), 0, 2 / self.zoom(), 0), 3 / self.zoom(), 3 / self.zoom())
                else:
                    mark.setAlphaF(0.5 if strong else 0.24)
                    painter.setBrush(mark)
                    painter.drawRoundedRect(self._match_rect(match).adjusted(2 / self.zoom(), 2 / self.zoom(), -2 / self.zoom(), -2 / self.zoom()),
                                            4 / self.zoom(), 4 / self.zoom())
        accent = theme_color("accent")
        for item in targets:
            hot = current is not None and current["item"] is item
            color = QColor(accent)
            color.setAlphaF(1.0 if hot else 0.5)
            pen = QPen(color, 2.4 if hot else 1.3)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(item.sceneBoundingRect().adjusted(-pad, -pad, pad, pad), 10 / self.zoom(), 10 / self.zoom())
        if current is not None and current["item"] is None:  # (a comment's pin)
            pen = QPen(accent, 2.4)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(self._match_rect(current).adjusted(-pad, -pad, pad, pad))

    def make_page_title(self, at: Optional[QPointF] = None) -> NoteItem:
        """A header for the page (not on it yet): its name, above everything on it (or at `at`)."""
        note = NoteItem(uuid.uuid4().hex)
        note.set_content(("", None, "title"))
        note.set_title_text(self.page_name or "Untitled")
        note.set_stats(store.counts_text(self.page_counts()))
        note.fit_title()
        if at is None:
            items = [item for item in self._items.values() if item.isVisible()]
            if items:
                bounds = QRectF()
                for item in items:
                    bounds = bounds.united(item.sceneBoundingRect())
                scale = max(0.05, min(8.0, 1.0 / self.zoom()))
                note.setScale(scale)
                note.setPos(bounds.left(), bounds.top() - note.boundingRect().height() * scale - 24 * scale)
            else:
                note.setScale(max(0.05, min(8.0, 1.0 / self.zoom())))
                center = self.view_center()
                note.setPos(center - QPointF(note.boundingRect().width(), note.boundingRect().height()) * note.scale() / 2)
        else:
            note.setScale(max(0.05, min(8.0, 1.0 / self.zoom())))
            note.setPos(at - QPointF(0, note.boundingRect().height() * note.scale() / 2))
        note.setZValue(max((i.zValue() for i in self._items.values()), default=0.0) + 1.0)
        return note

    def add_page_title(self, at: Optional[QPointF] = None, record: bool = True) -> NoteItem:
        """Add Here > Page Title: the page's header (its name, a line about it, what's on it). There's one per page:
        if it has one, that's shown. record: one undo step (a new page's header isn't one)."""
        title = self.title_note()
        if title is not None:
            self._scene.clearSelection()
            title.setSelected(True)
            self.frame([title])
            return title
        self.finish_note_edit()
        self.finish_renaming()
        note = self.make_page_title(at)
        if record:
            self.undo_stack.push(_AddCommand(self, [note], "page title"))
        else:
            self.attach(note)
            self.changed.emit()
        self._scene.clearSelection()
        note.setSelected(True)
        return note

    def set_page_name(self, name: str) -> None:
        """The page was renamed (in Pages): its header follows (not an undo step)."""
        self.page_name = name
        title = self.title_note()
        if title is not None and title.toPlainText() != name and not title.editing():
            title.set_title_text(name)
            title.fit_title()
            self.changed.emit()

    def edit_page_about(self, note: NoteItem) -> None:
        """Double-click under the page header's name: type the line about the page (Enter or Esc when done)."""
        about = note._about_text()
        self._start_renaming(note, about)
        note.lay_out()

    def _start_renaming(self, item, text: "InlineText") -> None:
        """Type in place: a column's title or a line's label (Enter or Esc when done; finish_renaming keeps it)."""
        self.finish_note_edit()
        self.finish_renaming()
        before = self._column_snapshot([item]) if isinstance(item, ColumnItem) else self._snapshot([item])
        self._renaming = {"item": item, "text": text, "before": before}
        self.setFocus()
        text.start_editing()
        item.update()
        self.viewport().update()

    def finish_renaming(self) -> None:
        """Keep what was typed (one undo step): a column's empty title is "Untitled"; a line's empty label, none."""
        renaming, self._renaming = self._renaming, None
        if renaming is None:
            return
        item, text = renaming["item"], renaming["text"]
        if isinstance(item, TableItem):
            self._finish_table_typing(renaming)
            return
        text.stop_editing()
        typed = " ".join(text.toPlainText().split())
        if isinstance(item, NoteItem):  # (the page header's line)
            item.set_about(typed)
            item.fit_title()
            if item.item_id in self._items:
                self._push_states(renaming["before"], "page line")
            self.viewport().update()
            return
        if isinstance(item, ColumnItem):
            typed, what = typed[:MAX_COLUMN_TITLE] or "Untitled", "rename column"
        else:
            typed, what = typed[:MAX_LINE_LABEL], "label line"
        if typed != text.toPlainText():
            text.setPlainText(typed)
        if isinstance(item, ColumnItem):
            item.lay_out_header()
        else:
            item.place_label()
        self.lay_out_columns()
        if item.item_id in self._items:
            self._push_states(renaming["before"], what)
        item.update()
        self.viewport().update()

    def renaming_column(self) -> Optional["ColumnItem"]:
        item = self._renaming["item"] if self._renaming else None
        return item if isinstance(item, ColumnItem) else None

    def delete_column(self, column: "ColumnItem", keep_cards: bool) -> None:
        """Delete a column with its cards, or keep them: they stay on the board where they are (one undo step)."""
        if self.renaming_column() is column:
            self.finish_renaming()
        if self.is_locked(column):
            self.show_message("A locked group can't be deleted: unlock it first (the lock above it).")
            return
        items = [column] + ([] if keep_cards else self.column_cards(column))
        items += self.lines_of(items)
        self.undo_stack.push(_RemoveCommand(self, items, "delete column" if keep_cards else "delete column and cards"))

    def column_entries(self, column: "ColumnItem") -> list:
        """The right-click menu's Column."""
        return [
            ("Rename", None, lambda: self.edit_column_title(column), not self.is_locked(column)),
            ("Open" if column.folded else "Fold", None, lambda: self.toggle_fold(column), True),
            None,
            *self.column_delete_entries(column),
        ]

    def column_delete_entries(self, column: "ColumnItem") -> list:
        """What the x on a column offers."""
        unlocked = not self.is_locked(column)
        return [
            ("Delete Column and Its Cards", None, lambda: self.delete_column(column, keep_cards=False), unlocked),
            ("Delete Column, Keep the Cards", None, lambda: self.delete_column(column, keep_cards=True), unlocked),
        ]

    def column_delete_menu(self, column: "ColumnItem") -> QMenu:
        menu = QMenu(self)
        self._fill_menu(menu, self.column_delete_entries(column))
        return menu

    def ask_delete_column(self, column: "ColumnItem", at: QPoint) -> None:
        """The x on a column: delete it with its cards, or keep them."""
        menu = self.column_delete_menu(column)
        menu.exec(at)
        menu.deleteLater()

    # ------------------ the page's Trash ------------------
    def page_trash(self) -> list[dict[str, Any]]:
        """The Trash as it's saved (TRASH_DAYS at most)."""
        return [dict(entry) for entry in clean_trash(self._trash)]

    def trash_add(self, entry_id: str, items: list) -> None:
        """Items going off the page (a deletion): kept in the Trash, as they were."""
        if any(entry["id"] == entry_id for entry in self._trash):
            return
        dicts = [item.to_dict() for item in items]
        groups = {item.group: dict(self._groups[item.group]) for item in items if item.group and item.group in self._groups}
        self._trash.append({"id": entry_id, "time": time.time(), "items": dicts, "groups": groups})
        del self._trash[:-MAX_TRASH]
        self.trash_changed.emit()

    def trash_put(self, entry: dict[str, Any]) -> None:
        if not any(e["id"] == entry["id"] for e in self._trash):
            self._trash.append(entry)
            self._trash.sort(key=lambda e: e["time"])
            self.trash_changed.emit()

    def trash_discard(self, entry_id: str) -> None:
        before = len(self._trash)
        self._trash = [entry for entry in self._trash if entry["id"] != entry_id]
        if len(self._trash) != before:
            self.trash_changed.emit()

    def trash_entries(self) -> list[dict[str, Any]]:
        """The Trash, newest first: {"id", "time", "label", "kinds", "items"} (for the tray)."""
        out = []
        for entry in reversed(self._trash):
            items = [d for d in entry["items"] if d.get("kind") != "line"] or entry["items"]
            label = trash_label(items[0])
            if len(items) > 1:
                label += f" and {len(items) - 1} more"
            out.append({"id": entry["id"], "time": entry["time"], "label": label, "items": entry["items"]})
        return out

    def restore_trash(self, entry_id: str) -> int:
        """Put a deletion's items back where they were (one undo step); how many came back."""
        entry = next((e for e in self._trash if e["id"] == entry_id), None)
        if entry is None:
            return 0
        items = []
        for data in entry["items"]:
            if data.get("id") in self._items:
                continue  # (it's on the page already)
            try:
                item = item_from_dict(data)
            except Exception:  # pylint: disable=broad-except
                item = None
            if item is not None:
                items.append(item)
        if not items:
            self.trash_discard(entry_id)
            return 0
        self.undo_stack.push(_RestoreCommand(self, entry, items))
        self._scene.clearSelection()
        for item in items:
            if not isinstance(item, LineItem):
                item.setSelected(True)
        return len(items)

    def restore_all_trash(self) -> int:
        entries = [entry["id"] for entry in self._trash]
        if not entries:
            return 0
        self.undo_stack.beginMacro("restore all")
        count = sum(self.restore_trash(entry_id) for entry_id in entries)
        self.undo_stack.endMacro()
        return count

    def empty_trash(self, entry_id: Optional[str] = None) -> None:
        """Delete for good: one deletion's items, or everything in the Trash (Undo can't bring them back)."""
        if entry_id is None:
            self._trash = []
        else:
            self._trash = [entry for entry in self._trash if entry["id"] != entry_id]
        self.trash_changed.emit()
        self.changed.emit()

    def trash_picture(self, data: dict[str, Any]) -> Optional[Path]:
        """A deleted item's picture file (an image's kept copy, a card's picture), for its row in the tray."""
        if not self.page_id:
            return None
        blob = (data.get("source") or {}).get("blob") if data.get("kind", "image") == "image" else (data.get("picture") or data.get("favicon"))
        path = store.blob_path(self.page_id, blob) if isinstance(blob, str) and blob else None
        return path if path is not None and path.is_file() else None

    # ------------------ comments ------------------
    def page_comments(self, saving: bool = False) -> list[dict[str, Any]]:
        """The page's threads (saving: not those on items gone for good, out of the Trash too)."""
        threads = self._comments
        if saving:
            kept = set(self._items) | {d.get("id") for entry in self._trash for d in entry["items"]}
            kept |= {i.item_id for command in range(self.undo_stack.count()) for i in getattr(self.undo_stack.command(command), "items", [])}
            threads = [t for t in threads if t["item"] is None or t["item"] in kept]
        return [dict(thread, notes=[dict(n) for n in thread["notes"]]) for thread in threads]

    def set_comments(self, threads: list[dict[str, Any]]) -> None:
        self._comments = [dict(t, notes=[dict(n) for n in t["notes"]]) for t in threads]
        if self._open_thread is not None:
            current = self._thread(self._open_thread["id"])
            if current is not None:
                self._open_thread = current
                self.comment_thread.show_thread(self._numbered(current))
            elif self._open_thread.get("notes"):
                self.close_comments()  # (undone: it's gone)
        self.viewport().update()

    def _thread(self, thread_id: str) -> Optional[dict[str, Any]]:
        return next((t for t in self._comments if t["id"] == thread_id), None)

    def _numbered(self, thread: dict[str, Any]) -> dict[str, Any]:
        pins = [t["id"] for t in self._comments if t["item"] is None]
        return dict(thread, number=pins.index(thread["id"]) + 1 if thread["id"] in pins else len(pins) + 1)

    def comment_threads(self) -> list[dict[str, Any]]:
        """The threads that show: pins, and those on items on the board (shown, not in a folded column)."""
        return [t for t in self._comments if t["item"] is None or (t["item"] in self._items and self._items[t["item"]].isVisible())]

    def _comment_anchor(self, thread: dict[str, Any]) -> Optional[QPointF]:
        if thread.get("item") is None:
            return QPointF(thread["x"], thread["y"])
        item = self._items.get(thread["item"])
        if item is None or not item.isVisible():
            return None
        return item.mapToScene(item.crop_rect().topRight())

    def _comment_at(self, pos: QPointF):
        """The thread whose badge or pin is at a point of the view, or ("new", item) on a hovered item's faint bubble."""
        reach = COMMENT_BADGE_PX + 2
        for thread in reversed(self.comment_threads()):
            anchor = self._comment_anchor(thread)
            if anchor is not None:
                spot = self.mapFromScene(anchor)
                if math.hypot(spot.x() - pos.x(), spot.y() - pos.y()) <= reach:
                    return thread
        item = self._comment_hover
        if item is not None and item.scene() is self._scene:
            spot = self.mapFromScene(item.mapToScene(item.crop_rect().topRight()))
            if math.hypot(spot.x() - pos.x(), spot.y() - pos.y()) <= reach:
                return ("new", item)
        return None

    def _hover_comments(self, pos: QPointF) -> None:
        """An item under the mouse that has no comments: a faint bubble on its corner (click it to comment)."""
        item = self._image_at(pos)
        busy = self._draw_on or self._crop or self._picking or self._line_tool or self._editing or self._renaming
        if busy or not isinstance(item, BOARD_ITEMS) or isinstance(item, (LineItem, StrokeItem, PointItem)) or self.is_locked(item):
            item = None
        if item is not None and any(t["item"] == item.item_id for t in self._comments):
            item = None
        if item is not self._comment_hover:
            self._comment_hover = item
            self.viewport().update()

    def open_comments(self, target=None, at: Optional[QPointF] = None) -> None:
        """M (or right-click > Comment): the comments on an item (target), or on a pin at `at` (the view's middle).
        A new thread joins the page with its first comment."""
        self.finish_note_edit()
        self.finish_renaming()
        if isinstance(target, dict):
            thread = target
        elif target is not None:
            thread = next((t for t in self._comments if t["item"] == target.item_id), None)
            thread = thread or {"id": uuid.uuid4().hex, "item": target.item_id, "x": 0.0, "y": 0.0, "notes": []}
        else:
            point = at if at is not None else self.view_center()
            thread = {"id": uuid.uuid4().hex, "item": None, "x": point.x(), "y": point.y(), "notes": []}
        self._open_thread = thread
        panel = self.comment_thread
        panel.show_thread(self._numbered(thread))
        self._place_comment_thread()
        panel.show()
        panel.raise_()
        panel.edit.setFocus()
        self.viewport().update()

    def close_comments(self) -> None:
        if self._comment_thread is not None and self._comment_thread.isVisible():
            self._comment_thread.hide()
            self.setFocus()
        self._open_thread = None
        self.viewport().update()

    def comment_on_selection(self) -> None:
        """M: comment on the one selected item, else a pin where the mouse is (or the view's middle)."""
        items = [i for i in self.selected() if not isinstance(i, (LineItem, PointItem))]
        if len(items) == 1:
            self.open_comments(items[0])
            return
        under = self.viewport().mapFromGlobal(QCursor.pos())
        self.open_comments(None, self.mapToScene(under) if self.viewport().rect().contains(under) else None)

    def _change_comments(self, change: Callable[[list], None], text: str) -> None:
        before = self.page_comments()
        after = [dict(t, notes=[dict(n) for n in t["notes"]]) for t in self._comments]
        change(after)
        after = [t for t in after if t["notes"]]  # (a thread with no comments left: gone)
        self.undo_stack.push(_CommentsCommand(self, before, after, text))

    def post_comment(self, thread_id: str, text: str) -> None:
        text = " ".join(text.split())[:2000]
        if not text or self._open_thread is None or self._open_thread["id"] != thread_id:
            return
        note = {"text": text, "time": time.time(), "done": False}
        fresh = self._thread(thread_id) is None
        pending = dict(self._open_thread, notes=list(self._open_thread["notes"]))

        def change(threads: list) -> None:
            thread = next((t for t in threads if t["id"] == thread_id), None)
            if thread is None:
                threads.append(dict(pending, notes=[note]))
            else:
                thread["notes"].append(note)

        if fresh and len(self._comments) >= MAX_COMMENTS:
            self.show_message("This page has as many comments as it can hold.")
            return
        self._change_comments(change, "comment")

    def set_comment_done(self, thread_id: str, index: int, done: bool) -> None:
        def change(threads: list) -> None:
            for thread in threads:
                if thread["id"] == thread_id and 0 <= index < len(thread["notes"]):
                    thread["notes"][index]["done"] = done

        thread = self._thread(thread_id)
        if thread is not None and 0 <= index < len(thread["notes"]) and thread["notes"][index]["done"] != done:
            self._change_comments(change, "comment done" if done else "comment not done")

    def delete_comment(self, thread_id: str, index: int) -> None:
        def change(threads: list) -> None:
            for thread in threads:
                if thread["id"] == thread_id and 0 <= index < len(thread["notes"]):
                    thread["notes"].pop(index)

        if self._thread(thread_id) is not None:
            self._change_comments(change, "delete comment")
            if self._thread(thread_id) is None:
                self.close_comments()

    def move_pin(self, thread_id: str, point: QPointF, before: Optional[QPointF] = None) -> None:
        """A pin dragged somewhere else (one undo step, from before)."""
        thread = self._thread(thread_id)
        if thread is None:
            return
        old = self.page_comments()
        if before is not None:
            for t in old:
                if t["id"] == thread_id:
                    t["x"], t["y"] = before.x(), before.y()
        after = self.page_comments()
        for t in after:
            if t["id"] == thread_id:
                t["x"], t["y"] = point.x(), point.y()
        self.undo_stack.push(_CommentsCommand(self, old, after, "move comment"))

    @property
    def comment_thread(self) -> CommentThread:
        if self._comment_thread is None:
            self._comment_thread = CommentThread(self)
        return self._comment_thread

    def _place_comment_thread(self) -> None:
        panel = self._comment_thread
        if panel is None or not panel.isVisible() or self._open_thread is None:
            return
        anchor = self._comment_anchor(self._open_thread)
        if anchor is None:  # (its item went)
            self.close_comments()
            return
        spot = self.mapFromScene(anchor)
        x = spot.x() + int(COMMENT_BADGE_PX) + 8
        if x + panel.width() > self.width() - 8:
            x = spot.x() - int(COMMENT_BADGE_PX) - 8 - panel.width()
        y = min(max(8, spot.y() - 20), self.height() - panel.height() - 8)
        panel.move(max(8, x), y)

    def _draw_comments(self, painter: QPainter) -> None:
        """Badges on items with comments (how many; faint when all are done), numbered pins, a hovered item's bubble."""
        zoom = self.zoom()
        r = COMMENT_BADGE_PX / zoom
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pins = [t["id"] for t in self._comments if t["item"] is None]
        font = QFont(QApplication.font())
        font.setPointSizeF(8.5 / zoom)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        for thread in self.comment_threads():
            anchor = self._comment_anchor(thread)
            if anchor is None:
                continue
            done = all(n["done"] for n in thread["notes"])
            is_open = self._open_thread is not None and self._open_thread["id"] == thread["id"]
            if thread["item"] is None:
                text = str(pins.index(thread["id"]) + 1)
                painter.setPen(QPen(theme_color("on_accent"), 2 / zoom))
                painter.setBrush(theme_color("text_faint") if done and not is_open else theme_color("accent"))
                painter.drawEllipse(anchor, r, r)
                painter.setPen(theme_color("on_accent"))
            else:
                text = str(len(thread["notes"]))
                self._bubble(painter, anchor, r, filled=is_open, faint=done and not is_open)
                painter.setPen(theme_color("on_accent") if is_open else (theme_color("text_faint") if done else theme_color("accent_text")))
            painter.drawText(QRectF(anchor.x() - r, anchor.y() - r, 2 * r, 2 * r), Qt.AlignmentFlag.AlignCenter, text)
        if self._open_thread is not None and not self._open_thread["notes"]:  # (a new one, not posted yet)
            anchor = self._comment_anchor(self._open_thread)
            if anchor is not None:
                self._bubble(painter, anchor, r, filled=True, faint=False)
        elif self._comment_hover is not None and self._comment_hover.scene() is self._scene and self._comment_hover.isVisible():
            item = self._comment_hover
            anchor = item.mapToScene(item.crop_rect().topRight())
            self._bubble(painter, anchor, r, filled=False, faint=True)
            painter.setPen(QPen(theme_color("text_muted"), 1.6 / zoom, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            arm = r * 0.4
            painter.drawLine(anchor - QPointF(arm, 0), anchor + QPointF(arm, 0))
            painter.drawLine(anchor - QPointF(0, arm), anchor + QPointF(0, arm))
        painter.restore()

    def _bubble(self, painter: QPainter, center: QPointF, r: float, filled: bool, faint: bool) -> None:
        """A speech bubble (a rounded box with a tail at its bottom left)."""
        box = QRectF(center.x() - r * 1.15, center.y() - r * 0.95, r * 2.3, r * 1.9)
        path = QPainterPath()
        path.addRoundedRect(box, r * 0.75, r * 0.75)
        tail = QPainterPath()
        tail.moveTo(box.left() + r * 0.5, box.bottom() - r * 0.2)
        tail.lineTo(box.left() + r * 0.25, box.bottom() + r * 0.55)
        tail.lineTo(box.left() + r * 1.05, box.bottom() - r * 0.2)
        path = path.united(tail)
        edge = QColor(theme_color("accent_border"))
        if faint:
            edge.setAlphaF(0.55)
        painter.setPen(QPen(edge, 1.2 / self.zoom()))
        painter.setBrush(theme_color("accent") if filled else theme_color("raised"))
        painter.drawPath(path)

    # ------------------ link, file and page cards ------------------
    def cards(self) -> list["RefCardItem"]:
        return [item for item in self._items.values() if isinstance(item, RefCardItem)]

    def _place_new_card(self, result: dict[str, Any], batch: dict[str, Any]) -> "RefCardItem":
        """A card made from what was added (a link, a file), in its batch's row, in the look last used for its kind."""
        data = result["card"]
        card = RefCardItem(uuid.uuid4().hex, data["kind"], get_artbook_card_style(data["kind"]), data)
        card.image = result.get("image")
        card.image_blob = data.get("picture")
        card.favicon = result.get("favicon")
        card.favicon_blob = data.get("favicon")
        card.setScale(min(8.0, max(0.05, 1.0 / self.zoom())))  # reads the same size on screen at any zoom
        width, height = card.width * card.scale(), card.height * card.scale()
        if batch["first"]:
            x = batch["x"]
            batch["first"] = False
        else:
            x = batch["x"] + DROP_GAP + width / 2.0
        batch["x"] = x + width / 2.0
        card.setPos(QPointF(x - width / 2, batch["y"] - height / 2))
        card.setZValue(max((i.zValue() for i in self._items.values()), default=0.0) + 1.0)
        self.undo_stack.push(_AddCommand(self, [card], "add " + {"link": "link", "file": "file", "page_card": "page link"}[card.kind]))
        self._scene.clearSelection()
        card.setSelected(True)
        if card.kind == "link":
            self.toast.hide()  # ("Reading the link...": done)
        return card

    def add_page_card(self, page_id: str, at: Optional[QPointF] = None) -> Optional["RefCardItem"]:
        """A card that opens another page (dragged from the Pages list, or Add Here > Page Link)."""
        summary = store.page_summary(page_id) if page_id else None
        if summary is None:
            return None
        if page_id == self.page_id:
            self.show_message("That's this page: a page card opens another one.")
            return None
        self.finish_note_edit()
        self.finish_renaming()
        if at is None:
            at = self.view_center()
        card = RefCardItem(uuid.uuid4().hex, "page_card", get_artbook_card_style("page_card"),
                           {"page": page_id, "name": summary.get("name") or "Page"})
        card.setScale(min(8.0, max(0.05, 1.0 / self.zoom())))
        self._load_card(card)
        card.setPos(at - QPointF(card.width, card.height) * card.scale() / 2)
        card.setZValue(max((i.zValue() for i in self._items.values()), default=0.0) + 1.0)
        self.undo_stack.push(_AddCommand(self, [card], "add page link"))
        self._scene.clearSelection()
        card.setSelected(True)
        return card

    def _load_card(self, card: "RefCardItem") -> None:
        """What a card shows that isn't saved with it: its picture, whether what it stands for is there, a file's
        apps and icon, a page's name, picture and what's on it."""
        if card.kind == "page_card":
            page_id = card.data.get("page", "")
            summary = store.page_summary(page_id) if page_id else None
            card.missing = summary is None
            if summary is not None:
                card.data["name"] = summary.get("name") or card.data.get("name", "Page")
                card.page_info = store.counts_text(summary.get("counts") or (0, 0, 0, 0), "Empty")
                thumb = store.thumbnail_path(page_id)
                card.image = QImage(str(thumb)) if thumb.is_file() else None
        else:
            blob = card.data.get("picture")
            if blob != card.image_blob or (card.image is None and blob):
                path = store.blob_path(self.page_id, blob) if blob and self.page_id else None
                card.image = QImage(str(path)) if path is not None and path.is_file() else None
                card.image_blob = blob
        if card.kind == "link":
            blob = card.data.get("favicon")
            if blob != card.favicon_blob or (card.favicon is None and blob):
                path = store.blob_path(self.page_id, blob) if blob and self.page_id else None
                card.favicon = QImage(str(path)) if path is not None and path.is_file() else None
                card.favicon_blob = blob
        if card.kind == "file":
            path = card.data.get("path", "")
            card.missing = not (path and Path(path).is_file())
            ext = (card.data.get("ext") or Path(path).suffix).lower()
            card.apps = [(app.id, app.name) for app in self._card_apps(ext)]
            card.icon = app_icons.known_icon(ext)
            if card.icon is None and ext and card.image is None:
                run_job(lambda: app_icons.icon_for(ext), lambda icon: self._on_card_icon(card, icon), lambda _error: None)
        card.lay_out()
        card.update()

    def _on_card_icon(self, card: "RefCardItem", icon: Optional[QImage]) -> None:
        if icon is not None and card.scene() is self._scene:
            card.icon = icon
            card.update()

    @staticmethod
    def _card_apps(ext: str) -> list:
        """The apps a file opens in, Maya first (the main app), then the rest in Open In's order."""
        return sorted(apps.apps_for(ext), key=lambda app: app.id != "maya") if ext else []

    def refresh_page_cards(self, page_id: Optional[str] = None) -> None:
        """A page was saved, renamed or deleted: its cards show it as it is now."""
        for card in self.cards():
            if card.kind == "page_card" and (page_id is None or card.data.get("page") == page_id):
                self._load_card(card)

    def open_card(self, card: Optional["RefCardItem"] = None, app_id: Optional[str] = None) -> None:
        """Double-click a card: a link opens in the browser, a file in its app (Maya first; or app_id's), a page
        here (with Back to return)."""
        card = card or self.card_bar_target()
        if card is None:
            return
        if card.kind == "link" and card.data.get("url"):
            QDesktopServices.openUrl(QUrl(card.data["url"]))
        elif card.kind == "page_card":
            self._load_card(card)
            if card.missing:
                self.show_message("That page is gone (deleted, or not on this computer).")
            else:
                self.page_open_requested.emit(card.data["page"])
        elif card.kind == "file":
            self._load_card(card)
            if card.missing:
                self.show_message(f"{card.title()} isn't there any more: Relink... finds it.")
                return
            path = Path(card.data["path"])
            choices = self._card_apps(path.suffix.lower())
            app = next((a for a in choices if a.id == app_id), None) if app_id else (choices[0] if choices else None)
            if app is None:
                if not file_actions.open_file(path):
                    self.show_message(f"Couldn't open {path.name}.")
                return
            try:
                sent = apps.launch(app, path)
            except apps.AppError as exc:
                self.show_message(str(exc))
                return
            self.show_message(f"Sent {path.name} to the {app.name} that's open." if sent else f"Opening {path.name} in {app.name}...")

    def card_bar_target(self) -> Optional["RefCardItem"]:
        items = self.selected()
        return items[0] if len(items) == 1 and isinstance(items[0], RefCardItem) else None

    def set_card_style(self, style: str, cards: Optional[list] = None) -> None:
        """Big or compact, for the selected cards (one undo step); new cards of their kind come that way too."""
        cards = [c for c in (cards if cards is not None else self.selected()) if isinstance(c, RefCardItem) and not self.is_locked(c)]
        if style not in CARD_STYLES or not cards:
            return
        before = self._snapshot(cards)
        for card in cards:
            card.set_style(style)
            set_artbook_card_style(card.kind, style)
        self.lay_out_columns()
        self._push_states(before, "card style")
        self._sync_card_bar()

    def relink_card(self, card: "RefCardItem") -> None:
        """A file card whose file moved: pick it where it is now (its picture is read again)."""
        if self.is_locked(card):
            return
        start = str(Path(card.data.get("path", "")).parent) if card.data.get("path") else ""
        path, _filter = QFileDialog.getOpenFileName(self, f"Relink {card.title()}", start)
        if path:
            self._refresh_file_card(card, Path(path), "relink")

    def refresh_card(self, card: "RefCardItem") -> None:
        """Read a link's page (or a file's picture) again."""
        if card.kind == "link" and card.data.get("url"):
            url, page_id = card.data["url"], self.page_id
            self.show_message("Reading the link...")
            run_job(lambda: _prepare_link(page_id, url), lambda result: self._on_card_refreshed(card, result, "refresh link"),
                    lambda error: self.show_message(error))
        elif card.kind == "file" and card.data.get("path") and Path(card.data["path"]).is_file():
            self._refresh_file_card(card, Path(card.data["path"]), "refresh picture")

    def _refresh_file_card(self, card: "RefCardItem", path: Path, text: str) -> None:
        page_id = self.page_id
        run_job(lambda: _prepare_file_card(page_id, path), lambda result: self._on_card_refreshed(card, result, text),
                lambda error: self.show_message(error))

    def _on_card_refreshed(self, card: "RefCardItem", result: dict[str, Any], text: str) -> None:
        if card.scene() is not self._scene or "card" not in result:
            return
        before = self._snapshot([card])
        data = {k: v for k, v in result["card"].items() if k != "kind"}
        if card.kind == "link" and "favicon" not in data and card.data.get("favicon"):
            data["favicon"] = card.data["favicon"]  # (its site's icon couldn't be read this time: it keeps the one it had)
        card.set_data(data)
        card.image = result.get("image")
        card.image_blob = card.data.get("picture")
        if result.get("favicon") is not None:
            card.favicon, card.favicon_blob = result["favicon"], card.data.get("favicon")
        self._load_card(card)
        self.lay_out_columns()
        self._push_states(before, text)
        self.toast.hide()

    def set_card_picture(self, card: "RefCardItem", image: Optional[QImage]) -> None:
        """A link's (or file's) picture set by hand (None: none), one undo step: for a site that won't show its
        preview to the app."""
        if self.is_locked(card) or card.kind == "page_card":
            return
        before = self._snapshot([card])
        blob = _card_picture_blob(self.page_id, image) if image is not None and not image.isNull() else None
        if blob:
            card.data["picture"] = blob
        else:
            card.data.pop("picture", None)
        self._load_card(card)
        self.lay_out_columns()
        self._push_states(before, "card picture")

    def choose_card_picture(self, card: "RefCardItem") -> None:
        """Set Picture...: a picture file for the card."""
        extensions = " ".join(f"*{ext}" for ext in sorted(store.IMAGE_EXTENSIONS))
        path, _filter = QFileDialog.getOpenFileName(self, "Picture for the Card", get_last_pictures_dir(), f"Images ({extensions})")
        if not path:
            return
        set_last_pictures_dir(str(Path(path).parent))
        image = QImage(path)
        if image.isNull():
            try:
                image, _size = store.decode_bytes(Path(path).read_bytes())
            except Exception:  # pylint: disable=broad-except
                image = QImage()
        if image.isNull():
            self.show_message(f"Couldn't read {Path(path).name} as a picture.")
            return
        self.set_card_picture(card, image)

    @staticmethod
    def clipboard_picture() -> Optional[QImage]:
        """A picture on the clipboard (copied, or a copied picture file), or None."""
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return None
        if mime.hasImage():
            image = QImage(mime.imageData())
            if not image.isNull():
                return image
        for url in mime.urls() if mime.hasUrls() else []:
            if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in store.IMAGE_EXTENSIONS:
                image = QImage(url.toLocalFile())
                if not image.isNull():
                    return image
        return None

    def paste_card_picture(self, card: "RefCardItem") -> None:
        """Paste Picture: the copied picture (or screenshot) is the card's."""
        image = self.clipboard_picture()
        if image is None:
            self.show_message("Copy a picture (or take a screenshot) first, then Paste Picture.")
            return
        self.set_card_picture(card, image)

    def show_card_in_library(self, card: "RefCardItem") -> None:
        if card.data.get("path"):
            self.show_in_library_requested.emit(card.data["path"])

    def card_entries(self, card: "RefCardItem") -> list:
        """The card bar's More (and the right-click menu's Card)."""
        unlocked = not self.is_locked(card)
        style = [
            ("Big", None, lambda: self.set_card_style("big", [card]), unlocked, card.style == "big"),
            ("Compact", None, lambda: self.set_card_style("compact", [card]), unlocked, card.style == "compact"),
        ]
        if card.kind == "link":
            return style + [
                None,
                ("Open in Browser", None, lambda: self.open_card(card), bool(card.data.get("url"))),
                ("Copy Address", None, lambda: QApplication.clipboard().setText(card.data.get("url", "")), True),
                ("Read the Page Again", None, lambda: self.refresh_card(card), unlocked),
                None,
                *self._card_picture_entries(card),
            ]
        if card.kind == "page_card":
            return style + [None, ("Open Page", None, lambda: self.open_card(card), not card.missing)]
        there = not card.missing
        choices = [(app_id, name) for app_id, name in card.apps]
        return style + [
            None,
            *[(f"Open in {name}", None, lambda app_id=app_id: self.open_card(card, app_id), there) for app_id, name in choices],
            ("Open with Windows' App", None, lambda: file_actions.open_file(Path(card.data.get("path", ""))), there),
            None,
            ("Show in Explorer", None, lambda: file_actions.reveal(Path(card.data.get("path", ""))), there),
            ("Show in the Library", None, lambda: self.show_card_in_library(card), there),
            ("Copy Path", None, lambda: QApplication.clipboard().setText(card.data.get("path", "")), True),
            None,
            ("Relink...", None, lambda: self.relink_card(card), unlocked),
            ("Read the Picture Again", None, lambda: self.refresh_card(card), unlocked and there),
            *self._card_picture_entries(card),
        ]

    def _card_picture_entries(self, card: "RefCardItem") -> list:
        unlocked = not self.is_locked(card)
        return [
            ("Set Picture...", None, lambda: self.choose_card_picture(card), unlocked),
            ("Paste Picture", None, lambda: self.paste_card_picture(card), unlocked and self.clipboard_picture() is not None),
            ("Remove Picture", None, lambda: self.set_card_picture(card, None), unlocked and bool(card.data.get("picture"))),
        ]

    def _card_control_at(self, pos: QPointF) -> Optional[tuple["RefCardItem", str]]:
        item = self._image_at(pos)
        if not isinstance(item, RefCardItem) or not item.isVisible():
            return None
        what = item.control_at(item.mapFromScene(self.mapToScene(pos.toPoint())))
        return (item, what) if what is not None else None

    def _hover_cards(self, pos: QPointF) -> None:
        """The card under the mouse shows its buttons."""
        item = self._image_at(pos)
        busy = self._draw_on or self._crop or self._picking or self._line_tool
        target = item if isinstance(item, RefCardItem) and not busy else None
        for card in self.cards():
            if card is target:
                card.set_hover(True, card.control_under(card.mapFromScene(self.mapToScene(pos.toPoint()))))
            else:
                card.set_hover(False)

    def _click_card_control(self, card: "RefCardItem", what: str, at: QPoint) -> None:
        """A hovered card's button: open it, open the file in an app, its More menu, Relink."""
        if what == "relink":
            self.relink_card(card)
        elif what == "more":
            menu = QMenu(self)
            self._fill_menu(menu, self.card_entries(card))
            menu.exec(at)
            menu.deleteLater()
        elif what.startswith("app:"):
            self.open_card(card, what[4:] or None)
        else:
            self.open_card(card)

    @property
    def card_bar(self) -> CardBar:
        if self._card_bar is None:
            self._card_bar = CardBar(self)
        return self._card_bar

    def _sync_card_bar(self) -> None:
        card = self.card_bar_target()
        if self._card_bar is not None and card is not None:
            self._card_bar.sync(card.kind, card.style, self.is_locked(card))

    def _place_card_bar(self) -> None:
        card = self.card_bar_target()
        if card is None or self._crop or self._picking or self._draw_on or not card.isVisible():
            if self._card_bar is not None:
                self._card_bar.hide()
            return
        box = self.mapFromScene(card.mapRectToScene(card.full_rect())).boundingRect()
        bar = self.card_bar
        key = repr((card.item_id, card.style, self.is_locked(card)))
        if not bar.isVisible() or bar.property("card") != key:
            bar.setProperty("card", key)
            bar.sync(card.kind, card.style, self.is_locked(card))
        bar.resize(bar.sizeHint())
        x = max(4, min(box.center().x() - bar.width() // 2, self.width() - bar.width() - 4))
        y = box.top() - bar.height() - NOTE_BAR_GAP
        if y < 4:
            y = min(box.bottom() + 12, self.height() - bar.height() - 4)  # no room above: below it
        bar.move(x, y)
        bar.show()
        bar.raise_()

    def page_link_entries(self, at: QPointF) -> list:
        """Add Here > Page Link: the other pages."""
        pages = [p for p in store.list_pages() if p["id"] != self.page_id and not p.get("damaged")]
        entries = [(p.get("name") or "Page", None, lambda page_id=p["id"]: self.add_page_card(page_id, at), True)
                   for p in sorted(pages, key=lambda p: (p.get("name") or "").lower())[:200]]
        return entries or [("(No other pages yet)", None, lambda: None, False)]

    # ------------------ tables ------------------
    def tables(self) -> list["TableItem"]:
        return [item for item in self._items.values() if isinstance(item, TableItem)]

    def table_at(self, scene_point: QPointF) -> Optional["TableItem"]:
        """The top-most table at a point of the board."""
        for item in self._scene.items(scene_point):
            if isinstance(item, TableItem) and item.isVisible():
                return item
            if isinstance(item, BOARD_ITEMS):
                return None
        return None

    def add_table(self, at: Optional[QPointF] = None) -> "TableItem":
        """Shift+B: a new table at the cursor (or `at`, or the middle of the view): Task, Time and Status columns and
        three empty rows, its first cell ready to type in. Made over a column, it goes in there."""
        self.finish_note_edit()
        self.finish_renaming()
        if at is None:
            under = self.viewport().mapFromGlobal(QCursor.pos())
            at = self.mapToScene(under) if self.viewport().rect().contains(under) else self.view_center()
        table = TableItem(uuid.uuid4().hex)
        table.setScale(min(8.0, max(0.05, 1.0 / self.zoom())))  # reads the same size on screen at any zoom
        table.setPos(at - QPointF(table.width, table.height) * table.scale() / 2)
        table.setZValue(max((i.zValue() for i in self._items.values()), default=0.0) + 1.0)
        self.undo_stack.beginMacro("table")
        self.undo_stack.push(_AddCommand(self, [table], "table"))
        join = self._join_column_at(table, at)
        if join is not None:
            column, before = join
            self.undo_stack.push(_StateCommand(self, {column.item_id: before}, {column.item_id: column.state()}, "table"))
        self.undo_stack.endMacro()
        self._scene.clearSelection()
        table.setSelected(True)
        self.edit_table_cell(table, (0, 0))
        return table

    def editing_table(self) -> Optional["TableItem"]:
        """The table whose cell (or title) is being typed in."""
        item = self._renaming["item"] if self._renaming else None
        return item if isinstance(item, TableItem) else None

    def edit_table_cell(self, table: "TableItem", cell: tuple[int, int], select_all: bool = True) -> None:
        """Type in a table's cell (row -1: a column's name). Tab, Shift+Tab and Enter move on (one undo step for all
        the typing); Esc or a click elsewhere when done. A status cell isn't typed in: it's picked (its chip)."""
        row, column = cell
        if self.is_locked(table) or not (-1 <= row < len(table.rows)) or not (0 <= column < len(table.columns)):
            return
        if (row < 0 and not table.header) or (row >= 0 and table.columns[column][1] == "status"):
            table.current = cell
            table.update()
            return
        renaming = self._renaming
        if renaming is not None and renaming["item"] is table and renaming.get("cell") is not None:
            table.stop_cell_edit()  # (on to another cell: the same undo step)
            renaming["cell"] = cell
        else:
            self.finish_note_edit()
            self.finish_renaming()
            if self.selected() != [table]:  # (typing in a table selects it: its bar shows)
                self._scene.clearSelection()
                table.setSelected(True)
            self._renaming = {"item": table, "text": table.editor, "before": self._snapshot([table]), "cell": cell}
        self.setFocus()
        table.start_cell_edit(cell, select_all)
        self.lay_out_columns()
        self._sync_table_bar()
        self.viewport().update()

    def edit_table_title(self, table: "TableItem") -> None:
        """Double-click a table's title (or the bar's Title): type it (Enter or Esc when done; empty: no title)."""
        if self.is_locked(table):
            return
        self._start_renaming(table, table.title_item)
        table.lay_out()
        self.lay_out_columns()

    def _finish_table_typing(self, renaming: dict[str, Any]) -> None:
        table, text = renaming["item"], renaming["text"]
        if renaming.get("cell") is not None:
            table.stop_cell_edit()
        else:
            text.stop_editing()
            typed = " ".join(text.toPlainText().split())[:MAX_TABLE_TITLE]
            if typed != text.toPlainText():
                text.setPlainText(typed)
            table.lay_out()
        self.lay_out_columns()
        if table.item_id in self._items:
            self._push_states(renaming["before"], "edit table")
        table.update()
        self._sync_table_bar()
        self.viewport().update()

    @staticmethod
    def _cell_order(table: "TableItem") -> list[tuple[int, int]]:
        """The cells Tab goes through: the column names (with the header shown), then the rows' typed cells."""
        typed = [c for c, column in enumerate(table.columns) if column[1] != "status"]
        cells = [(-1, c) for c in range(len(table.columns))] if table.header else []
        return cells + [(r, c) for r in range(len(table.rows)) for c in typed]

    def next_table_cell(self, step: int) -> None:
        """Tab (Shift+Tab) while typing in a table: the next (previous) cell. Past the last one, a new row."""
        table = self.editing_table()
        if table is None or self._renaming.get("cell") is None:
            return
        order = self._cell_order(table)
        cell = self._renaming["cell"]
        index = order.index(cell) if cell in order else 0
        if index + step < 0:
            return
        if index + step >= len(order):
            typed = [c for c, column in enumerate(table.columns) if column[1] != "status"]
            if not typed or len(table.rows) >= MAX_TABLE_ROWS:
                return
            table.rows.append([""] * len(table.columns))
            table.lay_out()
            self.edit_table_cell(table, (len(table.rows) - 1, typed[0]))
            return
        self.edit_table_cell(table, order[index + step])

    def table_cell_below(self) -> None:
        """Enter while typing in a table: the cell below. Under the last row, a new row."""
        table = self.editing_table()
        if table is None or self._renaming.get("cell") is None:
            return
        row, column = self._renaming["cell"]
        if row + 1 >= len(table.rows):
            if len(table.rows) >= MAX_TABLE_ROWS:
                self.finish_renaming()
                return
            table.rows.append([""] * len(table.columns))
            table.lay_out()
        self.edit_table_cell(table, (row + 1, column))

    def _change_table(self, table: Optional["TableItem"], text: str, change: Callable[["TableItem"], None]) -> None:
        """Change a table (one undo step), done typing in it first."""
        if table is None or self.is_locked(table):
            return
        if self.editing_table() is table:
            self.finish_renaming()
        before = self._snapshot([table])
        change(table)
        table.normalize()
        table.lay_out()
        self.lay_out_columns()
        self._push_states(before, text)
        self._sync_table_bar()
        self.viewport().update()

    def table_bar_target(self) -> Optional["TableItem"]:
        """The table the bar works on: the one being typed in, or the only selected item."""
        table = self.editing_table()
        if table is not None:
            return table
        items = self.selected()
        return items[0] if len(items) == 1 and isinstance(items[0], TableItem) else None

    def add_table_row(self, table: Optional["TableItem"] = None, index: Optional[int] = None) -> None:
        """A new empty row at index (default: under the last)."""
        table = table or self.table_bar_target()

        def change(t: "TableItem") -> None:
            if len(t.rows) < MAX_TABLE_ROWS:
                at = len(t.rows) if index is None else max(0, min(index, len(t.rows)))
                t.rows.insert(at, [""] * len(t.columns))
                t.current = (at, t.current[1])

        self._change_table(table, "add row", change)

    def add_table_column(self, table: Optional["TableItem"] = None, index: Optional[int] = None) -> None:
        """A new text column at index (default: past the last)."""
        table = table or self.table_bar_target()

        def change(t: "TableItem") -> None:
            if len(t.columns) < MAX_TABLE_COLUMNS:
                at = len(t.columns) if index is None else max(0, min(index, len(t.columns)))
                t.columns.insert(at, [f"Column {len(t.columns) + 1}", "text", 160.0, ""])
                for row in t.rows:
                    row.insert(at, "")
                t.current = (t.current[0], at)

        self._change_table(table, "add column", change)

    def delete_table_row(self, table: Optional["TableItem"] = None, row: Optional[int] = None) -> None:
        table = table or self.table_bar_target()
        if table is None:
            return
        row = table.current[0] if row is None else row
        if 0 <= row < len(table.rows):
            self._change_table(table, "delete row", lambda t: t.rows.pop(row))

    def delete_table_column(self, table: Optional["TableItem"] = None, column: Optional[int] = None) -> None:
        table = table or self.table_bar_target()
        if table is None:
            return
        column = table.current[1] if column is None else column
        if len(table.columns) < 2:
            self.show_message("A table keeps at least one column: delete the table instead.")
            return

        def change(t: "TableItem") -> None:
            t.columns.pop(column)
            for cells in t.rows:
                cells.pop(column)

        if 0 <= column < len(table.columns):
            self._change_table(table, "delete column", change)

    def move_table_row(self, table: "TableItem", row: int, to: int) -> None:
        """Move a row to index `to` (counted once it's out)."""
        if not (0 <= row < len(table.rows)) or not (0 <= to < len(table.rows)) or row == to:
            return

        def change(t: "TableItem") -> None:
            t.rows.insert(to, t.rows.pop(row))
            t.current = (to, t.current[1])

        self._change_table(table, "move row", change)

    def move_table_column(self, table: "TableItem", column: int, to: int) -> None:
        if not (0 <= column < len(table.columns)) or not (0 <= to < len(table.columns)) or column == to:
            return

        def change(t: "TableItem") -> None:
            t.columns.insert(to, t.columns.pop(column))
            for cells in t.rows:
                cells.insert(to, cells.pop(column))
            t.current = (t.current[0], to)

        self._change_table(table, "move column", change)

    def set_table_column_kind(self, kind: str, table: Optional["TableItem"] = None, column: Optional[int] = None) -> None:
        """Text, Number (a total under it) or Status (chips). The cells keep their text either way."""
        table = table or self.table_bar_target()
        if table is None or kind not in TABLE_KINDS:
            return
        column = table.current[1] if column is None else column
        if 0 <= column < len(table.columns) and table.columns[column][1] != kind:
            self._change_table(table, "column type", lambda t: t.columns[column].__setitem__(1, kind))

    def set_table_column_align(self, align: str, table: Optional["TableItem"] = None, column: Optional[int] = None) -> None:
        """Left, Center or Right for a column's cells (its name and total too)."""
        table = table or self.table_bar_target()
        if table is None or align not in TABLE_ALIGNS:
            return
        column = table.current[1] if column is None else column
        if 0 <= column < len(table.columns) and table.align_of(column) != align:
            self._change_table(table, "align column", lambda t: t.columns[column].__setitem__(3, align))

    def toggle_table_header(self, table: Optional["TableItem"] = None) -> None:
        table = table or self.table_bar_target()
        if table is not None:
            self._change_table(table, "header row", lambda t: setattr(t, "header", not t.header))

    def toggle_table_title(self, table: Optional["TableItem"] = None) -> None:
        """A title over the table (typed straight away), or none."""
        table = table or self.table_bar_target()
        if table is None or self.is_locked(table):
            return
        if table.title():
            self._change_table(table, "remove title", lambda t: t.set_title(""))
            return
        self.finish_renaming()
        before = self._snapshot([table])
        table.set_title("Untitled")
        self.edit_table_title(table)
        self._renaming["before"] = before  # (undo takes the title away)

    def set_table_status(self, table: "TableItem", row: int, column: int, name: str) -> None:
        """A status cell's status ("": none)."""
        if 0 <= row < len(table.rows) and 0 <= column < len(table.columns) and table.rows[row][column] != name:
            def change(t: "TableItem") -> None:
                t.rows[row][column] = name
                t.current = (row, column)

            self._change_table(table, "status", change)

    def add_table_status(self, table: "TableItem", name: str, color: Optional[str] = None) -> bool:
        """A new status for the table's status columns (False: no name, or it has one by that name)."""
        name = " ".join(name.split())[:MAX_TABLE_TITLE]
        if not name or name in dict(table.statuses) or len(table.statuses) >= MAX_STATUSES:
            return False
        if color is None:
            used = {c for _n, c in table.statuses}
            color = next((c for _n, c in STATUS_PALETTE if c not in used), STATUS_PALETTE[0][1])
        self._change_table(table, "add status", lambda t: t.statuses.append((name, color)))
        return True

    def rename_table_status(self, table: "TableItem", old: str, new: str) -> bool:
        """Rename a status: the cells that have it follow."""
        new = " ".join(new.split())[:MAX_TABLE_TITLE]
        if not new or old not in dict(table.statuses) or (new != old and new in dict(table.statuses)):
            return False

        def change(t: "TableItem") -> None:
            t.statuses = [(new if n == old else n, c) for n, c in t.statuses]
            status_columns = [c for c, column in enumerate(t.columns) if column[1] == "status"]
            for cells in t.rows:
                for c in status_columns:
                    if cells[c] == old:
                        cells[c] = new

        self._change_table(table, "rename status", change)
        return True

    def set_table_status_color(self, table: "TableItem", name: str, color: str) -> None:
        if name in dict(table.statuses) and QColor(color).isValid():
            self._change_table(table, "status colour", lambda t: setattr(
                t, "statuses", [(n, color if n == name else c) for n, c in t.statuses]))

    def delete_table_status(self, table: "TableItem", name: str) -> None:
        """Take a status away: the cells that had it have none."""
        if name not in dict(table.statuses):
            return

        def change(t: "TableItem") -> None:
            t.statuses = [(n, c) for n, c in t.statuses if n != name]
            status_columns = [c for c, column in enumerate(t.columns) if column[1] == "status"]
            for cells in t.rows:
                for c in status_columns:
                    if cells[c] == name:
                        cells[c] = ""

        self._change_table(table, "delete status", change)

    def set_table_statuses(self, table: "TableItem", entries: list[tuple[Optional[str], str, str]]) -> None:
        """Edit Statuses: the table's statuses as the dialog left them, one undo step. entries: (the name it had, None
        for a new one; its name; its colour). A renamed one's cells follow; a removed one's are left empty. An empty
        name keeps the one it had (a new one without a name isn't added); two with one name become one."""
        statuses: list[tuple[str, str]] = []
        renamed: dict[str, str] = {}
        for original, name, color in entries:
            name = " ".join(name.split())[:MAX_TABLE_TITLE] or (original or "")
            if not name:
                continue
            if original is not None:
                renamed[original] = name
            if name not in dict(statuses) and len(statuses) < MAX_STATUSES:
                statuses.append((name, color if QColor(color).isValid() else DEFAULT_STATUSES[0][1]))

        def change(t: "TableItem") -> None:
            known = dict(t.statuses)
            status_columns = [c for c, column in enumerate(t.columns) if column[1] == "status"]
            for cells in t.rows:
                for c in status_columns:
                    if cells[c] in known:
                        cells[c] = renamed.get(cells[c], "")
            t.statuses = statuses

        self._change_table(table, "edit statuses", change)

    def edit_table_statuses(self, table: Optional["TableItem"] = None) -> None:
        """Edit Statuses...: all the table's statuses in a dialog, to rename, recolour, add and remove."""
        table = table or self.table_bar_target()
        if table is None or self.is_locked(table):
            return
        dialog = StatusDialog(table.statuses, STATUS_PALETTE, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_table_statuses(table, dialog.statuses())
        dialog.deleteLater()

    def status_menu(self, table: "TableItem", row: int, column: int) -> QMenu:
        """A status cell's picker: the table's statuses, none, a new one, and editing them."""
        menu = QMenu(self)
        current = table.rows[row][column]
        for name, color in table.statuses:
            action = menu.addAction(swatch_icon(color), name)
            action.setCheckable(True)
            action.setChecked(name == current)
            action.triggered.connect(lambda _checked=False, name=name: self.set_table_status(table, row, column, name))
        clear = menu.addAction("No Status")
        clear.setEnabled(bool(current))
        clear.triggered.connect(lambda _checked=False: self.set_table_status(table, row, column, ""))
        menu.addSeparator()
        menu.addAction("Edit Statuses...").triggered.connect(lambda _checked=False: self.edit_table_statuses(table))
        return menu

    def ask_table_status(self, table: "TableItem", row: int, column: int, at: QPoint) -> None:
        """Click a status chip: pick its status."""
        if self.is_locked(table):
            return
        table.current = (row, column)
        menu = self.status_menu(table, row, column)
        menu.exec(at)
        menu.deleteLater()

    def table_column_entries(self, table: "TableItem") -> list:
        """The bar's Column (and the right-click menu's): the outlined cell's column."""
        unlocked = not self.is_locked(table)
        column = table.current[1]
        kind = table.columns[column][1]
        align = table.align_of(column)
        last = len(table.columns) - 1
        return [
            ("Text", None, lambda: self.set_table_column_kind("text", table, column), unlocked, kind == "text"),
            ("Number (with a Total)", None, lambda: self.set_table_column_kind("number", table, column), unlocked, kind == "number"),
            ("Status", None, lambda: self.set_table_column_kind("status", table, column), unlocked, kind == "status"),
            *([("Edit Statuses...", None, lambda: self.edit_table_statuses(table), unlocked)] if kind == "status" else []),
            None,
            ("Align", [
                ("Left", None, lambda: self.set_table_column_align("left", table, column), unlocked, align == "left"),
                ("Center", None, lambda: self.set_table_column_align("center", table, column), unlocked, align == "center"),
                ("Right", None, lambda: self.set_table_column_align("right", table, column), unlocked, align == "right"),
            ]),
            None,
            ("Add Column Left", None, lambda: self.add_table_column(table, column), unlocked),
            ("Add Column Right", None, lambda: self.add_table_column(table, column + 1), unlocked),
            ("Move Column Left", None, lambda: self.move_table_column(table, column, column - 1), unlocked and column > 0),
            ("Move Column Right", None, lambda: self.move_table_column(table, column, column + 1), unlocked and column < last),
            None,
            ("Delete Column", None, lambda: self.delete_table_column(table, column), unlocked and last > 0),
        ]

    def table_row_entries(self, table: "TableItem") -> list:
        """The bar's Row (and the right-click menu's): the outlined cell's row."""
        unlocked = not self.is_locked(table)
        row = table.current[0]
        on_row = 0 <= row < len(table.rows)
        return [
            ("Add Row Above", None, lambda: self.add_table_row(table, max(0, row)), unlocked and on_row),
            ("Add Row Below", None, lambda: self.add_table_row(table, row + 1), unlocked),
            ("Move Row Up", None, lambda: self.move_table_row(table, row, row - 1), unlocked and on_row and row > 0),
            ("Move Row Down", None, lambda: self.move_table_row(table, row, row + 1), unlocked and on_row and row < len(table.rows) - 1),
            None,
            ("Delete Row", None, lambda: self.delete_table_row(table, row), unlocked and on_row),
        ]

    def table_entries(self, table: "TableItem") -> list:
        """The right-click menu's Table."""
        unlocked = not self.is_locked(table)
        return [
            ("Column", self.table_column_entries(table)),
            ("Row", self.table_row_entries(table)),
            None,
            ("Header Row", None, lambda: self.toggle_table_header(table), unlocked, table.header),
            ("Title", None, lambda: self.toggle_table_title(table), unlocked, bool(table.title())),
        ]

    def table_formats(self) -> dict[str, Any]:
        table = self.table_bar_target()
        return {"kind": table.columns[table.current[1]][1], "align": table.align_of(table.current[1]),
                "header": table.header, "title": bool(table.title()), "locked": self.is_locked(table)}

    def _table_reach(self, table: "TableItem") -> float:
        """TABLE_EDGE_PX on screen, in the table's own units."""
        return TABLE_EDGE_PX / max(1e-9, self.zoom() * abs(table.scale()))

    def _table_control_at(self, pos: QPointF) -> Optional[tuple["TableItem", str, Any]]:
        """What a click on a table does: (table, "plus_row" / "plus_column" / "grip" (a row) / "edge" (a column) /
        "status" (a cell), what); None: a plain click (select it, move it, a cell to type in)."""
        item = self._image_at(pos)
        if not isinstance(item, TableItem) or not item.isVisible() or self.is_locked(item):
            return None
        local = item.mapFromScene(self.mapToScene(pos.toPoint()))
        if item.hovered:
            for which in ("row", "column"):
                if item.plus_rect(which).contains(local):
                    return item, "plus_" + which, None
        if not item.full_rect().contains(local):
            return None
        row = item.row_at(local.y())
        if row is not None and item.grip_rect(row).contains(local):
            return item, "grip", row
        edge = item.edge_at(local, self._table_reach(item))
        if edge is not None:
            return item, "edge", edge
        cell = item.cell_at(local)
        if cell is not None and cell[0] >= 0:
            chip = item.chip_rect(*cell)
            if chip is not None and chip.contains(local):
                return item, "status", cell
        return None

    def _hover_tables(self, pos: QPointF) -> None:
        """The table under the mouse shows its + bars, its row's grip and the column edge it's on."""
        item = self._image_at(pos)
        busy = self._draw_on or self._crop or self._picking or self._line_tool
        target = item if isinstance(item, TableItem) and not busy and not self.is_locked(item) else None
        for table in self.tables():
            if table is not target:
                table.set_hover(False)
                continue
            local = table.mapFromScene(self.mapToScene(pos.toPoint()))
            plus = next((w for w in ("row", "column") if table.hovered and table.plus_rect(w).contains(local)), None)
            inside = table.full_rect().contains(local)
            row = table.row_at(local.y()) if plus is None and inside else None
            edge = table.edge_at(local, self._table_reach(table)) if plus is None and inside else None
            table.set_hover(True, plus, row, edge)

    def _drag_table_row(self, drag: dict[str, Any], pos: QPointF) -> None:
        table = drag["table"]
        local = table.mapFromScene(self.mapToScene(pos.toPoint()))
        table.drop_row = table.drop_index(local.y())
        table.update()

    def _drop_table_row(self, drag: dict[str, Any]) -> None:
        table = drag["table"]
        drop, table.drop_row = table.drop_row, None
        row = drag["row"]
        if drop is not None:
            to = drop if drop <= row else drop - 1  # (counted once the row is out)
            if to != row:
                self.move_table_row(table, row, to)
        table.update()

    def _drag_table_column(self, drag: dict[str, Any], pos: QPointF) -> None:
        table = drag["table"]
        local = table.mapFromScene(self.mapToScene(pos.toPoint()))
        table.set_column_width(drag["column"], drag["width"] + local.x() - drag["press"])
        table.edge_hot = drag["column"]
        self.lay_out_columns()
        self.viewport().update()

    @property
    def table_bar(self) -> TableBar:
        if self._table_bar is None:
            self._table_bar = TableBar(self)
        return self._table_bar

    def _sync_table_bar(self) -> None:
        if self._table_bar is not None and self.table_bar_target() is not None:
            self._table_bar.sync(self.table_formats())

    def _place_table_bar(self) -> None:
        table = self.table_bar_target()
        if table is None or self._crop or self._picking or self._draw_on or table.scene() is not self._scene or not table.isVisible():
            if self._table_bar is not None:
                self._table_bar.hide()
            return
        box = self.mapFromScene(table.mapRectToScene(table.full_rect())).boundingRect()
        bar = self.table_bar
        bar.resize(bar.sizeHint())
        x = max(4, min(box.center().x() - bar.width() // 2, self.width() - bar.width() - 4))
        y = box.top() - bar.height() - NOTE_BAR_GAP
        if y < 4:
            y = min(box.bottom() + 12, self.height() - bar.height() - 4)  # no room above: below it
        formats = self.table_formats()
        key = (table.item_id, table.current, tuple(sorted(formats.items())))
        if not bar.isVisible() or bar.property("table") != repr(key):
            bar.setProperty("table", repr(key))
            bar.sync(formats)
        bar.move(x, y)
        bar.show()
        bar.raise_()

    def editing_note(self) -> Optional[NoteItem]:
        return self._editing["note"] if self._editing else None

    def add_tool(self, tool: str, at: Optional[QPointF] = None) -> None:
        """Add one of the rail's tools (ui.artbook_rail): at `at`, or in the middle of the view."""
        if tool in ("note", "heading"):
            self.add_note(at if at is not None else self.view_center(), style="card" if tool == "note" else "heading")
        elif tool == "todo":
            self.add_todo(at if at is not None else self.view_center())
        elif tool == "column":
            self.add_column(at if at is not None else self.view_center())
        elif tool == "table":
            self.add_table(at if at is not None else self.view_center())
        elif tool == "line":
            self.toggle_line_tool()
        elif tool == "pictures":
            self.choose_pictures(at)
        elif tool == "draw":
            self.toggle_draw()

    def add_todo(self, at: Optional[QPointF] = None) -> NoteItem:
        """X: a to-do at the cursor (or `at`): a card with a title to type, then a checklist (Enter goes on to it)."""
        note = self.add_note(at)
        document = note.document()
        cursor = QTextCursor(document)
        cursor.insertBlock()
        set_paragraph(QTextCursor(document.findBlockByNumber(1)), "check")
        title = QTextCursor(document.firstBlock())
        set_paragraph(title, "h2")
        note.setTextCursor(title)
        note.update()
        return note

    def toggle_check(self, note: NoteItem, row: int) -> None:
        """Tick a to-do's line (or untick it): one undo step (while typing, part of the typing)."""
        if self._editing and self._editing["note"] is not note:
            self.finish_note_edit()
        before = note.content()
        tick_row(note.document(), row)
        note.update()
        if not note.editing() and note.content() != before:
            self.undo_stack.push(_TextCommand(note, before, note.content(), "tick"))
        self.viewport().update()

    def _drag_row(self, drag: dict[str, Any], pos: QPointF) -> None:
        note = drag["note"]
        local = note.mapFromScene(self.mapToScene(pos.toPoint()))
        target = note.row_at(local)
        listed = note.document().findBlockByNumber(drag["row"]).textList()
        if target is not None and note.document().findBlockByNumber(target).textList() == listed:
            drag["target"] = target
            below = target if target < drag["row"] else target + 1  # (the line goes above this one)
            block = note.document().findBlockByNumber(below)
            note.drop_row = below if block.isValid() and block.textList() == listed else None
            if note.drop_row is None:  # (to the very end: the line under the last one)
                note.drop_row = target
        note.update()

    def _drop_row(self, drag: dict[str, Any]) -> None:
        note = drag["note"]
        note.drop_row = None
        if drag["target"] != drag["row"]:
            before = note.content()
            move_row(note.document(), drag["row"], drag["target"])
            if not note.editing() and note.content() != before:
                self.undo_stack.push(_TextCommand(note, before, note.content(), "move line"))
        note.update()

    def _hover_rows(self, pos: QPointF) -> None:
        """Show the dots of the checklist line under the mouse."""
        item = self._image_at(pos)
        row = None
        if isinstance(item, NoteItem) and not self.is_locked(item):
            row = item.row_at(item.mapFromScene(self.mapToScene(pos.toPoint())))
        previous = getattr(self, "_row_note", None)
        if previous is not None and previous is not item and previous.scene() is self._scene:
            previous.set_hover_row(None)
        if isinstance(item, NoteItem):
            item.set_hover_row(row)
            self._row_note = item
        else:
            self._row_note = None

    def choose_pictures(self, at: Optional[QPointF] = None) -> int:
        """Pick image files and add them (at `at`, or in the middle of the view); returns how many were added."""
        extensions = " ".join(f"*{ext}" for ext in sorted(store.IMAGE_EXTENSIONS))
        paths, _filter = QFileDialog.getOpenFileNames(self, "Add Pictures", get_last_pictures_dir(), f"Images ({extensions})")
        if not paths:
            return 0
        set_last_pictures_dir(str(Path(paths[0]).parent))
        return self.add_files([Path(path) for path in paths], at)

    def add_note(self, at: Optional[QPointF] = None, style: str = "card") -> NoteItem:
        """T: a new note at the cursor (or `at`, or the middle of the view), ready to type in. Shift+T: a heading."""
        self.finish_note_edit()
        if at is None:
            under = self.viewport().mapFromGlobal(QCursor.pos())
            at = self.mapToScene(under) if self.viewport().rect().contains(under) else self.view_center()
        note = NoteItem(uuid.uuid4().hex)
        if style in NOTE_STYLES and style != "card":
            note.set_content(("", None, style))
        _center_text(note)
        note.setScale(min(8.0, max(0.05, 1.0 / self.zoom())))  # reads the same size on screen at any zoom
        box = note.boundingRect()
        note.setPos(at - QPointF(box.width(), box.height()) * note.scale() / 2)
        note.setZValue(max((i.zValue() for i in self._items.values()), default=0.0) + 1.0)
        self.attach(note)  # recorded for undo once it has text (see finish_note_edit)
        self._edit(note, new=True)
        join = self._join_column_at(note, at)  # made over a column: it goes in there
        if join is not None:
            self._editing["join"] = join
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
        if note.style == "title":
            name = " ".join(text.split())[:store.MAX_NAME]
            if name != text:
                note.set_title_text(name or self.page_name or "Untitled")
            note.fit_title()
            text = note.toPlainText()
            if not editing["new"] and note.content() != before:
                self.undo_stack.push(_TextCommand(note, before, note.content(), "rename page"))
            self.viewport().update()
            return
        if len(text) > MAX_NOTE_TEXT:
            note.setPlainText(text[:MAX_NOTE_TEXT])  # (drops its formatting: only absurdly long notes)
            text = note.toPlainText()
        if editing["new"]:
            join = editing.get("join")
            if text.strip() and join is not None and join[0].item_id in self._items:
                column, before = join  # (added to the column with it: one undo step)
                self.undo_stack.beginMacro("note")
                self.undo_stack.push(_AddCommand(self, [note], "note"))
                self.undo_stack.push(_StateCommand(self, {column.item_id: before}, {column.item_id: column.state()}, "note"))
                self.undo_stack.endMacro()
            elif text.strip():
                self.undo_stack.push(_AddCommand(self, [note], "note"))
            else:
                self.detach(note)  # never typed in: as if it was never made
                for column in self.columns():
                    if note.item_id in column.members:
                        column.members.remove(note.item_id)
                self.lay_out_columns()
        elif not text.strip():
            note.set_content(before)  # deleting a note's text deletes the note (undo brings it back)
            self.undo_stack.push(_RemoveCommand(self, [note] + self.lines_of([note]), "delete note"))
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

    def live_change(self, on: bool) -> None:
        """A custom colour is being picked (changing live: a note's, or a group's): its changes add up
        to one undo step."""
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
        block = cursor.blockFormat()
        if highlight is None and block.hasProperty(QTextFormat.Property.BackgroundBrush):
            highlight = block.background().color()  # a whole paragraph's (pasted with the text)
        return {
            "bold": fmt.fontWeight() >= QFont.Weight.DemiBold.value,
            "italic": fmt.fontItalic(),
            "underline": fmt.fontUnderline(),
            "size": fmt.fontPointSize() or note.font().pointSizeF(),
            "family": (fmt.fontFamilies() or [note.font().family()])[0] if fmt.hasProperty(QTextCharFormat.Property.FontFamilies) else note.font().family(),
            "color": color,
            "highlight": highlight,
            "fill": card_colors(note.fill)[0],
            "align": cursor.blockFormat().alignment() & Qt.AlignmentFlag.AlignHorizontal_Mask,
            "style": note.style,
            "paragraph": paragraph_of(cursor),
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
        """Highlight the text (None: no highlight). Either way, the paragraphs it touches lose any
        background of their own (pasted text brings one), so this colour is the only one behind it."""

        def change(cursor: QTextCursor) -> None:
            clear_paragraph_backgrounds(cursor.document(), cursor.selectionStart(), cursor.selectionEnd())
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

    def set_note_style(self, style: str) -> None:
        """Card, Heading or Plain (one undo step). A heading's text is big and bold; back to a card or plain,
        it's the usual size again."""
        note = self.note_bar_target()
        if note is None or style not in NOTE_STYLES or note.style == style or "title" in (style, note.style):
            return  # (the page's header stays one: Add Here > Page Title makes it)
        before = note.content()
        was_heading = note.style == "heading"
        note.style = style
        note.setFont(note.base_font())
        if style == "heading" or was_heading:
            fmt = QTextCharFormat()
            fmt.setFontPointSize(float(HEADING_PT if style == "heading" else NOTE_FONT_PT))
            fmt.setFontWeight(QFont.Weight.Bold if style == "heading" else QFont.Weight.Normal)
            cursor = QTextCursor(note.document())
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.mergeCharFormat(fmt)
        note.lay_out()
        note.update()
        if not note.editing():
            self.undo_stack.push(_TextCommand(note, before, note.content(), "note style"))
        self._sync_note_bar()
        self._queue_note_bar()

    def set_note_paragraph(self, kind: str) -> None:
        """Text, Heading 1 or 2, a bulleted or a numbered list: the lines the text cursor is on while typing,
        otherwise every line of the note (one undo step)."""
        note = self.note_bar_target()
        if note is None or kind not in PARAGRAPHS:
            return
        before = note.content()
        typing = note.editing()
        cursor = note.textCursor() if typing else QTextCursor(note.document())
        if not typing:
            cursor.select(QTextCursor.SelectionType.Document)
        set_paragraph(cursor, kind, note.style)
        if not typing and note.content() != before:
            self.undo_stack.push(_TextCommand(note, before, note.content(), "paragraph"))
        self._sync_note_bar()

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
        """Plain text in the theme's colours, centered (as a new note is): all formatting gone."""
        note = self.note_bar_target()
        if note is None:
            return
        before = note.content()
        note.setPlainText(note.toPlainText())
        _center_text(note)
        note.lay_out()  # (a heading's room for its line: setting the text resets it)
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
        self._place_group_bar()
        self._place_image_bar()
        self._place_line_bar()
        self._place_table_bar()
        self._place_card_bar()
        self._place_comment_thread()
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
                if isinstance(item, (NoteItem, StrokeItem, ColumnItem, LineItem, TableItem, RefCardItem, PointItem)):
                    item.apply_theme()  # notes and strokes in the theme's colours follow it
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
        if len(items) != 1 or not isinstance(items[0], ImageItem) or self.is_locked(items[0]):
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
        items = self._unlocked(self.selected())
        if len(items) < len(self.selected()):
            self.show_message("A locked group can't be deleted: unlock it first (the lock above it).")
        for column in [i for i in items if isinstance(i, ColumnItem)]:
            items += [card for card in self.column_cards(column) if card not in items]  # (a column goes with its cards)
        points = [i for i in items if isinstance(i, PointItem)]
        if points:
            self.undo_stack.beginMacro("delete")
            items += self._rejoin_lines(points, items)  # (a point's two lines: one again)
        items += [line for line in self.lines_of(items) if line not in items]  # (and lines to what goes)
        if items:
            self.undo_stack.push(_RemoveCommand(self, items, "delete"))
        if points:
            self.undo_stack.endMacro()

    def select_all(self) -> None:
        for item in self._items.values():
            item.setSelected(True)

    def copy_selection(self) -> bool:
        """Ctrl+C: the selected cards themselves (Ctrl+V puts them on this page or another), and for other apps the
        top one's text (a note's, a table's tab-separated cells, a card's address) or picture."""
        items = sorted(self.selected(), key=lambda i: i.zValue())
        if not items:
            return False
        mime = QMimeData()
        package = self.selection_package(items)
        if package is not None:
            mime.setData(PACKAGE_MIME, json.dumps(package).encode("utf-8"))
        top = items[-1]
        if isinstance(top, NoteItem):
            mime.setText(top.toPlainText())
        elif isinstance(top, TableItem):
            mime.setText(top.plain_text())  # (tab-separated: pastes into a spreadsheet)
        elif isinstance(top, RefCardItem):  # (a link's address, a file's path, a page's name)
            mime.setText(top.data.get("url") or top.data.get("path") or top.title())
        elif isinstance(top, ImageItem) and top.pixmap is not None:
            sharp = self.sharp_pictures([top], lambda i: EXPORT_DETAIL_SIDE).get(top.item_id)  # (not just what's loaded)
            mime.setImageData(top.image(sharp))
        if not mime.formats():
            return False
        QApplication.clipboard().setMimeData(mime)
        return True

    # ------------------ duplicate, paste and send cards (to this page or another) ------------------
    def selection_package(self, items: Optional[list] = None) -> Optional[dict[str, Any]]:
        """The selected things as saved, to put down again (here or on another page): a column with its cards, the
        lines between them, their comments and their groups' colours; None if there's nothing to copy."""
        items = [i for i in (self.selected() if items is None else items) if not isinstance(i, LineItem)]
        for column in [i for i in items if isinstance(i, ColumnItem)]:
            items += [card for card in self.column_cards(column) if card not in items]
        if not items:
            return None
        ids = {i.item_id for i in items}
        groups = {i.group for i in items if i.group}
        lines = []
        for line in self.lines_of(items):
            ends = [line.to_dict()[key] for key in ("start", "end")]
            if all((end.get("item") in ids) if end.get("item") else (not end.get("group") or end["group"] in groups) for end in ends):
                lines.append(line)
        bounds = QRectF()
        for item in items:
            bounds = bounds.united(item.sceneBoundingRect())
        return {
            "version": 1, "page": self.page_id,
            "items": [i.to_dict() for i in sorted(items, key=lambda i: i.zValue())] + [line.to_dict() for line in lines],
            "comments": [json.loads(json.dumps(t)) for t in self._comments if t.get("item") in ids],
            "groups": {gid: dict(meta) for gid, meta in self._groups.items() if gid in groups},
            "bounds": [bounds.x(), bounds.y(), bounds.width(), bounds.height()],
        }

    @staticmethod
    def package_for_page(package: dict[str, Any], page_id: Optional[str]) -> tuple[list, list, dict]:
        """A package's items, comments and group colours with new ids (columns, lines, comments and groups following),
        their pictures copied into page_id's folder if they came from another page."""
        data = json.loads(json.dumps(package))
        items = [d for d in data.get("items", []) if isinstance(d, dict)]
        ids = {d["id"]: uuid.uuid4().hex for d in items if isinstance(d.get("id"), str)}
        groups = {gid: uuid.uuid4().hex for gid in {d.get("group") for d in items} if isinstance(gid, str)}
        source = data.get("page")

        def blob(name: Any) -> Any:
            if not isinstance(name, str) or not name or source == page_id or not source or not page_id:
                return name
            path = store.blob_path(source, name)
            return store.store_blob(page_id, path.read_bytes(), path.suffix) if path.is_file() else name

        for d in items:
            d["id"] = ids.get(d.get("id"), uuid.uuid4().hex)
            if d.get("group") in groups:
                d["group"] = groups[d["group"]]
            kind = d.get("kind", "image")
            if kind == "column":
                d["members"] = [ids[m] for m in d.get("members", []) if m in ids]
            elif kind == "line":
                for key in ("start", "end"):
                    end = d.get(key) if isinstance(d.get(key), dict) else {}
                    if end.get("item"):
                        end["item"] = ids.get(end["item"])
                    if end.get("group"):
                        end["group"] = groups.get(end["group"])
            elif kind == "image":
                if isinstance(d.get("source"), dict):
                    d["source"]["blob"] = blob(d["source"].get("blob"))
                if isinstance(d.get("original"), dict) and isinstance(d["original"].get("source"), dict):
                    d["original"]["source"]["blob"] = blob(d["original"]["source"].get("blob"))
            elif kind in CARD_KINDS:
                for key in ("picture", "favicon"):
                    if d.get(key):
                        d[key] = blob(d[key])
        comments = []
        for thread in data.get("comments", []):
            if isinstance(thread, dict) and thread.get("item") in ids:
                comments.append(dict(thread, id=uuid.uuid4().hex, item=ids[thread["item"]]))
        metas = {groups[gid]: meta for gid, meta in (data.get("groups") or {}).items() if gid in groups and isinstance(meta, dict)}
        return items, comments, metas

    def insert_package(self, package: dict[str, Any], at: Optional[QPointF] = None, offset: Optional[QPointF] = None,
                       text: str = "paste") -> list:
        """Put a package's things on this page (new ones: one undo step): centred on at, or moved by offset from where
        they were, on top of everything; they're selected."""
        if self.page_id is None:
            return []
        self.finish_note_edit()
        self.finish_renaming()
        dicts, comments, metas = self.package_for_page(package, self.page_id)
        made = [item for item in (item_from_dict(d) for d in dicts if d.get("kind") != "line") if item is not None]
        if not made:
            return []
        bounds = QRectF()
        for item in made:
            bounds = bounds.united(item.sceneBoundingRect())
        delta = (at - bounds.center()) if at is not None else (offset if offset is not None else QPointF(0, 0))
        for item in made:
            item.setPos(item.pos() + delta)
        for d in dicts:  # (a line's free end moves with the rest)
            if d.get("kind") == "line":
                for key in ("start", "end"):
                    end = d.get(key) if isinstance(d.get(key), dict) else {}
                    if not end.get("item") and not end.get("group"):
                        end["x"], end["y"] = float(end.get("x", 0)) + delta.x(), float(end.get("y", 0)) + delta.y()
                line = item_from_dict(d)
                if line is not None:
                    made.append(line)
        top = max((i.zValue() for i in self._items.values()), default=0.0) + 1.0
        lowest = min(i.zValue() for i in made)
        for item in made:
            item.setZValue(item.zValue() - lowest + top)
        for gid, meta in metas.items():
            self._groups.setdefault(gid, meta)
        self._comments += comments
        self.undo_stack.push(_AddCommand(self, made, text))
        self.lay_out_lines()
        self._scene.clearSelection()
        for item in made:
            if not isinstance(item, LineItem):
                item.setSelected(True)
        self.viewport().update()
        return made

    def duplicate_selection(self) -> list:
        """Ctrl+D: a copy of the selection beside it (lines between the cards copied too)."""
        package = self.selection_package()
        if package is None:
            return []
        step = 24.0 / self.zoom()
        return self.insert_package(package, offset=QPointF(step, step), text="duplicate")

    def paste_package(self, mime, at: Optional[QPointF] = None) -> bool:
        """Copied cards (Ctrl+C, from this page or another): put down at at (the mouse, or the middle of the view)."""
        if mime is None or not mime.hasFormat(PACKAGE_MIME):
            return False
        try:
            package = json.loads(bytes(mime.data(PACKAGE_MIME)).decode("utf-8"))
        except ValueError:
            return False
        if not isinstance(package, dict):
            return False
        if at is None:
            under = self.viewport().mapFromGlobal(QCursor.pos())
            at = self.mapToScene(under) if self.viewport().rect().contains(under) else self.view_center()
        return bool(self.insert_package(package, at=at))

    def send_selection(self, page_id: str, move: bool) -> int:
        """Right-click > Move to Page / Copy to Page: the selection onto another page (to the right of what's there),
        one undo step (undo takes it off that page again, and a move puts it back here); how many things went."""
        package = self.selection_package()
        if package is None or not page_id or page_id == self.page_id:
            return 0
        try:
            target = store.load_page(page_id)
        except store.PageError as exc:
            self.show_message(str(exc))
            return 0
        dicts, comments, metas = self.package_for_page(package, page_id)
        place = _free_spot(target.get("items", []))
        left, top = package["bounds"][0], package["bounds"][1]
        dx, dy = place[0] - left, place[1] - top
        for d in dicts:
            if d.get("kind") == "line":
                for key in ("start", "end"):
                    end = d.get(key) if isinstance(d.get(key), dict) else {}
                    if not end.get("item") and not end.get("group"):
                        end["x"], end["y"] = float(end.get("x", 0)) + dx, float(end.get("y", 0)) + dy
            elif isinstance(d.get("x"), (int, float)):
                d["x"], d["y"] = d["x"] + dx, float(d.get("y", 0)) + dy
        top_z = max((float(d.get("z", 0)) for d in target.get("items", []) if isinstance(d, dict)), default=0.0) + 1.0
        for d in dicts:
            d["z"] = float(d.get("z", 0)) + top_z
        items = [self._items[d["id"]] for d in package["items"] if d.get("id") in self._items] if move else []
        if move:
            items += [line for line in self.lines_of(items) if line not in items]
        name = target.get("name") or "Untitled"
        count = len([d for d in dicts if d.get("kind") != "line"])
        self.undo_stack.push(_SendCommand(self, page_id, dicts, comments, metas, items,
                                          f"{'move' if move else 'copy'} to {name}"))
        self.show_message(f"{'Moved' if move else 'Copied'} {count} thing{'s' if count != 1 else ''} to \u201c{name}\u201d.")
        return count

    def send_menu_entries(self, move: bool) -> list:
        """Right-click > Move to Page / Copy to Page: the other pages, by folder."""
        entries: list = []
        has = bool(self.selected())
        tree = store.page_tree()

        def add(folder: dict[str, Any], depth: int) -> None:
            pages = [p for p in folder.get("pages", []) if p != self.page_id]
            folders = folder.get("folders", [])
            if depth and (pages or folders):
                entries.append(QAction(folder.get("name", "Folder").upper(), self))
                entries[-1].setEnabled(False)
            for page_id in pages:
                summary = store.page_summary(page_id) or {}
                entries.append((summary.get("name") or "Untitled", None, lambda p=page_id: self.send_selection(p, move), has))
            for sub in folders:
                add(sub, depth + 1)

        add(tree, 0)
        if not entries:
            entries.append(("(No other pages yet)", None, lambda: None, False))
        return entries

    # ------------------ arrow keys: nudge ------------------
    def nudge(self, dx: float, dy: float) -> None:
        """Arrow keys: move the selection a pixel on screen (Shift: ten); nudges in a row are one undo step."""
        items = [i for i in self._unlocked(self.selected()) if not isinstance(i, LineItem) and self.column_of(i) is None]
        if not items:  # (nothing selected: the arrows move about the board)
            self.set_view(self.zoom(), self.view_center() + QPointF(dx, dy) * 40 / self.zoom())
            return
        before = self._snapshot(items)
        step = QPointF(dx, dy) / self.zoom()
        for item in items:
            item.setPos(item.pos() + step)
        self.lay_out_columns()
        self.lay_out_lines()
        after = {item.item_id: item.state() for item in items}
        self.undo_stack.push(_MergingStateCommand(self, before, after, "nudge", NUDGE_MERGE))

    # ------------------ a card's colour ------------------
    @staticmethod
    def takes_colour(item) -> bool:
        return isinstance(item, (TableItem, ColumnItem, RefCardItem)) or (isinstance(item, NoteItem) and item.style != "plain")

    def selection_colour(self) -> Optional[str]:
        colours = {i.strip for i in self.selected() if self.takes_colour(i)}
        return colours.pop() if len(colours) == 1 else None

    def set_colour(self, color: Optional[str]) -> None:
        """Right-click > Colour: the selected cards' colour (a strip down a card's edge, a column's outline, a heading's
        or the header's line); None: the theme's. One undo step."""
        items = [i for i in self._unlocked(self.selected()) if self.takes_colour(i)]
        color = _clean_strip(color)
        self._change_selection("colour", lambda _item, state: replace(state, strip=color), items)

    def _colour_action(self, parent: QMenu) -> QWidgetAction:
        """The colours in a row (the first: none, the theme's), for the selection."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(30, 4, 12, 4)
        layout.setSpacing(4)
        current = self.selection_colour()
        enabled = any(self.takes_colour(i) for i in self.selected())
        for name, color in (("None (the theme's)", None),) + STRIP_COLORS:
            swatch = ColourSwatch(color, color == current or (color is None and current is None and enabled))
            swatch.setToolTip(name)
            swatch.clicked.connect(lambda _checked=False, c=color: (self.set_colour(c), parent.close()))
            layout.addWidget(swatch)
        row.setEnabled(enabled)
        action = QWidgetAction(parent)
        action.setDefaultWidget(row)
        return action

    def choose_colour(self) -> None:
        start = QColor(self.selection_colour() or theme_color("accent"))
        color = QColorDialog.getColor(start, self, "Card Colour")
        if color.isValid():
            self.set_colour(color.name())

    # ------------------ double-click empty space ------------------
    def show_quick_add(self, at: QPointF, pos: QPoint) -> None:
        """Double-click empty space: a row of what to put there (a note, a heading, a to-do...)."""
        from ui.artbook_quick_add import QuickAdd

        if self._quick_add is None:
            self._quick_add = QuickAdd(self)
        self._quick_add.show_at(at, pos)

    def hide_quick_add(self) -> None:
        if self._quick_add is not None and self._quick_add.isVisible():
            self._quick_add.hide()

    def add_link_here(self, at: Optional[QPointF] = None) -> None:
        """Link...: a web page's address, as a link card (or an image's, as the picture)."""
        url, ok = QInputDialog.getText(self, "Add a Link", "A web page's address (or an image's):")
        url = url.strip()
        if ok and url:
            if not _is_web_url(url):
                url = "https://" + url
            if _is_web_url(url):
                self.add_web_link(url, at)
            else:
                self.show_message("That isn't a web address.")

    # ------------------ snapping while dragging ------------------
    def _snap_move(self, free: bool) -> None:
        """Dragging: within SNAP_PX of lining up with another card (its edges or middle), or of an equal gap between
        neighbours, the selection snaps there; _guides keeps what to show. Alt (free): no snapping."""
        self._guides = None
        moving = [i for i in self.selected() if not isinstance(i, (LineItem, StrokeItem)) and i.isVisible()]
        if free or not moving or any(self.column_of(i) is not None for i in moving):
            self.viewport().update()
            return
        rect = QRectF()
        for item in moving:
            rect = rect.united(_seen_rect(item))
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        near = view.adjusted(-view.width(), -view.height(), view.width(), view.height())
        others = [_seen_rect(i) for i in self._items.values()
                  if i.isVisible() and not i.isSelected() and not isinstance(i, (LineItem, StrokeItem))
                  and self.column_of(i) is None and i.sceneBoundingRect().intersects(near)]
        reach = SNAP_PX / self.zoom()
        dx, gaps_x = _snap_axis(rect, others, reach, True)
        dy, gaps_y = _snap_axis(rect, others, reach, False)
        if dx or dy:
            for item in moving:
                item.setPos(item.pos() + QPointF(dx, dy))
            rect.translate(dx, dy)
        lines = _aligned_lines(rect, others, 0.5 / self.zoom())
        gaps = _gaps_shown(rect, others, gaps_x, True) + _gaps_shown(rect, others, gaps_y, False)
        self._guides = {"lines": lines, "gaps": gaps} if lines or gaps else None
        self.viewport().update()

    def _draw_guides(self, painter: QPainter) -> None:
        if not self._guides:
            return
        accent = theme_color("accent")
        pen = QPen(accent, 1.3, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        cross = 4 / self.zoom()
        for a, b in self._guides["lines"]:
            painter.setPen(pen)
            painter.drawLine(a, b)
            solid = QPen(accent, 1.5)
            solid.setCosmetic(True)
            painter.setPen(solid)
            for end in (a, b):
                painter.drawLine(end + QPointF(-cross, -cross), end + QPointF(cross, cross))
                painter.drawLine(end + QPointF(-cross, cross), end + QPointF(cross, -cross))
        font = QFont(QApplication.font())
        font.setPointSizeF(8)
        font.setWeight(QFont.Weight.DemiBold)
        metrics = QFontMetricsF(font)
        for a, b, value in self._guides["gaps"]:
            solid = QPen(accent, 1.2)
            solid.setCosmetic(True)
            painter.setPen(solid)
            painter.drawLine(a, b)
            mid = (a + b) / 2
            painter.save()
            painter.translate(mid)
            painter.scale(1 / self.zoom(), 1 / self.zoom())
            text = str(round(value))
            box = QRectF(0, 0, metrics.horizontalAdvance(text) + 12, metrics.height() + 4)
            box.moveCenter(QPointF(0, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(box, box.height() / 2, box.height() / 2)
            painter.setFont(font)
            painter.setPen(theme_color("on_accent"))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
            painter.restore()


    # ------------------ the selection frame ------------------
    def _frame_corners(self) -> Optional[list[QPointF]]:
        """Scene corners of the selection's frame: the item's own (rotated) outline for one image."""
        items = [item for item in self.selected() if not isinstance(item, (LineItem, PointItem))]  # (lines: handles of their own; points: their ring)
        if not items:
            return None
        if len(items) == 1:
            outline = items[0].boundingRect() if getattr(items[0], "cropping", False) else items[0].crop_rect()
            return list(items[0].mapToScene(outline))[:4]
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
        if corners is None or self._crop or self._editing or self._renaming or any(self.is_locked(item) for item in self.selected()):
            return None
        if not self._transformable():
            return None
        if self._turnable():
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
        self._draw_column_drop(painter)
        self._draw_line_overlays(painter)
        self._draw_find(painter)
        self._draw_guides(painter)
        self._draw_comments(painter)
        corners = self._frame_corners()
        if corners is None:
            return
        accent = theme_color("accent_border")
        pen = QPen(accent, 1.5)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for item in self.selected():
            if not isinstance(item, (LineItem, PointItem)):  # (a selected line or point glows)
                painter.drawPolygon(item.mapToScene(item.crop_rect() if isinstance(item, (ColumnItem, TableItem, RefCardItem)) else item.boundingRect()))
        if self._editing or self._renaming:
            return  # typing in a note: just its outline (the handles don't work meanwhile)
        painter.drawPolygon(QPolygonF(corners))
        if any(self.is_locked(item) for item in self.selected()) or not self._transformable():
            return  # a locked group (or a card in a column): its frame, but no handles
        size = HANDLE_PX / self.zoom()
        painter.setBrush(QBrush(QColor("#ffffff")))
        for handle in self._frame_handles(corners):
            painter.drawRect(QRectF(handle.x() - size / 2, handle.y() - size / 2, size, size))
        if self._turnable():
            handle = self._rotate_handle(corners)
            top_mid = (corners[0] + corners[1]) / 2.0
            painter.drawLine(top_mid, handle)
            painter.drawEllipse(handle, size * 0.6, size * 0.6)

    def _draw_column_drop(self, painter: QPainter) -> None:
        """Cards dragged over a column: an accent line where they'd go, over everything (the card itself too)."""
        target = self._column_drop
        if target is None or target[0].drop_y is None:
            return
        column = target[0]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme_color("accent"), 3.0 * column.scale(), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(column.mapToScene(QPointF(COLUMN_PAD, column.drop_y)),
                         column.mapToScene(QPointF(column.width - COLUMN_PAD, column.drop_y)))
        painter.restore()

    def _transformable(self) -> bool:
        """False for a card in a column on its own: the column sizes it."""
        items = self.selected()
        return not (len(items) == 1 and self.column_of(items[0]) is not None)

    def _turnable(self) -> bool:
        """One item selected that turns (a column or a line doesn't)."""
        items = self.selected()
        return len(items) == 1 and not isinstance(items[0], (ColumnItem, LineItem, TableItem, RefCardItem, PointItem))

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
        self.hide_quick_add()
        self.finish_moves()  # (a glide ends where it's going before anything is grabbed)
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
        if button == Qt.MouseButton.LeftButton and not self._draw_on and not self._crop and not self._space_down:
            found = self._comment_at(pos)
            if found is not None:  # (artbook.comment_open) a badge, a pin, or a hovered item's faint bubble
                self.finish_note_edit()
                self.finish_renaming()
                if isinstance(found, tuple):
                    self.open_comments(found[1])
                elif found["item"] is None:  # (a pin: a click opens it, a drag moves it: artbook.comment_pin_drag)
                    self._drag = {"mode": "comment_pin", "thread": found["id"], "press": pos,
                                  "start": QPointF(found["x"], found["y"]), "moved": False}
                else:
                    self.open_comments(found)
                event.accept()
                return
        if self._comment_thread is not None and self._comment_thread.isVisible():
            self.close_comments()  # (a click elsewhere: done reading)
        if self._renaming:
            if button == Qt.MouseButton.LeftButton and self._inline_text_item_at(pos) is self._renaming["text"]:
                super().mousePressEvent(event)  # placing the text cursor, selecting text
                return
            self.finish_renaming()
        if button == Qt.MouseButton.LeftButton and not self._draw_on and not self._crop and not self._space_down:
            handle = self._line_handle_at(pos)
            if handle is not None:  # (artbook.line_edit)
                self._begin_line_handle(handle, pos)
                event.accept()
                return
            dot = self._dot_at(pos)
            if dot is not None or self._line_tool:  # (artbook.line_dots, artbook.line)
                self._begin_line(dot[0] if dot is not None else self._line_target_at(pos), pos)
                event.accept()
                return
            control = self._column_control_at(pos)
            if control is not None:
                self.finish_note_edit()
                column, what = control
                if what == "close":  # (artbook.column_delete)
                    self.ask_delete_column(column, event.globalPosition().toPoint())
                else:  # (artbook.column_fold)
                    self.toggle_fold(column)
                event.accept()
                return
            card_control = self._card_control_at(pos)
            if card_control is not None:  # a hovered card's buttons (artbook.card_buttons)
                self.finish_note_edit()
                self._click_card_control(*card_control, event.globalPosition().toPoint())
                event.accept()
                return
            control = self._table_control_at(pos)
            if control is not None:
                self.finish_note_edit()
                table, what, value = control
                if what == "plus_row":  # (artbook.table_add)
                    self.add_table_row(table)
                elif what == "plus_column":
                    self.add_table_column(table)
                elif what == "grip":  # (artbook.table_row_drag)
                    self._drag = {"mode": "table_row", "table": table, "row": value}
                    self.viewport().setCursor(Qt.CursorShape.SizeVerCursor)
                elif what == "edge":  # (artbook.table_column_width)
                    self._drag = {"mode": "table_column", "table": table, "column": value, "before": self._snapshot([table]),
                                  "width": table.columns[value][2], "press": table.mapFromScene(self.mapToScene(pos.toPoint())).x()}
                    table.edge_hot = value
                else:  # a status chip (artbook.table_status)
                    self.ask_table_status(table, *value, event.globalPosition().toPoint())
                event.accept()
                return
            note = self._image_at(pos)
            if isinstance(note, NoteItem) and not self.is_locked(note):
                local = note.mapFromScene(self.mapToScene(pos.toPoint()))
                row = note.checkbox_at(local)
                if row is not None:  # (artbook.todo_tick)
                    self.toggle_check(note, row)
                    event.accept()
                    return
                row = note.grip_at(local)
                if row is not None:  # (artbook.todo_row_drag)
                    if self._editing and self._editing["note"] is not note:
                        self.finish_note_edit()
                    self._drag = {"mode": "row", "note": note, "row": row, "target": row}
                    self.viewport().setCursor(Qt.CursorShape.SizeVerCursor)
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
        if button == Qt.MouseButton.LeftButton and self._draw_on:
            self._begin_stroke(pos)  # (artbook.draw_drag)
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
                selected = not item.isSelected()
                self._expanding = True  # (a group comes and goes as one)
                try:
                    for member in self.group_members(item.group) or [item]:
                        member.setSelected(selected)
                finally:
                    self._expanding = False
                self.viewport().update()
                event.accept()
                return
            group = self._group_at(self.mapToScene(pos.toPoint())) if item is None else None
            if group is not None:  # artbook.group_click: a group's own panel picks up the whole group
                members = self.group_members(group)
                self._expanding = True
                try:
                    if modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
                        selected = not members[0].isSelected()
                        for member in members:
                            member.setSelected(selected)
                    else:
                        if not members[0].isSelected():
                            self._scene.clearSelection()
                        for member in members:
                            member.setSelected(True)
                finally:
                    self._expanding = False
                if not self.group_locked(group) and not modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
                    self._drag = {
                        "mode": "group_move", "last": self.mapToScene(pos.toPoint()),
                        "before": self._snapshot(self._unlocked(self.selected())),
                    }
                self.viewport().update()
                event.accept()
                return
            if item is not None and modifiers & Qt.KeyboardModifier.AltModifier and item.group:
                self._single_pick = True  # artbook.group_pick: just this one (until the button is let go)
                self._scene.clearSelection()
                item.setSelected(True)
            cell_click = None
            if isinstance(item, TableItem) and not self.is_locked(item):
                cell = item.cell_at(item.mapFromScene(self.mapToScene(pos.toPoint())))
                if cell is not None:
                    if item.isSelected() and self.selected() == [item]:
                        cell_click = (item, cell, QPointF(item.pos()))  # (artbook.table_cell: let go without moving it)
                    item.current = cell  # (the bar's Row and Column work on it)
                    item.update()
            super().mousePressEvent(event)
            self._drag = {"mode": "move", "before": self._snapshot(self.selected())} if item is not None else None
            if self._drag is not None and cell_click is not None:
                self._drag["cell"] = cell_click
            return
        super().mousePressEvent(event)

    def _begin_transform(self, handle: tuple[str, int], pos: QPointF) -> None:
        kind, index = handle
        items = self.selected()
        corners = self._frame_corners()
        drag = {"mode": kind, "before": self._snapshot(items), "items": items, "press": self.mapToScene(QPointF(pos).toPoint())}
        if kind == "scale" and index in (5, 7) and len(items) == 1 and isinstance(items[0], (NoteItem, ColumnItem, TableItem, RefCardItem)):
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
        if self._stroke is not None:
            self._extend_stroke(pos)
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
        if drag and drag["mode"] == "row":
            self._drag_row(drag, pos)
            return
        if drag and drag["mode"] == "line_draw":
            self._drag_line(drag, pos)
            return
        if drag and drag["mode"] == "line_handle":
            self._drag_line_handle(drag, pos)
            return
        if drag and drag["mode"] == "table_row":
            self._drag_table_row(drag, pos)
            return
        if drag and drag["mode"] == "comment_pin":
            if (pos - drag["press"]).manhattanLength() > 3 or drag["moved"]:
                drag["moved"] = True
                thread = self._thread(drag["thread"])
                if thread is not None:
                    point = drag["start"] + (self.mapToScene(pos.toPoint()) - self.mapToScene(drag["press"].toPoint()))
                    thread["x"], thread["y"] = point.x(), point.y()
                    self.viewport().update()
            return
        if drag and drag["mode"] == "table_column":
            self._drag_table_column(drag, pos)
            return
        if drag and drag["mode"] == "group_move":
            point = self.mapToScene(pos.toPoint())
            delta, drag["last"] = point - drag["last"], point
            for item_id in drag["before"]:
                item = self._items.get(item_id)
                if item is not None:
                    item.setPos(item.pos() + delta)
            self.lay_out_columns()
            self.viewport().update()
            return
        if drag and drag["mode"] == "rotate":
            snap = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._drag_rotate(drag, self.mapToScene(pos.toPoint()), snap)
            return
        super().mouseMoveEvent(event)
        if drag and drag["mode"] == "move" and event.buttons() & Qt.MouseButton.LeftButton:
            self._snap_move(bool(event.modifiers() & Qt.KeyboardModifier.AltModifier))
            self._track_column_drop(pos)
            self.lay_out_lines()  # (lines follow the cards being dragged)
        if not event.buttons():
            self._hover_rows(pos)
            self._hover_columns(pos)
            self._hover_tables(pos)
            self._hover_cards(pos)
            self._hover_comments(pos)
            self._hover_dots(pos)
            self._hover_line_spot(pos)
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
            note.setTextWidth(max(MIN_NOTE_WIDTH, start_width - local.x()))
            note.setPos(drag["start"].map(QPointF(start_width - note.textWidth(), 0)))
        self.lay_out_columns()  # (a column's cards take its new width; lines follow)
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
        if self._draw_on:
            return QCursor(Qt.CursorShape.CrossCursor)
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
        if self._line_tool:
            return QCursor(Qt.CursorShape.CrossCursor)
        line_handle = self._line_handle_at(pos)
        if line_handle is not None:
            return QCursor(Qt.CursorShape.SizeAllCursor if line_handle == "bend" else Qt.CursorShape.CrossCursor)
        if self._dot_at(pos) is not None:
            return QCursor(Qt.CursorShape.CrossCursor)
        if self._column_control_at(pos) is not None:
            return QCursor(Qt.CursorShape.PointingHandCursor)
        if self._card_control_at(pos) is not None or self._comment_at(pos) is not None:
            return QCursor(Qt.CursorShape.PointingHandCursor)
        control = self._table_control_at(pos)
        if control is not None:
            return QCursor({"grip": Qt.CursorShape.SizeVerCursor, "edge": Qt.CursorShape.SplitHCursor}.get(
                control[1], Qt.CursorShape.PointingHandCursor))
        item = self._image_at(pos)
        if isinstance(item, TableItem) and item.isSelected() and not self.is_locked(item):
            cell = item.cell_at(item.mapFromScene(self.mapToScene(pos.toPoint())))
            if cell is not None and (cell[0] < 0 or item.columns[cell[1]][1] != "status"):
                return QCursor(Qt.CursorShape.IBeamCursor)
        if isinstance(item, LineItem):
            return QCursor(Qt.CursorShape.PointingHandCursor)
        if isinstance(item, NoteItem) and not self.is_locked(item):
            local = item.mapFromScene(self.mapToScene(pos.toPoint()))
            if item.checkbox_at(local) is not None:
                return QCursor(Qt.CursorShape.PointingHandCursor)
            if item.grip_at(local) is not None:
                return QCursor(Qt.CursorShape.SizeVerCursor)
        if item is not None and not self.is_locked(item):
            return QCursor(Qt.CursorShape.SizeAllCursor)
        return QCursor(Qt.CursorShape.ArrowCursor)

    def _update_hover_cursor(self, pos: QPointF) -> None:
        self.viewport().setCursor(self.hover_cursor(pos))

    def mouseReleaseEvent(self, event) -> None:
        if self._stroke is not None and event.button() == Qt.MouseButton.LeftButton:
            self._finish_stroke()
            return
        drag, self._drag = self._drag, None
        self._single_pick = False
        if drag and drag["mode"] == "pan":
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self._space_down else Qt.CursorShape.ArrowCursor)
            return
        if drag and drag["mode"] in ("scale", "rotate", "note_width"):
            text = drag["mode"]
            if text == "note_width":
                text = ("column width" if isinstance(drag["note"], ColumnItem)
                        else "table width" if isinstance(drag["note"], TableItem)
                        else "card width" if isinstance(drag["note"], RefCardItem) else "note width")
            self._push_states(drag["before"], text)
            return
        if drag and drag["mode"] == "group_move":
            self._push_states(drag["before"], "move")
            return
        if drag and drag["mode"] == "crop":
            return  # applied with Enter (or C, or clicking elsewhere)
        if drag and drag["mode"] == "line_draw":
            self._drag_line(drag, event.position())
            self._finish_line(drag)
            return
        if drag and drag["mode"] == "line_handle":
            self._push_states(drag["before"], "bend line" if drag["which"] == "bend" else "reconnect line")
            self.viewport().update()
            return
        if drag and drag["mode"] == "row":
            self._drag_row(drag, event.position())  # (where it's let go: back on its own line, nothing moves)
            self._drop_row(drag)
            self._update_hover_cursor(event.position())
            return
        if drag and drag["mode"] == "comment_pin":
            thread = self._thread(drag["thread"])
            if thread is not None and drag["moved"]:
                self.move_pin(drag["thread"], QPointF(thread["x"], thread["y"]), drag["start"])
            elif thread is not None:
                self.open_comments(thread)
            return
        if drag and drag["mode"] == "table_row":
            self._drag_table_row(drag, event.position())
            self._drop_table_row(drag)
            self._update_hover_cursor(event.position())
            return
        if drag and drag["mode"] == "table_column":
            drag["table"].edge_hot = None
            drag["table"].update()
            self._push_states(drag["before"], "column width")
            return
        super().mouseReleaseEvent(event)
        if self._guides is not None:
            self._guides = None
            self.viewport().update()
        if drag and drag["mode"] == "move" and not self._finish_card_move(drag):
            self._push_states(drag["before"], "move")
        self._show_column_drop(None, [])
        if drag and drag.get("cell") is not None:
            table, cell, was = drag["cell"]
            if table.pos() == was and table.scene() is self._scene:
                self.edit_table_cell(table, cell, select_all=False)  # (a click on a selected table's cell: type in it)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._picking:
            event.accept()
            return
        item = self._image_at(event.position())
        if isinstance(item, LineItem) and event.button() == Qt.MouseButton.LeftButton:
            if self._renaming and self._renaming["item"] is item:
                super().mouseDoubleClickEvent(event)  # selects a word
                return
            at = self.mapToScene(event.position().toPoint())
            if item.has_pill() and item.pill_rect().contains(at):
                self.edit_line_label(item)  # (artbook.line_label)
            else:
                self.add_line_point(item, at)  # (artbook.line_point)
            return
        if isinstance(item, PointItem) and event.button() == Qt.MouseButton.LeftButton:
            event.accept()  # (nothing to open: not zoomed to, like a card would be)
            return
        if isinstance(item, ColumnItem) and event.button() == Qt.MouseButton.LeftButton:
            local = item.mapFromScene(self.mapToScene(event.position().toPoint()))
            if local.y() < item.header_height():  # (artbook.column_rename)
                if not (self._renaming and self._renaming["item"] is item):
                    self.edit_column_title(item)
                else:
                    super().mouseDoubleClickEvent(event)  # selects a word
                return
        if isinstance(item, RefCardItem) and event.button() == Qt.MouseButton.LeftButton:  # (artbook.card_open)
            self.open_card(item)
            return
        if isinstance(item, TableItem) and event.button() == Qt.MouseButton.LeftButton:
            renaming = self._renaming
            if renaming and renaming["item"] is item and self._inline_text_item_at(event.position()) is renaming["text"]:
                super().mouseDoubleClickEvent(event)  # selects a word
                return
            local = item.mapFromScene(self.mapToScene(event.position().toPoint()))
            if item.title_rect().contains(local):  # (artbook.table_cell)
                self.edit_table_title(item)
                return
            cell = item.cell_at(local)
            if cell is not None:
                if cell[0] >= 0 and item.columns[cell[1]][1] == "status":
                    self.ask_table_status(item, *cell, event.globalPosition().toPoint())
                else:
                    self.edit_table_cell(item, cell, select_all=False)
                return
        if isinstance(item, NoteItem) and event.button() == Qt.MouseButton.LeftButton:
            if self._renaming and self._renaming["item"] is item:
                super().mouseDoubleClickEvent(event)  # selects a word
                return
            local = item.mapFromScene(self.mapToScene(event.position().toPoint()))
            if item.style == "title" and item.about_rect().contains(local) and self.editing_note() is not item:
                self.edit_page_about(item)  # (artbook.page_title)
            elif self.editing_note() is item:
                super().mouseDoubleClickEvent(event)  # selects a word
            else:
                self.edit_note(item)
            return
        if item is not None and event.button() == Qt.MouseButton.LeftButton:
            self.frame([item])
            return
        if item is None and event.button() == Qt.MouseButton.LeftButton and not self._editing and not self._renaming:
            self.show_quick_add(self.mapToScene(event.position().toPoint()), event.position().toPoint())  # (artbook.quick_add)
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
        reset = self._reset_entries()
        selection = [
            ("Crop", "artbook.crop", self.start_crop, selected == 1 and len(images) == 1),
            ("Grayscale", "artbook.gray", self.toggle_gray, bool(images), bool(images) and all(item.gray for item in images)),
            ("Values", self._values_menu(images)),
            ("Guides", self._guides_menu(images)),
            ("Opacity", [self._opacity_action(menu, has_selection)]),
            ("Pixelated", None, self.toggle_pixelated, bool(images), bool(images) and all(item.pixelated for item in images)),
            ("Caption...", None, self.edit_caption, bool(images)),
            None,
            ("Group", "artbook.group", self.group_selection, selected >= 2),
            ("Ungroup", "artbook.ungroup", self.ungroup_selection, any(item.group for item in items)),
            ("Group Colour", self._group_color_menu(bool(self.selected_groups()))),
            ("Lock Group", None, self.toggle_group_lock, bool(self.selected_groups()),
             bool(self.selected_groups()) and all(self.group_locked(g) for g in self.selected_groups())),
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
        align = self._align_entries()
        match_size = self._match_size_entries()
        canvas = [
            ("Frame Selection" if has_selection else "Frame Everything", "artbook.fit", self.frame, bool(self._items)),
            ("Zoom to 100%", "artbook.zoom_100", lambda: self.animate_view(1.0, self.view_center()), True),
            ("Grayscale Board", "artbook.gray_board", self.toggle_board_gray, True, self._scene.gray_all),
            ("Values Board", "artbook.values_board", self.toggle_board_values, True, bool(self._scene.values_all)),
            ("Values Board Tones", self._board_tones_menu()),
            ("Mirror Board", "artbook.mirror_board", self.toggle_mirror, True, self.mirrored),
            ("Navigation Hints", None, self.toggle_nav_hints, True, self.nav_hints_shown),
            None,
            ("Export", [
                ("Export as PNG...", "artbook.export_png", self.request_export_image, bool(self._items)),
                ("Export as JPG...", None, lambda: self.request_export("jpg"), bool(self._items)),
                ("Export as PDF...", None, lambda: self.request_export("pdf"), bool(self._items)),
                ("Copy as Image", "artbook.copy_image", lambda: self.request_export("copy"), bool(self._items)),
                None,
                ("Export Options...", "artbook.export_options", lambda: self.request_export("options"), bool(self._items)),
            ]),
        ]
        files = self._file_entries()
        images_menu = [
            ("Select All", "artbook.select_all", self.select_all, bool(self._items)),
            ("Image List", "artbook.image_list", self.toggle_image_list, True, self.image_list_open()),
            ("Selection", selection),
            ("Upscale...", None, self.upscale_selection, bool(self.upscale_candidates())),
            ("Remove Background...", None, lambda: self.upscale_selection(cutout=True), bool(self.upscale_candidates())),
            ("Revert to Original", None, self.revert_upscales, any(item.original and not self.is_locked(item) for item in images)),
            ("Arrange", arrange),
            ("Align", align),
            ("Match Size", match_size),
            ("File", files),
        ]
        entries = [
            ("Add Here", self.add_here_entries(at)),
            None,
        ]
        column = self.column_at(at)
        if column is not None:
            entries[1:1] = [("Column", self.column_entries(column))]
        card = next((i for i in self._scene.items(at) if isinstance(i, BOARD_ITEMS)), None)
        if isinstance(card, RefCardItem) and card.isVisible():
            entries[1:1] = [("Card", self.card_entries(card))]
        table = self.table_at(at)
        if table is not None:
            cell = table.cell_at(table.mapFromScene(at))
            if cell is not None:
                table.current = cell
            entries[1:1] = [("Table", self.table_entries(table))]
        under = next((i for i in self._scene.items(at) if isinstance(i, BOARD_ITEMS) and not isinstance(i, (LineItem, StrokeItem, PointItem))), None)
        entries += [
            ("Comment", "artbook.comment", lambda: self.open_comments(under, at), True),
            ("Card Colour", [
                self._colour_action(menu), None,
                ("Custom...", None, self.choose_colour, any(self.takes_colour(i) for i in items)),
            ]),
            None,
            ("Undo", "artbook.undo", self.undo_stack.undo, self.undo_stack.canUndo()),
            ("Redo", "artbook.redo", self.undo_stack.redo, self.undo_stack.canRedo()),
            None,
            ("Duplicate", "artbook.duplicate", self.duplicate_selection, has_selection),
            ("Copy", [
                ("Selection", "artbook.copy", self.copy_selection, has_selection),
                ("File Path", None, self.copy_file_paths, any(item.linked_path() for item in images)),
            ]),
            ("Paste", "artbook.paste", lambda: self.paste(at), self.can_paste()),
            ("Move to Page", self.send_menu_entries(True)),
            ("Copy to Page", self.send_menu_entries(False)),
            ("Colour Picker", "artbook.picker", self.start_picker, True),
            None,
            ("Canvas", canvas),
            ("Images", images_menu),
        ]
        if self.page_menu is not None:
            entries.append(("Pages", self.page_menu()))
        self._fill_menu(menu, entries)
        if self.window_menu is not None:
            menu.addSeparator()
            menu.addMenu(self.window_menu(menu))
        return menu

    # ------------------ the right-click menu's lists (the image bar's dropdowns show them too) ------------------
    def _reset_entries(self) -> list:
        items, images = self.selected(), self.selected_images()
        states = [item.state() for item in items]
        return [
            ("All", "artbook.reset", self.reset_transform, bool(items)),
            None,
            ("Rotation", None, self.reset_rotation, any(s.rotation % 360 for s in states)),
            ("Size", None, self.reset_size, any(s.scale != 1.0 for s in states)),
            ("Flips", None, self.reset_flips, any(item.state().flip_h or item.state().flip_v for item in images)),
            ("Crop", None, self.reset_crop, any(item.crop for item in images)),
        ]

    def _align_entries(self) -> list:
        selected = len(self.selected())
        return [
            ("Left", None, lambda: self.align("left"), selected >= 2),
            ("Centers", None, lambda: self.align("center"), selected >= 2),
            ("Right", None, lambda: self.align("right"), selected >= 2),
            ("Top", None, lambda: self.align("top"), selected >= 2),
            ("Middles", None, lambda: self.align("middle"), selected >= 2),
            ("Bottom", None, lambda: self.align("bottom"), selected >= 2),
            None,
            ("Distribute Horizontally", None, lambda: self.distribute(True), selected >= 3),
            ("Distribute Vertically", None, lambda: self.distribute(False), selected >= 3),
            None,
            ("Stack", None, self.stack, selected >= 2),
        ]

    def _match_size_entries(self) -> list:
        selected = len(self.selected())
        return [
            ("Same Height", None, lambda: self.same_size(True), selected >= 2),
            ("Same Width", None, lambda: self.same_size(False), selected >= 2),
        ]

    def _file_entries(self) -> list:
        selected, images = len(self.selected()), self.selected_images()
        has_files = bool(self.selected_files())
        return [
            ("Show in Explorer", None, self.show_in_explorer, has_files),
            ("Open Source File", None, self.open_source_files, has_files),
            None,
            ("Replace Image...", None, self.replace_image, selected == 1 and len(images) == 1 and not self.is_locked(images[0])),
            ("Relink Missing Images...", None, self.relink_missing, bool(self.missing_images())),
        ]

    def image_adjust_entries(self, menu: QMenu) -> list:
        """The image bar's Adjust: how the selected images look."""
        images = self.selected_images()
        return [
            ("Grayscale", "artbook.gray", self.toggle_gray, bool(images), bool(images) and all(item.gray for item in images)),
            ("Pixelated", None, self.toggle_pixelated, bool(images), bool(images) and all(item.pixelated for item in images)),
            None,
            ("Values", self._values_menu(images)),
            ("Guides", self._guides_menu(images)),
            ("Opacity", [self._opacity_action(menu, bool(self.selected()))]),
        ]

    def image_arrange_entries(self, _menu: QMenu) -> list:
        """The image bar's Arrange: placing two or more images."""
        return [
            ("Auto-Arrange", "artbook.arrange", self.auto_arrange, len(self.arrange_scope()) >= 2),
            None,
            *self._align_entries(),
            None,
            *self._match_size_entries(),
        ]

    def image_more_entries(self, _menu: QMenu) -> list:
        """The image bar's More: the rest of what the right-click menu does to the selection."""
        items, images = self.selected(), self.selected_images()
        return [
            ("Reset", self._reset_entries()),
            None,
            ("Bring to Front", "artbook.front", lambda: self.restack(True), bool(items)),
            ("Send to Back", "artbook.back", lambda: self.restack(False), bool(items)),
            None,
            ("Caption...", None, self.edit_caption, bool(images)),
            ("Revert to Original", None, self.revert_upscales, any(item.original and not self.is_locked(item) for item in images)),
            ("File", self._file_entries()),
            ("Copy File Path", None, self.copy_file_paths, any(item.linked_path() for item in images)),
            None,
            ("Export Selection as PNG...", None, lambda: self.request_export("png", "selection"), bool(items)),
            None,
            ("Delete", "artbook.delete", self.delete_selection, bool(items)),
        ]

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

    def add_here_entries(self, at: QPointF) -> list:
        """The right-click menu's Add Here: the rail's tools, placed where you clicked."""
        entries: list = []
        for tool in TOOLS:
            if tool is None:
                entries.append(("Page Title", None, lambda: self.add_page_title(at), self.title_note() is None))
                entries.append(("Page Link", self.page_link_entries(at)))
                entries.append(None)
            elif tool.checkable:
                on = self._draw_on if tool.tool_id == "draw" else self._line_tool
                entries.append((tool.name, tool.shortcut_id, lambda t=tool.tool_id: self.add_tool(t, at), True, on))
            else:
                name = tool.name if tool.draggable else f"{tool.name}..."
                entries.append((name, tool.shortcut_id, lambda t=tool.tool_id: self.add_tool(t, at), True))
        return entries

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
        if event.type() == QEvent.Type.ShortcutOverride and (self._editing or self._renaming or self._key_id(event)):
            event.accept()
            return True
        if (event.type() == QEvent.Type.KeyPress and self._renaming is not None and self._renaming.get("cell") is not None
                and any(shortcuts.matches(key, event) for key in self.TABLE_TYPING_KEYS)):
            self.keyPressEvent(event)  # (the view would hand Tab to the scene, to move focus between items)
            return True
        return super().event(event)

    # Only while typing in a note (Ctrl+B is the Pages sidebar otherwise)
    TYPING_KEYS = {
        "artbook.note_bold": "toggle_note_bold",
        "artbook.note_italic": "toggle_note_italic",
        "artbook.note_underline": "toggle_note_underline",
        "artbook.note_paste_plain": "paste_plain_into_note",
    }

    # Only while typing in a table's cell (the board takes every key then): Tab and Shift+Tab move between cells
    TABLE_TYPING_KEYS = ("artbook.table_next", "artbook.table_prev")

    def paste_into_note(self, plain: bool = False) -> None:
        """Ctrl+V while typing: the clipboard's text at the cursor, with its formatting but no
        paragraph backgrounds (a web page's would stay behind the text); plain: the text alone, in
        the note's own style."""
        note = self.editing_note()
        mime = QApplication.clipboard().mimeData()
        if note is None or mime is None:
            return
        cursor = note.textCursor()
        if not plain and mime.hasHtml():
            cursor.insertFragment(pasted_html(mime.html()))
        elif mime.hasText():
            cursor.insertText(mime.text().replace("\r\n", "\n"))
        else:
            return
        note.setTextCursor(cursor)
        self._sync_note_bar()

    def paste_plain_into_note(self) -> None:
        self.paste_into_note(plain=True)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        self.finish_moves()
        key = self._key_id(event)
        if self._renaming:  # typing a column's title (a line's label, a table's cell): Enter or Esc when done
            in_cell = self._renaming.get("cell") is not None
            enter = event.text() in ("\r", "\n")
            if in_cell and shortcuts.matches("artbook.table_next", event):
                self.next_table_cell(1)
            elif in_cell and shortcuts.matches("artbook.table_prev", event):
                self.next_table_cell(-1)
            elif in_cell and enter and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)  # a new line in the cell
            elif in_cell and enter:
                self.table_cell_below()
            elif key == "artbook.deselect" or enter:
                self.finish_renaming()
            else:
                super().keyPressEvent(event)  # to the title
            event.accept()
            return
        if self._editing:
            typing_key = next((k for k in self.TYPING_KEYS if shortcuts.matches(k, event)), None)
            if key == "artbook.deselect" or (self._editing["note"].style == "title" and event.text() in ("\r", "\n")):
                self.finish_note_edit()  # Esc (or Enter in the page's header): done typing
            elif typing_key is not None:
                getattr(self, self.TYPING_KEYS[typing_key])()
            elif key == "artbook.paste":
                self.paste_into_note()
            else:
                super().keyPressEvent(event)  # to the note
                self._sync_note_bar()
            event.accept()
            return
        if self._picking and key in ("artbook.deselect", "artbook.picker"):
            self.stop_picker()  # Esc, or I again
            event.accept()
            return
        if self._line_tool and key in ("artbook.deselect", "artbook.line"):
            self.stop_line_tool()  # Esc, or Shift+L again
            event.accept()
            return
        if self._draw_on and key in ("artbook.deselect", "artbook.draw"):
            self.stop_draw()  # Esc, or D again
            event.accept()
            return
        if self._draw_on and key == "artbook.eraser":
            self.toggle_eraser()
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
            "artbook.crop_apply": self._enter_on_selection,
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
            "artbook.deselect": lambda: (self.hide_quick_add(), self._scene.clearSelection()),
            "artbook.undo": self.undo_stack.undo,
            "artbook.redo": self.undo_stack.redo,
            "artbook.arrange": self.auto_arrange,
            "artbook.crop": self.start_crop,
            "artbook.gray": self.toggle_gray,
            "artbook.note": self.add_note,
            "artbook.heading": lambda: self.add_note(style="heading"),
            "artbook.todo": self.add_todo,
            "artbook.column": self.add_column,
            "artbook.table": self.add_table,
            "artbook.comment": self.comment_on_selection,
            "artbook.tray": self.toggle_tray,
            "artbook.line": self.toggle_line_tool,
            "artbook.picker": self.start_picker,
            "artbook.gray_board": self.toggle_board_gray,
            "artbook.values_board": self.toggle_board_values,
            "artbook.mirror_board": self.toggle_mirror,
            "artbook.export_png": self.request_export_image,
            "artbook.export_options": lambda: self.request_export("options"),
            "artbook.duplicate": self.duplicate_selection,
            "artbook.nudge_left": lambda: self.nudge(-NUDGE_PX, 0),
            "artbook.nudge_right": lambda: self.nudge(NUDGE_PX, 0),
            "artbook.nudge_up": lambda: self.nudge(0, -NUDGE_PX),
            "artbook.nudge_down": lambda: self.nudge(0, NUDGE_PX),
            "artbook.nudge_left_big": lambda: self.nudge(-NUDGE_BIG_PX, 0),
            "artbook.nudge_right_big": lambda: self.nudge(NUDGE_BIG_PX, 0),
            "artbook.nudge_up_big": lambda: self.nudge(0, -NUDGE_BIG_PX),
            "artbook.nudge_down_big": lambda: self.nudge(0, NUDGE_BIG_PX),
            "artbook.copy_image": lambda: self.request_export("copy"),
            "artbook.group": self.group_selection,
            "artbook.ungroup": self.ungroup_selection,
            "artbook.image_list": self.toggle_image_list,
            "artbook.draw": self.toggle_draw,
            "artbook.find": self.open_find,
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
