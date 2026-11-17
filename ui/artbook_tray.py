"""The Artbook's tray (U, or the tray button), floating on the board's right: two tabs.

Unsorted: what the browser extension (or the asset grid's Send to Artbook > Unsorted) sent, waiting to go on a page
(core.artbook_unsorted). Drag a row onto the board (or double-click it, or Place All); placed, it leaves the tray.
Trash: what was deleted from this page (kept TRASH_DAYS): Restore puts it back where it was; Empty Trash deletes it
for good.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QMimeData, QSize, Qt
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
)

from core import artbook_unsorted as unsorted
from ui import shortcuts
from ui.artbook_comments import when_text
from ui.controls import GhostButton, soft_shadow
from ui.duotone import duotone_icon, set_icon

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

WIDTH = 310
MARGIN = 12
ICON = 52
ID_ROLE = Qt.ItemDataRole.UserRole
UNSORTED_MIME = "application/x-artbook-unsorted"  # rows dragged from the tray: their entry ids
KIND_ICONS = {"note": "sticky_note", "column": "column", "table": "table", "line": "line", "stroke": "edit",
              "link": "web", "file": "cube", "page_card": "page", "image": "import"}


class _Rows(QListWidget):
    """The tray's list: its Unsorted rows drag onto the board (artbook.unsorted_drag)."""

    def __init__(self, tray: "TrayPanel"):
        super().__init__()
        self.tray = tray

    def mimeData(self, items) -> QMimeData:
        mime = QMimeData()
        if self.tray.tab == "unsorted":
            mime.setData(UNSORTED_MIME, " ".join(row.data(ID_ROLE) for row in items).encode("utf-8"))
        return mime

    def mimeTypes(self) -> list[str]:
        return [UNSORTED_MIME]


