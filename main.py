"""Asset Manager - Windows desktop application using PySide6.

The main window puts the parts together and runs what the user asks for. The parts:
- ui.library_state     the files and their tags / favorites / details: scans, live updates, the snapshot
- core.asset_filter    which files the grid shows (sidebar row, tag matches, search words and filters)
- ui.library_sidebar   the sidebar's rows: categories, favorites, collections
- ui.asset_grid        the virtual grid, its card painting
- ui.asset_menu        the grid's right-click menu, and sending pictures to Artbook pages
- ui.library_tools     the Tools menu: duplicates, tag export / import, backups
- ui.tag_widgets       the search bar with tag chips, the tagging dialog
- ui.viewer            the asset viewer (3D, images, PDFs, video); ui.artbook the Artbook
- self_test            the release build's --self-test
"""

from __future__ import annotations

import html
import sys
import threading
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QItemSelection,
    QItemSelectionModel,
    QSize,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QKeyEvent,
    QPixmap,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core import archives, crash_log
from core.asset_filter import filter_assets
from core.library_snapshot import load_snapshot
from core.scanner import AssetItem
from core import artbook_store
from core.settings import (
    apply_new_defaults,
    get_archive_contents,
    get_pages_dir,
    get_background_texture,
    get_file_extensions,
    get_glass_frost,
    get_motion,
    get_scan_folders,
    get_seven_zip_path,
    get_theme,
    get_model_view,
    get_thumbnail_size,
    is_scan_folder,
    set_archive_contents,
    set_scan_folders,
)
from core.search_query import ParsedQuery, parse_query
from core.tag_repository import NO_MARKS, Marks, SavedSearch, Tag, TagChange, get_repository, is_damage_error
from core.thumbnail_manager import ThumbnailManager, prune_disk_cache
from core.updater import cleanup_after_update
from core.video_tools import apply_media_backend
from core.version import APP_NAME, APP_VERSION
from ui.animations import install_popup_animations, set_motion_mode
from ui.archive_jobs import unpack_in_background
from ui.artbook import ArtbookPanel
from ui.asset_grid import ASSET_ROLE, AssetCardDelegate, AssetListModel, GridView, rating_text
from ui.asset_menu import RATING_SHORTCUTS, ArtbookSender, AssetMenuActions, build_asset_menu
from ui.duotone import set_icon
from ui import file_actions
from ui.glass import set_frost
from ui.icons import app_icon
from ui.library_sidebar import LibrarySidebar
from ui.library_tools import LibraryTools
from ui.library_state import LibraryState, LiveUpdate, SavedView, ScanReport
from ui import main_thread_gc, scroll_bar_watch
from ui import shortcuts
from ui.settings_dialog import SettingsDialog
from ui.sidebar import SidebarHost
from ui.tag_widgets import TagAssetsDialog, TagSearchBar, run_in_background, wait_for_background
from ui.themes import apply_theme, theme_signals
from ui import title_bar
from ui.toolbar import CompactToolbar, PathField
from ui.undo import UndoHistory, UndoStep
from ui.view_modes import ViewModes, ViewSwitchButton
from ui.viewer import AssetViewerDialog, ViewerTarget
from ui.views import install_view_effects
from self_test import run_self_test

