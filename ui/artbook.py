"""The Artbook: reference boards ("Pages") beside the asset library.

ArtbookPanel is the Artbook side of the main window (see ui.view_modes): its own toolbar
row above the board (ui.artbook_board), with the Pages sidebar (ui.artbook_pages) sliding
out over or beside the board. It opens the page you had open last, the first time it's
shown, and saves it about two seconds after each change (one save at a time, off the UI
thread), when you switch to another page, and once more when the app quits. Each save
also refreshes the page's thumbnail in the Pages list.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QImage
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core import artbook_share, pur_import
from core import artbook_store as store
from core.settings import get_artbook_page, get_last_export_dir, set_artbook_page, set_last_export_dir
from ui import shortcuts
from ui.artbook_board import BoardView
from ui.artbook_pages import PagesPanel
from ui.duotone import DuotoneGlyph, set_icon
from ui.sidebar import SidebarHost
from ui.tag_widgets import run_job
from ui.toolbar import CompactToolbar

AUTOSAVE_MS = 2000
DEFAULT_PAGE_NAME = "Page 1"
BOARD_SUFFIXES = (artbook_share.SUFFIX, pur_import.SUFFIX)  # files that open as pages


def import_file(path: Path) -> tuple[dict[str, Any], Counter]:
    """A page from a shared .artbook page or a PureRef board: (the page, what was left out). Off the UI thread."""
    if path.suffix.lower() == artbook_share.SUFFIX:
        return artbook_share.import_page(path), Counter()
    if path.suffix.lower() == pur_import.SUFFIX:
        return pur_import.import_board(path)
    raise store.PageError(f"{path.name} isn't a board file (.artbook or .pur).")


def _unique_file(folder: Path, stem: str, suffix: str) -> str:
    """A file name in folder that isn't taken yet: stem.suffix, else stem 2.suffix..."""
    stem = "".join("_" if c in r'<>:"/\|?*' else c for c in stem).strip(" .") or "Page"  # (not allowed in file names)
    candidate, number = folder / f"{stem}{suffix}", 2
    while candidate.exists():
        candidate, number = folder / f"{stem} {number}{suffix}", number + 1
    return str(candidate)


