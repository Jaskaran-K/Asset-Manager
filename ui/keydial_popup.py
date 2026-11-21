"""KeyDial's pop-up: a pill at the bottom (or top) of the screen saying which profile is in use and
the dial's mode, shown for a moment when either changes, then faded out.

It never takes the focus (the app in front keeps its keys, and decides the profile) and the mouse
goes through it.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ui.duotone import duotone_icon
from ui.themes import theme_color

SHOW_MS = 1500
FADE_MS = 350


def _font(pixels: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont()
    font.setPixelSize(int(pixels))
    font.setWeight(weight)
    return font


class ProfilePopup(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.place = "bottom"
        self._profile = ""
        self._mode: Optional[str] = None
        self._index = 0
        self._count = 0
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(FADE_MS)
        self._fade.valueChanged.connect(self._set_opacity)
        self._fade.finished.connect(self._faded)

    def show_state(self, profile: str, mode: Optional[str], index: int, count: int) -> None:
        self._profile, self._mode, self._index, self._count = profile, mode, index, count
        self.resize(self._size())
        self._move_to_screen()
        self._fade.stop()
        self._set_opacity(1.0)
        self.show()
        self.raise_()
        self.update()
        self._hide_timer.start(SHOW_MS)

    def text(self) -> tuple[str, Optional[str], int, int]:
        return self._profile, self._mode, self._index, self._count

    def _size(self):
        name_w = QFontMetricsF(_font(15, QFont.Weight.Bold)).horizontalAdvance(self._profile)
        mode_w = QFontMetricsF(_font(14, QFont.Weight.DemiBold)).horizontalAdvance(f"Dial: {self._mode}") if self._mode else 0
        width = 20 + 18 + 10 + name_w + (30 + max(mode_w, 12 * self._count + 50) if self._mode else 0) + 26
        return QSize(int(max(180, width)) + 16, 52 + 16)

    def _move_to_screen(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = area.center().x() - self.width() // 2
        y = area.top() + 40 if self.place == "top" else area.bottom() - self.height() - 60
        self.move(x, y)

    def _set_opacity(self, value) -> None:
        self.setWindowOpacity(float(value))

    def _fade_out(self) -> None:
        self._fade.stop()
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _faded(self) -> None:
        if self.windowOpacity() <= 0.01:
            self.hide()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pill = QRectF(self.rect()).adjusted(8, 6, -8, -10)
        for n in range(6, 0, -2):  # (a soft shadow)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 18))
            p.drawRoundedRect(pill.adjusted(-n / 2, -n / 2 + 3, n / 2, n / 2 + 3), pill.height() / 2, pill.height() / 2)
        fill = theme_color("raised")
        fill.setAlphaF(0.97)
        p.setPen(QPen(theme_color("border"), 1))
        p.setBrush(fill)
        p.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        x = pill.left() + 18
        p.drawPixmap(int(x), int(pill.center().y() - 9), duotone_icon("keydial").pixmap(18, 18))
        x += 28
        p.setFont(_font(15, QFont.Weight.Bold))
        p.setPen(theme_color("text_strong"))
        name_w = QFontMetricsF(p.font()).horizontalAdvance(self._profile)
        p.drawText(QRectF(x, pill.top(), name_w + 2, pill.height()), int(Qt.AlignmentFlag.AlignVCenter), self._profile)
        x += name_w + 14
        if not self._mode:
            p.end()
            return
        p.fillRect(QRectF(x, pill.top() + 13, 1, pill.height() - 26), theme_color("border"))
        x += 14
        p.setFont(_font(14, QFont.Weight.DemiBold))
        p.setPen(theme_color("accent_text"))
        p.drawText(QRectF(x, pill.top() + 5, pill.right() - x, 22), int(Qt.AlignmentFlag.AlignVCenter), f"Dial: {self._mode}")
        dot_y = pill.top() + 35
        for k in range(self._count):
            p.setPen(Qt.PenStyle.NoPen)
            dot = theme_color("accent") if k == self._index else theme_color("text_muted")
            if k != self._index:
                dot.setAlphaF(0.5)
            p.setBrush(dot)
            p.drawEllipse(QPointF(x + 4 + k * 12, dot_y), 3.5, 3.5)
        p.setFont(_font(11))
        p.setPen(theme_color("text_muted"))
        p.drawText(QRectF(x + 12 * self._count + 6, dot_y - 9, 60, 18), int(Qt.AlignmentFlag.AlignVCenter),
                   f"{self._index + 1} of {self._count}")
        p.end()
