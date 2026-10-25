"""Settings dialog for the Asset Manager.

Pages:
- General > Folders:     library folders scanned for assets
- General > Files:       file extensions the scanner picks up
- General > Categories:  sidebar main categories / subcategories and their file types
- General > Appearance:  theme, grid size, category colors & icons
- General > Video:       ffmpeg location, playback backend, preview cache
- General > Tags:        custom tags (create, rename, recolor, delete)
- General > Shortcuts:   every keyboard shortcut / mouse control (from ui.shortcuts)
- About:                 application description, version, GitHub update check
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.scanner import AssetItem
from core.settings import (
    DEFAULT_CATEGORIES,
    DEFAULT_FILE_EXTENSIONS,
    MAX_CATEGORY_ICON_BYTES,
    MEDIA_BACKENDS,
    PREVIEWABLE_EXTENSIONS,
    THUMBNAIL_SIZE_RANGE,
    category_extensions,
    get_categories,
    get_ffmpeg_path,
    get_file_extensions,
    get_media_backend,
    get_scan_folders,
    get_theme,
    get_thumbnail_size,
    import_category_icon,
    parse_extension_list,
    remove_unused_category_icons,
    set_categories,
    set_ffmpeg_path,
    set_file_extensions,
    set_media_backend,
    set_scan_folders,
    set_theme,
    set_thumbnail_size,
)
from core.tag_repository import MAX_TAG_NAME_LENGTH, Tag, TagError, TagRepository, get_repository
from core.thumbnail_manager import render_placeholder_image
from core.updater import (
    UpdateError,
    UpdateInfo,
    backup_description,
    check_for_update,
    install_update,
    restart_application,
)
from core.version import APP_NAME, APP_VERSION, GITHUB_URL
from core.video_tools import CACHE_ROOT, cache_size_bytes, clear_video_cache, find_ffmpeg
from ui.icons import (
    MAIN_CATEGORY_COLORS,
    SUB_CATEGORY_COLORS,
    category_icon,
    color_swatch_icon,
    load_icon_image,
    resolve_category_colors,
)
from ui import shortcuts
from ui.tag_widgets import next_tag_color
from ui.themes import THEMES, apply_theme

# Data roles on category tree items
EXTENSIONS_ROLE = Qt.ItemDataRole.UserRole
COLOR_ROLE = Qt.ItemDataRole.UserRole + 1
ICON_ROLE = Qt.ItemDataRole.UserRole + 2


def _page_header(title: str, hint: str) -> QWidget:
    header = QWidget()
    layout = QVBoxLayout(header)
    layout.setContentsMargins(0, 0, 0, 8)
    layout.setSpacing(4)
    lbl_title = QLabel(title)
    lbl_title.setObjectName("pageTitle")
    lbl_hint = QLabel(hint)
    lbl_hint.setObjectName("pageHint")
    lbl_hint.setWordWrap(True)
    layout.addWidget(lbl_title)
    layout.addWidget(lbl_hint)
    return header


# ----------------------------------------------------------------------
# General > Folders
# ----------------------------------------------------------------------
class FoldersPage(QWidget):
    """Manage the list of library folders scanned for assets."""

    def __init__(self, folders: list[Path], parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(_page_header(
            "Folders",
            "Library folders are scanned (including all subfolders) on startup and when you click Refresh.",
        ))

        self.list_folders = QListWidget()
        self.list_folders.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_folders.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.list_folders, 1)

        buttons = QHBoxLayout()
        self.btn_add = QPushButton("+ Add Folder...")
        self.btn_add.setObjectName("primaryBtn")
        self.btn_add.clicked.connect(self._on_add)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self._on_remove)
        buttons.addWidget(self.btn_add)
        buttons.addWidget(self.btn_remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        for folder in folders:
            self._add_folder_item(folder)
        self._update_buttons()

    def _add_folder_item(self, folder: Path) -> None:
        item = QListWidgetItem(str(folder))
        item.setData(Qt.ItemDataRole.UserRole, str(folder))
        if not folder.is_dir():
            item.setText(f"{folder}   (not found)")
            item.setForeground(Qt.GlobalColor.red)
            item.setToolTip("This folder doesn't exist or isn't reachable; it will be skipped when scanning.")
        self.list_folders.addItem(item)

    def _on_add(self) -> None:
        start = self.folders()[-1] if self.folders() else Path.home()
        chosen = QFileDialog.getExistingDirectory(self, "Add Library Folder", str(start))
        if not chosen:
            return
        folder = Path(chosen).resolve()
        if any(folder == existing.resolve() for existing in self.folders()):
            QMessageBox.information(self, "Already Added", f"'{folder}' is already a library folder.")
            return
        self._add_folder_item(folder)

    def _on_remove(self) -> None:
        for item in self.list_folders.selectedItems():
            self.list_folders.takeItem(self.list_folders.row(item))

    def _update_buttons(self) -> None:
        self.btn_remove.setEnabled(bool(self.list_folders.selectedItems()))

    def folders(self) -> list[Path]:
        return [
            Path(self.list_folders.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.list_folders.count())
        ]


# ----------------------------------------------------------------------
# General > Files
# ----------------------------------------------------------------------
class FilesPage(QWidget):
    """Edit the comma-separated list of file types the scanner picks up."""

    validity_changed = Signal(bool)

    def __init__(self, extensions: list[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(_page_header(
            "Files",
            "File types to scan for, separated by commas. Format: .pdf, .png, .obj, .fbx",
        ))

        self.txt_extensions = QPlainTextEdit()
        self.txt_extensions.setPlaceholderText(".pdf, .png, .obj, .fbx")
        self.txt_extensions.setPlainText(", ".join(extensions))
        self.txt_extensions.textChanged.connect(self._validate)
        layout.addWidget(self.txt_extensions, 1)

        self.lbl_count = QLabel()
        self.lbl_count.setObjectName("pageHint")
        layout.addWidget(self.lbl_count)

        self.lbl_warning = QLabel()
        self.lbl_warning.setObjectName("warningLabel")
        self.lbl_warning.setWordWrap(True)
        layout.addWidget(self.lbl_warning)

        self.lbl_note = QLabel()
        self.lbl_note.setObjectName("noteLabel")
        self.lbl_note.setWordWrap(True)
        layout.addWidget(self.lbl_note)

        buttons = QHBoxLayout()
        btn_reset = QPushButton("Reset to Defaults")
        btn_reset.clicked.connect(
            lambda: self.txt_extensions.setPlainText(", ".join(DEFAULT_FILE_EXTENSIONS))
        )
        buttons.addWidget(btn_reset)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._extensions: list[str] = []
        self._valid = True
        self._validate()

    def _validate(self) -> None:
        extensions, errors = parse_extension_list(self.txt_extensions.toPlainText())
        self._extensions = extensions
        valid = not errors

        if errors:
            shown = errors[:6]
            more = f"\n…and {len(errors) - 6} more" if len(errors) > 6 else ""
            self.lbl_warning.setText("⚠ Invalid format:\n• " + "\n• ".join(shown) + more)
            self.lbl_warning.show()
        else:
            self.lbl_warning.hide()

        unpreviewable = [e for e in extensions if e not in PREVIEWABLE_EXTENSIONS]
        if unpreviewable:
            self.lbl_note.setText(
                "Note: these types will be listed but can't be previewed in the built-in viewer yet: "
                + ", ".join(unpreviewable)
            )
            self.lbl_note.show()
        else:
            self.lbl_note.hide()

        self.lbl_count.setText(f"{len(extensions)} file type(s)")

        self.txt_extensions.setProperty("invalid", not valid)
        self.txt_extensions.style().unpolish(self.txt_extensions)
        self.txt_extensions.style().polish(self.txt_extensions)

        if valid != self._valid:
            self._valid = valid
            self.validity_changed.emit(valid)

    def is_valid(self) -> bool:
        return self._valid

    def extensions(self) -> list[str]:
        return list(self._extensions)


# ----------------------------------------------------------------------
# General > Categories
# ----------------------------------------------------------------------
class CategoriesPage(QWidget):
    """Edit sidebar main categories, subcategories, and the file types each shows."""

    def __init__(self, categories: list[dict[str, Any]], extensions: list[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._available: list[str] = list(extensions)
        self._updating_checks = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(_page_header(
            "Categories",
            "Main categories and subcategories appear in the sidebar. A main category shows its own "
            "file types plus everything in its subcategories. Double-click a name to rename it.",
        ))

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)

        # Left: category tree + controls
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)

        self.tree = QTreeWidget()
        self.tree.setObjectName("categoryTree")
        self.tree.setHeaderHidden(True)
        self.tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self.tree.itemChanged.connect(self._on_item_renamed)
        left_layout.addWidget(self.tree, 1)

        row1 = QHBoxLayout()
        self.btn_add_main = QPushButton("+ Category")
        self.btn_add_main.clicked.connect(self._on_add_main)
        self.btn_add_sub = QPushButton("+ Subcategory")
        self.btn_add_sub.clicked.connect(self._on_add_sub)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self._on_remove)
        row1.addWidget(self.btn_add_main)
        row1.addWidget(self.btn_add_sub)
        row1.addWidget(self.btn_remove)
        left_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_up = QPushButton("▲ Up")
        self.btn_up.clicked.connect(lambda: self._move_current(-1))
        self.btn_down = QPushButton("▼ Down")
        self.btn_down.clicked.connect(lambda: self._move_current(1))
        btn_reset = QPushButton("Reset to Defaults")
        btn_reset.clicked.connect(self._on_reset)
        row2.addWidget(self.btn_up)
        row2.addWidget(self.btn_down)
        row2.addStretch(1)
        row2.addWidget(btn_reset)
        left_layout.addLayout(row2)

        # Right: file-type checklist for the selected category
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)

        self.lbl_types_title = QLabel("File types")
        self.lbl_types_title.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        right_layout.addWidget(self.lbl_types_title)

        self.list_types = QListWidget()
        self.list_types.itemChanged.connect(self._on_type_toggled)
        right_layout.addWidget(self.list_types, 1)

        self.lbl_unassigned = QLabel()
        self.lbl_unassigned.setObjectName("noteLabel")
        self.lbl_unassigned.setWordWrap(True)
        right_layout.addWidget(self.lbl_unassigned)

        body.addWidget(left)
        body.addWidget(right)
        body.setSizes([340, 260])
        layout.addWidget(body, 1)

        self._load_categories(categories)

    # ------------------ tree data ------------------
    def _make_item(
        self,
        entry: dict[str, Any],
        parent: Optional[QTreeWidgetItem] = None,
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem([entry["name"]])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        item.setData(0, EXTENSIONS_ROLE, list(entry.get("extensions", [])))
        item.setData(0, COLOR_ROLE, entry["color"])
        item.setData(0, ICON_ROLE, entry.get("icon"))
        if parent is None:
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
        self.refresh_decoration(item)
        return item

    @staticmethod
    def refresh_decoration(item: QTreeWidgetItem) -> None:
        """Update an item's icon from its color / icon-image data."""
        item.setIcon(0, category_icon(item.data(0, COLOR_ROLE), item.data(0, ICON_ROLE)))

    def _next_color(self, is_main: bool) -> str:
        """Palette color for a newly added category, continuing the sidebar's cycle."""
        mains = self.tree.topLevelItemCount()
        if is_main:
            return MAIN_CATEGORY_COLORS[mains % len(MAIN_CATEGORY_COLORS)]
        subs = sum(self.tree.topLevelItem(i).childCount() for i in range(mains))
        return SUB_CATEGORY_COLORS[subs % len(SUB_CATEGORY_COLORS)]

    def _load_categories(self, categories: list[dict[str, Any]]) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        for main in resolve_category_colors(categories):
            main_item = self._make_item(main)
            self.tree.addTopLevelItem(main_item)
            for sub in main.get("subcategories", []):
                main_item.addChild(self._make_item(sub, main_item))
            main_item.setExpanded(True)
        self.tree.blockSignals(False)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self._on_current_changed(None, None)
        self._update_unassigned()

    def categories(self) -> list[dict[str, Any]]:
        """Return the edited categories, keeping only currently scanned file types."""
        available = set(self._available)

        def entry(item: QTreeWidgetItem) -> dict[str, Any]:
            data: dict[str, Any] = {
                "name": item.text(0).strip(),
                "color": item.data(0, COLOR_ROLE),
                "extensions": [e for e in item.data(0, EXTENSIONS_ROLE) if e in available],
            }
            if item.data(0, ICON_ROLE):
                data["icon"] = item.data(0, ICON_ROLE)
            return data

        result = []
        for i in range(self.tree.topLevelItemCount()):
            main_item = self.tree.topLevelItem(i)
            main = entry(main_item)
            main["subcategories"] = [entry(main_item.child(j)) for j in range(main_item.childCount())]
            result.append(main)
        return result

    def validation_error(self) -> Optional[str]:
        for main in self.categories():
            if not main["name"]:
                return "Every category needs a name."
            if any(not sub["name"] for sub in main["subcategories"]):
                return f"Every subcategory in '{main['name']}' needs a name."
        return None

    def set_available_extensions(self, extensions: list[str]) -> None:
        """Called when the Files page changes so the checklist reflects the scanned types."""
        self._available = list(extensions)
        self._populate_types(self.tree.currentItem())
        self._update_unassigned()

    # ------------------ checklist ------------------
    def _on_current_changed(self, current: Optional[QTreeWidgetItem], _previous) -> None:
        is_item = current is not None
        is_main = is_item and current.parent() is None
        self.btn_add_sub.setEnabled(is_item)
        self.btn_remove.setEnabled(is_item)
        self.btn_up.setEnabled(is_item)
        self.btn_down.setEnabled(is_item)
        self.btn_add_sub.setToolTip("Add a subcategory under the selected main category")
        if is_item:
            kind = "category" if is_main else "subcategory"
            self.lbl_types_title.setText(f"File types shown in {kind} '{current.text(0)}'")
        else:
            self.lbl_types_title.setText("Select a category to choose its file types")
        self._populate_types(current)

    def _populate_types(self, current: Optional[QTreeWidgetItem]) -> None:
        self._updating_checks = True
        self.list_types.clear()
        if current is not None:
            own = set(current.data(0, EXTENSIONS_ROLE))
            inherited: set[str] = set()
            if current.parent() is None:
                for j in range(current.childCount()):
                    inherited.update(current.child(j).data(0, EXTENSIONS_ROLE))

            for ext in self._available:
                item = QListWidgetItem(ext)
                if ext in inherited and ext not in own:
                    # Included via a subcategory; shown checked but not editable here
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    item.setCheckState(Qt.CheckState.Checked)
                    item.setText(f"{ext}   (from subcategory)")
                    item.setForeground(Qt.GlobalColor.gray)
                else:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if ext in own else Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole, ext)
                self.list_types.addItem(item)
        self._updating_checks = False

    def _on_type_toggled(self, item: QListWidgetItem) -> None:
        if self._updating_checks:
            return
        current = self.tree.currentItem()
        if current is None:
            return
        checked = [
            self.list_types.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list_types.count())
            if self.list_types.item(i).flags() & Qt.ItemFlag.ItemIsUserCheckable
            and self.list_types.item(i).checkState() == Qt.CheckState.Checked
        ]
        # Preserve any stored extensions no longer in the Files list (dropped on save)
        stale = [e for e in current.data(0, EXTENSIONS_ROLE) if e not in self._available]
        current.setData(0, EXTENSIONS_ROLE, checked + stale)
        self._update_unassigned()

    def _update_unassigned(self) -> None:
        assigned: set[str] = set()
        for main in self.categories():
            assigned |= category_extensions(main)
        unassigned = [e for e in self._available if e not in assigned]
        if unassigned:
            self.lbl_unassigned.setText(
                "Not in any category (shown under 'Uncategorized'): " + ", ".join(unassigned)
            )
            self.lbl_unassigned.show()
        else:
            self.lbl_unassigned.hide()

    # ------------------ tree editing ------------------
    def _on_item_renamed(self, item: QTreeWidgetItem, _column: int) -> None:
        if item is self.tree.currentItem():
            self._on_current_changed(item, None)

    def _on_add_main(self) -> None:
        item = self._make_item({"name": "New Category", "color": self._next_color(True)})
        self.tree.addTopLevelItem(item)
        self.tree.setCurrentItem(item)
        self.tree.editItem(item, 0)
        self._update_unassigned()

    def _on_add_sub(self) -> None:
        current = self.tree.currentItem()
        if current is None:
            return
        main_item = current if current.parent() is None else current.parent()
        item = self._make_item({"name": "New Subcategory", "color": self._next_color(False)}, main_item)
        main_item.addChild(item)
        main_item.setExpanded(True)
        self.tree.setCurrentItem(item)
        self.tree.editItem(item, 0)

    def _on_remove(self) -> None:
        current = self.tree.currentItem()
        if current is None:
            return
        if current.parent() is None and current.childCount():
            answer = QMessageBox.question(
                self,
                "Remove Category",
                f"Remove '{current.text(0)}' and its {current.childCount()} subcategories?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        parent = current.parent()
        if parent is None:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(current))
        else:
            parent.removeChild(current)
        self._on_current_changed(self.tree.currentItem(), None)
        self._update_unassigned()

    def _move_current(self, delta: int) -> None:
        current = self.tree.currentItem()
        if current is None:
            return
        parent = current.parent()
        if parent is None:
            index = self.tree.indexOfTopLevelItem(current)
            new_index = index + delta
            if not 0 <= new_index < self.tree.topLevelItemCount():
                return
            expanded = current.isExpanded()
            self.tree.takeTopLevelItem(index)
            self.tree.insertTopLevelItem(new_index, current)
            current.setExpanded(expanded)
        else:
            index = parent.indexOfChild(current)
            new_index = index + delta
            if not 0 <= new_index < parent.childCount():
                return
            parent.takeChild(index)
            parent.insertChild(new_index, current)
        self.tree.setCurrentItem(current)

    def _on_reset(self) -> None:
        answer = QMessageBox.question(
            self, "Reset Categories", "Replace all categories with the default layout?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._load_categories(copy.deepcopy(DEFAULT_CATEGORIES))


# ----------------------------------------------------------------------
# General > Appearance
# ----------------------------------------------------------------------
def _theme_swatch(theme_id: str, width: int = 64, height: int = 40) -> QPixmap:
    """Miniature preview of a theme: background, toolbar strip, sidebar and accent."""
    t = THEMES[theme_id]
    pix = QPixmap(width, height)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    if theme_id == "dark_purple":
        background = QLinearGradient(0, 0, width, height)
        background.setColorAt(0.0, QColor("#241338"))
        background.setColorAt(1.0, QColor("#0c0716"))
        p.setBrush(background)
    else:
        p.setBrush(QColor(t["window_bg"]))
    p.setPen(QColor(t["button_border"]))
    p.drawRoundedRect(0, 0, width - 1, height - 1, 6, 6)

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(t["button"]))
    p.drawRoundedRect(4, 4, width - 9, 7, 2, 2)
    p.setBrush(QColor(t["accent"]))
    p.drawRoundedRect(5, 5, 12, 5, 2, 2)
    p.drawRoundedRect(4, 15, 16, 5, 2, 2)
    p.setBrush(QColor(t["thumb_border"]))
    for i in range(3):
        p.drawRoundedRect(25 + i * 12, 15, 9, 9, 2, 2)
        p.drawRoundedRect(25 + i * 12, 27, 9, 9, 2, 2)
    p.end()
    return pix


