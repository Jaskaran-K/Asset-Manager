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

from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QImage,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QTransform,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QMenu,
    QStyleOptionGraphicsItem,
    QWidget,
)

from PIL import Image

from core import artbook_store as store
from ui import animations, shortcuts
from ui.tag_widgets import run_job
from ui.themes import theme_color

ZOOM_RANGE = (0.02, 32.0)
WHEEL_ZOOM_STEP = 1.2  # per wheel notch
ZOOM_MS = 140
FIT_MS = 260
FIT_MARGIN = 0.08
DROP_GAP = 24.0  # between images dropped together
HANDLE_PX = 9.0  # on screen, whatever the zoom
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


@dataclass(frozen=True)
class ItemState:
    """Where an image sits: center position, uniform scale, rotation (degrees), flips, stacking."""
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    z: float = 0.0


class ImageItem(QGraphicsItem):
    """One reference image. Its origin is its center; size is the image's full pixel size."""

    def __init__(self, item_id: str, source: dict[str, Optional[str]], size: tuple[int, int]):
        super().__init__()
        self.item_id = item_id
        self.source = source  # {"path": linked file or None, "blob": file in the page's images/ or None}
        self.natural = (max(1, int(size[0])), max(1, int(size[1])))
        self.pixmap: Optional[QPixmap] = None
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsMovable)

    # ------------------ state ------------------
    def state(self) -> ItemState:
        flip = self.transform()
        return ItemState(
            self.pos().x(), self.pos().y(), self.scale(), self.rotation(),
            flip.m11() < 0, flip.m22() < 0, self.zValue(),
        )

    def apply_state(self, state: ItemState) -> None:
        self.setPos(state.x, state.y)
        self.setScale(state.scale)
        self.setRotation(state.rotation)
        self.setTransform(QTransform.fromScale(-1.0 if state.flip_h else 1.0, -1.0 if state.flip_v else 1.0))
        self.setZValue(state.z)

    def to_dict(self) -> dict[str, Any]:
        s = self.state()
        return {
            "id": self.item_id, "kind": "image", "source": self.source, "size": list(self.natural),
            "x": s.x, "y": s.y, "scale": s.scale, "rotation": s.rotation,
            "flip_h": s.flip_h, "flip_v": s.flip_v, "z": s.z,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImageItem":
        item = cls(str(data.get("id") or uuid.uuid4().hex), dict(data.get("source") or {}), tuple(data.get("size") or (512, 512)))
        item.apply_state(ItemState(
            float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("scale", 1)) or 1.0,
            float(data.get("rotation", 0)), bool(data.get("flip_h")), bool(data.get("flip_v")), float(data.get("z", 0)),
        ))
        return item

    def set_image(self, image: QImage) -> None:
        self.pixmap = QPixmap.fromImage(image)
        self.update()

    def image(self) -> Optional[QImage]:
        return self.pixmap.toImage() if self.pixmap is not None else None

    # ------------------ painting ------------------
    def boundingRect(self) -> QRectF:
        w, h = self.natural
        return QRectF(-w / 2.0, -h / 2.0, w, h)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        rect = self.boundingRect()
        if self.pixmap is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(rect, self.pixmap, QRectF(self.pixmap.rect()))
        else:
            # Still loading (or its file is gone): a quiet placeholder of the right size
            placeholder = QColor(theme_color("text_faint"))
            placeholder.setAlpha(40)
            painter.fillRect(rect, placeholder)
        # The selection frame is drawn by the view (BoardView.drawForeground), not here


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

    # Keys the board handles itself while it has the focus (even ones the window also uses)
    KEYS = (
        "artbook.paste", "artbook.copy", "artbook.fit", "artbook.zoom_100", "artbook.flip_h", "artbook.flip_v",
        "artbook.front", "artbook.back", "artbook.reset", "artbook.delete", "artbook.select_all",
        "artbook.deselect", "artbook.undo", "artbook.redo", "artbook.pan_key",
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
        self.toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(3500)
        self._toast_timer.timeout.connect(self.toast.hide)

    # ------------------ page ------------------
    def load_page(self, page: dict[str, Any]) -> None:
        """Show a page (its images fill in as they're decoded)."""
        self._generation += 1
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
        self._scene.render(painter, target, bounds)  # items only: the selection frame is the view's
        painter.end()
        return image

    def view_state(self) -> dict[str, float]:
        center = self.view_center()
        return {"x": center.x(), "y": center.y(), "zoom": self.zoom()}

    def items_list(self) -> list[ImageItem]:
        return list(self._items.values())

    def item(self, item_id: str) -> Optional[ImageItem]:
        return self._items.get(item_id)

    def attach(self, item: ImageItem) -> None:
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
        self._toast_timer.start()

    def _place_overlays(self) -> None:
        self.hint.setGeometry(self.rect().adjusted(40, 0, -40, 0))
        width = min(self.toast.sizeHint().width() + 24, self.width() - 40)
        self.toast.setGeometry((self.width() - width) // 2, self.height() - 56, width, 34)

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
        if self._take(event.mimeData(), at):
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
        return [i for i in self._scene.selectedItems() if isinstance(i, ImageItem)]

    def _on_selection_changed(self) -> None:
        self.viewport().update()

    def _image_at(self, pos: QPointF) -> Optional[ImageItem]:
        for item in self.items(pos.toPoint()):
            if isinstance(item, ImageItem):
                return item
        return None

    def _snapshot(self, items: list[ImageItem]) -> dict[str, ItemState]:
        return {item.item_id: item.state() for item in items}

    def _push_states(self, before: dict[str, ItemState], text: str) -> None:
        after = {item_id: self._items[item_id].state() for item_id in before if item_id in self._items}
        if after != before:
            self.undo_stack.push(_StateCommand(self, before, after, text))

    def _change_selection(self, text: str, change: Callable[[ImageItem, ItemState], ItemState]) -> None:
        items = self.selected()
        if not items:
            return
        before = self._snapshot(items)
        after = {item.item_id: change(item, before[item.item_id]) for item in items}
        if after != before:
            self.undo_stack.push(_StateCommand(self, before, after, text))

    def flip(self, horizontal: bool) -> None:
        self._change_selection(
            "flip", lambda _item, s: replace(s, flip_h=not s.flip_h) if horizontal else replace(s, flip_v=not s.flip_v)
        )

    def reset_transform(self) -> None:
        self._change_selection("reset", lambda _item, s: replace(s, scale=1.0, rotation=0.0, flip_h=False, flip_v=False))

    def restack(self, to_front: bool) -> None:
        items = sorted(self.selected(), key=lambda i: i.zValue())
        others = [i.zValue() for i in self._items.values() if not i.isSelected()]
        if not items:
            return
        start = (max(others, default=0.0) + 1.0) if to_front else (min(others, default=0.0) - len(items))
        order = {item.item_id: start + n for n, item in enumerate(items)}
        self._change_selection("restack", lambda item, s: replace(s, z=order[item.item_id]))

    def delete_selection(self) -> None:
        items = self.selected()
        if items:
            self.undo_stack.push(_RemoveCommand(self, items, "delete"))

    def select_all(self) -> None:
        for item in self._items.values():
            item.setSelected(True)

    def copy_selection(self) -> bool:
        items = sorted(self.selected(), key=lambda i: i.zValue())
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

    def _rotate_handle(self, corners: list[QPointF]) -> QPointF:
        top_mid = (corners[0] + corners[1]) / 2.0
        bottom_mid = (corners[3] + corners[2]) / 2.0
        up = top_mid - bottom_mid
        length = math.hypot(up.x(), up.y()) or 1.0
        return top_mid + up / length * (ROTATE_HANDLE_PX / self.zoom())

    def _handle_at(self, pos: QPointF) -> Optional[tuple[str, int]]:
        corners = self._frame_corners()
        if corners is None:
            return None
        if len(self.selected()) == 1:
            handle = self.mapFromScene(self._rotate_handle(corners))
            if math.hypot(handle.x() - pos.x(), handle.y() - pos.y()) <= HANDLE_PX:
                return ("rotate", -1)
        for index, corner in enumerate(corners):
            point = self.mapFromScene(corner)
            if abs(point.x() - pos.x()) <= HANDLE_PX and abs(point.y() - pos.y()) <= HANDLE_PX:
                return ("scale", index)
        return None

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
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
        painter.drawPolygon(QPolygonF(corners))
        size = HANDLE_PX / self.zoom()
        painter.setBrush(QBrush(QColor("#ffffff")))
        for corner in corners:
            painter.drawRect(QRectF(corner.x() - size / 2, corner.y() - size / 2, size, size))
        if len(self.selected()) == 1:
            handle = self._rotate_handle(corners)
            top_mid = (corners[0] + corners[1]) / 2.0
            painter.drawLine(top_mid, handle)
            painter.drawEllipse(handle, size * 0.6, size * 0.6)

    # ------------------ mouse ------------------
    def mousePressEvent(self, event) -> None:
        self.setFocus()
        pos = event.position()
        button = event.button()
        if button == Qt.MouseButton.MiddleButton or (button == Qt.MouseButton.LeftButton and self._space_down):
            self._drag = {"mode": "pan", "last": pos}
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
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
        if kind == "scale":
            drag["fixed"] = corners[(index + 2) % 4]
            drag["grab"] = corners[index]
        else:
            drag["center"] = items[0].scenePos()
        self._drag = drag

    def mouseMoveEvent(self, event) -> None:
        drag = self._drag
        pos = event.position()
        if drag and drag["mode"] == "pan":
            delta = pos - drag["last"]
            drag["last"] = pos
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(delta.y()))
            return
        if drag and drag["mode"] == "scale":
            self._drag_scale(drag, self.mapToScene(pos.toPoint()))
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

    def _drag_rotate(self, drag: dict[str, Any], point: QPointF, snap: bool) -> None:
        center, press = drag["center"], drag["press"]
        start = math.degrees(math.atan2(press.y() - center.y(), press.x() - center.x()))
        now = math.degrees(math.atan2(point.y() - center.y(), point.x() - center.x()))
        (item_id, state), = drag["before"].items()
        angle = state.rotation + now - start
        if snap:
            angle = round(angle / 15.0) * 15.0
        self.apply_states({item_id: replace(state, rotation=angle % 360.0)})

    def hover_cursor(self, pos: QPointF) -> QCursor:
        """What the cursor shows over pos: resize on corners (pointing the way that corner
        scales, rotation included), rotate on the round handle, move over images."""
        if self._space_down:
            return QCursor(Qt.CursorShape.OpenHandCursor)
        handle = self._handle_at(pos)
        if handle is not None and handle[0] == "rotate":
            return rotate_cursor()
        if handle is not None:
            corners = [self.mapFromScene(c) for c in self._frame_corners()]
            corner, opposite = corners[handle[1]], corners[(handle[1] + 2) % 4]
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
        if drag and drag["mode"] in ("scale", "rotate"):
            self._push_states(drag["before"], drag["mode"])
            return
        super().mouseReleaseEvent(event)
        if drag and drag["mode"] == "move":
            self._push_states(drag["before"], "move")

    def mouseDoubleClickEvent(self, event) -> None:
        item = self._image_at(event.position())
        if item is not None and event.button() == Qt.MouseButton.LeftButton:
            self.frame([item])
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = self.context_menu(self.mapToScene(event.pos()))
        menu.exec(event.globalPos())
        menu.deleteLater()  # its actions carry shortcuts: don't leave them around

    def context_menu(self, at: QPointF) -> QMenu:
        """The right-click menu; what can't be done right now (e.g. nothing selected) is greyed out."""
        menu = QMenu(self)
        has_selection = bool(self.selected())
        entries = [
            ("Paste", "artbook.paste", lambda: self.paste(at), self.can_paste()),
            ("Copy Image", "artbook.copy", self.copy_selection, has_selection),
            None,
            ("Flip Horizontally", "artbook.flip_h", lambda: self.flip(True), has_selection),
            ("Flip Vertically", "artbook.flip_v", lambda: self.flip(False), has_selection),
            ("Reset Rotation, Flips and Size", "artbook.reset", self.reset_transform, has_selection),
            None,
            ("Bring to Front", "artbook.front", lambda: self.restack(True), has_selection),
            ("Send to Back", "artbook.back", lambda: self.restack(False), has_selection),
            None,
            ("Frame Everything" if not has_selection else "Frame Selection", "artbook.fit", self.frame, bool(self._items)),
            ("Select All", "artbook.select_all", self.select_all, bool(self._items)),
            ("Delete", "artbook.delete", self.delete_selection, has_selection),
        ]
        for entry in entries:
            if entry is None:
                menu.addSeparator()
                continue
            text, shortcut_id, handler, enabled = entry
            action = menu.addAction(text)
            action.setShortcut(shortcuts.sequence(shortcut_id))  # shown as a hint; the board handles the key
            action.setEnabled(enabled)
            # Called with no arguments: triggered passes `checked`, which frame() would take as its items
            action.triggered.connect(lambda _checked=False, handler=handler: handler())
        return menu

    # ------------------ keys ------------------
    def _key_id(self, event: QKeyEvent) -> Optional[str]:
        return next((key for key in self.KEYS if shortcuts.matches(key, event)), None)

    def event(self, event) -> bool:
        # Claim the board's keys before window shortcuts (e.g. the library's Ctrl+Z) see them
        if event.type() == QEvent.Type.ShortcutOverride and self._key_id(event):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = self._key_id(event)
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
