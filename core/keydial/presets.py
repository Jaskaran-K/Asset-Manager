"""The starter profiles: what KeyDial's profiles.json holds the first time, and what
"Add Profile for..." starts an app from. Every shortcut here is one the app has by default;
change any of them in the editor.

Keys are K1..K18, left to right, top to bottom, as on the device: rows of four, then K13-K15 and
the tall K16, then the wide K17 and K18 (ui.keydial_device draws them).
"""
from __future__ import annotations

import copy
from typing import Any

# K1..K18 in order: (name shown on the key, action)
_Layout = list[tuple[str, Any]]


def _profile(name: str, apps: list[str], layout: _Layout, dial: list[dict], inherits: str | None = "Default") -> dict:
    profile: dict[str, Any] = {"name": name}
    if apps:
        profile["apps"] = apps
    if inherits:
        profile["inherits"] = inherits
    profile["labels"] = {f"K{n + 1}": label for n, (label, _) in enumerate(layout) if label is not None}
    profile["buttons"] = {f"K{n + 1}": action for n, (_, action) in enumerate(layout) if action is not None}
    profile["buttons"]["DIAL"] = {"dial_mode": "next"}
    profile["dial"] = dial
    return profile


def _mode(name: str, right: Any, left: Any) -> dict:
    return {"name": name, "cw": right, "ccw": left}


UNDO_REDO = lambda redo: _mode("Undo / Redo", redo, "ctrl+z")  # noqa: E731

DEFAULT_DIAL = [
    _mode("Volume", "volume_up", "volume_down"),
    _mode("Scroll", {"scroll": -2}, {"scroll": 2}),
    _mode("Zoom", {"scroll": 1, "with": "ctrl"}, {"scroll": -1, "with": "ctrl"}),
    UNDO_REDO("ctrl+y"),
]

# A number pad, as the KD100 is laid out for one: the tall key is Enter, the wide one 0, and the
# arrows printed on the keys sit on 8, 4, 6 and 2
DEFAULT = _profile("Default", [], [
    ("Esc", "esc"), ("/", "num_div"), ("*", "num_mul"), ("Back", "backspace"),
    ("7", "num7"), ("8", "num8"), ("9", "num9"), ("-", "num_sub"),
    ("4", "num4"), ("5", "num5"), ("6", "num6"), ("+", "num_add"),
    ("1", "num1"), ("2", "num2"), ("3", "num3"), ("Enter", "enter"),
    ("0", "num0"), (".", "num_dot"),
], copy.deepcopy(DEFAULT_DIAL), inherits=None)

# The first starter Default (KeyDial 0.7.x before the number pad): keys still set as it had them are
# moved to the number pad when profiles.json is upgraded (store.upgrade_document)
DEFAULT_V1_BUTTONS = {
    "K1": "ctrl+z", "K2": "ctrl+y", "K3": "ctrl+c", "K4": "ctrl+v", "K5": "ctrl+s", "K6": "up", "K7": "ctrl+x",
    "K8": "delete", "K9": "left", "K10": "space", "K11": "right", "K12": "enter", "K13": "esc", "K14": "down",
    "K15": "ctrl+a", "K16": {"hold": "alt"}, "K17": {"hold": "ctrl"}, "K18": {"hold": "shift"},
    "DIAL": {"dial_mode": "next"},
}

MAYA = _profile("Maya", ["maya.exe"], [
    ("Undo", "ctrl+z"), ("Redo", "shift+z"), ("Save", "ctrl+s"), ("Hotbox", {"hold": "space"}),
    ("Select", "q"), ("Move", "w"), ("Rotate", "e"), ("Scale", "r"),
    ("Frame", "f"), ("Wireframe", "4"), ("Shaded", "5"), ("Textured", "6"),
    ("Duplicate", "ctrl+d"), ("Group", "ctrl+g"), ("Smooth", "3"), ("Camera", {"hold": "alt"}),
    ("Ctrl", {"hold": "ctrl"}), ("Shift", {"hold": "shift"}),
], [
    _mode("Zoom", {"scroll": 1}, {"scroll": -1}),
    _mode("Timeline", "alt+period", "alt+comma"),
    _mode("Keyframes", "period", "comma"),
    UNDO_REDO("shift+z"),
])