_PREVIEW_SAMPLES = [
    ("Hero_Character.fbx", ".fbx", "3D Models", "FBX"),
    ("Crate.obj", ".obj", "3D Models", "OBJ"),
    ("Helmet.glb", ".glb", "3D Models", "glTF"),
    ("Brick_Albedo.png", ".png", "Textures", "PNG"),
    ("Style_Guide.pdf", ".pdf", "Documents", "PDF"),
]


class AppearancePage(QWidget):
    """Theme, grid size and category color / icon options."""

    theme_selected = Signal(str)

    def __init__(
        self,
        categories_page: CategoriesPage,
        theme_id: str,
        thumbnail_size: int,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.categories_page = categories_page
        self._theme_id = theme_id
        # Maps mirror tree items to the CategoriesPage items they represent
        self._source_items: dict[int, QTreeWidgetItem] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("scrollContent")
        scroll.setWidget(content)
        outer.addWidget(scroll)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        layout.addWidget(_page_header("Appearance", "Customize how the asset manager looks."))

        # --- Theme ---
        layout.addWidget(self._section_title("Theme"))
        theme_row = QHBoxLayout()
        self.theme_group = QButtonGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_buttons: dict[str, QPushButton] = {}
        for tid, tokens in THEMES.items():
            btn = QPushButton(tokens["label"])
            btn.setObjectName("themeCard")
            btn.setCheckable(True)
            btn.setIcon(QIcon(_theme_swatch(tid)))
            btn.setIconSize(QSize(64, 40))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, t=tid: self._on_theme_clicked(t))
            self.theme_group.addButton(btn)
            self.theme_buttons[tid] = btn
            theme_row.addWidget(btn)
        theme_row.addStretch(1)
        self.theme_buttons.get(theme_id, self.theme_buttons["dark"]).setChecked(True)
        layout.addLayout(theme_row)
        hint = QLabel("Themes apply instantly so you can preview them; Cancel restores the previous theme.")
        hint.setObjectName("pageHint")
        layout.addWidget(hint)
        layout.addSpacing(14)

        # --- Grid size ---
        layout.addWidget(self._section_title("Grid Size"))
        size_row = QHBoxLayout()
        lbl_small = QLabel("Small")
        lbl_small.setObjectName("pageHint")
        self.slider_size = QSlider(Qt.Orientation.Horizontal)
        lo, hi = THUMBNAIL_SIZE_RANGE
        self.slider_size.setRange(lo, hi)
        self.slider_size.setSingleStep(8)
        self.slider_size.setPageStep(32)
        self.slider_size.setValue(thumbnail_size)
        self.slider_size.valueChanged.connect(self._on_size_changed)
        lbl_large = QLabel("Large")
        lbl_large.setObjectName("pageHint")
        self.lbl_size = QLabel()
        self.lbl_size.setFixedWidth(60)
        self.lbl_size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        size_row.addWidget(lbl_small)
        size_row.addWidget(self.slider_size, 1)
        size_row.addWidget(lbl_large)
        size_row.addWidget(self.lbl_size)
        layout.addLayout(size_row)

        self.grid_preview = QListWidget()
        self.grid_preview.setObjectName("assetGrid")
        self.grid_preview.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid_preview.setMovement(QListWidget.Movement.Static)
        self.grid_preview.setFlow(QListWidget.Flow.LeftToRight)
        self.grid_preview.setWrapping(False)
        self.grid_preview.setWordWrap(True)
        self.grid_preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.grid_preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.grid_preview.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._preview_assets = [
            AssetItem(Path(name), name, ext, 0, 0.0, group, cat) for name, ext, group, cat in _PREVIEW_SAMPLES
        ]
        layout.addWidget(self.grid_preview)
        layout.addSpacing(14)

        # --- Category colors & icons ---
        layout.addWidget(self._section_title("Category Colors & Icons"))
        hint = QLabel(
            "Pick a color for each category's sidebar badge, or use a small image as its icon "
            "(PNG, JPG, SVG, ICO; up to 1 MB). Double-click a row to change its color."
        )
        hint.setObjectName("pageHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        cat_row = QHBoxLayout()
        self.color_tree = QTreeWidget()
        self.color_tree.setObjectName("categoryTree")
        self.color_tree.setHeaderHidden(True)
        self.color_tree.setMinimumHeight(260)
        self.color_tree.setIconSize(QSize(16, 16))
        self.color_tree.currentItemChanged.connect(lambda *_: self._update_category_buttons())
        self.color_tree.itemDoubleClicked.connect(lambda *_: self._on_choose_color())
        cat_row.addWidget(self.color_tree, 1)

        cat_buttons = QVBoxLayout()
        self.btn_color = QPushButton("Change Color...")
        self.btn_color.clicked.connect(self._on_choose_color)
        self.btn_icon = QPushButton("Set Icon Image...")
        self.btn_icon.clicked.connect(self._on_choose_icon)
        self.btn_clear_icon = QPushButton("Remove Icon")
        self.btn_clear_icon.clicked.connect(self._on_clear_icon)
        for btn in (self.btn_color, self.btn_icon, self.btn_clear_icon):
            cat_buttons.addWidget(btn)
        cat_buttons.addStretch(1)
        cat_row.addLayout(cat_buttons)
        layout.addLayout(cat_row)
        layout.addStretch(1)

        self._on_size_changed(thumbnail_size)
        self._update_category_buttons()

    @staticmethod
    def _section_title(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("sectionTitle")
        return lbl

    # ------------------ theme & grid ------------------
    def theme_id(self) -> str:
        return self._theme_id

    def thumbnail_size(self) -> int:
        return self.slider_size.value()

    def _on_theme_clicked(self, theme_id: str) -> None:
        if theme_id != self._theme_id:
            self._theme_id = theme_id
            self.theme_selected.emit(theme_id)

    def refresh_preview(self) -> None:
        """Re-render the sample cards (after a size or theme change)."""
        size = self.slider_size.value()
        self.grid_preview.clear()
        self.grid_preview.setIconSize(QSize(size, size))
        self.grid_preview.setGridSize(QSize(size + 35, size + 50))
        self.grid_preview.setFixedHeight(size + 50 + 30)
        for asset in self._preview_assets:
            item = QListWidgetItem(QIcon(QPixmap.fromImage(render_placeholder_image(asset, size))), asset.name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            self.grid_preview.addItem(item)

    def _on_size_changed(self, value: int) -> None:
        # Snap to multiples of 8 so the slider lands on tidy sizes
        snapped = int(round(value / 8.0) * 8)
        if snapped != value:
            self.slider_size.setValue(snapped)
            return
        self.lbl_size.setText(f"{value} px")
        self.refresh_preview()

    # ------------------ category colors & icons ------------------
    def showEvent(self, event) -> None:
        # Categories may have been edited on the Categories page since last shown
        self.refresh_categories()
        super().showEvent(event)

    def refresh_categories(self) -> None:
        source_tree = self.categories_page.tree
        current = self._source_items.get(id(self.color_tree.currentItem())) if self.color_tree.currentItem() else None
        self.color_tree.clear()
        self._source_items.clear()
        restore: Optional[QTreeWidgetItem] = None

        def mirror(src: QTreeWidgetItem, parent: Optional[QTreeWidgetItem]) -> QTreeWidgetItem:
            nonlocal restore
            item = QTreeWidgetItem([src.text(0)])
            item.setIcon(0, src.icon(0))
            item.setFont(0, src.font(0))
            if parent is None:
                self.color_tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            self._source_items[id(item)] = src
            if src is current:
                restore = item
            return item

        for i in range(source_tree.topLevelItemCount()):
            src_main = source_tree.topLevelItem(i)
            main_item = mirror(src_main, None)
            for j in range(src_main.childCount()):
                mirror(src_main.child(j), main_item)
            main_item.setExpanded(True)

        if restore is not None:
            self.color_tree.setCurrentItem(restore)
        self._update_category_buttons()

    def _current_source(self) -> tuple[Optional[QTreeWidgetItem], Optional[QTreeWidgetItem]]:
        item = self.color_tree.currentItem()
        if item is None:
            return None, None
        return item, self._source_items.get(id(item))

    def _update_category_buttons(self) -> None:
        item, src = self._current_source()
        has_item = src is not None
        self.btn_color.setEnabled(has_item)
        self.btn_icon.setEnabled(has_item)
        self.btn_clear_icon.setEnabled(has_item and bool(src.data(0, ICON_ROLE)))

    def _apply_change(self, item: QTreeWidgetItem, src: QTreeWidgetItem) -> None:
        self.categories_page.refresh_decoration(src)
        item.setIcon(0, src.icon(0))
        self._update_category_buttons()

    def _on_choose_color(self) -> None:
        item, src = self._current_source()
        if src is None:
            return
        color = QColorDialog.getColor(QColor(src.data(0, COLOR_ROLE)), self, f"Color for '{src.text(0)}'")
        if color.isValid():
            src.setData(0, COLOR_ROLE, color.name())
            self._apply_change(item, src)

    def _on_choose_icon(self) -> None:
        item, src = self._current_source()
        if src is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Icon Image for '{src.text(0)}'",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.svg *.ico *.bmp *.webp)",
        )
        if not path:
            return
        if Path(path).stat().st_size > MAX_CATEGORY_ICON_BYTES:
            QMessageBox.warning(
                self, "Icon Too Large",
                "Please choose an image of 1 MB or less. "
                "Icons are shown at 16 px, so a small PNG works best.",
            )
            return
        if load_icon_image(path) is None:
            QMessageBox.warning(self, "Unsupported Image", "That file couldn't be read as an image.")
            return
        try:
            copied = import_category_icon(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Icon Error", f"Couldn't use that image: {exc}")
            return
        src.setData(0, ICON_ROLE, str(copied))
        self._apply_change(item, src)

    def _on_clear_icon(self) -> None:
        item, src = self._current_source()
        if src is not None:
            src.setData(0, ICON_ROLE, None)
            self._apply_change(item, src)


# ----------------------------------------------------------------------
# General > Video
# ----------------------------------------------------------------------
class VideoPage(QWidget):
    """ffmpeg location, playback backend, and the proxy / poster-frame cache."""

    _BACKEND_LABELS = {
        "auto": "Automatic (FFmpeg, Qt's default)",
        "ffmpeg": "FFmpeg",
        "windows": "Windows Media Foundation",
    }

    def __init__(self, ffmpeg_path: str, media_backend: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)
        layout.addWidget(_page_header(
            "Video",
            "Videos play inside the viewer. ffmpeg (a free external program) is used for grid "
            "thumbnails and for 720p preview copies of files that won't play or that stutter.",
        ))

        layout.addWidget(self._section_label("ffmpeg location"))
        row = QHBoxLayout()
        self.txt_ffmpeg = QLineEdit(ffmpeg_path)
        self.txt_ffmpeg.setPlaceholderText("Leave blank to use ffmpeg from the system PATH")
        self.txt_ffmpeg.textChanged.connect(self._update_ffmpeg_status)
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._on_browse)
        row.addWidget(self.txt_ffmpeg, 1)
        row.addWidget(btn_browse)
        layout.addLayout(row)
        self.lbl_ffmpeg_status = QLabel()
        self.lbl_ffmpeg_status.setObjectName("pageHint")
        self.lbl_ffmpeg_status.setWordWrap(True)
        self.lbl_ffmpeg_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.lbl_ffmpeg_status)

        layout.addSpacing(10)
        layout.addWidget(self._section_label("Playback backend"))
        self.cb_backend = QComboBox()
        for backend_id in MEDIA_BACKENDS:
            self.cb_backend.addItem(self._BACKEND_LABELS[backend_id], backend_id)
        self.cb_backend.setCurrentIndex(max(0, self.cb_backend.findData(media_backend)))
        layout.addWidget(self.cb_backend, 0, Qt.AlignmentFlag.AlignLeft)
        backend_hint = "Try Windows Media Foundation if videos play incorrectly. Takes effect after restarting the app."
        env_backend = os.environ.get("QT_MEDIA_BACKEND")
        if env_backend and env_backend != media_backend:
            backend_hint += f" (The QT_MEDIA_BACKEND environment variable currently forces \"{env_backend}\".)"
        lbl_backend = QLabel(backend_hint)
        lbl_backend.setObjectName("pageHint")
        lbl_backend.setWordWrap(True)
        layout.addWidget(lbl_backend)

        layout.addSpacing(10)
        layout.addWidget(self._section_label("Preview cache"))
        self.lbl_cache = QLabel()
        self.lbl_cache.setObjectName("pageHint")
        self.lbl_cache.setWordWrap(True)
        layout.addWidget(self.lbl_cache)
        cache_row = QHBoxLayout()
        btn_open_cache = QPushButton("Open Cache Folder")
        btn_open_cache.clicked.connect(self._on_open_cache)
        btn_clear_cache = QPushButton("Clear Cache")
        btn_clear_cache.clicked.connect(self._on_clear_cache)
        cache_row.addWidget(btn_open_cache)
        cache_row.addWidget(btn_clear_cache)
        cache_row.addStretch(1)
        layout.addLayout(cache_row)
        layout.addStretch(1)

        self._update_ffmpeg_status()
        self._update_cache_label()

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        return label

    def ffmpeg_path(self) -> str:
        return self.txt_ffmpeg.text().strip()

    def media_backend(self) -> str:
        return self.cb_backend.currentData()

    def _on_browse(self) -> None:
        start = self.ffmpeg_path() or str(Path.home())
        chosen, _ = QFileDialog.getOpenFileName(self, "Locate ffmpeg.exe", start, "ffmpeg (ffmpeg.exe);;Programs (*.exe)")
        if chosen:
            self.txt_ffmpeg.setText(str(Path(chosen)))

    def _update_ffmpeg_status(self) -> None:
        found = find_ffmpeg(self.ffmpeg_path())
        if found:
            self.lbl_ffmpeg_status.setText(f"✔ Using {found}")
        elif self.ffmpeg_path():
            self.lbl_ffmpeg_status.setText("✖ No ffmpeg.exe at that location.")
        else:
            self.lbl_ffmpeg_status.setText(
                "✖ ffmpeg isn't on the system PATH. Install it (for example: winget install ffmpeg) "
                "or browse to ffmpeg.exe. Videos still play; only thumbnails and preview copies need it."
            )

    def _update_cache_label(self) -> None:
        size_mb = cache_size_bytes() / (1024 * 1024)
        self.lbl_cache.setText(f"{size_mb:.1f} MB of preview copies and thumbnails in {CACHE_ROOT}")

    def _on_open_cache(self) -> None:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(CACHE_ROOT)))

    def _on_clear_cache(self) -> None:
        clear_video_cache()
        self._update_cache_label()


