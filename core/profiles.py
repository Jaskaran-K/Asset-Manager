"""Profiles: separate libraries (a "ZBrush" one, a "Substance Painter" one), switched between.

A profile has its own library folders, file types, categories and collections (saved searches,
core.tag_repository) and its own startup snapshot (core.library_snapshot). Everything else is
shared: tags, favorites and ratings belong to the files, and the theme, apps, viewer, Artbook
and shortcuts are the app's.

Stored in the config (core.settings): the current profile's lists stay at the top level, where
they always were (so the rest of the app, and an older version of it, reads them unchanged), and
"profiles" lists every profile with the saved lists of the ones not in use. Switching swaps them.
A config from before profiles has one, "Default" (id "default"), with its current lists.
"""

from __future__ import annotations

import copy
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import settings

DEFAULT_ID = "default"
DEFAULT_NAME = "Default"
MAX_NAME_LENGTH = 40
# The config keys a profile has its own of ("default_folder": the old single library folder)
PROFILE_KEYS = ("scan_folders", "default_folder", "file_extensions", "categories", "defaults_seen")
_BAD_NAME_CHARACTERS = '<>:"/\\|?*'  # (a profile's name goes in a desktop shortcut's file name)


class ProfileError(ValueError):
    """Invalid profile change; the message is suitable to show to the user."""


@dataclass(frozen=True)
class Profile:
    id: str
    name: str


def _entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    """The config's profiles (one, Default, until a second is made)."""
    raw = config.get("profiles")
    entries = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("name"), str):
                entries.append(entry)
    return entries or [{"id": DEFAULT_ID, "name": DEFAULT_NAME}]


def _current_id(config: dict[str, Any], entries: list[dict[str, Any]]) -> str:
    current = config.get("profile")
    return current if any(e["id"] == current for e in entries) else entries[0]["id"]


def list_profiles() -> list[Profile]:
    """Every profile, in the order they were made."""
    return [Profile(e["id"], e["name"]) for e in _entries(settings._load_raw_config())]


def current_profile() -> Profile:
    config = settings._load_raw_config()
    entries = _entries(config)
    current = _current_id(config, entries)
    return next(Profile(e["id"], e["name"]) for e in entries if e["id"] == current)


def find_profile(name_or_id: str) -> Optional[Profile]:
    """A profile by its name (ignoring case) or id, for --profile."""
    wanted = " ".join(name_or_id.split()).casefold()
    profiles = list_profiles()
    return next((p for p in profiles if p.id == name_or_id), None) or next(
        (p for p in profiles if p.name.casefold() == wanted), None
    )


def validate_name(name: str, exclude_id: Optional[str] = None, taken: Optional[list[Profile]] = None) -> str:
    """The name, tidied, or ProfileError saying what's wrong with it. taken: the profiles to
    compare with (default: the saved ones)."""
    name = " ".join(name.split())
    if not name:
        raise ProfileError("A profile needs a name.")
    if len(name) > MAX_NAME_LENGTH:
        raise ProfileError(f"Profile names can be up to {MAX_NAME_LENGTH} characters long.")
    if any(c in _BAD_NAME_CHARACTERS for c in name):
        raise ProfileError(f"Profile names can't contain any of these: {' '.join(_BAD_NAME_CHARACTERS)}")
    for profile in list_profiles() if taken is None else taken:
        if profile.id != exclude_id and profile.name.casefold() == name.casefold():
            raise ProfileError(f'There is already a profile named "{profile.name}".')
    return name


def _lists_of(config: dict[str, Any], entry: dict[str, Any], current: str) -> dict[str, Any]:
    """A profile's own lists (the ones it stores; the rest follow the defaults)."""
    source = config if entry["id"] == current else entry
    return {key: copy.deepcopy(source[key]) for key in PROFILE_KEYS if key in source}


