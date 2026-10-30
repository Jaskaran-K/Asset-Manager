"""The bar at the top of the Artbook board while drawing (D): the pen's colour (the theme's,
white, black, or a custom one), its size, smoothing, the eraser, and Done.

Like the note bar, nothing here takes the keyboard focus. The board (ui.artbook_board) draws.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QPushButton, QToolButton

from ui import shortcuts
from ui.artbook_note_bar import ColorButton
from ui.duotone import set_icon

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

PEN_SIZES = (1, 2, 4, 6, 8, 12, 16, 24, 32, 48)  # on screen, at the zoom a stroke is drawn at


class DrawBar(QFrame):
    """See the module docstring. sync() shows the pen."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setObjectName("drawBar")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        self.btn_color = ColorButton("edit", "Pen colour", board.set_pen_color, None, self.avoid_rect)
        layout.addWidget(self.btn_color)
        self.size_box = QComboBox()
        self.size_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.size_box.setToolTip("Pen size (how thick a stroke looks at the zoom it's drawn at)")
        for size in PEN_SIZES:
            self.size_box.addItem(f"{size} px", size)
        self.size_box.activated.connect(self._on_size_picked)
        layout.addWidget(self.size_box)
        self.btn_smooth = QToolButton()
        set_icon(self.btn_smooth, "smooth", size=16)
        self.btn_smooth.setCheckable(True)
        self.btn_smooth.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_smooth.setFixedSize(30, 28)
        self.btn_smooth.setToolTip("Smoothing: steadies lines drawn with a mouse, as with a pen (on / off)")
        self.btn_smooth.clicked.connect(board.toggle_smoothing)
        layout.addWidget(self.btn_smooth)
        self.btn_eraser = QToolButton()
        set_icon(self.btn_eraser, "eraser", size=16)
        self.btn_eraser.setCheckable(True)
        self.btn_eraser.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_eraser.setFixedSize(30, 28)
        self.btn_eraser.setToolTip(f"Eraser ({shortcuts.get('artbook.eraser').keys}): drag over strokes to remove them")
        self.btn_eraser.clicked.connect(board.toggle_eraser)
        layout.addWidget(self.btn_eraser)
        self.btn_done = QPushButton("Done")
        self.btn_done.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_done.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_done.setToolTip(f"Stop drawing ({shortcuts.get('artbook.draw').keys} or Esc)")
        self.btn_done.clicked.connect(board.stop_draw)
        layout.addWidget(self.btn_done)
        self.adjustSize()
        self.hide()

    def _on_size_picked(self, index: int) -> None:
        self.board.set_pen_width(self.size_box.itemData(index))

    def sync(self, color: QColor, width: int, eraser: bool, smooth: bool = True) -> None:
        self.btn_color.set_color(color)
        index = self.size_box.findData(width)
        if index >= 0:
            self.size_box.setCurrentIndex(index)
        for button, on in ((self.btn_eraser, eraser), (self.btn_smooth, smooth)):
            button.blockSignals(True)
            button.setChecked(on)
            button.blockSignals(False)

    def avoid_rect(self) -> QRect:
        return QRect(self.mapToGlobal(QPoint(0, 0)), self.size())

    def close_pickers(self) -> None:
        self.btn_color.close_picker()

    def sizeHint(self) -> QSize:
        return self.layout().sizeHint()
