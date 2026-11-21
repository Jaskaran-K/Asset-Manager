"""Changing the profiles document (profiles.json as saved) the way the editor does.

A profile can inherit another ("inherits", in practice "Default"): keys, names and dial modes it
doesn't set itself are the other one's. These helpers work that out and keep it intact when a
profile is renamed or deleted.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from .profiles import DEFAULT_PROFILE
from .protocol import ALL_BUTTONS


def profile_names(doc: dict) -> list[str]:
    return [p.get("name", "") for p in doc.get("profiles", [])]


def find(doc: dict, name: str) -> Optional[dict]:
    return next((p for p in doc.get("profiles", []) if p.get("name") == name), None)


def resolve(doc: dict, name: str) -> dict:
    """What the profile does, inherited parts included:
    {"buttons": {button: spec}, "labels": {button: name}, "dial": [modes],
     "own": {buttons it sets itself}, "own_dial": bool, "parent": name or None}."""
    profile = find(doc, name) or {}
    parent_name = profile.get("inherits")
    if parent_name and parent_name != name and find(doc, parent_name) is not None:
        base = resolve(doc, parent_name)
    else:
        base = {"buttons": {}, "labels": {}, "dial": []}
        parent_name = None
    buttons = dict(base["buttons"])
    labels = dict(base["labels"])
    own = set()
    for button, spec in (profile.get("buttons") or {}).items():
        buttons[button] = spec
        own.add(button)
    for button, label in (profile.get("labels") or {}).items():
        labels[button] = label
        own.add(button)
    own_dial = "dial" in profile
    return {"buttons": buttons, "labels": labels, "dial": copy.deepcopy(profile["dial"] if own_dial else base["dial"]),
            "own": own, "own_dial": own_dial, "parent": parent_name}


def set_button(doc: dict, name: str, button: str, spec: Any, label: Optional[str] = None) -> None:
    """The profile's own action (and name) for a key; spec None: the key does nothing."""
    if button not in ALL_BUTTONS:
        raise KeyError(button)
    profile = find(doc, name)
    profile.setdefault("buttons", {})[button] = copy.deepcopy(spec)
    if label is not None:
        profile.setdefault("labels", {})[button] = label


def clear_button(doc: dict, name: str, button: str) -> None:
    """The key goes back to doing what the inherited profile does."""
    profile = find(doc, name)
    for key in ("buttons", "labels"):
        (profile.get(key) or {}).pop(button, None)


def set_dial(doc: dict, name: str, modes: list) -> None:
    find(doc, name)["dial"] = copy.deepcopy(modes)


def clear_dial(doc: dict, name: str) -> None:
    find(doc, name).pop("dial", None)


def unique_name(doc: dict, wanted: str) -> str:
    names = set(profile_names(doc))
    if wanted not in names:
        return wanted
    n = 2
    while f"{wanted} {n}" in names:
        n += 1
    return f"{wanted} {n}"


def add_profile(doc: dict, profile: dict) -> str:
    """Add a profile at the end (its name made unique). Returns its name."""
    profile = copy.deepcopy(profile)
    profile["name"] = unique_name(doc, profile.get("name") or "Profile")
    if profile.get("inherits") and find(doc, profile["inherits"]) is None:
        profile["inherits"] = DEFAULT_PROFILE
    doc.setdefault("profiles", []).append(profile)
    return profile["name"]


def replace_profile(doc: dict, name: str, profile: dict) -> None:
    """Put profile (a starter layout) in place of name's, keeping its name and programs."""
    old = find(doc, name)
    fresh = copy.deepcopy(profile)
    fresh["name"] = name
    if old.get("apps"):
        fresh["apps"] = list(old["apps"])
    if name == DEFAULT_PROFILE:
        fresh.pop("inherits", None)
    doc["profiles"][doc["profiles"].index(old)] = fresh


def rename_profile(doc: dict, old: str, new: str) -> str:
    """Rename (made unique); profiles inheriting it follow. Default keeps its name. Returns the name used."""
    if old == DEFAULT_PROFILE or not new.strip() or new == old:
        return old
    new = unique_name(doc, new.strip())
    find(doc, old)["name"] = new
    for profile in doc.get("profiles", []):
        if profile.get("inherits") == old:
            profile["inherits"] = new
    return new


def remove_profile(doc: dict, name: str) -> None:
    """Delete a profile (never Default); profiles that inherited it inherit what it did."""
    if name == DEFAULT_PROFILE:
        raise ValueError("Default can't be deleted")
    gone = find(doc, name)
    if gone is None:
        return
    for profile in doc.get("profiles", []):
        if profile.get("inherits") == name:
            if gone.get("inherits"):
                profile["inherits"] = gone["inherits"]
            else:
                profile.pop("inherits", None)
    doc["profiles"].remove(gone)


def set_apps(doc: dict, name: str, apps: list[str]) -> None:
    profile = find(doc, name)
    cleaned = []
    for app in apps:
        app = app.strip().lower()
        if app and app not in cleaned:
            cleaned.append(app)
    if cleaned:
        profile["apps"] = cleaned
    else:
        profile.pop("apps", None)


def set_inherits(doc: dict, name: str, inherits: bool) -> None:
    profile = find(doc, name)
    if inherits and name != DEFAULT_PROFILE:
        profile["inherits"] = DEFAULT_PROFILE
    else:
        profile.pop("inherits", None)
