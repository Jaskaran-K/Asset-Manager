"""Go to a page quickly (Ctrl+P in the Artbook): a box over the board. Type a few letters of a page's name; the pages
that match (best first: those starting with them, then a word starting with them, then anywhere, then the letters in
order) show with their folder, their small picture and what's on them. Empty, it lists the pages you had open last.
Up / Down pick, Enter opens (Back returns), Esc closes."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from core import artbook_store as store
from core.settings import get_artbook_recent_pages
from ui import shortcuts
from ui.controls import soft_shadow
from ui.duotone import DuotoneGlyph
from ui.themes import theme_color

if TYPE_CHECKING:
    from ui.artbook import ArtbookPanel

WIDTH = 600
ROWS = 7
ROW_HEIGHT = 64
THUMB = QSize(72, 48)
ROLE = Qt.ItemDataRole.UserRole  # a row: its page id
MAX_ROWS = 60


def match(name: str, query: str) -> Optional[tuple[int, list[int]]]:
    """How well a page's name matches what's typed (lower is better: 0 starts with it, 1 a word starts with it, 2
    anywhere, 3 its letters in order), and the letters that matched; None if it doesn't."""
    low, q = name.lower(), query.lower().strip()
    if not q:
        return 3, []
    at = low.find(q)
    if at == 0:
        return 0, list(range(len(q)))
    if at > 0:
        words = [n for n in range(len(low)) if (n == 0 or not low[n - 1].isalnum()) and low.startswith(q, n)]
        if words:
            return 1, list(range(words[0], words[0] + len(q)))
        return 2, list(range(at, at + len(q)))
    letters, start = [], 0
    for char in q:
        found = low.find(char, start)
        if found < 0:
            return None
        letters.append(found)
        start = found + 1
    return 3, letters


def marked(name: str, letters: list[int]) -> str:
    """The name, its matched letters marked."""
    out = []
    for n, char in enumerate(name):
        text = html.escape(char)
        out.append(f"<span style='background-color:#ffcf3d; color:#1d1d1d'>{text}</span>" if n in letters else text)
    return "".join(out)


def ranked(pages: list[dict[str, Any]], query: str, recent: list[str], skip: Optional[str] = None) -> list[tuple[dict, list[int]]]:
    """The pages to show for what's typed, best first (nothing typed: the recent ones first)."""
    order = {page_id: n for n, page_id in enumerate(recent)}
    out = []
    for page in pages:
        if page["id"] == skip:
            continue
        found = match(page.get("name") or "", query)
        if found is not None:
            score, letters = found
            out.append(((score, order.get(page["id"], len(order)), (page.get("name") or "").lower()), page, letters))
    out.sort(key=lambda row: row[0])
    return [(page, letters) for _key, page, letters in out[:MAX_ROWS]]


