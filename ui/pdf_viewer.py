"""The file viewer's PDF view: the pages drawn by the app (so they stay sharp at any zoom, and
notes can sit on them), with a toolbar and a sidebar.

- PdfPageView (a QGraphicsView) lays every page out top to bottom. Pages are rendered off the UI
  thread (QPdfPageRenderer) at the detail they're seen at, nearest the view first; pages well out
  of view let their picture go. Links on a page are followed with a click (a page of the PDF, or
  a web address in the browser). Two tools: Hand (drag pans) and Select Text (drag selects, across
  pages too; Ctrl+C copies). Search (PdfSearch) reads each page's text once and finds matches
  itself (Qt's own search model takes most of a minute on a long book); where a match sits on its
  page is only worked out when that page is drawn.
- PdfViewer adds the toolbar (page box, zoom, fit width / page) and the sidebar: the PDF's
  contents (its bookmarks) and a thumbnail of every page.
- Your marks (ui.pdf_marks: highlights, pen drawings, notes) sit on the pages, with tools to make
  them, an eraser, picking (Hand tool: drag to move, Delete) and undo. A Notes tab lists them.
- Where you were (page, zoom, sidebar) and your marks are kept per PDF by core.pdf_notes, never in
  the PDF.
- A page can be sent to the Artbook as a picture (with or without your marks), and a copy of the
  pages with your marks drawn in can be exported (one PDF, or PNGs). The PDF itself is only read.

Controls are registered in ui.shortcuts (the PDF Viewer category). The file viewer's own keys
(Page Up / Down and Alt+Left / Right for the previous / next file, Esc) pass through.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import (
    QAbstractListModel, QEvent, QMarginsF, QModelIndex, QObject, QPoint, QPointF, QRect, QRectF, QSize, QSizeF, Qt,
    QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QImage, QIntValidator, QPageSize, QPainter, QPdfWriter, QPen, QPixmap, QPolygonF,
    QTransform, QUndoStack,
)
from PySide6.QtPdf import QPdfBookmarkModel, QPdfDocument, QPdfDocumentRenderOptions, QPdfLinkModel, QPdfPageRenderer
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QMessageBox,
    QProgressDialog,
    QRadioButton,
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionGraphicsItem,
    QStyleOptionViewItem,
    QToolTip,
    QTreeView,
    QVBoxLayout,
    QApplication,
    QWidget,
)

from core import pdf_notes
from core.settings import get_last_export_dir, set_last_export_dir
from ui import shortcuts
from ui.artbook_board import smooth_points
from ui.artbook_note_bar import swatch_icon
from ui.color_popup import ColorPopup
from ui.duotone import set_icon
from ui.pdf_marks import MARK_TYPES, InkMark, Mark, MarksCommand, NoteMark, from_dict, new_id
from ui.themes import theme_color

POINT_PX = 96.0 / 72.0  # screen pixels per PDF point at 100%
PAGE_GAP = 14.0  # points between pages
MARGIN = 24.0  # points above the first page and below the last (none beside: the view centres them)
ZOOM_RANGE = (0.1, 8.0)
ZOOM_STEP = 1.25  # buttons and keys
WHEEL_ZOOM_STEP = 1.15  # per wheel notch, with Ctrl
ZOOM_PRESETS = (50, 75, 100, 125, 150, 200, 300, 400)
FIT_GUTTER_PX = 24  # room left beside the page when fitting it
MAX_RENDER_SIDE = 6000  # pixels: a page is never rendered bigger (deeper zooms show it scaled up)
KEEP_SCREENS = 2.0  # rendered pages this many screens beyond the view are let go
RENDERS_AT_ONCE = 3
PREVIEW_SCALE = 0.5  # pixels per point of a quick first picture of a page (the sharp one follows)
RENDER_TICK_MS = 30
THUMB_WIDTH = 110
SAVE_DELAY_MS = 800
DRAG_PX = 4  # a press that moves less than this is a click
MISSING_PAGE = QSizeF(612.0, 792.0)  # (Letter: for a page whose size can't be read)
TOOLS = ("hand", "select", "highlight", "pen", "note", "eraser")
TOOL_COLORS = {  # (the first is where each starts)
    "highlight": ("#ffd400", "#7ee081", "#7cc4ff", "#ff9ccb", "#ffb070"),
    "pen": ("#e5484d", "#1f6feb", "#1a7f37", "#1f2328", "#ffffff", "#ffd400"),
    "note": ("#fff3a3", "#c8f7c5", "#cfe3ff", "#ffd6e7", "#ffd8b0", "#ffffff"),
}
PEN_SIZES = (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0)  # points (a mark on the paper: it zooms with the page)
PEN_FOLLOW = 0.5  # the line moves this far toward the pointer at each step (steadies a mouse)
PEN_STEP_PX = 1.5  # a new point every this many pixels the pointer moves
ERASER_PX = 7  # the eraser's reach around the pointer
SEND_SCALE = 3.0  # pixels per point of a page sent to the Artbook (216 dpi)...
SEND_MAX_SIDE = 4096  # ...at most this big
EXPORT_DPI = {"standard": 150, "high": 300}
MESSAGE_MS = 3500
SEARCH_PAGES_PER_STEP = 25  # pages searched between UI updates (the UI stays responsive on long books)
SEARCH_DELAY_MS = 250  # after the last key typed in the find box
MATCH_COLOR = QColor(255, 212, 0, 120)  # like a highlighter pen (drawn multiplied over the page)
CURRENT_MATCH_COLOR = QColor(255, 140, 0, 200)


def line_boxes(polygons: list[QPolygonF]) -> list[QRectF]:
    """Glyph outlines (in reading order) merged into one box per line of text."""
    lines: list[QRectF] = []
    for polygon in polygons:
        box = polygon.boundingRect()
        if box.isEmpty():
            continue
        if lines:
            last = lines[-1]
            overlap = min(box.bottom(), last.bottom()) - max(box.top(), last.top())
            if overlap > 0.4 * min(box.height(), last.height()) and box.left() >= last.left() - 2:
                lines[-1] = last.united(box)
                continue
        lines.append(QRectF(box))
    return lines


class PdfSearch(QObject):
    """Finds text in a PDF, a few pages at a time (progress fires as matches come in). Each page's
    text is read once. matches: (page, first character, length), in reading order. Words match
    across line breaks, whatever their case."""

    progress = Signal()

    def __init__(self, document: QPdfDocument, parent=None):
        super().__init__(parent)
        self.document = document
        self.query = ""
        self.matches: list[tuple[int, int, int]] = []
        self.done = True
        self._texts: dict[int, str] = {}
        self._pattern: Optional[re.Pattern] = None
        self._next_page = 0
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._step)

    def text(self, page: int) -> str:
        if page not in self._texts:
            self._texts[page] = self.document.getAllText(page).text()
        return self._texts[page]

    def start(self, query: str) -> None:
        words = query.split()
        self.query = " ".join(words)
        self.matches = []
        self._next_page = 0
        if not words:
            self._pattern = None
            self.done = True
            self._timer.stop()
            self.progress.emit()
            return
        self._pattern = re.compile(r"\s+".join(re.escape(word) for word in words), re.IGNORECASE)
        self.done = False
        self._timer.start()

    def _step(self) -> None:
        if self._pattern is None:
            self._timer.stop()
            return
        count = self.document.pageCount()
        last = min(count, self._next_page + SEARCH_PAGES_PER_STEP)
        for page in range(self._next_page, last):
            for found in self._pattern.finditer(self.text(page)):
                self.matches.append((page, found.start(), found.end() - found.start()))
        self._next_page = last
        if last >= count:
            self.done = True
            self._timer.stop()
        self.progress.emit()


def _render_options() -> QPdfDocumentRenderOptions:
    options = QPdfDocumentRenderOptions()
    options.setRenderFlags(QPdfDocumentRenderOptions.RenderFlag.Annotations)  # (the PDF's own, e.g. form fields)
    return options


class PageItem(QGraphicsItem):
    """One page, in points (its top-left corner at its pos). Paper until its picture is in."""

    def __init__(self, index: int, size: QSizeF):
        super().__init__()
        self.index = index
        self.size = size
        self.image: Optional[QImage] = None
        self.image_scale = 0.0  # pixels per point of the picture it has
        self.requested = 0.0  # pixels per point of the picture being rendered (0: none)
        self.marks = None  # (set by the view) marks(index) -> (search matches as (boxes, current), selected boxes)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.size.width(), self.size.height())

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        rect = self.boundingRect()
        painter.fillRect(rect, QColor("#ffffff"))
        if self.image is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(rect, self.image)
        if self.marks is not None:
            matches, selected = self.marks(self.index)
            if matches or selected:
                painter.save()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)  # (the ink shows through)
                for boxes, current in matches:
                    painter.setBrush(CURRENT_MATCH_COLOR if current else MATCH_COLOR)
                    for box in boxes:
                        painter.drawRect(box.adjusted(-1, -1, 1, 1))
                if selected:
                    tint = QColor(theme_color("accent"))
                    tint.setAlpha(110)
                    painter.setBrush(tint)
                    for box in selected:
                        painter.drawRect(box)
                painter.restore()
        edge = QPen(QColor(0, 0, 0, 70), 1.0)
        edge.setCosmetic(True)
        painter.setPen(edge)
        painter.drawRect(rect)


class PdfPageView(QGraphicsView):
    """Every page of a PDF, top to bottom (see the module docstring)."""

    page_changed = Signal(int)
    zoom_changed = Signal(float)
    view_changed = Signal()  # scrolled or zoomed (for remembering where you were)
    mode_changed = Signal(str)  # "hand" / "select"
    selection_changed = Signal()
    matches_changed = Signal()  # found more, or the current match moved
    marks_changed = Signal()  # a mark added, changed or removed (for saving and the Notes list)
    undo_changed = Signal()

    def __init__(self, document: QPdfDocument, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("pdfView")
        self.document = document
        self._render_timer = QTimer(self)  # (first: making the view scrolls it)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(RENDER_TICK_MS)
        self._render_timer.timeout.connect(self.update_renders)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)  # (links show a hand when hovered)
        self.renderer = QPdfPageRenderer(self)
        self.renderer.setRenderMode(QPdfPageRenderer.RenderMode.MultiThreaded)
        self.renderer.setDocument(document)
        self.renderer.pageRendered.connect(self._on_rendered)
        self._requests: dict[int, tuple[int, float]] = {}  # render request id -> (page, pixels per point)
        self._links: dict[int, list[tuple[QRectF, int, QPointF, QUrl]]] = {}
        self._link_model = QPdfLinkModel(self)
        self._link_model.setDocument(document)
        self._fit: Optional[str] = "width"  # "width" / "page": refitted when the view's size changes
        self._press: Optional[dict[str, Any]] = None
        self._current = -1
        self.mode = "hand"
        self._space = False  # Space held: drag pans, whatever tool is on
        self._selecting: Optional[dict[str, Any]] = None  # a text selection being dragged
        self.selection: dict[int, tuple[str, list[QRectF]]] = {}  # page -> (its selected text, boxes)
        self._glyphs: dict[int, list[QRectF]] = {}  # page -> each letter's box (for selecting)
        self.search = PdfSearch(document, self)
        self.search.progress.connect(self._on_search_progress)
        self.current_match = -1
        self._page_matches: dict[int, list[int]] = {}  # page -> its matches (indexes into search.matches)
        self._match_boxes: dict[int, list[QRectF]] = {}  # match index -> its boxes on its page
        self._seen_matches = 0
        self._jump_from: Optional[int] = None  # a new search: go to the first match from this page on
        self.undo_stack = QUndoStack(self)
        self.undo_stack.indexChanged.connect(self._on_undo_changed)
        self._marks: dict[str, tuple[int, Mark]] = {}  # mark id -> (page, its item)
        self.chosen: Optional[Mark] = None  # picked with the Hand tool
        self.colors = {tool: colors[0] for tool, colors in TOOL_COLORS.items()}
        self.pen_width = 2.0
        self.marks_shown = True
        self._drawing: Optional[dict[str, Any]] = None  # a pen line being drawn
        self._erasing: Optional[list[Mark]] = None  # marks rubbed out so far (hidden until the drag ends)
        self._erase_last = QPoint()
        self._moving: Optional[dict[str, Any]] = None  # a mark being dragged
        self._editing: Optional[dict[str, Any]] = None  # typing in a note: {"note", "before" (None: new)}
        self.page_menu = None  # (set by PdfViewer) page_menu(menu, page): more entries for the right-click menu
        self.pages: list[PageItem] = []
        self.layout_pages()
        self.set_zoom(1.0)

    # ------------------ layout ------------------
    def layout_pages(self) -> None:
        scene = self.scene()
        scene.clear()
        self.pages = []
        y = 0.0
        widest = 0.0
        for index in range(self.document.pageCount()):
            size = self.document.pagePointSize(index)
            if not (size.width() > 0 and size.height() > 0):
                size = QSizeF(MISSING_PAGE)
            item = PageItem(index, size)
            item.marks = self.marks
            item.setPos(-size.width() / 2.0, y)
            scene.addItem(item)
            self.pages.append(item)
            y += size.height() + PAGE_GAP
            widest = max(widest, size.width())
        height = max(0.0, y - PAGE_GAP)
        self.setSceneRect(QRectF(-widest / 2.0, -MARGIN, widest, height + 2 * MARGIN))

    def page_rect(self, index: int) -> QRectF:
        item = self.pages[index]
        return QRectF(item.pos(), item.size)

    def page_at(self, scene_point: QPointF) -> Optional[PageItem]:
        for item in self.pages:
            if QRectF(item.pos(), item.size).contains(scene_point):
                return item
        return None

    # ------------------ zoom ------------------
    def zoom(self) -> float:
        return self.transform().m11() / POINT_PX

    def fit_mode(self) -> Optional[str]:
        return self._fit

    def set_zoom(self, zoom: float, anchor: Optional[QPointF] = None, keep_fit: bool = False) -> None:
        """Zoom (1.0: actual size), keeping the point under anchor (a viewport position; default:
        the middle of the view) where it is."""
        if not keep_fit:
            self._fit = None
        zoom = max(ZOOM_RANGE[0], min(ZOOM_RANGE[1], zoom))
        anchor = anchor if anchor is not None else QPointF(self.viewport().rect().center())
        fixed = self.mapToScene(anchor.toPoint())
        self.setTransform(QTransform.fromScale(zoom * POINT_PX, zoom * POINT_PX))
        moved = self.mapFromScene(fixed) - anchor.toPoint()
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + moved.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + moved.y())
        self.zoom_changed.emit(zoom)
        self.view_changed.emit()
        self._schedule_renders()

    def zoom_by(self, factor: float, anchor: Optional[QPointF] = None) -> None:
        self.set_zoom(self.zoom() * factor, anchor)

    def fit(self, mode: str) -> None:
        """Fit the current page's width ("width") or all of it ("page") in the view, and keep it so."""
        if not self.pages:
            return
        page = self.pages[max(0, self.current_page())]
        top = self.mapToScene(QPoint(0, 0)).y()
        offset = top - page.pos().y()  # (stay at the same place on the page)
        width = max(1, self.viewport().width() - 2 * FIT_GUTTER_PX) / (page.size.width() * POINT_PX)
        zoom = width
        if mode == "page":
            height = max(1, self.viewport().height() - 2 * FIT_GUTTER_PX) / (page.size.height() * POINT_PX)
            zoom = min(width, height)
            offset = -FIT_GUTTER_PX / (zoom * POINT_PX)
        self._fit = mode
        self.set_zoom(zoom, keep_fit=True)
        self.show_page(page.index, offset)
        if mode == "page":
            self.centerOn(QPointF(0.0, self.mapToScene(self.viewport().rect().center()).y()))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit is not None and self.pages:
            page = self.current_page()
            self.fit(self._fit)
            self.show_page(page, self.page_offset() if self._fit == "width" else None)
        self._schedule_renders()

    # ------------------ pages ------------------
    def current_page(self) -> int:
        """The page in the top third of the view."""
        if not self.pages:
            return -1
        point = self.mapToScene(QPoint(self.viewport().width() // 2, self.viewport().height() // 3))
        best, best_distance = 0, math.inf
        for item in self.pages:
            top, bottom = item.pos().y(), item.pos().y() + item.size.height()
            distance = 0.0 if top <= point.y() <= bottom else min(abs(point.y() - top), abs(point.y() - bottom))
            if distance < best_distance:
                best, best_distance = item.index, distance
        return best

    def page_offset(self) -> float:
        """How far down the current page the view's top is, in points."""
        index = self.current_page()
        if index < 0:
            return 0.0
        return self.mapToScene(QPoint(0, 0)).y() - self.pages[index].pos().y()

    def show_page(self, index: int, offset: Optional[float] = None) -> None:
        """Scroll so page index is at the top (offset: points down the page; default just above it)."""
        if not self.pages:
            return
        index = max(0, min(len(self.pages) - 1, index))
        target = self.pages[index].pos().y() + (offset if offset is not None else -PAGE_GAP / 2.0)
        top = self.mapToScene(QPoint(0, 0)).y()
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + round((target - top) * self.transform().m22()))
        self._after_scroll()

    def step_page(self, step: int) -> None:
        self.show_page(self.current_page() + step)

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        self._after_scroll()

    def _after_scroll(self) -> None:
        page = self.current_page()
        if page != self._current:
            self._current = page
            self.page_changed.emit(page)
        self.view_changed.emit()
        self._schedule_renders()

    # ------------------ rendering ------------------
    def _schedule_renders(self) -> None:
        if not self._render_timer.isActive():
            self._render_timer.start()

    def wanted_scale(self, item: PageItem) -> float:
        """Pixels per point to render a page at for how it's seen now: half steps of powers of two
        (zooming doesn't render at every step), at most MAX_RENDER_SIDE."""
        want = max(0.05, self.zoom() * POINT_PX * self.devicePixelRatioF())
        scale = 2.0 ** (math.ceil(math.log2(want) * 2.0) / 2.0)
        return min(scale, MAX_RENDER_SIDE / max(item.size.width(), item.size.height()))

    def update_renders(self) -> None:
        if not self.pages:
            return
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        keep = view.adjusted(0, -view.height() * KEEP_SCREENS, 0, view.height() * KEEP_SCREENS)
        near = view.adjusted(0, -view.height(), 0, view.height())  # (the next page down is ready when it's reached)
        middle = view.center().y()
        wanted: list[tuple[float, PageItem]] = []
        for item in self.pages:
            box = QRectF(item.pos(), item.size)
            if not box.intersects(keep):
                if item.image is not None:
                    item.image, item.image_scale = None, 0.0
                    item.update()
                continue
            if not box.intersects(near):
                continue
            scale = self.wanted_scale(item)
            if item.image is None and not item.requested and scale > PREVIEW_SCALE * 1.5:
                scale = PREVIEW_SCALE  # (something to see straight away: a big page takes a while)
            if abs(item.image_scale - scale) > scale * 0.01 and abs(item.requested - scale) > scale * 0.01:
                in_view = box.intersects(view)
                wanted.append((0.0 if in_view else 1.0, abs(box.center().y() - middle), item, scale))
        wanted.sort(key=lambda entry: (entry[0], entry[1]))
        for _in_view, _distance, item, scale in wanted:
            if len(self._requests) >= RENDERS_AT_ONCE:
                break
            size = QSize(max(1, math.ceil(item.size.width() * scale)), max(1, math.ceil(item.size.height() * scale)))
            request = self.renderer.requestPage(item.index, size, _render_options())
            self._requests[request] = (item.index, scale)
            item.requested = scale

    def _on_rendered(self, page: int, _size: QSize, image: QImage, _options, request: int) -> None:
        index, scale = self._requests.pop(request, (page, 0.0))
        if 0 <= index < len(self.pages):
            item = self.pages[index]
            if abs(item.requested - scale) <= scale * 0.01:
                item.requested = 0.0
            if not image.isNull():
                item.image, item.image_scale = image, scale
                item.update()
        self._schedule_renders()

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, theme_color("grid_bg"))

    # ------------------ marks: search matches and the selection ------------------
    def marks(self, page: int) -> tuple[list[tuple[list[QRectF], bool]], list[QRectF]]:
        matches = [(self.match_boxes(index), index == self.current_match) for index in self._page_matches.get(page, [])]
        selected = self.selection.get(page)
        return matches, (selected[1] if selected else [])

    def match_boxes(self, index: int) -> list[QRectF]:
        """Where a match sits on its page (worked out the first time it's drawn or visited)."""
        if index not in self._match_boxes:
            page, start, length = self.search.matches[index]
            self._match_boxes[index] = line_boxes(self.document.getSelectionAtIndex(page, start, length).bounds())
        return self._match_boxes[index]

    def _repaint_page(self, page: int) -> None:
        if 0 <= page < len(self.pages):
            self.pages[page].update()

    # ------------------ search ------------------
    def find(self, query: str) -> None:
        """Search for query (empty: stop searching); the first match from the current page on is shown."""
        pages = set(self._page_matches)
        self._page_matches, self._match_boxes, self._seen_matches = {}, {}, 0
        self.current_match = -1
        self._jump_from = self.current_page() if query.strip() else None
        self.search.start(query)
        for page in pages:
            self._repaint_page(page)

    def _on_search_progress(self) -> None:
        matches = self.search.matches
        for index in range(self._seen_matches, len(matches)):
            page = matches[index][0]
            self._page_matches.setdefault(page, []).append(index)
            self._repaint_page(page)
        self._seen_matches = len(matches)
        if self._jump_from is not None:
            ahead = next((i for i, m in enumerate(matches) if m[0] >= self._jump_from), None)
            if ahead is not None:
                self._jump_from = None
                self.go_to_match(ahead)
            elif self.search.done:
                self._jump_from = None
                if matches:
                    self.go_to_match(0)  # (none after this page: back to the first)
        self.matches_changed.emit()

    def go_to_match(self, index: int) -> None:
        if not self.search.matches:
            return
        old = self.current_match
        self.current_match = index % len(self.search.matches)
        page = self.search.matches[self.current_match][0]
        boxes = self.match_boxes(self.current_match)
        if boxes:
            area = QRectF(boxes[0])
            for box in boxes[1:]:
                area = area.united(box)
            area.translate(self.pages[page].pos())
            shown = self.mapToScene(self.viewport().rect()).boundingRect()
            inner = shown.adjusted(0, shown.height() * 0.1, 0, -shown.height() * 0.2)
            if not inner.contains(area):  # (off screen, or near an edge: a third of the way down)
                bar = self.verticalScrollBar()
                bar.setValue(bar.value() + round((area.top() - shown.top() - shown.height() / 3.0) * self.transform().m22()))
            self.ensureVisible(area, 60, 0)  # (sideways, when zoomed in)
        else:
            self.show_page(page)
        if 0 <= old < len(self.search.matches):
            self._repaint_page(self.search.matches[old][0])
        self._repaint_page(page)
        self._after_scroll()
        self.matches_changed.emit()

    def step_match(self, step: int) -> None:
        """The next (1) / previous (-1) match; from the current page if none is current yet."""
        matches = self.search.matches
        if not matches:
            return
        if self.current_match < 0:
            here = self.current_page()
            ahead = [i for i, m in enumerate(matches) if m[0] >= here]
            self.go_to_match(ahead[0] if ahead and step > 0 else (ahead[0] - 1 if ahead else -1))
            return
        self.go_to_match(self.current_match + step)

    # ------------------ your marks ------------------
    def load_marks(self, saved: Any) -> None:
        """The marks kept for this PDF (no undo step)."""
        for data in saved if isinstance(saved, list) else []:
            self._add_mark(data)
        self.marks_changed.emit()

    def marks_data(self) -> list[dict[str, Any]]:
        """Every mark as a dict (what's kept), in page order."""
        entries = sorted(self._marks.values(), key=lambda e: (e[0], e[1].mapRectToParent(e[1].boundingRect()).top()))
        return [item.to_dict(page) for page, item in entries]  # (page by page, top to bottom)

    def mark_items(self) -> list[tuple[int, Mark]]:
        return list(self._marks.values())

    def _add_mark(self, data: dict[str, Any]) -> Optional[Mark]:
        if not isinstance(data, dict) or not isinstance(data.get("page"), int) or not 0 <= data["page"] < len(self.pages):
            return None
        item = from_dict(data)
        if item is None:
            return None
        self._remove_mark(item.mark_id)
        item.setParentItem(self.pages[data["page"]])
        item.setVisible(self.marks_shown)
        if isinstance(item, NoteMark):
            item.on_edit_finished = self._on_note_focus_lost
        self._marks[item.mark_id] = (data["page"], item)
        return item

    def _remove_mark(self, mark_id: str) -> None:
        entry = self._marks.pop(mark_id, None)
        if entry is None:
            return
        item = entry[1]
        if self.chosen is item:
            self.chosen = None
        if self._editing is not None and self._editing["note"] is item:
            self._editing = None
        if item.scene() is not None:
            item.scene().removeItem(item)

    def _apply_marks(self, remove_ids: list[str], add: list[dict[str, Any]]) -> None:
        for mark_id in remove_ids:
            self._remove_mark(mark_id)
        for data in add:
            self._add_mark(data)
        self.marks_changed.emit()

    def change_marks(self, before: list[dict[str, Any]], after: list[dict[str, Any]], text: str) -> None:
        """One undo step: the marks before (their dicts) become the ones after."""
        if before or after:
            self.undo_stack.push(MarksCommand(self._apply_marks, before, after, text))

    def mark_dict(self, item: Mark) -> dict[str, Any]:
        return item.to_dict(self._marks[item.mark_id][0])

    def _on_undo_changed(self, _index: int) -> None:
        self.undo_changed.emit()

    def undo(self) -> None:
        self.finish_note_edit()
        self.undo_stack.undo()

    def redo(self) -> None:
        self.finish_note_edit()
        self.undo_stack.redo()

    def mark_at(self, pos: QPoint) -> Optional[Mark]:
        for item in self.items(pos):
            if isinstance(item, MARK_TYPES) and item.isVisible():
                return item
        return None

    def choose(self, item: Optional[Mark]) -> None:
        """Pick a mark with the Hand tool (to move or delete it), or none."""
        if self.chosen is item:
            return
        if self.chosen is not None:
            self.chosen.chosen = False
            self.chosen.update()
        self.chosen = item
        if item is not None:
            item.chosen = True
            item.update()

    def delete_chosen(self) -> bool:
        if self.chosen is None:
            return False
        self.change_marks([self.mark_dict(self.chosen)], [], "delete mark")
        return True

    def set_marks_shown(self, shown: bool) -> None:
        """Show or hide every mark (hidden: the tools that make marks are off)."""
        self.finish_note_edit()
        self.marks_shown = shown
        for _page, item in self._marks.values():
            item.setVisible(shown)
        if not shown:
            self.choose(None)
            if self.mode in ("highlight", "pen", "note", "eraser"):
                self.set_mode("hand")

    def show_mark(self, mark_id: str) -> None:
        """Scroll to a mark (a third of the way down) and pick it."""
        entry = self._marks.get(mark_id)
        if entry is None:
            return
        page, item = entry
        area = item.mapRectToScene(item.boundingRect())
        shown = self.mapToScene(self.viewport().rect()).boundingRect()
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + round((area.top() - shown.top() - shown.height() / 3.0) * self.transform().m22()))
        self.ensureVisible(area, 60, 0)
        self._after_scroll()
        if not self.marks_shown:
            self.set_marks_shown(True)
        self.choose(item)

    def page_marks(self, page: int) -> list[Mark]:
        return [item for at, item in self._marks.values() if at == page]

    def page_picture(self, page: int, scale: float, marks: bool = True) -> QImage:
        """A page as a picture, scale pixels per point (on white), with your marks drawn in if marks."""
        item = self.pages[page]
        size = QSize(max(1, round(item.size.width() * scale)), max(1, round(item.size.height() * scale)))
        picture = QImage(size, QImage.Format.Format_RGB32)  # (opaque: exported PDFs keep it as a JPEG)
        picture.fill(QColor("#ffffff"))
        painter = QPainter(picture)
        painter.drawImage(0, 0, self.document.render(page, size, _render_options()))
        if marks:
            painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
            painter.scale(size.width() / item.size.width(), size.height() / item.size.height())
            order = {"highlight": 0, "ink": 1, "note": 2}
            for mark in sorted(self.page_marks(page), key=lambda m: order[m.kind]):
                chosen, mark.chosen = mark.chosen, False  # (not the picked look)
                painter.save()
                painter.translate(mark.pos())
                option = QStyleOptionGraphicsItem()
                option.exposedRect = mark.boundingRect()
                mark.paint(painter, option, None)
                painter.restore()
                mark.chosen = chosen
        painter.end()
        return picture

    def highlight_selection(self) -> bool:
        """Highlight the selected text (one mark per page); the selection goes."""
        if not self.selection:
            return False
        added = []
        for page in sorted(self.selection):
            text, boxes = self.selection[page]
            added.append({
                "id": new_id(), "kind": "highlight", "page": page, "color": self.colors["highlight"],
                "text": text.replace("\r\n", " ").strip()[:2000],
                "boxes": [[b.x(), b.y(), b.width(), b.height()] for b in boxes],
            })
        self.clear_selection()
        self.change_marks([], added, "highlight")
        return True

    # ------------------ pen ------------------
    def _begin_ink(self, pos: QPoint) -> None:
        scene = self.mapToScene(pos)
        page = self.page_at(scene)
        if page is None:
            return
        local = scene - page.pos()
        item = InkMark(new_id(), [local], self.colors["pen"], self.pen_width)
        item.setParentItem(page)  # (drawn on the page while it's made; added for good when it's done)
        self._drawing = {"page": page, "item": item, "last": pos, "end": local}

    def _extend_ink(self, pos: QPoint) -> None:
        drawing = self._drawing
        page = drawing["page"]
        local = self.mapToScene(pos) - page.pos()
        local = QPointF(min(max(local.x(), 0.0), page.size.width()), min(max(local.y(), 0.0), page.size.height()))
        drawing["end"] = local
        if (pos - drawing["last"]).manhattanLength() < PEN_STEP_PX:
            return
        drawing["last"] = pos
        item = drawing["item"]
        steady = item.points[-1] + (local - item.points[-1]) * PEN_FOLLOW
        item.set_points(item.points + [steady])

    def _finish_ink(self) -> None:
        drawing, self._drawing = self._drawing, None
        item = drawing["item"]
        points = item.points + [drawing["end"]] if len(item.points) > 1 else item.points
        data = {
            "id": item.mark_id, "kind": "ink", "page": drawing["page"].index, "color": item.color, "width": item.width,
            "points": [[p.x(), p.y()] for p in smooth_points(points)],
        }
        if item.scene() is not None:
            item.scene().removeItem(item)
        self.change_marks([], [data], "draw")

    # ------------------ notes ------------------
    def add_note(self, pos: QPoint) -> Optional[NoteMark]:
        """A new note where the pointer is, to type in straight away (kept once it has text)."""
        scene = self.mapToScene(pos)
        page = self.page_at(scene)
        if page is None:
            return None
        local = scene - page.pos()
        note = self._add_mark({
            "id": new_id(), "kind": "note", "page": page.index, "color": self.colors["note"],
            "x": min(local.x(), max(0.0, page.size.width() - 40)), "y": local.y(), "text": "",
        })
        self.edit_note(note, new=True)
        return note

    def edit_note(self, note: NoteMark, new: bool = False) -> None:
        self.finish_note_edit()
        self.choose(None)
        self._editing = {"note": note, "before": None if new else self.mark_dict(note)}
        self.setFocus()
        note.start_editing()

    def editing_note(self) -> Optional[NoteMark]:
        return self._editing["note"] if self._editing is not None else None

    def _on_note_focus_lost(self, note: NoteMark) -> None:
        if self.editing_note() is note:
            self.finish_note_edit()

    def finish_note_edit(self) -> None:
        """Keep what was typed (one undo step). A note left empty goes."""
        editing, self._editing = self._editing, None
        if editing is None:
            return
        note, before = editing["note"], editing["before"]
        note.stop_editing()
        if note.mark_id not in self._marks:
            return
        after = self.mark_dict(note)
        empty = not after["text"].strip()
        if before is None:
            self._remove_mark(note.mark_id)  # (the command puts it back)
            if not empty:
                self.change_marks([], [after], "note")
            else:
                self.marks_changed.emit()
        elif empty:
            self.change_marks([before], [], "delete note")
        elif after != before:
            self.change_marks([before], [after], "edit note")

    # ------------------ eraser ------------------
    def _erase_along(self, pos: QPoint) -> None:
        """Along the whole way from the last point: a quick drag mustn't skip a mark."""
        last = self._erase_last
        steps = max(1, math.ceil(math.hypot(pos.x() - last.x(), pos.y() - last.y()) / ERASER_PX))
        for n in range(1, steps + 1):
            self._erase_at(last + (pos - last) * (n / steps))
        self._erase_last = pos

    def _erase_at(self, pos: QPoint) -> None:
        pos = QPoint(round(pos.x()), round(pos.y()))
        reach = ERASER_PX
        for item in self.items(pos.x() - reach, pos.y() - reach, 2 * reach, 2 * reach):
            if isinstance(item, MARK_TYPES) and item.isVisible() and item.mark_id in self._marks:
                item.setVisible(False)  # (gone for good when the drag ends: one undo step)
                self._erasing.append(item)

    def _finish_erasing(self) -> None:
        erased, self._erasing = self._erasing, None
        for item in erased:
            item.setVisible(True)
        self.change_marks([self.mark_dict(item) for item in erased], [], "erase")

    # ------------------ tools and selecting text ------------------
    def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.finish_note_edit()
            if mode in ("highlight", "pen", "note", "eraser") and not self.marks_shown:
                self.set_marks_shown(True)
            if mode != "hand":
                self.choose(None)
            self.mode = mode
            self._tool_cursor()
            self.mode_changed.emit(mode)

    def toggle_mode(self, mode: str) -> None:
        self.set_mode("hand" if self.mode == mode else mode)

    def glyph_boxes(self, page: int) -> list[QRectF]:
        """Boxes around the page's text, in reading order (a letter, a word or a whole line each,
        depending on the PDF)."""
        if page not in self._glyphs:
            self._glyphs[page] = [p.boundingRect() for p in self.document.getAllText(page).bounds()]
        return self._glyphs[page]

    def _nearest_glyph(self, page: int, point: QPointF) -> Optional[QPointF]:
        """point, moved into the nearest box of text (on its line, if it's level with one): where
        the letter nearest it is."""
        boxes = self.glyph_boxes(page)
        if not boxes:
            return None

        def distance(box: QRectF) -> float:
            if box.contains(point):
                return -1.0
            dy = 0.0 if box.top() <= point.y() <= box.bottom() else min(abs(point.y() - box.top()), abs(point.y() - box.bottom()))
            return dy * 4.0 + abs(box.center().x() - point.x())

        box = min(boxes, key=distance)
        return QPointF(min(max(point.x(), box.left() + 0.5), box.right() - 0.5), box.center().y())

    def select_between(self, first: tuple[int, QPointF], last: tuple[int, QPointF]) -> None:
        """Select the text from one point to another (each a page and a point on it, in points)."""
        if last[0] < first[0]:
            first, last = last, first
        pages = set(self.selection)
        self.selection = {}
        for page in range(first[0], last[0] + 1):
            boxes = self.glyph_boxes(page)
            if not boxes:
                continue
            start = self._nearest_glyph(page, first[1]) if page == first[0] else QPointF(boxes[0].left() + 0.5, boxes[0].center().y())
            end = self._nearest_glyph(page, last[1]) if page == last[0] else QPointF(boxes[-1].right() - 0.5, boxes[-1].center().y())
            chosen = self.document.getSelection(page, start, end)
            if chosen.isValid() and chosen.text():
                self.selection[page] = (chosen.text(), line_boxes(chosen.bounds()))
        for page in pages | set(self.selection):
            self._repaint_page(page)
        self.selection_changed.emit()

    def select_page_text(self, page: Optional[int] = None) -> None:
        page = self.current_page() if page is None else page
        if page < 0:
            return
        everything = self.document.getAllText(page)
        pages = set(self.selection)
        self.selection = {page: (everything.text(), line_boxes(everything.bounds()))} if everything.text() else {}
        for changed in pages | {page}:
            self._repaint_page(changed)
        self.selection_changed.emit()

    def clear_selection(self) -> None:
        if self.selection:
            pages, self.selection = set(self.selection), {}
            for page in pages:
                self._repaint_page(page)
            self.selection_changed.emit()

    def selected_text(self) -> str:
        return "\n".join(self.selection[page][0] for page in sorted(self.selection)).replace("\r\n", "\n").replace("\r", "\n")

    def copy_selection(self) -> bool:
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)
        return bool(text)

    def _page_point(self, pos: QPoint) -> Optional[tuple[int, QPointF]]:
        """(page, point on it) under a viewport position: the nearest page if it's between pages."""
        if not self.pages:
            return None
        scene = self.mapToScene(pos)
        item = self.page_at(scene)
        if item is None:
            item = min(self.pages, key=lambda p: min(abs(scene.y() - p.pos().y()), abs(scene.y() - p.pos().y() - p.size.height())))
        local = scene - item.pos()
        local = QPointF(min(max(local.x(), 0.0), item.size.width()), min(max(local.y(), 0.0), item.size.height()))
        return item.index, local

    def contextMenuEvent(self, event) -> None:
        self.finish_note_edit()
        menu = QMenu(self)
        mark = self.mark_at(event.pos())
        if mark is not None:
            self.choose(mark)
            if isinstance(mark, NoteMark):
                menu.addAction("Edit Note").triggered.connect(lambda: self.edit_note(mark))
            delete = menu.addAction("Delete " + {"highlight": "Highlight", "ink": "Drawing", "note": "Note"}[mark.kind])
            delete.setShortcut(shortcuts.sequence("pdf.delete_mark"))
            delete.triggered.connect(self.delete_chosen)
            menu.addSeparator()
        highlight = menu.addAction("Highlight Selection")
        highlight.setShortcut(shortcuts.sequence("pdf.highlight_tool"))
        highlight.setEnabled(bool(self.selection))
        highlight.triggered.connect(self.highlight_selection)
        copy = menu.addAction("Copy")
        copy.setShortcut(shortcuts.sequence("pdf.copy"))
        copy.setEnabled(bool(self.selection))
        copy.triggered.connect(self.copy_selection)
        page = self._page_point(event.pos())
        select = menu.addAction("Select Text on This Page")
        select.setShortcut(shortcuts.sequence("pdf.select_page"))
        select.triggered.connect(lambda: self.select_page_text(page[0] if page else None))
        if self.page_menu is not None and page is not None:
            menu.addSeparator()
            self.page_menu(menu, page[0])
        menu.exec(event.globalPos())
        menu.deleteLater()  # (its actions carry shortcuts: don't leave them around)

    # ------------------ links ------------------
    def page_links(self, index: int) -> list[tuple[QRectF, int, QPointF, QUrl]]:
        """(where on the page, page it goes to or -1, where on that page, web address) for each link."""
        if index not in self._links:
            self._link_model.setPage(index)
            links = []
            model = self._link_model
            for row in range(model.rowCount(QModelIndex())):
                at = model.index(row, 0)
                links.append((
                    model.data(at, QPdfLinkModel.Role.Rectangle.value), int(model.data(at, QPdfLinkModel.Role.Page.value)),
                    model.data(at, QPdfLinkModel.Role.Location.value), model.data(at, QPdfLinkModel.Role.Url.value),
                ))
            self._links[index] = links
        return self._links[index]

    def link_at(self, pos: QPoint) -> Optional[tuple[QRectF, int, QPointF, QUrl]]:
        scene = self.mapToScene(pos)
        item = self.page_at(scene)
        if item is None:
            return None
        local = scene - item.pos()
        for link in self.page_links(item.index):
            if link[0].contains(local):
                return link
        return None

    @staticmethod
    def link_text(link: tuple[QRectF, int, QPointF, QUrl]) -> str:
        if link[1] >= 0:
            return f"Go to page {link[1] + 1}"
        return link[3].toString()

    def follow(self, link: tuple[QRectF, int, QPointF, QUrl]) -> None:
        """A page of the PDF: show it there. A web address: open it in the browser."""
        _rect, page, location, url = link
        if page >= 0:
            self.show_page(page, location.y() - PAGE_GAP / 2.0 if location.y() > 0 else None)
            return
        text = url.toString().strip()
        if text.lower().startswith("www."):
            url = QUrl("http://" + text)
        if url.scheme().lower() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)

    # ------------------ mouse ------------------
    def mousePressEvent(self, event) -> None:
        editing = self.editing_note()
        if editing is not None:
            if event.button() == Qt.MouseButton.LeftButton and self.mark_at(event.position().toPoint()) is editing:
                super().mousePressEvent(event)  # (clicking in the note being typed in: its cursor moves)
                return
            self.finish_note_edit()
        left = event.button() == Qt.MouseButton.LeftButton and not self._space
        pos = event.position().toPoint()
        if left and self.mode == "pen":
            self._begin_ink(pos)
            event.accept()
            return
        if left and self.mode == "note":
            note = self.mark_at(pos)
            if isinstance(note, NoteMark):
                self.edit_note(note)
            else:
                self.add_note(pos)
            event.accept()
            return
        if left and self.mode == "eraser":
            self._erasing = []
            self._erase_last = pos
            self._erase_at(pos)
            event.accept()
            return
        if left and self.mode == "hand":
            item = self.mark_at(pos)
            self.choose(item)
            if isinstance(item, (InkMark, NoteMark)):
                self._moving = {"item": item, "start": self.mapToScene(pos), "origin": item.pos(), "before": self.mark_dict(item), "moved": False}
                event.accept()
                return
            if item is not None:
                event.accept()
                return
        if left and self.mode in ("select", "highlight"):
            pos = event.position().toPoint()
            anchor = self._page_point(pos)
            self._selecting = {"anchor": anchor, "pos": pos, "dragged": False}
            event.accept()
            return
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            self._press = {
                "pos": event.position().toPoint(), "h": self.horizontalScrollBar().value(),
                "v": self.verticalScrollBar().value(), "dragged": False, "button": event.button(),
            }
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        if self._drawing is not None:
            self._extend_ink(pos)
            return
        if self._erasing is not None:
            self._erase_along(pos)
            return
        moving = self._moving
        if moving is not None:
            delta = self.mapToScene(pos) - moving["start"]
            if moving["moved"] or abs(delta.x()) + abs(delta.y()) >= DRAG_PX / max(0.01, self.transform().m11()):
                moving["moved"] = True
                moving["item"].setPos(moving["origin"] + delta)
            return
        selecting = self._selecting
        if selecting is not None:
            if selecting["dragged"] or (pos - selecting["pos"]).manhattanLength() >= DRAG_PX:
                selecting["dragged"] = True
                end = self._page_point(pos)
                if selecting["anchor"] is not None and end is not None:
                    self.select_between(selecting["anchor"], end)
            return
        press = self._press
        if press is not None:
            delta = pos - press["pos"]
            if press["dragged"] or delta.manhattanLength() >= DRAG_PX:
                press["dragged"] = True
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                self.horizontalScrollBar().setValue(press["h"] - delta.x())
                self.verticalScrollBar().setValue(press["v"] - delta.y())
            return
        link = self.link_at(pos)
        if link is not None:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            QToolTip.showText(event.globalPosition().toPoint(), self.link_text(link), self.viewport())
        else:
            self._tool_cursor()
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def _tool_cursor(self) -> None:
        cursors = {
            "select": Qt.CursorShape.IBeamCursor, "highlight": Qt.CursorShape.IBeamCursor, "note": Qt.CursorShape.IBeamCursor,
            "pen": Qt.CursorShape.CrossCursor, "eraser": Qt.CursorShape.CrossCursor,
        }
        if self._space:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        elif self.mode in cursors:
            self.viewport().setCursor(cursors[self.mode])
        else:
            self.viewport().unsetCursor()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._drawing is not None:
                self._finish_ink()
                event.accept()
                return
            if self._erasing is not None:
                self._finish_erasing()
                event.accept()
                return
            moving, self._moving = self._moving, None
            if moving is not None:
                if moving["moved"]:
                    item = moving["item"]
                    after = self.mark_dict(item)
                    item.setPos(moving["origin"])
                    self.change_marks([moving["before"]], [after], "move mark")
                    self.choose(self._marks[after["id"]][1] if after["id"] in self._marks else None)
                event.accept()
                return
        selecting, self._selecting = self._selecting, None
        if selecting is not None and event.button() == Qt.MouseButton.LeftButton:
            if selecting["dragged"] and self.mode == "highlight":
                self.highlight_selection()
            elif not selecting["dragged"]:
                link = self.link_at(event.position().toPoint())
                if link is not None:
                    self.follow(link)  # (links work with either tool)
                else:
                    self.clear_selection()
            event.accept()
            return
        press, self._press = self._press, None
        if press is not None and event.button() == press["button"]:
            self._tool_cursor()
            if not press["dragged"] and press["button"] == Qt.MouseButton.LeftButton:
                link = self.link_at(event.position().toPoint())
                if link is not None:
                    self.follow(link)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        note = self.mark_at(event.position().toPoint())
        if isinstance(note, NoteMark) and event.button() == Qt.MouseButton.LeftButton and self.mode in ("hand", "note", "select"):
            self._moving = None
            self.edit_note(note)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            notches = event.angleDelta().y() / 120.0
            if notches:
                self.zoom_by(WHEEL_ZOOM_STEP ** notches, event.position())
            event.accept()
            return
        super().wheelEvent(event)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.NativeGesture and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            self.zoom_by(1.0 + event.value(), self.viewport().mapFromGlobal(event.globalPosition().toPoint()).toPointF())
            return True
        return super().event(event)

    # ------------------ keys ------------------
    def keyPressEvent(self, event) -> None:
        if self.editing_note() is not None:
            if shortcuts.matches("pdf.escape", event):
                self.finish_note_edit()
                event.accept()
                return
            super().keyPressEvent(event)  # (typing: the note gets every key)
            event.accept()
            return
        if shortcuts.matches("pdf.escape", event) and (self.chosen is not None or self.selection or self.mode != "hand"):
            if self.chosen is not None:
                self.choose(None)
            elif self.selection:
                self.clear_selection()
            else:
                self.set_mode("hand")
            event.accept()
            return
        if shortcuts.matches("pdf.highlight_tool", event):
            if not self.highlight_selection():
                self.toggle_mode("highlight")
            event.accept()
            return
        for shortcut_id, mode in (("pdf.pen_tool", "pen"), ("pdf.note_tool", "note"), ("pdf.eraser_tool", "eraser")):
            if shortcuts.matches(shortcut_id, event):
                self.toggle_mode(mode)
                event.accept()
                return
        if shortcuts.matches("pdf.undo", event):
            self.undo()
            event.accept()
            return
        if shortcuts.matches("pdf.redo", event) or shortcuts.matches("pdf.redo_alt", event):
            self.redo()
            event.accept()
            return
        if shortcuts.matches("pdf.delete_mark", event) and self.delete_chosen():
            event.accept()
            return
        for shortcut_id in ("viewer.previous", "viewer.next", "viewer.previous_alt", "viewer.next_alt", "viewer.exit_fullscreen"):
            if shortcuts.matches(shortcut_id, event):
                event.ignore()  # (the file viewer's: previous / next file, Esc)
                return
        if shortcuts.matches("pdf.space_pan", event):
            if not event.isAutoRepeat():
                self._space = True
                self._tool_cursor()
        elif shortcuts.matches("pdf.select_tool", event):
            self.toggle_mode("select")
        elif shortcuts.matches("pdf.copy", event):
            self.copy_selection()
        elif shortcuts.matches("pdf.select_page", event):
            self.select_page_text()
        elif shortcuts.matches("pdf.previous_page", event):
            self.step_page(-1)
        elif shortcuts.matches("pdf.next_page", event):
            self.step_page(1)
        elif shortcuts.matches("pdf.first_page", event):
            self.show_page(0)
        elif shortcuts.matches("pdf.last_page", event):
            self.show_page(len(self.pages) - 1)
        elif shortcuts.matches("pdf.zoom_in", event) or shortcuts.matches("pdf.zoom_in_alt", event):
            self.zoom_by(ZOOM_STEP)
        elif shortcuts.matches("pdf.zoom_out", event):
            self.zoom_by(1.0 / ZOOM_STEP)
        elif shortcuts.matches("pdf.actual_size", event):
            self.set_zoom(1.0)
        elif shortcuts.matches("pdf.fit_width", event):
            self.fit("width")
        elif shortcuts.matches("pdf.fit_page", event):
            self.fit("page")
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def keyReleaseEvent(self, event) -> None:
        if shortcuts.matches("pdf.space_pan", event) and not event.isAutoRepeat():
            self._space = False
            self._tool_cursor()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:
        self._space = False  # (a Space released elsewhere)
        super().focusOutEvent(event)


