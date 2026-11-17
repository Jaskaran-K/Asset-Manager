"""The bar that floats above the selected lines on the Artbook board: their shape (curved, straight or elbow; Reset
Bend), their ends (no arrows, an arrow at the end, arrows at both ends; Reverse Direction), colour, thickness,
dashed, a label, and delete. Changes apply to every selected line, as one undo step; the board (ui.artbook_board)
makes them.

Like the note bar, nothing here takes the keyboard focus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout

from ui import shortcuts
from ui.artbook_note_bar import ColorButton
from ui.controls import DropButton, GhostButton, fit_popup, soft_shadow
from ui.duotone import set_icon

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

SHAPES = {"curve": "Curved", "straight": "Straight", "elbow": "Elbow"}
ENDS = {"none": "No Arrows", "end": "Arrow at the End", "both": "Arrows at Both Ends"}
WIDTHS = (1, 2, 3, 5)  # on screen, at the zoom the line was drawn at


class LineBar(QFrame):
    """See the module docstring. sync() shows the selected lines' look."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setObjectName("noteBar")  # (the note bar's look)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(3)
        soft_shadow(self)

        self.btn_shape = DropButton(None, "Curved", "Shape: curved, straight or elbow (drag the line's middle to bend it)")
        self.btn_shape.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.shape_actions = {}
        for key, name in SHAPES.items():
            action = self.btn_shape.menu().addAction(name)
            action.setCheckable(True)
            action.setData(key)
            action.triggered.connect(self._on_shape_picked)
            self.shape_actions[key] = action
        self.btn_shape.menu().addSeparator()
        self.btn_shape.menu().addAction("Reset Bend").triggered.connect(board.reset_line_bend)
        layout.addWidget(self.btn_shape)
        layout.addWidget(self._divider())

        self.btn_ends = DropButton(None, "Arrow at the End", "Ends: no arrows, an arrow at the end, or arrows at both ends")
        self.btn_ends.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.end_actions = {}
        for key, name in ENDS.items():
            action = self.btn_ends.menu().addAction(name)
            action.setCheckable(True)
            action.setData(key)
            action.triggered.connect(self._on_ends_picked)
            self.end_actions[key] = action
        self.btn_ends.menu().addSeparator()
        self.btn_ends.menu().addAction("Reverse Direction").triggered.connect(board.reverse_lines)
        layout.addWidget(self.btn_ends)
        layout.addWidget(self._divider())

        self.btn_color = ColorButton("line", "Line colour", board.set_line_color, board.live_change, self.avoid_rect)
        layout.addWidget(self.btn_color)
        self.width_box = QComboBox()
        self.width_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.width_box.setToolTip("Thickness")
        for width in WIDTHS:
            self.width_box.addItem(f"{width} px", width)
        self.width_box.activated.connect(self._on_width_picked)
        fit_popup(self.width_box)
        layout.addWidget(self.width_box)
        self.btn_dashed = self._text_button("Dashed", "Dashed line (on / off)", board.toggle_line_dashed)
        self.btn_dashed.setCheckable(True)
        layout.addWidget(self.btn_dashed)
        layout.addWidget(self._divider())
        self.btn_label = self._text_button(
            "Label", f"Label: type text on the line ({shortcuts.get('artbook.crop_apply').keys}, or double-click the label, works too)",
            lambda: board.edit_line_label())  # (not the click's checked flag as the line)
        layout.addWidget(self.btn_label)
        self.btn_delete = GhostButton()
        set_icon(self.btn_delete, "close", size=16)
        self.btn_delete.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_delete.setFixedSize(30, 28)
        self.btn_delete.setToolTip(f"Delete ({shortcuts.get('artbook.delete').keys})")
        self.btn_delete.clicked.connect(board.delete_selection)
        layout.addWidget(self.btn_delete)
        self.adjustSize()
        self.hide()

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("noteBarDivider")
        line.setFixedSize(1, 18)
        return line

    @staticmethod
    def _text_button(text: str, tooltip: str, handler) -> GhostButton:
        button = GhostButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setFixedHeight(28)
        button.clicked.connect(handler)
        return button

    def _on_shape_picked(self) -> None:
        self.board.set_line_shape(self.sender().data())

    def _on_ends_picked(self) -> None:
        self.board.set_line_heads(self.sender().data())

    def _on_width_picked(self, index: int) -> None:
        self.board.set_line_width(self.width_box.itemData(index))

    def sync(self, formats: dict[str, Any]) -> None:
        """Show the selected lines' look (from BoardView.line_formats)."""
        self.btn_shape.setText(SHAPES.get(formats["shape"], "Curved"))
        for key, action in self.shape_actions.items():
            action.setChecked(key == formats["shape"])
        self.btn_ends.setText(ENDS.get(formats["heads"], "Arrow at the End"))
        for key, action in self.end_actions.items():
            action.setChecked(key == formats["heads"])
        self.btn_color.set_color(formats["color"])
        index = self.width_box.findData(formats["width"])
        if index >= 0:
            self.width_box.setCurrentIndex(index)
        self.btn_dashed.blockSignals(True)
        self.btn_dashed.setChecked(formats["dashed"])
        self.btn_dashed.blockSignals(False)
        self.btn_label.setEnabled(formats["one"])  # (one line's label at a time)
        self.resize(self.sizeHint())

    def avoid_rect(self) -> QRect:
        return QRect(self.mapToGlobal(QPoint(0, 0)), self.size())

    def close_pickers(self) -> None:
        self.btn_color.close_picker()

    def sizeHint(self) -> QSize:
        return self.layout().sizeHint()
