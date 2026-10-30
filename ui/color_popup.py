"""The custom colour picker: a small floating window that changes a colour live while you pick it.
It opens beside what it colours (not on top of it) and moves by dragging its title.

A saturation / brightness square, a hue slider and an opacity slider, a dropper that takes a
colour from anywhere on screen, the colour as HEX / RGB / HSL text (with a copy button), the
opacity in percent, and a row of ready-made colours (the first one is the theme's default).

`changed` fires on every change (the caller applies it straight away), `default_picked` when the
theme-default swatch is clicked, and `finished` once the window closes.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QKeySequence, QScreen, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.duotone import set_icon
from ui.themes import theme_color

PALETTE = (
    ("Black", "#111111"), ("Dark Gray", "#374151"), ("Gray", "#6b7280"), ("Light Gray", "#d1d5db"),
    ("White", "#ffffff"), ("Red", "#ef4444"), ("Orange", "#f97316"), ("Amber", "#f59e0b"),
    ("Yellow", "#facc15"), ("Lime", "#84cc16"), ("Green", "#22c55e"), ("Teal", "#14b8a6"),
    ("Cyan", "#06b6d4"), ("Blue", "#3b82f6"), ("Indigo", "#6366f1"), ("Purple", "#a855f7"),
    ("Pink", "#ec4899"),
)
MODES = ("HEX", "RGB", "HSL")
HANDLE_R = 6.5
GAP = 12  # between the picker and what it mustn't cover


def color_text(color: QColor) -> str:
    """A colour as stored on the board: #rrggbb, or #aarrggbb when it's see-through."""
    return color.name(QColor.NameFormat.HexArgb) if color.alpha() < 255 else color.name()


def _checker(painter: QPainter, rect: QRectF, size: float = 5.0) -> None:
    """The grey and white squares that show through a see-through colour."""
    painter.fillRect(rect, QColor("#ffffff"))
    y, row = rect.top(), 0
    while y < rect.bottom():
        x = rect.left() + (size if row % 2 else 0)
        while x < rect.right():
            painter.fillRect(QRectF(x, y, size, size).intersected(rect), QColor("#c8c8c8"))
            x += size * 2
        y += size
        row += 1


def _handle(painter: QPainter, center: QPointF, fill: Optional[QColor]) -> None:
    painter.setPen(QPen(QColor(0, 0, 0, 90), 3.5))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(center, HANDLE_R, HANDLE_R)
    painter.setPen(QPen(QColor("#ffffff"), 2))
    painter.setBrush(fill if fill is not None else Qt.BrushStyle.NoBrush)
    painter.drawEllipse(center, HANDLE_R, HANDLE_R)


class _SaturationValue(QWidget):
    """Left to right: grey to the full colour. Top to bottom: bright to black."""

    moved = Signal(float, float)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.hue, self.sat, self.val = 0.0, 0.0, 1.0
        self.setFixedHeight(190)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("Drag to pick how strong and how bright the colour is")

    def _area(self) -> QRectF:
        return QRectF(self.rect()).adjusted(HANDLE_R, HANDLE_R, -HANDLE_R, -HANDLE_R)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self._area()
        clip = QPainterPath()
        clip.addRoundedRect(area, 5, 5)
        painter.save()
        painter.setClipPath(clip)
        painter.fillRect(area, QColor.fromHsvF(self.hue, 1.0, 1.0))
        across = QLinearGradient(area.topLeft(), area.topRight())
        across.setColorAt(0, QColor(255, 255, 255, 255))
        across.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillRect(area, across)
        down = QLinearGradient(area.topLeft(), area.bottomLeft())
        down.setColorAt(0, QColor(0, 0, 0, 0))
        down.setColorAt(1, QColor(0, 0, 0, 255))
        painter.fillRect(area, down)
        painter.restore()
        at = QPointF(area.left() + self.sat * area.width(), area.top() + (1 - self.val) * area.height())
        _handle(painter, at, QColor.fromHsvF(self.hue, self.sat, self.val))

    def _drag(self, pos: QPointF) -> None:
        area = self._area()
        sat = min(1.0, max(0.0, (pos.x() - area.left()) / area.width()))
        val = 1 - min(1.0, max(0.0, (pos.y() - area.top()) / area.height()))
        self.moved.emit(sat, val)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag(event.position())

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._drag(event.position())


