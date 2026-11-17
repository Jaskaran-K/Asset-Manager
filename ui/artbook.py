"""The Artbook: reference boards ("Pages") beside the asset library.

ArtbookPanel is the Artbook side of the main window (see ui.view_modes): its own toolbar
row above the board (ui.artbook_board), with the Pages sidebar (ui.artbook_pages) sliding
out over or beside the board. It opens the page you had open last, the first time it's
shown, and saves it about two seconds after each change (one save at a time, off the UI
thread), when you switch to another page, and once more when the app quits. Each save
also refreshes the page's thumbnail in the Pages list.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QImage, QPainter
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QLabel, QMenu, QToolButton, QVBoxLayout, QWidget

from core import artbook_share, artbook_unsorted, pur_import
from core import artbook_store as store
from core.settings import (
    add_artbook_recent_page,
    get_artbook_headers_hint_shown,
    get_artbook_page,
    get_board_link,
    get_last_export_dir,
    set_artbook_headers_hint_shown,
    set_artbook_page,
    set_board_link,
    set_last_export_dir,
)
from ui import shortcuts
from ui.artbook_board import BoardView, export_pictures
from ui.artbook_pages import PagesPanel
from ui.controls import ghost_button
from ui.duotone import DuotoneGlyph
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
    show_in_library = Signal(str)  # a file card's Show in the Library: the asset browser shows that file

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("artbookPanel")
        self.page: Optional[dict[str, Any]] = None
        self._saving = False
        self._save_again = False
        self.versions_dialog = None  # the Earlier Versions window, while it's open
        self._dirty = False  # changed since the last thumbnail
        self._imports: list[tuple[Path, Optional[str]]] = []  # boards waiting to be imported (file, folder)
        self._importing: Optional[tuple[Path, Optional[str]]] = None
        self._board_links: dict[str, tuple[str, float]] = {}  # file being imported -> the board to link its page to
        self._imported: list[str] = []  # this round's new pages
        self._import_skipped: Counter = Counter()
        self._import_errors: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("controlBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(10, 6, 10, 6)
        bar_layout.setSpacing(6)
        self.btn_pages = ghost_button("menu", "Show / hide the Pages sidebar (Ctrl+B)", checkable=True)
        bar_layout.addWidget(self.btn_pages)
        title = QLabel("Artbook")
        title.setObjectName("panelTitle")
        bar_layout.addWidget(DuotoneGlyph("view_artbook", size=20))
        bar_layout.addWidget(title)
        self.lbl_page = QLabel()
        self.lbl_page.setObjectName("mutedLabel")
        bar_layout.addWidget(self.lbl_page)
        # After a page card opened another page: back to the one it's on (as it was left)
        self.btn_back = ghost_button("back", "Back to the page you came from, where you were on it", "Back")
        self.btn_back.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_back.hide()
        self.btn_back.clicked.connect(self.go_back)
        bar_layout.addWidget(self.btn_back)
        self._back: list[tuple[str, dict[str, Any]]] = []  # (page id, its view) for each page card followed
        self._following = False  # (opening a page from a page card, or going back: the way back stays)
        bar_layout.addStretch(1)

        # Export: each format in one click, with the last settings (Export Options... changes them)
        self.btn_export = ghost_button("export", f"Export: PNG ({shortcuts.get('artbook.export_png').keys}), JPG, PDF or "
                                       f"copy it, with your last settings; Export Options... ({shortcuts.get('artbook.export_options').keys}) changes them")
        self.export_menu = QMenu(self.btn_export)
        self.export_menu.aboutToShow.connect(self._fill_export_menu)
        self.btn_export.setMenu(self.export_menu)
        self.btn_export.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        bar_layout.addWidget(self.btn_export)
        self.btn_images = ghost_button("grid", "Images: show / hide the list of every image on the page (L)", checkable=True)
        bar_layout.addWidget(self.btn_images)
        self.btn_tray = ghost_button("tray", "Tray: Unsorted (sent from your browser, waiting to go on a page) and this "
                                     "page's Trash (U)", checkable=True)
        self.btn_tray.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bar_layout.addWidget(self.btn_tray)
        self.btn_fit = ghost_button("fit", "Fit: frame the selection, or everything on the page (F)")
        bar_layout.addWidget(self.btn_fit)
        self.btn_zoom = ghost_button(None, "Current zoom. Click for 100% (Ctrl+0)", "100%")
        self.btn_zoom.setMinimumWidth(52)
        bar_layout.addWidget(self.btn_zoom)

        # The window's View / Settings buttons sit here when the Artbook is on the right
        self.toolbar_slot = QHBoxLayout()
        self.toolbar_slot.setSpacing(10)
        bar_layout.addLayout(self.toolbar_slot)
        layout.addWidget(bar)
        # Narrow (a side of Split View): icon-only buttons, no page name
        self.toolbar = CompactToolbar(self, bar, [], hide=[self.lbl_page])  # (its buttons are icons already)

        self.board = BoardView()
        self.pages = PagesPanel(self)
        self.sidebar = SidebarHost(
            self.pages, self.board,
            title="PAGES", settings_key="artbook_sidebar", header_widgets=self.pages.header_widgets(),
            close_shortcut="pages.close_sidebar", content_name="the board", picking="open a page",
        )
        self.sidebar.watch(self.board.viewport())  # floating: your images show through the glass
        self.board.rail.set_cover(self.sidebar.panel)  # (floating over the board: the tool rail moves out from under it)
        self.sidebar.open_changed.connect(self.btn_pages.setChecked)
        self.btn_pages.setChecked(self.sidebar.is_open())
        self.btn_pages.clicked.connect(self._on_pages_clicked)
        layout.addWidget(self.sidebar, 1)
        # Ctrl+B while working in the Artbook (the asset side has its own, for its sidebar)
        toggle_pages = QAction(self)
        toggle_pages.setShortcut(shortcuts.sequence("pages.sidebar"))
        toggle_pages.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        toggle_pages.triggered.connect(self.sidebar.toggle)
        # Ctrl+P: go to a page (the page switcher)
        switch = QAction(self)
        switch.setShortcut(shortcuts.sequence("pages.switch"))
        switch.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        switch.triggered.connect(self.show_switcher)
        self.addAction(switch)
        self._switcher = None
        self._hinted = False  # (the older pages' headers: said once, a little after the Artbook first shows)
        self.addAction(toggle_pages)

        self.board.changed.connect(self._schedule_save)
        self.board.boards_dropped.connect(self._on_boards_dropped)
        self.board.export_requested.connect(self.export)
        self.board.export_page_requested.connect(self._on_export_page_requested)
        self.board.page_rename_requested.connect(self._on_header_renamed)
        self.board.page_menu = self._page_menu
        self.btn_images.clicked.connect(self.board.toggle_image_list)
        self.board.image_list_toggled.connect(self.btn_images.setChecked)
        self.btn_tray.clicked.connect(self._on_tray_clicked)
        self.board.tray_toggled.connect(self.btn_tray.setChecked)
        self.board.unsorted_changed.connect(self.show_unsorted_count)
        self.board.tray_toggled.connect(self._on_tray_toggled)
        self.show_unsorted_count()
        self.board.zoom_changed.connect(self._on_zoom_changed)
        self.btn_fit.clicked.connect(self._on_fit_clicked)
        self.btn_zoom.clicked.connect(self._on_zoom_clicked)
        self.page_saved.connect(self.pages.refresh_thumbnail)
        self.page_saved.connect(self.board.refresh_page_cards)
        self.board.page_open_requested.connect(self.follow_page)
        self.board.show_in_library_requested.connect(self.show_in_library)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(AUTOSAVE_MS)
        self._save_timer.timeout.connect(self._save_in_background)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.toolbar.update()

    def _on_tray_clicked(self) -> None:
        self.board.toggle_tray()

    def _on_tray_toggled(self, _shown: bool) -> None:
        self.btn_images.setChecked(self.board.image_list_open())

    def show_unsorted_count(self) -> None:
        """The tray button: how many things wait in Unsorted (none: no number)."""
        count = artbook_unsorted.count()
        self.btn_tray.setText(str(count) if count else "")

    def unsorted_arrived(self) -> None:
        """The browser extension sent something to Unsorted."""
        self.board.unsorted_changed.emit()

    def _on_pages_clicked(self) -> None:
        self.sidebar.toggle()

    # ------------------ pages ------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.page is None:
            self.open_page(get_artbook_page())
        if not self._hinted:
            self._hinted = True
            QTimer.singleShot(4000, self._offer_headers)  # (older pages' headers: said once)

    def current_page_id(self) -> Optional[str]:
        return self.page["id"] if self.page is not None else None

    def open_page(self, page_id: str) -> None:
        """Show a page (falls back to the first one, or a new one, if it can't be opened)."""
        if self.page is not None:
            if page_id == self.page["id"]:
                return
            self.save_now()
        page = None
        problem = ""
        candidates = [page_id] if page_id else []
        candidates += [p for p in store.ordered_page_ids() if p != page_id]
        for candidate in candidates:
            try:
                page = store.load_page(candidate)
                break
            except store.PageError as exc:
                problem = problem or (str(exc) if candidate == page_id else "")
                continue
        if page is None:
            page = store.create_page(DEFAULT_PAGE_NAME)
        if store.take_restored(page["id"]) and not problem:  # (the page asked for comes first)
            problem = ("This page's file was damaged, so its previous save was opened instead: "
                       "the last changes made to it may be missing.")
        store.remove_unused_images(page)  # (before there's undo history that could need them)
        self.page = page
        self._dirty = False
        set_artbook_page(page["id"])
        add_artbook_recent_page(page["id"])
        self._show_name()
        self.board.load_page(page)
        self.pages.rebuild()
        if not self._following:
            self._back.clear()  # (a page opened another way: no way back)
        self._show_back()
        if problem:
            self.board.show_message(problem)

    def follow_page(self, page_id: str) -> None:
        """A page card was opened: show its page, with Back (at the top) to return to this one as it is now."""
        if self.page is None or page_id == self.page["id"]:
            return
        self._back.append((self.page["id"], self.board.view_state()))
        del self._back[:-20]
        self._following = True
        try:
            self.open_page(page_id)
        finally:
            self._following = False
        if self.current_page_id() != page_id:  # (it couldn't be opened: no way back to keep)
            self._back.pop()
            self._show_back()

    def go_back(self) -> None:
        """Back: the page the last page card was on, where it was left."""
        if not self._back:
            return
        page_id, view = self._back.pop()
        self._following = True
        try:
            self.open_page(page_id)
        finally:
            self._following = False
        if self.current_page_id() == page_id:
            self.board.set_view(float(view.get("zoom", 1.0)), QPointF(float(view.get("x", 0)), float(view.get("y", 0))))
        self._show_back()

    def _show_back(self) -> None:
        if not self._back:
            self.btn_back.hide()
            return
        summary = store.page_summary(self._back[-1][0])
        name = (summary or {}).get("name") or "the last page"
        self.btn_back.setText(f"Back to {name}" if len(name) <= 40 else f"Back to {name[:38]}...")
        self.btn_back.show()

    def leave_pages(self) -> None:
        """Save and let go of the open page (before the pages folder changes; reload_pages after)."""
        self.save_now()
        self._save_timer.stop()

    def reload_pages(self) -> None:
        """Show the pages from the pages folder again (it changed): the same page if it's there."""
        current = self.current_page_id() or get_artbook_page()
        self.page = None
        self.open_page(current)

    def _show_name(self) -> None:
        self.lbl_page.setText(f"·  {self.page.get('name') or 'Untitled'}" if self.page else "")

    def new_page(self, folder_id: Optional[str] = None, name: Optional[str] = None) -> str:
        """Create a page ("Page N", or name) in a folder and open it, with its header; returns its id."""
        page = store.create_page(name or store.unique_page_name(), folder_id)
        self.open_page(page["id"])
        self.board.add_page_title(QPointF(0, 0), record=False)
        self.save_now()
        return page["id"]

    def rename_page(self, page_id: str, name: str) -> None:
        if page_id == self.current_page_id():
            self.page["name"] = store.clean_name(name)
            self.board.set_page_name(self.page["name"])  # (its header follows)
            self.save_now()  # this copy is newer than the file: it carries the name
            self._show_name()
        else:
            store.rename_page(page_id, name)

    def create_page(self, kind: str, template_id: str, name: str, folder_id: Optional[str] = None) -> None:
        """A page from the New Page window: blank (with its header), from a built-in template, or from one of yours
        (made off the UI thread: it may hold pictures)."""
        from core import artbook_templates as templates
        from ui import artbook_templates

        try:
            if kind == "built_in":
                page = artbook_templates.create_built_in(template_id, name, folder_id)
                self.open_page(page["id"])
                self.board.frame()
            elif kind == "yours":
                self.board.show_message("Making the page...")
                run_job(lambda: templates.page_from_template(template_id, name, folder_id),
                        self._on_template_page_made, lambda error: self.board.show_message(f"Couldn't make the page: {error}"))
                return
            else:
                self.new_page(folder_id, name)
        except store.PageError as exc:
            self.board.show_message(str(exc))
            return
        self.pages.rebuild()

    def _on_template_page_made(self, page: dict[str, Any]) -> None:
        self.open_page(page["id"])
        self.board.frame()
        self.board.toast.hide()
        self.pages.rebuild()

    def save_as_template(self, page_id: Optional[str] = None) -> None:
        """Right-click a page > Save as Template: its name, whether to keep its pictures and untick its to-dos."""
        from core import artbook_templates as templates
        from ui import artbook_templates

        page_id = page_id or self.current_page_id()
        if page_id is None:
            return
        if page_id == self.current_page_id():
            self.save_now()  # (what's on the board, not the last autosave)
        summary = store.page_summary(page_id) or {}
        picked = artbook_templates.ask_save_template(self.window(), summary.get("name") or "Page")
        if picked is None:
            return
        name, keep, untick = picked
        run_job(lambda: templates.save_template(page_id, name, keep, untick),
                lambda entry: self.board.show_message(f"Saved the template “{entry['name']}”: New Page lists it."),
                lambda error: self.board.show_message(f"Couldn't save the template: {error}"))

    # ------------------ going to a page ------------------
    def show_switcher(self) -> None:
        """Ctrl+P: the page switcher (type a page's name; Enter opens it)."""
        from ui.artbook_switcher import PageSwitcher

        if self._switcher is None:
            self._switcher = PageSwitcher(self)
        self._switcher.open()

    # ------------------ headers for older pages ------------------
    @staticmethod
    def pages_without_header() -> list[str]:
        """The pages (by id) that have no header (pages made before headers came, mostly)."""
        out = []
        for page_id in store.ordered_page_ids():
            try:
                page = store.load_page(page_id)
            except store.PageError:
                continue
            if not any(isinstance(d, dict) and d.get("kind") == "note" and d.get("style") == "title" for d in page.get("items", [])):
                out.append(page_id)
        return out

    def add_headers_to_all_pages(self) -> int:
        """Pages > Add Headers to Every Page...: a header (the page's name, what's on it) above what's on each page
        that has none, after asking; how many got one."""
        from PySide6.QtWidgets import QMessageBox

        if self.page is not None:
            self.save_now()
        missing = self.pages_without_header()
        if not missing:
            self.board.show_message("Every page has a header already.")
            return 0
        answer = QMessageBox.question(
            self.window(), "Add Headers",
            f"{len(missing)} page{'s have' if len(missing) != 1 else ' has'} no header. Add one to each, above what's on it "
            "(its name, a line about it, what's on it)?")
        if answer != QMessageBox.StandardButton.Yes:
            return 0
        return self.add_headers(missing)

    def add_headers(self, page_ids: list[str]) -> int:
        from ui.artbook_board import page_header_dict

        done = 0
        for page_id in page_ids:
            if page_id == self.current_page_id():
                if self.board.title_note() is None:
                    self.board.add_page_title(record=False)
                    self.save_now()
                    done += 1
                continue
            try:
                page = store.load_page(page_id)
                page["items"] = [page_header_dict(page)] + list(page.get("items", []))
                store.save_page_now(page)
                done += 1
            except (store.PageError, OSError):
                continue
        self.board.show_message(f"Added a header to {done} page{'s' if done != 1 else ''}.")
        return done

    def _offer_headers(self) -> None:
        """Once: older pages have no header; say how to add them all."""
        if get_artbook_headers_hint_shown():
            return

        def check() -> int:
            return len(self.pages_without_header())

        def done(count: int) -> None:
            if count and not get_artbook_headers_hint_shown():
                set_artbook_headers_hint_shown()
                self.board.show_message(f"{count} older page{'s have' if count != 1 else ' has'} no header (its name and "
                                        "what's on it): right-click > Pages > Add Headers to Every Page adds them.")

        run_job(check, done, lambda _error: None)

    def _on_header_renamed(self, name: str) -> None:
        """The page's name was typed in its header (or undone there): the page is renamed."""
        if self.page is None:
            return
        try:
            self.rename_page(self.page["id"], name)
        except store.PageError:
            return
        self.pages.rebuild()

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

    def show_versions(self, page_id: Optional[str] = None) -> None:
        """The Earlier Versions window for a page (default: the open one)."""
        from ui.artbook_versions import VersionsDialog

        page_id = page_id or self.current_page_id()
        if page_id is None:
            return
        if page_id == self.current_page_id():
            self.save_now()  # (today's first change keeps its version now, not in a few seconds)
            name = self.page.get("name") or "Untitled"
        else:
            name = (store.page_summary(page_id) or {}).get("name") or "Untitled"
        dialog = VersionsDialog(self, page_id, name)
        dialog.finished.connect(dialog.deleteLater)
        dialog.open()
        self.versions_dialog = dialog

    def restore_version(self, page_id: str, stamp: str, as_new: bool = False) -> bool:
        """Restore one of a page's earlier versions in place (it reopens if it's the open page), or as
        a new page that opens. False (and says why) if it couldn't."""
        current = page_id == self.current_page_id()
        if current:
            self.save_now()
        try:
            if as_new:
                page = store.restore_version_as_new(page_id, stamp)
                self.open_page(page["id"])
                return True
            store.restore_version(page_id, stamp)
        except (store.PageError, OSError) as exc:
            self.board.show_message(f"Couldn't restore that version: {exc}")
            return False
        if current:
            self._save_timer.stop()
            self.page = None  # (what's on the board is now a version: reopen without saving it)
            self.open_page(page_id)
        else:
            self.pages.rebuild()
        self.page_saved.emit(page_id)
        return True

    def _page_menu(self) -> list:
        """The board's right-click Pages submenu (entries as BoardView._fill_menu takes them)."""
        return [
            ("New Page", None, self._menu_new_page, True),
            ("Duplicate Page", None, self._menu_duplicate_page, self.page is not None),
            ("Rename Page...", None, self._menu_rename_page, self.page is not None),
            ("Earlier Versions...", None, self.show_versions, self.page is not None),
            ("Save as Template...", None, self.save_as_template, self.page is not None),
            ("Go to Page...", "pages.switch", self.show_switcher, True),
            ("Add Headers to Every Page...", None, self.add_headers_to_all_pages, True),
            None,
            ("Export Page (.artbook)...", None, self._on_export_page_requested, self.page is not None),
        ]

    def _menu_new_page(self) -> None:
        """A new page beside this one (in its folder), its name ready to type over in the sidebar."""
        self.sidebar.set_open(True)
        self.pages.show_current()
        self.pages.new_page(self.pages.current_folder())

    def _menu_duplicate_page(self) -> None:
        if self.page is not None:
            self.pages.duplicate(self.page["id"])
            self.board.show_message(f"Duplicated as \"{(self.page.get('name') or 'Untitled')} copy\" (in Pages)")

    def _menu_rename_page(self) -> None:
        self.sidebar.set_open(True)
        self.pages.show_current()
        self.pages.rename_selected()

    # ------------------ importing boards ------------------
    def import_boards(self, paths: list[Path], folder_id: Optional[str] = None) -> None:
        """Make a page from each PureRef board, one at a time in the background; the last one opens."""
        self._imports += [(Path(p), folder_id) for p in paths]
        if self._importing is None:
            self._import_next()

    def open_board(self, file: Path, board: str, modified: float, again: bool = False) -> None:
        """Show the page a board in the library opens as: the one it's linked to, or a page named like
        it (imported earlier by hand: linked from now on), or else a new one imported from file. board:
        what it's known by in the settings; again: a new page even if it has one. Edits on a page are
        never overwritten: a board that changed since says so, and Import Again makes a new page."""
        pages = {p["id"]: p.get("name") or "" for p in store.list_pages()}
        link = None if again else get_board_link(board)
        if link is not None and link["page"] in pages:
            self.open_page(link["page"])
            if link.get("modified") != modified:
                self.board.show_message(
                    f"{file.stem} has changed since it became this page. Right-click it in the grid > Import Again makes a new page."
                )
            return
        if not again:
            stem = " ".join(file.stem.split()).casefold()
            named = next((page_id for page_id, name in pages.items() if name.casefold() == stem), None)
            if named is not None:
                set_board_link(board, named, modified)
                self.open_page(named)
                return
        self._board_links[str(file)] = (board, modified)
        self.import_boards([file])

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
        link = self._board_links.pop(str(self._importing[0]), None) if self._importing else None
        if link is not None:
            set_board_link(link[0], page["id"], link[1])
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
        if skipped["drawing"]:
            parts.append(f"Left out {skipped['drawing']} drawing{'s' if skipped['drawing'] != 1 else ''} that couldn't be read.")
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

    def _fill_export_menu(self) -> None:
        """The top bar's Export menu: each format with the last settings (shown), the options, the .artbook file."""
        from ui.artbook_export import settings_text

        menu = self.export_menu
        menu.clear()
        has = self.page is not None and bool(self.board.items_list())
        for text, fmt, shortcut_id in (("Export as PNG...", "png", "artbook.export_png"), ("Export as JPG...", "jpg", None),
                                       ("Export as PDF...", "pdf", None), ("Copy as Image", "copy", "artbook.copy_image")):
            action = menu.addAction(text)
            if shortcut_id:
                action.setShortcut(shortcuts.sequence(shortcut_id))  # (a hint: the board handles the key)
            action.setEnabled(has)
            action.triggered.connect(lambda _checked=False, fmt=fmt: self.export(fmt))
        menu.addSeparator()
        menu.addSection("Using your last settings")
        info = menu.addAction(settings_text())
        info.setEnabled(False)
        options = menu.addAction("Export Options...")
        options.setShortcut(shortcuts.sequence("artbook.export_options"))
        options.setEnabled(has)
        options.triggered.connect(lambda: self.export("options"))
        menu.addSeparator()
        page = menu.addAction("Export Page (.artbook)...")
        page.setEnabled(self.page is not None)
        page.triggered.connect(self._on_export_page_requested)

    def export(self, fmt: str, area: str = "") -> None:
        """Export what's on the board: fmt png / jpg / pdf (asks where), copy (to the clipboard), or options (the
        Export dialog first). With the last export settings; area: a different what (the selection, say)."""
        from core.settings import get_artbook_export

        if self.page is None:
            return
        if fmt == "options":
            self.show_export_dialog()
            return
        options = get_artbook_export()
        if area:
            options["area"] = area
        plan = self.board.export_plan(options["area"], options["size"], options["title"])
        if plan is None:
            self.board.show_message("Nothing to export: the board is empty.")
            return
        path = None
        if fmt != "copy":
            stem = self.page.get("name") or "Page"
            suffix, file_filter = {"png": (".png", "PNG image (*.png)"), "jpg": (".jpg", "JPG image (*.jpg)"),
                                   "pdf": (".pdf", "PDF document (*.pdf)")}[fmt]
            path = self._ask_save_path(f"Export as {fmt.upper()}", stem, suffix, file_filter)
            if path is None:
                return
            self.board.show_message(f"Exporting {path.name}...")
        page_id, requests = self.page["id"], plan[3]
        # The sharp pictures are decoded in the background (the board may only have small copies)
        run_job(lambda: export_pictures(page_id, requests), lambda pictures: self._save_export(fmt, path, pictures, options),
                self._on_export_failed)

    def show_export_dialog(self) -> None:
        """Export Options... (Ctrl+Shift+E): the Export dialog; then export (or copy) as chosen."""
        from ui.artbook_export import ExportDialog

        if self.page is None or not self.board.items_list():
            self.board.show_message("Nothing to export: the board is empty.")
            return
        dialog = ExportDialog(self.board, self.window())
        try:
            if dialog.exec() != ExportDialog.DialogCode.Accepted:
                return
            options = dialog.options()
            action = dialog.action
        finally:
            dialog.deleteLater()
        self.export("copy" if action == "copy" else options["format"], options["area"])

    def _save_export(self, fmt: str, path: Optional[Path], pictures: dict, options: dict) -> None:
        board = self.board
        background = "white" if fmt == "jpg" and options["background"] == "clear" else options["background"]
        if fmt == "pdf":
            try:
                board.export_pdf(path, pictures, options["area"], options["size"], options["title"], background,
                                 options["pins"], options["links"])
            except (store.PageError, OSError) as exc:
                self._on_export_failed(str(exc))
                return
            self._on_exported(path.name)
            return
        image = board.export_image(pictures, options["area"], options["size"], options["title"], background, options["pins"])
        if image is None:
            board.show_message("Nothing to export: the board is empty.")
            return
        if fmt == "copy":
            QApplication.clipboard().setImage(image)
            board.show_message(f"Copied: {image.width()} × {image.height()} px, ready to paste")
            return
        if fmt == "jpg":
            flat = QImage(image.size(), QImage.Format.Format_RGB32)  # (no see-through parts in a JPG)
            flat.fill(Qt.GlobalColor.white)
            painter = QPainter(flat)
            painter.drawImage(0, 0, image)
            painter.end()
            image = flat

        def save() -> str:
            temp = path.with_name(path.name + ".tmp")  # (whole, or not there)
            if not image.save(str(temp), "JPG" if fmt == "jpg" else "PNG", 92 if fmt == "jpg" else -1):
                temp.unlink(missing_ok=True)
                raise store.PageError(f"Couldn't save {path.name}.")
            os.replace(temp, path)
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
        page["groups"] = self.board.page_groups()
        page["comments"] = self.board.page_comments(saving=True)
        page["trash"] = self.board.page_trash()
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
