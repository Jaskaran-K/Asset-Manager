"""The library sidebar's rows: All Assets, Favorites, a row per category (Settings > Categories),
Uncategorized, and Collections (saved searches; right-click one to rename or delete it).

Clicking a row emits row_chosen (a filter for the grid) or collection_chosen (a search to run);
the main window applies them. Row counts come from update_counts().
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox, QTreeWidget, QTreeWidgetItem, QWidget

from core.scanner import AssetItem
from core.settings import category_extensions, get_categories
from core.tag_repository import SavedSearch, TagError, TagRepository
from ui.icons import category_icon, color_dot_icon, resolve_category_colors


class LibrarySidebar(QTreeWidget):
    row_chosen = Signal(str, object)  # filter mode ("all", "favorites", "exts", "uncategorized"), extensions
    collection_chosen = Signal(object)  # SavedSearch

    def __init__(self, repo: TagRepository, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.repo = repo
        self.setObjectName("sidebarTree")
        self.setHeaderHidden(True)
        self.setIconSize(QSize(16, 16))
        self.setIndentation(16)
        self.itemClicked.connect(self._on_item_clicked)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        # (tree item, display name, extensions) for every category row, for count updates
        self._rows: list[tuple[QTreeWidgetItem, str, frozenset[str]]] = []
        self.categorized: frozenset[str] = frozenset()  # extensions some category takes
        self.rebuild()

    def rows(self) -> list[tuple[QTreeWidgetItem, str, frozenset[str]]]:
        return list(self._rows)

    def rebuild(self) -> None:
        """(Re)create the rows from the saved categories, with All Assets selected."""
        self.clear()
        self._rows.clear()

        self.item_all = QTreeWidgetItem(self, ["All Assets (0)"])
        self.item_all.setIcon(0, color_dot_icon("#93c5fd"))
        self.item_all.setData(0, Qt.ItemDataRole.UserRole, ("all", None))

        self.item_favorites = QTreeWidgetItem(self, ["Favorites (0)"])
        self.item_favorites.setIcon(0, color_dot_icon("#fbbf24"))
        self.item_favorites.setData(0, Qt.ItemDataRole.UserRole, ("favorites", None))
        self.item_favorites.setToolTip(0, "Files you starred: select files and press F")

        categorized: set[str] = set()
        for main in resolve_category_colors(get_categories()):
            main_exts = frozenset(category_extensions(main))
            categorized |= main_exts
            main_item = QTreeWidgetItem(self, [main["name"]])
            main_item.setIcon(0, category_icon(main["color"], main.get("icon")))
            main_item.setData(0, Qt.ItemDataRole.UserRole, ("exts", main_exts))
            main_item.setToolTip(0, ", ".join(sorted(main_exts)) or "No file types assigned")
            main_item.setExpanded(True)
            self._rows.append((main_item, main["name"], main_exts))

            for sub in main["subcategories"]:
                sub_exts = frozenset(sub["extensions"])
                sub_item = QTreeWidgetItem(main_item, [sub["name"]])
                sub_item.setIcon(0, category_icon(sub["color"], sub.get("icon")))
                sub_item.setData(0, Qt.ItemDataRole.UserRole, ("exts", sub_exts))
                sub_item.setToolTip(0, ", ".join(sorted(sub_exts)) or "No file types assigned")
                self._rows.append((sub_item, sub["name"], sub_exts))

        self.categorized = frozenset(categorized)

        # Catch-all for scanned file types that aren't assigned to any category
        self.item_uncategorized = QTreeWidgetItem(self, ["Uncategorized (0)"])
        self.item_uncategorized.setIcon(0, color_dot_icon("#6b7280"))
        self.item_uncategorized.setData(0, Qt.ItemDataRole.UserRole, ("uncategorized", None))
        self.item_uncategorized.setHidden(True)

        # Saved searches
        self.item_collections = QTreeWidgetItem(self, ["Collections"])
        self.item_collections.setIcon(0, color_dot_icon("#fbbf24"))
        self.item_collections.setToolTip(0, "Saved searches: search for something, then click the star in the search box")
        self.item_collections.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.item_collections.setExpanded(True)
        self.reload_collections()

        self.setCurrentItem(self.item_all)

    def update_counts(self, assets: Sequence[AssetItem], is_favorite: Callable[[AssetItem], bool]) -> None:
        """Refresh the file counts shown on each row."""
        ext_counts: dict[str, int] = {}
        for asset in assets:
            ext_counts[asset.extension] = ext_counts.get(asset.extension, 0) + 1

        self.item_all.setText(0, f"All Assets ({len(assets)})")
        self.item_favorites.setText(0, f"Favorites ({sum(1 for a in assets if is_favorite(a))})")
        for item, name, exts in self._rows:
            item.setText(0, f"{name} ({sum(ext_counts.get(e, 0) for e in exts)})")

        uncategorized = sum(c for e, c in ext_counts.items() if e not in self.categorized)
        self.item_uncategorized.setText(0, f"Uncategorized ({uncategorized})")
        self.item_uncategorized.setHidden(uncategorized == 0)

    # Collections ---------------------------------------------------------------
    def reload_collections(self) -> None:
        root = self.item_collections
        root.takeChildren()
        try:
            searches = self.repo.list_saved_searches()
        except Exception:  # pylint: disable=broad-except
            searches = []  # tag database unavailable: browsing still works
        for saved in searches:
            item = QTreeWidgetItem(root, [saved.name])
            item.setIcon(0, color_dot_icon("#fde68a"))
            item.setData(0, Qt.ItemDataRole.UserRole, ("collection", saved))
            item.setToolTip(0, f"{saved.query}\nRight-click to rename or delete")
        root.setHidden(not searches)

    def save_collection(self, query: str) -> Optional[str]:
        """Ask for a name and save the search as a collection: its name, or None if cancelled."""
        name, ok = QInputDialog.getText(self, "Save Collection", f"Name for this search:\n{query}")
        while ok:
            try:
                saved: SavedSearch = self.repo.save_search(name, query)
            except TagError as exc:
                QMessageBox.warning(self, "Can't Save Collection", str(exc))
                name, ok = QInputDialog.getText(self, "Save Collection", f"Name for this search:\n{query}", text=name)
                continue
            self.reload_collections()
            return saved.name
        return None

    # Events --------------------------------------------------------------------
    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        if data[0] == "collection":
            self.collection_chosen.emit(data[1])
        else:
            self.row_chosen.emit(data[0], data[1])

    def _show_menu(self, pos) -> None:
        item = self.itemAt(pos)
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not data or data[0] != "collection":
            return
        saved = data[1]
        menu = QMenu(self)
        action_rename = menu.addAction("Rename...")
        action_delete = menu.addAction("Delete")
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        menu.deleteLater()
        if chosen is action_rename:
            name, ok = QInputDialog.getText(self, "Rename Collection", "Name:", text=saved.name)
            if ok:
                try:
                    self.repo.rename_saved_search(saved.id, name)
                except TagError as exc:
                    QMessageBox.warning(self, "Can't Rename Collection", str(exc))
                self.reload_collections()
        elif chosen is action_delete:
            self.repo.delete_saved_search(saved.id)
            self.reload_collections()