# ----------------------------------------------------------------------
# General > Shortcuts
# ----------------------------------------------------------------------
class ShortcutsPage(QWidget):
    """Read-only list of every shortcut, grouped by where it's used (built from ui.shortcuts)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(_page_header(
            "Shortcuts",
            "Keyboard shortcuts and mouse controls, grouped by where they work.",
        ))

        self.txt_filter = QLineEdit()
        self.txt_filter.setPlaceholderText("Filter shortcuts...")
        self.txt_filter.setClearButtonEnabled(True)
        self.txt_filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self.txt_filter)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Shortcut", "Action"])
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(12)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bold = QFont("Segoe UI", 9, QFont.Weight.Bold)
        for category, items in shortcuts.by_category():
            if not items:
                continue
            root = QTreeWidgetItem(self.tree, [category, ""])
            root.setFont(0, bold)
            root.setFirstColumnSpanned(True)
            for shortcut in items:
                row = QTreeWidgetItem(root, [shortcut.keys, shortcut.action])
                row.setFont(0, bold)
        self.tree.expandAll()
        for root_index in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(root_index).setFirstColumnSpanned(True)
        self.tree.resizeColumnToContents(0)
        self.tree.setColumnWidth(0, self.tree.columnWidth(0) + 24)
        layout.addWidget(self.tree, 1)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for root_index in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(root_index)
            category_hit = needle in root.text(0).casefold()
            visible = 0
            for i in range(root.childCount()):
                row = root.child(i)
                hit = not needle or category_hit or needle in row.text(0).casefold() or needle in row.text(1).casefold()
                row.setHidden(not hit)
                visible += hit
            root.setHidden(visible == 0)


# ----------------------------------------------------------------------
# General > Tags
# ----------------------------------------------------------------------
class TagsPage(QWidget):
    """Create, rename, recolor and delete custom tags. Changes are saved immediately."""

    def __init__(self, repo: TagRepository, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.repo = repo
        self.changed = False  # set once anything was edited, so the main window can refresh

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(_page_header(
            "Tags",
            "Custom tags let you group files however you like. Tag files from the grid "
            "(right-click > Tag..., or Ctrl+T) and search with tag:name. "
            "File type tags are added automatically and can't be edited. Changes here are saved immediately.",
        ))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Tag", "Files"])
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(12)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, self.tree.header().ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, self.tree.header().ResizeMode.ResizeToContents)
        self.tree.currentItemChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self._on_rename())
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        self.btn_new = QPushButton("+ New Tag...")
        self.btn_new.setObjectName("primaryBtn")
        self.btn_new.clicked.connect(self._on_new)
        self.btn_rename = QPushButton("Rename...")
        self.btn_rename.clicked.connect(self._on_rename)
        self.btn_color = QPushButton("Change Color...")
        self.btn_color.clicked.connect(self._on_color)
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.clicked.connect(self._on_delete)
        for btn in (self.btn_new, self.btn_rename, self.btn_color, self.btn_delete):
            buttons.addWidget(btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self, select_id: Optional[int] = None) -> None:
        self.tree.clear()
        tags = self.repo.list_tags()
        custom_root = QTreeWidgetItem(self.tree, ["Custom Tags", ""])
        default_root = QTreeWidgetItem(self.tree, ["File Type Tags (automatic)", ""])
        for root in (custom_root, default_root):
            root.setFont(0, QFont("Segoe UI", 9, QFont.Weight.Bold))
            root.setFlags(Qt.ItemFlag.ItemIsEnabled)
        selected = None
        for tag in tags:
            item = QTreeWidgetItem(default_root if tag.is_default else custom_root, [tag.name, str(tag.file_count)])
            item.setData(0, Qt.ItemDataRole.UserRole, tag)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if tag.is_default:
                item.setForeground(0, self.palette().color(self.palette().ColorRole.PlaceholderText))
                item.setToolTip(0, "Added automatically from the file extension; can't be edited")
            else:
                item.setIcon(0, color_swatch_icon(tag.color or "#9ca3af", 14, 14))
            if tag.id == select_id:
                selected = item
        if not custom_root.childCount():
            hint = QTreeWidgetItem(custom_root, ["No custom tags yet. Click + New Tag... to make one.", ""])
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
        self.tree.expandAll()
        if selected is not None:
            self.tree.setCurrentItem(selected)
        self._update_buttons()

    def _current_custom(self) -> Optional[Tag]:
        item = self.tree.currentItem()
        tag = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return tag if tag is not None and not tag.is_default else None

    def _update_buttons(self, *_args) -> None:
        enabled = self._current_custom() is not None
        for btn in (self.btn_rename, self.btn_color, self.btn_delete):
            btn.setEnabled(enabled)

    def _ask_name(self, title: str, initial: str = "") -> Optional[str]:
        name, ok = QInputDialog.getText(self, title, f"Tag name (up to {MAX_TAG_NAME_LENGTH} characters):", text=initial)
        return name if ok else None

    def _on_new(self) -> None:
        name = self._ask_name("New Tag")
        while name is not None:
            try:
                tag = self.repo.create_custom_tag(name, next_tag_color(self.repo.list_tags(kind="custom")))
            except TagError as exc:
                QMessageBox.warning(self, "Can't Create Tag", str(exc))
                name = self._ask_name("New Tag", name)
                continue
            self.changed = True
            self.refresh(select_id=tag.id)
            return

    def _on_rename(self) -> None:
        tag = self._current_custom()
        if tag is None:
            return
        name = self._ask_name("Rename Tag", tag.name)
        while name is not None and name.strip() != tag.name:
            try:
                self.repo.rename_custom_tag(tag.id, name)
            except TagError as exc:
                QMessageBox.warning(self, "Can't Rename Tag", str(exc))
                name = self._ask_name("Rename Tag", name)
                continue
            self.changed = True
            self.refresh(select_id=tag.id)
            return

    def _on_color(self) -> None:
        tag = self._current_custom()
        if tag is None:
            return
        color = QColorDialog.getColor(QColor(tag.color or "#9ca3af"), self, f"Color for {tag.name}")
        if color.isValid():
            self.repo.set_tag_color(tag.id, color.name())
            self.changed = True
            self.refresh(select_id=tag.id)

    def _on_delete(self) -> None:
        tag = self._current_custom()
        if tag is None:
            return
        files = f"{tag.file_count} file{'s' if tag.file_count != 1 else ''}"
        answer = QMessageBox.question(
            self, "Delete Tag",
            f'Delete the tag "{tag.name}"?\n\nIt will be removed from {files}. The files themselves are not affected.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.repo.delete_custom_tag(tag.id)
            self.changed = True
            self.refresh()


# ----------------------------------------------------------------------
# About
# ----------------------------------------------------------------------
class _UpdateWorker(QObject):
    """Runs an update check or install off the UI thread."""

    checked = Signal(object)  # UpdateInfo
    installed = Signal(bool)  # requirements changed
    progress = Signal(str)
    failed = Signal(str)

    def __init__(self, info: Optional[UpdateInfo] = None):
        super().__init__()
        self.info = info

    def check(self) -> None:
        try:
            self.checked.emit(check_for_update())
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # pylint: disable=broad-except
            self.failed.emit(f"Unexpected error while checking for updates: {exc}")

    def install(self) -> None:
        try:
            self.installed.emit(install_update(self.info, self.progress.emit))
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # pylint: disable=broad-except
            self.failed.emit(f"Unexpected error while installing the update: {exc}")


class AboutPage(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[_UpdateWorker] = None
        self._progress_dialog: Optional[QProgressDialog] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)

        title = QLabel(APP_NAME)
        title.setObjectName("pageTitle")
        title.setStyleSheet("font-size: 22px;")
        layout.addWidget(title)

        version = QLabel(f"Version {APP_VERSION}")
        version.setObjectName("pageHint")
        layout.addWidget(version)
        layout.addSpacing(12)

        description = QLabel(
            "Asset Manager is a desktop library for the files behind your creative work. "
            "Point it at your asset folders and it gathers 3D models, textures, images, "
            "alphas and reference documents into one searchable, thumbnail-based library, "
            "organized by the categories you define."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        layout.addSpacing(6)

        description2 = QLabel(
            "Preview FBX, OBJ, glTF and other models in a hardware-accelerated 3D viewer, inspect "
            "textures including 16-bit TIFF and PSD files, and read PDFs, all without launching "
            "Blender, Maya, ZBrush, Photoshop or other heavy creative software."
        )
        description2.setWordWrap(True)
        layout.addWidget(description2)
        layout.addSpacing(10)

        link = QLabel(f'<a href="{GITHUB_URL}">{GITHUB_URL.replace("https://", "")}</a>')
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(link)
        layout.addSpacing(16)

        row = QHBoxLayout()
        self.btn_update = QPushButton("Check for Updates")
        self.btn_update.setObjectName("primaryBtn")
        self.btn_update.clicked.connect(self._on_check_updates)
        row.addWidget(self.btn_update)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addSpacing(8)

        self.lbl_update_status = QLabel()
        self.lbl_update_status.setObjectName("pageHint")
        self.lbl_update_status.setWordWrap(True)
        self.lbl_update_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.lbl_update_status)
        layout.addStretch(1)

    # ------------------ background work ------------------
    def _run_in_thread(self, worker: _UpdateWorker, start: Callable[[], None]) -> None:
        self._worker = worker
        self._thread = QThread(self)
        worker.moveToThread(self._thread)
        self._thread.started.connect(start)
        for signal in (worker.checked, worker.installed, worker.failed):
            signal.connect(self._thread.quit)
        self._thread.start()

    def is_busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _set_status(self, text: str, style: str = "pageHint") -> None:
        self.lbl_update_status.setObjectName(style)
        self.lbl_update_status.setText(text)
        self.lbl_update_status.style().unpolish(self.lbl_update_status)
        self.lbl_update_status.style().polish(self.lbl_update_status)

    # ------------------ check ------------------
    def _on_check_updates(self) -> None:
        if self.is_busy():
            return
        self.btn_update.setEnabled(False)
        self.btn_update.setText("Checking...")
        self._set_status("Contacting GitHub...")
        worker = _UpdateWorker()
        worker.checked.connect(self._on_checked)
        worker.failed.connect(self._on_check_failed)
        self._run_in_thread(worker, worker.check)

    def _reset_button(self) -> None:
        self.btn_update.setEnabled(True)
        self.btn_update.setText("Check for Updates")

    def _on_check_failed(self, message: str) -> None:
        self._reset_button()
        self._set_status(message, "warningLabel")

    def _on_checked(self, info: UpdateInfo) -> None:
        self._reset_button()
        if not info.is_newer:
            self._set_status(f"You're up to date. Version {APP_VERSION} is the latest version.")
            return

        self._set_status(f"Version {info.latest_version} is available (you have {APP_VERSION}).", "noteLabel")
        box = QMessageBox(self)
        box.setWindowTitle("Update Available")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"<b>{APP_NAME} {info.latest_version}</b> is available.<br>You're running {APP_VERSION}.")
        details = "Your current files will be backed up before the update is installed."
        if info.notes:
            details = f"What's new:\n{info.notes[:1200]}\n\n{details}"
        box.setInformativeText(details)
        btn_install = box.addButton("Download && Install", QMessageBox.ButtonRole.AcceptRole)
        btn_view = box.addButton("View on GitHub", QMessageBox.ButtonRole.HelpRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_install)
        box.exec()

        if box.clickedButton() is btn_install:
            self._start_install(info)
        elif box.clickedButton() is btn_view:
            QDesktopServices.openUrl(QUrl(info.page_url))

    # ------------------ install ------------------
    def _start_install(self, info: UpdateInfo) -> None:
        self.btn_update.setEnabled(False)
        self._progress_dialog = QProgressDialog("Preparing update...", "", 0, 0, self)
        self._progress_dialog.setWindowTitle("Installing Update")
        self._progress_dialog.setCancelButton(None)  # file copying can't be safely interrupted
        self._progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress_dialog.setMinimumDuration(0)
        self._progress_dialog.setMinimumWidth(360)
        self._progress_dialog.show()

        worker = _UpdateWorker(info)
        worker.progress.connect(self._progress_dialog.setLabelText)
        worker.installed.connect(lambda changed: self._on_installed(info, changed))
        worker.failed.connect(self._on_install_failed)
        self._run_in_thread(worker, worker.install)

    def _close_progress(self) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
            self._progress_dialog = None

    def _on_install_failed(self, message: str) -> None:
        self._close_progress()
        self._reset_button()
        self._set_status(message, "warningLabel")
        QMessageBox.warning(self, "Update Failed", message)

    def _on_installed(self, info: UpdateInfo, requirements_changed: bool) -> None:
        self._close_progress()
        self.btn_update.setText("Restart to Finish Updating")
        self.btn_update.setEnabled(True)
        self.btn_update.clicked.disconnect()
        self.btn_update.clicked.connect(self._restart)
        extra = " New Python packages were installed as well." if requirements_changed else ""
        self._set_status(
            f"Version {info.latest_version} was installed.{extra} Restart the app to start using it. "
            + backup_description(),
            "noteLabel",
        )
        answer = QMessageBox.question(
            self,
            "Update Installed",
            f"{APP_NAME} {info.latest_version} was installed.{extra}\n\nRestart now to start using it?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._restart()

    def _restart(self) -> None:
        restart_application()
        QApplication.closeAllWindows()
        QApplication.quit()


# ----------------------------------------------------------------------
# Dialog
# ----------------------------------------------------------------------
class SettingsDialog(QDialog):
    """Application settings. Saves to the config file when the user clicks Save."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("settingsDialog")
        self.setWindowTitle("Settings")
        self.resize(900, 600)
        self.setMinimumSize(760, 520)

        self._orig_folders = get_scan_folders()
        self._orig_extensions = get_file_extensions()
        self._orig_categories = get_categories()
        self._orig_theme = get_theme()
        self._orig_thumbnail_size = get_thumbnail_size()
        self._orig_ffmpeg_path = get_ffmpeg_path()
        self._orig_media_backend = get_media_backend()

        # Set after Save so the main window knows what to refresh
        self.folders_changed = False
        self.extensions_changed = False
        self.categories_changed = False
        self.theme_changed = False
        self.thumbnail_size_changed = False

        self.page_folders = FoldersPage(self._orig_folders)
        self.page_files = FilesPage(self._orig_extensions)
        self.page_categories = CategoriesPage(self._orig_categories, self._orig_extensions)
        self.page_appearance = AppearancePage(self.page_categories, self._orig_theme, self._orig_thumbnail_size)
        self.page_appearance.theme_selected.connect(self._on_theme_selected)
        self.page_video = VideoPage(self._orig_ffmpeg_path, self._orig_media_backend)
        self.page_tags = TagsPage(get_repository())
        self.page_shortcuts = ShortcutsPage()
        self.page_about = AboutPage()

        self.page_files.validity_changed.connect(self._on_files_validity_changed)
        self.page_files.txt_extensions.textChanged.connect(self._sync_category_extensions)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        body = QHBoxLayout()
        body.setSpacing(0)

        self.nav = QTreeWidget()
        self.nav.setObjectName("settingsNav")
        self.nav.setHeaderHidden(True)
        self.nav.setFixedWidth(200)
        self.nav.setIndentation(14)

        self.stack = QStackedWidget()
        self._nav_pages: dict[int, QWidget] = {}

        general = QTreeWidgetItem(self.nav, ["General"])
        general.setFont(0, QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.nav_items: dict[str, QTreeWidgetItem] = {}
        for name, page in (
            ("Folders", self.page_folders),
            ("Files", self.page_files),
            ("Categories", self.page_categories),
            ("Appearance", self.page_appearance),
            ("Video", self.page_video),
            ("Tags", self.page_tags),
            ("Shortcuts", self.page_shortcuts),
        ):
            item = QTreeWidgetItem(general, [name])
            item.setData(0, Qt.ItemDataRole.UserRole, self.stack.addWidget(page))
            self.nav_items[name] = item
        general.setData(0, Qt.ItemDataRole.UserRole, 0)  # General opens its first page
        general.setExpanded(True)

        about = QTreeWidgetItem(self.nav, ["About"])
        about.setFont(0, QFont("Segoe UI", 10, QFont.Weight.Bold))
        about.setData(0, Qt.ItemDataRole.UserRole, self.stack.addWidget(self.page_about))
        self.nav_items["About"] = about

        self.nav.currentItemChanged.connect(self._on_nav_changed)
        self.nav.setCurrentItem(self.nav_items["Folders"])

        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        # Footer buttons
        footer = QFrame()
        footer.setObjectName("settingsFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 10)
        self.lbl_footer_warning = QLabel("Fix the file type format on the Files page before saving.")
        self.lbl_footer_warning.setObjectName("footerWarning")
        self.lbl_footer_warning.hide()
        footer_layout.addWidget(self.lbl_footer_warning)
        footer_layout.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        self.btn_save = QPushButton("Save")
        self.btn_save.setObjectName("primaryBtn")
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self._on_save)
        footer_layout.addWidget(btn_cancel)
        footer_layout.addWidget(self.btn_save)
        root.addWidget(footer)

    def show_page(self, name: str) -> None:
        if name in self.nav_items:
            self.nav.setCurrentItem(self.nav_items[name])

    def _on_nav_changed(self, current: Optional[QTreeWidgetItem], _previous) -> None:
        if current is not None:
            self.stack.setCurrentIndex(current.data(0, Qt.ItemDataRole.UserRole))

    def _on_files_validity_changed(self, valid: bool) -> None:
        self.btn_save.setEnabled(valid)
        self.lbl_footer_warning.setVisible(not valid)

    def _on_theme_selected(self, theme_id: str) -> None:
        # Live preview; reverted in reject() if the dialog is cancelled
        apply_theme(theme_id)
        self.page_appearance.refresh_preview()

    def reject(self) -> None:
        if self.page_about.is_busy():
            return
        if get_theme() != self.page_appearance.theme_id():
            apply_theme(get_theme())
        # Discard icon images imported during this session
        remove_unused_category_icons(get_categories())
        super().reject()

    def _sync_category_extensions(self) -> None:
        if self.page_files.is_valid():
            self.page_categories.set_available_extensions(self.page_files.extensions())

    def _on_save(self) -> None:
        if self.page_about.is_busy():
            return
        if not self.page_files.is_valid():
            self.show_page("Files")
            return
        error = self.page_categories.validation_error()
        if error:
            self.show_page("Categories")
            QMessageBox.warning(self, "Categories", error)
            return

        folders = self.page_folders.folders()
        extensions = self.page_files.extensions()
        categories = self.page_categories.categories()

        theme_id = self.page_appearance.theme_id()
        thumbnail_size = self.page_appearance.thumbnail_size()

        self.folders_changed = [str(f) for f in folders] != [str(f) for f in self._orig_folders]
        self.extensions_changed = extensions != self._orig_extensions
        self.categories_changed = categories != self._orig_categories
        self.theme_changed = theme_id != self._orig_theme
        self.thumbnail_size_changed = thumbnail_size != self._orig_thumbnail_size

        if self.folders_changed:
            set_scan_folders(folders)
        if self.extensions_changed:
            set_file_extensions(extensions)
        if self.categories_changed or self.extensions_changed:
            # Re-save categories so file types removed on the Files page are dropped
            set_categories(categories)
        remove_unused_category_icons(categories)
        if self.theme_changed:
            set_theme(theme_id)
        if self.thumbnail_size_changed:
            set_thumbnail_size(thumbnail_size)
        if self.page_video.ffmpeg_path() != self._orig_ffmpeg_path:
            set_ffmpeg_path(self.page_video.ffmpeg_path())
        if self.page_video.media_backend() != self._orig_media_backend:
            set_media_backend(self.page_video.media_backend())
            QMessageBox.information(
                self, "Restart Required", "The new video playback backend will be used after you restart the app."
            )

        self.accept()
