"""The image list: every image on the page, beside the board (L, or the Images button).

Each row: a small picture, the image's name, its size, and where its picture comes from (its
linked file, the copy kept in the page, or neither: missing). Clicking a row selects the image
and frames it on the board; selecting on the board highlights its rows. With images missing
their files, Relink... looks for them (BoardView.relink_missing).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QToolButton,
    QVBoxLayout,
)

from ui import artbook_layout, shortcuts
from ui.duotone import set_icon
from ui.themes import theme_color

if TYPE_CHECKING:
    from ui.artbook_board import BoardView, ImageItem

WIDTH = 290
MARGIN = 12
ICON = 44
ID_ROLE = Qt.ItemDataRole.UserRole


class ImageListPanel(QFrame):
    """See the module docstring. Floats over the right side of the board."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setObjectName("imageList")
        self._icons: dict[str, tuple[int, QIcon]] = {}  # item id -> (its pixmap's cache key, icon)
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(6)
        title = QLabel("IMAGES")
        title.setObjectName("imageListTitle")
        header.addWidget(title)
        self.count = QLabel()
        self.count.setObjectName("mutedLabel")
        header.addWidget(self.count)
        header.addStretch(1)
        self.btn_relink = QPushButton("Relink...")
        self.btn_relink.setToolTip("Find the images whose files moved: pick the folder they're in now")
        self.btn_relink.clicked.connect(self._on_relink)
        header.addWidget(self.btn_relink)
        close = QToolButton()
        set_icon(close, "close", size=14)
        close.setToolTip(f"Close the image list ({shortcuts.get('artbook.image_list').keys})")
        close.clicked.connect(self.hide)
        header.addWidget(close)
        layout.addLayout(header)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search images")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.rebuild)
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("imageListView")
        self.list.setIconSize(QSize(ICON, ICON))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.itemSelectionChanged.connect(self._on_rows_selected)
        self.list.itemClicked.connect(self._on_row_clicked)
        layout.addWidget(self.list, 1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)  # (many changes in a row: one rebuild)
        self._timer.timeout.connect(self.rebuild)
        board.changed.connect(self.schedule)
        board.files_changed.connect(self.schedule)
        board.scene().selectionChanged.connect(self.show_selection)
        self.hide()

    # ------------------ showing ------------------
    def place(self) -> None:
        width = min(WIDTH, max(160, self.board.width() - 2 * MARGIN))
        self.setGeometry(self.board.width() - width - MARGIN, MARGIN, width, max(120, self.board.height() - 2 * MARGIN))

    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.place()
            self.show()
            self.raise_()
            self.rebuild()

    def schedule(self) -> None:
        if self.isVisible():
            self._timer.start()

    def _images(self) -> list["ImageItem"]:
        from ui.artbook_board import ImageItem

        images = {item.item_id: item for item in self.board.items_list() if isinstance(item, ImageItem)}
        order = artbook_layout.reading_order([(item_id, item.sceneBoundingRect()) for item_id, item in images.items()])
        return [images[item_id] for item_id in order]

    @staticmethod
    def status(item: "ImageItem") -> tuple[str, bool]:
        """(how its picture is kept, whether that's a problem)."""
        w, h = item.natural
        if item.load_failed:
            return "Can't be shown (its file and copy are gone)", True
        if item.file_missing():
            return "Missing file: showing the copy in the page", True
        if item.linked_path() is not None:
            return f"{w} x {h}  ·  linked file", False
        return f"{w} x {h}  ·  kept in the page", False

    def _icon(self, item: "ImageItem") -> QIcon:
        if item.pixmap is None:
            return QIcon()
        key = item.pixmap.cacheKey()
        cached = self._icons.get(item.item_id)
        if cached is None or cached[0] != key:
            ratio = self.devicePixelRatioF()
            small = item.pixmap.scaled(
                round(ICON * ratio), round(ICON * ratio), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
            small.setDevicePixelRatio(ratio)
            cached = (key, QIcon(small))
            self._icons[item.item_id] = cached
        return cached[1]

    def rebuild(self) -> None:
        if not self.isVisible() and not self.search.text():
            return
        images = self._images()
        words = self.search.text().strip().lower()
        scroll = self.list.verticalScrollBar().value()
        self._syncing = True
        try:
            self.list.clear()
            for item in images:
                path = item.linked_path()
                if words and words not in item.display_name().lower() and words not in str(path or "").lower():
                    continue
                status, problem = self.status(item)
                row = QListWidgetItem(self._icon(item), f"{item.display_name()}\n{status}")
                row.setData(ID_ROLE, item.item_id)
                row.setToolTip(str(path) if path is not None else "Kept in the page (no file)")
                if problem:
                    row.setForeground(theme_color("warn_text"))
                row.setSizeHint(QSize(0, ICON + 10))
                self.list.addItem(row)
                row.setSelected(item.isSelected())
        finally:
            self._syncing = False
        self.list.verticalScrollBar().setValue(scroll)
        missing = sum(1 for item in images if item.file_missing() or item.load_failed)
        self.count.setText(f"{len(images)}" + (f"  ·  {missing} missing" if missing else ""))
        self.btn_relink.setVisible(any(item.file_missing() for item in images))
        alive = {item.item_id for item in images}
        self._icons = {k: v for k, v in self._icons.items() if k in alive}

    def show_selection(self) -> None:
        """The board's selection, highlighted in the list."""
        if not self.isVisible() or self._syncing:
            return
        self._syncing = True
        try:
            for index in range(self.list.count()):
                row = self.list.item(index)
                item = self.board.item(row.data(ID_ROLE))
                row.setSelected(item is not None and item.isSelected())
        finally:
            self._syncing = False

    # ------------------ handlers ------------------
    def _on_rows_selected(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            chosen = {row.data(ID_ROLE) for row in self.list.selectedItems()}
            for item in self.board.items_list():
                item.setSelected(item.item_id in chosen)
        finally:
            self._syncing = False

    def _on_row_clicked(self, row: QListWidgetItem) -> None:
        item = self.board.item(row.data(ID_ROLE))
        if item is not None:
            self.board.frame([item])

    def _on_relink(self) -> None:
        self.board.relink_missing()

    def selected_row_ids(self) -> list[Optional[str]]:
        return [row.data(ID_ROLE) for row in self.list.selectedItems()]
