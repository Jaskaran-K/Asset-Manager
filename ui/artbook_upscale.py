"""The Artbook's Upscale window: the selected images made bigger by Real-ESRGAN (core.upscaler), compared with
their originals before anything changes on the board.

The list has the images; Upscale does the ones selected in it with the model and size chosen (so images can
differ), one at a time in the background. The comparison shows the original as the board shows it zoomed
(left of the line) and the upscaled picture (right): drag the line, zoom with the wheel, drag to move.
Use Upscaled Pictures puts the ticked ones on the board as one undo step; each image keeps its original
(Images > Revert to Original). An image upscaled again starts from its original, not from the last result.
"""

from __future__ import annotations

import io
from typing import Any, Optional

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import artbook_store as store
from core import upscaler
from core.archives import Cancelled
from core.settings import get_upscale_settings, set_upscale_settings
from ui.archive_jobs import UnpackJob, unpack_in_background
from ui.artbook_board import BASE_SIDE, BoardView, ImageItem, _decode_saved
from ui.duotone import set_icon
from ui.tag_widgets import run_job
from ui.themes import theme_color

MIN_ZOOM, MAX_ZOOM = 1.0, 16.0
HANDLE_REACH = 10  # pixels either side of the line that grab it


# ----------------------------------------------------------------------
# The work (worker threads)
# ----------------------------------------------------------------------
def _pil(image: QImage) -> Image.Image:
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    data = bytes(image.constBits())[: image.sizeInBytes()]
    return Image.frombuffer("RGBA", (image.width(), image.height()), data, "raw", "RGBA", image.bytesPerLine(), 1).copy()


def upscale_picture(page_id: str, source: dict[str, Any], model: str, factor: int, progress, should_stop) -> dict[str, Any]:
    """Upscale an image's picture and store the result in its page: {"source", "size", "base" (a small
    copy, QImage)}. progress(done, total) as it goes; raises upscaler.UpscaleError, Cancelled."""
    picture = _pil(_decode_saved(page_id, source, upscaler.MAX_SIDE))
    result = upscaler.upscale(picture, model, factor, lambda share: progress(int(share * 1000), 1000), should_stop)
    if should_stop():
        raise Cancelled()
    ext = upscaler.disk_name(source.get("path") or source.get("blob"))
    if result.mode == "RGBA":
        ext = ".png"  # (transparency: JPEG can't keep it)
    buffer = io.BytesIO()
    if ext == ".jpg":
        result.save(buffer, "JPEG", quality=95)
    else:
        result.save(buffer, "PNG", compress_level=6)
    blob = store.store_blob(page_id, buffer.getvalue(), ext)
    size = result.size
    return {"source": {"path": None, "blob": blob}, "size": size, "base": store._to_qimage(result, BASE_SIDE)}  # pylint: disable=protected-access


def _load_pair(page_id: str, original: dict[str, Any], upscaled: Optional[dict[str, Any]]) -> tuple[QImage, Optional[QImage]]:
    before = _decode_saved(page_id, original, upscaler.MAX_SIDE)
    after = store.decode_file(store.blob_path(page_id, upscaled["blob"]), upscaler.MAX_SIDE)[0] if upscaled else None
    return before, after


