"""Double-click empty space on the Artbook board: a small row of what to put there (a note, a heading, a to-do, a
column, a table, a link, pictures), beside the spot. Picking one adds it there; a click anywhere else, or Esc, closes
the row (BoardView.hide_quick_add). The keys still work without it (T, Shift+T, X...)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QPoint, QPointF, QSize, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout

from ui import shortcuts
from ui.controls import GhostButton, soft_shadow
from ui.duotone import set_icon

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

TOOLS = (  # (what, icon, name, its key)
    ("note", "sticky_note", "Note", "artbook.note"),
    ("heading", "heading", "Heading", "artbook.heading"),
    ("todo", "todo", "To-do", "artbook.todo"),
    ("column", "column", "Column", "artbook.column"),
    ("table", "table", "Table", "artbook.table"),
    ("link", "web", "Link...", None),
    ("pictures", "import", "Pictures...", None),
)
GAP = 12


class QuickAdd(QFrame):
    """See the module docstring. show_at() shows it by a spot of the board."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.at: Optional[QPointF] = None
        self.setObjectName("imageList")  # (the floating panels' look)
        soft_shadow(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        self.buttons: dict[str, GhostButton] = {}
        for tool, icon, name, key in TOOLS:
            button = GhostButton()
            set_icon(button, icon, size=18)
            button.setText(name)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setMinimumSize(QSize(66, 46))
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            hint = f" ({shortcuts.get(key).keys})" if key else ""
            button.setToolTip({"link": "A link card: a web page's address", "pictures": "Pictures from your files"}.get(
                tool, f"A {name.lower()} here{hint}"))
            button.clicked.connect(lambda _checked=False, t=tool: self.pick(t))
            layout.addWidget(button)
            self.buttons[tool] = button
        self.hide()

    def show_at(self, at: QPointF, pos: QPoint) -> None:
        """Beside a spot of the board (at: on the board; pos: in the view), kept inside the view."""
        self.at = at
        self.adjustSize()
        x = min(max(GAP, pos.x() + GAP), self.board.width() - self.width() - GAP)
        y = min(max(GAP, pos.y() - self.height() // 2), self.board.height() - self.height() - GAP)
        self.move(x, y)
        self.show()
        self.raise_()

    def pick(self, tool: str) -> None:
        at = self.at
        self.hide()
        if tool == "link":
            self.board.add_link_here(at)
        else:
            self.board.add_tool(tool, at)

