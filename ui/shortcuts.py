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
    Shortcut("main.multi_select", MAIN, "Ctrl+Click / Shift+Click", "Select several files"),
    Shortcut("main.context_menu", MAIN, "Right-click", "File actions (preview, tag, open, copy path...)"),

    # Search bar
    Shortcut("search.tag_token", SEARCH, "tag:name then Enter or Space", "Add a tag filter (tag:\"two words\" for spaces)"),
    Shortcut("search.remove_last_chip", SEARCH, "Backspace (empty box)", "Remove the last tag filter", "Backspace"),
    Shortcut("search.clear", SEARCH, "Esc", "Clear the search text and all tag filters", "Esc"),

    # Asset viewer window
    Shortcut("viewer.fullscreen", VIEWER, "F11", "Toggle full screen", "F11"),
    Shortcut("viewer.exit_fullscreen", VIEWER, "Esc", "Leave full screen (closes the viewer when not full screen)", "Esc"),

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
