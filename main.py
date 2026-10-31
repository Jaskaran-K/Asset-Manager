"""Asset Manager - Windows desktop application using PySide6.

Features:
- Category and format navigation sidebar (3D Models, Textures, Documents)
- Grid view area for asset thumbnails with multi-format support
- Status bar for asset counts and selection details
- Settings for library folders, scanned file types, sidebar categories, and appearance
- Built-in lightweight interactive viewer for 3D objects, textures, PDFs, and videos
- Tagging: automatic file type tags, custom tags (bulk tagging), and tag:name search
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import (
    QItemSelection,
    QItemSelectionModel,
    QObject,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QKeyEvent,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import crash_log
from core.library_snapshot import load_snapshot, save_snapshot
from core.library_watcher import FolderRescan, LibraryWatcher, rescan_folders
from core.scanner import AssetItem, AssetScanner
from core import artbook_store
from core.settings import (
    category_extensions,
    get_artbook_page,
    get_categories,
    get_pages_dir,
    get_background_texture,
    get_file_extensions,
    get_glass_frost,
    get_motion,
    get_scan_folders,
    get_theme,
    get_thumbnail_size,
    is_scan_folder,
    set_scan_folders,
)
from core.metadata import read_cost, read_details
from core.search_query import ParsedQuery, parse_query
from core.tag_repository import NO_MARKS, AssetMeta, Marks, Tag, TagChange, TagError, get_repository
from core.thumbnail_manager import ThumbnailManager, prune_disk_cache
from core.updater import cleanup_after_update
from core.video_tools import apply_media_backend
from core.version import APP_NAME, APP_VERSION
from ui.animations import install_popup_animations, set_motion_mode
from ui.artbook import ArtbookPanel
from ui.asset_grid import ASSET_ROLE, AssetCardDelegate, AssetListModel, GridView, rating_text
from ui.duotone import set_icon
from ui.duplicates_dialog import DuplicatesDialog
from ui.glass import set_frost
from ui.icons import app_icon, category_icon, color_dot_icon, resolve_category_colors
from ui import main_thread_gc
from ui import shortcuts
from ui.settings_dialog import SettingsDialog
from ui.sidebar import SidebarHost
from ui.tag_widgets import TagAssetsDialog, TagSearchBar, run_in_background, wait_for_background
from ui.themes import apply_theme
from ui.toolbar import CompactToolbar
from ui.undo import UndoHistory, UndoStep
from ui.view_modes import ViewModes, ViewSwitchButton
from ui.viewer import AssetViewerDialog, ViewerTarget
from ui.views import install_view_effects

# ----------------------------------------------------------------------
# Background Scanner Thread
# ----------------------------------------------------------------------
class ScanWorker(QObject):
    """Background worker to scan folders without blocking the UI thread."""

    finished = Signal(list, object)  # assets, info ({"ids", "tags", "relinked", "error", "dirs"})
    failed = Signal(str)

    def __init__(self, folders: list[Path], extensions: list[str], save_snapshot: bool = False):
        super().__init__()
        self.folders = folders
        self.extensions = extensions
        self.save_snapshot = save_snapshot  # library scans are remembered for an instant next launch

    def run(self) -> None:
        try:
            scanner = AssetScanner(supported_extensions=self.extensions)
            assets: list[AssetItem] = []
            seen: set[Path] = set()
            directories: set[Path] = set()  # every folder walked, for the library watcher
            # Overlapping folders (e.g. a parent and its subfolder) must not duplicate files
            for folder in self.folders:
                for asset in scanner.scan(folder, recursive=True, directories=directories):
                    if asset.path not in seen:
                        seen.add(asset.path)
                        assets.append(asset)
            assets.sort(key=lambda a: a.name.lower())
            info = self._sync_tags(assets)
            info["dirs"] = directories
            if self.save_snapshot and info["error"] is None:
                save_snapshot(self.folders, self.extensions, assets, info["ids"])
            self.finished.emit(assets, info)
        except Exception as exc:  # pylint: disable=broad-except
            self.failed.emit(str(exc))

    @staticmethod
    def _sync_tags(assets: list[AssetItem]) -> dict:
        """Record the assets and their default file type tags (also backfills older libraries)."""
        repo = get_repository()
        try:
            relinked = repo.relink_moved_assets(assets)  # before sync, so moved files keep their rows
            ids = repo.sync_assets(assets)
            repo.fill_missing_hashes()
            asset_ids = list(ids.values())
            return {
                "ids": ids, "tags": repo.tags_by_asset(asset_ids), "marks": repo.marks_by_asset(asset_ids),
                "relinked": relinked, "error": None,
            }
        except Exception as exc:  # pylint: disable=broad-except
            # Browsing still works without the tag database
            return {"ids": {}, "tags": {}, "marks": {}, "relinked": 0, "error": str(exc)}
        finally:
            repo.close_thread_connection()


# ----------------------------------------------------------------------
# Main Application Window
# ----------------------------------------------------------------------
class MainWindow(QMainWindow):
    """Main window for the Asset Manager."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1220, 780)
        self.setMinimumSize(900, 560)

        # None = library mode (all scan folders); otherwise a single folder opened via "Open Folder"
        self.browse_folder: Optional[Path] = None
        self.all_assets: list[AssetItem] = []

        # Sidebar filter: ("all", None), ("exts", frozenset of extensions), ("uncategorized", None)
        self.filter_mode: str = "all"
        self.filter_value: Optional[frozenset[str]] = None
        self.search_filter: str = ""
        # Tagging: database ids by str(asset.path), each asset's tags, and the tag search result
        # (None when no tag filter is active)
        self.repo = get_repository()
        self._asset_ids: dict[str, int] = {}
        self._asset_tags: dict[int, list[Tag]] = {}
        self._asset_marks: dict[int, Marks] = {}  # favorites / ratings (unmarked files are left out)
        self._asset_meta: dict[int, AssetMeta] = {}  # resolution / duration / faces, read in the background
        self._meta_generation = 0
        self._meta_todo = 0
        self._query = ParsedQuery()  # detail filters in the search text
        self._tag_match_ids: Optional[set[int]] = None
        self._tag_search_generation = 0
        self.thumbnail_size: int = get_thumbnail_size()
        # (tree item, display name, extensions) for every category row, for count updates
        self._sidebar_rows: list[tuple[QTreeWidgetItem, str, frozenset[str]]] = []
        self._categorized_exts: frozenset[str] = frozenset()

        # Background scan worker thread
        self.scan_thread: Optional[QThread] = None
        self.scan_worker: Optional[ScanWorker] = None

        # Asynchronous thumbnail loader & in-memory cache
        self.thumbnail_manager = ThumbnailManager(self)
        self.thumbnail_manager.thumbnail_loaded.connect(self._on_async_thumbnail_loaded)

        # Picks up files added / removed / edited in the scanned folders while the app is open
        self.watcher = LibraryWatcher(self)
        self.watcher.folders_changed.connect(self._on_folders_changed)
        self._rescan_busy = False
        self._queued_folders: list[Path] = []
        self._scan_done_once = False  # a real scan has finished (snapshot data is outdated then)
        self.undo_history = UndoHistory(on_change=lambda: self._update_undo_actions())

        self._init_ui()
        self._load_library()

    def _init_ui(self) -> None:
        """Construct the UI layout and widgets."""
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. The asset panel: its toolbar, then the grid with the slide-out sidebar beside (or over) it
        self.asset_panel = QWidget()
        asset_layout = QVBoxLayout(self.asset_panel)
        asset_layout.setContentsMargins(0, 0, 0, 0)
        asset_layout.setSpacing(0)
        asset_layout.addWidget(self._build_control_bar())
        sidebar_tree = self._build_sidebar()
        self.grid_container = self._build_grid_view()
        self.sidebar = SidebarHost(sidebar_tree, self.grid_container)
        self.sidebar.watch(self.list_grid.viewport())
        self.sidebar.open_changed.connect(self.btn_sidebar.setChecked)
        self.btn_sidebar.setChecked(self.sidebar.is_open())
        asset_layout.addWidget(self.sidebar, 1)
        # Ctrl+B while working in the asset side (the Artbook has its own, for its Pages sidebar)
        toggle_sidebar = QAction(self.asset_panel)
        toggle_sidebar.setShortcut(shortcuts.sequence("main.sidebar"))
        toggle_sidebar.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        toggle_sidebar.triggered.connect(self.sidebar.toggle)
        self.asset_panel.addAction(toggle_sidebar)

        # 2. The Artbook panel, and the views that show either or both (ui.view_modes)
        pages_dir, pages_note = get_pages_dir(), ""
        if pages_dir and Path(pages_dir).is_dir():
            artbook_store.set_pages_dir(Path(pages_dir))
        elif pages_dir:  # (a drive that isn't plugged in: the setting stays, for next time)
            pages_note = f"Your pages folder ({pages_dir}) isn't available, so the default one is used for now."
        self.artbook_panel = ArtbookPanel()
        if pages_note:
            self.artbook_panel.board.show_message(pages_note)
        self.view_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.view_splitter.setChildrenCollapsible(False)
        self.view_splitter.setHandleWidth(3)
        self.view_splitter.addWidget(self.asset_panel)
        self.view_splitter.addWidget(self.artbook_panel)
        main_layout.addWidget(self.view_splitter, 1)
        self.view_modes = ViewModes(
            self, self.view_splitter, self.asset_panel, self.artbook_panel,
            self.asset_toolbar_slot, self.artbook_panel.toolbar_slot, self._build_window_controls,
        )
        self._view_restored = False
        # Narrow asset side (Split View): icon-only buttons and a narrower search box
        self.asset_toolbar = CompactToolbar(
            self.asset_panel, self.control_bar,
            [
                (self.btn_open_folder, "Open Folder"), (self.btn_library, "Library"), (self.btn_refresh, "Refresh"),
                (self.btn_view_asset, "Preview"), (self.btn_tag, "Tag"), (self.btn_tools, "Tools"),
            ],
            on_change=self._on_toolbar_compact,
        )
        self.asset_panel.installEventFilter(self)  # its resizes: see eventFilter
        # The shared View / Settings buttons moving in or out changes what the toolbar needs
        self.view_modes.mode_changed.connect(self._on_view_mode_changed)

        # 3. Status Bar
        self._build_status_bar()

    def _on_view_mode_changed(self, mode: str) -> None:
        self.asset_toolbar.update()
        self.artbook_panel.toolbar.update()
        # Keys like Ctrl+B act on the side with the keyboard focus: keep it on a side you can see
        focus = QApplication.focusWidget()
        if mode == "artbook" and not (focus and self.artbook_panel.isAncestorOf(focus)):
            self.artbook_panel.board.setFocus()
        elif mode in ("asset", "separate") and not (focus and self.asset_panel.isAncestorOf(focus)):
            # The grid, or (an empty library shows the welcome screen instead) the first thing that takes focus
            candidates = [self.list_grid, *self.asset_panel.findChildren(QWidget)]
            target = next(
                (w for w in candidates if w.isVisible() and w.isEnabled() and w.focusPolicy() & Qt.FocusPolicy.TabFocus),
                None,
            )
            if target is not None:
                target.setFocus()

    def _build_window_controls(self, modes: ViewModes) -> QWidget:
        """The View and Settings buttons, at the top right of whichever toolbar is rightmost."""
        controls = QWidget()
        layout = QHBoxLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.btn_view = ViewSwitchButton(modes)
        layout.addWidget(self.btn_view)
        self.btn_settings = QPushButton("Settings")
        set_icon(self.btn_settings, "settings")
        self.btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_settings.setToolTip("Settings (Ctrl+,)")
        self.btn_settings.setShortcut(shortcuts.sequence("main.settings"))
        self.btn_settings.clicked.connect(self._open_settings)
        layout.addWidget(self.btn_settings)
        return controls

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._view_restored:
            # Once the window has its size, so a split gets its proportions right
            self._view_restored = True
            QTimer.singleShot(0, self.view_modes.restore)

    def _build_control_bar(self) -> QWidget:
        """Build top toolbar with folder picker, library toggle, refresh, preview, search, and settings."""
        bar = QWidget()
        bar.setObjectName("controlBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)
        self.control_bar = bar

        # Shows / hides the sidebar
        self.btn_sidebar = QPushButton()
        set_icon(self.btn_sidebar, "menu", size=18)
        self.btn_sidebar.setCheckable(True)
        self.btn_sidebar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sidebar.setToolTip("Show / hide the sidebar (Ctrl+B)")
        self.btn_sidebar.clicked.connect(lambda: self.sidebar.toggle())
        layout.addWidget(self.btn_sidebar)

        # "Open Folder" button: browse a single folder outside the library
        self.btn_open_folder = QPushButton("Open Folder")
        set_icon(self.btn_open_folder, "folder", "light")
        self.btn_open_folder.setObjectName("primaryBtn")
        self.btn_open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_folder.setToolTip("Browse a single folder without adding it to the library")
        self.btn_open_folder.clicked.connect(self._on_choose_folder)
        layout.addWidget(self.btn_open_folder)

        # "Library" split button: the main part returns to the library (all library folders);
        # its menu also adds / removes the folder being browsed
        self.btn_library = QToolButton()
        self.btn_library.setObjectName("splitBtn")
        self.btn_library.setText("Library")
        self.btn_library.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_library.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.btn_library.setCursor(Qt.CursorShape.PointingHandCursor)
        set_icon(self.btn_library, "home", size=16)
        self.btn_library.clicked.connect(self._load_library)
        library_menu = QMenu(self.btn_library)
        library_menu.setToolTipsVisible(True)
        self.action_library = library_menu.addAction("Library")
        self.action_library.setToolTip("Show assets from all library folders (Settings > Folders)")
        set_icon(self.action_library, "home")
        self.action_library.triggered.connect(self._load_library)
        self.action_library_toggle = library_menu.addAction("Add to Library")
        set_icon(self.action_library_toggle, "star")
        self.action_library_toggle.triggered.connect(self._on_toggle_library_folder)
        self.btn_library.setMenu(library_menu)
        layout.addWidget(self.btn_library)

        # "Refresh" button
        self.btn_refresh = QPushButton("Refresh")
        set_icon(self.btn_refresh, "refresh")
        self.btn_refresh.setToolTip("Scan the folders again (changes are also picked up automatically while the app is open)")
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.clicked.connect(self._rescan_current_folder)
        layout.addWidget(self.btn_refresh)

        # "Preview" button
        self.btn_view_asset = QPushButton("Preview")
        set_icon(self.btn_view_asset, "eye", "light")
        self.btn_view_asset.setObjectName("viewBtn")
        self.btn_view_asset.setEnabled(False)
        self.btn_view_asset.setToolTip("Open lightweight viewer for selected asset (or press Space)")
        self.btn_view_asset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_view_asset.clicked.connect(self._open_viewer_for_selected)
        layout.addWidget(self.btn_view_asset)

        # "Tag" button: tag the selected files
        self.btn_tag = QPushButton("Tag")
        set_icon(self.btn_tag, "tag")
        self.btn_tag.setEnabled(False)
        self.btn_tag.setToolTip("Add or remove tags on the selected files (Ctrl+T)")
        self.btn_tag.setShortcut(shortcuts.sequence("main.tag"))
        self.btn_tag.setCursor(Qt.CursorShape.PointingHandCursor)
        # lambda: clicked passes a "checked" bool that must not land in the assets argument
        self.btn_tag.clicked.connect(lambda: self._open_tag_dialog())
        layout.addWidget(self.btn_tag)

        # Path display field
        self.txt_path = QLineEdit()
        self.txt_path.setReadOnly(True)
        self.txt_path.setPlaceholderText("No library folders. Add some in Settings > Folders, or click 'Open Folder'...")
        layout.addWidget(self.txt_path, 1)

        # Search: file names plus tag filter chips (tag:name)
        self.search_bar = TagSearchBar()
        self.search_bar.setMinimumWidth(280)
        self.search_bar.setMaximumWidth(560)
        self.search_bar.changed.connect(self._on_search_changed)
        self.search_bar.save_requested.connect(self._on_save_search)
        self.search_bar.unknown_tag.connect(
            lambda name: self.status_label.setText(f'No tag named "{name}". Create it in Settings > Tags.')
        )
        layout.addWidget(self.search_bar)

        # Tools: library maintenance
        self.btn_tools = QPushButton("Tools")
        set_icon(self.btn_tools, "tools")
        self.btn_tools.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_tools.setToolTip("Find duplicates; back up, restore, export or import your tags")
        self.btn_tools.setMenu(self._build_tools_menu())
        layout.addWidget(self.btn_tools)

        # The window's View / Settings buttons sit here unless the Artbook is on the right
        self.asset_toolbar_slot = QHBoxLayout()
        self.asset_toolbar_slot.setSpacing(10)
        layout.addLayout(self.asset_toolbar_slot)

        return bar

    def _build_tools_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        # Undo / redo for tags, favorites and ratings; added to the window too, so the keys work anywhere
        self.action_undo = QAction("Undo", self)
        self.action_undo.setShortcut(shortcuts.sequence("main.undo"))
        self.action_undo.triggered.connect(self._on_undo)
        self.action_redo = QAction("Redo", self)
        self.action_redo.setShortcut(shortcuts.sequence("main.redo"))
        self.action_redo.triggered.connect(self._on_redo)
        for action in (self.action_undo, self.action_redo):
            menu.addAction(action)
            self.addAction(action)
        menu.addSeparator()
        self._update_undo_actions()
        entries = (
            ("Find Duplicates...", "Find identical files in the files shown, and recycle extra copies", self._on_find_duplicates),
            None,
            ("Export Tags...", "Save your custom tags and the files they're on to a .json file", self._on_export_tags),
            ("Import Tags...", "Add tags from an exported .json file (nothing is removed)", self._on_import_tags),
            None,
            ("Back Up Library Now", "Save a copy of the tag database (tags, favorites, ratings)", self._on_backup_now),
            ("Restore Library Backup...", "Go back to an earlier copy of the tag database", self._on_restore_backup),
            ("Open Backups Folder", "Backups are also made automatically before upgrades and updates", self._on_open_backups),
        )
        for entry in entries:
            if entry is None:
                menu.addSeparator()
                continue
            text, tip, handler = entry
            action = menu.addAction(text)
            action.setToolTip(tip)
            action.triggered.connect(handler)
        return menu

    def _update_undo_actions(self) -> None:
        undo, redo = self.undo_history.next_undo(), self.undo_history.next_redo()
        self.action_undo.setEnabled(undo is not None)
        self.action_undo.setText(f"Undo {undo.text}" if undo else "Undo")
        self.action_redo.setEnabled(redo is not None)
        self.action_redo.setText(f"Redo {redo.text}" if redo else "Redo")

    def _on_undo(self) -> None:
        self._run_history(self.undo_history.undo, "undo", "Undid")

    def _on_redo(self) -> None:
        self._run_history(self.undo_history.redo, "redo", "Redid")

    def _run_history(self, action, verb: str, past: str) -> None:
        try:
            step = action()
        except Exception as exc:  # pylint: disable=broad-except
            self.undo_history.clear()  # the library changed underneath (e.g. a restored backup)
            self.status_label.setText(f"Couldn't {verb}: {exc}")
            return
        self.status_label.setText(f"{past} the {step.text}." if step else f"Nothing to {verb}.")

    def _push_tag_undo(self, change: TagChange) -> None:
        files = len(change.asset_ids)
        text = f"tag change on {files} file{'s' if files != 1 else ''}"

        def undo() -> None:
            self.repo.undo_tag_change(change)
            self._after_tags_changed(change.asset_ids)

        def redo() -> None:
            self.repo.redo_tag_change(change)
            self._after_tags_changed(change.asset_ids)

        self.undo_history.push(UndoStep(text, undo, redo))

    def _on_find_duplicates(self) -> None:
        if not self.all_assets:
            QMessageBox.information(self, "Find Duplicates", "There are no files to compare. Scan a folder first.")
            return
        dialog = DuplicatesDialog(self.repo, self.all_assets, self)
        dialog.exec()
        dialog.deleteLater()
        if dialog.removed:
            self.status_label.setText(f"Moved {dialog.removed} duplicate file{'s' if dialog.removed != 1 else ''} to the Recycle Bin.")
            # The library watcher drops them from the grid; rescan if folders aren't all watched
            if not self.watcher.is_complete():
                self._rescan_current_folder()

    def _on_export_tags(self) -> None:
        default = Path.home() / "Documents" / "asset-manager-tags.json"
        target, _ = QFileDialog.getSaveFileName(self, "Export Tags", str(default), "Tag export (*.json)")
        if not target:
            return
        try:
            files = self.repo.export_tags(target)
        except OSError as exc:
            QMessageBox.warning(self, "Export Failed", f"The file couldn't be saved:\n{exc}")
            return
        self.status_label.setText(f"Exported tags for {files} file{'s' if files != 1 else ''} to {target}")

    def _on_import_tags(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, "Import Tags", str(Path.home() / "Documents"), "Tag export (*.json)")
        if not source:
            return
        try:
            result = self.repo.import_tags(source)
        except TagError as exc:
            QMessageBox.warning(self, "Import Failed", str(exc))
            return
        self._after_tags_changed()
        self._rebuild_collections()
        message = (
            f"Tagged {result['files']} file{'s' if result['files'] != 1 else ''}; "
            f"created {result['tags_created']} new tag{'s' if result['tags_created'] != 1 else ''}."
        )
        if result["skipped_tags"]:
            message += f"\n\n{result['skipped_tags']} tag(s) were skipped: a file type tag now has that name."
        QMessageBox.information(self, "Tags Imported", message)

    def _on_backup_now(self) -> None:
        try:
            backup = self.repo.backup_database("manual")
        except Exception as exc:  # pylint: disable=broad-except
            QMessageBox.warning(self, "Backup Failed", str(exc))
            return
        self.status_label.setText(f"Library backed up to {backup}")

    def _on_restore_backup(self) -> None:
        folder = self.repo.backup_dir()
        source, _ = QFileDialog.getOpenFileName(self, "Restore Library Backup", str(folder), "Library backup (*.db)")
        if not source:
            return
        answer = QMessageBox.question(
            self, "Restore Backup",
            f"Replace your current tags, favorites and ratings with the backup\n{Path(source).name}?\n\n"
            "A backup of the current state is made first, so this can be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repo.restore_database(source)
        except TagError as exc:
            QMessageBox.warning(self, "Restore Failed", str(exc))
            return
        self._rebuild_collections()
        self.undo_history.clear()  # undo steps refer to the replaced data
        self._rescan_current_folder()  # file ids may differ in the backup: resync everything
        self.status_label.setText("Backup restored. Rescanning the library...")

    def _on_open_backups(self) -> None:
        folder = self.repo.backup_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _build_sidebar(self) -> QTreeWidget:
        """Build the category navigation sidebar; rows come from the Categories settings."""
        tree = QTreeWidget()
        tree.setObjectName("sidebarTree")
        tree.setHeaderHidden(True)
        tree.setIconSize(QSize(16, 16))
        tree.setIndentation(16)
        tree.itemClicked.connect(self._on_sidebar_item_clicked)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._show_sidebar_menu)
        self.sidebar_tree = tree
        self._rebuild_sidebar()
        return tree

    def _rebuild_collections(self) -> None:
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

    def _on_save_search(self) -> None:
        query = self.search_bar.query_text()
        if not query:
            return
        name, ok = QInputDialog.getText(self, "Save Collection", f"Name for this search:\n{query}")
        while ok:
            try:
                self.repo.save_search(name, query)
            except TagError as exc:
                QMessageBox.warning(self, "Can't Save Collection", str(exc))
                name, ok = QInputDialog.getText(self, "Save Collection", f"Name for this search:\n{query}", text=name)
                continue
            self._rebuild_collections()
            self.status_label.setText(f'Saved the collection "{" ".join(name.split())}".')
            return

    def _show_sidebar_menu(self, pos) -> None:
        item = self.sidebar_tree.itemAt(pos)
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not data or data[0] != "collection":
            return
        saved = data[1]
        menu = QMenu(self)
        action_rename = menu.addAction("Rename...")
        action_delete = menu.addAction("Delete")
        chosen = menu.exec(self.sidebar_tree.viewport().mapToGlobal(pos))
        if chosen is action_rename:
            name, ok = QInputDialog.getText(self, "Rename Collection", "Name:", text=saved.name)
            if ok:
                try:
                    self.repo.rename_saved_search(saved.id, name)
                except TagError as exc:
                    QMessageBox.warning(self, "Can't Rename Collection", str(exc))
                self._rebuild_collections()
        elif chosen is action_delete:
            self.repo.delete_saved_search(saved.id)
            self._rebuild_collections()

    def _rebuild_sidebar(self) -> None:
        """(Re)create sidebar rows from the saved categories and reset the filter to All Assets."""
        tree = self.sidebar_tree
        tree.clear()
        self._sidebar_rows.clear()

        self.item_all = QTreeWidgetItem(tree, ["All Assets (0)"])
        self.item_all.setIcon(0, color_dot_icon("#93c5fd"))
        self.item_all.setData(0, Qt.ItemDataRole.UserRole, ("all", None))

        self.item_favorites = QTreeWidgetItem(tree, ["Favorites (0)"])
        self.item_favorites.setIcon(0, color_dot_icon("#fbbf24"))
        self.item_favorites.setData(0, Qt.ItemDataRole.UserRole, ("favorites", None))
        self.item_favorites.setToolTip(0, "Files you starred: select files and press F")

        categorized: set[str] = set()
        for main in resolve_category_colors(get_categories()):
            main_exts = frozenset(category_extensions(main))
            categorized |= main_exts
            main_item = QTreeWidgetItem(tree, [main["name"]])
            main_item.setIcon(0, category_icon(main["color"], main.get("icon")))
            main_item.setData(0, Qt.ItemDataRole.UserRole, ("exts", main_exts))
            main_item.setToolTip(0, ", ".join(sorted(main_exts)) or "No file types assigned")
            main_item.setExpanded(True)
            self._sidebar_rows.append((main_item, main["name"], main_exts))

            for sub in main["subcategories"]:
                sub_exts = frozenset(sub["extensions"])
                sub_item = QTreeWidgetItem(main_item, [sub["name"]])
                sub_item.setIcon(0, category_icon(sub["color"], sub.get("icon")))
                sub_item.setData(0, Qt.ItemDataRole.UserRole, ("exts", sub_exts))
                sub_item.setToolTip(0, ", ".join(sorted(sub_exts)) or "No file types assigned")
                self._sidebar_rows.append((sub_item, sub["name"], sub_exts))

        self._categorized_exts = frozenset(categorized)

        # Catch-all for scanned file types that aren't assigned to any category
        self.item_uncategorized = QTreeWidgetItem(tree, ["Uncategorized (0)"])
        self.item_uncategorized.setIcon(0, color_dot_icon("#6b7280"))
        self.item_uncategorized.setData(0, Qt.ItemDataRole.UserRole, ("uncategorized", None))
        self.item_uncategorized.setHidden(True)

        # Saved searches
        self.item_collections = QTreeWidgetItem(tree, ["Collections"])
        self.item_collections.setIcon(0, color_dot_icon("#fbbf24"))
        self.item_collections.setToolTip(0, "Saved searches: search for something, then click the star in the search box")
        self.item_collections.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.item_collections.setExpanded(True)
        self._rebuild_collections()

        tree.setCurrentItem(self.item_all)
        self.filter_mode, self.filter_value = "all", None
        self._update_sidebar_counts()

    def _update_sidebar_counts(self) -> None:
        """Refresh the asset counts shown on each sidebar row."""
        ext_counts: dict[str, int] = {}
        for asset in self.all_assets:
            ext_counts[asset.extension] = ext_counts.get(asset.extension, 0) + 1

        self.item_all.setText(0, f"All Assets ({len(self.all_assets)})")
        self.item_favorites.setText(0, f"Favorites ({sum(1 for a in self.all_assets if self._marks_of(a).favorite)})")
        for item, name, exts in self._sidebar_rows:
            item.setText(0, f"{name} ({sum(ext_counts.get(e, 0) for e in exts)})")

        uncategorized = sum(c for e, c in ext_counts.items() if e not in self._categorized_exts)
        self.item_uncategorized.setText(0, f"Uncategorized ({uncategorized})")
        self.item_uncategorized.setHidden(uncategorized == 0)

    def _build_grid_view(self) -> QWidget:
        """Build thumbnail grid widget and empty placeholder state."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Virtualized: the model only does work (thumbnails, tooltips) for cards on screen
        self.grid_model = AssetListModel(
            lambda asset: self.thumbnail_manager.get_thumbnail(asset, size=self.thumbnail_size),
            self._asset_tooltip,
            self,
        )
        self.list_grid = GridView()  # (cards glide into place when the window is resized)
        self.list_grid.setObjectName("assetGrid")
        self.list_grid.setViewMode(QListView.ViewMode.IconMode)
        self.list_grid.setModel(self.grid_model)
        self._apply_grid_size()
        self.list_grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_grid.setMovement(QListView.Movement.Static)
        self.list_grid.setUniformItemSizes(True)
        self.list_grid.setLayoutMode(QListView.LayoutMode.Batched)  # big libraries lay out without freezing
        self.list_grid.setBatchSize(500)
        self.list_grid.setWordWrap(True)
        self.list_grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Drag files out to Explorer, Blender, Photoshop, Unreal... (copies; the library keeps its files)
        self.list_grid.setDragEnabled(True)
        self.list_grid.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.list_grid.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.list_grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_grid.customContextMenuRequested.connect(self._show_context_menu)
        self.list_grid.doubleClicked.connect(self._on_item_double_clicked)
        self.list_grid.selectionModel().selectionChanged.connect(self._on_item_selection_changed)
        self.list_grid.setItemDelegate(AssetCardDelegate(self._marks_for_index, self.list_grid))
        # Favorite / rating keys: caught before the grid's type-to-find uses the letters and digits
        self.list_grid.installEventFilter(self)

        # Empty state label shown when no assets are present
        self.lbl_empty_state = QLabel(
            "No assets to display.\nClick 'Open Folder' to scan 3D models, textures, or documents."
        )
        self.lbl_empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_empty_state.setObjectName("emptyState")

        self.welcome_panel = self._build_welcome_panel()

        layout.addWidget(self.welcome_panel)
        layout.addWidget(self.lbl_empty_state)
        layout.addWidget(self.list_grid)
        self.list_grid.hide()
        self.welcome_panel.hide()

        return container

    def _build_welcome_panel(self) -> QWidget:
        """First run (no library folders yet): what the app does and how to start."""
        panel = QWidget()
        panel.setObjectName("welcomePanel")
        outer = QVBoxLayout(panel)
        outer.addStretch(1)
        box = QVBoxLayout()
        box.setSpacing(12)
        title = QLabel(f"Welcome to {APP_NAME}")
        title.setObjectName("welcomeTitle")
        text = QLabel(
            "Point it at the folders where you keep 3D models, textures, videos and documents. "
            "They're scanned with all their subfolders into one searchable library. "
            "Nothing is moved or changed."
        )
        text.setObjectName("welcomeText")
        text.setWordWrap(True)
        for label in (title, text):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.addWidget(label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        btn_add = QPushButton("+ Add Library Folder...")
        btn_add.setObjectName("primaryBtn")
        btn_add.setToolTip("Pick a folder; it's scanned now and every time the app starts")
        btn_add.clicked.connect(self._on_welcome_add_folder)
        btn_browse = QPushButton("Just Browse a Folder...")
        btn_browse.setToolTip("Look at one folder without adding it to the library")
        btn_browse.clicked.connect(self._on_choose_folder)
        buttons.addWidget(btn_add)
        buttons.addWidget(btn_browse)
        buttons.addStretch(1)
        box.addLayout(buttons)

        tips = QLabel(
            "Space preview  •  Ctrl+T tag  •  F favorite  •  1-5 rate  •  drag files into other apps  •  "
            "hover over the search box for search tricks  •  all shortcuts: Settings > Shortcuts"
        )
        tips.setObjectName("hintLabel")
        tips.setWordWrap(True)
        tips.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addSpacing(8)
        box.addWidget(tips)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addLayout(box, 3)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)
        return panel

    def _on_welcome_add_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Add a Library Folder", str(Path.home()))
        if chosen:
            set_scan_folders(get_scan_folders() + [Path(chosen)])
            self._load_library()

    def _apply_grid_size(self) -> None:
        """Size grid cells around the thumbnail size (room for the card padding and file name)."""
        size = self.thumbnail_size
        self.list_grid.setIconSize(QSize(size, size))
        self.list_grid.setGridSize(QSize(size + 35, size + 50))

    def _build_status_bar(self) -> None:
        """Build the bottom status bar."""
        status_bar = QStatusBar(self)
        self.setStatusBar(status_bar)

        self.status_label = QLabel("Ready")
        self.status_info = QLabel("0 assets")
        self.status_info.setObjectName("mutedLabel")

        status_bar.addWidget(self.status_label, 1)
        status_bar.addPermanentWidget(self.status_info)

    # ------------------------------------------------------------------
    # Library Folders & Settings
    # ------------------------------------------------------------------
    def _load_library(self) -> None:
        """Switch to library mode and scan every configured library folder."""
        self.browse_folder = None
        self._update_library_controls()
        folders = [f for f in get_scan_folders() if f.is_dir()]
        if folders:
            if not self.all_assets:
                # First load: show last session's library right away while the real scan runs
                snapshot = load_snapshot(folders, get_file_extensions())
                if snapshot is not None:
                    self._show_snapshot(*snapshot)
            self._start_scan(folders, save_snapshot=True)
        else:
            self.all_assets = []
            self._update_sidebar_counts()
            self._refresh_grid_display()
            self.status_label.setText("No library folders set. Add folders in Settings > Folders.")

    def _update_library_controls(self) -> None:
        """Update the path field and library buttons for the current mode."""
        in_library_mode = self.browse_folder is None
        toggle = self.action_library_toggle
        if in_library_mode:
            folders = get_scan_folders()
            self.txt_path.setText("; ".join(str(f) for f in folders))
            self.txt_path.setToolTip("\n".join(str(f) for f in folders))
            self.btn_library.setToolTip(
                "Showing your library: every folder in Settings > Folders. Click to scan it again.\n"
                "Use Open Folder to look at another folder; the arrow can then add it to the library."
            )
            toggle.setText("Add to Library")
            toggle.setToolTip("Open a folder first (Open Folder), then add it here")
            toggle.setEnabled(False)
            set_icon(toggle, "star")
        else:
            self.txt_path.setText(str(self.browse_folder))
            self.txt_path.setToolTip(str(self.browse_folder))
            toggle.setEnabled(True)
            if is_scan_folder(self.browse_folder):
                toggle.setText("Remove from Library")
                toggle.setToolTip("This folder is in the library; stop scanning it on startup")
                set_icon(toggle, "close")
                state = "This folder is in the library."
            else:
                toggle.setText("Add to Library")
                toggle.setToolTip("Add this folder to the library so it's scanned on startup")
                set_icon(toggle, "star")
                state = "This folder isn't in the library."
            self.btn_library.setToolTip(
                f"Back to your library (every folder in Settings > Folders).\n{state} "
                "Click the arrow to add or remove it."
            )
        # Highlighted while the library is what's shown
        self.btn_library.setProperty("active", in_library_mode)
        self.btn_library.style().unpolish(self.btn_library)
        self.btn_library.style().polish(self.btn_library)

    def _on_toggle_library_folder(self) -> None:
        """Add or remove the browsed folder from the library folders."""
        if self.browse_folder is None:
            return
        folders = get_scan_folders()
        if is_scan_folder(self.browse_folder):
            target = self.browse_folder.resolve()
            set_scan_folders([f for f in folders if f.resolve() != target])
            self.status_label.setText(f"Removed '{self.browse_folder}' from the library.")
        else:
            set_scan_folders(folders + [self.browse_folder])
            self.status_label.setText(f"Added '{self.browse_folder}' to the library.")
        self._update_library_controls()

    def _open_settings(self) -> None:
        """Open the settings dialog and apply any saved changes."""
        dialog = SettingsDialog(self, artbook=self.artbook_panel)
        accepted = dialog.exec() == SettingsDialog.DialogCode.Accepted
        if dialog.page_tags.changed:
            self._after_tags_changed()
        if not accepted:
            return

        if dialog.thumbnail_size_changed:
            self.thumbnail_size = get_thumbnail_size()
            self._apply_grid_size()

        if dialog.theme_changed or dialog.thumbnail_size_changed:
            # Thumbnails bake in the card colors and size, so regenerate them
            self.thumbnail_manager.clear_cache()
            self._refresh_grid_display()

        if dialog.categories_changed:
            self._rebuild_sidebar()
            self._refresh_grid_display()

        if dialog.folders_changed or dialog.extensions_changed:
            if self.browse_folder is None:
                self._load_library()
            else:
                self._update_library_controls()
                if dialog.extensions_changed:
                    self._start_scan([self.browse_folder])

    # ------------------------------------------------------------------
    # Folder Scanning & Data Loading
    # ------------------------------------------------------------------
    def _on_choose_folder(self) -> None:
        """Prompt user with directory picker dialog and scan that single folder."""
        start = self.browse_folder or next(iter(get_scan_folders()), Path.home())
        chosen_dir = QFileDialog.getExistingDirectory(
            self, "Select Folder Containing 3D Assets & Textures", str(start)
        )
        if chosen_dir:
            self.browse_folder = Path(chosen_dir)
            self._update_library_controls()
            self._start_scan([self.browse_folder])

    def _rescan_current_folder(self) -> None:
        """Rescan the browsed folder, or all library folders."""
        if self.browse_folder is not None:
            self._start_scan([self.browse_folder])
        else:
            self._load_library()

    def _show_snapshot(self, assets: list[AssetItem], ids: dict[str, int]) -> None:
        self.all_assets, self._asset_ids = assets, ids
        self._update_sidebar_counts()
        self._refresh_grid_display()
        asset_ids = list(ids.values())
        run_in_background(
            self.repo,
            lambda repo: (repo.tags_by_asset(asset_ids), repo.marks_by_asset(asset_ids)),
            self._on_snapshot_details,
            self._ignore_result,
        )

    def _on_snapshot_details(self, result) -> None:
        if self._scan_done_once:
            return  # the real scan already delivered newer data
        self._asset_tags, self._asset_marks = result
        self._update_sidebar_counts()
        self._reload_tag_suggestions()
        self.list_grid.viewport().update()

    def _start_scan(self, folders: list[Path], save_snapshot: bool = False) -> None:
        """Scan directories in a background thread to keep UI snappy."""
        if len(folders) == 1:
            self.status_label.setText(f"Scanning '{folders[0]}'...")
        else:
            self.status_label.setText(f"Scanning {len(folders)} library folders...")
        self.btn_open_folder.setEnabled(False)
        self.btn_refresh.setEnabled(False)

        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.quit()
            self.scan_thread.wait()

        self.scan_thread = QThread(self)
        self.scan_worker = ScanWorker(folders, get_file_extensions(), save_snapshot=save_snapshot)
        self.scan_worker.moveToThread(self.scan_thread)

        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.finished.connect(self._on_scan_finished)
        self.scan_worker.failed.connect(self._on_scan_failed)
        self.scan_worker.finished.connect(self.scan_thread.quit)
        self.scan_worker.failed.connect(self.scan_thread.quit)

        self.scan_thread.start()

    def _on_scan_finished(self, assets: list[AssetItem], tag_info: dict) -> None:
        """Handle scan results, update categories, and populate grid."""
        self.btn_open_folder.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self._scan_done_once = True
        before = {str(a.path): a.modified_timestamp for a in self.all_assets}
        after = {str(a.path): a.modified_timestamp for a in assets}
        same_files = before.keys() == after.keys()
        unchanged = before == after
        for path_str, mtime in after.items():
            if before.get(path_str, mtime) != mtime:
                self.thumbnail_manager.invalidate(path_str)  # edited since it was last drawn
        self.all_assets = assets
        self._asset_ids = tag_info["ids"]
        self._asset_tags = tag_info["tags"]
        self._asset_marks = tag_info["marks"]
        self.watcher.watch(tag_info["dirs"])
        self._update_sidebar_counts()
        self._reload_tag_suggestions()

        models = sum(1 for a in assets if a.is_3d_model)
        textures = sum(1 for a in assets if a.is_texture)
        docs = sum(1 for a in assets if a.is_document)
        videos = sum(1 for a in assets if a.is_video)
        other = len(assets) - models - textures - docs - videos
        summary = f"{models} 3D models, {textures} textures, {docs} documents, {videos} videos"
        if other:
            summary += f", {other} other"
        self.status_label.setText(f"Loaded {len(assets)} assets ({summary})")
        if tag_info["relinked"]:
            moved = tag_info["relinked"]
            self.status_label.setText(
                f"Loaded {len(assets)} assets. Kept the tags of {moved} moved or renamed file{'s' if moved != 1 else ''}."
            )
        if tag_info["error"]:
            self.status_label.setText(f"Loaded {len(assets)} assets; tags unavailable: {tag_info['error']}")
        self.status_info.setText(f"{len(assets)} assets")

        if self.search_bar.has_tag_filters():
            self._run_tag_search()  # asset ids may have changed
        elif unchanged and before:
            # Nothing changed since the snapshot (or the last scan): keep the grid as it is
            self.list_grid.viewport().update()
        else:
            self._refresh_grid_display(keep_view=same_files or bool(before.keys() & after.keys()))
        self._start_meta_indexing()

    def _on_scan_failed(self, error_msg: str) -> None:
        """Handle scanning failure."""
        self.btn_open_folder.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self.status_label.setText(f"Scan failed: {error_msg}")

    # ------------------------------------------------------------------
    # Live updates (LibraryWatcher)
    # ------------------------------------------------------------------
    def _on_folders_changed(self, folders: list[Path]) -> None:
        """Rescan just the folders whose contents changed."""
        if self.scan_thread is not None and self.scan_thread.isRunning():
            return  # the full scan in progress picks the change up
        if self._rescan_busy:
            self._queued_folders.extend(folders)
            return
        self._rescan_busy = True
        watched, extensions = self.watcher.watched_keys(), get_file_extensions()
        run_in_background(
            self.repo,
            lambda repo: rescan_folders(folders, extensions, watched, repo),
            self._on_folders_rescanned,
            self._on_folders_rescan_failed,
        )

    def _on_folders_rescanned(self, result: FolderRescan) -> None:
        self._rescan_busy = False
        for tree in result.removed_trees:
            self.watcher.remove_tree(tree)
        self.watcher.add_directories(result.new_dirs)

        before = {str(a.path): a.modified_timestamp for a in self.all_assets}
        self.all_assets = result.merge_into(self.all_assets)
        after = {str(a.path) for a in self.all_assets}
        for asset in result.assets:
            old_mtime = before.get(str(asset.path))
            if old_mtime is not None and old_mtime != asset.modified_timestamp:
                self.thumbnail_manager.invalidate(str(asset.path))  # edited: redraw its thumbnail
        self._asset_ids.update(result.ids)
        self._asset_tags.update(result.tags)
        self._asset_marks.update(result.marks)

        added, removed = len(after - before.keys()), len(before.keys() - after)
        changed = added or removed or any(
            before.get(str(a.path)) != a.modified_timestamp for a in result.assets
        )
        if changed:
            self._update_sidebar_counts()
            self._reload_tag_suggestions()
            if self.search_bar.has_tag_filters():
                self._run_tag_search()
            else:
                self._refresh_grid_display(keep_view=True)
            parts = [f"{added} added" if added else "", f"{removed} removed" if removed else ""]
            summary = ", ".join(p for p in parts if p) or "files changed"
            self.status_label.setText(f"Library updated: {summary}.")
            self.status_info.setText(f"{len(self.all_assets)} assets")
            self._start_meta_indexing()

        if self._queued_folders:
            folders, self._queued_folders = self._queued_folders, []
            self._on_folders_changed(folders)

    def _on_folders_rescan_failed(self, error: str) -> None:
        self._rescan_busy = False
        self._queued_folders.clear()
        self.status_label.setText(f"Couldn't update the library: {error}. Click Refresh to rescan.")

    # ------------------------------------------------------------------
    # Filtering & Grid Rendering
    # ------------------------------------------------------------------
    def _on_sidebar_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """Filter assets when clicking a sidebar category or group."""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data[0] == "collection":
            # A saved search: show it across the whole library
            self.filter_mode, self.filter_value = "all", None
            self.search_bar.set_query_text(data[1].query)
        elif data:
            self.filter_mode, self.filter_value = data
            self._refresh_grid_display()
        if data:
            self.sidebar.close_if_floating()

    def _on_search_changed(self) -> None:
        """Filter the grid as the search text or tag chips change."""
        self._query = parse_query(self.search_bar.text())
        self.search_filter = self._query.text.casefold()
        if self._query.errors:
            self.status_label.setText(
                f"Didn't understand {', '.join(self._query.errors)}. Hover over the search box for examples."
            )
        elif self._query.uses_meta and self._meta_todo:
            self.status_label.setText(f"Still reading file details ({self._meta_todo} left); results will fill in.")
        if self.search_bar.has_tag_filters():
            self._run_tag_search()
        else:
            self._tag_match_ids = None
            self._refresh_grid_display()

    def _run_tag_search(self) -> None:
        """Query the tag database off the UI thread; only the newest query's result is used."""
        self._tag_search_generation += 1
        generation = self._tag_search_generation
        query, text = self.search_bar.tag_query(), self.search_filter
        run_in_background(
            self.repo,
            lambda repo: repo.find_assets(query.all_of, query.any_of, query.none_of, text=text),
            partial(self._on_tag_search_done, generation),
            lambda error: self.status_label.setText(f"Tag search failed: {error}"),
        )

    def _on_tag_search_done(self, generation: int, asset_ids: set[int]) -> None:
        if generation != self._tag_search_generation or not self.search_bar.has_tag_filters():
            return  # a newer search is running, or the filters were cleared meanwhile
        self._tag_match_ids = asset_ids
        self._refresh_grid_display()

    def _get_filtered_assets(self) -> list[AssetItem]:
        """Compute filtered assets based on active category and search filter."""
        filtered = self.all_assets

        # Apply sidebar category filter
        if self.filter_mode == "exts" and self.filter_value is not None:
            filtered = [a for a in filtered if a.extension in self.filter_value]
        elif self.filter_mode == "uncategorized":
            filtered = [a for a in filtered if a.extension not in self._categorized_exts]
        elif self.filter_mode == "favorites":
            filtered = [a for a in filtered if self._marks_of(a).favorite]

        # Apply tag filter (matched in SQL by _run_tag_search; all tags must match)
        if self._tag_match_ids is not None:
            matches, ids = self._tag_match_ids, self._asset_ids
            filtered = [a for a in filtered if ids.get(str(a.path)) in matches]

        # Apply search filter
        if self.search_filter:
            filtered = [a for a in filtered if self.search_filter in a.name.casefold()]

        # Detail filters (is:fav, rating:>=3, res:>=2048, ...)
        if self._query.filters:
            filtered = [a for a in filtered if self._query.matches(a, self._marks_of(a), self._meta_of(a))]

        return filtered

    def _meta_of(self, asset: AssetItem) -> Optional[AssetMeta]:
        return self._asset_meta.get(self._asset_ids.get(str(asset.path)))

    # ------------------------------------------------------------------
    # File details for searching (core.metadata), read in the background
    # ------------------------------------------------------------------
    def _start_meta_indexing(self) -> None:
        self._meta_generation += 1
        generation, ids = self._meta_generation, list(self._asset_ids.values())

        def load(repo):
            todo = repo.assets_needing_meta(ids)
            todo = [row for row in todo if read_cost(row[2]) < 3]
            todo.sort(key=lambda row: read_cost(row[2]))  # images first: they take milliseconds
            return repo.meta_by_asset(ids), todo

        run_in_background(self.repo, load, partial(self._on_meta_loaded, generation), self._ignore_result, slow=True)

    def _on_meta_loaded(self, generation: int, result) -> None:
        if generation != self._meta_generation:
            return
        self._asset_meta, todo = result
        self._meta_todo = len(todo)
        if self._query.uses_meta:
            self._refresh_grid_display(keep_view=True)
        self._read_meta_batch(generation, todo)

    def _read_meta_batch(self, generation: int, todo: list) -> None:
        if not todo or generation != self._meta_generation:
            self._meta_todo = 0
            return

        def read(repo):
            details, deadline, done = {}, time.monotonic() + 0.5, 0
            for asset_id, path, ext, key in todo:  # stop after ~0.5 s so a new scan can take over quickly
                done += 1
                meta = read_details(path, ext)
                if meta is not None:
                    details[asset_id] = (meta, key)
                if time.monotonic() > deadline:
                    break
            if details:
                repo.store_meta(details)
            return details, todo[done:]

        def finished(result) -> None:
            details, rest = result
            if generation != self._meta_generation:
                return
            self._asset_meta.update({asset_id: meta for asset_id, (meta, _key) in details.items()})
            self._meta_todo = len(rest)
            if self._query.uses_meta and details:
                self._refresh_grid_display(keep_view=True)
            self._read_meta_batch(generation, rest)

        run_in_background(self.repo, read, finished, self._ignore_result, slow=True)

    def _refresh_grid_display(self, keep_view: bool = False) -> None:
        """Show the currently filtered assets in the grid.

        keep_view: keep the selection and scroll position (for live library updates).
        """
        selected = {str(a.path) for a in self._selected_assets()} if keep_view else set()
        current = self.list_grid.currentIndex().data(ASSET_ROLE) if keep_view else None
        scroll = self.list_grid.verticalScrollBar().value() if keep_view else 0
        self._fill_grid()
        if keep_view:
            self._select_paths(selected, current)
            self.list_grid.verticalScrollBar().setValue(scroll)

    def _select_paths(self, paths: set[str], current: Optional[AssetItem] = None) -> None:
        selection = QItemSelection()
        for path in paths:
            row = self.grid_model.row_of(path)
            if row is not None:
                index = self.grid_model.index(row)
                selection.select(index, index)
        model = self.list_grid.selectionModel()
        model.select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        row = self.grid_model.row_of(str(current.path)) if current is not None else None
        if row is not None:
            model.setCurrentIndex(self.grid_model.index(row), QItemSelectionModel.SelectionFlag.NoUpdate)

    def _fill_grid(self) -> None:
        filtered = self._get_filtered_assets()
        self.grid_model.set_assets(filtered)

        first_run = not self.all_assets and self.browse_folder is None and not get_scan_folders()
        self.welcome_panel.setVisible(first_run)
        if not filtered:
            self.list_grid.hide()
            self.lbl_empty_state.setVisible(not first_run)
            if not self.all_assets:
                self.lbl_empty_state.setText(
                    "No assets found.\nAdd library folders in Settings > Folders, or click 'Open Folder'."
                )
            else:
                self.lbl_empty_state.setText(
                    "No assets match the active filter."
                )
            self.status_info.setText("0 assets shown")
            self.btn_view_asset.setEnabled(False)
            self.btn_tag.setEnabled(False)
            return

        self.lbl_empty_state.hide()
        self.list_grid.show()
        self.status_info.setText(f"{len(filtered)} of {len(self.all_assets)} assets shown")

    def _marks_of(self, asset: AssetItem) -> Marks:
        return self._asset_marks.get(self._asset_ids.get(str(asset.path)), NO_MARKS)

    def _marks_for_index(self, index) -> Marks:
        asset = index.data(ASSET_ROLE)
        return self._marks_of(asset) if asset is not None else NO_MARKS

    def _asset_tooltip(self, asset: AssetItem) -> str:
        tags = self._asset_tags.get(self._asset_ids.get(str(asset.path)), [])
        tag_line = f"<br>Tags: {', '.join(t.name for t in tags)}" if tags else ""
        marks = self._marks_of(asset)
        if marks.favorite or marks.rating:
            parts = (["★ Favorite"] if marks.favorite else []) + ([f"Rating: {rating_text(marks.rating)}"] if marks.rating else [])  # glyph-ok: tooltip text
            tag_line += "<br>" + " • ".join(parts)
        return (
            f"<b>{asset.name}</b><br>"
            f"Type: {asset.asset_group} ({asset.category_name})<br>"
            f"Size: {asset.formatted_size}<br>"
            f"Modified: {asset.formatted_modified_time}<br>"
            f"Path: {asset.path}"
            f"{tag_line}"
        )

    def _on_async_thumbnail_loaded(self, path_str: str, pixmap: QPixmap) -> None:
        """Smoothly update thumbnail icon when background decoding completes."""
        self.grid_model.thumbnail_changed(path_str)

    # ------------------------------------------------------------------
    # User Interactions & Viewer Trigger
    # ------------------------------------------------------------------
    def _on_item_selection_changed(self, *_args) -> None:
        """Update status bar and preview button state based on selection."""
        selected = self._selected_assets()
        self.btn_view_asset.setEnabled(len(selected) == 1)
        self.btn_tag.setEnabled(bool(selected))

        if not selected:
            self.status_label.setText(f"Showing {self.grid_model.rowCount()} assets")
            return

        if len(selected) == 1:
            asset = selected[0]
            self.status_label.setText(
                f"Selected: {asset.name} ({asset.category_name}, {asset.formatted_size})"
            )
        else:
            self.status_label.setText(f"{len(selected)} assets selected")

    def _on_item_double_clicked(self, index) -> None:
        """Open built-in lightweight viewer on double click."""
        asset: AssetItem = index.data(ASSET_ROLE)
        if asset:
            self._open_viewer(asset)

    def _open_viewer_for_selected(self) -> None:
        """Open built-in lightweight viewer for selected item."""
        selected = self._selected_assets()
        if selected:
            self._open_viewer(selected[0])

    def _viewer_target(self, asset: AssetItem, position: str = "") -> ViewerTarget:
        asset_id = self._asset_ids.get(str(asset.path))
        tags = self._asset_tags.get(asset_id, []) if asset_id is not None else None
        edit_tags = (lambda: self._open_tag_dialog([asset])) if asset_id is not None else None
        return ViewerTarget(asset, tags, edit_tags, position)

    def _open_viewer(self, asset: AssetItem) -> None:
        """Open the interactive AssetViewerDialog modal for the given asset."""
        # Previous / next walk through the grid as it's shown now (filters included)
        assets = self.grid_model.assets()
        row = self.grid_model.row_of(str(asset.path))
        state = {"row": row}

        def navigate(step: int) -> Optional[ViewerTarget]:
            new_row = state["row"] + step
            if not 0 <= new_row < len(assets):
                return None
            state["row"] = new_row
            # Keep the grid selection on the file being viewed, so closing the viewer lands there
            self._select_paths({str(assets[new_row].path)}, assets[new_row])
            self.list_grid.scrollTo(self.grid_model.index(new_row))
            return self._viewer_target(assets[new_row], f"{new_row + 1} of {len(assets)}")

        target = self._viewer_target(asset, f"{row + 1} of {len(assets)}" if row is not None else "")
        dialog = AssetViewerDialog(
            asset, self, tags=target.tags, edit_tags=target.edit_tags,
            navigate=navigate if row is not None and len(assets) > 1 else None, position=target.position,
            artbook=self,
        )
        dialog.exec()
        # Free the viewer (GL context, decoded image, video player) instead of keeping it parented
        dialog.deleteLater()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Handle keyboard shortcuts (e.g. Spacebar to quick-preview)."""
        if shortcuts.matches("main.preview", event):
            selected = self._selected_assets()
            if selected:
                self._open_viewer(selected[0])
                return
        super().keyPressEvent(event)

    _RATING_SHORTCUTS = (
        ("main.rate_0", 0), ("main.rate_1", 1), ("main.rate_2", 2),
        ("main.rate_3", 3), ("main.rate_4", 4), ("main.rate_5", 5),
    )

    SEARCH_WIDTH = (280, 150)  # normal, compact toolbar

    def _on_toolbar_compact(self, compact: bool) -> None:
        self.search_bar.setMinimumWidth(self.SEARCH_WIDTH[compact])

    def eventFilter(self, obj, event) -> bool:
        if obj is self.asset_panel and event.type() == event.Type.Resize:
            self.asset_toolbar.update()
        if obj is self.list_grid and event.type() == event.Type.KeyPress:
            if shortcuts.matches("main.favorite", event):
                self._toggle_favorite()
                return True
            for shortcut_id, rating in self._RATING_SHORTCUTS:
                if shortcuts.matches(shortcut_id, event):
                    self._set_rating(rating)
                    return True
        return super().eventFilter(obj, event)

    def _selected_assets(self) -> list[AssetItem]:
        """Selected assets in grid order."""
        rows = sorted(index.row() for index in self.list_grid.selectionModel().selectedIndexes())
        return [a for a in (self.grid_model.asset_at(r) for r in rows) if a is not None]

    def _selected_asset_ids(self) -> list[int]:
        return [self._asset_ids[str(a.path)] for a in self._selected_assets() if str(a.path) in self._asset_ids]

    def _toggle_favorite(self) -> None:
        ids = self._selected_asset_ids()
        if not ids:
            return
        # Like most apps: if any selected file isn't a favorite yet, star them all; else unstar
        favorite = not all(self._asset_marks.get(i, NO_MARKS).favorite for i in ids)
        files = f"{len(ids)} file{'s' if len(ids) != 1 else ''}"
        self._change_marks(
            {i: Marks(favorite, self._asset_marks.get(i, NO_MARKS).rating) for i in ids},
            f"{'Added' if favorite else 'Removed'} {files} {'to' if favorite else 'from'} Favorites.",
            "favorite" if favorite else "unfavorite",
        )

    def _set_rating(self, rating: int) -> None:
        ids = self._selected_asset_ids()
        if not ids:
            return
        files = f"{len(ids)} file{'s' if len(ids) != 1 else ''}"
        self._change_marks(
            {i: Marks(self._asset_marks.get(i, NO_MARKS).favorite, rating) for i in ids},
            f"Rated {files} {rating_text(rating)}." if rating else f"Cleared the rating of {files}.",
            "rating",
        )

    def _change_marks(self, new: dict[int, Marks], message: str, what: str) -> None:
        old = {i: self._asset_marks.get(i, NO_MARKS) for i in new}
        self._apply_marks(new, message)
        files = f"{len(new)} file{'s' if len(new) != 1 else ''}"
        self.undo_history.push(UndoStep(
            f"{what} on {files}",
            lambda: self._apply_marks(old, f"Undid the {what} change."),
            lambda: self._apply_marks(new, message),
        ))

    def _apply_marks(self, marks: dict[int, Marks], message: str) -> None:
        self.repo.restore_marks(marks)
        for asset_id, m in marks.items():
            if m == NO_MARKS:
                self._asset_marks.pop(asset_id, None)
            else:
                self._asset_marks[asset_id] = m
        self._after_marks_changed(message)

    def _after_marks_changed(self, message: str) -> None:
        run_in_background(self.repo, lambda repo: repo.fill_missing_hashes(), self._ignore_result)  # relink if moved
        self._update_sidebar_counts()
        if self.filter_mode == "favorites" or self._query.uses_marks:
            self._refresh_grid_display(keep_view=True)
        else:
            self.list_grid.viewport().update()
        self.status_label.setText(message)

    def _show_context_menu(self, pos) -> None:
        """Display right-click context menu for selected asset."""
        index = self.list_grid.indexAt(pos)
        asset: Optional[AssetItem] = index.data(ASSET_ROLE) if index.isValid() else None
        if not asset:
            return

        menu = QMenu(self)

        action_view = QAction("Preview in Asset Viewer", self)
        font = action_view.font()
        font.setBold(True)
        action_view.setFont(font)
        action_view.triggered.connect(lambda: self._open_viewer(asset))
        menu.addAction(action_view)

        menu.addSeparator()

        action_open = QAction("Open with External App", self)
        action_open.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(asset.path)))
        )
        menu.addAction(action_open)

        action_reveal = QAction("Show in File Explorer", self)
        action_reveal.triggered.connect(lambda: self._reveal_in_explorer(asset.path))
        menu.addAction(action_reveal)

        menu.addSeparator()

        selected = len(self._selected_assets())
        action_tag = QAction("Tag..." if selected <= 1 else f"Tag {selected} Files...", self)
        action_tag.setShortcut(shortcuts.sequence("main.tag"))
        action_tag.triggered.connect(lambda: self._open_tag_dialog())
        menu.addAction(action_tag)

        ids = self._selected_asset_ids()
        all_favorite = bool(ids) and all(self._asset_marks.get(i, NO_MARKS).favorite for i in ids)
        action_fav = QAction("Remove from Favorites" if all_favorite else "Add to Favorites", self)
        action_fav.setShortcut(shortcuts.sequence("main.favorite"))
        action_fav.triggered.connect(self._toggle_favorite)
        menu.addAction(action_fav)

        rating_menu = menu.addMenu("Rating")
        current = {self._asset_marks.get(i, NO_MARKS).rating for i in ids}
        for shortcut_id, rating in self._RATING_SHORTCUTS[1:] + self._RATING_SHORTCUTS[:1]:
            action = QAction(rating_text(rating) if rating else "No Rating", self)
            action.setShortcut(shortcuts.sequence(shortcut_id))
            action.setCheckable(True)
            action.setChecked(current == {rating})
            action.triggered.connect(lambda _checked=False, r=rating: self._set_rating(r))
            rating_menu.addAction(action)

        menu.addSeparator()
        self._add_send_to_artbook_menu(menu)

        action_copy_path = QAction("Copy Full Path", self)
        action_copy_path.triggered.connect(
            lambda: QGuiApplication.clipboard().setText(str(asset.path))
        )
        menu.addAction(action_copy_path)

        menu.exec(self.list_grid.mapToGlobal(pos))

    def _add_send_to_artbook_menu(self, menu: QMenu) -> None:
        """Send to Artbook: a new page, or any page (in its folders). Image files only."""
        paths = [a.path for a in self._selected_assets() if a.path.suffix.lower() in artbook_store.IMAGE_EXTENSIONS]
        send = menu.addMenu("Send to Artbook" if paths else "Send to Artbook (images only)")
        send.setEnabled(bool(paths))
        if paths:
            self.fill_artbook_menu(send, lambda page_id: self._send_to_artbook(paths, page_id))

    def fill_artbook_menu(self, send: QMenu, on_pick: Callable[[Optional[str]], None]) -> None:
        """Fill a "Send to Artbook" menu: New Page, then every page (in its folders). on_pick(page id,
        or None for a new page). Also used by the PDF viewer (ui.pdf_viewer) for its pages."""
        action_new = send.addAction("New Page")
        set_icon(action_new, "new_page")
        action_new.triggered.connect(lambda: on_pick(None))
        send.addSeparator()
        names = {p["id"]: p.get("name") or "Untitled" for p in artbook_store.list_pages()}
        current = self.artbook_panel.current_page_id() or get_artbook_page()

        def add(target: QMenu, folder: dict) -> None:
            for sub in folder["folders"]:
                submenu = target.addMenu(sub["name"])
                set_icon(submenu.menuAction(), "folder")
                add(submenu, sub)
                submenu.setEnabled(not submenu.isEmpty())
            for page_id in folder["pages"]:
                action = target.addAction(names.get(page_id, "Untitled"))
                set_icon(action, "page")
                if page_id == current:
                    font = action.font()
                    font.setBold(True)  # the open page
                    action.setFont(font)
                action.triggered.connect(lambda _checked=False, pid=page_id: on_pick(pid))

        add(send, artbook_store.page_tree())

    def _open_artbook_page(self, page_id: Optional[str]) -> str:
        """Open an Artbook page to send things to (None: a new one): "" or what went wrong."""
        panel = self.artbook_panel
        try:
            if page_id is None:
                page_id = panel.new_page()
            else:
                panel.open_page(page_id)
        except (artbook_store.PageError, OSError) as exc:
            return f"Couldn't open the Artbook page: {exc}"
        if panel.current_page_id() != page_id:
            return "That Artbook page couldn't be opened."
        return ""

    def _sent_message(self, count: int) -> str:
        panel = self.artbook_panel
        name = panel.page.get("name") or "Untitled"
        hint = " Ctrl+2 shows the Artbook." if not panel.isVisible() else ""
        return f'Sent {count} image{"s" if count != 1 else ""} to "{name}".{hint}'

    def _send_to_artbook(self, paths: list[Path], page_id: Optional[str]) -> None:
        """Add image files to an Artbook page (None: a new one), which becomes the open page."""
        error = self._open_artbook_page(page_id)
        if error:
            self.status_label.setText(error)
            return
        count = self.artbook_panel.board.add_files(paths)
        self.status_label.setText(self._sent_message(count))

    def send_image_to_artbook(self, image: QImage, name: str, page_id: Optional[str]) -> str:
        """Add a picture (a PDF page) to an Artbook page (None: a new one); says how it went."""
        error = self._open_artbook_page(page_id)
        if error:
            return error
        self.artbook_panel.board.add_image(image, name=name)
        message = self._sent_message(1)
        self.status_label.setText(message)
        return message

    # ------------------------------------------------------------------
    # Tagging
    # ------------------------------------------------------------------
    def _open_tag_dialog(self, assets: Optional[list[AssetItem]] = None) -> Optional[list[Tag]]:
        """Tag the given assets (default: the grid selection).

        Returns the first asset's updated tags if anything changed (used by the viewer), else None.
        """
        if assets is None:
            assets = self._selected_assets()
        ids = [self._asset_ids[str(a.path)] for a in assets if str(a.path) in self._asset_ids]
        if not ids:
            QMessageBox.information(self, "Tags Unavailable", "These files aren't in the tag database yet. Try Refresh.")
            return None
        dialog = TagAssetsDialog(self.repo, ids, self)
        dialog.exec()
        dialog.deleteLater()
        if not dialog.changed:
            return None
        if dialog.last_change:
            self._push_tag_undo(dialog.last_change)
        self._after_tags_changed(ids)
        return self._asset_tags.get(ids[0], [])

    def _after_tags_changed(self, asset_ids: Optional[list[int]] = None) -> None:
        """Refresh cached tags, tooltips, suggestions and tag search after tags were edited.

        asset_ids: the files that changed, or None after tags were renamed / recolored / deleted
        (which can affect every file; reloaded in the background).
        """
        self._reload_tag_suggestions()
        # Fingerprint newly tagged files right away, so their tags survive a move before the next scan
        run_in_background(self.repo, lambda repo: repo.fill_missing_hashes(), self._ignore_result)
        if asset_ids is not None:
            self._apply_tag_cache(self.repo.tags_by_asset(asset_ids))
        else:
            run_in_background(self.repo, lambda repo: repo.tags_by_asset(list(self._asset_ids.values())),
                              self._apply_tag_cache)

    @staticmethod
    def _ignore_result(_result) -> None:
        pass

    def _apply_tag_cache(self, tags_by_asset: dict[int, list[Tag]]) -> None:
        self._asset_tags.update(tags_by_asset)  # tooltips are built on hover, so they pick this up
        if self.search_bar.has_tag_filters():
            self._run_tag_search()

    def _reload_tag_suggestions(self) -> None:
        try:
            self.search_bar.set_available_tags(self.repo.list_tags())
        except Exception as exc:  # pylint: disable=broad-except
            self.status_label.setText(f"Tags unavailable: {exc}")

    @staticmethod
    def _reveal_in_explorer(path: Path) -> None:
        """Reveal file in Windows File Explorer."""
        try:
            if sys.platform == "win32":
                norm_path = os.path.normpath(str(path.resolve()))
                subprocess.Popen(f'explorer /select,"{norm_path}"', shell=True)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
        except Exception as exc:  # pylint: disable=broad-except
            print(f"Error revealing in file explorer: {exc}")

    def show_crash_notice(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Asset Manager Closed Unexpectedly")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("Asset Manager crashed or stopped responding the last time it ran.")
        box.setInformativeText(
            "Details were saved to the crash log. If it keeps happening, please include the log "
            "when reporting the problem."
        )
        btn_open = box.addButton("Open Crash Log", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is btn_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(crash_log.crash_log_path())))

    def closeEvent(self, event) -> None:
        """Ensure background scan and thumbnail threads terminate cleanly on exit."""
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.quit()
            self.scan_thread.wait(1000)
        # Stop background work before Qt is torn down: a job still running then crashes the process
        self._meta_generation += 1  # ends the file-details reading chain
        self.view_modes.shutdown()  # the separate Artbook window closes with the app
        self.watcher.clear()
        self.thumbnail_manager.shutdown()
        wait_for_background()
        self.artbook_panel.save_now()  # after background jobs, so an older autosave can't land on top
        super().closeEvent(event)


# ----------------------------------------------------------------------
# Application Entry Point
# ----------------------------------------------------------------------
def run_self_test(log_path: Optional[str]) -> int:
    """Exercise the bundled libraries without showing a window (used by the release build).

    Returns a process exit code; writes a short report to log_path if given, since the
    windowed executable has no console.
    """
    import tempfile
    import traceback

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Qt painting/fonts (used by thumbnails) need an application object first
    app = QApplication.instance() or QApplication(sys.argv[:1])
    lines: list[str] = []
    ok = True

    def step(name: str, fn) -> None:
        nonlocal ok
        try:
            detail = fn()
            lines.append(f"PASS  {name}" + (f": {detail}" if detail else ""))
        except Exception:  # pylint: disable=broad-except
            ok = False
            lines.append(f"FAIL  {name}\n{traceback.format_exc()}")

    def check_imports():
        import fbxloader, OpenGL.GL, scipy.spatial, trimesh  # noqa: E401,F401
        return f"trimesh {trimesh.__version__}"

    def check_models():
        import trimesh
        from core.model_loader import load_3d_model
        with tempfile.TemporaryDirectory() as tmp:
            results = []
            for ext in (".obj", ".glb", ".stl"):
                path = Path(tmp) / f"box{ext}"
                trimesh.creation.box().export(path)
                mesh = load_3d_model(path)
                if mesh.face_count != 12:
                    raise RuntimeError(f"{ext}: expected 12 faces, got {mesh.face_count}")
                results.append(ext)
            return ", ".join(results)

    def check_images():
        from PIL import Image
        from core.thumbnail_manager import load_texture_thumbnail_image
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tex.png"
            Image.new("RGB", (64, 64), (200, 50, 50)).save(path)
            if load_texture_thumbnail_image(path, "PNG", 110) is None:
                raise RuntimeError("thumbnail generation failed")

    def check_ui():
        apply_theme(get_theme(), app)
        # The Settings dialog's Tags page opens the library database: use a throwaway one,
        # so a self-test never upgrades or changes the user's real library
        import core.tag_repository as tag_repository
        with tempfile.TemporaryDirectory() as tmp:
            saved = tag_repository._repository
            tag_repository._repository = tag_repository.TagRepository(Path(tmp) / "self-test.db")
            try:
                dialog = SettingsDialog()
                dialog.close()
                dialog.deleteLater()
            finally:
                tag_repository._repository.close_thread_connection()
                tag_repository._repository = saved
        from PySide6.QtPdf import QPdfDocument  # noqa: F401
        if app_icon().isNull():
            raise RuntimeError("application icon missing from the build")
        return "settings dialog built"

    def check_tags():
        from core.tag_repository import TagRepository
        from core.scanner import AssetScanner
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.png").write_bytes(b"x")
            repo = TagRepository(Path(tmp) / "test.db")
            ids = repo.sync_assets(AssetScanner().scan(Path(tmp)))
            tag = repo.create_custom_tag("SelfTest")
            repo.add_tags(list(ids.values()), [tag.id])
            png = repo.find_tag("PNG")
            if repo.find_assets_by_tags([tag.id, png.id]) != set(ids.values()):
                raise RuntimeError("tag search returned the wrong files")
            version = repo.schema_version()
            repo.close_thread_connection()  # the temp folder can't be deleted while the DB is open
            return f"schema v{version}"

    def check_video():
        from PySide6.QtMultimedia import QMediaFormat
        from ui.video_player import VideoPlayer
        formats = QMediaFormat().supportedFileFormats(QMediaFormat.ConversionMode.Decode)
        if not formats:
            raise RuntimeError("no multimedia backend: Qt can't decode any video format")
        player = VideoPlayer()
        player.stop()
        player.close()
        return f"{len(formats)} decodable container formats"

    step("imports", check_imports)
    step("3D model loading", check_models)
    step("image thumbnails", check_images)
    step("user interface", check_ui)
    step("video playback", check_video)
    step("tag database", check_tags)

    lines.append("RESULT: " + ("PASS" if ok else "FAIL"))
    report = "\n".join(lines)
    if log_path:
        Path(log_path).write_text(report, encoding="utf-8")
    elif sys.stdout is not None:
        print(report)
    return 0 if ok else 1


def main() -> None:
    """Initialize and run the PySide6 application."""
    if "--self-test" in sys.argv:
        index = sys.argv.index("--self-test")
        log_path = sys.argv[index + 1] if index + 1 < len(sys.argv) else None
        sys.exit(run_self_test(log_path))

    from PySide6.QtCore import QtMsgType, QTimer, qInstallMessageHandler

    # Suppress cosmetic Qt libtiff warnings and allocation limit messages
    def _qt_message_handler(mode, context, message):
        if "ImageDescription" in message or "allocation limit" in message:
            return  # Filter known harmless libtiff tag / memory limit logs
        if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            crash_log.log_error(f"Qt: {message}")
        if sys.stderr is not None:  # None in the windowed (--noconsole) executable
            sys.stderr.write(f"{message}\n")

    from PySide6.QtGui import QImageReader, QSurfaceFormat

    # Configure hardware OpenGL format with 24-bit depth and 4x MSAA antialiasing
    gl_format = QSurfaceFormat()
    gl_format.setDepthBufferSize(24)
    gl_format.setSamples(4)
    gl_format.setVersion(3, 3)
    gl_format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(gl_format)

    cleanup_after_update()
    previous_session_crashed = crash_log.start_session(APP_NAME, APP_VERSION)
    qInstallMessageHandler(_qt_message_handler)
    QImageReader.setAllocationLimit(2048)

    # Qt multimedia picks its backend when QApplication starts
    apply_media_backend()

    if sys.platform == "win32":
        # Own taskbar identity, so Windows shows our icon instead of python.exe's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AssetManager.App")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())
    set_motion_mode(get_motion())
    apply_theme(get_theme(), app, get_background_texture())
    set_frost(get_glass_frost())
    install_popup_animations(app)
    install_view_effects(app)
    main_thread_gc.install(app)  # a collection on a worker thread can free UI objects there, and crash

    # Hang watchdog: if the UI thread stops ticking, crash_log dumps every thread's stack
    heartbeat = QTimer(app)
    heartbeat.timeout.connect(crash_log.heartbeat)
    heartbeat.start(2000)
    crash_log.heartbeat()

    # Keep the thumbnail disk cache under its size limit (off the UI thread; it's only file deletes)
    threading.Thread(target=prune_disk_cache, name="prune-thumbnails", daemon=True).start()

    window = MainWindow()
    window.show()
    if previous_session_crashed:
        QTimer.singleShot(400, window.show_crash_notice)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
