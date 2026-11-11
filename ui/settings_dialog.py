"""Settings dialog for the Asset Manager.

Pages:
- General > Folders:     library folders scanned for assets
- General > Files:       file extensions the scanner picks up
- General > Categories:  sidebar main categories / subcategories and their file types and tags
- General > Profiles:    make, rename, duplicate and delete profiles (core.profiles); Folders, Files and
                         Categories edit the profile in use
- General > Appearance:  theme, grid size, category colors & icons
- General > Video:       ffmpeg location, playback backend, preview cache
- General > Archives:    files inside archives in the library, 7-Zip's location, unpacked copies
- General > Apps:        the apps files open in (Open In), found by themselves or picked
- General > Artbook:     where the Artbook's pages are kept (changed right away, not on Save)
- General > Tags:        custom tags (create, rename, recolor, delete)
- General > Shortcuts:   every keyboard shortcut / mouse control (from ui.shortcuts)
- Guide:                 the Artbook guide and the Read Me (docs/artbook-guide.md, README.md), shown here
- About:                 application description, version, GitHub update check
"""

from __future__ import annotations

import copy
import html
import os
import re
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import shiboken6
from PySide6.QtCore import QObject, QSize, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont, QIcon, QLinearGradient, QPainter, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QTextBrowser,
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

