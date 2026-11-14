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

from core import app_helpers, apps, artbook_store
from core.hdr_image import HDR_EXTENSIONS
from core.picture_look import color_name, hex_of
from core.scanner import AssetItem
from core.settings import get_artbook_page
from core.tag_repository import Marks
from ui import file_actions, shortcuts
from ui.asset_grid import rating_text
from ui.duotone import set_icon
from ui.icons import color_swatch_icon

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

    def add_send_menu(
        self, menu: QMenu, paths: Sequence[Path], resolve: Optional[Callable[[list[Path]], list[Path]]] = None
    ) -> None:
        """Send to Artbook: a new page, or any page (in its folders). Image files only.
        resolve(paths): the real files to send, when picked (files inside archives unpacked)."""
        paths = [p for p in paths if p.suffix.lower() in artbook_store.IMAGE_EXTENSIONS]
        send = menu.addMenu("Send to Artbook" if paths else "Send to Artbook (images only)")
        send.setEnabled(bool(paths))
        if paths:
            self.fill_artbook_menu(send, lambda page_id: self.send_files(resolve(paths) if resolve else paths, page_id))

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
    open_external: Optional[Callable[[], None]] = None  # (default: the file's own app, by its path)
    extract: Optional[Callable[[], None]] = None  # an archive, or a file inside one: save a copy
    send_files: Optional[Callable[[list[Path]], list[Path]]] = None  # the real files to send to the Artbook
    open_in: Optional[Callable[[apps.App], None]] = None  # open it in one of the apps (Open In)
    import_board: Optional[Callable[[], None]] = None  # a PureRef board: make a new Artbook page from it
    use_as_lighting: Optional[Callable[[], None]] = None  # an HDR image: light 3D models with it
    find_similar: Optional[Callable[[], None]] = None  # a picture: show it and its look-alikes
    colors: Sequence[tuple[tuple[int, int, int], float]] = ()  # a picture's main colours (core.picture_look)
    search_color: Optional[Callable[[str], None]] = None  # search for color:<word>
    add_folder_rule: Optional[Callable[[], None]] = None  # a rule for the file's folder (core.folder_rules)
    rename: Optional[Callable[[], None]] = None  # batch rename the selected files (ui.batch_dialogs)
    convert: Optional[Callable[[], None]] = None  # convert / resize the selected pictures
    more_like: Optional[Callable[[], None]] = None  # AI Search: the files with the same kind of content
    remove_background: Optional[Callable[[], None]] = None  # cut-outs of the selected pictures (BEN2)
    send_material: Optional[Callable[[str], None]] = None  # a texture set: make it a material in "blender" / "maya"


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
    menu.setToolTipsVisible(True)
    archive = asset.is_archive and not asset.in_archive

    board = asset.extension.lower() == ".pur"

    texture_set = asset.texture_set
    action_view = menu.addAction(
        "Preview Material" if texture_set is not None else "Look Inside" if archive else "Open in Artbook" if board
        else "Preview in Asset Viewer"
    )
    font = action_view.font()
    font.setBold(True)
    action_view.setFont(font)
    if archive:
        set_icon(action_view, "archive")
    elif texture_set is not None:
        set_icon(action_view, "material")
        action_view.setToolTip("The maps as a material on a sphere, cube or plane")
    action_view.triggered.connect(actions.open_viewer)
    if texture_set is not None and actions.send_material is not None:
        for app_id, name, what in (("blender", "Blender", "a Principled BSDF"), ("maya", "Maya", "an Arnold aiStandardSurface")):
            action = menu.addAction(f"Send to {name} as Material")
            set_icon(action, "open_in")
            action.setToolTip(f"Make {what} material with these maps in the {name} that's open, on its selected objects")
            action.triggered.connect(lambda _checked=False, a=app_id: actions.send_material(a))
    if board and actions.import_board is not None:
        action_again = menu.addAction("Import Again")
        set_icon(action_again, "import")
        action_again.setToolTip("Make a new Artbook page from the board as it is now. The page it has already stays as it is")
        action_again.triggered.connect(actions.import_board)
    if actions.use_as_lighting is not None and asset.extension.lower() in HDR_EXTENSIONS:
        action_light = menu.addAction("Use as Viewer Lighting")
        set_icon(action_light, "light")
        action_light.setToolTip("Light 3D models in the viewer with this HDRI (a colour 2:1 panorama)")
        action_light.triggered.connect(actions.use_as_lighting)

    menu.addSeparator()

    fitting = apps.apps_for(asset.extension) if actions.open_in is not None and texture_set is None else []
    if fitting:
        open_in = menu.addMenu("Open In")
        open_in.setToolTipsVisible(True)
        set_icon(open_in.menuAction(), "open_in")
        for app in fitting:
            action = open_in.addAction(app.name)
            if app_helpers.goes_to_open_window(app.id, asset.path):
                action.setToolTip(f"Imports it into the {app.name} that's open")
            elif asset.in_archive:
                action.setToolTip(f"Unpacks it first, then opens it in {app.name}")
            action.triggered.connect(lambda _checked=False, a=app: actions.open_in(a))

    if texture_set is None:
        action_open = menu.addAction("Open with External App")
        if asset.in_archive:
            action_open.setToolTip("Unpacks it first, then opens it in the program Windows uses for it")
        action_open.triggered.connect(actions.open_external or (lambda: file_actions.open_file(asset.path)))

    action_reveal = menu.addAction("Show Archive in File Explorer" if asset.in_archive else "Show in File Explorer")
    action_reveal.triggered.connect(actions.reveal)

    if actions.extract is not None and (archive or asset.in_archive):
        action_extract = menu.addAction("Extract To..." if archive else "Extract...")
        set_icon(action_extract, "extract")
        action_extract.setToolTip(
            "Unpack every file into a folder you choose" if archive else "Save a copy of this file where you choose"
        )
        action_extract.triggered.connect(actions.extract)

    if (asset.is_texture and (actions.find_similar is not None or actions.colors)) or actions.more_like is not None:
        menu.addSeparator()
    if asset.is_texture and actions.find_similar is not None:
        action_similar = menu.addAction("Find Similar")
        set_icon(action_similar, "similar")
        action_similar.setToolTip("Show this picture and its look-alikes (resized or re-saved copies), closest first")
        action_similar.triggered.connect(actions.find_similar)
    if actions.more_like is not None:
        action_like = menu.addAction("More Like This")
        set_icon(action_like, "ai")
        action_like.setToolTip("Files with the same kind of content (AI Search), closest first: other skulls, other brick walls")
        action_like.triggered.connect(actions.more_like)
    if asset.is_texture and actions.colors and actions.search_color is not None:
        colour_menu = menu.addMenu("Search by Its Colours")
        colour_menu.setToolTipsVisible(True)
        set_icon(colour_menu.menuAction(), "palette")
        for rgb, share in actions.colors:
            word = hex_of(rgb)
            action = colour_menu.addAction(color_swatch_icon(word, 22, 14), f"{color_name(rgb).capitalize()}  {word}  ({share:.0%})")
            action.setToolTip(f"Pictures with colours close to {word} on 15% of them or more")
            action.triggered.connect(lambda _checked=False, w=word: actions.search_color(w))

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

    files = len(selection)
    if actions.rename is not None:
        action_rename = menu.addAction("Rename..." if files <= 1 else f"Rename {files} Files...")
        action_rename.setShortcut(shortcuts.sequence("main.rename"))
        action_rename.setToolTip("New names from a pattern, with a preview first (Ctrl+Z undoes it)")
        action_rename.setEnabled(not all(a.in_archive for a in selection))
        action_rename.triggered.connect(actions.rename)
    if actions.convert is not None and any(a.is_texture for a in selection):
        action_convert = menu.addAction("Convert and Resize...")
        set_icon(action_convert, "upscale")
        action_convert.setToolTip("Make copies in another type or smaller (PNG, JPG, TGA, TIF; 2K, 4K ...); the originals stay")
        action_convert.triggered.connect(actions.convert)
    if actions.remove_background is not None and any(a.is_texture for a in selection):
        pictures = sum(1 for a in selection if a.is_texture)
        action_cutout = menu.addAction("Remove Background..." if pictures <= 1 else f"Remove Background ({pictures} Pictures)...")
        set_icon(action_cutout, "cutout")
        action_cutout.setToolTip("Make a copy with the background removed beside each picture (a PNG with transparency); the originals stay")
        action_cutout.triggered.connect(actions.remove_background)

    if actions.add_folder_rule is not None:
        action_rule = menu.addAction("Folder Rule...")
        set_icon(action_rule, "folder")
        action_rule.setToolTip("Tag files added to this file's folder from now on (Settings > Folder Rules)")
        action_rule.triggered.connect(actions.add_folder_rule)

    menu.addSeparator()
    sender.add_send_menu(menu, [a.path for a in selection], actions.send_files)

    action_copy_path = menu.addAction("Copy Full Path")
    action_copy_path.triggered.connect(lambda: QGuiApplication.clipboard().setText(str(asset.path)))
    return menu
