"""Key-combo parsing ("ctrl+shift+z" -> ("ctrl", "shift", "z")).

Pure module: knows key *names* only. `output.py` turns names into real
keystrokes.

Combo syntax: names joined with "+", case-insensitive. Use the word "plus"
for the + key. A single printable character ("z", "[", "1") is also a key.
"""
from __future__ import annotations

MODIFIERS = ("ctrl", "shift", "alt", "win")

ALIASES = {
    "control": "ctrl",
    "option": "alt",
    "cmd": "win",
    "super": "win",
    "windows": "win",
    "return": "enter",
    "escape": "esc",
    "del": "delete",
    "ins": "insert",
    "pgup": "page_up",
    "pageup": "page_up",
    "pgdn": "page_down",
    "pagedown": "page_down",
    "bksp": "backspace",
    "vol_up": "volume_up",
    "vol_down": "volume_down",
    "prev": "prev_track",
    "next": "next_track",
}

# our name -> pynput `Key` attribute name
PYNPUT_KEYS = {
    "ctrl": "ctrl_l",  # (the left ones: what a keyboard sends, and what apps reading raw input expect)
    "shift": "shift_l",
    "alt": "alt_l",
    "win": "cmd",
    "enter": "enter",
    "esc": "esc",
    "tab": "tab",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete",
    "insert": "insert",
    "home": "home",
    "end": "end",
    "page_up": "page_up",
    "page_down": "page_down",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "caps_lock": "caps_lock",
    "print_screen": "print_screen",
    "menu": "menu",
    "volume_up": "media_volume_up",
    "volume_down": "media_volume_down",
    "mute": "media_volume_mute",
    "play_pause": "media_play_pause",
    "next_track": "media_next",
    "prev_track": "media_previous",
    "media_stop": "media_stop",
    **{f"f{i}": f"f{i}" for i in range(1, 25)},
}

# our name -> Windows virtual-key code (keys pynput has no name for)
VK_KEYS = {
    "plus": 0xBB,  # the =/+ key (what apps mean by Ctrl+"+")
    "minus": 0xBD,
    "comma": 0xBC,
    "period": 0xBE,
    **{f"num{i}": 0x60 + i for i in range(10)},
    "num_mul": 0x6A,
    "num_add": 0x6B,
    "num_sub": 0x6D,
    "num_dot": 0x6E,
    "num_div": 0x6F,
}

KNOWN_NAMES = frozenset(PYNPUT_KEYS) | frozenset(VK_KEYS)


class ComboError(ValueError):
    pass


def normalize_token(token: str) -> str:
    t = token.strip()
    if len(t) == 1:
        return t.lower()
    t = t.lower().replace("-", "_")
    t = ALIASES.get(t, t)
    if t not in KNOWN_NAMES:
        raise ComboError(f"unknown key {token!r}")
    return t


def parse_combo(text: str) -> tuple[str, ...]:
    """Parse "ctrl+shift+z". Modifiers are moved to the front, duplicates dropped."""
    if not isinstance(text, str) or not text.strip():
        raise ComboError("empty key combo")
    parts = text.split("+")
    if any(not p.strip() for p in parts):
        raise ComboError(f"bad combo {text!r} (use 'plus' for the + key)")
    tokens: list[str] = []
    for p in parts:
        t = normalize_token(p)
        if t not in tokens:
            tokens.append(t)
    mods = [t for t in tokens if t in MODIFIERS]
    rest = [t for t in tokens if t not in MODIFIERS]
    return tuple(mods + rest)


# -- the editor: keys pressed on the keyboard, and combos shown as text ------------------------
# Windows virtual-key code -> our name (what a shortcut field records when a key is pressed)
_VK_NAMES = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x1B: "esc", 0x20: "space",
    0x21: "page_up", 0x22: "page_down", 0x23: "end", 0x24: "home",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x2C: "print_screen", 0x2D: "insert", 0x2E: "delete", 0x14: "caps_lock", 0x5D: "menu",
    0xAD: "mute", 0xAE: "volume_down", 0xAF: "volume_up",
    0xB0: "next_track", 0xB1: "prev_track", 0xB2: "media_stop", 0xB3: "play_pause",
    0x6A: "num_mul", 0x6B: "num_add", 0x6D: "num_sub", 0x6E: "num_dot", 0x6F: "num_div",
    0xBB: "plus", 0xBD: "minus", 0xBC: "comma", 0xBE: "period",
    0xBA: ";", 0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
    **{0x41 + i: chr(ord("a") + i) for i in range(26)},
    **{0x30 + i: str(i) for i in range(10)},
    **{0x60 + i: f"num{i}" for i in range(10)},
    **{0x70 + i: f"f{i + 1}" for i in range(24)},
}
_VK_MODIFIERS = {0x10: "shift", 0xA0: "shift", 0xA1: "shift", 0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
                 0x12: "alt", 0xA4: "alt", 0xA5: "alt", 0x5B: "win", 0x5C: "win"}

_SHOWN = {
    "ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "win": "Win", "esc": "Esc", "page_up": "Page Up",
    "page_down": "Page Down", "print_screen": "Print Screen", "caps_lock": "Caps Lock",
    "plus": "+", "minus": "-", "comma": ",", "period": ".", "num_mul": "Num *", "num_add": "Num +",
    "num_sub": "Num -", "num_dot": "Num .", "num_div": "Num /", "volume_up": "Volume Up",
    "volume_down": "Volume Down", "mute": "Mute", "play_pause": "Play / Pause", "next_track": "Next Track",
    "prev_track": "Previous Track", "media_stop": "Stop",
}

# Keys most keyboards can't press, offered from a list in the editor
SPECIAL_KEYS = ("play_pause", "prev_track", "next_track", "media_stop", "mute", "volume_up", "volume_down",
                *(f"f{i}" for i in range(13, 25)), *(f"num{i}" for i in range(10)),
                "num_add", "num_sub", "num_mul", "num_div", "num_dot", "print_screen", "menu")


def modifier_for_vk(vk: int) -> str | None:
    """"ctrl" / "shift" / "alt" / "win" for a modifier key's code, else None."""
    return _VK_MODIFIERS.get(vk)


def name_for_vk(vk: int) -> str | None:
    """Our name for a (non-modifier) key's virtual-key code; None if it has none."""
    return _VK_NAMES.get(vk)


def combo_spec(combo: tuple[str, ...]) -> str:
    """("ctrl", "z") -> "ctrl+z" (as saved in profiles.json)."""
    return "+".join(combo)


def key_text(name: str) -> str:
    """One key as the editor shows it: "ctrl" -> "Ctrl", "z" -> "Z", "num1" -> "Num 1"."""
    if name in _SHOWN:
        return _SHOWN[name]
    if len(name) == 1:
        return name.upper()
    if name.startswith("num") and name[3:].isdigit():
        return f"Num {name[3:]}"
    if name.startswith("f") and name[1:].isdigit():
        return name.upper()
    return name.replace("_", " ").title()


def combo_text(combo: tuple[str, ...] | str) -> str:
    """("ctrl", "shift", "z") or "ctrl+shift+z" -> "Ctrl+Shift+Z" ("" if it doesn't parse)."""
    if isinstance(combo, str):
        try:
            combo = parse_combo(combo)
        except ComboError:
            return ""
    return "+".join(key_text(k) for k in combo)