ZBRUSH = _profile("ZBrush", ["zbrush.exe"], [
    ("Undo", "ctrl+z"), ("Redo", "ctrl+shift+z"), ("Save", "ctrl+s"), ("Brushes", "b"),
    ("Draw", "q"), ("Move", "w"), ("Scale", "e"), ("Rotate", "r"),
    ("Lower Sub", "shift+d"), ("Higher Sub", "d"), ("Symmetry", "x"), ("Polyframe", "shift+f"),
    ("Frame", "f"), ("Invert Mask", "ctrl+i"), ("Draw Size", "s"), ("Smooth", {"hold": "shift"}),
    ("Mask", {"hold": "ctrl"}), ("Subtract", {"hold": "alt"}),
], [
    _mode("Brush Size", "]", "["),
    _mode("Subdivision", "d", "shift+d"),
    UNDO_REDO("ctrl+shift+z"),
])

PAINTER = _profile("Substance 3D Painter", ["adobe substance 3d painter.exe"], [
    ("Undo", "ctrl+z"), ("Redo", "ctrl+y"), ("Save", "ctrl+s"), ("Export", "ctrl+shift+e"),
    ("Paint", "1"), ("Eraser", "2"), ("Projection", "3"), ("Poly Fill", "4"),
    ("Smudge", "5"), ("Clone", "6"), ("Channel", "c"), ("Material", "m"),
    ("3D / 2D", "f1"), ("3D", "f2"), ("2D", "f3"), ("Light", {"hold": "shift"}),
    ("Ctrl", {"hold": "ctrl"}), ("Camera", {"hold": "alt"}),
], [
    _mode("Brush Size", "]", "["),
    _mode("Zoom", {"scroll": 1}, {"scroll": -1}),
    UNDO_REDO("ctrl+y"),
])

PHOTOSHOP = _profile("Photoshop", ["photoshop.exe"], [
    ("Undo", "ctrl+z"), ("Redo", "ctrl+shift+z"), ("Save", "ctrl+s"), ("Pan", {"hold": "space"}),
    ("Brush", "b"), ("Eraser", "e"), ("Lasso", "l"), ("Move", "v"),
    ("Smaller", "["), ("Bigger", "]"), ("Swap", "x"), ("Rotate", "r"),
    ("New Layer", "ctrl+shift+n"), ("Merge", "ctrl+e"), ("Transform", "ctrl+t"), ("Pick", {"hold": "alt"}),
    ("Ctrl", {"hold": "ctrl"}),
    ("Merged", {"macro": ["ctrl+a", {"delay": 50}, "ctrl+shift+c", "ctrl+shift+v"]}),
], [
    _mode("Brush Size", "]", "["),
    _mode("Hardness", "shift+]", "shift+["),
    _mode("Zoom", {"scroll": 1, "with": "alt"}, {"scroll": -1, "with": "alt"}),
    UNDO_REDO("ctrl+shift+z"),
])

BLENDER = _profile("Blender", ["blender.exe"], [
    ("Undo", "ctrl+z"), ("Redo", "ctrl+shift+z"), ("Save", "ctrl+s"), ("Search", "f3"),
    ("Grab", "g"), ("Rotate", "r"), ("Scale", "s"), ("Edit Mode", "tab"),
    ("Front", "num1"), ("Right", "num3"), ("Top", "num7"), ("Persp", "num5"),
    ("Frame", "num_dot"), ("Shading", "z"), ("Add", "shift+a"), ("Shift", {"hold": "shift"}),
    ("Ctrl", {"hold": "ctrl"}), ("Alt", {"hold": "alt"}),
], [
    _mode("Zoom", {"scroll": 1}, {"scroll": -1}),
    _mode("Timeline", "right", "left"),
    UNDO_REDO("ctrl+shift+z"),
])

TOOLBAG = _profile("Marmoset Toolbag", ["toolbag.exe"], [
    ("Undo", "ctrl+z"), ("Redo", "ctrl+y"), ("Save", "ctrl+s"), ("", "none"),
    ("Select", "q"), ("Move", "w"), ("Rotate", "e"), ("Scale", "r"),
    ("Frame", "f"), ("Duplicate", "ctrl+d"), ("Delete", "delete"), ("", "none"),
    ("", "none"), ("", "none"), ("", "none"), ("Camera", {"hold": "alt"}),
    ("Ctrl", {"hold": "ctrl"}), ("Shift", {"hold": "shift"}),
], [
    _mode("Zoom", {"scroll": 1}, {"scroll": -1}),
    UNDO_REDO("ctrl+y"),
])

