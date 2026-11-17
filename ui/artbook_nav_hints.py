"""The board's mouse controls, in small light words in its bottom-right corner ("MMB pan · LMB drag (snaps) · ...").
They brighten while the mouse is over them. Clicks go straight through to the board (the words are only words), so
the hover is read from the board's own mouse moves. Right-click > Canvas > Navigation Hints turns them off."""

from __future__ import annotations

from typing import TYPE_CHECKING

import shiboken6
from PySide6.QtCore import QEasingCurve, QEvent, QObject, QRect, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PySide6.QtWidgets import QWidget

from ui import animations
from ui.themes import theme_color

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

# (the button / gesture, what it does): each is registered in ui/shortcuts.py (artbook.pan, .snap, .snap_off, .zoom)
HINTS = (
    ("MMB", "pan"),
    ("LMB", "drag (snaps)"),
    ("Alt+LMB", "drag freely"),
    ("Wheel", "zoom"),
)
SEPARATOR = "·"
GAP = 7  # between a button and what it does
SPACE = 11  # either side of a separator
MARGIN = 14  # from the board's edges (and from a panel on the right)
PAD_Y = 4
IDLE = 0.62  # how strong the words are at rest (1: as bright as on hover)


def _mix(low: QColor, high: QColor, amount: float) -> QColor:
    return QColor.fromRgbF(*(a + (b - a) * amount for a, b in zip(low.getRgbF(), high.getRgbF())))


class NavHints(QWidget):
    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAccessibleName(self.text())
        self.hovered = False
        self.level = 0.0  # 0 at rest, 1 hovered (eased between)
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(animations.HOVER_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._on_fade)
        self._viewport = board.viewport()
        self._viewport.setMouseTracking(True)
        self._viewport.installEventFilter(self)
        board.toast.installEventFilter(self)  # (out of a message's way while it shows)

    # ------------------ size and place ------------------
    def _fonts(self) -> tuple[QFont, QFont]:
        word = QFont(self.font())
        key = QFont(word)
        key.setWeight(QFont.Weight.DemiBold)
        return key, word

    def _runs(self) -> list[tuple[str, bool, float]]:
        """(text, is it a button, how far after the previous run) left to right."""
        runs: list[tuple[str, bool, float]] = []
        for n, (key, action) in enumerate(HINTS):
            if n:
                runs.append((SEPARATOR, False, SPACE))
            runs.append((key, True, SPACE if n else 0))
            runs.append((action, False, GAP))
        return runs

    def sizeHint(self) -> QSize:
        key_font, word_font = self._fonts()
        keys, words = QFontMetricsF(key_font), QFontMetricsF(word_font)
        width = sum(before + (keys if is_key else words).horizontalAdvance(text) for text, is_key, before in self._runs())
        return QSize(int(width) + 4, int(max(keys.height(), words.height())) + 2 * PAD_Y)

    def place(self) -> None:
        """In the bottom-right corner, left of a panel open on the right; hidden where there's no room."""
        board = self.board
        right = board.width() - MARGIN
        for panel in (board._image_list, board._tray):
            if panel is not None:
                panel.installEventFilter(self)  # (placed again when it opens or closes, by its own button too)
                if panel.isVisible():
                    right = min(right, panel.x() - MARGIN)
        size = self.sizeHint()
        self.setGeometry(right - size.width(), board.height() - MARGIN - size.height(), size.width(), size.height())
        message = board.toast.isVisible() and board.toast.geometry().intersects(self.geometry().adjusted(-8, -8, 8, 8))
        self.setVisible(board.nav_hints_shown and right - size.width() > 120 and not message)  # (back when the message goes)
        if self.isVisible():
            self.raise_()

    # ------------------ hover ------------------
    def set_hovered(self, hovered: bool) -> None:
        if hovered == self.hovered:
            return
        self.hovered = hovered
        target = 1.0 if hovered else 0.0
        self._fade.stop()
        if not animations.motion_enabled():
            self._on_fade(target)
            return
        self._fade.setStartValue(self.level)
        self._fade.setEndValue(target)
        self._fade.start()

    def _on_fade(self, value) -> None:
        self.level = float(value)
        self.update()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.board):
            return False  # (the board closing: its last events)
        kind = event.type()
        if watched is not self._viewport:
            if kind in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Move, QEvent.Type.Resize):
                self.place()
            return False
        if kind == QEvent.Type.MouseMove and self.isVisible():
            at = self.board.mapFromGlobal(event.globalPosition().toPoint())
            self.set_hovered(self.geometry().adjusted(-4, -4, 4, 4).contains(at))
        elif kind == QEvent.Type.Leave:
            self.set_hovered(False)
        return False

    # ------------------ painting ------------------
    def colors(self) -> tuple[QColor, QColor]:
        """(the buttons' colour, the words' colour) as they are now."""
        amount = IDLE + (1.0 - IDLE) * self.level
        key = _mix(theme_color("text_muted"), theme_color("text_strong"), self.level)
        word = _mix(theme_color("text_muted"), theme_color("text"), self.level)
        key.setAlphaF(amount)
        word.setAlphaF(amount * (0.85 + 0.15 * self.level))
        return key, word

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        key_font, word_font = self._fonts()
        key_color, word_color = self.colors()
        shade = QColor(theme_color("grid_bg"))
        shade.setAlphaF(0.55)  # (a faint dark edge: readable over the grid and pictures)
        x = 2.0
        for text, is_key, before in self._runs():
            x += before
            font = key_font if is_key else word_font
            painter.setFont(font)
            width = QFontMetricsF(font).horizontalAdvance(text)
            box = QRect(int(x), 0, int(width) + 2, self.height())
            painter.setPen(shade)
            painter.drawText(box.translated(0, 1), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            painter.setPen(key_color if is_key else word_color)
            painter.drawText(box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            x += width
        painter.end()

    @staticmethod
    def text() -> str:
        """The words as shown."""
        return f" {SEPARATOR} ".join(f"{key} {action}" for key, action in HINTS)
