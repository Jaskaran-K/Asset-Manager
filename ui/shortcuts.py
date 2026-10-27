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
CATEGORIES = (MAIN, SEARCH, VIEWER, MODEL, IMAGE, VIDEO, TAG_DIALOG)

SHORTCUTS: tuple[Shortcut, ...] = (
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
    Shortcut("main.drag_out", MAIN, "Drag files out of the grid", "Copy them into Explorer, Blender, Photoshop, Unreal..."),
    Shortcut("main.sidebar", MAIN, "Ctrl+B", "Show / hide the sidebar (or click the menu button)", "Ctrl+B"),
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
