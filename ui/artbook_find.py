"""The Artbook's find bar (Ctrl+F), floating at the top of the board.

This page: what matches lights up on the board (BoardView.find: the words marked, the things outlined, the rest
faded); Enter / Shift+Enter go to the next / previous match, the board moving there. All pages: the same, plus a list
under the bar of every page with a match (this one first, from what's on the board; the rest from their files, read
off the UI thread by core.artbook_search), each match with the words around it. Picking one opens its page (Back
returns) and goes to it.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import artbook_search
from ui import shortcuts
from ui.controls import GhostButton, soft_shadow
from ui.duotone import DuotoneGlyph, set_icon
from ui.tag_widgets import run_job

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

WIDTH = 620
MARGIN = 12
RESULTS_HEIGHT = 440
TYPING_PAUSE_MS = 160
ROLE = Qt.ItemDataRole.UserRole  # a result row: (page id, item id, thread id)
KIND_ICONS = {"note": "sticky_note", "todo": "todo", "heading": "heading", "title": "heading", "table": "table",
              "link": "web", "file": "cube", "page_card": "page", "image": "grid", "column": "column", "line": "line",
              "comment": "pin", "name": "page"}
KIND_NAMES = {"note": "Note", "todo": "To-do", "heading": "Heading", "title": "Page title", "table": "Table",
              "link": "Link", "file": "File", "page_card": "Page link", "image": "Image", "column": "Column",
              "line": "Line", "comment": "Comment", "name": "Page name"}


def snippet_html(before: str, match: str, after: str, mark: str) -> str:
    """A result's words, the match marked."""
    return (f"{html.escape(before)}<span style='background-color:{mark}; color:#1d1d1d'>{html.escape(match)}</span>"
            f"{html.escape(after)}")


