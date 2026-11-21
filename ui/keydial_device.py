"""KeyDial's picture of the Huion KD100, as it is: the dial in its grey panel, and the 18 keys
(rows of four, then three and the tall K16, then the wide K17 and K18). Each key shows its name
and what it does; click one (or the dial) to pick it.

The device's own colours are drawn as they are (it's black whatever the theme); only the
selection, hover and pressed glow use the theme's accent.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from core.keydial.protocol import DIAL_BUTTON
from ui.themes import theme_color

# (column, row, columns wide, rows tall) for K1..K18
KEY_CELLS = [(c, r, 1, 1) for r in range(3) for c in range(4)] + [
    (0, 3, 1, 1), (1, 3, 1, 1), (2, 3, 1, 1), (3, 3, 1, 2), (0, 4, 2, 1), (2, 4, 1, 1)]
GAP = 0.16  # (of a key's width)
PAD = 0.42
TOP = 1.75  # (the dial panel's height, in keys)


def _units() -> tuple[float, float]:
    """The device's size in key widths."""
    width = 4 + 3 * GAP + 2 * PAD + 2 * GAP
    height = TOP + 5 + 4 * GAP + 2 * PAD + 2 * GAP
    return width, height


class KD100View(QWidget):
    """picked(button): "K1".."K18" or "DIAL"."""

    picked = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(260, 380)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._labels: dict[str, tuple[str, str, bool]] = {}  # button -> (name, what it does, from Default)
        self._dial_text = ("", "")
        self._selected: Optional[str] = None
        self._hover: Optional[str] = None
        self._pressed: set[str] = set()

    # -- what it shows ---------------------------------------------------------------------------
    def set_labels(self, labels: dict[str, tuple[str, str, bool]]) -> None:
        self._labels = dict(labels)
        self.update()

    def set_dial_text(self, title: str, sub: str) -> None:
        self._dial_text = (title, sub)
        self.update()

    def set_selected(self, button: Optional[str]) -> None:
        self._selected = button
        self.update()

    def selected(self) -> Optional[str]:
        return self._selected

    def set_pressed(self, button: str, down: bool) -> None:
        if down:
            self._pressed.add(button)
        else:
            self._pressed.discard(button)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(380, 560)

    # -- where things are ------------------------------------------------------------------------
    def _unit(self) -> tuple[float, float, float]:
        """(key width, left, top) so the device fits the widget, centred."""
        w_units, h_units = _units()
        unit = min((self.width() - 8) / w_units, (self.height() - 8) / h_units)
        return unit, (self.width() - unit * w_units) / 2, (self.height() - unit * h_units) / 2

    def body_rect(self) -> QRectF:
        unit, x, y = self._unit()
        w_units, h_units = _units()
        return QRectF(x, y, unit * w_units, unit * h_units)

    def dial_circle(self) -> tuple[QPointF, float]:
        unit, x, y = self._unit()
        panel = self._panel(unit, x, y)
        return QPointF(panel.left() + panel.height() / 2, panel.center().y()), panel.height() / 2 + unit * 0.04

    def _panel(self, unit: float, x: float, y: float) -> QRectF:
        w_units, _ = _units()
        return QRectF(x + PAD * unit, y + PAD * unit * 0.8, (w_units - 2 * PAD) * unit, (TOP - PAD * 0.6) * unit)

    def key_rect(self, index: int) -> QRectF:
        """K(index + 1)'s rectangle."""
        unit, x, y = self._unit()
        gap = GAP * unit
        left = x + PAD * unit + gap
        top = y + TOP * unit + PAD * unit * 0.6 + gap
        col, row, cols, rows = KEY_CELLS[index]
        return QRectF(left + col * (unit + gap), top + row * (unit + gap), cols * unit + (cols - 1) * gap,
                      rows * unit + (rows - 1) * gap)

    def button_at(self, pos: QPointF) -> Optional[str]:
        center, radius = self.dial_circle()
        if (pos.x() - center.x()) ** 2 + (pos.y() - center.y()) ** 2 <= radius ** 2:
            return DIAL_BUTTON
        for n in range(18):
            if self.key_rect(n).contains(pos):
                return f"K{n + 1}"
        return None

    # -- the mouse -------------------------------------------------------------------------------
    def mouseMoveEvent(self, event) -> None:
        hover = self.button_at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover else Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            button = self.button_at(event.position())
            if button:
                self.picked.emit(button)
                return
        super().mousePressEvent(event)

    def event(self, event) -> bool:
        if event.type() == event.Type.ToolTip:
            button = self.button_at(QPointF(event.pos()))
            if button == DIAL_BUTTON:
                QToolTip.showText(event.globalPos(), "The dial: click to set its modes and what pressing it does", self)
            elif button:
                name, does, inherited = self._labels.get(button, ("", "", False))
                tip = f"Key {button[1:]}" + (f": {name}" if name else "") + (f" ({does})" if does else "")
                QToolTip.showText(event.globalPos(), tip + (" - from Default" if inherited else "") + ". Click to change it", self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    # -- painting --------------------------------------------------------------------------------
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        unit, x, y = self._unit()
        accent = theme_color("accent")
        body = self.body_rect()
        for n in range(8, 0, -2):  # (a soft shadow)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 14))
            p.drawRoundedRect(body.adjusted(-n, -n + 4, n, n + 4), unit * 0.62 + n, unit * 0.62 + n)
        grad = QLinearGradient(0, body.top(), 0, body.bottom())
        grad.setColorAt(0, QColor("#2a2c30"))
        grad.setColorAt(1, QColor("#1b1c1f"))
        p.setPen(QPen(QColor("#3a3d42"), 1.2))
        p.setBrush(grad)
        p.drawRoundedRect(body, unit * 0.62, unit * 0.62)
        self._paint_dial(p, unit, x, y, accent)
        well = QRectF(x + PAD * unit, y + TOP * unit + PAD * unit * 0.6, (4 + 5 * GAP) * unit, (5 + 6 * GAP) * unit)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#232528"))
        p.drawRoundedRect(well, unit * 0.28, unit * 0.28)
        for n in range(18):
            self._paint_key(p, n, unit, accent)
        p.end()

    def _paint_dial(self, p: QPainter, unit: float, x: float, y: float, accent: QColor) -> None:
        panel = self._panel(unit, x, y)
        grad = QLinearGradient(0, panel.top(), 0, panel.bottom())
        grad.setColorAt(0, QColor("#6a6d72"))
        grad.setColorAt(1, QColor("#55585d"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(panel, panel.height() / 2, panel.height() / 2)
        center, radius = self.dial_circle()
        selected = self._selected == DIAL_BUTTON
        if selected or self._hover == DIAL_BUTTON or DIAL_BUTTON in self._pressed:
            glow = QColor(accent)
            glow.setAlphaF(0.35 if selected else 0.2)
            p.setBrush(glow)
            p.drawEllipse(center, radius + 5, radius + 5)
        dial = QRadialGradient(QPointF(center.x() - radius * 0.3, center.y() - radius * 0.4), radius * 1.5)
        dial.setColorAt(0, QColor("#8c8f94"))
        dial.setColorAt(1, QColor("#45484d"))
        p.setBrush(dial)
        p.setPen(QPen(accent, 3) if selected else QPen(QColor("#9a9da2"), 1.2))
        p.drawEllipse(center, radius, radius)
        knob = QRadialGradient(QPointF(center.x() - 4, center.y() - 5), radius * 0.4)
        knob.setColorAt(0, QColor("#74777c") if DIAL_BUTTON not in self._pressed else QColor("#9a9da2"))
        knob.setColorAt(1, QColor("#3c3f44"))
        p.setBrush(knob)
        p.setPen(QPen(QColor("#2e3034"), 1.5))
        p.drawEllipse(center, radius * 0.36, radius * 0.36)
        title, sub = self._dial_text
        left = center.x() + radius + unit * 0.3
        room = QRectF(left, center.y() - unit * 0.42, panel.right() - left - unit * 0.25, unit * 0.4)
        p.setFont(self._font(unit * 0.2, QFont.Weight.Bold))
        p.setPen(QColor("#f2f3f5"))
        p.drawText(room, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self._fit(p, title, room.width()))
        p.setFont(self._font(unit * 0.16))
        p.setPen(QColor("#d4d6da"))
        below = QRectF(left, center.y(), room.width(), unit * 0.4)
        p.drawText(below, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self._fit(p, sub, below.width()))

    def _paint_key(self, p: QPainter, n: int, unit: float, accent: QColor) -> None:
        button = f"K{n + 1}"
        key = self.key_rect(n)
        down = button in self._pressed
        selected = self._selected == button
        if selected or down:
            glow = QColor(accent)
            glow.setAlphaF(0.35 if selected else 0.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawRoundedRect(key.adjusted(-4, -4, 4, 4), unit * 0.24, unit * 0.24)
        grad = QLinearGradient(0, key.top(), 0, key.bottom())
        top = QColor("#5a5e64") if (down or self._hover == button) else QColor("#4a4d52")
        grad.setColorAt(0, top)
        grad.setColorAt(1, QColor("#35383c"))
        p.setPen(QPen(accent, 2.6) if selected else QPen(QColor("#1a1b1e"), 1))
        p.setBrush(grad)
        p.drawRoundedRect(key, unit * 0.18, unit * 0.18)
        name, does, inherited = self._labels.get(button, ("", "", False))
        inner = key.adjusted(4, 0, -4, 0)
        p.setFont(self._font(unit * 0.17, QFont.Weight.DemiBold))
        p.setPen(QColor("#eef0f3") if not inherited else QColor("#b9bcc1"))
        p.drawText(inner.adjusted(0, 0, 0, -key.height() * 0.14), int(Qt.AlignmentFlag.AlignCenter), self._fit(p, name, inner.width()))
        p.setFont(self._font(unit * 0.135))
        p.setPen(QColor("#a7abb1") if not inherited else QColor("#80848a"))
        p.drawText(inner.adjusted(0, key.height() * 0.36, 0, 0) if key.height() < unit * 1.5 else inner.adjusted(0, unit * 0.36, 0, 0),
                   int(Qt.AlignmentFlag.AlignCenter), self._fit(p, does, inner.width()))
        if inherited:  # (a small dot: it does what Default does)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#80848a"))
            p.drawEllipse(QPointF(key.right() - 7, key.top() + 7), 2.2, 2.2)

    @staticmethod
    def _font(pixels: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
        font = QFont()
        font.setPixelSize(max(8, int(pixels)))
        font.setWeight(weight)
        return font

    @staticmethod
    def _fit(p: QPainter, text: str, width: float) -> str:
        return QFontMetricsF(p.font()).elidedText(text, Qt.TextElideMode.ElideRight, width)