MEDIA_APPS = ["spotify.exe", "vlc.exe", "wmplayer.exe", "microsoft.media.player.exe", "foobar2000.exe",
              "mpc-hc64.exe", "mpc-be64.exe", "potplayermini64.exe", "musicbee.exe", "itunes.exe", "aimp.exe"]

MULTIMEDIA = _profile("Multimedia", MEDIA_APPS, [
    ("Previous", "prev_track"), ("Play", "play_pause"), ("Next", "next_track"), ("Stop", "media_stop"),
    ("Mute", "mute"), ("Up", "up"), ("Volume -", "volume_down"), ("Volume +", "volume_up"),
    ("Back", "left"), ("Space", "space"), ("Forward", "right"), ("Full Screen", "f"),
    ("Esc", "esc"), ("Down", "down"), ("Captions", "c"), ("Play", "play_pause"),
    ("Slower", "shift+comma"), ("Faster", "shift+period"),  # (YouTube's speed keys)
], [
    _mode("Volume", "volume_up", "volume_down"),
    _mode("Seek", "right", "left"),
    _mode("Track", "next_track", "prev_track"),
])
MULTIMEDIA_V1_BUTTONS = {  # (the first starter Multimedia: K15-K18 were left to Default)
    "K1": "prev_track", "K2": "play_pause", "K3": "next_track", "K4": "media_stop", "K5": "mute", "K6": "up",
    "K7": "volume_down", "K8": "volume_up", "K9": "left", "K10": "space", "K11": "right", "K12": "f", "K13": "esc",
    "K14": "down", "DIAL": {"dial_mode": "next"},
}

# Every web browser: the "@browsers" program stands for all of them (core.keydial.browsers)
BROWSERS = _profile("Browsers", ["@browsers"], [
    ("Back", "alt+left"), ("Forward", "alt+right"), ("Reload", "f5"), ("New Tab", "ctrl+t"),
    ("Close Tab", "ctrl+w"), ("Reopen Tab", "ctrl+shift+t"), ("Prev Tab", "ctrl+shift+tab"), ("Next Tab", "ctrl+tab"),
    ("Previous", "prev_track"), ("Play", "play_pause"), ("Next", "next_track"), ("Full Screen", "f"),
    ("Zoom Out", "ctrl+minus"), ("Zoom In", "ctrl+plus"), ("Zoom 100%", "ctrl+0"), ("Address", "ctrl+l"),
    ("Ctrl", {"hold": "ctrl"}), ("Downloads", "ctrl+j"),
], [
    _mode("Scroll", {"scroll": -2}, {"scroll": 2}),
    _mode("Tabs", "ctrl+tab", "ctrl+shift+tab"),
    _mode("Volume", "volume_up", "volume_down"),
    _mode("Zoom", "ctrl+plus", "ctrl+minus"),
])

# (in the order "Add Profile for..." lists them: Maya first)
APP_PRESETS = (MAYA, ZBRUSH, PAINTER, PHOTOSHOP, BLENDER, TOOLBAG, MULTIMEDIA, BROWSERS)

# profiles.json's "starter" number: what an older file is upgraded from (store.upgrade_document)
STARTER_VERSION = 2


def starter_document() -> dict:
    """profiles.json the first time: Default plus every app above."""
    return {"starter": STARTER_VERSION, "profiles": [copy.deepcopy(p) for p in (DEFAULT, *APP_PRESETS)]}


def preset_for(name: str) -> dict | None:
    """A fresh copy of the starter profile called name."""
    found = next((p for p in (DEFAULT, *APP_PRESETS) if p["name"] == name), None)
    return copy.deepcopy(found) if found else None


def blank_profile(name: str, apps: list[str]) -> dict:
    """A profile for any other program: every key does what Default does until it's changed."""
    return {"name": name, "apps": apps, "inherits": "Default", "buttons": {}, "labels": {}}