class _Row(QWidget):
    """A result: what it is (an icon and a word), then its words with the match marked."""

    def __init__(self, kind: str, words: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(10)
        layout.addWidget(DuotoneGlyph(KIND_ICONS.get(kind, "search"), size=18, tone="text"))
        column = QVBoxLayout()
        column.setSpacing(0)
        what = QLabel(KIND_NAMES.get(kind, "").upper())
        what.setObjectName("imageListTitle")
        column.addWidget(what)
        text = QLabel(words)
        text.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(text)
        layout.addLayout(column, 1)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # (the list takes the clicks)


class FindBar(QFrame):
    """See the module docstring. open() shows it, ready to type."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setObjectName("imageList")  # (the floating panels' look)
        soft_shadow(self)
        self._generation = 0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 8, 6)
        layout.setSpacing(4)
        layout.addWidget(DuotoneGlyph("search", size=18))
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Find on this page")
        self.edit.setFrame(False)
        self.edit.setToolTip("What to find: in notes, to-dos, tables, cards, image names and comments")
        self.edit.textChanged.connect(self._on_typed)
        self.edit.installEventFilter(self)
        layout.addWidget(self.edit, 1)
        self.count = QLabel()
        self.count.setObjectName("mutedLabel")
        layout.addWidget(self.count)
        self.btn_previous = self._button("arrow_up", f"Previous match ({shortcuts.get('artbook.find_previous').keys})", lambda: self.step(-1))
        self.btn_next = self._button("arrow_down", f"Next match ({shortcuts.get('artbook.find_next').keys})", lambda: self.step(1))
        layout.addWidget(self.btn_previous)
        layout.addWidget(self.btn_next)
        self.scopes = QButtonGroup(self)
        self.btn_page = self._scope("This page", "Find on this page")
        self.btn_all = self._scope("All pages", "Find on every page: a list of where, grouped by page")
        layout.addWidget(self.btn_page)
        layout.addWidget(self.btn_all)
        self.btn_page.setChecked(True)
        close = self._button("close", f"Close ({shortcuts.get('artbook.deselect').keys})", board.close_find)
        layout.addWidget(close)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(TYPING_PAUSE_MS)
        self.timer.timeout.connect(self.run)
        self.results = QFrame(board)
        self.results.setObjectName("imageList")
        soft_shadow(self.results)
        results_layout = QVBoxLayout(self.results)
        results_layout.setContentsMargins(8, 8, 8, 8)
        self.list = QListWidget()
        self.list.setObjectName("imageListView")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.itemClicked.connect(self._on_result_clicked)
        results_layout.addWidget(self.list)
        self.results.hide()
        self.hide()

    def _button(self, icon: str, tooltip: str, handler) -> GhostButton:
        button = GhostButton()
        set_icon(button, icon, size=16)
        button.setToolTip(tooltip)
        button.setFixedSize(QSize(30, 30))
        button.clicked.connect(handler)
        return button

    def _scope(self, text: str, tooltip: str) -> GhostButton:
        button = GhostButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setCheckable(True)
        button.setFixedHeight(28)
        self.scopes.addButton(button)
        button.clicked.connect(self._on_scope)
        return button

    # ------------------ showing ------------------
    def place(self) -> None:
        width = min(WIDTH, max(260, self.board.width() - 2 * MARGIN))
        left = (self.board.width() - width) // 2
        self.setGeometry(left, MARGIN, width, 44)
        height = min(RESULTS_HEIGHT, max(120, self.board.height() - 44 - 3 * MARGIN))
        self.results.setGeometry(left, MARGIN + 44 + 6, width, height)

    def open(self) -> None:
        self.place()
        self.show()
        self.raise_()
        self.edit.setFocus()
        self.edit.selectAll()
        if self.edit.text().strip():
            self.run()

    def hideEvent(self, event) -> None:
        self.results.hide()
        super().hideEvent(event)

    def all_pages(self) -> bool:
        return self.btn_all.isChecked()

    # ------------------ finding ------------------
    def _on_typed(self) -> None:
        self.timer.start()

    def _on_scope(self) -> None:
        self.edit.setPlaceholderText("Find on every page" if self.all_pages() else "Find on this page")
        self.run()
        self.edit.setFocus()

    def run(self) -> None:
        """Find what's typed: on the board, and (All pages) on every page."""
        self.timer.stop()
        query = self.edit.text().strip()
        self.board.find(query)
        self._show_count()
        self._generation += 1
        if not (self.all_pages() and query):
            self.list.clear()
            self.results.hide()
            return
        here = artbook_search.search_page({"items": self.board.page_items(), "comments": self.board.page_comments()}, query)
        page_id, name = self.board.page_id, self.board.page_name
        generation = self._generation
        run_job(lambda: artbook_search.search_pages(query, skip=page_id),
                lambda found: self._show_results(generation, query, page_id, name, here, found), lambda _error: None)

    def _show_count(self) -> None:
        number, total = self.board.find_position()
        query = self.edit.text().strip()
        self.count.setText("" if not query else (f"{number} of {total}" if total else "No matches"))
        self.btn_previous.setEnabled(total > 1)
        self.btn_next.setEnabled(total > 1)

    def step(self, direction: int) -> None:
        self.board.find_step(direction)
        self._show_count()

    def _show_results(self, generation: int, query: str, page_id: Optional[str], name: str,
                      here: list[dict[str, Any]], found: list[dict[str, Any]]) -> None:
        if generation != self._generation or not self.isVisible():
            return  # (typed on since)
        from ui.artbook_board import FIND_MARK

        mark = FIND_MARK
        self.list.clear()
        groups = ([{"page": page_id, "name": name, "count": sum(h["count"] for h in here), "hits": here, "this": True}]
                  if here else []) + found
        for group in groups:
            title = (group["name"] or "Untitled").upper() + ("  ·  THIS PAGE" if group.get("this") else "")
            head = QListWidgetItem(f"{title}    {group['count'] or ''}".rstrip())
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(head)
            hits = list(group["hits"])
            if group.get("name_match") and not hits:
                hits = [{"item": None, "thread": None, "kind": "name", "before": "", "match": "", "after": "Its name has it"}]
            for hit in hits:
                row = QListWidgetItem()
                row.setData(ROLE, (group["page"], hit["item"], hit["thread"]))
                widget = _Row(hit["kind"], snippet_html(hit["before"], hit["match"], hit["after"], mark))
                row.setSizeHint(QSize(10, 52))
                self.list.addItem(row)
                self.list.setItemWidget(row, widget)
        if not groups:
            empty = QListWidgetItem(f"Nothing on any page has “{query}”")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(empty)
        self.place()
        self.results.show()
        self.results.raise_()

    def _on_result_clicked(self, row: QListWidgetItem) -> None:
        self.open_result(row)

    def open_result(self, row: Optional[QListWidgetItem] = None) -> None:
        """A result picked: its page (opened, if it's another: Back returns), at its match."""
        row = row or self.list.currentItem()
        data = row.data(ROLE) if row is not None else None
        if not data:
            return
        page_id, item_id, thread_id = data
        if page_id != self.board.page_id:
            self.board.page_open_requested.emit(page_id)
            if self.board.page_id != page_id:
                return
            self.board.find(self.edit.text().strip())
        if item_id or thread_id:
            self.board.find_go_to(item_id, thread_id)
        self._show_count()
        self.edit.setFocus()

    def _move_result(self, step: int) -> None:
        rows = [n for n in range(self.list.count()) if self.list.item(n).flags() & Qt.ItemFlag.ItemIsEnabled]
        if not rows:
            return
        current = self.list.currentRow()
        later = [n for n in rows if (n > current if step > 0 else n < current)]
        target = (later[0] if step > 0 else later[-1]) if later else (rows[0] if step > 0 else rows[-1])
        self.list.setCurrentRow(target)

    # ------------------ keys ------------------
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.edit and event.type() == QEvent.Type.KeyPress:
            results = self.results.isVisible() and self.list.currentItem() is not None
            if shortcuts.matches("artbook.deselect", event):
                self.board.close_find()
            elif shortcuts.matches("artbook.find_previous", event):
                self.step(-1)
            elif shortcuts.matches("artbook.find_next", event):
                if results:
                    self.open_result()
                else:
                    self.step(1)
            elif shortcuts.matches("artbook.find_result_down", event) and self.results.isVisible():
                self._move_result(1)
            elif shortcuts.matches("artbook.find_result_up", event) and self.results.isVisible():
                self._move_result(-1)
            else:
                return False
            return True
        return super().eventFilter(watched, event)