class ExportDialog(QDialog):
    """Export a Copy: which pages, as one PDF or PNG images, how sharp, and whether your marks are drawn in."""

    def __init__(self, parent: QWidget, marked: int, current: int, total: int):
        super().__init__(parent)
        self.setWindowTitle("Export a Copy")
        column = QVBoxLayout(self)
        column.setSpacing(10)
        intro = QLabel("Saves the pages as a new file, with your highlights, drawings and notes drawn in. The PDF itself isn't changed.")
        intro.setWordWrap(True)
        column.addWidget(intro)

        pages = QGroupBox("Pages")
        pages_column = QVBoxLayout(pages)
        self.page_choices: dict[str, QRadioButton] = {}
        for name, label, tip in (
            ("marked", f"Pages with marks ({marked})", "Only the pages you've highlighted, drawn or written on"),
            ("all", f"All pages ({total})", "Every page of the PDF"),
            ("current", f"This page ({current + 1})", "Just the page you're on"),
        ):
            choice = QRadioButton(label)
            choice.setToolTip(tip)
            pages_column.addWidget(choice)
            self.page_choices[name] = choice
        self.page_choices["marked"].setEnabled(marked > 0)
        self.page_choices["marked" if marked else "current"].setChecked(True)
        column.addWidget(pages)

        kind = QGroupBox("Save as")
        kind_column = QVBoxLayout(kind)
        self.as_pdf = QRadioButton("One PDF")
        self.as_pdf.setToolTip("All the pages in one PDF file (its pages are pictures: their text can't be selected)")
        self.as_png = QRadioButton("PNG images (one per page)")
        self.as_png.setToolTip("A picture of each page, in a folder you pick")
        self.as_pdf.setChecked(True)
        kind_column.addWidget(self.as_pdf)
        kind_column.addWidget(self.as_png)
        self.quality = QComboBox()
        self.quality.setToolTip("Standard (150 dpi) for reading on screen; High (300 dpi) for printing")
        self.quality.addItem("Standard (150 dpi)", "standard")
        self.quality.addItem("High (300 dpi)", "high")
        kind_column.addWidget(self.quality)
        column.addWidget(kind)

        self.with_marks = QCheckBox("Draw my highlights, drawings and notes")
        self.with_marks.setToolTip("Off: the pages as they are")
        self.with_marks.setChecked(True)
        column.addWidget(self.with_marks)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setToolTip("Close without exporting")
        cancel.clicked.connect(self.reject)
        export = QPushButton("Export...")
        export.setObjectName("primaryBtn")
        export.setToolTip("Pick where to save it, then export")
        export.setDefault(True)
        export.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(export)
        column.addLayout(buttons)

    def choice(self) -> dict[str, Any]:
        return {
            "pages": next(name for name, button in self.page_choices.items() if button.isChecked()),
            "format": "pdf" if self.as_pdf.isChecked() else "png",
            "dpi": EXPORT_DPI[self.quality.currentData()],
            "marks": self.with_marks.isChecked(),
        }


