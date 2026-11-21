"""Action types and parsing from config JSON.

Config forms (anywhere an action is expected):

    "ctrl+z"                                  tap a key combo
    null  or  "none"                          do nothing
    {"keys": "ctrl+z"}                        tap a key combo
    {"hold": "space"}                         hold while the button is held
    {"text": "hello"}                         type text
    {"scroll": 3}                             mouse wheel (+ up / - down)
    {"scroll": 1, "with": "ctrl"}             wheel while holding modifiers
    {"hscroll": 3}                            horizontal wheel (+ right)
    {"launch": "C:/path/app.exe", "args": []} start a program / open a file or URL
    {"delay": 100}                            wait (ms) - for macros
    {"macro": [step, step, ...]}              run steps in order (any of the above except hold)
    {"dial_mode": "next" | "prev" | "<mode name>"}  switch the dial's mode
    {"profile": "next" | "prev" | "auto" | "<profile name>"}  manual mode with that profile (or back to auto)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union

from .keys import ComboError, parse_combo


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Keys:
    combo: tuple[str, ...]


@dataclass(frozen=True)
class Hold:
    combo: tuple[str, ...]


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Scroll:
    dx: int = 0
    dy: int = 0
    modifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Launch:
    target: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Delay:
    ms: int


@dataclass(frozen=True)
class Macro:
    steps: tuple["Action", ...]


@dataclass(frozen=True)
class DialMode:
    target: str  # "next", "prev" or a mode name


@dataclass(frozen=True)
class ProfileSwitch:
    target: str  # "next", "prev", "auto" or a profile name


Action = Union[Keys, Hold, Text, Scroll, Launch, Delay, Macro, DialMode, ProfileSwitch]

_PRIMARY = ("keys", "hold", "text", "scroll", "hscroll", "launch", "delay", "macro", "dial_mode", "profile")
_ALLOWED_EXTRA = {"scroll": {"with"}, "hscroll": {"with"}, "launch": {"args"}}


def _combo(value: Any, where: str) -> tuple[str, ...]:
    try:
        return parse_combo(value)
    except ComboError as exc:
        raise ConfigError(f"{where}: {exc}") from None


def _int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected a whole number, got {value!r}")
    return value


def parse_action(spec: Any, where: str = "action", *, in_macro: bool = False) -> Optional[Action]:
    if spec is None:
        return None
    if isinstance(spec, str):
        if spec.strip().lower() in ("", "none"):
            return None
        return Keys(_combo(spec, where))
    if not isinstance(spec, dict):
        raise ConfigError(f"{where}: expected a string or object, got {type(spec).__name__}")

    kinds = [k for k in _PRIMARY if k in spec]
    if len(kinds) != 1:
        raise ConfigError(f"{where}: need exactly one of {', '.join(_PRIMARY)}; got {sorted(spec)}")
    kind = kinds[0]
    extra = set(spec) - {kind} - _ALLOWED_EXTRA.get(kind, set())
    if extra:
        raise ConfigError(f"{where}: unexpected field(s) {sorted(extra)} for {kind!r}")
    value = spec[kind]

    if kind == "keys":
        return Keys(_combo(value, where))
    if kind == "hold":
        if in_macro:
            raise ConfigError(f"{where}: 'hold' can't be used inside a macro")
        return Hold(_combo(value, where))
    if kind == "text":
        if not isinstance(value, str):
            raise ConfigError(f"{where}: text must be a string")
        return Text(value)
    if kind in ("scroll", "hscroll"):
        amount = _int(value, where)
        mods = _combo(spec["with"], where) if "with" in spec else ()
        return Scroll(dx=amount, modifiers=mods) if kind == "hscroll" else Scroll(dy=amount, modifiers=mods)
    if kind == "launch":
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{where}: launch target must be a non-empty string")
        args = spec.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ConfigError(f"{where}: args must be a list of strings")
        return Launch(value, tuple(args))
    if kind == "delay":
        ms = _int(value, where)
        if ms < 0:
            raise ConfigError(f"{where}: delay can't be negative")
        return Delay(ms)
    if kind == "macro":
        if not isinstance(value, list) or not value:
            raise ConfigError(f"{where}: macro must be a non-empty list of steps")
        steps = []
        for i, step in enumerate(value):
            a = parse_action(step, f"{where} step {i + 1}", in_macro=True)
            if a is not None:
                steps.append(a)
        return Macro(tuple(steps))
    if kind == "dial_mode":
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{where}: dial_mode must be 'next', 'prev' or a mode name")
        return DialMode(value)
    if kind == "profile":
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{where}: profile must be 'next', 'prev', 'auto' or a profile name")
        return ProfileSwitch(value)
    raise AssertionError(kind)


# -- the editor: what kind an action (as saved) is, and how it's described ---------------------
KINDS = ("keys", "hold", "text", "scroll", "launch", "macro", "dial_mode", "profile", "none")


def kind_of(spec: Any) -> str:
    """Which of KINDS an action spec is ("delay" only appears inside macros; "hscroll" is a scroll)."""
    if spec is None or (isinstance(spec, str) and spec.strip().lower() in ("", "none")):
        return "none"
    if isinstance(spec, str):
        return "keys"
    if isinstance(spec, dict):
        for kind in ("keys", "hold", "text", "launch", "macro", "dial_mode", "profile", "delay"):
            if kind in spec:
                return kind
        if "scroll" in spec or "hscroll" in spec:
            return "scroll"
    return "none"


def scroll_parts(spec: dict) -> tuple[str, int, str]:
    """(direction "up" / "down" / "left" / "right", steps, held modifiers as "ctrl+shift" or "")."""
    if "hscroll" in spec:
        amount = int(spec["hscroll"])
        direction = "right" if amount >= 0 else "left"
    else:
        amount = int(spec.get("scroll", 0))
        direction = "up" if amount >= 0 else "down"
    return direction, abs(amount), str(spec.get("with", ""))


def scroll_spec(direction: str, steps: int, held: str = "") -> dict:
    steps = max(1, int(steps))
    spec: dict[str, Any] = ({"hscroll": steps if direction == "right" else -steps} if direction in ("left", "right")
                            else {"scroll": steps if direction == "up" else -steps})
    if held:
        spec["with"] = held
    return spec


def describe(spec: Any) -> str:
    """Short text for an action: "Ctrl+Z", "hold Space", "Scroll up 2 (Ctrl)", "Macro: 4 steps"."""
    from .keys import combo_text

    kind = kind_of(spec)
    if kind == "none":
        return "Nothing"
    if kind == "keys":
        return combo_text(spec if isinstance(spec, str) else spec["keys"]) or str(spec)
    if kind == "hold":
        return f"hold {combo_text(spec['hold'])}"
    if kind == "text":
        text = spec["text"]
        return f'Type "{text if len(text) <= 18 else text[:17] + "..."}"'
    if kind == "scroll":
        direction, steps, held = scroll_parts(spec)
        return f"Scroll {direction}" + (f" {steps}" if steps != 1 else "") + (f" ({combo_text(held)})" if held else "")
    if kind == "launch":
        target = spec["launch"].replace("\\", "/").rstrip("/")
        return f"Open {target.rsplit('/', 1)[-1] or target}"
    if kind == "macro":
        steps = spec["macro"]
        return f"Macro: {len(steps)} step" + ("" if len(steps) == 1 else "s")
    if kind == "dial_mode":
        target = spec["dial_mode"]
        return {"next": "Next dial mode", "prev": "Previous dial mode"}.get(target, f"Dial: {target}")
    if kind == "profile":
        target = spec["profile"]
        return {"next": "Next profile", "prev": "Previous profile", "auto": "Auto mode"}.get(target, f"Profile: {target}")
    if kind == "delay":
        return f"Wait {spec['delay']} ms"
    return str(spec)