class _Slider(QWidget):
    """A thin bar with a round handle: the hue (a rainbow) or the opacity (clear to solid)."""

    moved = Signal(float)

    def __init__(self, kind: str, tooltip: str, parent: QWidget):
        super().__init__(parent)
        self.kind = kind
        self.value = 0.0
        self.color = QColor("#ffffff")  # (opacity: the colour it fades to)
        self.setFixedHeight(int(HANDLE_R * 2 + 4))
        self.setToolTip(tooltip)

    def _bar(self) -> QRectF:
        middle = self.height() / 2
        return QRectF(HANDLE_R, middle - 5, self.width() - HANDLE_R * 2, 10)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = self._bar()
        clip = QPainterPath()
        clip.addRoundedRect(bar, 5, 5)
        painter.save()
        painter.setClipPath(clip)
        gradient = QLinearGradient(bar.topLeft(), bar.topRight())
        if self.kind == "hue":
            for i in range(7):
                gradient.setColorAt(i / 6, QColor.fromHsvF((i / 6) % 1.0, 1.0, 1.0))
        else:
            _checker(painter, bar)
            clear, solid = QColor(self.color), QColor(self.color)
            clear.setAlpha(0)
            solid.setAlpha(255)
            gradient.setColorAt(0, clear)
            gradient.setColorAt(1, solid)
        painter.fillRect(bar, gradient)
        painter.restore()
        at = QPointF(bar.left() + self.value * bar.width(), self.height() / 2)
        _handle(painter, at, QColor.fromHsvF(self.value % 1.0, 1.0, 1.0) if self.kind == "hue" else None)

    def _drag(self, x: float) -> None:
        bar = self._bar()
        self.moved.emit(min(1.0, max(0.0, (x - bar.left()) / bar.width())))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag(event.position().x())

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._drag(event.position().x())