def new_lists(copy_of: Optional[str] = None) -> dict[str, Any]:
    """The lists a new profile starts with: a copy of a profile's, or no folders, no categories
    (the sidebar has just All Assets and Favorites) and the default file types."""
    if copy_of is None:
        return {"scan_folders": [], "categories": [], "defaults_seen": settings.DEFAULTS_VERSION}
    config = settings._load_raw_config()
    entries = _entries(config)
    entry = next((e for e in entries if e["id"] == copy_of), None)
    if entry is None:
        raise ProfileError("That profile no longer exists.")
    return _lists_of(config, entry, _current_id(config, entries))


def create_profile(name: str, copy_of: Optional[str] = None, lists: Optional[dict[str, Any]] = None) -> Profile:
    """Add a profile (not switched to). copy_of: a profile whose folders, file types and
    categories it starts with; else it has no folders or categories, and the default file types.
    lists: start with these instead (Settings copies a profile as it was when it was asked to)."""
    name = validate_name(name)
    config = settings._load_raw_config()
    entries = _entries(config)
    if "profiles" not in config:
        config["profile"] = entries[0]["id"]
    profile = Profile(uuid.uuid4().hex[:12], name)
    start = copy.deepcopy(lists) if lists is not None else new_lists(copy_of)
    entries.append({"id": profile.id, "name": profile.name, **start})
    config["profiles"] = entries
    settings._save_raw_config(config)
    return profile


def rename_profile(profile_id: str, name: str) -> Profile:
    name = validate_name(name, exclude_id=profile_id)
    config = settings._load_raw_config()
    entries = _entries(config)
    entry = next((e for e in entries if e["id"] == profile_id), None)
    if entry is None:
        raise ProfileError("That profile no longer exists.")
    entry["name"] = name
    config["profiles"] = entries
    config.setdefault("profile", _current_id(config, entries))
    settings._save_raw_config(config)
    return Profile(profile_id, name)


def delete_profile(profile_id: str) -> None:
    """Remove a profile (not the one in use: switch to another first). Its collections are the
    caller's to delete (core.tag_repository); its snapshot is deleted here."""
    config = settings._load_raw_config()
    entries = _entries(config)
    if profile_id == _current_id(config, entries):
        raise ProfileError("The profile in use can't be deleted. Switch to another one first.")
    if not any(e["id"] == profile_id for e in entries):
        return
    config["profiles"] = [e for e in entries if e["id"] != profile_id]
    settings._save_raw_config(config)
    from .library_snapshot import snapshot_path  # (it reads the library database's folder)

    try:
        snapshot_path(profile_id).unlink(missing_ok=True)
    except OSError:
        pass


def switch_profile(profile_id: str) -> Profile:
    """Make a profile the one in use: its lists go to the top of the config, and the old one's
    are kept in its entry. Then new default file types reach its lists (apply_new_defaults)."""
    config = settings._load_raw_config()
    entries = _entries(config)
    current = _current_id(config, entries)
    target = next((e for e in entries if e["id"] == profile_id), None)
    if target is None:
        raise ProfileError("That profile no longer exists.")
    if profile_id != current:
        old = next(e for e in entries if e["id"] == current)
        for key in PROFILE_KEYS:
            old.pop(key, None)
            if key in config:
                old[key] = config.pop(key)
        for key in PROFILE_KEYS:
            if key in target:
                config[key] = target.pop(key)
    config["profiles"] = entries
    config["profile"] = profile_id
    settings._save_raw_config(config)
    settings.apply_new_defaults()
    return Profile(target["id"], target["name"])


def other_profiles_folders() -> list[Path]:
    """The library folders of every profile but the one in use (a scan keeps what's known about
    their files: core.tag_repository.forget_unscanned_assets)."""
    config = settings._load_raw_config()
    entries = _entries(config)
    current = _current_id(config, entries)
    folders: list[Path] = []
    for entry in entries:
        if entry["id"] == current:
            continue
        stored = entry.get("scan_folders")
        if stored is None and isinstance(entry.get("default_folder"), str):
            stored = [entry["default_folder"]]
        if isinstance(stored, list):
            folders += [Path(f) for f in stored if isinstance(f, str) and f]
    return folders