THUMB_SIZE_ROLE = Qt.ItemDataRole.UserRole  # a thumbnail's size (QSize), without rendering it


class ThumbnailModel(QAbstractListModel):
    """A small picture of every page (rendered as the list asks for them), named by its page label.
    sizes: every page's size in points (read once: asking the PDF again waits on the renderers)."""

    def __init__(self, document: QPdfDocument, sizes: list[QSizeF], ratio: float, parent=None):
        super().__init__(parent)
        self.document = document
        self.sizes = sizes
        self.ratio = ratio
        self.renderer = QPdfPageRenderer(self)
        self.renderer.setRenderMode(QPdfPageRenderer.RenderMode.MultiThreaded)
        self.renderer.setDocument(document)
        self.renderer.pageRendered.connect(self._on_rendered)
        self._pictures: dict[int, QPixmap] = {}
        self._asked: set[int] = set()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else self.document.pageCount()

    def thumb_size(self, page: int) -> QSize:
        size = self.sizes[page]
        return QSize(THUMB_WIDTH, max(1, round(THUMB_WIDTH * size.height() / size.width())))

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        page = index.row()
        if role == Qt.ItemDataRole.DisplayRole:
            return self.document.pageLabel(page) or str(page + 1)
        if role == Qt.ItemDataRole.DecorationRole:
            picture = self._pictures.get(page)
            if picture is None:
                if page not in self._asked:
                    self._asked.add(page)
                    size = self.thumb_size(page)
                    self.renderer.requestPage(page, QSize(round(size.width() * self.ratio), round(size.height() * self.ratio)), _render_options())
                picture = QPixmap(self.thumb_size(page))
                picture.fill(QColor("#ffffff"))
            return picture
        if role == THUMB_SIZE_ROLE:
            return self.thumb_size(page)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"Page {page + 1}"
        return None

    def _on_rendered(self, page: int, _size: QSize, image: QImage, _options, _request: int) -> None:
        if image.isNull():
            return
        paper = QImage(image.size(), QImage.Format.Format_RGB32)  # (pages render see-through where there's no ink)
        paper.fill(QColor("#ffffff"))
        painter = QPainter(paper)
        painter.drawImage(0, 0, image)
        painter.end()
        picture = QPixmap.fromImage(paper)
        picture.setDevicePixelRatio(self.ratio)
        self._pictures[page] = picture
        at = self.index(page, 0)
        self.dataChanged.emit(at, at, [Qt.ItemDataRole.DecorationRole])