class ArtbookPanel(QWidget):
    """Toolbar plus board and Pages sidebar. toolbar_slot is where the window's shared controls can go."""

    page_saved = Signal(str)  # a page (and its thumbnail) was written

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("artbookPanel")
        self.page: Optional[dict[str, Any]] = None
        self._saving = False
        self._save_again = False
        self._dirty = False  # changed since the last thumbnail
        self._imports: list[tuple[Path, Optional[str]]] = []  # boards waiting to be imported (file, folder)
        self._importing: Optional[tuple[Path, Optional[str]]] = None
        self._imported: list[str] = []  # this round's new pages
        self._import_skipped: Counter = Counter()
        self._import_errors: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("controlBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(12, 8, 12, 8)
        bar_layout.setSpacing(10)
        self.btn_pages = QPushButton()
        set_icon(self.btn_pages, "menu", size=18)
        self.btn_pages.setCheckable(True)
        self.btn_pages.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pages.setToolTip("Show / hide the Pages sidebar (Ctrl+B)")
        bar_layout.addWidget(self.btn_pages)
        title = QLabel("Artbook")
        title.setObjectName("panelTitle")
        bar_layout.addWidget(DuotoneGlyph("view_artbook", size=20))
        bar_layout.addWidget(title)
        self.lbl_page = QLabel()
        self.lbl_page.setObjectName("mutedLabel")
        bar_layout.addWidget(self.lbl_page)
        bar_layout.addStretch(1)

        self.btn_fit = QPushButton("Fit")
        set_icon(self.btn_fit, "fit")
        self.btn_fit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_fit.setToolTip("Frame the selection, or everything on the page (F)")
        bar_layout.addWidget(self.btn_fit)
        self.btn_zoom = QPushButton("100%")
        self.btn_zoom.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_zoom.setToolTip("Current zoom. Click for 100% (Ctrl+0)")
        bar_layout.addWidget(self.btn_zoom)

        # The window's View / Settings buttons sit here when the Artbook is on the right
        self.toolbar_slot = QHBoxLayout()
        self.toolbar_slot.setSpacing(10)
        bar_layout.addLayout(self.toolbar_slot)
        layout.addWidget(bar)
        # Narrow (a side of Split View): icon-only buttons, no page name
        self.toolbar = CompactToolbar(self, bar, [(self.btn_fit, "Fit")], hide=[self.lbl_page])

        self.board = BoardView()
        self.pages = PagesPanel(self)
        self.sidebar = SidebarHost(
            self.pages, self.board,
            title="PAGES", settings_key="artbook_sidebar", header_widgets=self.pages.header_widgets(),
            close_shortcut="pages.close_sidebar", content_name="the board", picking="open a page",
        )
        self.sidebar.watch(self.board.viewport())  # floating: your images show through the glass
        self.sidebar.open_changed.connect(self.btn_pages.setChecked)
        self.btn_pages.setChecked(self.sidebar.is_open())
        self.btn_pages.clicked.connect(self._on_pages_clicked)
        layout.addWidget(self.sidebar, 1)
        # Ctrl+B while working in the Artbook (the asset side has its own, for its sidebar)
        toggle_pages = QAction(self)
        toggle_pages.setShortcut(shortcuts.sequence("pages.sidebar"))
        toggle_pages.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        toggle_pages.triggered.connect(self.sidebar.toggle)
        self.addAction(toggle_pages)

        self.board.changed.connect(self._schedule_save)
        self.board.boards_dropped.connect(self._on_boards_dropped)
        self.board.export_image_requested.connect(self.export_image)
        self.board.export_page_requested.connect(self._on_export_page_requested)
        self.board.zoom_changed.connect(self._on_zoom_changed)
        self.btn_fit.clicked.connect(self._on_fit_clicked)
        self.btn_zoom.clicked.connect(self._on_zoom_clicked)
        self.page_saved.connect(self.pages.refresh_thumbnail)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(AUTOSAVE_MS)
        self._save_timer.timeout.connect(self._save_in_background)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.toolbar.update()

    def _on_pages_clicked(self) -> None:
        self.sidebar.toggle()

    # ------------------ pages ------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.page is None:
            self.open_page(get_artbook_page())

    def current_page_id(self) -> Optional[str]:
        return self.page["id"] if self.page is not None else None

    def open_page(self, page_id: str) -> None:
        """Show a page (falls back to the first one, or a new one, if it can't be opened)."""
        if self.page is not None:
            if page_id == self.page["id"]:
                return
            self.save_now()
        page = None
        candidates = [page_id] if page_id else []
        candidates += [p for p in store.ordered_page_ids() if p != page_id]
        for candidate in candidates:
            try:
                page = store.load_page(candidate)
                break
            except store.PageError:
                continue
        if page is None:
            page = store.create_page(DEFAULT_PAGE_NAME)
        self.page = page
        self._dirty = False
        set_artbook_page(page["id"])
        self._show_name()
        self.board.load_page(page)
        self.pages.rebuild()

    def _show_name(self) -> None:
        self.lbl_page.setText(f"·  {self.page.get('name') or 'Untitled'}" if self.page else "")

    def new_page(self, folder_id: Optional[str] = None) -> str:
        """Create a page ("Page N") in a folder and open it; returns its id."""
        page = store.create_page(store.unique_page_name(), folder_id)
        self.open_page(page["id"])
        return page["id"]

    def rename_page(self, page_id: str, name: str) -> None:
        if page_id == self.current_page_id():
            self.page["name"] = store.clean_name(name)
            self.save_now()  # this copy is newer than the file: it carries the name
            self._show_name()
        else:
            store.rename_page(page_id, name)

    def duplicate_page(self, page_id: str) -> dict[str, Any]:
        if page_id == self.current_page_id():
            self.save_now()  # copy what's on the board, not the last autosave
        return store.duplicate_page(page_id)

    def delete_page(self, page_id: str) -> None:
        """Send a page to the Recycle Bin. If it's the open one, the next page opens (or a new one)."""
        order = store.ordered_page_ids()
        store.trash_page(page_id)
        if page_id != self.current_page_id():
            return
        self._save_timer.stop()
        self.page = None  # don't save it on the way out
        position = order.index(page_id) if page_id in order else 0
        rest = [p for p in order if p != page_id]
        self.open_page(rest[min(position, len(rest) - 1)] if rest else "")

    # ------------------ importing boards ------------------
    def import_boards(self, paths: list[Path], folder_id: Optional[str] = None) -> None:
        """Make a page from each PureRef board, one at a time in the background; the last one opens."""
        self._imports += [(Path(p), folder_id) for p in paths]
        if self._importing is None:
            self._import_next()

    def _on_boards_dropped(self, paths: list) -> None:
        self.import_boards([Path(p) for p in paths])

    def importing(self) -> bool:
        return self._importing is not None

    def _import_next(self) -> None:
        if not self._imports:
            self._importing = None
            self._finish_imports()
            return
        self._importing = self._imports.pop(0)
        path = self._importing[0]
        left = len(self._imports)
        self.board.show_message(f"Importing {path.stem}..." + (f" ({left} more after it)" if left else ""))
        run_job(lambda: import_file(path), self._on_board_imported, self._on_board_import_failed)

    def _on_board_imported(self, result: tuple[dict[str, Any], Counter]) -> None:
        page, skipped = result
        folder_id = self._importing[1] if self._importing else None
        try:
            store.page_tree()  # (files the new page in the index, at the top level)
            if folder_id is not None:
                store.move_page(page["id"], folder_id)
        except store.PageError:
            pass  # (the folder went: it stays at the top level)
        self._imported.append(page["id"])
        self._import_skipped.update(skipped)
        self.pages.rebuild()
        self._import_next()

    def _on_board_import_failed(self, error: str) -> None:
        self._import_errors.append(error)
        self._import_next()

    def _finish_imports(self) -> None:
        imported, errors, skipped = self._imported, self._import_errors, self._import_skipped
        self._imported, self._import_errors, self._import_skipped = [], [], Counter()
        if imported:
            self.open_page(imported[-1])
            self.board.frame()
        parts = []
        if imported:
            parts.append(f"Imported {len(imported)} board{'s' if len(imported) != 1 else ''}.")
        if skipped["group"]:
            parts.append("Groups were ungrouped (everything kept its place).")
        if skipped["drawing"]:
            parts.append(f"Left out {skipped['drawing']} drawing{'s' if skipped['drawing'] != 1 else ''} (not on the Artbook yet).")
        if skipped["missing image"]:
            parts.append(f"{skipped['missing image']} image{'s' if skipped['missing image'] != 1 else ''} had no picture saved and were left out.")
        parts += errors
        if parts:
            self.board.show_message(" ".join(parts))

    # ------------------ exporting ------------------
    def _ask_save_path(self, title: str, stem: str, suffix: str, file_filter: str) -> Optional[Path]:
        folder = Path(get_last_export_dir() or Path.home())
        if not folder.is_dir():
            folder = Path.home()
        path, _filter = QFileDialog.getSaveFileName(self, title, _unique_file(folder, stem, suffix), file_filter)
        if not path:
            return None
        path = Path(path)
        if path.suffix.lower() != suffix:
            path = path.with_name(path.name + suffix)
        set_last_export_dir(str(path.parent))
        return path

    def export_image(self) -> None:
        """Ctrl+E: save the selection (or the whole board) as a PNG."""
        if self.page is None:
            return
        image = self.board.export_image()
        if image is None:
            self.board.show_message("Nothing to export: the board is empty.")
            return
        stem = self.page.get("name") or "Page"
        path = self._ask_save_path("Export as PNG", stem, ".png", "PNG image (*.png)")
        if path is None:
            return
        self.board.show_message(f"Exporting {path.name} ({image.width()} x {image.height()})...")

        def save() -> str:
            if not image.save(str(path), "PNG"):
                raise store.PageError(f"Couldn't save {path.name}.")
            return path.name

        run_job(save, self._on_exported, self._on_export_failed)

    def _on_export_page_requested(self) -> None:
        if self.page is not None:
            self.export_page(self.page["id"])

    def export_page(self, page_id: str) -> None:
        """Share a page: save it as an .artbook file (its images inside)."""
        if page_id == self.current_page_id():
            self.save_now()  # the file gets what's on the board
        summary = store.page_summary(page_id)
        stem = (summary or {}).get("name") or "Page"
        path = self._ask_save_path("Export Page", stem, artbook_share.SUFFIX, f"Artbook page (*{artbook_share.SUFFIX})")
        if path is None:
            return
        self.board.show_message(f"Exporting {path.name}...")

        def save() -> str:
            missing = artbook_share.export_page(page_id, path)
            return path.name + (f" ({missing} missing image{'s' if missing != 1 else ''} left as missing)" if missing else "")

        run_job(save, self._on_exported, self._on_export_failed)

    def _on_exported(self, name: str) -> None:
        self.board.show_message(f"Exported {name}")

    def _on_export_failed(self, error: str) -> None:
        self.board.show_message(error)

    def _snapshot(self) -> dict[str, Any]:
        page = dict(self.page)
        page["items"] = self.board.page_items()
        page["view"] = self.board.view_state()
        page["modified"] = time.time()
        return page

    # ------------------ saving ------------------
    def _schedule_save(self) -> None:
        self._dirty = True
        self._save_timer.start()

    def _take_thumbnail(self) -> tuple[bool, Optional[QImage]]:
        """(whether to write a thumbnail, its PNG): after changes, or if the page has none yet."""
        if not self._dirty and store.thumbnail_path(self.page["id"]).exists():
            return False, None
        self._dirty = False
        image = self.board.thumbnail()  # drawn here (the scene lives on the UI thread), encoded later
        return True, image

    def _save_in_background(self) -> None:
        if self.page is None:
            return
        if self._saving:
            self._save_again = True  # after the one in flight: saves never overtake each other
            return
        page = self._snapshot()
        self.page = page
        self._saving = True
        write_thumb, image = self._take_thumbnail()

        def save() -> str:
            store.save_page(page)
            if write_thumb:
                store.save_thumbnail(page["id"], store.encode_png(image) if image is not None else None)
            return page["id"]

        run_job(save, self._on_saved, self._on_save_failed)

    def _on_saved(self, page_id: str) -> None:
        self._saving = False
        self.page_saved.emit(page_id)
        if self._save_again:
            self._save_again = False
            self._save_in_background()

    def _on_save_failed(self, error: str) -> None:
        self._saving = False
        self.board.show_message(f"Couldn't save the page: {error}")

    def save_now(self) -> None:
        """Save straight away (quitting, switching page). Call once background jobs have finished."""
        if self.page is None:
            return
        self._save_timer.stop()
        self.page = self._snapshot()
        try:
            store.save_page(self.page)
            write_thumb, image = self._take_thumbnail()
            if write_thumb:
                store.save_thumbnail(self.page["id"], store.encode_png(image) if image is not None else None)
        except OSError as exc:
            self.board.show_message(f"Couldn't save the page: {exc}")
            return
        self.page_saved.emit(self.page["id"])

    # ------------------ toolbar ------------------
    def _on_fit_clicked(self) -> None:
        self.board.frame()
        self.board.setFocus()

    def _on_zoom_clicked(self) -> None:
        self.board.animate_view(1.0, self.board.view_center())
        self.board.setFocus()

    def _on_zoom_changed(self, zoom: float) -> None:
        self.btn_zoom.setText(f"{zoom * 100:.0f}%")
