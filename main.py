"""Asset Manager - Windows desktop application using PySide6.

Features:
- Category and format navigation sidebar (3D Models, Textures, Documents)
- Grid view area for asset thumbnails with multi-format support
- Status bar for asset counts and selection details
- Settings for library folders, scanned file types, sidebar categories, and appearance
- Built-in lightweight interactive viewer for 3D objects, textures, and PDFs
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QObject,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QThread,
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
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.scanner import AssetItem, AssetScanner
from core.settings import (
    category_extensions,
    get_categories,
    get_file_extensions,
    get_scan_folders,
    get_theme,
    get_thumbnail_size,
    is_scan_folder,
    set_scan_folders,
)
from core.thumbnail_manager import ThumbnailManager
from core.updater import cleanup_after_update
from core.version import APP_NAME
from ui.icons import category_icon, color_dot_icon, resolve_category_colors
from ui.settings_dialog import SettingsDialog
from ui.themes import apply_theme
from ui.viewer import AssetViewerDialog

# ----------------------------------------------------------------------
# Background Scanner Thread
# ----------------------------------------------------------------------
class ScanWorker(QObject):
    """Background worker to scan folders without blocking the UI thread."""

    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, folders: list[Path], extensions: list[str]):
        super().__init__()
        self.folders = folders
        self.extensions = extensions

    def run(self) -> None:
        try:
            scanner = AssetScanner(supported_extensions=self.extensions)
            assets: list[AssetItem] = []
            seen: set[Path] = set()
            # Overlapping folders (e.g. a parent and its subfolder) must not duplicate files
            for folder in self.folders:
                for asset in scanner.scan(folder, recursive=True):
                    if asset.path not in seen:
                        seen.add(asset.path)
                        assets.append(asset)
            assets.sort(key=lambda a: a.name.lower())
            self.finished.emit(assets)
        except Exception as exc:  # pylint: disable=broad-except
            self.failed.emit(str(exc))


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
        self._grid_item_map: dict[str, QListWidgetItem] = {}

        self._init_ui()
        self._load_library()

    def _init_ui(self) -> None:
        """Construct the UI layout and widgets."""
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. Top Control Bar
        control_bar = self._build_control_bar()
        main_layout.addWidget(control_bar)

        # 2. Main Splitter (Sidebar + Thumbnail Grid)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)

        splitter.addWidget(self._build_sidebar())

        self.grid_container = self._build_grid_view()
        splitter.addWidget(self.grid_container)

        splitter.setSizes([240, 980])
        main_layout.addWidget(splitter, 1)

        # 3. Status Bar
        self._build_status_bar()

    def _build_control_bar(self) -> QWidget:
        """Build top toolbar with folder picker, library toggle, refresh, preview, search, and settings."""
        bar = QWidget()
        bar.setObjectName("controlBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        # "Open Folder" button: browse a single folder outside the library
        self.btn_open_folder = QPushButton("Open Folder")
        self.btn_open_folder.setObjectName("primaryBtn")
        self.btn_open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_folder.setToolTip("Browse a single folder without adding it to the library")
        self.btn_open_folder.clicked.connect(self._on_choose_folder)
        layout.addWidget(self.btn_open_folder)

        # "Library" button: return to scanning all library folders
        self.btn_library = QPushButton("Library")
        self.btn_library.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_library.setToolTip("Show assets from all library folders")
        self.btn_library.clicked.connect(self._load_library)
        layout.addWidget(self.btn_library)

        # "Add to Library" toggle for the browsed folder
        self.btn_library_toggle = QPushButton("☆ Add to Library")
        self.btn_library_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_library_toggle.clicked.connect(self._on_toggle_library_folder)
        layout.addWidget(self.btn_library_toggle)

        # "Refresh" button
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.clicked.connect(self._rescan_current_folder)
        layout.addWidget(self.btn_refresh)

        # "Preview" button
        self.btn_view_asset = QPushButton("Preview")
        self.btn_view_asset.setObjectName("viewBtn")
        self.btn_view_asset.setEnabled(False)
        self.btn_view_asset.setToolTip("Open lightweight viewer for selected asset (or press Space)")
        self.btn_view_asset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_view_asset.clicked.connect(self._open_viewer_for_selected)
        layout.addWidget(self.btn_view_asset)

        # Path display field
        self.txt_path = QLineEdit()
        self.txt_path.setReadOnly(True)
        self.txt_path.setPlaceholderText("No library folders. Add some in Settings > Folders, or click 'Open Folder'...")
        layout.addWidget(self.txt_path, 1)

        # Search filter
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search assets...")
        self.txt_search.setFixedWidth(220)
        self.txt_search.textChanged.connect(self._on_search_text_changed)
        layout.addWidget(self.txt_search)

        # Settings
        self.btn_settings = QPushButton("⚙ Settings")
        self.btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_settings.setToolTip("Settings (Ctrl+,)")
        self.btn_settings.setShortcut("Ctrl+,")
        self.btn_settings.clicked.connect(self._open_settings)
        layout.addWidget(self.btn_settings)

        return bar

    def _build_sidebar(self) -> QTreeWidget:
        """Build the category navigation sidebar; rows come from the Categories settings."""
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setIconSize(QSize(16, 16))
        tree.setIndentation(16)
        tree.itemClicked.connect(self._on_sidebar_item_clicked)
        self.sidebar_tree = tree
        self._rebuild_sidebar()
        return tree

    def _rebuild_sidebar(self) -> None:
        """(Re)create sidebar rows from the saved categories and reset the filter to All Assets."""
        tree = self.sidebar_tree
        tree.clear()
        self._sidebar_rows.clear()

        self.item_all = QTreeWidgetItem(tree, ["All Assets (0)"])
        self.item_all.setIcon(0, color_dot_icon("#93c5fd"))
        self.item_all.setData(0, Qt.ItemDataRole.UserRole, ("all", None))

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

        tree.setCurrentItem(self.item_all)
        self.filter_mode, self.filter_value = "all", None
        self._update_sidebar_counts()

    def _update_sidebar_counts(self) -> None:
        """Refresh the asset counts shown on each sidebar row."""
        ext_counts: dict[str, int] = {}
        for asset in self.all_assets:
            ext_counts[asset.extension] = ext_counts.get(asset.extension, 0) + 1

        self.item_all.setText(0, f"All Assets ({len(self.all_assets)})")
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

        self.list_grid = QListWidget()
        self.list_grid.setObjectName("assetGrid")
        self.list_grid.setViewMode(QListWidget.ViewMode.IconMode)
        self._apply_grid_size()
        self.list_grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_grid.setMovement(QListWidget.Movement.Static)
        self.list_grid.setWordWrap(True)
        self.list_grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_grid.customContextMenuRequested.connect(self._show_context_menu)
        self.list_grid.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.list_grid.itemSelectionChanged.connect(self._on_item_selection_changed)

        # Empty state label shown when no assets are present
        self.lbl_empty_state = QLabel(
            "No assets to display.\nClick 'Open Folder' to scan 3D models, textures, or documents."
        )
        self.lbl_empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_empty_state.setObjectName("emptyState")

        layout.addWidget(self.lbl_empty_state)
        layout.addWidget(self.list_grid)
        self.list_grid.hide()

        return container

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
            self._start_scan(folders)
        else:
            self.all_assets = []
            self._update_sidebar_counts()
            self._refresh_grid_display()
            self.status_label.setText("No library folders set. Add folders in Settings > Folders.")

    def _update_library_controls(self) -> None:
        """Update the path field and library buttons for the current mode."""
        if self.browse_folder is None:
            folders = get_scan_folders()
            self.txt_path.setText("; ".join(str(f) for f in folders))
            self.txt_path.setToolTip("\n".join(str(f) for f in folders))
            self.btn_library.setEnabled(False)
            self.btn_library_toggle.hide()
            return

        self.txt_path.setText(str(self.browse_folder))
        self.txt_path.setToolTip(str(self.browse_folder))
        self.btn_library.setEnabled(True)
        self.btn_library_toggle.show()
        if is_scan_folder(self.browse_folder):
            self.btn_library_toggle.setText("★ In Library")
            self.btn_library_toggle.setObjectName("defaultActiveBtn")
            self.btn_library_toggle.setToolTip("This folder is in the library. Click to remove it.")
        else:
            self.btn_library_toggle.setText("☆ Add to Library")
            self.btn_library_toggle.setObjectName("")
            self.btn_library_toggle.setToolTip("Add this folder to the library so it's scanned on startup")

        # Re-apply stylesheet to update button style ID
        self.btn_library_toggle.style().unpolish(self.btn_library_toggle)
        self.btn_library_toggle.style().polish(self.btn_library_toggle)

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
        dialog = SettingsDialog(self)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
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

    def _start_scan(self, folders: list[Path]) -> None:
        """Scan directories in a background thread to keep UI snappy."""
        self.thumbnail_manager.clear_cache()
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
        self.scan_worker = ScanWorker(folders, get_file_extensions())
        self.scan_worker.moveToThread(self.scan_thread)

        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.finished.connect(self._on_scan_finished)
        self.scan_worker.failed.connect(self._on_scan_failed)
        self.scan_worker.finished.connect(self.scan_thread.quit)
        self.scan_worker.failed.connect(self.scan_thread.quit)

        self.scan_thread.start()

    def _on_scan_finished(self, assets: list[AssetItem]) -> None:
        """Handle scan results, update categories, and populate grid."""
        self.btn_open_folder.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self.all_assets = assets
        self._update_sidebar_counts()

        models = sum(1 for a in assets if a.is_3d_model)
        textures = sum(1 for a in assets if a.is_texture)
        docs = sum(1 for a in assets if a.is_document)
        other = len(assets) - models - textures - docs
        summary = f"{models} 3D models, {textures} textures, {docs} documents"
        if other:
            summary += f", {other} other"
        self.status_label.setText(f"Loaded {len(assets)} assets ({summary})")
        self.status_info.setText(f"{len(assets)} assets")

        self._refresh_grid_display()

    def _on_scan_failed(self, error_msg: str) -> None:
        """Handle scanning failure."""
        self.btn_open_folder.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self.status_label.setText(f"Scan failed: {error_msg}")

    # ------------------------------------------------------------------
    # Filtering & Grid Rendering
    # ------------------------------------------------------------------
    def _on_sidebar_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """Filter assets when clicking a sidebar category or group."""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            self.filter_mode, self.filter_value = data
            self._refresh_grid_display()

    def _on_search_text_changed(self, text: str) -> None:
        """Filter grid items in real-time as user types."""
        self.search_filter = text.strip().lower()
        self._refresh_grid_display()

    def _get_filtered_assets(self) -> list[AssetItem]:
        """Compute filtered assets based on active category and search filter."""
        filtered = self.all_assets

        # Apply sidebar category filter
        if self.filter_mode == "exts" and self.filter_value is not None:
            filtered = [a for a in filtered if a.extension in self.filter_value]
        elif self.filter_mode == "uncategorized":
            filtered = [a for a in filtered if a.extension not in self._categorized_exts]

        # Apply search filter
        if self.search_filter:
            filtered = [a for a in filtered if self.search_filter in a.name.lower()]

        return filtered

    def _refresh_grid_display(self) -> None:
        """Populate thumbnail grid with currently filtered assets without blocking UI thread."""
        self.list_grid.clear()
        self._grid_item_map.clear()
        filtered = self._get_filtered_assets()

        if not filtered:
            self.list_grid.hide()
            self.lbl_empty_state.show()
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
            return

        self.lbl_empty_state.hide()
        self.list_grid.show()

        for asset in filtered:
            # Immediate retrieval (cached or instant procedural placeholder)
            pixmap = self.thumbnail_manager.get_thumbnail(asset, size=self.thumbnail_size)
            icon = QIcon(pixmap)

            item = QListWidgetItem(icon, asset.name)
            item.setData(Qt.ItemDataRole.UserRole, asset)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            item.setToolTip(
                f"<b>{asset.name}</b><br>"
                f"Type: {asset.asset_group} ({asset.category_name})<br>"
                f"Size: {asset.formatted_size}<br>"
                f"Modified: {asset.formatted_modified_time}<br>"
                f"Path: {asset.path}"
            )
            self.list_grid.addItem(item)
            self._grid_item_map[str(asset.path)] = item

        self.status_info.setText(f"{len(filtered)} of {len(self.all_assets)} assets shown")

    def _on_async_thumbnail_loaded(self, path_str: str, pixmap: QPixmap) -> None:
        """Smoothly update thumbnail icon when background decoding completes."""
        if path_str in self._grid_item_map:
            item = self._grid_item_map[path_str]
            item.setIcon(QIcon(pixmap))

    # ------------------------------------------------------------------
    # User Interactions & Viewer Trigger
    # ------------------------------------------------------------------
    def _on_item_selection_changed(self) -> None:
        """Update status bar and preview button state based on selection."""
        selected_items = self.list_grid.selectedItems()
        self.btn_view_asset.setEnabled(len(selected_items) == 1)

        if not selected_items:
            self.status_label.setText(f"Showing {self.list_grid.count()} assets")
            return

        if len(selected_items) == 1:
            asset: AssetItem = selected_items[0].data(Qt.ItemDataRole.UserRole)
            self.status_label.setText(
                f"Selected: {asset.name} ({asset.category_name}, {asset.formatted_size})"
            )
        else:
            self.status_label.setText(f"{len(selected_items)} assets selected")

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        """Open built-in lightweight viewer on double click."""
        asset: AssetItem = item.data(Qt.ItemDataRole.UserRole)
        if asset:
            self._open_viewer(asset)

    def _open_viewer_for_selected(self) -> None:
        """Open built-in lightweight viewer for selected item."""
        selected_items = self.list_grid.selectedItems()
        if selected_items:
            asset: AssetItem = selected_items[0].data(Qt.ItemDataRole.UserRole)
            if asset:
                self._open_viewer(asset)

    def _open_viewer(self, asset: AssetItem) -> None:
        """Open the interactive AssetViewerDialog modal for the given asset."""
        dialog = AssetViewerDialog(asset, self)
        dialog.exec()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Handle keyboard shortcuts (e.g. Spacebar to quick-preview)."""
        if event.key() == Qt.Key.Key_Space:
            selected_items = self.list_grid.selectedItems()
            if selected_items:
                asset: AssetItem = selected_items[0].data(Qt.ItemDataRole.UserRole)
                if asset:
                    self._open_viewer(asset)
                    return
        super().keyPressEvent(event)

    def _show_context_menu(self, pos) -> None:
        """Display right-click context menu for selected asset."""
        item = self.list_grid.itemAt(pos)
        if not item:
            return

        asset: AssetItem = item.data(Qt.ItemDataRole.UserRole)
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

        action_copy_path = QAction("Copy Full Path", self)
        action_copy_path.triggered.connect(
            lambda: QGuiApplication.clipboard().setText(str(asset.path))
        )
        menu.addAction(action_copy_path)

        menu.exec(self.list_grid.mapToGlobal(pos))

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

    def closeEvent(self, event) -> None:
        """Ensure background scan and thumbnail threads terminate cleanly on exit."""
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.quit()
            self.scan_thread.wait(1000)
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
        dialog = SettingsDialog()
        dialog.close()
        from PySide6.QtPdf import QPdfDocument  # noqa: F401
        return "settings dialog built"

    step("imports", check_imports)
    step("3D model loading", check_models)
    step("image thumbnails", check_images)
    step("user interface", check_ui)

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

    # Suppress cosmetic Qt libtiff warnings and allocation limit messages
    def _qt_message_handler(mode, context, message):
        if "ImageDescription" in message or "allocation limit" in message:
            return  # Filter known harmless libtiff tag / memory limit logs
        if sys.stderr is not None:  # None in the windowed (--noconsole) executable
            sys.stderr.write(f"{message}\n")

    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtGui import QImageReader, QSurfaceFormat

    # Configure hardware OpenGL format with 24-bit depth and 4x MSAA antialiasing
    gl_format = QSurfaceFormat()
    gl_format.setDepthBufferSize(24)
    gl_format.setSamples(4)
    gl_format.setVersion(3, 3)
    gl_format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(gl_format)

    cleanup_after_update()
    qInstallMessageHandler(_qt_message_handler)
    QImageReader.setAllocationLimit(2048)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    apply_theme(get_theme(), app)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
