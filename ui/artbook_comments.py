"""A comment thread on the Artbook board, in a small panel beside its badge (on a card) or its pin (anywhere): each
comment with when it was written, a tick to mark it done (it greys) and an x to delete it, then a box to write a new
one (Enter posts it). The board (ui.artbook_board) keeps the threads and makes the changes, one undo step each.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QVBoxLayout, QWidget

from ui import shortcuts
from ui.controls import GhostButton, soft_shadow
from ui.duotone import set_icon

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

WIDTH = 300
MAX_HEIGHT = 380
MAX_COMMENT = 2000


def when_text(stamp: float, now: Optional[float] = None) -> str:
    """"Today 14:02", "Yesterday 09:15", "12 Nov 18:40"."""
    now = time.time() if now is None else now
    day = time.strftime("%Y-%m-%d", time.localtime(stamp))
    clock = time.strftime("%H:%M", time.localtime(stamp))
    if day == time.strftime("%Y-%m-%d", time.localtime(now)):
        return f"Today {clock}"
    if day == time.strftime("%Y-%m-%d", time.localtime(now - 86400)):
        return f"Yesterday {clock}"
    return f"{time.strftime('%d %b', time.localtime(stamp)).lstrip('0')} {clock}"


class CommentThread(QFrame):
    """See the module docstring. show_thread() fills it; the board places it beside what it's on."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setObjectName("imageList")  # (the floating panels' look)
        soft_shadow(self)
        self.thread_id: Optional[str] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)
        header = QHBoxLayout()
        self.title = QLabel("COMMENTS")
        self.title.setObjectName("imageListTitle")
        header.addWidget(self.title)
        header.addStretch(1)
        close = GhostButton()
        set_icon(close, "close", size=14)
        close.setToolTip("Close the comments (Esc)")
        close.clicked.connect(board.close_comments)
        header.addWidget(close)
        layout.addLayout(header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("commentBody")
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(8)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Add a comment... (Enter to post)")
        self.edit.setMaxLength(MAX_COMMENT)
        self.edit.returnPressed.connect(self._on_post)
        layout.addWidget(self.edit)
        self.setFixedWidth(WIDTH)
        self.hide()

    def show_thread(self, thread: dict[str, Any]) -> None:
        """Show a thread's comments (a new thread: none yet)."""
        self.thread_id = thread["id"]
        while self.rows.count():
            child = self.rows.takeAt(0).widget()
            if child is not None:
                child.hide()  # (out of sight now: deleteLater frees it a moment later)
                child.deleteLater()
        notes = thread.get("notes", [])
        self.title.setText("COMMENTS" if thread.get("item") else f"COMMENT {thread.get('number', '')}".strip())
        for index, note in enumerate(notes):
            self.rows.addWidget(self._row(index, note))
        if not notes:
            empty = QLabel("No comments yet: write the first below.")
            empty.setObjectName("mutedLabel")
            self.rows.addWidget(empty)
        self.rows.addStretch(1)
        self.body.adjustSize()
        height = min(MAX_HEIGHT, 70 + self.body.sizeHint().height() + 40)
        self.setFixedHeight(max(150, height))
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())

    def _row(self, index: int, note: dict[str, Any]) -> QWidget:
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(4)
        when = QLabel(when_text(float(note.get("time", 0))))
        when.setObjectName("mutedLabel")
        top.addWidget(when)
        top.addStretch(1)
        done = QCheckBox("Done", row)
        done.setChecked(bool(note.get("done")))
        done.setToolTip("Mark it done (it greys; when all are done, the badge fades)")
        done.setProperty("index", index)
        done.toggled.connect(self._on_done)
        top.addWidget(done)
        delete = GhostButton(row)
        set_icon(delete, "close", size=12)
        delete.setFixedSize(QSize(22, 22))
        delete.setToolTip("Delete this comment")
        delete.setProperty("index", index)
        delete.clicked.connect(self._on_delete)
        top.addWidget(delete)
        layout.addLayout(top)
        text = QLabel(str(note.get("text", "")))
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.setObjectName("mutedLabel" if note.get("done") else "commentText")
        layout.addWidget(text)
        return row

    def _on_post(self) -> None:
        text = " ".join(self.edit.text().split())
        if text and self.thread_id is not None:
            self.edit.clear()
            self.board.post_comment(self.thread_id, text)

    def _on_done(self, on: bool) -> None:
        if self.thread_id is not None:
            self.board.set_comment_done(self.thread_id, self.sender().property("index"), on)

    def _on_delete(self) -> None:
        if self.thread_id is not None:
            self.board.delete_comment(self.thread_id, self.sender().property("index"))

    def keyPressEvent(self, event) -> None:
        if shortcuts.matches("artbook.deselect", event):
            self.board.close_comments()
            event.accept()
            return
        super().keyPressEvent(event)
