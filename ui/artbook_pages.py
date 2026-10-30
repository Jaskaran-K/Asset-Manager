"""The Artbook's Pages list: the body of its slide-out sidebar (ui.sidebar).

Folders (which can hold folders) and pages, in the order kept by core.artbook_store's index,
each page with a small picture of its board. Click a page to open it, or a folder to open /
close it; the pencil on the hovered or selected row (or F2, or double-clicking a page) renames
in place; drag to reorder, or drop on a folder to move inside it; right-click for the rest. The search box filters pages by name (a folder's name matching shows all of it).

Page changes go through the ArtbookPanel (the open page lives there, newer than its file);
folder changes go straight to the store. Neither has undo: a deleted page goes to the
Recycle Bin, and deleting a folder keeps what was in it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QEvent, QModelIndex, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QCursor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QToolTip,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import artbook_store as store
from core import artbook_share, pur_import
from core.settings import get_last_import_dir, set_last_import_dir
from ui import shortcuts
from ui.duotone import draw_icon, set_icon
from ui.themes import theme_color

if TYPE_CHECKING:
    from ui.artbook import ArtbookPanel

THUMB_SIZE = QSize(40, 30)  # page rows; folder rows are lower
FOLDER_ICON_SIZE = QSize(40, 20)
PENCIL = 18  # the rename button drawn at the right of the hovered / selected row
PENCIL_MARGIN = 10
KIND_ROLE = Qt.ItemDataRole.UserRole  # ("page" | "folder", id)


def confirm(parent: QWidget, title: str, text: str) -> bool:
    """Ask before doing something (tests replace this)."""
    answer = QMessageBox.question(parent, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
    return answer == QMessageBox.StandardButton.Yes


def _canvas(size: QSize, ratio: float) -> QPixmap:
    pixmap = QPixmap(round(size.width() * ratio), round(size.height() * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    return pixmap


def page_icon(page_id: str, ratio: float) -> QIcon:
    """The page's thumbnail on a small rounded card (a page symbol when it has none yet)."""
    pixmap = _canvas(THUMB_SIZE, ratio)
    painter = QPainter(pixmap)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    card = QRectF(0.5, 0.5, THUMB_SIZE.width() - 1, THUMB_SIZE.height() - 1)
    path = QPainterPath()
    path.addRoundedRect(card, 4, 4)
    background = QColor(theme_color("text_faint"))
    background.setAlpha(40)
    painter.fillPath(path, background)
    thumb = QPixmap(str(store.thumbnail_path(page_id)))
    if thumb.isNull():
        draw_icon(painter, QRectF(card.center().x() - 9, card.center().y() - 9, 18, 18), "page", "text")
    else:
        fitted = QSize(thumb.width(), thumb.height()).scaled(
            round(card.width() * ratio), round(card.height() * ratio), Qt.AspectRatioMode.KeepAspectRatio,
        )
        target = QRectF(0, 0, fitted.width() / ratio, fitted.height() / ratio)
        target.moveCenter(card.center())
        painter.setClipPath(path)
        painter.drawPixmap(target, thumb, QRectF(thumb.rect()))
        painter.setClipping(False)
    border = QColor(theme_color("text_faint"))
    border.setAlpha(90)
    painter.setPen(border)
    painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)


def folder_icon(ratio: float) -> QIcon:
    """A folder; a light one for the selected row (the accent one would vanish on its accent pill)."""
    icon = QIcon()
    for tone, mode in (("accent", QIcon.Mode.Normal), ("light", QIcon.Mode.Selected)):
        pixmap = _canvas(FOLDER_ICON_SIZE, ratio)
        painter = QPainter(pixmap)
        draw_icon(painter, QRectF((FOLDER_ICON_SIZE.width() - 18) / 2, 1, 18, 18), "folder", tone)
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


