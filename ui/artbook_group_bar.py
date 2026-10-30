"""The bar that floats above the selected group on the Artbook board: the group's colour (the
theme's, white, black, or a custom one picked live) and locking it in place.

Like the note bar (ui.artbook_note_bar), nothing here takes the keyboard focus. The board
(ui.artbook_board) changes the group and records it for undo.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QToolButton

from ui.artbook_note_bar import ColorButton
from ui.duotone import set_icon

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

LOCK_TIP = "Lock the group: it can't be moved, resized, turned, cropped or deleted until it's unlocked"
UNLOCK_TIP = "Unlock the group (it's locked in place)"


class GroupBar(QFrame):
    """See the module docstring. sync() shows the selected group's colour and lock."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setObjectName("groupBar")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)
        self.btn_color = ColorButton("fill", "Group colour", board.set_group_fill, board.live_change, self.avoid_rect)
        layout.addWidget(self.btn_color)
        self.btn_lock = QToolButton()
        set_icon(self.btn_lock, "lock", size=16)
        self.btn_lock.setCheckable(True)
        self.btn_lock.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_lock.setFixedSize(30, 28)
        self.btn_lock.setToolTip(LOCK_TIP)
        self.btn_lock.clicked.connect(board.toggle_group_lock)
        layout.addWidget(self.btn_lock)
        self.adjustSize()
        self.hide()

    def sync(self, color: QColor, locked: bool) -> None:
        self.btn_color.set_color(color)
        self.btn_lock.blockSignals(True)
        self.btn_lock.setChecked(locked)
        self.btn_lock.blockSignals(False)
        self.btn_lock.setToolTip(UNLOCK_TIP if locked else LOCK_TIP)

    def avoid_rect(self) -> QRect:
        """The bar and its group on screen: what an open colour picker mustn't cover."""
        area = QRect(self.mapToGlobal(QPoint(0, 0)), self.size())
        box = self.board.group_bar_box()
        if box is not None:
            area = area.united(QRect(self.board.mapToGlobal(box.topLeft()), box.size()))
        return area

    def close_pickers(self) -> None:
        self.btn_color.close_picker()

    def sizeHint(self) -> QSize:
        return self.layout().sizeHint()