class _Swatch(QToolButton):
    """A ready-made colour, split corner to corner into solid and see-through (None: the theme's
    default, drawn crossed out)."""

    def __init__(self, name: str, color: Optional[str], parent: QWidget):
        super().__init__(parent)
        self.color = color
        self.setToolTip(name)
        self.setFixedSize(22, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        ring = self.palette().color(self.foregroundRole())
        ring.setAlpha(230 if self.underMouse() else 90)
        if self.color:
            clip = QPainterPath()
            clip.addRoundedRect(box, 4, 4)
            painter.save()
            painter.setClipPath(clip)
            _checker(painter, box, 3.5)
            faint = QColor(self.color)
            faint.setAlpha(110)
            painter.fillRect(box, faint)
            solid = QPainterPath(box.topLeft())
            solid.lineTo(box.topRight())
            solid.lineTo(box.bottomLeft())
            solid.closeSubpath()
            painter.fillPath(solid, QColor(self.color))
            painter.restore()
        painter.setPen(QPen(ring, 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(box, 4, 4)
        if not self.color:
            painter.drawLine(box.bottomLeft() + QPointF(4, -4), box.topRight() + QPointF(-4, 4))


class ColorPopup(QFrame):
    """See the module docstring."""

    changed = Signal(QColor)
    default_picked = Signal()
    finished = Signal()

    def __init__(self, title: str, color: QColor, parent: QWidget):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("colorPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)  # (the rounded corners)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)  # typing in the note goes on
        self.setFixedWidth(280)
        start = QColor(color) if color is not None and color.isValid() else QColor("#ffffff")
        self._hue = max(0.0, start.hsvHueF())
        self._sat, self._val, self._alpha = start.hsvSaturationF(), start.valueF(), start.alphaF()
        self._dropping: Optional[QColor] = None  # the colour before the dropper, while it's out
        self.overlays: list[_ScreenShot] = []  # (the dropper's: one per screen)
        self._drag_from: Optional[QPoint] = None  # (moving the window by its title)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.addSpacing(26)
        self.title = QLabel(title)
        self.title.setObjectName("colorPopupTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # (dragging it moves the window)
        header.addWidget(self.title, 1)
        close = QToolButton()
        set_icon(close, "close", size=14)
        close.setToolTip("Close (Esc). Drag the title to move the picker")
        close.setFixedSize(26, 26)
        close.clicked.connect(self.close)
        header.addWidget(close)
        layout.addLayout(header)

        self.square = _SaturationValue(self)
        self.square.moved.connect(self._on_square)
        layout.addWidget(self.square)

        sliders = QHBoxLayout()
        sliders.setSpacing(8)
        stack = QVBoxLayout()
        stack.setSpacing(4)
        self.hue_slider = _Slider("hue", "Hue: drag to pick the colour", self)
        self.hue_slider.moved.connect(self._on_hue)
        self.alpha_slider = _Slider("alpha", "Opacity: drag left to make the colour see-through", self)
        self.alpha_slider.moved.connect(self._on_alpha)
        stack.addWidget(self.hue_slider)
        stack.addWidget(self.alpha_slider)
        sliders.addLayout(stack, 1)
        self.btn_dropper = QToolButton()
        set_icon(self.btn_dropper, "eyedropper", size=18)
        self.btn_dropper.setToolTip("Take a colour from anywhere on screen (click to pick, Esc to cancel)")
        self.btn_dropper.setFixedSize(30, 30)
        self.btn_dropper.clicked.connect(self.start_dropper)
        sliders.addWidget(self.btn_dropper)
        layout.addLayout(sliders)

        values = QHBoxLayout()
        values.setSpacing(4)
        self.mode_box = QComboBox()
        self.mode_box.addItems(MODES)
        self.mode_box.setToolTip("Show the colour as HEX, RGB or HSL")
        self.mode_box.setFixedWidth(64)
        self.mode_box.currentIndexChanged.connect(self._show_text)
        values.addWidget(self.mode_box)
        self.value_edit = QLineEdit()
        self.value_edit.setObjectName("colorPopupValue")
        self.value_edit.setToolTip("Type a colour and press Enter")
        self.value_edit.editingFinished.connect(self._on_text)
        values.addWidget(self.value_edit, 1)
        self.btn_copy = QToolButton()
        set_icon(self.btn_copy, "copy", size=16)
        self.btn_copy.setToolTip("Copy the colour")
        self.btn_copy.setFixedSize(26, 26)
        self.btn_copy.clicked.connect(self._copy)
        values.addWidget(self.btn_copy)
        self.opacity_box = QSpinBox()
        self.opacity_box.setRange(0, 100)
        self.opacity_box.setSuffix("%")
        self.opacity_box.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.opacity_box.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.opacity_box.setToolTip("Opacity (100% is solid)")
        self.opacity_box.setFixedWidth(58)
        self.opacity_box.valueChanged.connect(self._on_opacity_box)
        values.addWidget(self.opacity_box)
        layout.addLayout(values)

        swatches = QGridLayout()
        swatches.setSpacing(5)
        for i, (name, hex_color) in enumerate((("Theme Default", None),) + PALETTE):
            swatch = _Swatch(name, hex_color, self)
            swatch.clicked.connect(self._on_swatch)
            swatches.addWidget(swatch, i // 9, i % 9)
        layout.addLayout(swatches)

        self._show()

    # ------------------ the colour ------------------
    def color(self) -> QColor:
        return QColor.fromHsvF(self._hue, self._sat, self._val, self._alpha).toRgb()

    def set_color(self, color: QColor, emit: bool = False) -> None:
        if not color.isValid():
            return
        if color.hsvHueF() >= 0:  # (greys have no hue: keep the one picked)
            self._hue = color.hsvHueF()
        self._sat, self._val, self._alpha = color.hsvSaturationF(), color.valueF(), color.alphaF()
        self._changed(emit)

    def _changed(self, emit: bool = True) -> None:
        self._show()
        if emit:
            self.changed.emit(self.color())

    def _show(self) -> None:
        self.square.hue, self.square.sat, self.square.val = self._hue, self._sat, self._val
        self.square.update()
        self.hue_slider.value = self._hue
        self.hue_slider.update()
        self.alpha_slider.value = self._alpha
        self.alpha_slider.color = self.color()
        self.alpha_slider.update()
        self.opacity_box.blockSignals(True)
        self.opacity_box.setValue(round(self._alpha * 100))
        self.opacity_box.blockSignals(False)
        self._show_text()

    def _show_text(self) -> None:
        color, mode = self.color(), self.mode_box.currentText()
        if mode == "HEX":
            text = color.name()[1:].upper()
        elif mode == "RGB":
            text = f"{color.red()}, {color.green()}, {color.blue()}"
        else:
            hsl = color.toHsl()
            text = f"{max(0, hsl.hslHue())}, {round(hsl.hslSaturationF() * 100)}%, {round(hsl.lightnessF() * 100)}%"
        if not self.value_edit.hasFocus() or self.value_edit.text() != text:
            self.value_edit.setText(text)

    @staticmethod
    def parse(text: str, mode: str) -> Optional[QColor]:
        """Typed text as a colour (None if it isn't one). HEX takes 3 or 6 digits, with or without #."""
        text = text.strip()
        if mode == "HEX":
            digits = text.lstrip("#")
            if len(digits) in (3, 6) and all(c in "0123456789abcdefABCDEF" for c in digits):
                return QColor("#" + digits)
            return None
        parts = [p.strip().rstrip("%").strip() for p in text.replace(";", ",").split(",")]
        try:
            numbers = [float(p) for p in parts]
        except ValueError:
            return None
        if len(numbers) != 3:
            return None
        if mode == "RGB":
            r, g, b = (min(255, max(0, round(n))) for n in numbers)
            return QColor(r, g, b)
        h, s, lightness = numbers
        return QColor.fromHslF((h % 360) / 360, min(100, max(0, s)) / 100, min(100, max(0, lightness)) / 100).toRgb()

    # ------------------ handlers ------------------
    def _on_square(self, sat: float, val: float) -> None:
        self._sat, self._val = sat, val
        self._changed()

    def _on_hue(self, hue: float) -> None:
        self._hue = min(hue, 0.9999)  # (1.0 is red again)
        self._changed()

    def _on_alpha(self, alpha: float) -> None:
        self._alpha = alpha
        self._changed()

    def _on_opacity_box(self, percent: int) -> None:
        self._alpha = percent / 100
        self._changed()

    def _on_text(self) -> None:
        color = self.parse(self.value_edit.text(), self.mode_box.currentText())
        if color is None:
            self._show_text()  # not a colour: back to the current one
            return
        color.setAlphaF(self._alpha)
        if color.rgba() != self.color().rgba():
            self.set_color(color, emit=True)

    def _on_swatch(self) -> None:
        hex_color = self.sender().color
        if hex_color is None:
            self.default_picked.emit()
            return
        color = QColor(hex_color)
        color.setAlphaF(self._alpha)
        self.set_color(color, emit=True)

    def _copy(self) -> None:
        text = self.value_edit.text()
        QApplication.clipboard().setText("#" + text if self.mode_box.currentText() == "HEX" else text)

    # ------------------ the dropper ------------------
    def dropping(self) -> bool:
        return self._dropping is not None

    def start_dropper(self) -> None:
        """Cover each screen with a still picture of it: moving previews the colour under the cursor,
        a click keeps it, Esc (or a right-click) puts the old one back."""
        if self.dropping():
            return
        self._dropping = self.color()
        self.overlays = [_ScreenShot(screen, self) for screen in QGuiApplication.screens()]
        for overlay in self.overlays:
            overlay.show()
            overlay.raise_()
        under = QGuiApplication.screenAt(QCursor.pos())
        focus = next((o for o in self.overlays if o.screen_ref is under), self.overlays[0] if self.overlays else None)
        if focus is not None:
            focus.activateWindow()
            focus.setFocus()

    def preview(self, color: QColor) -> None:
        if self.dropping() and color.isValid():
            color = QColor(color)
            color.setAlphaF(self._alpha)
            if color.rgba() != self.color().rgba():
                self.set_color(color, emit=True)

    def stop_dropper(self, keep: bool) -> None:
        before, self._dropping = self._dropping, None
        overlays, self.overlays = self.overlays, []
        for overlay in overlays:
            overlay.close()
        if not keep and before is not None:
            self.set_color(before, emit=True)

    # ------------------ moving ------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < self.square.y():
            self._drag_from = event.globalPosition().toPoint() - self.pos()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_from is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_from = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Cancel):  # artbook.custom_color
            self.close()
            return
        super().keyPressEvent(event)

    # ------------------ showing ------------------
    def show_beside(self, avoid: QRect) -> None:
        """Open next to (not on top of) avoid, a rectangle on screen: to its right, else its left,
        else below or above it; kept on the screen."""
        self.adjustSize()
        screen = (QGuiApplication.screenAt(avoid.center()) or QGuiApplication.primaryScreen()).availableGeometry()
        w, h = self.width(), self.height()
        y = min(max(screen.top(), avoid.top()), screen.bottom() - h)
        if avoid.right() + GAP + w <= screen.right():
            x = avoid.right() + GAP
        elif avoid.left() - GAP - w >= screen.left():
            x = avoid.left() - GAP - w
        else:
            x = min(max(screen.left(), avoid.center().x() - w // 2), screen.right() - w)
            below = avoid.bottom() + GAP
            y = below if below + h <= screen.bottom() else max(screen.top(), avoid.top() - GAP - h)
        self.move(x, y)
        self.show()

    def paintEvent(self, _event) -> None:
        # (painted here: a see-through window, for the rounded corners, skips the stylesheet's background)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme_color("border"), 1))
        painter.setBrush(theme_color("surface"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)

    def closeEvent(self, event) -> None:
        if self.dropping():
            self.stop_dropper(keep=False)
        super().closeEvent(event)
        self.finished.emit()


class _ScreenShot(QWidget):
    """The dropper's cover over one screen: a still picture of it, to take colours from."""

    def __init__(self, screen: QScreen, picker: ColorPopup):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.screen_ref = screen
        self.picker = picker
        self.picture = screen.grabWindow(0)
        self.image: QImage = self.picture.toImage()
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def color_at(self, pos: QPoint) -> QColor:
        """The colour at a position on this cover (the picture may have more pixels: high-DPI screens)."""
        if self.image.isNull() or self.width() <= 0 or self.height() <= 0:
            return QColor()
        x = min(self.image.width() - 1, max(0, int(pos.x() * self.image.width() / self.width())))
        y = min(self.image.height() - 1, max(0, int(pos.y() * self.image.height() / self.height())))
        return self.image.pixelColor(x, y)

    def paintEvent(self, _event) -> None:
        QPainter(self).drawPixmap(self.rect(), self.picture)

    def mouseMoveEvent(self, event) -> None:
        self.picker.preview(self.color_at(event.position().toPoint()))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.picker.preview(self.color_at(event.position().toPoint()))
            self.picker.stop_dropper(keep=True)
        else:
            self.picker.stop_dropper(keep=False)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Cancel):  # artbook.color_dropper
            self.picker.stop_dropper(keep=False)
            return
        super().keyPressEvent(event)