@dataclass(frozen=True)
class ArchiveReturn:
    """Where to go back to when leaving an opened archive (Back / Backspace)."""

    archive: Path
    browse_folder: Optional[Path]  # None: the library
    view: SavedView  # what the grid showed: back without waiting for a scan
    search: str


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

        # None = library mode (all scan folders); otherwise a single folder opened via "Open Folder",
        # or an archive opened like a folder (archive_return says where it was opened from)
        self.browse_folder: Optional[Path] = None
        self.archive_return: Optional[ArchiveReturn] = None
        self._opening_archive = False  # its files are being read (the grid says so meanwhile)

        # Sidebar filter: ("all", None), ("exts", frozenset of extensions), ("uncategorized", None)
        self.filter_mode: str = "all"
        self.filter_value: Optional[frozenset[str]] = None
        # The files and what's known about them (tags, favorites, details), kept up to date in the
        # background: scans, live updates, the snapshot (ui.library_state)
        self.repo = get_repository()
        self.library = LibraryState(self.repo, self)
        self.library.scan_finished.connect(self._on_scan_finished)
        self.library.scan_failed.connect(self._on_scan_failed)
        self.library.snapshot_details.connect(self._on_snapshot_details)
        self.library.live_update.connect(self._on_live_update)
        self.library.live_update_failed.connect(self._on_live_update_failed)
        self.library.details_loaded.connect(self._on_details_loaded)
        self.library.files_edited.connect(self._on_files_edited)
        # The search: name words and detail filters, and the tag search result (None: no tag filter)
        self._query = ParsedQuery()
        self._tag_match_ids: Optional[set[int]] = None
        self._tag_search_generation = 0
        self.thumbnail_size: int = get_thumbnail_size()

        # Asynchronous thumbnail loader & in-memory cache
        self.thumbnail_manager = ThumbnailManager(self)
        self.thumbnail_manager.render_models = get_model_view()["thumbnails"]
        self.thumbnail_manager.alpha_relief = get_model_view()["alpha_relief"]
        self.thumbnail_manager.alpha_of = self._known_alpha
        self.thumbnail_manager.thumbnail_loaded.connect(self._on_async_thumbnail_loaded)
        self.thumbnail_manager.thumbnail_skipped.connect(self._on_thumbnail_skipped)
        theme_signals().changed.connect(self._on_theme_changed)  # (also while previewing in Settings)

        self.undo_history = UndoHistory(on_change=lambda: self._update_undo_actions())
        # Tools menu: duplicates, tag export / import, backups (ui.library_tools)
        self.library_tools = LibraryTools(self.repo, lambda: self.library.assets, self)
        self._daily_backup_started = False  # (the automatic backup, once per session: after the first good scan)
        self.library_tools.status.connect(self._show_status)
        self.library_tools.tags_imported.connect(self._on_tags_imported)
        self.library_tools.restored.connect(self._on_backup_restored)
        self.library_tools.duplicates_removed.connect(self._on_duplicates_removed)

        self._init_ui()
        self._load_library()
        # The first OpenGL widget in a window makes Qt remake the window for OpenGL, and the 3D
        # viewer's made it remake this one too: it vanished and came back the first time a model
        # was opened. A hidden one from the start makes it that way once, before it's shown.
        self._opengl_ready = QOpenGLWidget(self)
        self._opengl_ready.hide()

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
        # The sidebar's rows: categories, favorites, collections (ui.library_sidebar)
        self.sidebar_tree = sidebar_tree = LibrarySidebar(self.repo)
        sidebar_tree.row_chosen.connect(self._on_sidebar_row)
        sidebar_tree.collection_chosen.connect(self._on_collection_chosen)
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
        # Sends images from the grid, and PDF pages from the viewer, to Artbook pages
        self.artbook_sender = ArtbookSender(self.artbook_panel, self._show_status)
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
                (self.btn_open_folder, "Open Folder"), (self.btn_library, "Library"), (self.btn_back, "Back"),
                (self.btn_refresh, "Refresh"),
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
        library_menu.addSeparator()
        self.action_archive_contents = library_menu.addAction("Include Files Inside Archives")
        self.action_archive_contents.setCheckable(True)
        self.action_archive_contents.setChecked(get_archive_contents())
        self.action_archive_contents.setToolTip(
            "Show the files inside archives (ZIP, RAR, 7z) among your other files, marked with a small box.\n"
            "Off: they're only shown when you open an archive (double-click it)."
        )
        set_icon(self.action_archive_contents, "archive")
        self.action_archive_contents.toggled.connect(self._on_archive_contents_toggled)
        self.btn_library.setMenu(library_menu)
        layout.addWidget(self.btn_library)

        # "Back": leaves an opened archive (shown only then)
        self.btn_back = QPushButton("Back")
        set_icon(self.btn_back, "back")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.setToolTip("Leave this archive: back to where you were (Backspace)")
        self.btn_back.clicked.connect(self._leave_archive)
        self.btn_back.hide()
        layout.addWidget(self.btn_back)

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

        # Path display field (a long path is shortened in the middle; the tooltip has all of it)
        self.txt_path = PathField()
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
        self.library_tools.add_to(menu)
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

    def _show_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _on_tags_imported(self) -> None:
        self._after_tags_changed()  # (tags, and the collections)
        self._reload_marks()  # (the export's favorites and ratings)

    def _on_backup_restored(self) -> None:
        self.sidebar_tree.reload_collections()
        self.undo_history.clear()  # undo steps refer to the replaced data
        self._rescan_current_folder()  # file ids may differ in the backup: resync everything

    def _on_duplicates_removed(self) -> None:
        # The library watcher drops them from the grid; rescan if folders aren't all watched
        if not self.library.watcher.is_complete():
            self._rescan_current_folder()

    def _on_save_search(self) -> None:
        query = self.search_bar.query_text()
        if not query:
            return
        name = self.sidebar_tree.save_collection(query)
        if name:
            self.status_label.setText(f'Saved the collection "{name}".')

    def _rebuild_sidebar(self) -> None:
        """Recreate the sidebar rows (the categories changed) and show All Assets."""
        self.sidebar_tree.rebuild()
        self.filter_mode, self.filter_value = "all", None
        self._update_sidebar_counts()

    def _update_sidebar_counts(self) -> None:
        self.sidebar_tree.update_counts(self.library.assets, lambda asset: self._marks_of(asset).favorite)

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
            file_for=self._drag_file,
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
        self.archive_return = None
        self._update_library_controls()
        folders = [f for f in get_scan_folders() if f.is_dir()]
        if folders:
            if not self.library.assets:
                # First load: show last session's library right away while the real scan runs
                snapshot = load_snapshot(folders, get_file_extensions(), get_archive_contents())
                if snapshot is not None:
                    self._show_snapshot(*snapshot)
            self._start_scan(folders, save_snapshot=True)
        else:
            self.library.clear()
            self._update_sidebar_counts()
            self._refresh_grid_display()
            self.status_label.setText("No library folders set. Add folders in Settings > Folders.")

    def _update_library_controls(self) -> None:
        """Update the path field and library buttons for the current mode."""
        in_library_mode = self.browse_folder is None
        toggle = self.action_library_toggle
        if self.btn_back.isVisibleTo(self) != (self.archive_return is not None):
            self.btn_back.setVisible(self.archive_return is not None)
            self.asset_toolbar.update()  # (the toolbar's contents changed)
        if self.archive_return is not None:
            archive = self.archive_return.archive
            self.txt_path.set_path_text(str(archive))
            self.txt_path.setToolTip(f"Inside {archive}\nBack or Backspace returns to where you were.")
            toggle.setText("Add to Library")
            toggle.setToolTip("An archive can't be a library folder; its files are part of the library's folder")
            toggle.setEnabled(False)
            set_icon(toggle, "star")
            self.btn_library.setToolTip("Back to your library (every folder in Settings > Folders)")
        elif in_library_mode:
            folders = get_scan_folders()
            self.txt_path.set_path_text("; ".join(str(f) for f in folders))
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
            self.txt_path.set_path_text(str(self.browse_folder))
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
        # Free it (a child of the window): each one kept would be restyled on every theme change
        dialog.deleteLater()  # (it can't close while moving pages or updating, so nothing is left running)
        if dialog.page_tags.changed:
            self._after_tags_changed()
        if not accepted:
            return

        if dialog.thumbnail_size_changed:
            self.thumbnail_size = get_thumbnail_size()
            self._apply_grid_size()

        if dialog.model_thumbnails_changed:
            self.thumbnail_manager.render_models = get_model_view()["thumbnails"]
        if dialog.alpha_relief_changed:
            self.thumbnail_manager.alpha_relief = get_model_view()["alpha_relief"]

        if dialog.thumbnail_size_changed or dialog.model_thumbnails_changed or dialog.alpha_relief_changed:
            # Thumbnails bake in the size, so regenerate them (a new theme already did: _on_theme_changed)
            self.thumbnail_manager.clear_cache()
            self._refresh_grid_display()

        if dialog.categories_changed:
            self._rebuild_sidebar()
            self._refresh_grid_display()

        if dialog.archives_changed:
            self.action_archive_contents.blockSignals(True)  # (saved already: only the tick follows)
            self.action_archive_contents.setChecked(get_archive_contents())
            self.action_archive_contents.blockSignals(False)
            self.thumbnail_manager.clear_cache()  # (cards that said "Install 7-Zip")

        if dialog.folders_changed or dialog.extensions_changed or dialog.archives_changed:
            if self.browse_folder is None:
                self._load_library()
            else:
                self._update_library_controls()
                if dialog.extensions_changed or dialog.archives_changed:
                    self._start_scan([self.browse_folder])

    # ------------------------------------------------------------------
    # Folder Scanning & Data Loading
    # ------------------------------------------------------------------
    def _on_choose_folder(self) -> None:
        """Prompt user with directory picker dialog and scan that single folder."""
        start = (self.browse_folder.parent if self.archive_return else self.browse_folder) or next(iter(get_scan_folders()), Path.home())
        chosen_dir = QFileDialog.getExistingDirectory(
            self, "Select Folder Containing 3D Assets & Textures", str(start)
        )
        if chosen_dir:
            self.browse_folder = Path(chosen_dir)
            self.archive_return = None
            self._update_library_controls()
            self._start_scan([self.browse_folder])

    def _rescan_current_folder(self) -> None:
        """Rescan the browsed folder, or all library folders."""
        if self.browse_folder is not None:
            self._start_scan([self.browse_folder])
        else:
            self._load_library()

    def _show_snapshot(self, assets: list[AssetItem], ids: dict[str, int]) -> None:
        self.library.show_snapshot(assets, ids)
        self._update_sidebar_counts()
        self._refresh_grid_display()

    def _on_snapshot_details(self) -> None:
        self._update_sidebar_counts()
        self._reload_tag_suggestions()
        self.list_grid.viewport().update()

    def _start_scan(self, folders: list[Path], save_snapshot: bool = False) -> None:
        """Scan directories in a background thread to keep UI snappy."""
        if self.archive_return is not None and folders == [self.browse_folder]:
            self.status_label.setText(f"Opening {folders[0].name}...")
        elif len(folders) == 1:
            self.status_label.setText(f"Scanning '{folders[0]}'...")
        else:
            self.status_label.setText(f"Scanning {len(folders)} library folders...")
        self.btn_open_folder.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self.library.start_scan(folders, get_file_extensions(), save_snapshot=save_snapshot)

    def _on_scan_finished(self, report: ScanReport) -> None:
        """Show a finished scan: sidebar counts, the status line, and the grid."""
        self._opening_archive = False
        self.btn_open_folder.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self._update_sidebar_counts()
        self._reload_tag_suggestions()

        assets = report.assets
        models = sum(1 for a in assets if a.is_3d_model)
        textures = sum(1 for a in assets if a.is_texture)
        docs = sum(1 for a in assets if a.is_document)
        videos = sum(1 for a in assets if a.is_video)
        zips = sum(1 for a in assets if a.is_archive)
        other = len(assets) - models - textures - docs - videos - zips
        summary = f"{models} 3D models, {textures} textures, {docs} documents, {videos} videos"
        if zips:
            summary += f", {zips} archives"
        inside = sum(1 for a in assets if a.in_archive)
        if inside and self.archive_return is None:
            summary += f" ({inside} of them inside archives)"
        if other:
            summary += f", {other} other"
        self.status_label.setText(f"Loaded {len(assets)} assets ({summary})")
        if report.relinked:
            moved = report.relinked
            self.status_label.setText(
                f"Loaded {len(assets)} assets. Kept the tags of {moved} moved or renamed file{'s' if moved != 1 else ''}."
            )
        if report.error:
            self.status_label.setText(f"Loaded {len(assets)} assets; tags unavailable: {report.error}")
            if is_damage_error(report.error):
                # (once the window is up: a dialog from inside this signal would block the scan's hand-over)
                QTimer.singleShot(0, lambda error=report.error: self.library_tools.offer_restore(error))
        elif not self._daily_backup_started:
            self._daily_backup_started = True
            self.library_tools.back_up_daily()  # (after the first scan: it doesn't compete with startup)
        self.status_info.setText(f"{len(assets)} assets")

        if self.search_bar.has_tag_filters():
            self._run_tag_search()  # asset ids may have changed
        elif report.unchanged and report.had_files:
            # Nothing changed since the snapshot (or the last scan): keep the grid as it is
            self.list_grid.viewport().update()
        else:
            self._refresh_grid_display(keep_view=report.same_files or report.overlap)

    def _on_scan_failed(self, error_msg: str) -> None:
        """Handle scanning failure."""
        self._opening_archive = False
        self.btn_open_folder.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self.status_label.setText(f"Scan failed: {error_msg}")

    def _on_files_edited(self, paths: list[str]) -> None:
        for path_str in paths:
            self.thumbnail_manager.invalidate(path_str)  # edited since it was last drawn

    def _on_live_update(self, update: LiveUpdate) -> None:
        """Files were added, removed or edited while the app is open."""
        self._update_sidebar_counts()
        self._reload_tag_suggestions()
        if self.search_bar.has_tag_filters():
            self._run_tag_search()
        else:
            self._refresh_grid_display(keep_view=True)
        parts = [f"{update.added} added" if update.added else "", f"{update.removed} removed" if update.removed else ""]
        summary = ", ".join(p for p in parts if p) or "files changed"
        self.status_label.setText(f"Library updated: {summary}.")
        self.status_info.setText(f"{len(self.library.assets)} assets")

    def _on_live_update_failed(self, error: str) -> None:
        self.status_label.setText(f"Couldn't update the library: {error}. Click Refresh to rescan.")

    # ------------------------------------------------------------------
    # Filtering & Grid Rendering
    # ------------------------------------------------------------------
    def _on_sidebar_row(self, mode: str, extensions: Optional[frozenset[str]]) -> None:
        """A sidebar category or group was clicked: show its files."""
        self.filter_mode, self.filter_value = mode, extensions
        self._refresh_grid_display()
        self.sidebar.close_if_floating()

    def _on_collection_chosen(self, saved: SavedSearch) -> None:
        """A saved search: show it across the whole library."""
        self.filter_mode, self.filter_value = "all", None
        self.search_bar.set_query_text(saved.query)
        self.sidebar.close_if_floating()

    def _on_search_changed(self) -> None:
        """Filter the grid as the search text or tag chips change."""
        self._query = parse_query(self.search_bar.text())
        if self._query.errors:
            self.status_label.setText(
                f"Didn't understand {', '.join(self._query.errors)}. Hover over the search box for examples."
            )
        elif self._query.uses_meta and self.library.meta_todo:
            self.status_label.setText(f"Still reading file details ({self.library.meta_todo} left); results will fill in.")
        if self.search_bar.has_tag_filters():
            self._run_tag_search()
        else:
            self._tag_match_ids = None
            self._refresh_grid_display()

    def _run_tag_search(self) -> None:
        """Query the tag database off the UI thread; only the newest query's result is used."""
        self._tag_search_generation += 1
        generation = self._tag_search_generation
        query = self.search_bar.tag_query()  # (file names are matched with the other filters)
        run_in_background(
            self.repo,
            lambda repo: repo.find_assets(query.all_of, query.any_of, query.none_of),
            partial(self._on_tag_search_done, generation),
            lambda error: self.status_label.setText(f"Tag search failed: {error}"),
        )

    def _on_tag_search_done(self, generation: int, asset_ids: set[int]) -> None:
        if generation != self._tag_search_generation or not self.search_bar.has_tag_filters():
            return  # a newer search is running, or the filters were cleared meanwhile
        self._tag_match_ids = asset_ids
        self._refresh_grid_display()

    def _get_filtered_assets(self) -> list[AssetItem]:
        """The files to show: the sidebar row, tag matches (found in SQL by _run_tag_search), name words
        and detail filters (core.asset_filter)."""
        return filter_assets(
            self.library.assets, mode=self.filter_mode, extensions=self.filter_value, categorized=self.sidebar_tree.categorized,
            tag_matches=self._tag_match_ids, query=self._query,
            ids=self.library.ids, marks=self.library.marks, meta=self.library.meta,
        )

    def _on_details_loaded(self) -> None:
        if self._query.uses_meta:
            self._refresh_grid_display(keep_view=True)

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

        first_run = not self.library.assets and self.browse_folder is None and not get_scan_folders()
        self.welcome_panel.setVisible(first_run)
        if not filtered:
            self.list_grid.hide()
            self.lbl_empty_state.setVisible(not first_run)
            if self.archive_return is not None and not self.library.assets:
                name = self.archive_return.archive.name
                known = archives.known_listing(self.archive_return.archive)
                if self._opening_archive:
                    text = f"Opening {name}..."
                elif not self.archive_return.archive.is_file():
                    text = f"{name} isn't there any more.\nBack (Backspace) returns."
                elif known is not None and known.error:
                    text = f"Couldn't look inside {name}.\n{known.error}\nBack (Backspace) returns."
                else:
                    text = f"Nothing to show inside {name}: none of its files are of the types you scan.\nBack (Backspace) returns."
                self.lbl_empty_state.setText(text)
            elif not self.library.assets:
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
        self.status_info.setText(f"{len(filtered)} of {len(self.library.assets)} assets shown")

    def _marks_of(self, asset: AssetItem) -> Marks:
        return self.library.marks_of(asset)

    def _known_alpha(self, asset: AssetItem) -> Optional[bool]:
        """Whether an image is an alpha, if its details have been read (for its thumbnail)."""
        meta = self.library.meta_of(asset)
        return meta.alpha if meta is not None else None

    def _marks_for_index(self, index) -> Marks:
        asset = index.data(ASSET_ROLE)
        return self._marks_of(asset) if asset is not None else NO_MARKS

    def _asset_tooltip(self, asset: AssetItem) -> str:
        tags = self.library.tags.get(self.library.ids.get(str(asset.path)), [])
        tag_line = f"<br>Tags: {', '.join(t.name for t in tags)}" if tags else ""
        marks = self._marks_of(asset)
        if marks.favorite or marks.rating:
            parts = (["★ Favorite"] if marks.favorite else []) + ([f"Rating: {rating_text(marks.rating)}"] if marks.rating else [])  # glyph-ok: tooltip text
            tag_line += "<br>" + " • ".join(parts)
        member = asset.archive
        if member is not None:
            where = (
                f"Inside: {html.escape(member.archive.name)}"
                + (f" &gt; {html.escape(member.folder)}" if member.folder else "")
                + f"<br>Archive: {html.escape(str(member.archive))}"
            )
        else:
            where = f"Path: {html.escape(str(asset.path))}"
        contents = ""
        if asset.is_archive and member is None:
            # (only what's known already: hovering never reads the archive)
            known = archives.known_listing(asset.path)
            if known is not None and known.error:
                contents = f"<br>{html.escape(known.error)}"
            elif known is not None:
                summary = archives.summarize(known)
                kinds = ", ".join(f"{count} {kind}" for kind, count in summary.by_type[:6])
                more = " ..." if len(summary.by_type) > 6 else ""
                contents = f"<br>{summary.files} files: {kinds}{more}<br>Double-click to look inside"
        return (
            f"<b>{html.escape(asset.name)}</b><br>"
            f"Type: {asset.asset_group} ({asset.category_name})<br>"
            f"Size: {asset.formatted_size}<br>"
            f"Modified: {asset.formatted_modified_time}<br>"
            f"{where}"
            f"{contents}"
            f"{tag_line}"
        )

    def _on_async_thumbnail_loaded(self, path_str: str, pixmap: QPixmap) -> None:
        """Smoothly update thumbnail icon when background decoding completes."""
        self.grid_model.thumbnail_changed(path_str)

    def _on_thumbnail_skipped(self, path_str: str) -> None:
        """A thumbnail wasn't made because its card had scrolled away: if it's on screen after
        all, repainting it asks again."""
        self.grid_model.thumbnail_changed(path_str)

    def _on_theme_changed(self) -> None:
        """Thumbnails bake in the theme's card colours: make them again (quick: the pictures
        are cached on disk without their card), keeping the grid's selection and scroll position."""
        self.thumbnail_manager.clear_cache()
        self.list_grid.viewport().update()

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
        asset_id = self.library.ids.get(str(asset.path))
        tags = self.library.tags.get(asset_id, []) if asset_id is not None else None
        edit_tags = (lambda: self._open_tag_dialog([asset])) if asset_id is not None else None
        return ViewerTarget(asset, tags, edit_tags, position)

    def _open_viewer(self, asset: AssetItem) -> None:
        """Open the interactive AssetViewerDialog modal for the given asset (an archive: look inside it)."""
        if asset.is_archive:
            if asset.in_archive:
                self.status_label.setText(
                    f"{asset.name} is an archive inside an archive. Extract it (right-click > Extract...) to look inside."
                )
            else:
                self._open_archive(asset)
            return
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
            artbook=self.artbook_sender, model_viewed=self.thumbnail_manager.model_viewed,
        )
        dialog.exec()
        # Files inside archives unpacked to be shown: their thumbnails can be made now (videos, big files)
        for path_str in dialog.unpacked:
            self.thumbnail_manager.invalidate(path_str)
            self.grid_model.thumbnail_changed(path_str)
        # Free the viewer (GL context, decoded image, video player) instead of keeping it parented
        dialog.deleteLater()

    # ------------------------------------------------------------------
    # Archives: looked inside like folders; their files unpacked when needed (core.archives)
    # ------------------------------------------------------------------
    def _open_archive(self, asset: AssetItem) -> None:
        """Show the files inside an archive (Back or Backspace returns to what was shown). Archives
        inside archives aren't opened, so there's only ever one level to go back from."""
        self.archive_return = ArchiveReturn(asset.path, self.browse_folder, self.library.save_view(), self.search_bar.text())
        self.browse_folder = asset.path
        self.filter_mode, self.filter_value = "all", None  # (a sidebar row such as Archives would hide its files)
        self.sidebar_tree.setCurrentItem(self.sidebar_tree.item_all)
        self.library.clear()  # (not the library's files while the archive's are read)
        self._opening_archive = True
        self.search_bar.set_query_text("")
        self._update_library_controls()
        self._refresh_grid_display()
        self._start_scan([asset.path])
        self.list_grid.setFocus()

    def _leave_archive(self) -> None:
        """Back to what was shown before the archive was opened, with the archive selected."""
        back = self.archive_return
        if back is None:
            return
        self.archive_return = None
        self._opening_archive = False
        self.browse_folder = back.browse_folder
        self.library.restore_view(back.view)
        self._update_library_controls()
        self.btn_open_folder.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self.search_bar.set_query_text(back.search)
        self._update_sidebar_counts()
        self._refresh_grid_display()
        row = self.grid_model.row_of(str(back.archive))
        if row is not None:
            index = self.grid_model.index(row)
            self.list_grid.setCurrentIndex(index)
            self.list_grid.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.list_grid.setFocus()
        # Brought up to date in the background (and watched for changes again)
        if self.browse_folder is None:
            folders = [f for f in get_scan_folders() if f.is_dir()]
            if folders:
                self._start_scan(folders, save_snapshot=True)
        else:
            self._start_scan([self.browse_folder])

    def _on_archive_contents_toggled(self, show: bool) -> None:
        set_archive_contents(show)
        self._rescan_current_folder()

    def _unpack_now(self, assets: list[AssetItem], what: str) -> list[Path]:
        """Real files for assets, for another app: a file inside an archive is unpacked first, if it's
        small enough to wait for (archives.MAX_DRAG_BYTES); bigger ones are left out, with a word why."""
        files, too_big = [], []
        for asset in assets:
            if not asset.in_archive:
                files.append(asset.path)
                continue
            member = asset.archive
            copy = archives.cached_copy(member)
            if copy is None and member.size > archives.MAX_DRAG_BYTES:
                too_big.append(asset.name)
                continue
            if copy is None:
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    copy = archives.materialize(member)
                except archives.ArchiveError as exc:
                    self.status_label.setText(str(exc))
                    continue
                finally:
                    QApplication.restoreOverrideCursor()
            files.append(copy)
        if too_big:
            names = ", ".join(too_big[:3]) + (" ..." if len(too_big) > 3 else "")
            self.status_label.setText(
                f"Too big to {what} straight from the archive: {names}. Right-click > Extract... saves a copy first."
            )
        return files

    def _drag_file(self, asset: AssetItem) -> Optional[Path]:
        if not asset.in_archive:
            return asset.path
        files = self._unpack_now([asset], "drag")
        return files[0] if files else None

    def _send_files(self, paths: list[Path]) -> list[Path]:
        """Send to Artbook: the real files for the paths chosen (files inside archives unpacked)."""
        assets = [self.grid_model.asset_at(row) for row in (self.grid_model.row_of(str(p)) for p in paths) if row is not None]
        return self._unpack_now([a for a in assets if a is not None], "send")

    def _open_external(self, asset: AssetItem) -> None:
        """Open with the app Windows uses for the file (inside an archive: unpacked first, with progress)."""
        if not asset.in_archive:
            if not file_actions.open_file(asset.path):
                self.status_label.setText(f"'{asset.name}' isn't there any more.")
            return

        def opened(path: Path) -> None:
            file_actions.open_file(path)
            self.status_label.setText(f"Opened {asset.name} (unpacked from {asset.archive.archive.name}).")

        self._unpack_with_progress(
            f"Unpacking {asset.name}", lambda progress, stop: archives.materialize(asset.archive, progress, stop), opened,
        )

    def _extract(self, asset: AssetItem) -> None:
        """Extract To... (an archive: every file, into a folder named after it) or Extract... (one file)."""
        if asset.in_archive:
            target, _filter = QFileDialog.getSaveFileName(
                self, f"Extract {asset.name}", str(asset.archive.archive.parent / asset.name),
            )
            if not target:
                return  # (the dialog itself asks before replacing a file)
            self._unpack_with_progress(
                f"Extracting {asset.name}",
                lambda progress, stop: archives.extract_one(asset.archive, Path(target), progress, stop),
                lambda path: self.status_label.setText(f"Extracted {asset.name} to {path.parent}."),
            )
            return
        chosen = QFileDialog.getExistingDirectory(self, f"Extract {asset.name} To", str(asset.path.parent))
        if not chosen:
            return
        result = archives.listing(asset.path, asset.size_bytes, asset.modified_timestamp)
        if result.error:
            self.status_label.setText(f"Couldn't extract {asset.name}: {result.error}")
            return
        folder = Path(chosen) / asset.path.stem
        plan = archives.plan_extract(result.members, folder)
        replace = False
        if plan.existing:
            count = len(plan.existing)
            box = QMessageBox(QMessageBox.Icon.Question, "Files Already There",
                              f"{count} file{'s' if count != 1 else ''} from {asset.name} already exist{'s' if count == 1 else ''} in {folder}.",
                              parent=self)
            replace_button = box.addButton("Replace Them", QMessageBox.ButtonRole.AcceptRole)
            skip_button = box.addButton("Skip Them", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(skip_button)
            box.exec()
            clicked = box.clickedButton()
            box.deleteLater()
            if clicked not in (replace_button, skip_button):
                return
            replace = clicked is replace_button

        def finished(outcome: archives.ExtractResult) -> None:
            parts = [f"Extracted {outcome.written} file{'s' if outcome.written != 1 else ''} to {folder}"]
            if outcome.skipped:
                parts.append(f"skipped {outcome.skipped}")
            if outcome.failed:
                parts.append(f"{len(outcome.failed)} failed ({outcome.failed[0]})")
            self.status_label.setText(", ".join(parts) + ".")

        self._unpack_with_progress(
            f"Extracting {asset.name}", lambda progress, stop: archives.extract(plan, replace, progress, stop), finished,
        )

    def _unpack_with_progress(self, what: str, work, on_done) -> None:
        """Run work(progress, should_stop) in the background, with its progress in the status line."""
        self.status_label.setText(f"{what}...")

        def progress(done: int, total: int) -> None:
            self.status_label.setText(f"{what}... {int(100 * done / total) if total else 100}%")

        unpack_in_background(
            work, on_done, lambda message: self.status_label.setText(f"{what} failed: {message}"), progress,
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Handle keyboard shortcuts (e.g. Spacebar to quick-preview)."""
        if shortcuts.matches("main.preview", event):
            selected = self._selected_assets()
            if selected:
                self._open_viewer(selected[0])
                return
        super().keyPressEvent(event)

    SEARCH_WIDTH = (280, 150)  # normal, compact toolbar

    def _on_toolbar_compact(self, compact: bool) -> None:
        self.search_bar.setMinimumWidth(self.SEARCH_WIDTH[compact])

    def eventFilter(self, obj, event) -> bool:
        if obj is self.asset_panel and event.type() == event.Type.Resize:
            self.asset_toolbar.update()
        if obj is self.list_grid and event.type() == event.Type.KeyPress:
            if self.archive_return is not None and shortcuts.matches("main.leave_archive", event):
                self._leave_archive()
                return True
            if shortcuts.matches("main.favorite", event):
                self._toggle_favorite()
                return True
            for shortcut_id, rating in RATING_SHORTCUTS:
                if shortcuts.matches(shortcut_id, event):
                    self._set_rating(rating)
                    return True
        return super().eventFilter(obj, event)

    def _selected_assets(self) -> list[AssetItem]:
        """Selected assets in grid order."""
        rows = sorted(index.row() for index in self.list_grid.selectionModel().selectedIndexes())
        return [a for a in (self.grid_model.asset_at(r) for r in rows) if a is not None]

    def _selected_asset_ids(self) -> list[int]:
        return [self.library.ids[str(a.path)] for a in self._selected_assets() if str(a.path) in self.library.ids]

    def _toggle_favorite(self) -> None:
        ids = self._selected_asset_ids()
        if not ids:
            return
        # Like most apps: if any selected file isn't a favorite yet, star them all; else unstar
        favorite = not all(self.library.marks.get(i, NO_MARKS).favorite for i in ids)
        files = f"{len(ids)} file{'s' if len(ids) != 1 else ''}"
        self._change_marks(
            {i: Marks(favorite, self.library.marks.get(i, NO_MARKS).rating) for i in ids},
            f"{'Added' if favorite else 'Removed'} {files} {'to' if favorite else 'from'} Favorites.",
            "favorite" if favorite else "unfavorite",
        )

    def _set_rating(self, rating: int) -> None:
        ids = self._selected_asset_ids()
        if not ids:
            return
        files = f"{len(ids)} file{'s' if len(ids) != 1 else ''}"
        self._change_marks(
            {i: Marks(self.library.marks.get(i, NO_MARKS).favorite, rating) for i in ids},
            f"Rated {files} {rating_text(rating)}." if rating else f"Cleared the rating of {files}.",
            "rating",
        )

    def _change_marks(self, new: dict[int, Marks], message: str, what: str) -> None:
        old = {i: self.library.marks.get(i, NO_MARKS) for i in new}
        self._apply_marks(new, message)
        files = f"{len(new)} file{'s' if len(new) != 1 else ''}"
        self.undo_history.push(UndoStep(
            f"{what} on {files}",
            lambda: self._apply_marks(old, f"Undid the {what} change."),
            lambda: self._apply_marks(new, message),
        ))

    def _apply_marks(self, marks: dict[int, Marks], message: str) -> None:
        self.library.set_marks(marks)
        self._after_marks_changed(message)

    def _reload_marks(self) -> None:
        """Read every file's favorite and rating from the database again (after an import)."""
        self.library.reload_marks()
        self._update_sidebar_counts()
        if self.filter_mode == "favorites" or self._query.uses_marks:
            self._refresh_grid_display(keep_view=True)
        else:
            self.list_grid.viewport().update()

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
        actions = AssetMenuActions(
            open_viewer=lambda: self._open_viewer(asset),
            reveal=lambda: self._reveal_in_explorer(asset.archive.archive if asset.in_archive else asset.path),
            tag=lambda: self._open_tag_dialog(),
            toggle_favorite=self._toggle_favorite,
            set_rating=self._set_rating,
            open_external=lambda: self._open_external(asset),
            extract=lambda: self._extract(asset),
            send_files=self._send_files,
        )
        marks = [self.library.marks.get(i, NO_MARKS) for i in self._selected_asset_ids()]
        menu = build_asset_menu(self, asset, self._selected_assets(), marks, actions, self.artbook_sender)
        menu.exec(self.list_grid.mapToGlobal(pos))
        menu.deleteLater()  # (its actions belong to it: nothing is left behind on the window)

    # ------------------------------------------------------------------
    # Tagging
    # ------------------------------------------------------------------
    def _open_tag_dialog(self, assets: Optional[list[AssetItem]] = None) -> Optional[list[Tag]]:
        """Tag the given assets (default: the grid selection).

        Returns the first asset's updated tags if anything changed (used by the viewer), else None.
        """
        if assets is None:
            assets = self._selected_assets()
        ids = [self.library.ids[str(a.path)] for a in assets if str(a.path) in self.library.ids]
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
        return self.library.tags.get(ids[0], [])

    def _after_tags_changed(self, asset_ids: Optional[list[int]] = None) -> None:
        """Refresh cached tags, tooltips, suggestions and tag search after tags were edited.

        asset_ids: the files that changed, or None after tags were renamed / recolored / deleted
        (which can affect every file; reloaded in the background).
        """
        self._reload_tag_suggestions()
        if asset_ids is None:
            self.sidebar_tree.reload_collections()  # (renaming a tag renames it in saved searches too)
        # Fingerprint newly tagged files right away, so their tags survive a move before the next scan
        run_in_background(self.repo, lambda repo: repo.fill_missing_hashes(), self._ignore_result)
        if asset_ids is not None:
            self._apply_tag_cache(self.repo.tags_by_asset(asset_ids))
        else:
            asset_ids = list(self.library.ids.values())  # (copied here: the job runs on another thread)
            run_in_background(self.repo, lambda repo: repo.tags_by_asset(asset_ids), self._apply_tag_cache)

    @staticmethod
    def _ignore_result(_result) -> None:
        pass

    def _apply_tag_cache(self, tags_by_asset: dict[int, list[Tag]]) -> None:
        self.library.update_tags(tags_by_asset)  # tooltips are built on hover, so they pick this up
        if self.search_bar.has_tag_filters():
            self._run_tag_search()

    def _reload_tag_suggestions(self) -> None:
        try:
            self.search_bar.set_available_tags(self.repo.list_tags())
        except Exception as exc:  # pylint: disable=broad-except
            self.status_label.setText(f"Tags unavailable: {exc}")

    def _reveal_in_explorer(self, path: Path) -> None:
        """Show the file selected in its folder in File Explorer."""
        try:
            if not file_actions.reveal(path):
                self.status_label.setText(f"'{path.name}' isn't there any more.")
        except OSError as exc:
            self.status_label.setText(f"Couldn't open File Explorer: {exc}")

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
        box.deleteLater()
        if box.clickedButton() is btn_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(crash_log.crash_log_path())))

    def closeEvent(self, event) -> None:
        """Ensure background scan and thumbnail threads terminate cleanly on exit."""
        # Stop background work before Qt is torn down: a job still running then crashes the process
        self.library.shutdown()  # the scan, live updates and file-details reading
        self.view_modes.shutdown()  # the separate Artbook window closes with the app
        self.thumbnail_manager.shutdown()
        wait_for_background()
        self.artbook_panel.save_now()  # after background jobs, so an older autosave can't land on top
        super().closeEvent(event)