from core import app_helpers, apps, archives, artbook_store, crash_log
from core.scanner import AssetItem
from core import category_rules, profiles, storage
from core.profiles import Profile, ProfileError
from core.settings import (
    DEFAULT_CATEGORIES,
    DEFAULT_FILE_EXTENSIONS,
    MAX_CATEGORY_ICON_BYTES,
    MEDIA_BACKENDS,
    PREVIEWABLE_EXTENSIONS,
    THUMBNAIL_SIZE_RANGE,
    category_extensions,
    get_app_choices,
    get_app_types,
    get_archive_contents,
    get_background_texture,
    get_categories,
    get_custom_apps,
    get_found_apps,
    get_glass_frost,
    get_ffmpeg_path,
    get_file_extensions,
    get_media_backend,
    get_motion,
    get_scan_folders,
    get_seven_zip_path,
    get_unpacked_limit_gb,
    get_theme,
    get_model_view,
    get_thumbnail_size,
    import_category_icon,
    parse_extension_list,
    remove_unused_category_icons,
    set_app_choices,
    set_app_types,
    set_archive_contents,
    set_background_texture,
    set_categories,
    set_custom_apps,
    set_found_apps,
    set_glass_frost,
    set_ffmpeg_path,
    set_file_extensions,
    set_media_backend,
    set_pages_dir,
    set_motion,
    set_scan_folders,
    set_seven_zip_path,
    set_unpacked_limit_gb,
    set_theme,
    set_model_view,
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
from core.video_tools import find_ffmpeg
from ui.animations import set_motion_mode
from ui.backgrounds import TEXTURES, render_tile
from ui.duotone import IconLabel, set_icon
from ui.glass import set_frost
from ui.icons import (
    MAIN_CATEGORY_COLORS,
    SUB_CATEGORY_COLORS,
    category_icon,
    color_swatch_icon,
    load_icon_image,
    resolve_category_colors,
)
from ui import shortcuts
from ui.tag_widgets import next_tag_color, run_job
from ui.themes import THEMES, apply_theme_soon, current_theme_id, parse_color, theme_color

# Data roles on category tree items
EXTENSIONS_ROLE = Qt.ItemDataRole.UserRole
COLOR_ROLE = Qt.ItemDataRole.UserRole + 1
ICON_ROLE = Qt.ItemDataRole.UserRole + 2
TAGS_ROLE = Qt.ItemDataRole.UserRole + 3  # tag names (core.category_rules)
MATCH_ALL_ROLE = Qt.ItemDataRole.UserRole + 4  # True: files need every tag; False: any of them


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
        self.btn_add.setToolTip("Add a folder to the library; it's scanned with all its subfolders")
        self.btn_add.setObjectName("primaryBtn")
        self.btn_add.clicked.connect(self._on_add)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setToolTip("Remove the selected folder from the library (the files themselves aren't touched)")
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

        self.lbl_warning = IconLabel(box_name="warningLabel")
        self.lbl_warning.set_icon("warning", "danger")
        layout.addWidget(self.lbl_warning)

        self.lbl_note = QLabel()
        self.lbl_note.setObjectName("noteLabel")
        self.lbl_note.setWordWrap(True)
        layout.addWidget(self.lbl_note)

        buttons = QHBoxLayout()
        btn_reset = QPushButton("Reset to Defaults")
        btn_reset.setToolTip("Restore the default list of file types")
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
            self.lbl_warning.setText("Invalid format:\n• " + "\n• ".join(shown) + more)
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
    """Edit sidebar main categories, subcategories, and the file types and tags each shows."""

    def __init__(
        self,
        categories: list[dict[str, Any]],
        extensions: list[str],
        tags: Optional[list[str]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._available: list[str] = list(extensions)
        self._tags: list[str] = list(tags or [])  # custom tag names, to pick from
        self._updating_checks = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(_page_header(
            "Categories",
            "Main categories and subcategories appear in the sidebar. A category shows the files of its "
            "file types and the files with its tags (the FBX type and a Main tag: every FBX file, plus "
            "anything tagged Main). A main category also shows everything in its subcategories. "
            "Double-click a name to rename it.",
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
        self.btn_add_main.setToolTip("Add a top-level sidebar category")
        self.btn_add_main.clicked.connect(self._on_add_main)
        self.btn_add_sub = QPushButton("+ Subcategory")
        self.btn_add_sub.setToolTip("Add a subcategory under the selected category")
        self.btn_add_sub.clicked.connect(self._on_add_sub)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setToolTip("Remove the selected category")
        self.btn_remove.clicked.connect(self._on_remove)
        row1.addWidget(self.btn_add_main)
        row1.addWidget(self.btn_add_sub)
        row1.addWidget(self.btn_remove)
        left_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_up = QPushButton("Up")
        set_icon(self.btn_up, "arrow_up", size=14)
        self.btn_up.setToolTip("Move the selected category up")
        self.btn_up.clicked.connect(lambda: self._move_current(-1))
        self.btn_down = QPushButton("Down")
        set_icon(self.btn_down, "arrow_down", size=14)
        self.btn_down.setToolTip("Move the selected category down")
        self.btn_down.clicked.connect(lambda: self._move_current(1))
        btn_reset = QPushButton("Reset to Defaults")
        btn_reset.setToolTip("Restore the default categories")
        btn_reset.clicked.connect(self._on_reset)
        row2.addWidget(self.btn_up)
        row2.addWidget(self.btn_down)
        row2.addStretch(1)
        row2.addWidget(btn_reset)
        left_layout.addLayout(row2)

        # Right: the selected category's file types and tags
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)

        self.lbl_types_title = QLabel("File types")
        self.lbl_types_title.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.lbl_types_title.setWordWrap(True)
        right_layout.addWidget(self.lbl_types_title)

        switch_row = QHBoxLayout()
        self.btn_show_types = QPushButton("File Types")
        self.btn_show_types.setToolTip("Choose the file types this category shows")
        self.btn_show_tags = QPushButton("Tags")
        self.btn_show_tags.setToolTip("Choose tags: files with them show in this category too, whatever their type")
        self._switch_group = QButtonGroup(self)
        self._switch_group.setExclusive(True)
        for button in (self.btn_show_types, self.btn_show_tags):
            button.setCheckable(True)
            self._switch_group.addButton(button)
            switch_row.addWidget(button)
        switch_row.addStretch(1)
        right_layout.addLayout(switch_row)
        self.btn_show_types.setChecked(True)

        self.right_stack = QStackedWidget()
        types_panel = QWidget()
        types_layout = QVBoxLayout(types_panel)
        types_layout.setContentsMargins(0, 0, 0, 0)
        self.list_types = QListWidget()
        self.list_types.itemChanged.connect(self._on_type_toggled)
        types_layout.addWidget(self.list_types, 1)

        tags_panel = QWidget()
        tags_layout = QVBoxLayout(tags_panel)
        tags_layout.setContentsMargins(0, 0, 0, 0)
        match_row = QHBoxLayout()
        match_row.addWidget(QLabel("Show files with"))
        self.btn_any_tag = QPushButton("Any of These Tags")
        self.btn_any_tag.setToolTip("Files with at least one of the ticked tags show in this category")
        self.btn_all_tags = QPushButton("All of Them")
        self.btn_all_tags.setToolTip("Only files with every ticked tag show in this category")
        self._match_group = QButtonGroup(self)
        self._match_group.setExclusive(True)
        for button in (self.btn_any_tag, self.btn_all_tags):
            button.setCheckable(True)
            self._match_group.addButton(button)
            match_row.addWidget(button)
        match_row.addStretch(1)
        self.btn_any_tag.setChecked(True)
        self.btn_all_tags.toggled.connect(self._on_match_toggled)
        tags_layout.addLayout(match_row)
        self.list_tags = QListWidget()
        self.list_tags.itemChanged.connect(self._on_tag_toggled)
        tags_layout.addWidget(self.list_tags, 1)
        self.lbl_no_tags = QLabel("No custom tags yet. Make them on the Tags page.")
        self.lbl_no_tags.setObjectName("noteLabel")
        self.lbl_no_tags.setWordWrap(True)
        tags_layout.addWidget(self.lbl_no_tags)

        self.right_stack.addWidget(types_panel)
        self.right_stack.addWidget(tags_panel)
        self.btn_show_tags.toggled.connect(self._on_switch_toggled)
        right_layout.addWidget(self.right_stack, 1)

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
        item.setData(0, TAGS_ROLE, list(entry.get("tags", [])))
        item.setData(0, MATCH_ALL_ROLE, entry.get("tag_match") == "all")
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
            if item.data(0, TAGS_ROLE):
                data["tags"] = list(item.data(0, TAGS_ROLE))
            if item.data(0, MATCH_ALL_ROLE):
                data["tag_match"] = "all"
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

    # ------------------ tags (Settings > Tags edits them while this page is open) ------------------
    def set_available_tags(self, tags: list[str]) -> None:
        """The custom tags to pick from (a tag was made, renamed or deleted on the Tags page)."""
        self._tags = list(tags)
        self._populate_tags(self.tree.currentItem())

    def _entries(self) -> list[QTreeWidgetItem]:
        items = []
        for i in range(self.tree.topLevelItemCount()):
            main_item = self.tree.topLevelItem(i)
            items += [main_item, *(main_item.child(j) for j in range(main_item.childCount()))]
        return items

    def rename_tag(self, old: str, new: str) -> None:
        """A tag was renamed: categories that use it follow."""
        for item in self._entries():
            item.setData(0, TAGS_ROLE, category_rules.renamed(item.data(0, TAGS_ROLE), old, new))
        self._populate_tags(self.tree.currentItem())

    def remove_tag(self, name: str) -> None:
        """A tag was deleted: it leaves every category (the categories stay)."""
        for item in self._entries():
            item.setData(0, TAGS_ROLE, category_rules.without(item.data(0, TAGS_ROLE), name))
        self._populate_tags(self.tree.currentItem())
        self._update_unassigned()

    def add_tag_category(self, tag: str) -> None:
        """A new main category named after a tag that shows the files with it (Tags page > right-click)."""
        item = self._make_item({"name": tag, "color": self._next_color(True), "tags": [tag]})
        self.tree.addTopLevelItem(item)
        self.tree.setCurrentItem(item)
        self.btn_show_tags.setChecked(True)
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
            self.lbl_types_title.setText(f"Shown in {kind} '{current.text(0)}'")
        else:
            self.lbl_types_title.setText("Select a category to choose its file types and tags")
        self._populate_types(current)
        self._populate_tags(current)

    def _on_switch_toggled(self, tags: bool) -> None:
        self.right_stack.setCurrentIndex(1 if tags else 0)

    def _update_switch_counts(self, current: Optional[QTreeWidgetItem]) -> None:
        types = len(current.data(0, EXTENSIONS_ROLE)) if current is not None else 0
        tags = len(current.data(0, TAGS_ROLE)) if current is not None else 0
        self.btn_show_types.setText(f"File Types ({types})" if types else "File Types")
        self.btn_show_tags.setText(f"Tags ({tags})" if tags else "Tags")

    def _populate_tags(self, current: Optional[QTreeWidgetItem]) -> None:
        self._updating_checks = True
        self.list_tags.clear()
        chosen = list(current.data(0, TAGS_ROLE)) if current is not None else []
        folded = {category_rules.fold(t) for t in chosen}
        known = {category_rules.fold(t) for t in self._tags}
        for name in self._tags:
            item = QListWidgetItem(name)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if category_rules.fold(name) in folded else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.list_tags.addItem(item)
        for name in chosen:  # (kept, but no tag has this name now: untick it to drop it)
            if category_rules.fold(name) not in known:
                item = QListWidgetItem(f"{name}   (no tag has this name)")
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                item.setForeground(Qt.GlobalColor.gray)
                item.setData(Qt.ItemDataRole.UserRole, name)
                self.list_tags.addItem(item)
        self.lbl_no_tags.setVisible(not self._tags)
        has_item = current is not None
        for widget in (self.list_tags, self.btn_any_tag, self.btn_all_tags):
            widget.setEnabled(has_item)
        (self.btn_all_tags if has_item and current.data(0, MATCH_ALL_ROLE) else self.btn_any_tag).setChecked(True)
        self._update_switch_counts(current)
        self._updating_checks = False

    def _on_tag_toggled(self, _item: QListWidgetItem) -> None:
        if self._updating_checks:
            return
        current = self.tree.currentItem()
        if current is None:
            return
        current.setData(0, TAGS_ROLE, [
            self.list_tags.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list_tags.count())
            if self.list_tags.item(i).checkState() == Qt.CheckState.Checked
        ])
        self._update_switch_counts(current)
        self._update_unassigned()

    def _on_match_toggled(self, match_all: bool) -> None:
        current = self.tree.currentItem()
        if self._updating_checks or current is None:
            return
        current.setData(0, MATCH_ALL_ROLE, match_all)

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
        self._update_switch_counts(current)
        self._update_unassigned()

    def _update_unassigned(self) -> None:
        assigned: set[str] = set()
        for main in self.categories():
            assigned |= category_extensions(main)
        unassigned = [e for e in self._available if e not in assigned]
        if unassigned:
            by_tag = any(item.data(0, TAGS_ROLE) for item in self._entries())
            where = "under 'Uncategorized', unless a category's tags pick them up" if by_tag else "under 'Uncategorized'"
            self.lbl_unassigned.setText(f"File types in no category (shown {where}): " + ", ".join(unassigned))
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
def _theme_background(theme_id: str, width: int, height: int) -> QBrush:
    """Brush for a theme's window background (a colour, or a gradient: Dark Purple's)."""
    from ui.theme_pack import gradient_stops

    value = THEMES[theme_id]["window_bg"]
    stops = gradient_stops(value)
    if stops:
        background = QLinearGradient(0, 0, width, height)
        for at, color in stops:
            background.setColorAt(at, parse_color(color))
        return QBrush(background)
    return QBrush(parse_color(value))


def _theme_swatch(theme_id: str, width: int = 64, height: int = 40) -> QPixmap:
    """Miniature preview of a theme: background, toolbar strip, sidebar and accent."""
    t = THEMES[theme_id]
    pix = QPixmap(width, height)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    p.setBrush(_theme_background(theme_id, width, height))
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


def _texture_swatch(theme_id: str, texture_id: str, width: int = 56, height: int = 36) -> QPixmap:
    """A background texture over the theme's window background."""
    t = THEMES[theme_id]
    pix = QPixmap(width, height)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(t["button_border"]))
    p.setBrush(_theme_background(theme_id, width, height))
    p.drawRoundedRect(0, 0, width - 1, height - 1, 6, 6)
    tile = render_tile(texture_id, QColor(t["texture_ink"]))
    if tile is not None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QPixmap.fromImage(tile)))
        for _ in range(3):  # stronger than the real thing, so it shows at this size
            p.drawRoundedRect(1, 1, width - 3, height - 3, 5, 5)
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
    texture_selected = Signal(str)

    def __init__(
        self,
        categories_page: CategoriesPage,
        theme_id: str,
        thumbnail_size: int,
        texture_id: str = "none",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.categories_page = categories_page
        self._theme_id = theme_id
        self._texture_id = texture_id
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
            btn.setToolTip(f"Switch to {tokens['label']} (previewed now; Save keeps it)")
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

        # --- Background texture ---
        layout.addWidget(self._section_title("Background Texture"))
        texture_grid = QGridLayout()
        texture_grid.setHorizontalSpacing(8)
        texture_grid.setVerticalSpacing(8)
        self.texture_group = QButtonGroup(self)
        self.texture_group.setExclusive(True)
        self.texture_buttons: dict[str, QPushButton] = {}
        for i, (texture, label) in enumerate(TEXTURES.items()):
            btn = QPushButton(label)
            btn.setObjectName("textureCard")
            btn.setCheckable(True)
            btn.setIconSize(QSize(56, 36))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(
                "Plain theme background, no texture" if texture == "none"
                else f"A faint {label.lower()} pattern behind the window and the grid (previewed now; Save keeps it)"
            )
            btn.clicked.connect(lambda _checked=False, t=texture: self._on_texture_clicked(t))
            self.texture_group.addButton(btn)
            self.texture_buttons[texture] = btn
            texture_grid.addWidget(btn, i // 4, i % 4)
        self.texture_buttons.get(texture_id, self.texture_buttons["none"]).setChecked(True)
        layout.addLayout(texture_grid)
        self._refresh_texture_swatches()
        layout.addSpacing(14)

        # --- Glass frost ---
        layout.addWidget(self._section_title("Glass Frost"))
        frost_row = QHBoxLayout()
        lbl_clear = QLabel("Clear")
        lbl_clear.setObjectName("pageHint")
        self.slider_frost = QSlider(Qt.Orientation.Horizontal)
        self.slider_frost.setRange(0, 100)
        self.slider_frost.setSingleStep(5)
        self.slider_frost.setPageStep(20)
        self.slider_frost.setValue(get_glass_frost())
        self.slider_frost.setToolTip("How frosted the sidebar's glass is: blur, milkiness and grain (previewed now)")
        self.slider_frost.valueChanged.connect(self._on_frost_changed)
        lbl_frosted = QLabel("Frosted")
        lbl_frosted.setObjectName("pageHint")
        self.lbl_frost = QLabel()
        self.lbl_frost.setFixedWidth(60)
        self.lbl_frost.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        frost_row.addWidget(lbl_clear)
        frost_row.addWidget(self.slider_frost, 1)
        frost_row.addWidget(lbl_frosted)
        frost_row.addWidget(self.lbl_frost)
        layout.addLayout(frost_row)
        frost_hint = QLabel(
            "The sidebar is glass: unpin it (the pin button) to float it over the grid and see the thumbnails blur through."
        )
        frost_hint.setObjectName("pageHint")
        frost_hint.setWordWrap(True)
        layout.addWidget(frost_hint)
        self._on_frost_changed(self.slider_frost.value())
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
        self.chk_model_pictures = QCheckBox("Pictures of 3D models", self)
        self.chk_model_pictures.setChecked(get_model_view()["thumbnails"])
        self.chk_model_pictures.setToolTip(
            "Render a picture of each 3D model for its thumbnail (once; then it's kept). Models over 150 MB get theirs "
            "when you first open them. A picture next to the model (model.png) is used instead when there is one."
        )
        layout.addWidget(self.chk_model_pictures)
        alpha_row = QHBoxLayout()
        alpha_row.addWidget(QLabel("Alpha thumbnails"))
        self.btn_alpha_relief = QPushButton("Relief")
        self.btn_alpha_relief.setToolTip(
            "Show grey height images (alphas, displacement maps) as a lit relief, like ZBrush does, "
            "drawn from their full 16-bit heights"
        )
        self.btn_alpha_normal = QPushButton("Normal")
        self.btn_alpha_normal.setToolTip("Show them as the flat grey image they are")
        self.alpha_thumbnail_group = QButtonGroup(self)
        self.alpha_thumbnail_group.setExclusive(True)
        for button in (self.btn_alpha_relief, self.btn_alpha_normal):
            button.setCheckable(True)
            self.alpha_thumbnail_group.addButton(button)
            alpha_row.addWidget(button)
        (self.btn_alpha_relief if get_model_view()["alpha_relief"] else self.btn_alpha_normal).setChecked(True)
        alpha_row.addStretch(1)
        layout.addLayout(alpha_row)

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

        # --- Animations ---
        layout.addWidget(self._section_title("Animations"))
        motion_row = QHBoxLayout()
        self.cb_motion = QComboBox()
        for mode, label in (
            ("auto", "Follow Windows (Animation effects)"),
            ("on", "On"),
            ("off", "Off (reduce motion)"),
        ):
            self.cb_motion.addItem(label, mode)
        self.cb_motion.setCurrentIndex(max(0, self.cb_motion.findData(get_motion())))
        self.cb_motion.setToolTip("Button, menu and dropdown animations. Off makes everything change instantly.")
        motion_row.addWidget(self.cb_motion)
        motion_row.addStretch(1)
        layout.addLayout(motion_row)
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
        self.btn_color.setToolTip("Pick the color of the selected category's sidebar badge")
        self.btn_color.clicked.connect(self._on_choose_color)
        self.btn_icon = QPushButton("Set Icon Image...")
        self.btn_icon.setToolTip("Use a small image (PNG, JPG, SVG or ICO, up to 1 MB) as the selected category's icon")
        self.btn_icon.clicked.connect(self._on_choose_icon)
        self.btn_clear_icon = QPushButton("Remove Icon")
        self.btn_clear_icon.setToolTip("Go back to the color badge for the selected category")
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

    def texture_id(self) -> str:
        return self._texture_id

    def frost(self) -> int:
        return self.slider_frost.value()

    def _on_frost_changed(self, value: int) -> None:
        self.lbl_frost.setText(f"{value}%")
        set_frost(value)  # live preview; the dialog restores the saved value on Cancel

    def _on_texture_clicked(self, texture_id: str) -> None:
        if texture_id != self._texture_id:
            self._texture_id = texture_id
            self.texture_selected.emit(texture_id)

    def _refresh_texture_swatches(self) -> None:
        theme = self._theme_id if self._theme_id in THEMES else current_theme_id()
        for texture, btn in self.texture_buttons.items():
            btn.setIcon(QIcon(_texture_swatch(theme, texture)))

    def refresh_preview(self) -> None:
        """Re-render the sample cards (after a size or theme change)."""
        self._refresh_texture_swatches()
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
        btn_browse.setToolTip("Pick ffmpeg.exe")
        btn_browse.clicked.connect(self._on_browse)
        row.addWidget(self.txt_ffmpeg, 1)
        row.addWidget(btn_browse)
        layout.addLayout(row)
        self.lbl_ffmpeg_status = IconLabel(text_name="pageHint")
        self.lbl_ffmpeg_status.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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
        storage = QLabel("Video posters and preview copies are listed, and can be cleared, in Settings > Storage.")
        storage.setObjectName("pageHint")
        storage.setWordWrap(True)
        layout.addWidget(storage)
        layout.addStretch(1)

        self._update_ffmpeg_status()

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
            self.lbl_ffmpeg_status.set_icon("check", "success")
            self.lbl_ffmpeg_status.setText(f"Using {found}")
        elif self.ffmpeg_path():
            self.lbl_ffmpeg_status.set_icon("close", "danger")
            self.lbl_ffmpeg_status.setText("No ffmpeg.exe at that location.")
        else:
            self.lbl_ffmpeg_status.set_icon("close", "danger")
            self.lbl_ffmpeg_status.setText(
                "ffmpeg isn't on the system PATH. Install it (for example: winget install ffmpeg) "
                "or browse to ffmpeg.exe. Videos still play; only thumbnails and preview copies need it."
            )


# ----------------------------------------------------------------------
# General > Archives
# ----------------------------------------------------------------------
class ArchivesPage(QWidget):
    """Files inside archives in the library, where 7-Zip is (RAR, 7z), and the unpacked copies."""

    def __init__(self, seven_zip_path: str, archive_contents: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)
        layout.addWidget(_page_header(
            "Archives",
            "ZIP, RAR and 7z archives are looked inside like folders (double-click one). ZIPs are read by "
            "the app itself; RAR and 7z need 7-Zip, a free program (7-zip.org).",
        ))

        layout.addWidget(VideoPage._section_label("In the library"))
        self.chk_contents = QCheckBox("Show the files inside archives among your other files", self)
        self.chk_contents.setChecked(archive_contents)
        self.chk_contents.setToolTip(
            "On: search finds them, marked with a small box. Off: they're only shown inside an opened archive."
        )
        layout.addWidget(self.chk_contents)

        layout.addSpacing(10)
        layout.addWidget(VideoPage._section_label("7-Zip location"))
        row = QHBoxLayout()
        self.txt_seven_zip = QLineEdit(seven_zip_path)
        self.txt_seven_zip.setPlaceholderText("Leave blank to find 7-Zip automatically")
        self.txt_seven_zip.textChanged.connect(self._update_status)
        btn_browse = QPushButton("Browse...")
        btn_browse.setToolTip("Pick 7z.exe")
        btn_browse.clicked.connect(self._on_browse)
        row.addWidget(self.txt_seven_zip, 1)
        row.addWidget(btn_browse)
        layout.addLayout(row)
        self.lbl_status = IconLabel(text_name="pageHint")
        self.lbl_status.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.lbl_status)

        layout.addSpacing(10)
        unpacked = QLabel(
            "Models, PDFs and videos inside archives are unpacked to be opened, and the copies kept so they open at "
            "once next time. How much they may take, and clearing them: Settings > Storage."
        )
        unpacked.setObjectName("pageHint")
        unpacked.setWordWrap(True)
        layout.addWidget(unpacked)
        layout.addStretch(1)

        self._update_status()

    def seven_zip_path(self) -> str:
        return self.txt_seven_zip.text().strip()

    def archive_contents(self) -> bool:
        return self.chk_contents.isChecked()

    def _on_browse(self) -> None:
        start = self.seven_zip_path() or str(Path.home())
        chosen, _ = QFileDialog.getOpenFileName(self, "Locate 7z.exe", start, "7-Zip (7z.exe);;Programs (*.exe)")
        if chosen:
            self.txt_seven_zip.setText(str(Path(chosen)))

    def _update_status(self) -> None:
        found = archives.find_seven_zip(self.seven_zip_path())
        if found:
            self.lbl_status.set_icon("check", "success")
            self.lbl_status.setText(f"Using {found}")
        elif self.seven_zip_path():
            self.lbl_status.set_icon("close", "danger")
            self.lbl_status.setText("No 7z.exe at that location.")
        else:
            self.lbl_status.set_icon("close", "danger")
            self.lbl_status.setText(
                "7-Zip wasn't found. Install it (for example: winget install 7zip.7zip) or browse to 7z.exe. "
                "ZIPs work without it; RAR and 7z cards say \"Install 7-Zip to look inside\"."
            )


# ----------------------------------------------------------------------
# General > Storage
# ----------------------------------------------------------------------
class StoragePage(QWidget):
    """What the app keeps on disk (core.storage): each area's size, with Clear / Remove / Open Folder,
    and how much the files unpacked from archives may take (saved with Save)."""

    def __init__(self, unpacked_limit_gb: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)
        layout.addWidget(_page_header(
            "Storage",
            "What the app keeps on disk. Caches can be cleared and the downloaded tools removed: they're made or "
            "downloaded again when needed. Your Artbook pages and the library's backups are only shown here.",
        ))
        self.lbl_total = QLabel("Adding up...", self)
        self.lbl_total.setObjectName("toolbarLabel")
        layout.addWidget(self.lbl_total)
        self.sizes: dict[str, int] = {}
        self.size_labels: dict[str, QLabel] = {}
        self.buttons: dict[str, QPushButton] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(0, 1)
        row = 0
        for area in storage.AREAS:
            name = QLabel(area.name, self)
            name.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
            size = QLabel("...", self)
            size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            size.setMinimumWidth(80)
            self.size_labels[area.key] = size
            grid.addWidget(name, row, 0)
            grid.addWidget(size, row, 1)
            actions = QHBoxLayout()
            actions.setSpacing(6)
            if area.key == "unpacked":
                self.cb_limit = QComboBox(self)
                for gb in storage.UNPACKED_LIMITS_GB:
                    self.cb_limit.addItem(f"Up to {gb} GB", gb)
                self.cb_limit.setCurrentIndex(max(0, self.cb_limit.findData(unpacked_limit_gb)))
                self.cb_limit.setToolTip("How much the unpacked copies may take: the oldest go first past it (applied on Save)")
                actions.addWidget(self.cb_limit)
            text = {"clear": "Clear", "remove": "Remove", "open": "Open Folder"}[area.action]
            button = QPushButton(text, self)
            button.setToolTip({
                "clear": "Delete these: they're made again when needed",
                "remove": "Delete the download: it's downloaded again when needed",
                "open": "Open the folder in Explorer",
            }[area.action])
            button.clicked.connect(lambda _checked=False, key=area.key: self._on_action(key))
            self.buttons[area.key] = button
            actions.addWidget(button)
            grid.addLayout(actions, row, 2, Qt.AlignmentFlag.AlignRight)
            about = QLabel(area.about, self)
            about.setObjectName("pageHint")
            about.setWordWrap(True)
            grid.addWidget(about, row + 1, 0, 1, 3)
            grid.setRowMinimumHeight(row + 2, 8)
            row += 3
        layout.addLayout(grid)
        self.lbl_status = QLabel("", self)
        self.lbl_status.setObjectName("pageHint")
        layout.addWidget(self.lbl_status)
        layout.addStretch(1)
        self._measuring = False
        self._measured_once = False

    def unpacked_limit_gb(self) -> int:
        return int(self.cb_limit.currentData())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._measured_once:
            self.refresh()

    def refresh(self) -> None:
        """Add up every area again, in the background."""
        if self._measuring:
            return
        self._measuring = True
        self._measured_once = True
        run_job(storage.measure, self._on_measured, self._on_measure_failed)

    def _on_measured(self, sizes: dict[str, int]) -> None:
        self._measuring = False
        if not shiboken6.isValid(self):
            return
        self.sizes = sizes
        for key, label in self.size_labels.items():
            label.setText(storage.size_text(sizes.get(key)))
        if sizes.get("versions"):
            self.size_labels["pages"].setToolTip(f"Earlier versions of pages: {storage.size_text(sizes['versions'])} of it")
        for area in storage.AREAS:
            if area.action != "open":
                self.buttons[area.key].setEnabled(bool(sizes.get(area.key)))
        total = sum(sizes.get(area.key, 0) for area in storage.AREAS) + sizes.get("logs", 0)
        self.lbl_total.setText(f"{storage.size_text(total)} in all")

    def _on_measure_failed(self, error: str) -> None:
        self._measuring = False
        if shiboken6.isValid(self):
            self.lbl_total.setText(f"Couldn't add it up: {error}")

    def _on_action(self, key: str) -> None:
        area = next(a for a in storage.AREAS if a.key == key)
        if area.action == "open":
            folder = storage.folder_of(key)
            folder.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
            return
        before = self.sizes.get(key, 0)
        storage.free(key)
        verb = "Removed" if area.action == "remove" else "Cleared"
        self.lbl_status.setText(f"{verb} {area.name.split(' (')[0]} ({storage.size_text(before)}).")
        self.refresh()


# ----------------------------------------------------------------------
# General > Apps
# ----------------------------------------------------------------------
class AppsPage(QWidget):
    """The apps files open in (Open In), whose icons also stand for their files on the cards; apps
    added by hand; and the helpers that import models into a Maya or Blender already open."""

    def __init__(
        self, found: dict[str, list[str]], chosen: dict[str, str], custom: Optional[list[dict[str, Any]]] = None,
        types: Optional[dict[str, list[str]]] = None, parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._found = found
        self._custom = [dict(c) for c in custom or []]
        self._types = dict(types or {})
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()  # (added apps can make it longer than the window)
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("scrollContent")
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)
        layout.addWidget(_page_header(
            "Apps",
            "Right-click > Open In opens files in these, and files without a picture show their app's icon. "
            "They're found by themselves, and the newest version is used unless you pick another here.",
        ))
        self._rows_host = QWidget(self)
        self._rows = QGridLayout(self._rows_host)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setHorizontalSpacing(10)
        self._rows.setColumnStretch(1, 1)
        self._boxes: dict[str, QComboBox] = {}
        for row, app in enumerate(apps.APPS):
            self._rows.addWidget(QLabel(app.name), row, 0)
            box = QComboBox(self._rows_host)
            box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(10)  # (long paths are cut short here, and shown whole in its list)
            box.setToolTip(f"The copy of {app.name} to use")
            box.currentIndexChanged.connect(self._update_helpers)
            self._boxes[app.id] = box
            self._fill(app.id, chosen.get(app.id, ""))
            self._rows.addWidget(box, row, 1)
            browse = QPushButton("Browse...")
            browse.setToolTip(f"Pick {app.name}'s program file")
            browse.clicked.connect(lambda _checked=False, a=app: self._on_browse(a))
            self._rows.addWidget(browse, row, 2)
            kinds = QPushButton("File Types...")
            kinds.setToolTip(f"Which file types Open In offers {app.name} for")
            kinds.clicked.connect(lambda _checked=False, a=app: self._on_edit_types(a))
            self._rows.addWidget(kinds, row, 3)
        self._custom_rows: list[QWidget] = []
        layout.addWidget(self._rows_host)
        layout.addSpacing(6)
        buttons = QHBoxLayout()
        self.btn_add = QPushButton("Add App...")
        self.btn_add.setToolTip("Add any other program to Open In, such as Krita: it's given the file when it starts")
        self.btn_add.clicked.connect(self._on_add)
        buttons.addWidget(self.btn_add)
        self.btn_find = QPushButton("Find Apps Again")
        self.btn_find.setToolTip("Look for them again: in what Windows records, the Start Menu and the Desktop")
        self.btn_find.clicked.connect(self._on_find)
        buttons.addWidget(self.btn_find)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        layout.addSpacing(10)
        layout.addWidget(VideoPage._section_label("Import into the open window"))
        hint = QLabel(
            "Open In starts a new window of Maya or Blender each time. With a small helper script in the app, OBJ and "
            "FBX go into the window you already have open. It only ever imports model files. These buttons act right "
            "away, and the helper starts the next time you start the app."
        )
        hint.setObjectName("pageHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        helpers = QGridLayout()
        helpers.setHorizontalSpacing(10)
        helpers.setColumnStretch(1, 1)
        self._helpers: dict[str, tuple[QLabel, QPushButton]] = {}
        for row, app_id in enumerate(("maya", "blender")):
            name = next(a.name for a in apps.APPS if a.id == app_id)
            helpers.addWidget(QLabel(name), row, 0)
            status = QLabel()
            status.setObjectName("pageHint")
            status.setWordWrap(True)
            helpers.addWidget(status, row, 1)
            button = QPushButton()
            button.setToolTip("Add or remove the helper script")  # (_update_helpers says which)
            button.clicked.connect(lambda _checked=False, a=app_id: self._on_helper(a))
            helpers.addWidget(button, row, 2)
            self._helpers[app_id] = (status, button)
        layout.addLayout(helpers)
        layout.addStretch(1)
        self._build_custom_rows()
        self._update_helpers()

    # ------------------ the apps found ------------------
    def _fill(self, app_id: str, current: str) -> None:
        box = self._boxes[app_id]
        box.blockSignals(True)
        box.clear()
        paths = list(self._found.get(app_id, []))
        if current and current not in paths:
            paths.append(current)
        for path in paths:
            box.addItem(path, path)
        if not paths:
            box.addItem("Not found: use Browse", "")
        if current:
            box.setCurrentIndex(paths.index(current))
        box.blockSignals(False)

    def found(self) -> dict[str, list[str]]:
        return self._found

    def choices(self) -> dict[str, str]:
        """The programs picked: only those that aren't the newest found, so a newer one is used when it comes."""
        chosen = {}
        for app_id, box in self._boxes.items():
            path = box.currentData()
            newest = self._found.get(app_id, [""])[0] if self._found.get(app_id) else ""
            if path and path != newest:
                chosen[app_id] = path
        return chosen

    def _on_browse(self, app: apps.App) -> None:
        start = self._boxes[app.id].currentData() or str(Path.home())
        chosen, _ = QFileDialog.getOpenFileName(self, f"Locate {app.name}", start, "Programs (*.exe)")
        if chosen:
            self._fill(app.id, str(Path(chosen)))
            self._update_helpers()

    def _on_find(self) -> None:
        self.btn_find.setEnabled(False)
        self.btn_find.setText("Looking...")
        run_job(apps.detect, self._on_found, self._on_find_failed)

    def _on_found(self, found: dict[str, list[str]]) -> None:
        if not shiboken6.isValid(self):
            return  # (Settings was closed meanwhile)
        current = {app_id: box.currentData() for app_id, box in self._boxes.items()}
        self._found = found
        for app_id in self._boxes:
            self._fill(app_id, current[app_id])
        self.btn_find.setEnabled(True)
        self.btn_find.setText("Find Apps Again")
        self._update_helpers()

    def _on_find_failed(self, _message: str) -> None:
        if shiboken6.isValid(self):
            self.btn_find.setEnabled(True)
            self.btn_find.setText("Find Apps Again")

    # ------------------ file types, and apps added by hand ------------------
    def custom(self) -> list[dict[str, Any]]:
        return self._custom

    def types(self) -> dict[str, list[str]]:
        """The file types set by hand for the apps that came with this one."""
        return self._types

    def types_in_use(self) -> list[str]:
        """Every file type set by hand: the library needs to pick them up for Open In to offer them."""
        return [e for entry in (*self._types.values(), *(c["types"] for c in self._custom)) for e in entry]

    def _ask_types(self, title: str, current: list[str], hint: str) -> Optional[list[str]]:
        """File types from the user, or None if cancelled or not valid (it says what's wrong)."""
        text, accepted = QInputDialog.getText(self, title, hint, text=", ".join(current))
        if not accepted:
            return None
        if not text.strip():
            return []
        extensions, errors = parse_extension_list(text)
        if errors:
            QMessageBox.warning(self, title, "\n".join(errors[:6]))
            return None
        return extensions

    def _on_edit_types(self, app: apps.App) -> None:
        current = self._types.get(app.id, sorted(app.opens))
        chosen = self._ask_types(
            f"{app.name}: File Types", current, "File types Open In offers it for, with commas (leave empty for its usual ones):",
        )
        if chosen is None:
            return
        if chosen and set(chosen) != set(app.opens):
            self._types[app.id] = chosen
        else:
            self._types.pop(app.id, None)

    def _on_add(self) -> None:
        program, _ = QFileDialog.getOpenFileName(self, "Add App", str(Path.home()), "Programs (*.exe)")
        if not program:
            return
        program = str(Path(program))
        suggested = Path(program).stem.replace("-", " ").replace("_", " ").title()
        name, accepted = QInputDialog.getText(self, "Add App", "Its name in Open In:", text=suggested)
        name = " ".join(name.split())
        if not accepted or not name:
            return
        title = f"{name}: File Types"
        types = self._ask_types(title, [], "File types Open In offers it for, with commas (.png, .psd, .kra):")
        if not types:
            return
        self._custom.append({"id": f"custom-{uuid.uuid4().hex[:8]}", "name": name, "program": program, "types": types})
        self._build_custom_rows()

    def _on_edit_custom(self, entry: dict[str, Any]) -> None:
        chosen = self._ask_types(f"{entry['name']}: File Types", entry["types"], "File types Open In offers it for, with commas:")
        if chosen:
            entry["types"] = chosen

    def _on_browse_custom(self, entry: dict[str, Any]) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, f"Locate {entry['name']}", entry["program"], "Programs (*.exe)")
        if chosen:
            entry["program"] = str(Path(chosen))
            self._build_custom_rows()

    def _on_remove_custom(self, entry: dict[str, Any]) -> None:
        self._custom.remove(entry)
        self._build_custom_rows()

    def _build_custom_rows(self) -> None:
        for widget in self._custom_rows:
            self._rows.removeWidget(widget)
            widget.deleteLater()
        self._custom_rows = []
        for offset, entry in enumerate(self._custom):
            row = len(apps.APPS) + offset
            label = QLabel(entry["name"])
            path = QLineEdit(entry["program"])
            path.setReadOnly(True)
            path.setToolTip(entry["program"])
            browse = QPushButton("Browse...")
            browse.setToolTip(f"Pick {entry['name']}'s program file")
            browse.clicked.connect(lambda _checked=False, e=entry: self._on_browse_custom(e))
            kinds = QPushButton("File Types...")
            kinds.setToolTip(f"Which file types Open In offers {entry['name']} for")
            kinds.clicked.connect(lambda _checked=False, e=entry: self._on_edit_custom(e))
            remove = QPushButton("Remove")
            remove.setToolTip(f"Take {entry['name']} out of Open In (the program itself isn't touched)")
            remove.clicked.connect(lambda _checked=False, e=entry: self._on_remove_custom(e))
            for column, widget in enumerate((label, path, browse, kinds, remove)):
                self._rows.addWidget(widget, row, column)
                self._custom_rows.append(widget)

    # ------------------ helpers ------------------
    def _update_helpers(self) -> None:
        for app_id, (status, button) in self._helpers.items():
            program = self._boxes[app_id].currentData() or None
            installed = app_helpers.is_installed(app_id, program)
            button.setEnabled(program is not None or installed)
            button.setText("Remove Helper" if installed else "Install Helper")
            button.setToolTip(
                "Take the helper script out of the app" if installed else
                "Add a small script to the app's own scripts folder, so OBJ and FBX can go into its open window"
            )
            if program is None:
                status.setText("Not found.")
            else:
                status.setText("Installed." if installed else "Not installed: Open In starts a new window.")

    def _on_helper(self, app_id: str) -> None:
        program = self._boxes[app_id].currentData() or None
        status = self._helpers[app_id][0]
        adding = not app_helpers.is_installed(app_id, program)
        try:
            (app_helpers.install if adding else app_helpers.remove)(app_id, program)
        except app_helpers.HelperError as exc:
            status.setText(str(exc))
            return
        self._update_helpers()
        name = next(a.name for a in apps.APPS if a.id == app_id)
        status.setText(
            f"Installed. It starts the next time you start {name}." if adding
            else f"Removed. A {name} window that's open keeps it until it's closed."
        )


# ----------------------------------------------------------------------
# General > Artbook
# ----------------------------------------------------------------------
class ArtbookPage(QWidget):
    """Where the Artbook's pages are kept. A change happens right away (pages are moved, which
    can take a while), so it isn't left for Save. artbook: the Artbook panel, saved before a
    change and shown again after it (None: only the files and the setting change)."""

    def __init__(self, artbook: Optional[QWidget] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.artbook = artbook
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)
        layout.addWidget(_page_header(
            "Artbook",
            "Each page is a folder with its layout and the images kept in it. Changes here happen "
            "right away.",
        ))
        layout.addWidget(VideoPage._section_label("Pages folder"))
        self.lbl_folder = QLabel()
        self.lbl_folder.setObjectName("pageHint")
        self.lbl_folder.setWordWrap(True)
        self.lbl_folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.lbl_folder)
        row = QHBoxLayout()
        self.btn_change = QPushButton("Change...")
        self.btn_change.setToolTip("Pick another folder for your pages: they're moved there")
        self.btn_change.clicked.connect(self._on_change)
        self.btn_open = QPushButton("Open Folder")
        self.btn_open.setToolTip("Open the pages folder in Explorer")
        self.btn_open.clicked.connect(self._on_open)
        self.btn_default = QPushButton("Use Default")
        self.btn_default.setToolTip(f"Move your pages back to the default folder ({artbook_store.DEFAULT_PAGES_DIR})")
        self.btn_default.clicked.connect(self._on_default)
        for button in (self.btn_change, self.btn_open, self.btn_default):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self.lbl_status = QLabel()
        self.lbl_status.setObjectName("pageHint")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)
        hint = QLabel(
            "A folder in a synced place (OneDrive, Dropbox) keeps your pages backed up. If the folder "
            "isn't there when the app starts (a drive that's unplugged), the default one is used until it's back."
        )
        hint.setObjectName("pageHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        self._update()

    def is_busy(self) -> bool:
        return self._busy

    def _update(self) -> None:
        folder = artbook_store.pages_dir()
        default = artbook_store.same_folder(folder, artbook_store.DEFAULT_PAGES_DIR)
        self.lbl_folder.setText(f"{folder}{'  (default)' if default else ''}")
        self.btn_change.setEnabled(not self._busy)
        self.btn_open.setEnabled(not self._busy)
        self.btn_default.setEnabled(not self._busy and not default)

    def _on_open(self) -> None:
        folder = artbook_store.pages_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _on_change(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Pages folder", str(artbook_store.pages_dir().parent))
        if chosen:
            self.change_to(artbook_store.pages_target(Path(chosen)))

    def _on_default(self) -> None:
        self.change_to(artbook_store.DEFAULT_PAGES_DIR)

    def change_to(self, target: Path, ask: bool = True) -> None:
        """Keep pages in target: move them there, or (if it already has pages) show those."""
        if artbook_store.same_folder(target, artbook_store.pages_dir()):
            self.lbl_status.setText("Your pages are already in that folder.")
            return
        if artbook_store.has_pages(target):
            if ask and not self._confirm(
                "Use These Pages",
                f"{target} already has Artbook pages.\n\nShow the pages there instead? Your current pages "
                f"stay where they are ({artbook_store.pages_dir()}), and you can switch back any time.",
                "Use These Pages",
            ):
                return
            if self.artbook is not None:
                self.artbook.leave_pages()
            artbook_store.set_pages_dir(target)
            self._finish(target, "Now showing the pages in that folder.")
            return
        if ask and not self._confirm(
            "Move Pages",
            f"Move your pages to {target}?\n\nThey're copied there first and only then removed from "
            f"{artbook_store.pages_dir()}, so nothing is lost if the copy fails.",
            "Move Pages",
        ):
            return
        if self.artbook is not None:
            self.artbook.leave_pages()
        self._busy = True
        self._update()
        self.lbl_status.setText("Moving your pages...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        run_job(lambda: artbook_store.move_pages(target), self._on_moved, self._on_move_failed)

    def _confirm(self, title: str, text: str, action: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=self)
        yes = box.addButton(action, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is yes

    def _on_moved(self, count: int) -> None:
        QApplication.restoreOverrideCursor()
        self._busy = False
        self._finish(artbook_store.pages_dir(), f"Moved {count} page{'s' if count != 1 else ''}.")

    def _on_move_failed(self, error: str) -> None:
        QApplication.restoreOverrideCursor()
        self._busy = False
        self._update()
        self.lbl_status.setText(f"{error} Your pages are still in {artbook_store.pages_dir()}.")
        if self.artbook is not None:
            self.artbook.reload_pages()

    def _finish(self, folder: Path, message: str) -> None:
        default = artbook_store.same_folder(folder, artbook_store.DEFAULT_PAGES_DIR)
        set_pages_dir("" if default else str(folder))
        if self.artbook is not None:
            self.artbook.reload_pages()
        self._update()
        self.lbl_status.setText(message)


# ----------------------------------------------------------------------
# Guide
# ----------------------------------------------------------------------
# Bundled with the app (AssetManager.spec): under sys._MEIPASS in the PyInstaller executable
DOCS_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
GUIDES = {  # page name in Settings -> the Markdown file, from DOCS_ROOT
    "Artbook Guide": "docs/artbook-guide.md",
    "Theme Studio": "docs/theme-studio.md",
    "Read Me": "README.md",
}


def _slug(text: str) -> str:
    """A heading's link name, as GitHub makes it ("The Artbook" -> "the-artbook")."""
    return re.sub(r"[^a-z0-9 -]", "", text.strip().lower()).replace(" ", "-")


class GuidePage(QWidget):
    """One of the guides, shown as formatted text. Links to the other guide (or one of its headings)
    open it here; web links open in the browser. open_guide(name, heading) switches guides."""

    def __init__(self, name: str, open_guide: Callable[[str, str], None], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.name = name
        self._open_guide = open_guide
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 12)
        layout.setSpacing(8)
        self.text = QTextBrowser()
        self.text.setObjectName("guideText")
        self.text.setOpenLinks(False)
        self.text.anchorClicked.connect(self._on_link)
        palette = self.text.palette()
        palette.setColor(palette.ColorRole.Link, QColor(theme_color("accent_text")))
        self.text.setPalette(palette)
        path = DOCS_ROOT / GUIDES[name]
        try:
            self.text.setMarkdown(path.read_text(encoding="utf-8"))
        except OSError:
            self.text.setPlainText(f"The {name} couldn't be found ({path}).")
        layout.addWidget(self.text, 1)

    def show_heading(self, heading: str) -> bool:
        """Scroll to the heading with this link name (see _slug); False if there's none."""
        block = self.text.document().begin()
        while block.isValid():
            if block.blockFormat().headingLevel() and _slug(block.text()) == heading:
                cursor = QTextCursor(block)
                self.text.setTextCursor(cursor)
                bar = self.text.verticalScrollBar()
                bar.setValue(round(self.text.document().documentLayout().blockBoundingRect(block).top()))
                return True
            block = block.next()
        return False

    def _on_link(self, url: QUrl) -> None:
        if url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return
        target = url.path().replace("\\", "/").split("/")[-1].lower()
        for name, file in GUIDES.items():
            if target and file.lower().endswith(target):
                self._open_guide(name, url.fragment())
                return
        if not target and url.fragment():
            self.show_heading(url.fragment())


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
    """Create, rename, recolor and delete custom tags.

    Edits are written straight away (so the counts shown are real), and each one records
    how to undo it; Cancel on the Settings dialog calls revert() to undo them all.
    """

    tags_changed = Signal()  # a tag was made, renamed or deleted (Categories lists them)
    tag_renamed = Signal(str, str)  # old name, new name
    tag_deleted = Signal(str)
    category_requested = Signal(str)  # right-click > Add as Sidebar Category: the tag's name

    def __init__(self, repo: TagRepository, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.repo = repo
        self.changed = False  # set once anything was edited, so the main window can refresh
        self._undo: list[Callable[[], None]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(_page_header(
            "Tags",
            "Custom tags let you group files however you like. Tag files from the grid "
            "(right-click > Tag..., or Ctrl+T) and search with tag:name. "
            "File type tags are added automatically and can't be edited. Cancel undoes the changes made here.",
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
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_menu)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        self.btn_new = QPushButton("+ New Tag...")
        self.btn_new.setToolTip("Create a custom tag")
        self.btn_new.setObjectName("primaryBtn")
        self.btn_new.clicked.connect(self._on_new)
        self.btn_rename = QPushButton("Rename...")
        self.btn_rename.setToolTip("Rename the selected tag (or double-click it)")
        self.btn_rename.clicked.connect(self._on_rename)
        self.btn_color = QPushButton("Change Color...")
        self.btn_color.setToolTip("Change the selected tag's color")
        self.btn_color.clicked.connect(self._on_color)
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setToolTip("Delete the selected tag (Cancel brings it back)")
        self.btn_delete.clicked.connect(self._on_delete)
        for btn in (self.btn_new, self.btn_rename, self.btn_color, self.btn_delete):
            buttons.addWidget(btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.refresh()

    def revert(self) -> None:
        """Undo every edit made on this page, newest first."""
        for undo in reversed(self._undo):
            try:
                undo()
            except TagError:
                pass  # e.g. the name was taken again meanwhile; skip that one
        self._undo.clear()

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

    def custom_tag_names(self) -> list[str]:
        return [t.name for t in self.repo.list_tags(kind="custom")]

    def _show_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is not None:
            self.tree.setCurrentItem(item)
        tag = self._current_custom()
        if tag is None:
            return
        menu = QMenu(self)
        action_category = menu.addAction("Add as Sidebar Category")
        action_category.setToolTip("Make a sidebar category that shows the files with this tag")
        action_rename = menu.addAction("Rename...")
        action_delete = menu.addAction("Delete")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        menu.deleteLater()
        if chosen is action_category:
            self.category_requested.emit(tag.name)
        elif chosen is action_rename:
            self._on_rename()
        elif chosen is action_delete:
            self._on_delete()

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
            self._undo.append(lambda tag_id=tag.id: self.repo.delete_custom_tag(tag_id))
            self.changed = True
            self.refresh(select_id=tag.id)
            self.tags_changed.emit()
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
            self._undo.append(lambda tag_id=tag.id, old=tag.name: self.repo.rename_custom_tag(tag_id, old))
            self.changed = True
            self.refresh(select_id=tag.id)
            self.tag_renamed.emit(tag.name, self.repo.get_tag(tag.id).name)
            self.tags_changed.emit()
            return

    def _on_color(self) -> None:
        tag = self._current_custom()
        if tag is None:
            return
        color = QColorDialog.getColor(QColor(tag.color or "#9ca3af"), self, f"Color for {tag.name}")
        if color.isValid():
            self.repo.set_tag_color(tag.id, color.name())
            self._undo.append(lambda tag_id=tag.id, old=tag.color: self.repo.set_tag_color(tag_id, old))
            self.changed = True
            self.refresh(select_id=tag.id)

    def _on_delete(self) -> None:
        tag = self._current_custom()
        if tag is None:
            return
        files = f"{tag.file_count} file{'s' if tag.file_count != 1 else ''}"
        answer = QMessageBox.question(
            self, "Delete Tag",
            f'Delete the tag "{tag.name}"?\n\nIt will be removed from {files}. The files themselves are not affected. '
            "(Cancel in Settings brings it back.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            snapshot = self.repo.custom_tag_snapshot(tag.id)  # the tag and its files, to undo on Cancel
            self.repo.delete_custom_tag(tag.id)
            self._undo.append(lambda: self.repo.restore_custom_tag(*snapshot))
            self.changed = True
            self.refresh()
            self.tag_deleted.emit(tag.name)
            self.tags_changed.emit()


# ----------------------------------------------------------------------
# General > Profiles
# ----------------------------------------------------------------------
class ProfilesPage(QWidget):
    """Make, rename, duplicate and delete profiles. Changes are made on Save (apply()); a desktop
    shortcut is made straight away (it changes no settings)."""

    def __init__(self, repo: TagRepository, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.repo = repo
        self._current = profiles.current_profile().id
        # Each row: {"id", "name", "new": made here (with its "lists")}
        self._rows: list[dict[str, Any]] = [{"id": p.id, "name": p.name, "new": False} for p in profiles.list_profiles()]
        self._saved_names = {p.id: p.name for p in profiles.list_profiles()}
        self._deleted: list[str] = []
        self._new_count = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(_page_header(
            "Profiles",
            "Each profile has its own library folders, file types, categories and collections, so a ZBrush "
            "profile only shows your ZBrush files. Tags, favorites and ratings are shared by every profile. "
            "Switch profile with the profile button at the top left of the window (Ctrl+Shift+P). "
            "Changes made here happen when you click Save.",
        ))
        self.list_profiles = QListWidget()
        self.list_profiles.currentRowChanged.connect(self._update_buttons)
        self.list_profiles.itemDoubleClicked.connect(lambda *_: self._on_rename())
        layout.addWidget(self.list_profiles, 1)

        buttons = QHBoxLayout()
        self.btn_new = QPushButton("+ New Profile...")
        self.btn_new.setObjectName("primaryBtn")
        self.btn_new.setToolTip("Make a profile, as a copy of the selected one or with no folders")
        self.btn_new.clicked.connect(self._on_new)
        self.btn_rename = QPushButton("Rename...")
        self.btn_rename.setToolTip("Rename the selected profile (or double-click it)")
        self.btn_rename.clicked.connect(self._on_rename)
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setToolTip("Delete the selected profile and its collections (not your files, tags or ratings)")
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_shortcut = QPushButton("Desktop Shortcut")
        set_icon(self.btn_shortcut, "open_in", size=14)
        self.btn_shortcut.setToolTip("Put a shortcut on the desktop that opens the app in the selected profile")
        self.btn_shortcut.clicked.connect(self._on_shortcut)
        for button in (self.btn_new, self.btn_rename, self.btn_delete, self.btn_shortcut):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.lbl_note = QLabel()
        self.lbl_note.setObjectName("noteLabel")
        self.lbl_note.setWordWrap(True)
        self.lbl_note.hide()
        layout.addWidget(self.lbl_note)
        self._fill()

    # ------------------ the list ------------------
    def pending(self) -> list[Profile]:
        """The profiles as they'll be after Save (for checking names)."""
        return [Profile(row["id"], row["name"]) for row in self._rows]

    def _fill(self, select: Optional[str] = None) -> None:
        select = select or self._selected_id() or self._current
        self.list_profiles.clear()
        for row in self._rows:
            text = row["name"]
            if row["id"] == self._current:
                text += "   (in use)"
            elif row["new"]:
                text += "   (new: made when you click Save)"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            if row["id"] == self._current:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.list_profiles.addItem(item)
            if row["id"] == select:
                self.list_profiles.setCurrentItem(item)
        self._update_buttons()

    def _selected_id(self) -> Optional[str]:
        item = self.list_profiles.currentItem() if hasattr(self, "list_profiles") else None
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _row(self, profile_id: Optional[str]) -> Optional[dict[str, Any]]:
        return next((r for r in self._rows if r["id"] == profile_id), None)

    def _update_buttons(self, *_args) -> None:
        row = self._row(self._selected_id())
        self.btn_rename.setEnabled(row is not None)
        in_use = row is not None and row["id"] == self._current
        self.btn_delete.setEnabled(row is not None and not in_use)
        self.btn_delete.setToolTip(
            "The profile in use can't be deleted: switch to another one first" if in_use
            else "Delete the selected profile and its collections (not your files, tags or ratings)"
        )
        self.btn_shortcut.setEnabled(row is not None and not row["new"])
        self.btn_shortcut.setToolTip(
            "Click Save first: the profile is made then" if row is not None and row["new"]
            else "Put a shortcut on the desktop that opens the app in the selected profile"
        )

    def _lists_for(self, profile_id: str) -> dict[str, Any]:
        row = self._row(profile_id)
        if row is not None and row["new"]:
            return copy.deepcopy(row["lists"])
        return profiles.new_lists(profile_id)

    # ------------------ editing ------------------
    def _on_new(self) -> None:
        from ui.profile_dialogs import NewProfileDialog

        source = self._row(self._selected_id()) or self._row(self._current)
        dialog = NewProfileDialog(Profile(source["id"], source["name"]), self, taken=self.pending())
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            copy_of = dialog.copy_of()
            lists = self._lists_for(copy_of) if copy_of is not None else profiles.new_lists(None)
            self._new_count += 1
            new_id = f"new-{self._new_count}"
            self._rows.append({"id": new_id, "name": dialog.name(), "new": True, "lists": lists})
            self._fill(select=new_id)
        finally:
            dialog.deleteLater()

    def _on_rename(self) -> None:
        row = self._row(self._selected_id())
        if row is None:
            return
        name, ok = QInputDialog.getText(self, "Rename Profile", f"Name (up to {profiles.MAX_NAME_LENGTH} characters):", text=row["name"])
        while ok:
            try:
                row["name"] = profiles.validate_name(name, exclude_id=row["id"], taken=self.pending())
            except ProfileError as exc:
                QMessageBox.warning(self, "Can't Rename Profile", str(exc))
                name, ok = QInputDialog.getText(self, "Rename Profile", "Name:", text=name)
                continue
            self._fill(select=row["id"])
            return

    def _on_delete(self) -> None:
        row = self._row(self._selected_id())
        if row is None or row["id"] == self._current:
            return
        if not row["new"]:
            own = sum(1 for saved in self.repo.list_saved_searches() if saved.profile == row["id"])
            collections = f" and its {own} collection{'s' if own != 1 else ''}" if own else ""
            answer = QMessageBox.question(
                self, "Delete Profile",
                f'Delete the profile "{row["name"]}"{collections}?\n\nIts folders, file types and categories go '
                "with it. Your files, tags, favorites and ratings aren't touched. (Cancel in Settings keeps it.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._deleted.append(row["id"])
        self._rows.remove(row)
        self._fill(select=self._current)

    def _on_shortcut(self) -> None:
        row = self._row(self._selected_id())
        if row is None or row["new"]:
            return
        profile = Profile(row["id"], row["name"])
        self.btn_shortcut.setEnabled(False)
        self.lbl_note.setText("Making the shortcut...")
        self.lbl_note.show()
        run_job(lambda: profiles.create_desktop_shortcut(profile), self._on_shortcut_made, self._on_shortcut_failed)

    def _on_shortcut_made(self, path: Path) -> None:
        if not shiboken6.isValid(self):
            return
        self.lbl_note.setText(f'Made "{path.name}" on the desktop: it opens the app in that profile.')
        self._update_buttons()

    def _on_shortcut_failed(self, error: str) -> None:
        if not shiboken6.isValid(self):
            return
        self.lbl_note.setText(f"The shortcut couldn't be made: {error}")
        self._update_buttons()

    # ------------------ Save ------------------
    def changed(self) -> bool:
        return bool(self._deleted) or any(
            row["new"] or row["name"] != self._saved_names.get(row["id"]) for row in self._rows
        )

    def apply(self) -> list[str]:
        """Make the changes (on Save). Returns the problems, if any (each change is tried)."""
        problems = []
        for profile_id in self._deleted:
            try:
                profiles.delete_profile(profile_id)
                self.repo.delete_profile_searches(profile_id)
            except (ProfileError, Exception) as exc:  # pylint: disable=broad-except
                problems.append(str(exc))
        for row in self._rows:
            if not row["new"] and row["name"] != self._saved_names.get(row["id"]):
                try:
                    profiles.rename_profile(row["id"], row["name"])
                except ProfileError as exc:
                    problems.append(str(exc))
        for row in self._rows:
            if row["new"]:
                try:
                    profiles.create_profile(row["name"], lists=row["lists"])
                except ProfileError as exc:
                    problems.append(str(exc))
        return problems


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
        self.btn_update.setToolTip("Look for a newer version on GitHub")
        self.btn_update.setObjectName("primaryBtn")
        self.btn_update.clicked.connect(self._on_check_updates)
        row.addWidget(self.btn_update)
        self.btn_crash_log = QPushButton("Open Crash Log")
        log_exists = crash_log.crash_log_path().exists()
        self.btn_crash_log.setEnabled(log_exists)
        self.btn_crash_log.setToolTip(
            "Crashes, freezes and errors are recorded here, for bug reports"
            if log_exists else "Nothing has been logged: no crashes or errors so far"
        )
        self.btn_crash_log.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(crash_log.crash_log_path())))
        )
        row.addWidget(self.btn_crash_log)
        self.btn_report = QPushButton("Report a Problem...")
        self.btn_report.setToolTip("Make a zip of the logs and this computer's details, for a bug report (nothing is uploaded)")
        self.btn_report.clicked.connect(self._on_report)
        row.addWidget(self.btn_report)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addSpacing(8)

        self.lbl_update_status = QLabel()
        self.lbl_update_status.setObjectName("pageHint")
        self.lbl_update_status.setWordWrap(True)
        self.lbl_update_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.lbl_update_status)
        layout.addStretch(1)

    def _on_report(self) -> None:
        from ui.report_dialog import ReportDialog

        dialog = ReportDialog(self)
        dialog.finished.connect(dialog.deleteLater)
        dialog.open()
        self.report_dialog = dialog

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

        # Connect bound slots only: a lambda would run on the worker thread and touch widgets there
        worker = _UpdateWorker(info)
        worker.progress.connect(self._progress_dialog.setLabelText)
        worker.installed.connect(self._on_installed)
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

    @Slot(bool)
    def _on_installed(self, requirements_changed: bool) -> None:
        info = self._worker.info
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

    def __init__(self, parent: Optional[QWidget] = None, artbook: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("settingsDialog")
        self.setWindowTitle("Settings")
        self.resize(900, 600)
        self.setMinimumSize(760, 520)

        self._orig_folders = get_scan_folders()
        self._orig_extensions = get_file_extensions()
        self._orig_categories = get_categories()
        self._orig_theme = get_theme()
        self._orig_texture = get_background_texture()
        self._orig_thumbnail_size = get_thumbnail_size()
        self._orig_ffmpeg_path = get_ffmpeg_path()
        self._orig_media_backend = get_media_backend()
        self._orig_seven_zip_path = get_seven_zip_path()
        self._orig_unpacked_limit = get_unpacked_limit_gb()
        self._orig_archive_contents = get_archive_contents()

        # Set after Save so the main window knows what to refresh
        self.folders_changed = False
        self.extensions_changed = False
        self.categories_changed = False
        self.theme_changed = False
        self.thumbnail_size_changed = False
        self.model_thumbnails_changed = False
        self.alpha_relief_changed = False
        self.archives_changed = False  # 7-Zip's location, or whether files inside archives are shown
        self.apps_changed = False  # the apps in use (Settings > Apps)
        self.profiles_changed = False  # made, renamed or deleted (Settings > Profiles)
        self._tag_edits: list[Callable[[list[str]], list[str]]] = []  # for other profiles' categories

        self.page_folders = FoldersPage(self._orig_folders)
        self.page_files = FilesPage(self._orig_extensions)
        self.page_tags = TagsPage(get_repository())
        self.page_categories = CategoriesPage(self._orig_categories, self._orig_extensions, self.page_tags.custom_tag_names())
        self.page_appearance = AppearancePage(
            self.page_categories, self._orig_theme, self._orig_thumbnail_size, self._orig_texture
        )
        self.page_appearance.theme_selected.connect(self._on_theme_selected)
        self.page_appearance.texture_selected.connect(self._on_texture_selected)
        self.page_video = VideoPage(self._orig_ffmpeg_path, self._orig_media_backend)
        self.page_archives = ArchivesPage(self._orig_seven_zip_path, self._orig_archive_contents)
        self.page_apps = AppsPage(get_found_apps(), get_app_choices(), get_custom_apps(), get_app_types())
        self.page_artbook = ArtbookPage(artbook)
        self.page_profiles = ProfilesPage(get_repository())
        self.page_shortcuts = ShortcutsPage()
        self.page_storage = StoragePage(self._orig_unpacked_limit)
        self.page_about = AboutPage()

        self.page_files.validity_changed.connect(self._on_files_validity_changed)
        self.page_tags.tags_changed.connect(self._on_tags_changed)
        self.page_tags.tag_renamed.connect(self.page_categories.rename_tag)
        self.page_tags.tag_deleted.connect(self.page_categories.remove_tag)
        self.page_tags.category_requested.connect(self._on_tag_category_requested)
        self.page_tags.tag_renamed.connect(self._on_tag_renamed)
        self.page_tags.tag_deleted.connect(self._on_tag_deleted)
        # (Folders, Files and Categories belong to the profile in use: they say which)
        in_use = profiles.current_profile().name
        for page in (self.page_folders, self.page_files, self.page_categories):
            note = QLabel(
                f"Profile: <b>{html.escape(in_use)}</b>. Each profile has its own folders, file types and "
                "categories (Settings > Profiles)."
            )
            note.setObjectName("mutedLabel")
            note.setWordWrap(True)
            page.layout().insertWidget(1, note)
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
            ("Profiles", self.page_profiles),
            ("Appearance", self.page_appearance),
            ("Video", self.page_video),
            ("Archives", self.page_archives),
            ("Apps", self.page_apps),
            ("Artbook", self.page_artbook),
            ("Tags", self.page_tags),
            ("Shortcuts", self.page_shortcuts),
            ("Storage", self.page_storage),
        ):
            item = QTreeWidgetItem(general, [name])
            item.setData(0, Qt.ItemDataRole.UserRole, self.stack.addWidget(page))
            self.nav_items[name] = item
        general.setData(0, Qt.ItemDataRole.UserRole, 0)  # General opens its first page
        general.setExpanded(True)

        guide = QTreeWidgetItem(self.nav, ["Guide"])
        guide.setFont(0, QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.guide_pages: dict[str, GuidePage] = {}
        for name in GUIDES:
            self.guide_pages[name] = GuidePage(name, self.open_guide)
            item = QTreeWidgetItem(guide, [name])
            item.setData(0, Qt.ItemDataRole.UserRole, self.stack.addWidget(self.guide_pages[name]))
            self.nav_items[name] = item
        guide.setData(0, Qt.ItemDataRole.UserRole, self.nav_items["Artbook Guide"].data(0, Qt.ItemDataRole.UserRole))
        guide.setExpanded(True)

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
        btn_cancel.setToolTip("Close without saving (tag edits made here are undone too)")
        btn_cancel.clicked.connect(self.reject)
        self.btn_save = QPushButton("Save")
        self.btn_save.setToolTip("Save all settings")
        self.btn_save.setObjectName("primaryBtn")
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self._on_save)
        footer_layout.addWidget(btn_cancel)
        footer_layout.addWidget(self.btn_save)
        root.addWidget(footer)

    def show_page(self, name: str) -> None:
        if name in self.nav_items:
            self.nav.setCurrentItem(self.nav_items[name])

    def open_guide(self, name: str, heading: str = "") -> None:
        """Show a guide, at one of its headings (a link name, see GuidePage) if given."""
        self.show_page(name)
        page = self.guide_pages[name]
        if heading:
            page.show_heading(heading)
        else:
            page.text.verticalScrollBar().setValue(0)

    def _on_nav_changed(self, current: Optional[QTreeWidgetItem], _previous) -> None:
        if current is not None:
            self.stack.setCurrentIndex(current.data(0, Qt.ItemDataRole.UserRole))

    def _on_files_validity_changed(self, valid: bool) -> None:
        self.btn_save.setEnabled(valid)
        self.lbl_footer_warning.setVisible(not valid)

    # Previews and Cancel's restore wait for the click to finish (apply_theme_soon): restyling the
    # app from inside a button's click handling can crash Qt
    def _on_theme_selected(self, theme_id: str) -> None:
        # Live preview; reverted in reject() if the dialog is cancelled
        apply_theme_soon(theme_id, then=self.page_appearance.refresh_preview)

    def _on_texture_selected(self, texture_id: str) -> None:
        apply_theme_soon(self.page_appearance.theme_id(), texture_id=texture_id)  # live preview, like themes

    def reject(self) -> None:
        if self.page_about.is_busy() or self.page_artbook.is_busy():
            return
        chosen = (self.page_appearance.theme_id(), self.page_appearance.texture_id())
        if chosen != (get_theme(), get_background_texture()):
            apply_theme_soon(get_theme(), texture_id=get_background_texture())
        set_frost(get_glass_frost())
        # Discard icon images imported during this session
        remove_unused_category_icons(get_categories())
        # Tag edits are saved as they're made; Cancel undoes them
        self.page_tags.revert()
        super().reject()

    def _on_tag_renamed(self, old: str, new: str) -> None:
        self._tag_edits.append(lambda tags, old=old, new=new: category_rules.renamed(tags, old, new))

    def _on_tag_deleted(self, name: str) -> None:
        self._tag_edits.append(lambda tags, name=name: category_rules.without(tags, name))

    def _on_tags_changed(self) -> None:
        self.page_categories.set_available_tags(self.page_tags.custom_tag_names())

    def _on_tag_category_requested(self, tag: str) -> None:
        """Tags page > right-click > Add as Sidebar Category: make it, and show it on the Categories page."""
        self.page_categories.add_tag_category(tag)
        self.show_page("Categories")

    def _sync_category_extensions(self) -> None:
        if self.page_files.is_valid():
            self.page_categories.set_available_extensions(self.page_files.extensions())

    def _on_save(self) -> None:
        if self.page_about.is_busy() or self.page_artbook.is_busy():
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
        # (A type given to an app in Settings > Apps, such as .kra: the library picks those up too)
        extensions += [e for e in dict.fromkeys(self.page_apps.types_in_use()) if e not in extensions]
        categories = self.page_categories.categories()

        theme_id = self.page_appearance.theme_id()
        thumbnail_size = self.page_appearance.thumbnail_size()

        self.folders_changed = [str(f) for f in folders] != [str(f) for f in self._orig_folders]
        self.extensions_changed = extensions != self._orig_extensions
        self.categories_changed = categories != self._orig_categories
        self.theme_changed = theme_id != self._orig_theme
        self.thumbnail_size_changed = thumbnail_size != self._orig_thumbnail_size
        self.model_thumbnails_changed = self.page_appearance.chk_model_pictures.isChecked() != get_model_view()["thumbnails"]
        self.alpha_relief_changed = self.page_appearance.btn_alpha_relief.isChecked() != get_model_view()["alpha_relief"]

        if self.folders_changed:
            set_scan_folders(folders)
        if self.extensions_changed:
            set_file_extensions(extensions)
        if self.categories_changed or self.extensions_changed:
            # Re-save categories so file types removed on the Files page are dropped
            set_categories(categories)
        for edit in self._tag_edits:  # (tags renamed or deleted: other profiles' categories follow)
            profiles.edit_other_profiles_categories(edit)
        if self.page_profiles.changed():
            self.profiles_changed = True
            problems = self.page_profiles.apply()
            if problems:
                QMessageBox.warning(self, "Profiles", "Some profile changes couldn't be made:\n\n" + "\n".join(problems))
        remove_unused_category_icons(categories)
        if self.theme_changed:
            set_theme(theme_id)
        if self.page_appearance.texture_id() != self._orig_texture:
            set_background_texture(self.page_appearance.texture_id())
        if self.page_appearance.frost() != get_glass_frost():
            set_glass_frost(self.page_appearance.frost())
        if self.thumbnail_size_changed:
            set_thumbnail_size(thumbnail_size)
        if self.model_thumbnails_changed:
            set_model_view(thumbnails=self.page_appearance.chk_model_pictures.isChecked())
        if self.alpha_relief_changed:
            set_model_view(alpha_relief=self.page_appearance.btn_alpha_relief.isChecked())
        motion = self.page_appearance.cb_motion.currentData()
        if motion != get_motion():
            set_motion(motion)
            set_motion_mode(motion)  # takes effect right away
        if self.page_video.ffmpeg_path() != self._orig_ffmpeg_path:
            set_ffmpeg_path(self.page_video.ffmpeg_path())
        if self.page_archives.seven_zip_path() != self._orig_seven_zip_path:
            set_seven_zip_path(self.page_archives.seven_zip_path())
            archives.configure_seven_zip(self.page_archives.seven_zip_path())
            self.archives_changed = True
        if self.page_archives.archive_contents() != self._orig_archive_contents:
            set_archive_contents(self.page_archives.archive_contents())
            self.archives_changed = True
        if self.page_storage.unpacked_limit_gb() != self._orig_unpacked_limit:
            set_unpacked_limit_gb(self.page_storage.unpacked_limit_gb())
            # (down to the new limit now, not at the next start: a big folder takes a moment)
            threading.Thread(target=archives.prune_extracted, name="prune-unpacked", daemon=True).start()
        page = self.page_apps
        if (
            page.found() != get_found_apps() or page.choices() != get_app_choices()
            or page.custom() != get_custom_apps() or page.types() != get_app_types()
        ):
            set_found_apps(page.found())
            set_app_choices(page.choices())
            set_custom_apps(page.custom())
            set_app_types(page.types())
            apps.load_settings()
            self.apps_changed = True
        if self.page_video.media_backend() != self._orig_media_backend:
            set_media_backend(self.page_video.media_backend())
            QMessageBox.information(
                self, "Restart Required", "The new video playback backend will be used after you restart the app."
            )

        self.accept()
