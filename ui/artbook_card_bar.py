"""The bar that floats above a selected link, file or page card on the Artbook board: its look (Big: a picture
card, or Compact: a row; new cards of its kind come the way it's set last), Open (the link in the browser, the
file in its app, the page), More (the card's other actions) and delete. The board (ui.artbook_board) does the work.

Like the note bar, nothing here takes the keyboard focus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QToolButton

from ui import shortcuts
from ui.controls import DropButton, GhostButton, soft_shadow
from ui.duotone import set_icon

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

OPEN_TIPS = {
    "link": "Open the page in your browser (double-click works too)",
    "file": "Open the file in its app: Maya first (double-click works too)",
    "page_card": "Open that page: Back, at the top, returns here (double-click works too)",
}


class CardBar(QFrame):
    """See the module docstring. sync() shows the card's look."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setObjectName("noteBar")  # (the note bar's look)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(3)
        soft_shadow(self)

        self.btn_big = self._text_button("Big", "Big: a picture card (new cards of this kind come big too)")
        self.btn_big.clicked.connect(self._on_big)
        layout.addWidget(self.btn_big)
        self.btn_compact = self._text_button("Compact", "Compact: a row (new cards of this kind come compact too)")
        self.btn_compact.clicked.connect(self._on_compact)
        layout.addWidget(self.btn_compact)
        layout.addWidget(self._divider())
        self.btn_open = self._text_button("Open", OPEN_TIPS["link"])
        set_icon(self.btn_open, "open_in", size=16)
        self.btn_open.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_open.setCheckable(False)
        self.btn_open.clicked.connect(self._on_open)
        layout.addWidget(self.btn_open)
        self.btn_more = DropButton(None, "More", "More: open in another app, show it, copy its address, relink...")
        self.btn_more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_more.menu().aboutToShow.connect(self._fill_more)
        layout.addWidget(self.btn_more)
        layout.addWidget(self._divider())
        self.btn_delete = GhostButton()
        set_icon(self.btn_delete, "close", size=16)
        self.btn_delete.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_delete.setFixedSize(30, 28)
        self.btn_delete.setToolTip(f"Delete the card ({shortcuts.get('artbook.delete').keys})")
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
    def _text_button(text: str, tooltip: str) -> QToolButton:
        button = GhostButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setFixedHeight(28)
        button.setCheckable(True)
        return button

    def _on_big(self) -> None:
        self.board.set_card_style("big")

    def _on_compact(self) -> None:
        self.board.set_card_style("compact")

    def _on_open(self) -> None:
        self.board.open_card()

    def _fill_more(self) -> None:
        menu = self.btn_more.menu()
        menu.clear()
        card = self.board.card_bar_target()
        if card is not None:
            self.board._fill_menu(menu, self.board.card_entries(card))

    def sync(self, kind: str, style: str, locked: bool) -> None:
        for button, on in ((self.btn_big, style == "big"), (self.btn_compact, style == "compact")):
            button.blockSignals(True)
            button.setChecked(on)
            button.blockSignals(False)
            button.setEnabled(not locked)
        self.btn_open.setToolTip(OPEN_TIPS.get(kind, OPEN_TIPS["link"]))
        self.btn_delete.setEnabled(not locked)
        self.resize(self.sizeHint())

    def sizeHint(self) -> QSize:
        return self.layout().sizeHint()