def every_profiles_categories() -> list[list[dict[str, Any]]]:
    """Each profile's categories (the defaults for one that doesn't store its own)."""
    config = settings._load_raw_config()
    entries = _entries(config)
    current = _current_id(config, entries)
    result = []
    for entry in entries:
        raw = (config if entry["id"] == current else entry).get("categories")
        categories = settings._sanitize_categories(raw)  # ([] is a profile with no categories)
        result.append(copy.deepcopy(settings.DEFAULT_CATEGORIES) if categories is None else categories)
    return result


def edit_other_profiles_categories(edit: Callable[[list[str]], list[str]]) -> None:
    """Change the tag lists of the categories of every profile not in use (a tag was renamed or
    deleted: Settings edits the current profile's itself)."""
    config = settings._load_raw_config()
    entries = _entries(config)
    current = _current_id(config, entries)
    changed = False
    for entry in entries:
        if entry["id"] == current or not isinstance(entry.get("categories"), list):
            continue
        for main in entry["categories"]:
            if not isinstance(main, dict):
                continue
            for item in (main, *(s for s in main.get("subcategories", []) if isinstance(s, dict))):
                if isinstance(item.get("tags"), list):
                    tags = edit(list(item["tags"]))
                    if tags != item["tags"]:
                        item["tags"] = tags
                        changed = True
    if changed:
        config["profiles"] = entries
        settings._save_raw_config(config)


def shortcut_name(profile: Profile) -> str:
    """The desktop shortcut's file name for a profile."""
    return f"Asset Manager - {profile.name}.lnk"


def launch_command(profile: Profile) -> tuple[str, str, str]:
    """(program, arguments, working folder) that open the app in a profile (by id: renaming it
    keeps the shortcut working)."""
    if getattr(sys, "frozen", False):
        program = sys.executable
        return program, f"--profile {profile.id}", os.path.dirname(program)
    python = Path(sys.executable)
    windowed = python.with_name("pythonw.exe")
    main = Path(__file__).resolve().parent.parent / "main.py"
    return (
        str(windowed if windowed.exists() else python),
        f'"{main}" --profile {profile.id}',
        str(main.parent),
    )



_SHORTCUT_SCRIPT = r"""
$folder = $env:AM_FOLDER
if (-not $folder) { $folder = [Environment]::GetFolderPath('Desktop') }
$path = Join-Path $folder $env:AM_NAME
$link = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
$link.TargetPath = $env:AM_TARGET
$link.Arguments = $env:AM_ARGS
$link.WorkingDirectory = $env:AM_DIR
$link.IconLocation = $env:AM_ICON
$link.Description = $env:AM_DESCRIPTION
$link.Save()
Write-Output $path
"""


def create_desktop_shortcut(profile: Profile, folder: Optional[Path] = None) -> Path:
    """Make a Windows shortcut that opens the app in a profile, on the desktop (or in folder).
    Returns where it went; ProfileError if Windows wouldn't make it."""
    import subprocess

    program, arguments, working = launch_command(profile)
    icon = program if getattr(sys, "frozen", False) else str(Path(__file__).resolve().parent.parent / "assets" / "Asset-Manager-Icon.ico")
    env = {
        **os.environ,
        "AM_FOLDER": str(folder) if folder is not None else "",
        "AM_NAME": shortcut_name(profile),
        "AM_TARGET": program,
        "AM_ARGS": arguments,
        "AM_DIR": working,
        "AM_ICON": f"{icon},0",
        "AM_DESCRIPTION": f"Asset Manager, in the {profile.name} profile",
    }
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _SHORTCUT_SCRIPT],
            env=env, capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProfileError(f"Windows couldn't make the shortcut: {exc}") from exc
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not lines or not Path(lines[-1]).is_file():
        detail = (result.stderr or "").strip().splitlines()
        raise ProfileError("Windows couldn't make the shortcut" + (f": {detail[0]}" if detail else "."))
    return Path(lines[-1])