# ----------------------------------------------------------------------
# The comparison
# ----------------------------------------------------------------------
class CompareView(QWidget):
    """The original left of a line, the upscaled picture right of it, in the same place. Drag the line
    to compare; the wheel zooms toward the cursor, dragging elsewhere moves, double-click fits."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setMinimumSize(420, 320)
        self.setMouseTracking(True)
        self.before: Optional[QPixmap] = None
        self.after: Optional[QPixmap] = None
        self.message = ""
        self.split = 0.5  # where the line is, as a share of the width
        self.zoom = 1.0
        self.center = QPointF(0.5, 0.5)  # the picture's point at the middle of the view (0..1 each way)
        self._drag: Optional[str] = None  # "line" or "move"
        self._last = QPointF()

    def set_pictures(self, before: Optional[QImage], after: Optional[QImage], message: str = "") -> None:
        self.before = QPixmap.fromImage(before) if before is not None and not before.isNull() else None
        self.after = QPixmap.fromImage(after) if after is not None and not after.isNull() else None
        self.message = message
        self.update()

    def set_message(self, message: str) -> None:
        self.message = message
        self.update()

    def fit(self) -> None:
        self.zoom, self.center = 1.0, QPointF(0.5, 0.5)
        self.update()

    def picture_rect(self) -> QRectF:
        """Where the picture is drawn (zoomed and moved), in the widget."""
        shown = self.after or self.before
        if shown is None:
            return QRectF()
        area = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        scale = min(area.width() / shown.width(), area.height() / shown.height()) * self.zoom
        w, h = shown.width() * scale, shown.height() * scale
        middle = area.center()
        return QRectF(middle.x() - self.center.x() * w, middle.y() - self.center.y() * h, w, h)

    def line_x(self) -> float:
        return self.width() * self.split

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        back = theme_color("surface_alt")
        p.fillRect(self.rect(), back if back.isValid() else QColor(30, 30, 34))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        target = self.picture_rect()
        if self.before is not None:
            line = self.line_x() if self.after is not None else float(self.width())
            p.save()
            p.setClipRect(QRectF(0, 0, line, self.height()))
            p.drawPixmap(target, self.before, QRectF(self.before.rect()))
            p.restore()
            if self.after is not None:
                p.save()
                p.setClipRect(QRectF(line, 0, self.width() - line, self.height()))
                p.drawPixmap(target, self.after, QRectF(self.after.rect()))
                p.restore()
                self._draw_line(p, line)
                self._draw_label(p, "Original", left=True)
                self._draw_label(p, "Upscaled", left=False)
        if self.message:
            p.setPen(theme_color("text_strong"))
            box = QRectF(self.rect()).adjusted(20, 20, -20, -20)
            if self.before is not None:
                box.setTop(box.bottom() - 40)
                back = QColor(0, 0, 0, 150)
                p.fillRect(box, back)
                p.setPen(QColor(240, 240, 245))
            p.drawText(box, int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), self.message)
        p.end()

    def _draw_line(self, p: QPainter, x: float) -> None:
        p.setPen(QPen(QColor(255, 255, 255, 230), 2))
        p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        handle = QPainterPath()
        handle.addEllipse(QPointF(x, self.height() / 2), 11, 11)
        p.fillPath(handle, QColor(255, 255, 255, 235))
        p.setPen(QPen(QColor(40, 40, 46), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        y = self.height() / 2
        for direction in (-1, 1):  # two small arrow heads: drag sideways
            tip = x + direction * 6
            p.drawLine(QPointF(tip, y), QPointF(tip - direction * 3.5, y - 3.5))
            p.drawLine(QPointF(tip, y), QPointF(tip - direction * 3.5, y + 3.5))

    def _draw_label(self, p: QPainter, text: str, left: bool) -> None:
        metrics = p.fontMetrics()
        w, h = metrics.horizontalAdvance(text) + 16, metrics.height() + 8
        x = 12 if left else self.width() - 12 - w
        box = QRectF(x, 12, w, h)
        path = QPainterPath()
        path.addRoundedRect(box, 5, 5)
        p.fillPath(path, QColor(0, 0, 0, 150))
        p.setPen(QColor(240, 240, 245))
        p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)

    # ------------------ mouse ------------------
    def _near_line(self, x: float) -> bool:
        return self.after is not None and abs(x - self.line_x()) <= HANDLE_REACH

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        self._drag = "line" if self._near_line(x) else "move"
        self._last = event.position()
        if self._drag == "move":
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        if self._drag == "line":
            self.split = min(1.0, max(0.0, pos.x() / max(1, self.width())))
            self.update()
        elif self._drag == "move":
            rect = self.picture_rect()
            if rect.width() > 0:
                delta = pos - self._last
                self.center = QPointF(self.center.x() - delta.x() / rect.width(), self.center.y() - delta.y() / rect.height())
                self._clamp()
                self.update()
            self._last = pos
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor if self._near_line(pos.x()) else Qt.CursorShape.OpenHandCursor)

    def mouseReleaseEvent(self, _event) -> None:
        self._drag = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, _event) -> None:
        self.fit()

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() / 120
        if not steps:
            return
        rect = self.picture_rect()
        if rect.width() <= 0:
            return
        pos = event.position()
        under = QPointF((pos.x() - rect.left()) / rect.width(), (pos.y() - rect.top()) / rect.height())
        zoom = min(MAX_ZOOM, max(MIN_ZOOM, self.zoom * (1.25 ** steps)))
        if zoom == self.zoom:
            return
        self.zoom = zoom
        after = self.picture_rect()  # (same center): keep the point under the cursor where it is
        moved = QPointF(after.left() + under.x() * after.width(), after.top() + under.y() * after.height()) - pos
        self.center = QPointF(self.center.x() + moved.x() / after.width(), self.center.y() + moved.y() / after.height())
        self._clamp()
        self.update()

    def _clamp(self) -> None:
        half = 0.5 / self.zoom
        self.center = QPointF(min(1 - half, max(half, self.center.x())), min(1 - half, max(half, self.center.y())))


# ----------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------
class UpscaleDialog(QDialog):
    """See the module docstring."""

    upscaled = Signal(int)  # how many pictures went on the board

    def __init__(self, board: BoardView, items: list[ImageItem]):
        super().__init__(board)
        self.board = board
        self.page_id = board.page_id
        self.setWindowTitle("Upscale Images")
        self.resize(1100, 720)
        self.rows: list[dict[str, Any]] = []
        for item in items:
            original = item.original
            source = dict(original["source"]) if original else dict(item.source)
            size = tuple(original["size"]) if original else item.natural
            eligible = upscaler.target_size(size, max(upscaler.FACTORS)) is not None
            self.rows.append({
                "item": item, "source": source, "size": size, "eligible": eligible, "result": None,
                "made_with": "", "status": "Not upscaled yet" if eligible else "Big enough already",
            })
        self._queue: list[int] = []
        self._running: Optional[int] = None
        self._job: Optional[UnpackJob] = None
        self._downloading = False
        self._shown = -1
        self._load_token = 0
        upscaler.clean_up()
        self._build()
        self._fill_list()
        self._refresh()
        self.list.setCurrentRow(next((i for i, row in enumerate(self.rows) if row["eligible"]), 0))
        self.list.selectAll()

    # ------------------ building ------------------
    def _build(self) -> None:
        settings = get_upscale_settings()
        layout = QVBoxLayout(self)
        middle = QHBoxLayout()
        layout.addLayout(middle, 1)
        self.list = QListWidget(self)
        self.list.setIconSize(QSize(56, 56))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setFixedWidth(300)
        self.list.setWordWrap(True)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setToolTip("Select the images to upscale (Ctrl or Shift for several). Tick the ones to use")
        self.list.currentRowChanged.connect(self._show_row)
        self.list.itemSelectionChanged.connect(self._refresh)
        self.list.itemChanged.connect(self._on_item_changed)
        middle.addWidget(self.list)
        right = QVBoxLayout()
        middle.addLayout(right, 1)
        self.compare = CompareView(self)
        right.addWidget(self.compare, 1)
        hint = QLabel("Drag the line to compare. Mouse wheel zooms, drag to move, double-click to fit.", self)
        hint.setObjectName("mutedLabel")
        right.addWidget(hint)

        options = QHBoxLayout()
        layout.addLayout(options)

        def heading(text: str) -> None:
            label = QLabel(text, self)
            label.setObjectName("toolbarLabel")
            options.addWidget(label)

        def choices(values: list[tuple[Any, str, str]], current: Any) -> QButtonGroup:
            group = QButtonGroup(self)
            group.setExclusive(True)
            for value, text, tip in values:
                button = QPushButton(text, self)
                button.setCheckable(True)
                button.setChecked(value == current)
                button.setToolTip(tip)
                button.setProperty("value", value)
                group.addButton(button)
                options.addWidget(button)
            return group

        heading("Kind of picture")
        self.model_choice = choices([(key, name, tip) for key, (_model, name, tip) in upscaler.MODELS.items()], settings["model"])
        options.addSpacing(14)
        heading("Size")
        self.factor_choice = choices([
            (2, "2x", "Twice as big: gentler, and half the disk space"),
            (4, "4x", "Four times as big: the most detail"),
        ], settings["factor"])
        self.model_choice.buttonClicked.connect(self._on_option_picked)
        self.factor_choice.buttonClicked.connect(self._on_option_picked)
        options.addStretch(1)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar(self)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setRange(0, 1000)
        self.progress.hide()
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        self.btn_upscale = QPushButton("Upscale", self)
        set_icon(self.btn_upscale, "upscale")
        self.btn_upscale.clicked.connect(self._on_upscale)
        buttons.addWidget(self.btn_upscale)
        self.btn_stop = QPushButton("Stop", self)
        self.btn_stop.setToolTip("Stop upscaling (pictures already done are kept)")
        self.btn_stop.clicked.connect(self._on_stop)
        buttons.addWidget(self.btn_stop)
        buttons.addStretch(1)
        self.btn_remove = QPushButton("Remove Real-ESRGAN", self)
        self.btn_remove.setToolTip("Delete the downloaded Real-ESRGAN (it can be downloaded again)")
        self.btn_remove.clicked.connect(self._on_remove)
        buttons.addWidget(self.btn_remove)
        self.btn_apply = QPushButton("Use Upscaled Pictures", self)
        self.btn_apply.setToolTip("Put the ticked upscaled pictures on the board (one Ctrl+Z; Images > Revert to Original any time)")
        self.btn_apply.clicked.connect(self._on_apply)
        buttons.addWidget(self.btn_apply)
        close = QPushButton("Cancel", self)
        close.setToolTip("Close without changing the board")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)

    def _fill_list(self) -> None:
        self.list.blockSignals(True)
        for row in self.rows:
            entry = QListWidgetItem(self.list)
            base = row["item"].base
            if base is not None:
                entry.setIcon(QIcon(base))
            self._show_status(entry, row)
        self.list.blockSignals(False)

    def _show_status(self, entry: QListWidgetItem, row: dict[str, Any]) -> None:
        self.list.blockSignals(True)
        w, h = row["size"]
        entry.setText(f"{row['item'].display_name()}\n{w} x {h}\n{row['status']}")
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if row["result"] is not None:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        entry.setFlags(flags if row["eligible"] else Qt.ItemFlag.ItemIsEnabled)
        if row["result"] is not None and entry.data(Qt.ItemDataRole.CheckStateRole) is None:
            entry.setCheckState(Qt.CheckState.Checked)
        elif row["result"] is None:
            entry.setData(Qt.ItemDataRole.CheckStateRole, None)
        self.list.blockSignals(False)

    # ------------------ state ------------------
    def _value(self, group: QButtonGroup) -> Any:
        return group.checkedButton().property("value")

    def model(self) -> str:
        return self._value(self.model_choice)

    def factor(self) -> int:
        return int(self._value(self.factor_choice))

    def busy(self) -> bool:
        return self._job is not None

    def _selected_rows(self) -> list[int]:
        return sorted(i for i in range(self.list.count()) if self.list.item(i).isSelected() and self.rows[i]["eligible"])

    def chosen(self) -> dict[str, dict[str, Any]]:
        """The upscaled pictures ticked to use: {item id: result}."""
        return {
            row["item"].item_id: row["result"] for i, row in enumerate(self.rows)
            if row["result"] is not None and self.list.item(i).checkState() == Qt.CheckState.Checked
        }

    def _refresh(self) -> None:
        busy = self.busy()
        for group in (self.model_choice, self.factor_choice):
            for button in group.buttons():
                button.setEnabled(not busy)
        installed = upscaler.is_installed()
        count = len(self._selected_rows())
        if not installed:
            self.btn_upscale.setText(f"Download Real-ESRGAN ({upscaler.DOWNLOAD.size / 1e6:.0f} MB) and Upscale")
            self.btn_upscale.setToolTip("Download Real-ESRGAN once (free), then upscale the selected images")
        else:
            self.btn_upscale.setText(f"Upscale {count} Image{'s' if count != 1 else ''}" if count != 1 else "Upscale")
            self.btn_upscale.setToolTip("Upscale the images selected in the list, with the kind of picture and size chosen")
        self.btn_upscale.setEnabled(not busy and count > 0 and upscaler.supported())
        self.btn_stop.setEnabled(busy)
        self.btn_remove.setVisible(installed)
        self.btn_remove.setEnabled(not busy)
        ready = len(self.chosen())
        self.btn_apply.setText(f"Use {ready} Upscaled Picture{'s' if ready != 1 else ''}" if ready else "Use Upscaled Pictures")
        self.btn_apply.setEnabled(not busy and ready > 0)
        if not busy and not self.status.text():
            if not upscaler.supported():
                self.status.setText("Upscaling is only available on Windows.")
            elif not installed:
                self.status.setText(
                    f"Real-ESRGAN is free and downloads once ({upscaler.DOWNLOAD.size / 1e6:.0f} MB). It runs on the "
                    "graphics card. Upscaling adds believable detail, not the real thing, so compare before using a picture."
                )

    def _say(self, text: str) -> None:
        self.status.setText(text)

    def _on_option_picked(self, _button) -> None:
        set_upscale_settings(model=self.model(), factor=self.factor())

    def _on_item_changed(self, _entry: QListWidgetItem) -> None:
        self._refresh()

    # ------------------ the comparison ------------------
    def _show_row(self, index: int) -> None:
        if not 0 <= index < len(self.rows):
            return
        row = self.rows[index]
        self._shown = index
        self._load_token += 1
        token = self._load_token
        self.compare.set_pictures(None, None, "Loading...")
        page_id, source, result = self.page_id, row["source"], row["result"]
        run_job(
            lambda: _load_pair(page_id, source, result["source"] if result else None),
            lambda pair: self._on_pair_loaded(token, index, pair),
            lambda error: self._on_pair_loaded(token, index, None, error),
        )

    def _on_pair_loaded(self, token: int, index: int, pair, error: str = "") -> None:
        if token != self._load_token:
            return
        if pair is None:
            self.compare.set_pictures(None, None, f"It couldn't be read: {error}")
            return
        before, after = pair
        row = self.rows[index]
        message = "" if after is not None else (row["status"] if row["eligible"] else "It's big enough already.")
        self.compare.set_pictures(before, after, message)

    # ------------------ upscaling ------------------
    def _on_upscale(self) -> None:
        self._queue = self._selected_rows()
        if not self._queue:
            return
        for index in self._queue:
            self.rows[index]["status"] = "Waiting..."
            self._show_status(self.list.item(index), self.rows[index])
        if not upscaler.is_installed():
            self._start_download()
            return
        self._run_next()

    def _made_with(self) -> str:
        return f"{upscaler.MODELS[self.model()][1]}, {self.factor()}x"

    def _start_download(self) -> None:
        self._downloading = True
        self.progress.setValue(0)
        self.progress.show()
        self._say("Downloading Real-ESRGAN...")
        self._job = UnpackJob()
        unpack_in_background(upscaler.install, self._on_downloaded, self._on_download_failed, self._on_download_progress, self._job)
        self._refresh()

    def _on_download_progress(self, done: int, total: int) -> None:
        self.progress.setValue(int(1000 * done / max(total, 1)))
        self._say(f"Downloading Real-ESRGAN: {done / 1e6:.0f} of {total / 1e6:.0f} MB")

    def _on_downloaded(self, _result: object) -> None:
        self._job = None
        self._downloading = False
        self._say("Real-ESRGAN is ready.")
        self._run_next()

    def _on_download_failed(self, message: str) -> None:
        self._job = None
        self._downloading = False
        self.progress.hide()
        self._say(message)
        self._reset_waiting()
        self._refresh()

    def _run_next(self) -> None:
        if not self._queue:
            self._running = None
            self.progress.hide()
            self._refresh()
            return
        index = self._queue.pop(0)
        self._running = index
        row = self.rows[index]
        row["status"] = "Upscaling..."
        self._show_status(self.list.item(index), row)
        self.progress.setValue(0)
        self.progress.show()
        left = len(self._queue)
        self._say(f"Upscaling {row['item'].display_name()}" + (f" ({left} more after it)" if left else "") + "...")
        page_id, source, model, factor = self.page_id, row["source"], self.model(), self.factor()
        made_with = self._made_with()
        self._job = UnpackJob()
        unpack_in_background(
            lambda progress, stop: upscale_picture(page_id, source, model, factor, progress, stop),
            lambda result: self._on_upscaled(index, made_with, result),
            lambda message: self._on_failed(index, message),
            lambda done, total: self._on_progress(index, done, total),
            self._job,
        )
        self._refresh()

    def _on_progress(self, index: int, done: int, total: int) -> None:
        if index != self._running:
            return
        self.progress.setValue(int(1000 * done / max(total, 1)))
        row = self.rows[index]
        row["status"] = f"Upscaling... {100 * done // max(total, 1)}%"
        self._show_status(self.list.item(index), row)

    def _on_upscaled(self, index: int, made_with: str, result: dict[str, Any]) -> None:
        self._job = None
        row = self.rows[index]
        row["result"], row["made_with"] = result, made_with
        row["status"] = self._done_text(row)
        self._show_status(self.list.item(index), row)
        self.list.item(index).setCheckState(Qt.CheckState.Checked)
        if index == self.list.currentRow():
            self._show_row(index)
        if not self._queue:
            done = sum(1 for r in self.rows if r["result"] is not None)
            self._say(f"Done. Compare each one, untick any you don't want, then Use Upscaled Pictures. ({done} ready)")
        self._run_next()

    def _on_failed(self, index: int, message: str) -> None:
        self._job = None
        row = self.rows[index]
        row["status"] = "Couldn't upscale"
        self._show_status(self.list.item(index), row)
        self._say(message)
        self._reset_waiting()  # (the same thing would stop the rest: a missing graphics card, say)
        self.progress.hide()
        self._running = None
        self._refresh()

    @staticmethod
    def _done_text(row: dict[str, Any]) -> str:
        """A row's status when nothing's happening to it: what it was upscaled with, or not yet."""
        if row["result"] is None:
            return "Not upscaled yet"
        w, h = row["result"]["size"]
        return f"{row['made_with']}: {w} x {h}"

    def _reset_waiting(self) -> None:
        for index in self._queue:
            row = self.rows[index]
            row["status"] = self._done_text(row)
            self._show_status(self.list.item(index), row)
        self._queue = []

    def _on_stop(self) -> None:
        if self._job is not None:
            self._job.cancel()
            self._job = None
        if self._running is not None:
            row = self.rows[self._running]
            row["status"] = self._done_text(row)
            self._show_status(self.list.item(self._running), row)
            self._running = None
        self._reset_waiting()
        self._downloading = False
        self.progress.hide()
        self._say("Stopped.")
        self._refresh()

    def _on_remove(self) -> None:
        upscaler.remove()
        self._say("")
        self._refresh()

    # ------------------ finishing ------------------
    def _on_apply(self) -> None:
        chosen = self.chosen()
        if not chosen or self.board.page_id != self.page_id:
            return
        count = self.board.apply_upscales(chosen)
        self.board.show_message(
            f"Upscaled {count} image{'s' if count != 1 else ''}. Ctrl+Z, or Images > Revert to Original, brings the original back."
        )
        self.upscaled.emit(count)
        self.accept()

    def done(self, result: int) -> None:
        if self._job is not None:
            self._job.cancel()
            self._job = None
        if self.board.upscale_dialog is self:
            self.board.upscale_dialog = None
        super().done(result)