class PagesTree(QTreeWidget):
    """The tree of folders and pages. Drops are handled here and applied to the index."""

    def __init__(self, pages: "PagesPanel"):
        super().__init__()
        self.pages = pages
        self.setObjectName("sidebarTree")
        self.setProperty("pencil", True)  # room for the rename pencil (stylesheet)
        self.setMouseTracking(True)
        self._pencil_press = False
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setIconSize(THUMB_SIZE)
        self.setUniformRowHeights(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)  # double-click / F2, below
        self.setExpandsOnDoubleClick(False)  # a click opens / closes a folder
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.invisibleRootItem().setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)

    # ------------------ the rename pencil ------------------
    def pencil_rect(self, index: QModelIndex) -> QRect:
        row = self.visualRect(index)
        right = self.viewport().width() - PENCIL_MARGIN
        return QRect(right - PENCIL, row.center().y() - PENCIL // 2 + 1, PENCIL, PENCIL)

    def _shows_pencil(self, index: QModelIndex) -> bool:
        if not index.isValid() or (self.state() == QAbstractItemView.State.EditingState and index == self.currentIndex()):
            return False
        under_mouse = self.viewport().mapFromGlobal(QCursor.pos())
        hovered = self.viewport().rect().contains(under_mouse) and self.indexAt(under_mouse) == index
        return hovered or self.selectionModel().isSelected(index)

    def drawRow(self, painter: QPainter, option, index: QModelIndex) -> None:
        super().drawRow(painter, option, index)
        if self._shows_pencil(index):
            tone = "light" if self.selectionModel().isSelected(index) else "accent"
            draw_icon(painter, QRectF(self.pencil_rect(index)), "edit", tone)

    def _pencil_at(self, pos) -> Optional[QModelIndex]:
        index = self.indexAt(pos)
        return index if self._shows_pencil(index) and self.pencil_rect(index).contains(pos) else None

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        over = self._pencil_at(event.position().toPoint()) is not None
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)
        self.viewport().update()  # the pencil follows the hovered row

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.viewport().update()

    def mousePressEvent(self, event) -> None:
        index = self._pencil_at(event.position().toPoint()) if event.button() == Qt.MouseButton.LeftButton else None
        if index is not None:
            self._pencil_press = True  # rename only: no opening, toggling or dragging
            item = self.itemFromIndex(index)
            self.setCurrentItem(item)
            self.editItem(item, 0)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._pencil_press:
            self._pencil_press = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._pencil_at(event.position().toPoint()) is not None:
            event.accept()  # a fast second click on the pencil: still just the rename
            return
        super().mouseDoubleClickEvent(event)

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip and self._pencil_at(event.pos()) is not None:
            QToolTip.showText(event.globalPos(), "Rename (F2)", self.viewport())
            return True
        return super().viewportEvent(event)

    def dropEvent(self, event) -> None:
        source = self.currentItem()
        if event.source() is not self or source is None:
            event.ignore()
            return
        target = self.itemAt(event.position().toPoint())
        indicator = self.dropIndicatorPosition()
        event.setDropAction(Qt.DropAction.IgnoreAction)  # the tree is rebuilt from the index instead
        event.accept()
        self.pages.move_item(source, target, indicator)


