"""Single source of truth for every keyboard shortcut and mouse gesture in the app.

Rule: a shortcut only exists if it is listed here. Code binds or checks keys by id,
never with hard-coded keys:

    button.setShortcut(sequence("main.tag"))         # QAction / QPushButton shortcuts
    if matches("viewer3d.grid", event): ...           # keyPressEvent / event filters

Settings > General > Shortcuts is generated from SHORTCUTS, so registering a shortcut
here is all it takes to document it. tests/test_shortcuts.py fails if a hard-coded key
(Qt.Key.Key_*, setShortcut("..."), QKeySequence("...")) appears anywhere else, or if code
uses an id that isn't registered. Mouse gestures can't be bound by id; list them here
with sequence=None so they still appear on the Shortcuts page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QKeyCombination, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence


@dataclass(frozen=True)
class Shortcut:
    id: str
    category: str
    keys: str  # shown to the user
    action: str
    sequence: Optional[str] = None  # QKeySequence text the code matches; None for mouse gestures / typed text


# Categories in the order the Shortcuts page shows them
MAIN = "Asset Browser"
SEARCH = "Search Bar"
VIEWER = "Asset Viewer"
MODEL = "3D Model Viewer"
IMAGE = "Image Viewer"
VIDEO = "Video Player"
TAG_DIALOG = "Tag Dialog"
VIEWS = "Views"
ARTBOOK = "Artbook"
PAGES = "Artbook Pages"
CATEGORIES = (VIEWS, MAIN, SEARCH, ARTBOOK, PAGES, VIEWER, MODEL, IMAGE, VIDEO, TAG_DIALOG)

SHORTCUTS: tuple[Shortcut, ...] = (
    # Views (both windows)
    Shortcut("view.asset", VIEWS, "Ctrl+1", "Asset View: the asset browser", "Ctrl+1"),
    Shortcut("view.artbook", VIEWS, "Ctrl+2", "Artbook View: your reference boards", "Ctrl+2"),
    Shortcut("view.split", VIEWS, "Ctrl+3", "Split View: assets and Artbook side by side", "Ctrl+3"),
    Shortcut("view.separate", VIEWS, "Ctrl+4", "Separate View: the Artbook in its own window", "Ctrl+4"),
    Shortcut("view.switch", VIEWS, "Click View", "Switch between Asset and Artbook view (the arrow lists every view)"),
    Shortcut("view.split_drag", VIEWS, "Drag the divider", "Split View: give one side more room (remembered)"),
    Shortcut("view.window_options", VIEWS, "Click Window", "Separate View: always on top, opacity and click-through for the Artbook window"),
    Shortcut("view.click_through", VIEWS, "Ctrl+Alt+T", "Separate View: turn click-through on / off. Works from any app", "Ctrl+Alt+T"),
    Shortcut("view.click_through_tray", VIEWS, "Click the tray icon", "Turn click-through off (the icon shows while it's on)"),

    # Asset browser (main window)
    Shortcut("main.preview", MAIN, "Space", "Preview the selected asset", "Space"),
    Shortcut("main.open", MAIN, "Double-click", "Open an asset in the viewer"),
    Shortcut("main.tag", MAIN, "Ctrl+T", "Tag the selected files", "Ctrl+T"),
    Shortcut("main.settings", MAIN, "Ctrl+,", "Open Settings", "Ctrl+,"),
    Shortcut("main.undo", MAIN, "Ctrl+Z", "Undo the last tag, favorite or rating change", "Ctrl+Z"),
    Shortcut("main.redo", MAIN, "Ctrl+Y", "Redo it", "Ctrl+Y"),
    Shortcut("main.favorite", MAIN, "F", "Add / remove the selected files from Favorites", "F"),
    Shortcut("main.rate_1", MAIN, "1", "Rate the selected files ★", "1"),  # glyph-ok: rating text
    Shortcut("main.rate_2", MAIN, "2", "Rate ★★", "2"),  # glyph-ok
    Shortcut("main.rate_3", MAIN, "3", "Rate ★★★", "3"),  # glyph-ok
    Shortcut("main.rate_4", MAIN, "4", "Rate ★★★★", "4"),  # glyph-ok
    Shortcut("main.rate_5", MAIN, "5", "Rate ★★★★★", "5"),  # glyph-ok
    Shortcut("main.rate_0", MAIN, "0", "Clear the rating", "0"),
    Shortcut("main.multi_select", MAIN, "Ctrl+Click / Shift+Click", "Select several files"),
    Shortcut("main.context_menu", MAIN, "Right-click", "File actions (preview, tag, open, copy path...)"),
    Shortcut("main.send_to_artbook", MAIN, "Right-click > Send to Artbook", "Add the selected images to any Artbook page, or a new one"),
    Shortcut("main.drag_out", MAIN, "Drag files out of the grid", "Copy them into Explorer, Blender, Photoshop, Unreal..."),
    Shortcut("main.sidebar", MAIN, "Ctrl+B", "Show / hide the sidebar (or click the menu button). In Split View: the side you're working in", "Ctrl+B"),
    Shortcut("main.close_sidebar", MAIN, "Esc / click beside it", "Close the floating sidebar (unpinned with the pin button)", "Esc"),
    Shortcut("main.sidebar_resize", MAIN, "Drag the sidebar's right edge", "Make the sidebar wider or narrower"),

    # Search bar
    Shortcut("search.tag_token", SEARCH, "tag:name then Enter or Space", "Add a tag filter (tag:\"two words\" for spaces)"),
    Shortcut("search.exclude_token", SEARCH, "-tag:name", "Only files that don't have the tag"),
    Shortcut("search.any_token", SEARCH, "tag:a|b", "Files with any of the tags"),
    Shortcut("search.toggle_chip", SEARCH, "Click a tag chip", "Switch it between has / doesn't have"),
    Shortcut("search.save", SEARCH, "Click the star in the search box", "Save the search as a sidebar collection"),
    Shortcut("search.details", SEARCH, "is:fav  rating:>=3  ext:png", "Favorites, star rating, file type"),
    Shortcut("search.details_size", SEARCH, "res:>=2048  size:>10mb", "Image / video resolution (res:4k = 4096), file size"),
    Shortcut("search.details_media", SEARCH, "dur:<30s  poly:<10k", "Video length, 3D model faces"),
    Shortcut("search.remove_last_chip", SEARCH, "Backspace (empty box)", "Remove the last tag filter", "Backspace"),
    Shortcut("search.clear", SEARCH, "Esc", "Clear the search text and all tag filters", "Esc"),

    # Artbook board (while it has the keyboard focus)
    Shortcut("artbook.drop", ARTBOOK, "Drop / drag images in", "From Explorer, a web browser or the asset grid"),
    Shortcut("artbook.import_drop", ARTBOOK, "Drop .artbook / .pur files on the board", "Import shared pages, or PureRef boards with their layouts (also: the Import button in Pages)"),
    Shortcut("artbook.paste", ARTBOOK, "Ctrl+V", "Paste an image, image files or an image link", "Ctrl+V"),
    Shortcut("artbook.copy", ARTBOOK, "Ctrl+C", "Copy the selected image", "Ctrl+C"),
    Shortcut("artbook.pan", ARTBOOK, "Middle-drag", "Pan around the board"),
    Shortcut("artbook.pan_key", ARTBOOK, "Hold Space + drag", "Pan with the left button", "Space"),
    Shortcut("artbook.zoom", ARTBOOK, "Mouse wheel", "Zoom toward the cursor"),
    Shortcut("artbook.move", ARTBOOK, "Drag an image", "Move it (and everything selected with it)"),
    Shortcut("artbook.select", ARTBOOK, "Click / drag a box", "Select images (click empty space to clear)"),
    Shortcut("artbook.multi_select", ARTBOOK, "Shift+Click / Ctrl+Click", "Add an image to the selection, or take it out"),
    Shortcut("artbook.scale", ARTBOOK, "Drag a corner handle", "Scale the selection (keeps its shape)"),
    Shortcut("artbook.rotate", ARTBOOK, "Drag the round handle", "Rotate the image (Shift: 15° steps)"),
    Shortcut("artbook.zoom_to", ARTBOOK, "Double-click an image", "Zoom to it"),
    Shortcut("artbook.fit", ARTBOOK, "F", "Frame the selection, or everything", "F"),
    Shortcut("artbook.zoom_100", ARTBOOK, "Ctrl+0", "Zoom to 100%", "Ctrl+0"),
    Shortcut("artbook.flip_h", ARTBOOK, "H", "Flip the selection horizontally", "H"),
    Shortcut("artbook.flip_v", ARTBOOK, "V", "Flip the selection vertically", "V"),
    Shortcut("artbook.front", ARTBOOK, "]", "Bring the selection to the front", "]"),
    Shortcut("artbook.back", ARTBOOK, "[", "Send the selection to the back", "["),
    Shortcut("artbook.reset", ARTBOOK, "R", "Reset rotation, size and flips (Right-click > Images > Selection > Reset: one at a time)", "R"),
    Shortcut("artbook.arrange", ARTBOOK, "A", "Auto-arrange the selection (or everything) into neat rows", "A"),
    Shortcut("artbook.arrange_menu", ARTBOOK, "Right-click > Images", "Align, distribute, match size; crop, grayscale, opacity"),
    Shortcut("artbook.crop", ARTBOOK, "C", "Crop the selected image: drag its handles (C again or Enter keeps it)", "C"),
    Shortcut("artbook.crop_apply", ARTBOOK, "Enter", "Crop mode: keep the crop (clicking elsewhere keeps it too)", "Return"),
    Shortcut("artbook.crop_drag", ARTBOOK, "Drag a crop handle", "Crop mode: cut an edge or corner away (drag back out to undo it)"),
    Shortcut("artbook.note", ARTBOOK, "T", "Add a text note at the cursor and type in it", "T"),
    Shortcut("artbook.note_edit", ARTBOOK, "Double-click a note", "Type in it (Esc or click elsewhere when done)"),
    Shortcut("artbook.note_bold", ARTBOOK, "Ctrl+B (typing)", "Bold, while typing in a note (otherwise Ctrl+B shows the Pages)", "Ctrl+B"),
    Shortcut("artbook.note_italic", ARTBOOK, "Ctrl+I (typing)", "Italic, while typing in a note", "Ctrl+I"),
    Shortcut("artbook.note_underline", ARTBOOK, "Ctrl+U (typing)", "Underline, while typing in a note", "Ctrl+U"),
    Shortcut("artbook.note_format", ARTBOOK, "The bar above a note", "Text colour, highlight, note colour, bold / italic / underline, size, font, alignment"),
    Shortcut("artbook.edge_handles", ARTBOOK, "Drag a side handle", "Scale from the opposite side; on a note, left / right set its width"),
    Shortcut("artbook.picker", ARTBOOK, "I", "Colour picker: each click copies a colour's hex code (Esc when done)", "I"),
    Shortcut("artbook.custom_color", ARTBOOK, "Note bar colour > Custom...", "Pick any colour and opacity; the note changes as you drag (one undo step). Drag the picker's title to move it, Esc closes it"),
    Shortcut("artbook.color_dropper", ARTBOOK, "Click the dropper, then anywhere", "Custom colour: take it from anywhere on screen (Esc cancels)"),
    Shortcut("artbook.gray", ARTBOOK, "G", "Grayscale on / off for the selected images", "G"),
    Shortcut("artbook.gray_board", ARTBOOK, "Shift+G", "Show the whole board in grayscale, to judge values", "Shift+G"),
    Shortcut("artbook.group", ARTBOOK, "Ctrl+G", "Group the selection: it's selected, moved and arranged as one", "Ctrl+G"),
    Shortcut("artbook.ungroup", ARTBOOK, "Ctrl+Shift+G", "Ungroup the selected groups (everything stays put)", "Ctrl+Shift+G"),
    Shortcut("artbook.draw", ARTBOOK, "D", "Draw: freehand strokes on the board (D again or Esc to stop)", "D"),
    Shortcut("artbook.eraser", ARTBOOK, "E", "While drawing: the eraser on / off (drag over strokes to remove them)", "E"),
    Shortcut("artbook.draw_drag", ARTBOOK, "Drag while drawing", "Draw a stroke (with the eraser on: remove the strokes you drag over)"),
    Shortcut("artbook.image_list", ARTBOOK, "L", "Show / hide the list of every image on the page", "L"),
    Shortcut("artbook.group_pick", ARTBOOK, "Alt+Click", "Select one image or note inside a group, on its own"),
    Shortcut("artbook.group_click", ARTBOOK, "Click / drag a group's panel", "Select the whole group from its empty space, and drag to move it"),
    Shortcut("artbook.export_png", ARTBOOK, "Ctrl+E", "Export the board (or the selection) as a PNG image", "Ctrl+E"),
    Shortcut("artbook.delete", ARTBOOK, "Delete", "Remove the selection from the page", "Del"),
    Shortcut("artbook.select_all", ARTBOOK, "Ctrl+A", "Select everything", "Ctrl+A"),
    Shortcut("artbook.deselect", ARTBOOK, "Esc", "Clear the selection (in crop mode: cancel the crop)", "Esc"),
    Shortcut("artbook.undo", ARTBOOK, "Ctrl+Z", "Undo a change to the page", "Ctrl+Z"),
    Shortcut("artbook.redo", ARTBOOK, "Ctrl+Y", "Redo it", "Ctrl+Y"),
    Shortcut("artbook.context_menu", ARTBOOK, "Right-click", "Board actions (paste, flip, order, delete...)"),

    # Artbook Pages sidebar
    Shortcut("pages.sidebar", PAGES, "Ctrl+B", "Show / hide the Pages sidebar (while you're working in the Artbook)", "Ctrl+B"),
    Shortcut("pages.close_sidebar", PAGES, "Esc / click beside it", "Close the floating Pages sidebar", "Esc"),
    Shortcut("pages.open", PAGES, "Click a page", "Open it (the page you were on is saved first)"),
    Shortcut("pages.rename", PAGES, "F2", "Rename the selected page or folder", "F2"),
    Shortcut("pages.rename_mouse", PAGES, "Click the pencil (or double-click a page)", "Rename a page or folder: the pencil shows on the hovered or selected row"),
    Shortcut("pages.toggle_folder", PAGES, "Click a folder", "Open or close it"),
    Shortcut("pages.drag", PAGES, "Drag a page or folder", "Reorder it, or drop it on a folder to move it inside"),
    Shortcut("pages.delete", PAGES, "Delete", "Send the selected page to the Recycle Bin (asks first)", "Del"),
    Shortcut("pages.context_menu", PAGES, "Right-click", "New page / folder, rename, duplicate, move, delete"),
    Shortcut("pages.sidebar_resize", PAGES, "Drag the sidebar's right edge", "Make the Pages sidebar wider or narrower"),

    # Asset viewer window
    Shortcut("viewer.fullscreen", VIEWER, "F11", "Toggle full screen", "F11"),
    Shortcut("viewer.exit_fullscreen", VIEWER, "Esc", "Leave full screen (closes the viewer when not full screen)", "Esc"),
    Shortcut("viewer.previous", VIEWER, "Page Up", "Previous file in the grid", "PgUp"),
    Shortcut("viewer.next", VIEWER, "Page Down", "Next file in the grid", "PgDown"),
    Shortcut("viewer.previous_alt", VIEWER, "Alt+Left", "Previous file in the grid", "Alt+Left"),
    Shortcut("viewer.next_alt", VIEWER, "Alt+Right", "Next file in the grid", "Alt+Right"),

    # 3D model viewport
    Shortcut("viewer3d.orbit", MODEL, "Left-drag", "Orbit the camera"),
    Shortcut("viewer3d.pan", MODEL, "Right-drag / Middle-drag", "Pan the camera"),
    Shortcut("viewer3d.zoom", MODEL, "Mouse wheel", "Zoom"),
    Shortcut("viewer3d.wireframe", MODEL, "W", "Cycle shaded / wireframe / both", "W"),
    Shortcut("viewer3d.grid", MODEL, "G", "Toggle the ground grid", "G"),
    Shortcut("viewer3d.reset", MODEL, "R", "Reset the camera", "R"),

    # Image / texture viewer
    Shortcut("image.zoom", IMAGE, "Mouse wheel", "Zoom toward the cursor"),
    Shortcut("image.pan", IMAGE, "Left-drag / Middle-drag", "Pan the image"),

    # Video player
    Shortcut("video.play_pause", VIDEO, "Space", "Play / pause", "Space"),
    Shortcut("video.back", VIDEO, "Left arrow", "Jump back by the jump time", "Left"),
    Shortcut("video.forward", VIDEO, "Right arrow", "Jump forward by the jump time", "Right"),
    Shortcut("video.mute", VIDEO, "M", "Mute / unmute (or click the speaker icon)", "M"),
    Shortcut("video.frame_back", VIDEO, ",", "Pause and step back one frame", ","),
    Shortcut("video.frame_forward", VIDEO, ".", "Pause and step forward one frame", "."),
    Shortcut("video.loop", VIDEO, "L", "A-B loop: set the start, then the end, then turn it off", "L"),
    Shortcut("video.restart", VIDEO, "Home", "Back to the start (videos otherwise resume where you left them)", "Home"),
    Shortcut("video.speed", VIDEO, "Click the speed button", "Pick a preset speed, or drag the slider (arrow keys: 0.05× steps)"),
    Shortcut("video.fullscreen", VIDEO, "F", "Toggle video full screen", "F"),
    Shortcut("video.fullscreen_mouse", VIDEO, "Double-click video", "Toggle video full screen"),
    Shortcut("video.exit_fullscreen", VIDEO, "Esc", "Leave video full screen", "Esc"),
    Shortcut("video.seek", VIDEO, "Click / drag seek bar", "Jump to a point / scrub (hover shows a preview)"),

    # Tag dialog
    Shortcut("tagdialog.create", TAG_DIALOG, "Enter (in the new tag box)", "Create the tag and tick it"),  # QLineEdit.returnPressed
)

_BY_ID = {s.id: s for s in SHORTCUTS}
assert len(_BY_ID) == len(SHORTCUTS), "duplicate shortcut id"


def get(shortcut_id: str) -> Shortcut:
    return _BY_ID[shortcut_id]


def sequence(shortcut_id: str) -> QKeySequence:
    """Key sequence to bind with setShortcut()."""
    seq = _BY_ID[shortcut_id].sequence
    if seq is None:
        raise ValueError(f"{shortcut_id} is a mouse gesture / typed text, not a key sequence")
    return QKeySequence(seq)


def matches(shortcut_id: str, event: QKeyEvent) -> bool:
    """True if a key press is this shortcut (keypad / number-pad variants count too)."""
    combo = event.keyCombination()
    modifiers = combo.keyboardModifiers() & ~Qt.KeyboardModifier.KeypadModifier
    pressed = QKeySequence(QKeyCombination(modifiers, combo.key()))
    return pressed.matches(sequence(shortcut_id)) == QKeySequence.SequenceMatch.ExactMatch


def by_category() -> list[tuple[str, list[Shortcut]]]:
    return [(cat, [s for s in SHORTCUTS if s.category == cat]) for cat in CATEGORIES]
