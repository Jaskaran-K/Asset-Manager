"""Where KeyDial keeps its files: %APPDATA%\\KeyDial

    profiles.json   the profiles (hand edits are fine: the app reloads it when it's saved)
    settings.json   start_with_windows, run_as_admin, show_popup, popup_place, mode ("auto": the
                    profile follows the app in front; "manual": manual_profile everywhere),
                    manual_profile, huion_tip_seen, tray_tip_seen, editor_geometry
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import browsers, presets
from .actions import ConfigError
from .profiles import load_config

DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "KeyDial"

DEFAULT_SETTINGS: dict[str, Any] = {
    "start_with_windows": True,
    "run_as_admin": False,
    "show_popup": True,
    "popup_place": "bottom",  # "bottom" or "top" of the screen
    "mode": "auto",  # "auto": the profile follows the app in front; "manual": manual_profile in every app
    "manual_profile": None,
    "huion_tip_seen": False,
    "tray_tip_seen": False,
    "editor_geometry": None,
}


def profiles_path() -> Path:
    return DATA_DIR / "profiles.json"


def settings_path() -> Path:
    return DATA_DIR / "settings.json"


def _write(path: Path, data: Any) -> None:
    """Write JSON so a crash midway never leaves half a file (written beside it, then swapped in)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def ensure_profiles() -> Path:
    """profiles.json, made from the starter profiles the first time."""
    path = profiles_path()
    if not path.exists():
        _write(path, presets.starter_document())
    return path


def load_document() -> tuple[dict, str | None]:
    """The profiles as saved (for the editor), and why they couldn't be read (then the starter ones).
    A file from an older KeyDial is upgraded first (upgrade_document), its old copy kept beside it."""
    path = ensure_profiles()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        load_config(data)  # (checked, so the editor never starts from something the keys can't use)
    except (OSError, ValueError) as exc:  # (ConfigError and JSONDecodeError are ValueErrors)
        return presets.starter_document(), str(exc)
    if upgrade_document(data):
        try:
            load_config(data)
            backup = path.with_name(f"profiles.starter-{presets.STARTER_VERSION - 1}.json")
            if not backup.exists():
                backup.write_bytes(path.read_bytes())
            _write(path, data)
        except (OSError, ValueError):
            pass  # (the upgrade is tried again next time)
    return data, None


def _merge_keys(profile: dict, starter: dict, old_buttons: dict) -> None:
    """Keys still set as the old starter layout had them (or not set) take the new layout's action
    and name; keys changed since stay as they are."""
    buttons = profile.setdefault("buttons", {})
    labels = profile.setdefault("labels", {})
    for button, action in starter["buttons"].items():
        if button not in buttons or buttons[button] == old_buttons.get(button, "__unset__"):
            buttons[button] = action
            if button in starter.get("labels", {}):
                labels[button] = starter["labels"][button]


def upgrade_document(doc: dict) -> bool:
    """Bring profiles.json from an older KeyDial up to the current starter profiles (True if changed):
    version 2 made Default a number pad, filled in Multimedia, and added Browsers."""
    if doc.get("starter", 1) >= presets.STARTER_VERSION:
        return False
    by_name = {p.get("name"): p for p in doc.get("profiles", [])}
    if "Default" in by_name:
        _merge_keys(by_name["Default"], presets.DEFAULT, presets.DEFAULT_V1_BUTTONS)
    if "Multimedia" in by_name:
        _merge_keys(by_name["Multimedia"], presets.MULTIMEDIA, presets.MULTIMEDIA_V1_BUTTONS)
    for preset in presets.APP_PRESETS:  # (keys a starter layout sets now and the profile doesn't: Toolbag's spares)
        if preset["name"] in by_name and preset["name"] != "Multimedia":
            _merge_keys(by_name[preset["name"]], preset, {})
    has_browsers = any("Browsers" == p.get("name") or browsers.TOKEN in (p.get("apps") or []) for p in doc["profiles"])
    if not has_browsers:
        doc["profiles"].append(presets.preset_for("Browsers"))
    doc["starter"] = presets.STARTER_VERSION
    return True


def save_document(data: dict) -> None:
    """Save the editor's profiles; raises ConfigError (and saves nothing) if they don't parse."""
    load_config(data)
    _write(profiles_path(), data)


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(settings_path().read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            if saved.get("pinned") and "mode" not in saved:  # (the first KeyDial's "Use Profile": manual mode)
                saved.update(mode="manual", manual_profile=saved["pinned"])
            settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return settings


def save_setting(key: str, value: Any) -> None:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    settings = load_settings()
    settings[key] = value
    _write(settings_path(), settings)


__all__ = ["DATA_DIR", "ConfigError", "ensure_profiles", "load_document", "save_document", "load_settings",
           "save_setting", "profiles_path", "settings_path"]