class PagesPanel(QWidget):
    """Search box over the tree, plus the New Page / New Folder buttons for the sidebar header."""

    def __init__(self, artbook: "ArtbookPanel"):
        super().__init__()
        self.artbook = artbook
        self._building = False
        self._items: dict[tuple[str, str], QTreeWidgetItem] = {}
        self._names: dict[str, str] = {}  # page id -> name
        self._open: dict[str, bool] = {}  # folder id -> expanded (outside a search)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 0)
        layout.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search pages")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)
        self.tree = PagesTree(self)
        layout.addWidget(self.tree, 1)
        self.setFocusProxy(self.tree)

        self.btn_new_page = QPushButton()
        set_icon(self.btn_new_page, "new_page", size=16)
        self.btn_new_page.setToolTip("New page (in the selected folder)")
        self.btn_new_folder = QPushButton()
        set_icon(self.btn_new_folder, "new_folder", size=16)
        self.btn_new_folder.setToolTip("New folder (inside the selected folder)")
        self.btn_import = QPushButton()
        set_icon(self.btn_import, "import", size=16)
        self.btn_import.setToolTip("Import pages: shared .artbook pages, or PureRef boards (.pur) with their layouts (or drop them on the board)")
        for button in (self.btn_new_page, self.btn_new_folder, self.btn_import):
            button.setObjectName("sidebarAction")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new_page.clicked.connect(self._on_new_page_clicked)
        self.btn_new_folder.clicked.connect(self._on_new_folder_clicked)
        self.btn_import.clicked.connect(self._on_import_clicked)

        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemCollapsed.connect(self._on_item_collapsed)
        self.tree.customContextMenuRequested.connect(self._show_menu)
        for shortcut_id, handler in (("pages.rename", self.rename_selected), ("pages.delete", self.delete_selected)):
            action = QAction(self.tree)
            action.setShortcut(shortcuts.sequence(shortcut_id))
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.triggered.connect(handler)
            self.tree.addAction(action)

    def header_widgets(self) -> list[QWidget]:
        return [self.btn_new_page, self.btn_new_folder, self.btn_import]

    # ------------------ building ------------------
    def rebuild(self) -> None:
        """Re-read the index and page names, and show them (keeping the search)."""
        index = store.page_tree()
        self._names = {p["id"]: p.get("name") or "Untitled" for p in store.list_pages()}
        self._open = {f["id"]: f["open"] for f in store.walk_folders(index)}
        scroll = self.tree.verticalScrollBar().value()
        self._building = True
        try:
            self.tree.clear()
            self._items.clear()
            ratio = self.devicePixelRatioF()
            self._fill(self.tree.invisibleRootItem(), index, folder_icon(ratio), ratio)
        finally:
            self._building = False
        self._apply_filter()
        self.show_current()
        self.tree.verticalScrollBar().setValue(scroll)

    def _fill(self, parent: QTreeWidgetItem, folder: dict[str, Any], icon: QIcon, ratio: float) -> None:
        for sub in folder["folders"]:
            item = QTreeWidgetItem(parent, [sub["name"]])
            item.setIcon(0, icon)
            item.setData(0, KIND_ROLE, ("folder", sub["id"]))
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
            )
            item.setToolTip(0, f"{sub['name']}\nClick to open or close. Drop pages or folders here to move them inside")
            self._items[("folder", sub["id"])] = item
            self._fill(item, sub, icon, ratio)
            item.setExpanded(sub["open"])
        for page_id in folder["pages"]:
            name = self._names.get(page_id, "Untitled")
            item = QTreeWidgetItem(parent, [name])
            item.setIcon(0, page_icon(page_id, ratio))
            item.setData(0, KIND_ROLE, ("page", page_id))
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
            item.setToolTip(0, f"{name}\nClick to open. The pencil (or double-click) renames it")
            self._items[("page", page_id)] = item

    def item_for(self, kind: str, item_id: str) -> Optional[QTreeWidgetItem]:
        return self._items.get((kind, item_id))

    def show_current(self) -> None:
        """Highlight the open page (bold, selected) and make sure it can be seen."""
        current = self.artbook.current_page_id()
        self._building = True
        try:
            for (kind, item_id), item in self._items.items():
                if kind == "page":
                    font = item.font(0)
                    font.setBold(item_id == current)
                    item.setFont(0, font)
        finally:
            self._building = False
        item = self.item_for("page", current) if current else None
        if item is not None:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)

    def refresh_thumbnail(self, page_id: str) -> None:
        item = self.item_for("page", page_id)
        if item is not None:
            self._building = True
            try:
                item.setIcon(0, page_icon(page_id, self.devicePixelRatioF()))
            finally:
                self._building = False

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.StyleChange and self._items:
            QTimer.singleShot(0, self.rebuild)  # the theme changed: the cards' colors come from it

    # ------------------ search ------------------
    def _apply_filter(self) -> None:
        words = self.search.text().casefold().split()

        def matches(text: str) -> bool:
            return all(word in text.casefold() for word in words)

        def visit(item: QTreeWidgetItem, parent_matches: bool) -> bool:
            kind = item.data(0, KIND_ROLE)[0]
            here = parent_matches or matches(item.text(0))
            if kind == "page":
                item.setHidden(not here)
                return here
            shown = [visit(item.child(i), here) for i in range(item.childCount())]
            visible = here or any(shown)
            item.setHidden(not visible)
            if words and visible:
                item.setExpanded(True)  # while searching (not remembered)
            return visible

        root = self.tree.invisibleRootItem()
        self._building = True
        try:
            for i in range(root.childCount()):
                visit(root.child(i), not words)
            if not words:
                for (kind, item_id), item in self._items.items():
                    if kind == "folder":
                        item.setExpanded(self._open.get(item_id, True))
        finally:
            self._building = False

    # ------------------ clicks ------------------
    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        kind, item_id = item.data(0, KIND_ROLE)
        if kind == "page":
            self.artbook.open_page(item_id)
            self.artbook.sidebar.close_if_floating()
        else:
            item.setExpanded(not item.isExpanded())

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, KIND_ROLE)[0] == "page":  # folders: a click opens / closes them (the pencil renames)
            self.tree.editItem(item, 0)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        self._remember_open(item, True)

    def _on_item_collapsed(self, item: QTreeWidgetItem) -> None:
        self._remember_open(item, False)

    def _remember_open(self, item: QTreeWidgetItem, open_: bool) -> None:
        if not self._building and not self.search.text().strip():
            folder_id = item.data(0, KIND_ROLE)[1]
            self._open[folder_id] = open_
            store.set_folder_open(folder_id, open_)

    # ------------------ renaming ------------------
    def rename_selected(self) -> None:
        item = self.tree.currentItem()
        if item is not None:
            self.tree.editItem(item, 0)

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._building:
            return
        kind, item_id = item.data(0, KIND_ROLE)
        try:
            if kind == "page":
                self.artbook.rename_page(item_id, item.text(0))
            else:
                store.rename_folder(item_id, item.text(0))
        except store.PageError as exc:
            self.artbook.board.show_message(str(exc))
        QTimer.singleShot(0, self.rebuild)  # after the editor closes: shows the cleaned-up name

    # ------------------ new ------------------
    def current_folder(self) -> Optional[str]:
        """The folder new things go in (see _selected_folder)."""
        return self._selected_folder()

    def duplicate(self, page_id: str) -> None:
        self._duplicate(page_id)

    def _selected_folder(self) -> Optional[str]:
        """The folder new things go in: the selected one, or the selected page's."""
        item = self.tree.currentItem()
        if item is None:
            return None
        kind, item_id = item.data(0, KIND_ROLE)
        if kind == "folder":
            return item_id
        parent = item.parent()
        return parent.data(0, KIND_ROLE)[1] if parent is not None else None

    def _on_new_page_clicked(self) -> None:
        self.new_page(self._selected_folder())

    def _on_new_folder_clicked(self) -> None:
        self.new_folder(self._selected_folder())

    def _on_import_clicked(self) -> None:
        self.import_boards(self._selected_folder())

    def import_boards(self, folder_id: Optional[str]) -> None:
        """Pick .artbook pages / PureRef boards; each becomes a page in folder_id."""
        artbook, pur = f"*{artbook_share.SUFFIX}", f"*{pur_import.SUFFIX}"
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Import Pages", get_last_import_dir(),
            f"Pages and boards ({artbook} {pur});;Artbook pages ({artbook});;PureRef boards ({pur})",
        )
        if paths:
            set_last_import_dir(str(Path(paths[0]).parent))
            self.artbook.import_boards([Path(p) for p in paths], folder_id)

    def new_page(self, folder_id: Optional[str]) -> None:
        """A new page in folder_id: opened, with its name ready to type over."""
        try:
            page_id = self.artbook.new_page(folder_id)
        except store.PageError as exc:
            self.artbook.board.show_message(str(exc))
            return
        self._start_rename("page", page_id)

    def new_folder(self, parent_id: Optional[str]) -> None:
        try:
            folder_id = store.create_folder("New Folder", parent_id)
        except store.PageError as exc:
            self.artbook.board.show_message(str(exc))
            return
        self.rebuild()
        self._start_rename("folder", folder_id)

    def _start_rename(self, kind: str, item_id: str) -> None:
        if self.search.text():
            self.search.clear()  # the new row might not match
        item = self.item_for(kind, item_id)
        if item is not None and self.isVisible():
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
            self.tree.setFocus()
            self.tree.editItem(item, 0)

    # ------------------ delete ------------------
    def delete_selected(self) -> None:
        item = self.tree.currentItem()
        if item is not None:
            kind, item_id = item.data(0, KIND_ROLE)
            if kind == "page":
                self.delete_page(item_id)
            else:
                self.delete_folder(item_id)

    def delete_page(self, page_id: str) -> None:
        name = self._names.get(page_id, "this page")
        if not confirm(self, "Delete Page", f'Move "{name}" to the Recycle Bin?\n\nYou can restore it from there.'):
            return
        try:
            self.artbook.delete_page(page_id)
        except store.PageError as exc:
            self.artbook.board.show_message(str(exc))
        self.rebuild()

    def delete_folder(self, folder_id: str) -> None:
        try:
            store.delete_folder(folder_id)  # what was in it moves up a level: nothing is lost
        except store.PageError as exc:
            self.artbook.board.show_message(str(exc))
        self.rebuild()

    # ------------------ moving ------------------
    def move_item(self, source: QTreeWidgetItem, target: Optional[QTreeWidgetItem], indicator) -> None:
        """Apply a drop: source (a page or folder row) dropped at target, as the indicator showed."""
        kind, item_id = source.data(0, KIND_ROLE)
        Position = QAbstractItemView.DropIndicatorPosition
        if target is None or indicator == Position.OnViewport:
            parent_id, position = None, None
        elif indicator == Position.OnItem:
            target_kind, target_id = target.data(0, KIND_ROLE)
            if target_kind != "folder":
                return
            parent_id, position = target_id, None
        else:
            target_kind, target_id = target.data(0, KIND_ROLE)
            parent = target.parent()
            parent_id = parent.data(0, KIND_ROLE)[1] if parent is not None else None
            after = indicator == Position.BelowItem
            if kind == target_kind:
                siblings = [
                    (parent or self.tree.invisibleRootItem()).child(i)
                    for i in range((parent or self.tree.invisibleRootItem()).childCount())
                ]
                same_kind = [s for s in siblings if s.data(0, KIND_ROLE)[0] == kind]
                position = same_kind.index(target) + (1 if after else 0)
            else:
                # Folders always come before pages: next to the other kind means first pages / last folders
                position = 0 if kind == "page" else None
        try:
            if kind == "page":
                store.move_page(item_id, parent_id, position)
            else:
                store.move_folder(item_id, parent_id, position)
        except store.PageError as exc:
            self.artbook.board.show_message(str(exc))
        QTimer.singleShot(0, self.rebuild)  # not during the drop: the dragged row still exists

    def move_to(self, kind: str, item_id: str, folder_id: Optional[str]) -> None:
        try:
            if kind == "page":
                store.move_page(item_id, folder_id)
            else:
                store.move_folder(item_id, folder_id)
        except store.PageError as exc:
            self.artbook.board.show_message(str(exc))
        self.rebuild()

    # ------------------ menu ------------------
    def _show_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        if item is None:
            menu.addAction("New Page").triggered.connect(lambda: self.new_page(None))
            menu.addAction("New Folder").triggered.connect(lambda: self.new_folder(None))
            menu.addSeparator()
            menu.addAction("Import Pages...").triggered.connect(lambda: self.import_boards(None))
        else:
            self.tree.setCurrentItem(item)
            kind, item_id = item.data(0, KIND_ROLE)
            if kind == "page":
                menu.addAction("Open").triggered.connect(lambda: self._on_item_clicked(item, 0))
                rename = menu.addAction("Rename")
                rename.setShortcut(shortcuts.sequence("pages.rename"))  # a hint: the tree handles the key
                rename.triggered.connect(lambda: self.tree.editItem(item, 0))
                menu.addAction("Duplicate").triggered.connect(lambda: self._duplicate(item_id))
                menu.addAction("Export Page...").triggered.connect(lambda: self.artbook.export_page(item_id))
                self._add_move_menu(menu, kind, item_id, item)
                menu.addSeparator()
                delete = menu.addAction("Delete...")
                delete.setShortcut(shortcuts.sequence("pages.delete"))
                delete.triggered.connect(lambda: self.delete_page(item_id))
            else:
                menu.addAction("New Page Here").triggered.connect(lambda: self.new_page(item_id))
                menu.addAction("New Folder Here").triggered.connect(lambda: self.new_folder(item_id))
                menu.addAction("Import Pages Here...").triggered.connect(lambda: self.import_boards(item_id))
                menu.addSeparator()
                rename = menu.addAction("Rename")
                rename.setShortcut(shortcuts.sequence("pages.rename"))
                rename.triggered.connect(lambda: self.tree.editItem(item, 0))
                self._add_move_menu(menu, kind, item_id, item)
                menu.addSeparator()
                delete = menu.addAction("Delete Folder (keeps its pages)")
                delete.setShortcut(shortcuts.sequence("pages.delete"))
                delete.triggered.connect(lambda: self.delete_folder(item_id))
        menu.exec(self.tree.viewport().mapToGlobal(pos))
        menu.deleteLater()  # its actions carry shortcuts: don't leave them around

    def _add_move_menu(self, menu: QMenu, kind: str, item_id: str, item: QTreeWidgetItem) -> None:
        index = store.page_tree()
        parent = item.parent()
        here = parent.data(0, KIND_ROLE)[1] if parent is not None else None
        excluded: set[str] = set()
        if kind == "folder":
            moving = next(f for f in store.walk_folders(index) if f["id"] == item_id)
            excluded = {item_id, *(f["id"] for f in store.walk_folders(moving))}
        move = menu.addMenu("Move To")
        top = move.addAction("Top Level")
        top.setEnabled(here is not None)
        top.triggered.connect(lambda: self.move_to(kind, item_id, None))

        def add(folder: dict[str, Any], depth: int) -> None:
            for sub in folder["folders"]:
                if sub["id"] in excluded:
                    continue
                action = move.addAction("    " * depth + sub["name"])
                action.setEnabled(sub["id"] != here)
                action.triggered.connect(lambda _checked=False, target=sub["id"]: self.move_to(kind, item_id, target))
                add(sub, depth + 1)

        add(index, 0)

    def _duplicate(self, page_id: str) -> None:
        try:
            copy = self.artbook.duplicate_page(page_id)
        except store.PageError as exc:
            self.artbook.board.show_message(str(exc))
            return
        self.rebuild()
        item = self.item_for("page", copy["id"])
        if item is not None:
            self.tree.setCurrentItem(item)
