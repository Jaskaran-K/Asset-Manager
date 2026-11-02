"""The grid's right-click menu, and sending pictures to Artbook pages.

ArtbookSender also serves the viewers: the PDF viewer gets it as its `artbook` and calls
fill_artbook_menu / send_image_to_artbook for its pages.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import QMenu, QWidget

from core import artbook_store
from core.scanner import AssetItem
from core.settings import get_artbook_page
from core.tag_repository import Marks
from ui import file_actions, shortcuts
from ui.asset_grid import rating_text
from ui.duotone import set_icon

# Rating keys on the grid (0 clears), and the menu's Rating entries
RATING_SHORTCUTS = (
    ("main.rate_0", 0), ("main.rate_1", 1), ("main.rate_2", 2),
    ("main.rate_3", 3), ("main.rate_4", 4), ("main.rate_5", 5),
)


class ArtbookSender:
    """Adds pictures to an Artbook page (a new one, or any page), which becomes the open page."""

    def __init__(self, panel, status: Callable[[str], None]):
        self.panel = panel  # ui.artbook.ArtbookPanel
        self._status = status  # shows a message in the status line

    def add_send_menu(self, menu: QMenu, paths: Sequence[Path]) -> None:
        """Send to Artbook: a new page, or any page (in its folders). Image files only."""
        paths = [p for p in paths if p.suffix.lower() in artbook_store.IMAGE_EXTENSIONS]
        send = menu.addMenu("Send to Artbook" if paths else "Send to Artbook (images only)")
        send.setEnabled(bool(paths))
        if paths:
            self.fill_artbook_menu(send, lambda page_id: self.send_files(paths, page_id))

    def fill_artbook_menu(self, send: QMenu, on_pick: Callable[[Optional[str]], None]) -> None:
        """Fill a "Send to Artbook" menu: New Page, then every page (in its folders). on_pick(page id,
        or None for a new page). Also used by the PDF viewer (ui.pdf_viewer) for its pages."""
        action_new = send.addAction("New Page")
        set_icon(action_new, "new_page")
        action_new.triggered.connect(lambda: on_pick(None))
        send.addSeparator()
        names = {p["id"]: p.get("name") or "Untitled" for p in artbook_store.list_pages()}
        current = self.panel.current_page_id() or get_artbook_page()

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

    def send_files(self, paths: list[Path], page_id: Optional[str]) -> None:
        """Add image files to an Artbook page (None: a new one), which becomes the open page."""
        error = self._open_page(page_id)
        if error:
            self._status(error)
            return
        count = self.panel.board.add_files(paths)
        self._status(self._sent_message(count))

    def send_image_to_artbook(self, image: QImage, name: str, page_id: Optional[str]) -> str:
        """Add a picture (a PDF page) to an Artbook page (None: a new one); says how it went."""
        error = self._open_page(page_id)
        if error:
            return error
        self.panel.board.add_image(image, name=name)
        message = self._sent_message(1)
        self._status(message)
        return message

    def _open_page(self, page_id: Optional[str]) -> str:
        """Open an Artbook page to send things to (None: a new one): "" or what went wrong."""
        panel = self.panel
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
        name = self.panel.page.get("name") or "Untitled"
        hint = " Ctrl+2 shows the Artbook." if not self.panel.isVisible() else ""
        return f'Sent {count} image{"s" if count != 1 else ""} to "{name}".{hint}'


@dataclass(frozen=True)
class AssetMenuActions:
    """What the grid's menu entries do (the main window's methods)."""

    open_viewer: Callable[[], None]
    reveal: Callable[[], None]
    tag: Callable[[], None]
    toggle_favorite: Callable[[], None]
    set_rating: Callable[[int], None]


def build_asset_menu(
    parent: QWidget,
    asset: AssetItem,
    selection: Sequence[AssetItem],
    marks: Sequence[Marks],
    actions: AssetMenuActions,
    sender: ArtbookSender,
) -> QMenu:
    """The right-click menu for a card (asset: the one clicked; selection: the selected files,
    marks: theirs). The caller shows it, then frees it (deleteLater): its actions belong to it."""
    menu = QMenu(parent)

    action_view = menu.addAction("Preview in Asset Viewer")
    font = action_view.font()
    font.setBold(True)
    action_view.setFont(font)
    action_view.triggered.connect(actions.open_viewer)

    menu.addSeparator()

    action_open = menu.addAction("Open with External App")
    action_open.triggered.connect(lambda: file_actions.open_file(asset.path))

    action_reveal = menu.addAction("Show in File Explorer")
    action_reveal.triggered.connect(actions.reveal)

    menu.addSeparator()

    selected = len(selection)
    action_tag = menu.addAction("Tag..." if selected <= 1 else f"Tag {selected} Files...")
    action_tag.setShortcut(shortcuts.sequence("main.tag"))
    action_tag.triggered.connect(actions.tag)

    all_favorite = bool(marks) and all(m.favorite for m in marks)
    action_fav = menu.addAction("Remove from Favorites" if all_favorite else "Add to Favorites")
    action_fav.setShortcut(shortcuts.sequence("main.favorite"))
    action_fav.triggered.connect(actions.toggle_favorite)

    rating_menu = menu.addMenu("Rating")
    current = {m.rating for m in marks}
    for shortcut_id, rating in RATING_SHORTCUTS[1:] + RATING_SHORTCUTS[:1]:
        action = rating_menu.addAction(rating_text(rating) if rating else "No Rating")
        action.setShortcut(shortcuts.sequence(shortcut_id))
        action.setCheckable(True)
        action.setChecked(current == {rating})
        action.triggered.connect(lambda _checked=False, r=rating: actions.set_rating(r))

    menu.addSeparator()
    sender.add_send_menu(menu, [a.path for a in selection])

    action_copy_path = menu.addAction("Copy Full Path")
    action_copy_path.triggered.connect(lambda: QGuiApplication.clipboard().setText(str(asset.path)))
    return menu