THUMB_LABEL_PX = 22  # under each thumbnail: its page label
THUMB_PAD_PX = 6


class ThumbnailDelegate(QStyledItemDelegate):
    """A page's thumbnail, centred, with its label under it; the current page has a soft panel
    behind it (Qt's own icon list would tint the picture instead)."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        size = index.data(THUMB_SIZE_ROLE)  # (not the picture: that would render every page)
        height = size.height() if isinstance(size, QSize) else THUMB_WIDTH * 1.4
        return QSize(THUMB_WIDTH + 2 * THUMB_PAD_PX, round(height) + THUMB_LABEL_PX + 2 * THUMB_PAD_PX)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(4, 2, -4, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            panel = QColor(theme_color("accent"))
            panel.setAlphaF(0.30 if selected else 0.12)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(panel)
            painter.drawRoundedRect(rect, 6, 6)
        picture = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(picture, QPixmap):
            size = picture.deviceIndependentSize()
            box = QRectF(rect.center().x() - size.width() / 2, rect.top() + THUMB_PAD_PX, size.width(), size.height())
            painter.drawPixmap(box.toRect(), picture)
            edge = QPen(QColor(0, 0, 0, 80), 1)
            painter.setPen(edge)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(box)
        painter.setPen(theme_color("accent_text") if selected else theme_color("text_muted"))
        label = QRectF(rect.left(), rect.bottom() - THUMB_LABEL_PX, rect.width(), THUMB_LABEL_PX)
        painter.drawText(label, Qt.AlignmentFlag.AlignCenter, str(index.data(Qt.ItemDataRole.DisplayRole) or ""))
        painter.restore()


class PdfViewer(QWidget):
    """A PDF in the file viewer: toolbar, sidebar (Contents / Pages) and the pages."""

    def __init__(self, path: Path, parent: Optional[QWidget] = None, artbook=None):
        """artbook: the main window (fill_artbook_menu, send_image_to_artbook), or None: pages can't be
        sent to the Artbook."""
        super().__init__(parent)
        self.setObjectName("pdfViewer")
        self.path = Path(path)
        self.artbook = artbook
        self._export: Optional[dict[str, Any]] = None  # an export under way
        self.doc = QPdfDocument(self)
        self.error = self._open()
        self.key = pdf_notes.key_for(self.path) if not self.error else None
        self._kept = pdf_notes.load(self.key)
        self._restored = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self.save_state)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        if self.error:
            message = QLabel(self.error)
            message.setObjectName("emptyState")
            message.setAlignment(Qt.AlignmentFlag.AlignCenter)
            message.setWordWrap(True)
            layout.addWidget(message, 1)
            self.view = None
            return
        self.view = PdfPageView(self.doc)
        self.view.load_marks(self._kept.get("annotations"))
        self.view.page_menu = self._page_menu
        self.toast = QLabel(self.view)
        self.toast.setObjectName("boardToast")
        self.toast.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.toast.setWordWrap(True)
        self.toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(MESSAGE_MS)
        self._toast_timer.timeout.connect(self.toast.hide)
        layout.addWidget(self._build_toolbar())
        layout.addWidget(self._build_marks_bar())
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(2)
        self.sidebar = self._build_sidebar()
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.view)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([220, 800])
        layout.addWidget(self.splitter, 1)

        self.view.page_changed.connect(self._on_page_changed)
        self.view.zoom_changed.connect(self._on_zoom_changed)
        self.view.mode_changed.connect(self._on_mode_changed)
        self.view.matches_changed.connect(self._on_matches_changed)
        self.view.marks_changed.connect(self._on_marks_changed)
        self.view.undo_changed.connect(self._sync_marks_bar)
        self.view.view_changed.connect(self._save_timer.start)
        self.setFocusProxy(self.view)
        view = self._kept.get("view") if isinstance(self._kept.get("view"), dict) else {}
        self.sidebar.setVisible(bool(view.get("sidebar", self.has_contents())))
        self.show_tab(view.get("tab") if view.get("tab") in ("contents", "pages", "notes") else ("contents" if self.has_contents() else "pages"))
        self._on_page_changed(0)
        self._on_marks_changed()

    # ------------------ opening ------------------
    def _open(self) -> str:
        """Load the PDF (asking for its password if it has one); "" or why it can't be shown."""
        self.doc.load(str(self.path))
        tries = 0
        while self.doc.status() == QPdfDocument.Status.Error and self.doc.error() == QPdfDocument.Error.IncorrectPassword and tries < 3:
            password, ok = QInputDialog.getText(
                self, "Password", f"{self.path.name} is protected. Its password:" if not tries else "That password didn't work. Try again:",
                QLineEdit.EchoMode.Password,
            )
            if not ok:
                return "This PDF is protected by a password."
            self.doc.setPassword(password)
            self.doc.load(str(self.path))
            tries += 1
        if self.doc.status() != QPdfDocument.Status.Ready or self.doc.pageCount() == 0:
            reasons = {
                QPdfDocument.Error.FileNotFound: "The file isn't there any more.",
                QPdfDocument.Error.InvalidFileFormat: "The file is damaged, or isn't a PDF.",
                QPdfDocument.Error.IncorrectPassword: "This PDF is protected by a password.",
                QPdfDocument.Error.UnsupportedSecurityScheme: "This PDF uses a kind of protection that can't be opened here.",
            }
            return f"Couldn't open this PDF. {reasons.get(self.doc.error(), '')}".strip()
        return ""

    def has_contents(self) -> bool:
        return self.contents_model.rowCount(QModelIndex()) > 0 if self.view is not None else False

    # ------------------ toolbar ------------------
    def _button(self, icon: str, tip: str, slot, text: str = "") -> QPushButton:
        button = QPushButton(text)
        set_icon(button, icon)
        button.setToolTip(tip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(slot)
        return button

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("viewerToolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(6)
        key = lambda shortcut_id: shortcuts.get(shortcut_id).keys  # noqa: E731
        self.btn_sidebar = self._button("menu", f"Show / hide Contents and Pages ({key('pdf.sidebar')})", self.toggle_sidebar)
        self.btn_sidebar.setCheckable(True)
        row.addWidget(self.btn_sidebar)
        row.addSpacing(8)
        row.addWidget(self._button("arrow_up", f"Previous page ({key('pdf.previous_page')})", lambda: self.view.step_page(-1)))
        self.page_box = QLineEdit()
        self.page_box.setObjectName("pdfPageBox")
        self.page_box.setFixedWidth(56)
        self.page_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_box.setValidator(QIntValidator(1, max(1, self.doc.pageCount()), self.page_box))
        self.page_box.setToolTip("The page you're on: type a page number and press Enter to go there")
        self.page_box.returnPressed.connect(self._on_page_typed)
        row.addWidget(self.page_box)
        self.lbl_count = QLabel(f"of {self.doc.pageCount()}")
        self.lbl_count.setObjectName("toolbarLabel")
        row.addWidget(self.lbl_count)
        row.addWidget(self._button("arrow_down", f"Next page ({key('pdf.next_page')})", lambda: self.view.step_page(1)))
        row.addStretch(1)
        self.find_box = QLineEdit()
        self.find_box.setObjectName("pdfFindBox")
        self.find_box.setPlaceholderText("Find in PDF")
        self.find_box.setClearButtonEnabled(True)
        self.find_box.setFixedWidth(180)
        self.find_box.setToolTip(f"Find words in the PDF ({key('pdf.find')}). Enter: next match, Shift+Enter: previous, Esc: clear")
        self.find_box.installEventFilter(self)
        self._find_timer = QTimer(self)
        self._find_timer.setSingleShot(True)
        self._find_timer.setInterval(SEARCH_DELAY_MS)
        self._find_timer.timeout.connect(self._on_find_typed)
        self.find_box.textChanged.connect(self._find_timer.start)
        row.addWidget(self.find_box)
        self.lbl_matches = QLabel("")
        self.lbl_matches.setObjectName("toolbarLabel")
        self.lbl_matches.setMinimumWidth(70)
        row.addWidget(self.lbl_matches)
        row.addWidget(self._button("arrow_up", f"Previous match ({key('pdf.find_previous')})", lambda: self.view.step_match(-1)))
        row.addWidget(self._button("arrow_down", f"Next match ({key('pdf.find_next')})", lambda: self.view.step_match(1)))
        row.addSpacing(8)
        row.addWidget(self._button("zoom_out", f"Zoom out ({key('pdf.zoom_out')}, or Ctrl+wheel)", lambda: self.view.zoom_by(1.0 / ZOOM_STEP)))
        self.zoom_box = QComboBox()
        self.zoom_box.setObjectName("pdfZoomBox")
        self.zoom_box.setEditable(True)
        self.zoom_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.zoom_box.setFixedWidth(104)
        self.zoom_box.setToolTip("Zoom: pick a size, or type one (like 140%) and press Enter")
        self.zoom_box.addItem("Fit Width", "width")
        self.zoom_box.addItem("Fit Page", "page")
        for percent in ZOOM_PRESETS:
            self.zoom_box.addItem(f"{percent}%", percent / 100.0)
        self.zoom_box.activated.connect(self._on_zoom_picked)
        self.zoom_box.lineEdit().returnPressed.connect(self._on_zoom_typed)
        row.addWidget(self.zoom_box)
        row.addWidget(self._button("zoom_in", f"Zoom in ({key('pdf.zoom_in')}, or Ctrl+wheel)", lambda: self.view.zoom_by(ZOOM_STEP)))
        row.addSpacing(8)
        row.addWidget(self._button("fit_width", f"Fit the page's width ({key('pdf.fit_width')})", lambda: self.view.fit("width")))
        row.addWidget(self._button("fit", f"Fit the whole page ({key('pdf.fit_page')})", lambda: self.view.fit("page")))
        return bar

    def _build_marks_bar(self) -> QWidget:
        """The second row: the tools, the colour and pen size, undo / redo, and showing your marks."""
        bar = QWidget()
        bar.setObjectName("viewerToolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 2, 10, 4)
        row.setSpacing(6)
        key = lambda shortcut_id: shortcuts.get(shortcut_id).keys  # noqa: E731
        self.tool_group = QButtonGroup(bar)
        self.tool_buttons: dict[str, QPushButton] = {}
        for mode, icon, label, tip in (
            ("hand", "hand", "", f"Hand: drag to move the pages; click a mark to pick it ({key('pdf.escape')} goes back to it)"),
            ("select", "text_select", "", f"Select Text: drag over text, then {key('pdf.copy')} to copy ({key('pdf.select_tool')})"),
            ("highlight", "highlight", "Highlight", f"Highlight: drag over text ({key('pdf.highlight_tool')}; with text selected, highlights it)"),
            ("pen", "edit", "Pen", f"Pen: draw on the page ({key('pdf.pen_tool')})"),
            ("note", "sticky_note", "Note", f"Note: click on the page and type ({key('pdf.note_tool')})"),
            ("eraser", "eraser", "", f"Eraser: drag over highlights, drawings and notes to remove them ({key('pdf.eraser_tool')})"),
        ):
            button = self._button(icon, tip, lambda _checked=False, m=mode: self.view.set_mode(m), label)
            button.setCheckable(True)
            self.tool_group.addButton(button)
            self.tool_buttons[mode] = button
            row.addWidget(button)
            if mode == "select":
                row.addSpacing(8)
        self.tool_buttons["hand"].setChecked(True)
        row.addSpacing(8)
        self.btn_color = QPushButton()
        self.btn_color.setToolTip("Colour for the highlighter, pen or notes (the tool that's on, or the last one used)")
        self.btn_color.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_color.clicked.connect(self._show_color_menu)
        row.addWidget(self.btn_color)
        self.size_box = QComboBox()
        self.size_box.setToolTip("Pen size (in points: it grows and shrinks with the page as you zoom)")
        self.size_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for size in PEN_SIZES:
            self.size_box.addItem(f"{size:g} pt", size)
        self.size_box.setCurrentIndex(self.size_box.findData(self.view.pen_width))
        self.size_box.activated.connect(self._on_size_picked)
        row.addWidget(self.size_box)
        row.addSpacing(8)
        self.btn_undo = self._button("undo", f"Undo ({key('pdf.undo')})", self.view.undo)
        self.btn_redo = self._button("redo", f"Redo ({key('pdf.redo')})", self.view.redo)
        row.addWidget(self.btn_undo)
        row.addWidget(self.btn_redo)
        row.addStretch(1)
        self.btn_export = self._button("copy", "Export a copy of the pages with your marks drawn in (a PDF or PNG images)", self.export_copy, "Export...")
        row.addWidget(self.btn_export)
        self.btn_show_marks = self._button("eye", "Show / hide your highlights, drawings and notes", self._on_show_marks, "Marks")
        self.btn_show_marks.setCheckable(True)
        self.btn_show_marks.setChecked(True)
        row.addWidget(self.btn_show_marks)
        self._color_tool = "highlight"  # (whose colour the colour button shows when no such tool is on)
        self._color_popup: Optional[ColorPopup] = None
        self._sync_marks_bar()
        return bar

    def _color_target(self) -> str:
        return self.view.mode if self.view.mode in TOOL_COLORS else self._color_tool

    def _sync_marks_bar(self) -> None:
        self.btn_color.setIcon(swatch_icon(self.view.colors[self._color_target()]))
        self.size_box.setEnabled(self.view.mode in ("pen", "hand", "select"))
        self.btn_undo.setEnabled(self.view.undo_stack.canUndo())
        self.btn_redo.setEnabled(self.view.undo_stack.canRedo())

    def _show_color_menu(self) -> None:
        tool = self._color_target()
        menu = QMenu(self)
        for color in TOOL_COLORS[tool]:
            action = menu.addAction(swatch_icon(color), color.upper())
            action.triggered.connect(lambda _checked=False, c=color: self._set_tool_color(tool, c))
        menu.addSeparator()
        menu.addAction("Custom...").triggered.connect(lambda: self._custom_color(tool))
        menu.exec(self.btn_color.mapToGlobal(QPoint(0, self.btn_color.height())))
        menu.deleteLater()

    def _set_tool_color(self, tool: str, color: str) -> None:
        self.view.colors[tool] = color
        self._sync_marks_bar()

    def _custom_color(self, tool: str) -> None:
        if self._color_popup is not None:
            self._color_popup.close()
        popup = ColorPopup({"highlight": "Highlighter colour", "pen": "Pen colour", "note": "Note colour"}[tool], QColor(self.view.colors[tool]), self)
        popup.changed.connect(lambda color, t=tool: self._set_tool_color(t, color.name(QColor.NameFormat.HexRgb if color.alpha() == 255 else QColor.NameFormat.HexArgb)))
        self._color_popup = popup
        popup.show_beside(QRect(self.btn_color.mapToGlobal(QPoint(0, 0)), self.btn_color.size()))

    def _on_size_picked(self, index: int) -> None:
        self.view.pen_width = float(self.size_box.itemData(index))
        self.view.setFocus()

    def _on_show_marks(self) -> None:
        self.view.set_marks_shown(self.btn_show_marks.isChecked())
        self.view.setFocus()

    # ------------------ messages ------------------
    def show_message(self, text: str) -> None:
        """A short note at the bottom of the pages (gone after a few seconds)."""
        self.toast.setText(text)
        width = min(560, max(200, self.view.width() - 40))
        height = max(34, self.toast.heightForWidth(width) + 4)
        self.toast.setGeometry((self.view.width() - width) // 2, self.view.height() - 22 - height, width, height)
        self.toast.show()
        self.toast.raise_()
        self._toast_timer.start()

    # ------------------ the Artbook and exporting ------------------
    def _page_menu(self, menu: QMenu, page: int) -> None:
        """The page's own entries in the right-click menu: send it to the Artbook, export a copy."""
        if self.artbook is not None:
            send = menu.addMenu("Send Page to Artbook")
            self.artbook.fill_artbook_menu(send, lambda page_id, p=page: self.send_page(p, page_id, True))
            if self.view.page_marks(page):
                plain = menu.addMenu("Send Page to Artbook (without your marks)")
                self.artbook.fill_artbook_menu(plain, lambda page_id, p=page: self.send_page(p, page_id, False))
        menu.addAction("Export a Copy...").triggered.connect(self.export_copy)

    def page_name(self, page: int) -> str:
        label = self.doc.pageLabel(page) or str(page + 1)
        return f"{self.path.stem}, p. {label}"

    def send_page(self, page: int, page_id: Optional[str], marks: bool = True) -> None:
        """Send a page to an Artbook page (None: a new one) as a picture, with your marks if marks."""
        if self.artbook is None:
            return
        self.view.finish_note_edit()
        item = self.view.pages[page]
        scale = min(SEND_SCALE, SEND_MAX_SIDE / max(item.size.width(), item.size.height()))
        message = self.artbook.send_image_to_artbook(self.view.page_picture(page, scale, marks), self.page_name(page), page_id)
        self.show_message(message)

    def export_copy(self) -> None:
        """Export a Copy...: ask what and where, then export (see export_pages)."""
        if self._export is not None:
            return
        self.view.finish_note_edit()
        marked = sorted({page for page, _item in self.view.mark_items()})
        current = max(0, self.view.current_page())
        dialog = ExportDialog(self, len(marked), current, self.doc.pageCount())
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        choice = dialog.choice()
        dialog.deleteLater()
        if not accepted:
            return
        pages = {"marked": marked, "all": list(range(self.doc.pageCount())), "current": [current]}[choice["pages"]]
        start = Path(get_last_export_dir() or self.path.parent)
        if choice["format"] == "pdf":
            chosen, _filter = QFileDialog.getSaveFileName(
                self, "Export a Copy", str(start / f"{self.path.stem} (with notes).pdf"), "PDF (*.pdf)",
            )
            if not chosen:
                return
            target = Path(chosen) if chosen.lower().endswith(".pdf") else Path(chosen + ".pdf")
        else:
            chosen = QFileDialog.getExistingDirectory(self, "Export a Copy: the folder for the pictures", str(start))
            if not chosen:
                return
            target = Path(chosen)
        set_last_export_dir(str(target.parent if choice["format"] == "pdf" else target))
        self.export_pages(pages, target, choice["format"], choice["dpi"], choice["marks"])

    def export_pages(self, pages: list[int], target: Path, kind: str, dpi: int, marks: bool) -> None:
        """Write the pages (with your marks if marks) to target: a PDF file (kind "pdf") or a folder
        of PNGs ("png"), a page at a time with a progress window (Cancel stops; a cancelled PDF is
        removed). Never over the PDF being shown."""
        if not pages:
            return
        if kind == "pdf" and target.exists() and target.resolve() == self.path.resolve():
            QMessageBox.warning(self, "Export a Copy", "That's this PDF: pick another name, so the original stays as it is.")
            return
        progress = QProgressDialog("Exporting pages...", "Cancel", 0, len(pages), self)
        progress.setWindowTitle("Export a Copy")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        export = {"pages": pages, "target": target, "kind": kind, "scale": dpi / 72.0, "marks": marks, "done": 0,
                  "progress": progress, "writer": None, "painter": None, "files": []}
        if kind == "pdf":
            writer = QPdfWriter(str(target))
            writer.setResolution(dpi)
            writer.setCreator("Asset Manager")
            writer.setTitle(self.path.stem)
            writer.setPageMargins(QMarginsF(0, 0, 0, 0))
            writer.setPageSize(QPageSize(self.view.pages[pages[0]].size, QPageSize.Unit.Point))
            export["writer"] = writer
        else:
            target.mkdir(parents=True, exist_ok=True)
        self._export = export
        export["timer"] = QTimer(self)
        export["timer"].setInterval(0)
        export["timer"].timeout.connect(self._export_step)
        export["timer"].start()

    def _export_step(self) -> None:
        export = self._export
        if export is None:
            return
        if export["progress"].wasCanceled():
            self._end_export("Export cancelled.", cancelled=True)
            return
        page = export["pages"][export["done"]]
        try:
            picture = self.view.page_picture(page, export["scale"], export["marks"])
            if export["kind"] == "pdf":
                writer = export["writer"]
                if export["painter"] is None:
                    export["painter"] = QPainter()
                    if not export["painter"].begin(writer):
                        raise OSError(f"Couldn't write {export['target'].name}.")
                else:
                    writer.setPageSize(QPageSize(self.view.pages[page].size, QPageSize.Unit.Point))
                    writer.newPage()
                export["painter"].drawImage(QRectF(0, 0, writer.width(), writer.height()), picture)
            else:
                digits = len(str(self.doc.pageCount()))
                name = export["target"] / f"{self.path.stem} p{page + 1:0{digits}d}.png"
                if not picture.save(str(name), "PNG"):
                    raise OSError(f"Couldn't write {name.name}.")
                export["files"].append(name)
        except OSError as exc:
            self._end_export(str(exc), cancelled=True)
            return
        export["done"] += 1
        export["progress"].setValue(export["done"])
        if export["done"] >= len(export["pages"]):
            count = len(export["pages"])
            where = export["target"].name if export["kind"] == "pdf" else str(export["target"])
            self._end_export(f"Exported {count} page{'s' if count != 1 else ''} to {where}.")

    def _end_export(self, message: str, cancelled: bool = False) -> None:
        export, self._export = self._export, None
        export["timer"].stop()
        if export["painter"] is not None and export["painter"].isActive():
            export["painter"].end()
        export["writer"] = None  # (the file is complete, and let go of)
        if cancelled and export["kind"] == "pdf":
            export["target"].unlink(missing_ok=True)  # (half a PDF is no use)
        export["progress"].close()
        export["progress"].deleteLater()
        self.show_message(message)

    def exporting(self) -> bool:
        return self._export is not None

    # ------------------ tools and find ------------------
    def _on_mode_changed(self, mode: str) -> None:
        self.tool_buttons[mode].setChecked(True)
        if mode in TOOL_COLORS:
            self._color_tool = mode
        self._sync_marks_bar()

    def _on_find_typed(self) -> None:
        self.view.find(self.find_box.text())

    def _on_matches_changed(self) -> None:
        search = self.view.search
        count = len(search.matches)
        if not search.query:
            text = ""
        elif not search.done and not count:
            text = "Searching..."
        elif not count:
            text = "No matches"
        elif self.view.current_match >= 0:
            text = f"{self.view.current_match + 1} of {count}{'' if search.done else '+'}"
        else:
            text = f"{count}{'' if search.done else '+'} found"
        self.lbl_matches.setText(text)

    def find_step(self, step: int) -> None:
        if self._find_timer.isActive() or self.find_box.text().split() != self.view.search.query.split():
            self._find_timer.stop()
            self.view.find(self.find_box.text())  # (typed and pressed Enter at once: search first)
            return
        self.view.step_match(step)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.find_box and event.type() == QEvent.Type.KeyPress:
            if shortcuts.matches("pdf.find_box_next", event) or shortcuts.matches("pdf.find_box_next_alt", event):
                self.find_step(1)
                return True
            if shortcuts.matches("pdf.find_box_previous", event):
                self.find_step(-1)
                return True
            if shortcuts.matches("pdf.escape", event):
                self.find_box.clear()
                self._find_timer.stop()
                self.view.find("")
                self.view.setFocus()
                return True
        return super().eventFilter(obj, event)

    def _on_page_typed(self) -> None:
        text = self.page_box.text().strip()
        if text.isdigit():
            self.view.show_page(int(text) - 1)
        self.view.setFocus()

    def _on_zoom_picked(self, index: int) -> None:
        value = self.zoom_box.itemData(index)
        if isinstance(value, str):
            self.view.fit(value)
        elif isinstance(value, float):
            self.view.set_zoom(value)
        self.view.setFocus()

    def _on_zoom_typed(self) -> None:
        text = self.zoom_box.currentText().strip().rstrip("%").strip()
        try:
            self.view.set_zoom(float(text) / 100.0)
        except ValueError:
            self._on_zoom_changed(self.view.zoom())
        self.view.setFocus()

    def _on_page_changed(self, page: int) -> None:
        self.page_box.setText(str(max(0, page) + 1))
        if not self.sidebar.isHidden() and self.thumbs.isVisible() and page >= 0:
            at = self.thumb_model.index(page, 0)
            self.thumbs.setCurrentIndex(at)
            self.thumbs.scrollTo(at)

    def _on_zoom_changed(self, zoom: float) -> None:
        mode = self.view.fit_mode()
        self.zoom_box.setEditText({"width": "Fit Width", "page": "Fit Page"}.get(mode, f"{round(zoom * 100)}%"))

    # ------------------ sidebar ------------------
    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("pdfSidebar")
        side.setMinimumWidth(150)
        column = QVBoxLayout(side)
        column.setContentsMargins(8, 8, 8, 8)
        column.setSpacing(6)
        tabs = QHBoxLayout()
        tabs.setSpacing(4)
        self.tab_group = QButtonGroup(side)
        self.tab_buttons: dict[str, QPushButton] = {}
        for name, label, tip in (
            ("contents", "Contents", "The PDF's chapters: click one to go there"),
            ("pages", "Pages", "Every page: click one to go there"),
            ("notes", "Notes", "Your highlights, drawings and notes: click one to go there"),
        ):
            button = QPushButton(label)
            button.setObjectName("pdfTab")  # (snug: three side by side in a narrow sidebar)
            button.setMinimumWidth(0)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _checked=False, n=name: self.show_tab(n))
            self.tab_group.addButton(button)
            self.tab_buttons[name] = button
            tabs.addWidget(button)
        column.addLayout(tabs)
        self.side_stack = QStackedWidget()

        contents = QWidget()
        contents_column = QVBoxLayout(contents)
        contents_column.setContentsMargins(0, 0, 0, 0)
        self.contents_model = QPdfBookmarkModel(self)
        self.contents_model.setDocument(self.doc)
        self.contents = QTreeView()
        self.contents.setObjectName("pdfContents")
        self.contents.setHeaderHidden(True)
        self.contents.setModel(self.contents_model)
        self.contents.setUniformRowHeights(True)
        self.contents.clicked.connect(self._on_contents_clicked)
        self.contents.activated.connect(self._on_contents_clicked)
        contents_column.addWidget(self.contents, 1)
        self.lbl_no_contents = QLabel("This PDF has no contents list. The Pages list shows every page.")
        self.lbl_no_contents.setObjectName("pageHint")
        self.lbl_no_contents.setWordWrap(True)
        contents_column.addWidget(self.lbl_no_contents)
        contents_column.addStretch(0)
        has = self.contents_model.rowCount(QModelIndex()) > 0
        nested = any(self.contents_model.hasChildren(self.contents_model.index(row, 0)) for row in range(self.contents_model.rowCount(QModelIndex())))
        self.contents.setRootIsDecorated(nested)  # (a flat list needs no room for arrows)
        self.contents.setVisible(has)
        self.lbl_no_contents.setVisible(not has)
        self.side_stack.addWidget(contents)

        self.thumb_model = ThumbnailModel(self.doc, [page.size for page in self.view.pages], self.devicePixelRatioF(), self)
        self.thumbs = QListView()
        self.thumbs.setObjectName("pdfThumbs")
        self.thumbs.setModel(self.thumb_model)
        self.thumbs.setItemDelegate(ThumbnailDelegate(self.thumbs))
        self.thumbs.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.thumbs.setSpacing(2)
        self.thumbs.clicked.connect(self._on_thumb_clicked)
        self.thumbs.activated.connect(self._on_thumb_clicked)
        self.side_stack.addWidget(self.thumbs)

        notes = QWidget()
        notes_column = QVBoxLayout(notes)
        notes_column.setContentsMargins(0, 0, 0, 0)
        self.notes_list = QListWidget()
        self.notes_list.setObjectName("pdfNotesList")
        self.notes_list.setWordWrap(True)
        self.notes_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.notes_list.itemClicked.connect(self._on_note_listed_clicked)
        self.notes_list.itemActivated.connect(self._on_note_listed_clicked)
        notes_column.addWidget(self.notes_list, 1)
        self.lbl_no_notes = QLabel("No highlights, drawings or notes yet. The Highlight, Pen and Note tools are above the pages.")
        self.lbl_no_notes.setObjectName("pageHint")
        self.lbl_no_notes.setWordWrap(True)
        notes_column.addWidget(self.lbl_no_notes)
        self.side_stack.addWidget(notes)
        column.addWidget(self.side_stack, 1)
        return side

    def _on_marks_changed(self) -> None:
        """List the marks (page by page) in the Notes tab, and keep them."""
        self.notes_list.clear()
        names = {"highlight": "Highlight", "ink": "Drawing", "note": "Note"}
        for data in self.view.marks_data():
            words = " ".join((data.get("text") or "").split())
            label = f"p. {data['page'] + 1}  {names[data['kind']]}" + (f": {words[:90]}{'...' if len(words) > 90 else ''}" if words else "")
            item = QListWidgetItem(swatch_icon(data["color"]), label)
            item.setData(Qt.ItemDataRole.UserRole, data["id"])
            self.notes_list.addItem(item)
        has = self.notes_list.count() > 0
        self.notes_list.setVisible(has)
        self.lbl_no_notes.setVisible(not has)
        self._sync_marks_bar()
        self._save_timer.start()

    def _on_note_listed_clicked(self, item: QListWidgetItem) -> None:
        self.view.show_mark(item.data(Qt.ItemDataRole.UserRole))
        self.view.setFocus()

    def show_tab(self, name: str) -> None:
        self.tab_buttons[name].setChecked(True)
        self.side_stack.setCurrentIndex({"contents": 0, "pages": 1, "notes": 2}[name])
        if name == "pages":
            self._on_page_changed(self.view.current_page())
        self._save_timer.start()

    def current_tab(self) -> str:
        return ("contents", "pages", "notes")[self.side_stack.currentIndex()]

    def toggle_sidebar(self) -> None:
        self.sidebar.setVisible(self.sidebar.isHidden())
        self.btn_sidebar.setChecked(not self.sidebar.isHidden())
        self._save_timer.start()

    def _on_contents_clicked(self, index: QModelIndex) -> None:
        page = self.contents_model.data(index, QPdfBookmarkModel.Role.Page.value)
        location = self.contents_model.data(index, QPdfBookmarkModel.Role.Location.value)
        if isinstance(page, int) and page >= 0:
            y = location.y() if isinstance(location, QPointF) else 0.0
            self.view.show_page(page, y - PAGE_GAP / 2.0 if y > 0 else None)
            self.view.setFocus()

    def _on_thumb_clicked(self, index: QModelIndex) -> None:
        self.view.show_page(index.row())
        self.view.setFocus()

    # ------------------ keys ------------------
    def keyPressEvent(self, event) -> None:
        if self.view is None:
            super().keyPressEvent(event)
            return
        if shortcuts.matches("pdf.sidebar", event):
            self.toggle_sidebar()
        elif shortcuts.matches("pdf.find", event):
            self.find_box.setFocus()
            self.find_box.selectAll()
        elif shortcuts.matches("pdf.find_next", event):
            self.find_step(1)
        elif shortcuts.matches("pdf.find_previous", event):
            self.find_step(-1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # ------------------ remembering where you were ------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.view is not None and not self._restored:
            self._restored = True
            QTimer.singleShot(0, self._restore)  # (once it has its size: fitting needs it)

    def _restore(self) -> None:
        view = self._kept.get("view") if isinstance(self._kept.get("view"), dict) else {}
        fit = view.get("fit") if view.get("fit") in ("width", "page") else None
        zoom = view.get("zoom")
        if fit is None and isinstance(zoom, (int, float)) and zoom > 0:
            self.view.set_zoom(float(zoom))
        else:
            self.view.fit(fit or "width")
        page = view.get("page")
        offset = view.get("offset")
        if isinstance(page, int):
            self.view.show_page(page, float(offset) if isinstance(offset, (int, float)) else None)
        else:
            self.view.show_page(0)
        self.btn_sidebar.setChecked(not self.sidebar.isHidden())
        self._on_zoom_changed(self.view.zoom())
        self.view.setFocus()

    def view_state(self) -> dict[str, Any]:
        return {
            "page": self.view.current_page(), "offset": round(self.view.page_offset(), 2), "zoom": round(self.view.zoom(), 4),
            "fit": self.view.fit_mode(), "sidebar": not self.sidebar.isHidden(), "tab": self.current_tab(),
        }

    def save_state(self) -> None:
        """Keep where you were in this PDF (core.pdf_notes)."""
        if self.view is None or not self.key or not self._restored:
            return
        self._save_timer.stop()
        self.view.finish_note_edit()
        self._kept["view"] = self.view_state()
        self._kept["annotations"] = self.view.marks_data()
        self._kept["path"] = str(self.path)
        try:
            pdf_notes.save(self.key, self._kept)
        except OSError:
            pass  # (only where you were: next time it opens at the start)