# ----------------------------------------------------------------------
# Application Entry Point (the release build's --self-test is in self_test.py)
# ----------------------------------------------------------------------
def main() -> None:
    """Initialize and run the PySide6 application."""
    if "--self-test" in sys.argv:
        index = sys.argv.index("--self-test")
        log_path = sys.argv[index + 1] if index + 1 < len(sys.argv) else None
        sys.exit(run_self_test(log_path))
    if "--theme-studio" in sys.argv:
        # The Theme Studio on its own (ui.theme_studio): the app's window isn't made at all
        from ui.theme_studio import run as run_theme_studio

        sys.exit(run_theme_studio(sys.argv))

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
    apply_new_defaults()  # new file types (zip, rar, 7z) reach the user's own lists once
    archives.configure_seven_zip(get_seven_zip_path())
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
    title_bar.install(app)  # title bars in the theme's colours, not Windows' light / dark
    main_thread_gc.install(app)  # a collection on a worker thread can free UI objects there, and crash
    scroll_bar_watch.install(app)  # logs which view had its scroll bar freed under it (a crash that follows)

    # Hang watchdog: if the UI thread stops ticking, crash_log dumps every thread's stack
    heartbeat = QTimer(app)
    heartbeat.timeout.connect(crash_log.heartbeat)
    heartbeat.start(2000)
    crash_log.heartbeat()

    # Keep the thumbnail disk cache under its size limit (off the UI thread; it's only file deletes)
    threading.Thread(target=prune_disk_cache, name="prune-thumbnails", daemon=True).start()
    threading.Thread(target=archives.prune_extracted, name="prune-unpacked", daemon=True).start()

    window = MainWindow()
    window.show()
    if previous_session_crashed:
        QTimer.singleShot(400, window.show_crash_notice)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