class TrayPanel(QFrame):
    """See the module docstring. refresh() shows what's in it now."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setObjectName("imageList")  # (the floating panels' look)
        soft_shadow(self)
        self.tab = "unsorted"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(4)
        self.tabs = QButtonGroup(self)
        self.btn_unsorted = self._tab("Unsorted", "What the browser extension sent, waiting to go on a page (every page's)")
        self.btn_trash = self._tab("Trash", "What was deleted from this page: restore it, or empty the Trash")
        header.addWidget(self.btn_unsorted)
        header.addWidget(self.btn_trash)
        header.addStretch(1)
        close = GhostButton()
        set_icon(close, "close", size=14)
        close.setToolTip(f"Close the tray ({shortcuts.get('artbook.tray').keys})")
        close.clicked.connect(self._on_close)
        header.addWidget(close)
        layout.addLayout(header)
        self.hint = QLabel()
        self.hint.setObjectName("mutedLabel")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.list = _Rows(self)
        self.list.setObjectName("imageListView")
        self.list.setIconSize(QSize(ICON, ICON))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setDragEnabled(True)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.list.itemDoubleClicked.connect(self._on_double_clicked)
        self.list.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self.list, 1)
        footer = QHBoxLayout()
        footer.setSpacing(4)
        self.btn_place = self._button("Place All", "Put everything in Unsorted on this page, in a row in the middle", self._on_place_all)
        self.btn_remove = self._button("Remove", "Take the selected ones out of Unsorted (without placing them)", self._on_remove)
        self.btn_restore = self._button("Restore", "Put the selected ones back on the page, where they were", self._on_restore)
        self.btn_restore_all = self._button("Restore All", "Put everything in the Trash back on the page", self._on_restore_all)
        self.btn_empty = self._button("Empty Trash", "Delete what's in this page's Trash for good", self._on_empty)
        for button in (self.btn_place, self.btn_remove, self.btn_restore, self.btn_restore_all, self.btn_empty):
            footer.addWidget(button)
        footer.addStretch(1)
        layout.addLayout(footer)
        self.btn_unsorted.setChecked(True)
        board.trash_changed.connect(self.refresh)
        self.hide()

    def _tab(self, text: str, tooltip: str) -> GhostButton:
        button = GhostButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setCheckable(True)
        button.setFixedHeight(28)
        self.tabs.addButton(button)
        button.clicked.connect(self._on_tab)
        return button

    def _button(self, text: str, tooltip: str, handler) -> GhostButton:
        button = GhostButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setFixedHeight(28)
        button.clicked.connect(handler)
        return button

    # ------------------ showing ------------------
    def place(self) -> None:
        width = min(WIDTH, max(180, self.board.width() - 2 * MARGIN))
        self.setGeometry(self.board.width() - width - MARGIN, MARGIN, width, max(160, self.board.height() - 2 * MARGIN))

    def show_tab(self, tab: str) -> None:
        self.tab = tab if tab in ("unsorted", "trash") else "unsorted"
        (self.btn_unsorted if self.tab == "unsorted" else self.btn_trash).setChecked(True)
        self.refresh()

    def _on_tab(self) -> None:
        self.show_tab("trash" if self.btn_trash.isChecked() else "unsorted")

    def refresh(self) -> None:
        trash = self.board.trash_entries()
        waiting = unsorted.entries()
        self.btn_unsorted.setText(f"Unsorted  {len(waiting)}" if waiting else "Unsorted")
        self.btn_trash.setText(f"Trash  {len(trash)}" if trash else "Trash")
        if not self.isVisible():
            return
        self.list.clear()
        if self.tab == "unsorted":
            self.hint.setText("Sent from the browser: drag onto the page (or double-click)." if waiting
                              else "Nothing waiting. In your browser, right-click a picture or a page > Asset Manager > Add to Artbook: Unsorted.")
            for entry in reversed(waiting):
                self._add_row(entry["id"], self._entry_icon(entry), entry_title(entry), entry_source(entry))
        else:
            self.hint.setText(f"Deleted from this page: kept {self.board.trash_days()} days." if trash else "Nothing deleted from this page.")
            for entry in trash:
                first = next((d for d in entry["items"] if d.get("kind") != "line"), entry["items"][0])
                self._add_row(entry["id"], self._trash_icon(first), entry["label"], "deleted " + when_text(entry["time"]).lower())
        self._sync_buttons()

    def _add_row(self, entry_id: str, icon: QIcon, title: str, under: str) -> None:
        row = QListWidgetItem(icon, f"{title}\n{under}")
        row.setData(ID_ROLE, entry_id)
        row.setToolTip(title)
        row.setSizeHint(QSize(0, ICON + 10))
        self.list.addItem(row)

    def _picture_icon(self, path) -> Optional[QIcon]:
        from ui.artbook_board import card_icon_image

        if path is not None and path.suffix.lower() == ".svg":  # (a site's icon may be an SVG)
            image = card_icon_image(path.read_bytes()) or QImage()
        else:
            image = QImage(str(path)) if path is not None else QImage()
        if image.isNull():
            return None
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap.fromImage(image.scaled(round(ICON * ratio), round(ICON * ratio), Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))
        pixmap.setDevicePixelRatio(ratio)
        return QIcon(pixmap)

    def _entry_icon(self, entry: dict) -> QIcon:
        return (self._picture_icon(entry.get("picture_path")) or self._picture_icon(entry.get("icon_path"))
                or duotone_icon(KIND_ICONS.get(entry["kind"], "import")))

    def _trash_icon(self, data: dict) -> QIcon:
        return self._picture_icon(self.board.trash_picture(data)) or duotone_icon(KIND_ICONS.get(data.get("kind", "image"), "import"))

    def _sync_buttons(self) -> None:
        on_unsorted = self.tab == "unsorted"
        chosen = bool(self.list.selectedItems())
        has_rows = self.list.count() > 0
        self.btn_place.setVisible(on_unsorted)
        self.btn_remove.setVisible(on_unsorted)
        self.btn_restore.setVisible(not on_unsorted)
        self.btn_restore_all.setVisible(not on_unsorted)
        self.btn_empty.setVisible(not on_unsorted)
        self.btn_place.setEnabled(has_rows)
        self.btn_remove.setEnabled(chosen)
        self.btn_restore.setEnabled(chosen)
        self.btn_restore_all.setEnabled(has_rows)
        self.btn_empty.setEnabled(has_rows)

    # ------------------ handlers ------------------
    def _on_close(self) -> None:
        self.board.toggle_tray()

    def selected_ids(self) -> list[str]:
        return [row.data(ID_ROLE) for row in self.list.selectedItems()]

    def _on_double_clicked(self, row: QListWidgetItem) -> None:
        if self.tab == "unsorted":
            self.board.place_unsorted([row.data(ID_ROLE)])
        else:
            self.board.restore_trash(row.data(ID_ROLE))

    def _on_place_all(self) -> None:
        self.board.place_unsorted([entry["id"] for entry in unsorted.entries()])

    def _on_remove(self) -> None:
        for entry_id in self.selected_ids():
            unsorted.remove(entry_id)
        self.board.unsorted_changed.emit()

    def _on_restore(self) -> None:
        ids = self.selected_ids()
        if len(ids) > 1:
            self.board.undo_stack.beginMacro("restore")
        for entry_id in ids:
            self.board.restore_trash(entry_id)
        if len(ids) > 1:
            self.board.undo_stack.endMacro()

    def _on_restore_all(self) -> None:
        self.board.restore_all_trash()

    def _on_empty(self) -> None:
        answer = QMessageBox.question(self, "Empty Trash", "Delete what's in this page's Trash for good? It can't be undone.")
        if answer == QMessageBox.StandardButton.Yes:
            self.board.empty_trash()


def entry_title(entry: dict) -> str:
    """An Unsorted entry's name in the tray."""
    if entry["kind"] == "link":
        return entry.get("title") or entry.get("url") or "Link"
    if entry["kind"] == "file":
        from pathlib import Path

        return entry.get("name") or Path(entry.get("path", "")).name or "File"
    return entry.get("name") or "Picture"


def entry_source(entry: dict) -> str:
    """Where it came from, and when."""
    source = entry.get("site") or entry.get("source") or ("the asset grid" if entry["kind"] == "file" else "")
    what = {"link": "link", "file": "file", "image": "picture"}[entry["kind"]]
    when = when_text(float(entry.get("time", time.time()))).lower()
    return f"{what} from {source}, {when}" if source else f"{what}, {when}"
