"""Profiles: per-app button and dial mappings, loaded from JSON.

{
  "profiles": [
    {
      "name": "Default",
      "buttons": {"K1": "ctrl+z", "DIAL": {"dial_mode": "next"}},
      "dial": [
        {"name": "Volume", "cw": "volume_up", "ccw": "volume_down"}
      ]
    },
    {
      "name": "Krita",
      "apps": ["krita.exe"],
      "inherits": "Default",          # start from Default's buttons/dial
      "buttons": {"K1": "ctrl+z"},    # override just these
      "labels": {"K1": "Undo"}        # names shown on the keys in the editor (optional)
    }
    # "apps": ["@browsers"] matches every web browser (core.keydial.browsers)
  ]
}

The profile used is the first one whose "apps" contains the foreground
program's exe name; otherwise the profile named "Default".
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import browsers
from .actions import Action, ConfigError, Hold, parse_action
from .protocol import ALL_BUTTONS

DEFAULT_PROFILE = "Default"


@dataclass(frozen=True)
class DialModeSpec:
    name: str
    cw: Optional[Action]
    ccw: Optional[Action]


@dataclass
class Profile:
    name: str
    apps: frozenset[str] = frozenset()
    buttons: dict[str, Optional[Action]] = field(default_factory=dict)
    dial: list[DialModeSpec] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    profiles: list[Profile]

    @property
    def default(self) -> Profile:
        return next(p for p in self.profiles if p.name == DEFAULT_PROFILE)

    def named(self, name: Optional[str]) -> Optional[Profile]:
        return next((p for p in self.profiles if p.name == name), None)

    def profile_for(self, exe: Optional[str]) -> Profile:
        if exe:
            exe = exe.lower()
            for p in self.profiles:
                if exe in p.apps:
                    return p
        return self.default


def _parse_dial(raw: Any, where: str) -> list[DialModeSpec]:
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: 'dial' must be a list of modes")
    modes = []
    for i, m in enumerate(raw):
        w = f"{where} dial mode {i + 1}"
        if not isinstance(m, dict) or not isinstance(m.get("name"), str):
            raise ConfigError(f"{w}: each mode needs a 'name'")
        extra = set(m) - {"name", "cw", "ccw"}
        if extra:
            raise ConfigError(f"{w}: unexpected field(s) {sorted(extra)}")
        cw = parse_action(m.get("cw"), f"{w} cw")
        ccw = parse_action(m.get("ccw"), f"{w} ccw")
        if isinstance(cw, Hold) or isinstance(ccw, Hold):
            raise ConfigError(f"{w}: 'hold' doesn't make sense on the dial")
        modes.append(DialModeSpec(m["name"], cw, ccw))
    return modes


def load_config(data: Any) -> Config:
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        raise ConfigError("config must be an object with a 'profiles' list")
    profiles: list[Profile] = []
    by_name: dict[str, Profile] = {}
    for i, raw in enumerate(data["profiles"]):
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise ConfigError(f"profile {i + 1}: needs a 'name'")
        name = raw["name"]
        where = f"profile {name!r}"
        if name in by_name:
            raise ConfigError(f"{where}: duplicate profile name")
        extra = set(raw) - {"name", "apps", "inherits", "buttons", "dial", "labels"}
        if extra:
            raise ConfigError(f"{where}: unexpected field(s) {sorted(extra)}")

        prof = Profile(name)
        parent_name = raw.get("inherits")
        if parent_name is not None:
            parent = by_name.get(parent_name)
            if parent is None:
                raise ConfigError(f"{where}: inherits {parent_name!r}, which must be defined above it")
            prof.buttons = dict(parent.buttons)
            prof.dial = list(parent.dial)
            prof.labels = dict(parent.labels)

        apps = raw.get("apps", [])
        if not isinstance(apps, list) or not all(isinstance(a, str) for a in apps):
            raise ConfigError(f"{where}: 'apps' must be a list of exe names")
        prof.apps = frozenset(a.lower() for a in apps)
        if browsers.TOKEN in prof.apps:  # ("@browsers": every web browser)
            prof.apps = prof.apps | browsers.all_browsers()

        buttons = raw.get("buttons", {})
        if not isinstance(buttons, dict):
            raise ConfigError(f"{where}: 'buttons' must be an object")
        for btn, spec in buttons.items():
            if btn not in ALL_BUTTONS:
                raise ConfigError(f"{where}: unknown button {btn!r} (use K1..K18 or DIAL)")
            prof.buttons[btn] = parse_action(spec, f"{where} {btn}")

        labels = raw.get("labels", {})
        if not isinstance(labels, dict) or not all(isinstance(v, str) for v in labels.values()):
            raise ConfigError(f"{where}: 'labels' must map buttons to names")
        prof.labels.update({k: v for k, v in labels.items() if k in ALL_BUTTONS})

        if "dial" in raw:
            prof.dial = _parse_dial(raw["dial"], where)

        profiles.append(prof)
        by_name[name] = prof

    if DEFAULT_PROFILE not in by_name:
        raise ConfigError(f"config needs a profile named {DEFAULT_PROFILE!r}")
    return Config(profiles)


def load_config_file(path: os.PathLike | str) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"can't read {path}: {exc}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from None
    return load_config(data)


class ConfigStore:
    """Holds the current config and reloads it when the file changes.

    A broken edit is reported through `on_error` and the last good config
    stays active, so a typo never leaves the device dead.
    """

    def __init__(self, path: os.PathLike | str, on_error: Callable[[str], None] = print,
                 fallback: Optional[Config] = None) -> None:
        """fallback: used if the file can't be read at the start (else that raises ConfigError)."""
        self.path = Path(path)
        self._on_error = on_error
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None
        try:
            self._config = load_config_file(self.path)
            self.error: Optional[str] = None
        except ConfigError as exc:
            if fallback is None:
                raise
            self._config = fallback
            self.error = str(exc)
            on_error(f"config NOT loaded, using the starter profiles: {exc}")

    def get(self) -> Config:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._config
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self._config = load_config_file(self.path)
                self.error = None
                self._on_error(f"config reloaded from {self.path.name}")
            except ConfigError as exc:
                self.error = str(exc)
                self._on_error(f"config NOT reloaded, keeping previous one: {exc}")
        return self._config

    def set(self, data: Any) -> None:
        """Use data (a profiles document the editor just saved) straight away."""
        self._config = load_config(data)
        self.error = None
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            pass
