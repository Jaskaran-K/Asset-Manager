"""The library sidebar's rows: All Assets, Favorites, a row per category (Settings > Categories:
file types and tags, core.category_rules), Uncategorized, and Collections (saved searches, of the profile in use or shown in every one;
right-click one to rename, move or delete it).

Clicking a row emits row_chosen (a filter for the grid) or collection_chosen (a search to run);
the main window applies them. Row counts come from update_counts().
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox, QTreeWidget, QTreeWidgetItem, QWidget

from core import profiles
from core.category_rules import RowFilter, everything_in, fold, names_of, row_filter
from core.scanner import AssetItem
from core.settings import get_categories
from core.tag_repository import EVERY_PROFILE, SavedSearch, TagError, TagRepository
from ui.icons import category_icon, color_dot_icon, resolve_category_colors


class LibrarySidebar(QTreeWidget):
    row_chosen = Signal(str, object)  # filter mode ("all", "favorites", "category", "uncategorized"), RowFilter
    collection_chosen = Signal(object)  # SavedSearch

    def __init__(self, repo: TagRepository, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.repo = repo
        self.setObjectName("sidebarTree")
        self.setHeaderHidden(True)
        self.setIconSize(QSize(16, 16))
        self.setIndentation(16)
        self.itemClicked.connect(self._on_item_clicked)
        self.itemExpanded.connect(self._on_item_expanded)
        self.itemCollapsed.connect(self._on_item_collapsed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        # (tree item, display name, what it shows) for every category row, for count updates
        self._rows: list[tuple[QTreeWidgetItem, str, RowFilter]] = []
        self.categorized = RowFilter()  # what some category shows (the rest is Uncategorized)
        # Categories the user opened (folded names): they start closed, and stay open across rebuilds
        self._open: set[str] = set()
        self._collections_open = False  # (Collections too)
        self._building = False
        self.rebuild()

    @property
    def uses_tags(self) -> bool:
        """Some category shows files by tag: its rows change when files are tagged."""
        return self.categorized.uses_tags

    def rows(self) -> list[tuple[QTreeWidgetItem, str, RowFilter]]:
        return list(self._rows)

    def rebuild(self, collapse: bool = False) -> None:
        """(Re)create the rows from the saved categories, with All Assets selected. Categories are
        closed unless the user opened them (collapse: close those too, e.g. for another profile)."""
        self._building = True
        try:
            self._rebuild(collapse)
        finally:
            self._building = False

    def _rebuild(self, collapse: bool) -> None:
        if collapse:
            self._open.clear()
            self._collections_open = False
        self.clear()
        self._rows.clear()

        self.item_all = QTreeWidgetItem(self, ["All Assets (0)"])
        self.item_all.setIcon(0, color_dot_icon("#93c5fd"))
        self.item_all.setData(0, Qt.ItemDataRole.UserRole, ("all", None))

        self.item_favorites = QTreeWidgetItem(self, ["Favorites (0)"])
        self.item_favorites.setIcon(0, color_dot_icon("#fbbf24"))
        self.item_favorites.setData(0, Qt.ItemDataRole.UserRole, ("favorites", None))
        self.item_favorites.setToolTip(0, "Files you starred: select files and press F")

        categories = resolve_category_colors(get_categories())
        for main in categories:
            main_item = self._category_row(self, main, row_filter(main))
            for sub in main["subcategories"]:
                self._category_row(main_item, sub, row_filter(sub, with_subcategories=False))
            main_item.setExpanded(fold(main["name"]) in self._open)
        self.categorized = everything_in(categories)
        self._has_categories = bool(categories)

        # Catch-all for scanned file types that aren't assigned to any category (with no
        # categories at all it would just repeat All Assets)
        self.item_uncategorized = QTreeWidgetItem(self, ["Uncategorized (0)"])
        self.item_uncategorized.setIcon(0, color_dot_icon("#6b7280"))
        self.item_uncategorized.setData(0, Qt.ItemDataRole.UserRole, ("uncategorized", None))
        self.item_uncategorized.setHidden(True)

        # Saved searches
        self.item_collections = QTreeWidgetItem(self, ["Collections"])
        self.item_collections.setIcon(0, color_dot_icon("#fbbf24"))
        self.item_collections.setToolTip(0, "Saved searches: search for something, then click the star in the search box")
        self.item_collections.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.reload_collections()
        self.item_collections.setExpanded(self._collections_open)

        self.setCurrentItem(self.item_all)

    def _category_row(self, parent, entry: dict, shows: RowFilter) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [entry["name"]])
        item.setIcon(0, category_icon(entry["color"], entry.get("icon")))
        item.setData(0, Qt.ItemDataRole.UserRole, ("category", shows))
        item.setToolTip(0, shows.describe())
        self._rows.append((item, entry["name"], shows))
        return item

    def update_counts(
        self,
        assets: Sequence[AssetItem],
        is_favorite: Callable[[AssetItem], bool],
        ids: Mapping[str, int] = {},  # noqa: B006 (read only)
        tag_names: Mapping[int, frozenset[str]] = {},  # noqa: B006
    ) -> None:
        """Refresh the file counts shown on each row. ids and tag_names (database ids by str(path),
        folded tag names by id) are needed for categories that show files by tag."""
        ext_counts: dict[str, int] = {}
        for asset in assets:
            ext_counts[asset.extension] = ext_counts.get(asset.extension, 0) + 1
        # (files by type are counted from the type totals; only rows with tags look at each file)
        file_tags = (
            [(a.extension, names_of(ids.get(str(a.path)), tag_names)) for a in assets] if self.uses_tags else []
        )

        def count(shows: RowFilter) -> int:
            if not shows.uses_tags:
                return sum(ext_counts.get(e, 0) for e in shows.extensions)
            return sum(1 for ext, names in file_tags if shows.matches(ext, names))

        self.item_all.setText(0, f"All Assets ({len(assets)})")
        self.item_favorites.setText(0, f"Favorites ({sum(1 for a in assets if is_favorite(a))})")
        for item, name, shows in self._rows:
            item.setText(0, f"{name} ({count(shows)})")

        uncategorized = len(assets) - count(self.categorized)
        self.item_uncategorized.setText(0, f"Uncategorized ({uncategorized})")
        self.item_uncategorized.setHidden(uncategorized == 0 or not self._has_categories)

    # Collections ---------------------------------------------------------------
    def reload_collections(self) -> None:
        root = self.item_collections
        root.takeChildren()
        try:
            searches = self.repo.collections_for(profiles.current_profile().id)
        except Exception:  # pylint: disable=broad-except
            searches = []  # tag database unavailable: browsing still works
        for saved in searches:
            item = QTreeWidgetItem(root, [saved.name])
            item.setIcon(0, color_dot_icon("#fde68a"))
            item.setData(0, Qt.ItemDataRole.UserRole, ("collection", saved))
            shared = "\nShown in every profile" if saved.profile is EVERY_PROFILE else ""
            item.setToolTip(0, f"{saved.query}{shared}\nRight-click to rename, move or delete")
        root.setHidden(not searches)

    def save_collection(self, query: str) -> Optional[str]:
        """Ask for a name and save the search as a collection: its name, or None if cancelled."""
        name, ok = QInputDialog.getText(self, "Save Collection", f"Name for this search:\n{query}")
        while ok:
            try:
                saved: SavedSearch = self.repo.save_search(name, query, profiles.current_profile().id)
            except TagError as exc:
                QMessageBox.warning(self, "Can't Save Collection", str(exc))
                name, ok = QInputDialog.getText(self, "Save Collection", f"Name for this search:\n{query}", text=name)
                continue
            self.reload_collections()
            self.item_collections.setExpanded(True)  # (show the one just saved)
            return saved.name
        return None

    # Events --------------------------------------------------------------------
    def _category_name(self, item: QTreeWidgetItem) -> Optional[str]:
        """The folded name of a main category row, else None."""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if item.parent() is not None or not data or data[0] != "category":
            return None
        return next((fold(name) for row, name, _shows in self._rows if row is item), None)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        self._remember(item, True)

    def _on_item_collapsed(self, item: QTreeWidgetItem) -> None:
        self._remember(item, False)

    def _remember(self, item: QTreeWidgetItem, is_open: bool) -> None:
        if self._building:
            return
        if item is self.item_collections:
            self._collections_open = is_open
            return
        name = self._category_name(item)
        if name is not None:
            (self._open.add if is_open else self._open.discard)(name)

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
        menu.setToolTipsVisible(True)
        action_rename = menu.addAction("Rename...")
        action_every = menu.addAction("Show in Every Profile")
        action_every.setCheckable(True)
        action_every.setChecked(saved.profile is EVERY_PROFILE)
        action_every.setToolTip("Show this collection whichever profile is in use (off: only in this one)")
        others = [p for p in profiles.list_profiles() if p.id != profiles.current_profile().id]
        moves = {}
        if others:
            move_menu = menu.addMenu("Move to Profile")
            move_menu.setToolTipsVisible(True)
            for profile in others:
                moves[move_menu.addAction(profile.name)] = profile
        action_delete = menu.addAction("Delete")
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        menu.deleteLater()
        if chosen is action_every or chosen in moves:
            target = moves[chosen].id if chosen in moves else (
                EVERY_PROFILE if action_every.isChecked() else profiles.current_profile().id
            )
            try:
                self.repo.move_saved_search(saved.id, target)
            except TagError as exc:
                QMessageBox.warning(self, "Can't Move Collection", str(exc))
            self.reload_collections()
            return
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