def _thumb(page_id: str) -> QPixmap:
    pixmap = QPixmap(THUMB)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    frame = QPainterPath()
    frame.addRoundedRect(QRectF(0.5, 0.5, THUMB.width() - 1, THUMB.height() - 1), 6, 6)
    painter.fillPath(frame, theme_color("grid_bg"))
    path = store.thumbnail_path(page_id)
    picture = QPixmap(str(path)) if path.is_file() else QPixmap()
    if not picture.isNull():
        fitted = picture.scaled(THUMB - QSize(6, 6), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap((THUMB.width() - fitted.width()) // 2, (THUMB.height() - fitted.height()) // 2, fitted)
    painter.setPen(theme_color("border"))
    painter.drawPath(frame)
    painter.end()
    return pixmap


class _Row(QWidget):
    def __init__(self, page_id: str, name_html: str, under: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(12)
        thumb = QLabel()
        thumb.setPixmap(_thumb(page_id))
        layout.addWidget(thumb)
        column = QVBoxLayout()
        column.setSpacing(2)
        name = QLabel(f"<b>{name_html}</b>")
        name.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(name)
        info = QLabel(under)
        info.setObjectName("mutedLabel")
        column.addWidget(info)
        layout.addLayout(column, 1)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


class PageSwitcher(QFrame):
    """See the module docstring. open() shows it, ready to type."""

    def __init__(self, panel: "ArtbookPanel"):
        super().__init__(panel.board)
        self.panel = panel
        self.setObjectName("imageList")  # (the floating panels' look)
        soft_shadow(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.addWidget(DuotoneGlyph("search", size=18))
        self.edit = QLineEdit()
        self.edit.setFrame(False)
        self.edit.setPlaceholderText("Go to a page: type its name")
        self.edit.setToolTip("Type a few letters of a page's name; Enter opens it")
        self.edit.textChanged.connect(self.refresh)
        self.edit.installEventFilter(self)
        top.addWidget(self.edit, 1)
        keys = QLabel(shortcuts.get("pages.switch").keys)
        keys.setObjectName("mutedLabel")
        top.addWidget(keys)
        layout.addLayout(top)
        self.list = QListWidget()
        self.list.setObjectName("imageListView")
        self.list.itemClicked.connect(self._on_clicked)
        layout.addWidget(self.list, 1)
        self.hint = QLabel()
        self.hint.setObjectName("mutedLabel")
        layout.addWidget(self.hint)
        self.hide()

    def place(self) -> None:
        board = self.panel.board
        width = min(WIDTH, board.width() - 24)
        rows = max(1, min(ROWS, self.list.count()))  # (as tall as what's listed)
        height = min(76 + rows * (ROW_HEIGHT + 2) + 30, board.height() - 40)
        self.setGeometry((board.width() - width) // 2, 20, width, height)

    def open(self) -> None:
        self.edit.clear()
        self.refresh()
        self.place()
        self.show()
        self.raise_()
        self.edit.setFocus()

    def refresh(self) -> None:
        query = self.edit.text()
        pages = store.list_pages()
        folders = self._folders()
        current = self.panel.current_page_id()
        rows = ranked(pages, query, get_artbook_recent_pages(), current)
        self.list.clear()
        for page, letters in rows:
            name = page.get("name") or "Untitled"
            parts = [folders.get(page["id"], "")] + [store.counts_text(page.get("counts") or (0, 0, 0, 0), "Empty")]
            row = QListWidgetItem()
            row.setData(ROLE, page["id"])
            row.setSizeHint(QSize(10, ROW_HEIGHT))
            self.list.addItem(row)
            self.list.setItemWidget(row, _Row(page["id"], marked(name, letters), " · ".join(p for p in parts if p)))
        if self.list.count():
            self.list.setCurrentRow(0)
        self.hint.setText("Your recent pages first. Enter opens it (Back returns)" if not query.strip()
                          else ("Enter opens it (Back returns)" if rows else "No page's name has that"))
        if self.isVisible():
            self.place()

    @staticmethod
    def _folders() -> dict[str, str]:
        out: dict[str, str] = {}

        def walk(folder: dict[str, Any], name: str) -> None:
            for page_id in folder.get("pages", []):
                out[page_id] = name
            for sub in folder.get("folders", []):
                walk(sub, sub.get("name", ""))

        walk(store.page_tree(), "")
        return out

    def open_row(self, row: Optional[QListWidgetItem] = None) -> None:
        row = row or self.list.currentItem()
        if row is None:
            return
        page_id = row.data(ROLE)
        self.hide()
        if self.panel.page is not None:
            self.panel.follow_page(page_id)
        else:
            self.panel.open_page(page_id)
        self.panel.board.setFocus()

    def _on_clicked(self, row: QListWidgetItem) -> None:
        self.open_row(row)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.edit and event.type() == QEvent.Type.KeyPress:
            if shortcuts.matches("artbook.deselect", event):
                self.hide()
                self.panel.board.setFocus()
            elif shortcuts.matches("pages.switch_open", event):
                self.open_row()
            elif shortcuts.matches("pages.switch_down", event):
                self.list.setCurrentRow(min(self.list.count() - 1, self.list.currentRow() + 1))
            elif shortcuts.matches("pages.switch_up", event):
                self.list.setCurrentRow(max(0, self.list.currentRow() - 1))
            else:
                return False
            return True
        return super().eventFilter(watched, event)
