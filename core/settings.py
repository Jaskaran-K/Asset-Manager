"""Application settings and configuration persistence.

Persists user preferences to ~/.3d_asset_manager_config.json:
- scan_folders:     library folders scanned on startup / refresh
- file_extensions:  file types the scanner picks up
- categories:       sidebar main categories, their subcategories, file types, tags, colors and icons
- theme:            UI theme id ("dark", "light", "dark_purple", "neon_cyan")
- thumbnail_size:   grid thumbnail size in pixels
- ffmpeg_path:      ffmpeg executable for video proxies / poster frames (blank = search PATH)
- media_backend:    Qt multimedia backend ("auto", "ffmpeg", "windows"); applied on restart
- video_skip_seconds: jump time for the video player's skip buttons and arrow keys
- video_speed:      playback speed (0.25-3.0)
- video_positions:  where recently watched videos were left, to resume there
- motion:           "auto" (follow Windows), "on" or "off" for UI animations
- background_texture: pattern behind windows and the grid (see ui.backgrounds)
- sidebar:          {"open", "pinned", "width"} of the library's slide-out sidebar
- artbook_sidebar:  the same for the Artbook's Pages sidebar
- glass_frost:      0-100, how frosted glass panels (the sidebar) are
- artbook_page:     id of the Artbook page last open (pages themselves: core.artbook_store)
- artbook_pages_dir: where the Artbook's pages are kept ("" = the default folder)
- view:             {"mode", "split", "window"}: Asset / Artbook / Split / Separate view, the split
                    ratio, and the separate Artbook window's geometry
- artbook_window:   {"on_top", "opacity", "click_through_tip"} of the separate Artbook window
                    (click-through itself is never remembered: the window would start out ignoring
                    the mouse; click_through_tip: show the heads-up when turning it on)
- archive_contents: show the files inside archives (zip, RAR, 7z) among the library's files
- seven_zip_path:   7-Zip's 7z.exe for RAR / 7z archives ("" = find it)
- found_apps:       {app id: [programs found on this computer, newest first]} (core.apps.detect)
- app_choices:      {app id: program}: the one picked in Settings > Apps, if not the newest found
- custom_apps:      [{"id", "name", "program", "types"}]: apps added by hand in Settings > Apps
- app_types:        {app id: [file types]}: Open In's types for an app, if set by hand
- board_pages:      {PureRef board: {"page", "modified"}}: the Artbook page it opens as
- defaults_seen:    the newest set of default file types / categories added to this config's own
                    lists (apply_new_defaults)
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .scanner import (
    APP_FILE_EXTENSIONS,
    ARCHIVE_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    MODEL_EXTENSIONS,
    TEXTURE_EXTENSIONS,
    VIDEO_EXTENSIONS,
)


CONFIG_FILE_PATH: Path = Path.home() / ".3d_asset_manager_config.json"
# Category icon images are copied here so they survive the original file moving
CATEGORY_ICONS_DIR: Path = Path.home() / ".3d_asset_manager" / "category_icons"
MAX_CATEGORY_ICON_BYTES = 1024 * 1024

THEME_IDS = ("dark", "light", "dark_purple", "neon_cyan")
DEFAULT_THEME = "dark"
THUMBNAIL_SIZE_RANGE = (64, 256)
DEFAULT_THUMBNAIL_SIZE = 110

# Extensions pre-filled in the "Files" settings page (ordered for display)
DEFAULT_FILE_EXTENSIONS: list[str] = [
    ".fbx", ".obj", ".gltf", ".glb", ".stl", ".dae", ".blend", ".3ds", ".ply",
    ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".tga", ".bmp", ".webp", ".exr",
    ".psd", ".psb", ".hdr", ".dds",
    ".pdf",
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
    ".zip", ".rar", ".7z",
    ".zbp", ".ztl", ".zpr", ".zmt",
    ".spp", ".sbsar", ".spsm", ".ma", ".mb", ".max", ".tbscene", ".pur",
]

# Mirrors the original hard-coded sidebar layout
DEFAULT_CATEGORIES: list[dict[str, Any]] = [
    {
        "name": "3D Models",
        "color": "#3b82f6",
        "extensions": [],
        "subcategories": [
            {"name": "FBX", "color": "#3b82f6", "extensions": [".fbx"]},
            {"name": "OBJ", "color": "#f59e0b", "extensions": [".obj"]},
            {"name": "glTF", "color": "#10b981", "extensions": [".gltf", ".glb"]},
            {"name": "Other 3D", "color": "#a855f7", "extensions": [".stl", ".dae", ".blend", ".3ds", ".ply"]},
        ],
    },
    {
        "name": "Textures & Images",
        "color": "#10b981",
        "extensions": [],
        "subcategories": [
            {"name": "PNG", "color": "#34d399", "extensions": [".png"]},
            {"name": "JPEG", "color": "#fbbf24", "extensions": [".jpg", ".jpeg"]},
            {"name": "TIFF", "color": "#60a5fa", "extensions": [".tiff", ".tif"]},
            {"name": "Other Textures", "color": "#c084fc", "extensions": [".tga", ".bmp", ".webp", ".exr", ".psd", ".psb", ".hdr", ".dds"]},
        ],
    },
    {
        "name": "Documents",
        "color": "#ef4444",
        "extensions": [],
        "subcategories": [
            {"name": "PDF", "color": "#ef4444", "extensions": [".pdf"]},
        ],
    },
    {
        "name": "Videos",
        "color": "#f59e0b",
        "extensions": [".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"],
        "subcategories": [],
    },
    {
        "name": "Archives",
        "color": "#a78bfa",
        "extensions": [".zip", ".rar", ".7z"],
        "subcategories": [],
    },
    {
        "name": "ZBrush",
        "color": "#f97316",
        "extensions": [".zmt"],
        "subcategories": [
            {"name": "Brushes", "color": "#fb923c", "extensions": [".zbp"]},
            {"name": "Tools", "color": "#fdba74", "extensions": [".ztl"]},
            {"name": "Projects", "color": "#ea580c", "extensions": [".zpr"]},
        ],
    },
    {
        "name": "App Files",
        "color": "#14b8a6",
        "extensions": [],
        "subcategories": [
            {"name": "Substance", "color": "#84cc16", "extensions": [".spp", ".sbsar", ".spsm"]},
            {"name": "Maya", "color": "#2dd4bf", "extensions": [".ma", ".mb"]},
            {"name": "3ds Max", "color": "#38bdf8", "extensions": [".max"]},
            {"name": "Toolbag", "color": "#fb7185", "extensions": [".tbscene"]},
            {"name": "PureRef", "color": "#94a3b8", "extensions": [".pur"]},
        ],
    },
]

# File types and categories added to the defaults after the first release. A config that stores its
# own lists (any edit in Settings saves them) never sees changed defaults, so apply_new_defaults()
# adds each set once; what the user removes afterwards stays removed. Append only.
NEW_DEFAULTS: tuple[dict[str, Any], ...] = (
    {"version": 2, "extensions": [".zip"], "categories": ["Archives"]},
    # (category_extensions: added to a category of that name the config has, if no category has them)
    {"version": 3, "extensions": [".rar", ".7z"], "categories": [], "category_extensions": {"Archives": [".rar", ".7z"]}},
    # (beside: added to whichever category or subcategory the config keeps that file type in)
    {
        "version": 4,
        "extensions": [
            ".psb", ".zbp", ".ztl", ".zpr", ".zmt", ".spp", ".sbsar", ".spsm", ".ma", ".mb", ".max", ".tbscene", ".pur",
        ],
        "categories": ["ZBrush", "App Files"],
        "beside": {".psd": [".psb"]},
    },
)
DEFAULTS_VERSION = NEW_DEFAULTS[-1]["version"]

# Extensions the built-in viewer / thumbnailer knows how to preview
PREVIEWABLE_EXTENSIONS: set[str] = (
    MODEL_EXTENSIONS | TEXTURE_EXTENSIONS | DOCUMENT_EXTENSIONS | VIDEO_EXTENSIONS | ARCHIVE_EXTENSIONS
    | APP_FILE_EXTENSIONS
)

_EXTENSION_PATTERN = re.compile(r"^\.[a-z0-9_]+$")
_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


# ----------------------------------------------------------------------
# Raw config I/O
# ----------------------------------------------------------------------
def _backup_path() -> Path:
    """The last good copy of the config (the one before the latest save)."""
    return CONFIG_FILE_PATH.with_name(CONFIG_FILE_PATH.name + ".bak")


_reported_damaged: set[str] = set()  # (each damaged file is reported once per session)


def _read_config_file(path: Path) -> dict[str, Any] | None:
    """The file's settings, or None if it's missing or damaged (a damaged one is logged)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        if str(path) not in _reported_damaged:
            _reported_damaged.add(str(path))
            from . import crash_log

            crash_log.log_error(f"Settings file couldn't be read ({path}): {exc}")
        return None
    return data if isinstance(data, dict) else None


def _load_raw_config() -> dict[str, Any]:
    """Load configuration dictionary from disk (the last good copy if the file is damaged)."""
    for path in (CONFIG_FILE_PATH, _backup_path()):
        data = _read_config_file(path)
        if data is not None:
            return data
    return {}


def _save_raw_config(data: dict[str, Any]) -> None:
    """Save configuration dictionary to disk. Written to a separate file and swapped in, so a
    crash or power cut mid-save can't leave it half written (which used to lose every setting:
    a damaged file read as empty, and the next save made that permanent). The copy it replaces
    is kept as the backup."""
    partial = CONFIG_FILE_PATH.with_name(CONFIG_FILE_PATH.name + ".partial")
    try:
        with open(partial, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if _read_config_file(CONFIG_FILE_PATH) is not None:
            try:
                os.replace(CONFIG_FILE_PATH, _backup_path())  # (only a good copy becomes the backup)
            except OSError:
                pass  # the backup is only a fallback
        os.replace(partial, CONFIG_FILE_PATH)
    except OSError as exc:
        print(f"Warning: Failed to persist config to {CONFIG_FILE_PATH}: {exc}")


def _update_config(key: str, value: Any) -> None:
    config = _load_raw_config()
    config[key] = value
    _save_raw_config(config)


def apply_new_defaults() -> bool:
    """Add the file types and categories introduced since this config last looked (NEW_DEFAULTS)
    to its own lists, once. Lists it doesn't store follow the defaults anyway. A category whose
    name is taken already is left alone. Call at startup; True if anything was saved."""
    config = _load_raw_config()
    if not config and (CONFIG_FILE_PATH.exists() or _backup_path().exists()):
        return False  # (unreadable: saving now would replace whatever is left of it)
    seen = config.get("defaults_seen", 1 if config else DEFAULTS_VERSION)
    if not config:
        config["defaults_seen"] = seen  # a new install: it has the newest defaults
        _save_raw_config(config)
        return True
    if not isinstance(seen, int) or seen >= DEFAULTS_VERSION:
        return False
    stored_exts = config.get("file_extensions")
    categories = _sanitize_categories(config.get("categories"))
    for change in NEW_DEFAULTS:
        if change["version"] <= seen:
            continue
        if isinstance(stored_exts, list):
            have = {str(e).lower() for e in stored_exts}
            stored_exts += [e for e in change["extensions"] if e not in have]
        if categories is not None:
            names = {c["name"].casefold() for c in categories}
            for name in change["categories"]:
                default = next(c for c in DEFAULT_CATEGORIES if c["name"] == name)
                if name.casefold() not in names:
                    categories.append(_without_taken_types(copy.deepcopy(default), categories))
                    names.add(name.casefold())
            for beside, exts in change.get("beside", {}).items():
                taken = set().union(*(category_extensions(c) for c in categories)) if categories else set()
                entries = [entry for c in categories for entry in (c, *c.get("subcategories", []))]
                target = next((entry for entry in entries if beside in entry.get("extensions", [])), None)
                if target is not None:
                    target["extensions"] += [e for e in exts if e not in taken]
            for name, exts in change.get("category_extensions", {}).items():
                taken = set().union(*(category_extensions(c) for c in categories)) if categories else set()
                target = next((c for c in categories if c["name"].casefold() == name.casefold()), None)
                if target is not None:
                    target["extensions"] += [e for e in exts if e not in taken]
    if isinstance(stored_exts, list):
        config["file_extensions"] = stored_exts
    if categories is not None:
        config["categories"] = categories
    config["defaults_seen"] = DEFAULTS_VERSION
    _save_raw_config(config)
    return True


def _without_taken_types(category: dict[str, Any], categories: list[dict[str, Any]]) -> dict[str, Any]:
    """A new category, minus the file types another category has already (the user's own stay there)."""
    taken = set().union(*(category_extensions(c) for c in categories)) if categories else set()
    for entry in (category, *category.get("subcategories", [])):
        entry["extensions"] = [e for e in entry.get("extensions", []) if e not in taken]
    return category


# ----------------------------------------------------------------------
# Library folders
# ----------------------------------------------------------------------
def get_scan_folders() -> list[Path]:
    """Get the library folders to scan (including ones that no longer exist)."""
    config = _load_raw_config()
    folders = config.get("scan_folders")
    if folders is None:
        # Migrate the legacy single default folder into the library list
        legacy = config.get("default_folder")
        folders = [legacy] if legacy else []
    if not isinstance(folders, list):
        return []
    return [Path(f) for f in folders if isinstance(f, str) and f]


def set_scan_folders(folders: list[Path | str]) -> None:
    """Save the library folders, dropping duplicates while preserving order."""
    unique: list[str] = []
    seen: set[str] = set()
    for folder in folders:
        normalized = str(Path(folder).resolve())
        if normalized.lower() not in seen:
            seen.add(normalized.lower())
            unique.append(normalized)
    config = _load_raw_config()
    config["scan_folders"] = unique
    config.pop("default_folder", None)
    _save_raw_config(config)


def is_scan_folder(path: Path | str | None) -> bool:
    """Check if the given folder is one of the library folders."""
    if path is None:
        return False
    try:
        target = Path(path).resolve()
        return any(target == folder.resolve() for folder in get_scan_folders())
    except OSError:
        return False


# ----------------------------------------------------------------------
# File extensions
# ----------------------------------------------------------------------
def parse_extension_list(text: str) -> tuple[list[str], list[str]]:
    """Parse a comma-separated extension list such as ".pdf, .png, .obj, .fbx".

    Returns (extensions, errors). Extensions are lower-cased and de-duplicated;
    errors describe entries that don't match the expected format.
    """
    extensions: list[str] = []
    errors: list[str] = []

    entries = [e.strip() for e in text.replace("\n", ",").split(",")]
    # A single trailing comma is harmless, so ignore empty entries at the end
    while entries and not entries[-1]:
        entries.pop()

    for entry in entries:
        if not entry:
            errors.append("Empty entry between commas (,,)")
            continue
        ext = entry.lower()
        if not ext.startswith("."):
            errors.append(f'"{entry}" must start with a dot, e.g. ".{ext}"')
        elif " " in ext:
            errors.append(f'"{entry}" contains a space; separate extensions with commas')
        elif ext.count(".") > 1:
            errors.append(f'"{entry}" has more than one dot; use a single extension like ".{ext.rsplit(".", 1)[-1]}"')
        elif not _EXTENSION_PATTERN.match(ext):
            errors.append(f'"{entry}" may only contain letters, numbers, or underscores after the dot')
        elif ext not in extensions:
            extensions.append(ext)

    if not entries:
        errors.append("At least one file type is required")

    return extensions, errors


def get_file_extensions() -> list[str]:
    """Get the file extensions the scanner picks up."""
    stored = _load_raw_config().get("file_extensions")
    if isinstance(stored, list) and stored:
        exts, errors = parse_extension_list(", ".join(str(e) for e in stored))
        if exts and not errors:
            return exts
    return list(DEFAULT_FILE_EXTENSIONS)


def set_file_extensions(extensions: list[str]) -> None:
    _update_config("file_extensions", list(extensions))


# ----------------------------------------------------------------------
# Categories
# ----------------------------------------------------------------------
def _sanitize_category_entry(raw: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": raw["name"],
        "extensions": [str(e).lower() for e in raw.get("extensions", [])],
    }
    color = raw.get("color")
    if isinstance(color, str) and _COLOR_PATTERN.match(color):
        entry["color"] = color.lower()
    icon = raw.get("icon")
    if isinstance(icon, str) and icon:
        entry["icon"] = icon
    # Tags (core.category_rules): files with any (or, with tag_match "all", every one) of them show too
    tags = raw.get("tags")
    if isinstance(tags, list):
        names = list(dict.fromkeys(" ".join(t.split()) for t in tags if isinstance(t, str) and t.strip()))
        if names:
            entry["tags"] = names
    if raw.get("tag_match") == "all":
        entry["tag_match"] = "all"
    return entry


def _sanitize_categories(raw: Any) -> list[dict[str, Any]] | None:
    """Validate category data loaded from disk; returns None if malformed."""
    if not isinstance(raw, list):
        return None
    result: list[dict[str, Any]] = []
    for main in raw:
        if not isinstance(main, dict) or not isinstance(main.get("name"), str):
            return None
        subs = []
        for sub in main.get("subcategories", []):
            if not isinstance(sub, dict) or not isinstance(sub.get("name"), str):
                return None
            subs.append(_sanitize_category_entry(sub))
        entry = _sanitize_category_entry(main)
        entry["subcategories"] = subs
        result.append(entry)
    return result


def get_categories() -> list[dict[str, Any]]:
    """Get sidebar categories: [{name, extensions, subcategories: [{name, extensions}]}]."""
    categories = _sanitize_categories(_load_raw_config().get("categories"))
    if categories is None:
        return copy.deepcopy(DEFAULT_CATEGORIES)
    return categories


def set_categories(categories: list[dict[str, Any]]) -> None:
    _update_config("categories", categories)


def get_seven_zip_path() -> str:
    """7-Zip's 7z.exe (or its folder) for RAR / 7z archives; "" = find it (core.archives.find_seven_zip)."""
    value = _load_raw_config().get("seven_zip_path", "")
    return value if isinstance(value, str) else ""


def set_seven_zip_path(path: str) -> None:
    _update_config("seven_zip_path", path.strip())


def get_archive_contents() -> bool:
    """Show the files inside archives among the library's files (else only inside an opened one)."""
    return _load_raw_config().get("archive_contents") is not False


def set_archive_contents(show: bool) -> None:
    _update_config("archive_contents", bool(show))


def get_found_apps() -> dict[str, list[str]]:
    """The apps found on this computer when last looked (core.apps.detect): {app id: programs, newest first}."""
    value = _load_raw_config().get("found_apps")
    if not isinstance(value, dict):
        return {}
    return {k: [p for p in v if isinstance(p, str)] for k, v in value.items() if isinstance(k, str) and isinstance(v, list)}


def set_found_apps(found: dict[str, list[str]]) -> None:
    _update_config("found_apps", found)


def get_app_choices() -> dict[str, str]:
    """The program picked in Settings > Apps for each app whose newest found one isn't wanted."""
    value = _load_raw_config().get("app_choices")
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def set_app_choices(choices: dict[str, str]) -> None:
    _update_config("app_choices", choices)


def get_custom_apps() -> list[dict[str, Any]]:
    """The apps added by hand in Settings > Apps: {"id", "name", "program", "types"}."""
    value = _load_raw_config().get("custom_apps")
    if not isinstance(value, list):
        return []
    return [
        {"id": c["id"], "name": c["name"], "program": c["program"], "types": [t for t in c["types"] if isinstance(t, str)]}
        for c in value
        if isinstance(c, dict) and all(isinstance(c.get(k), str) for k in ("id", "name", "program")) and isinstance(c.get("types"), list)
    ]


def set_custom_apps(custom: list[dict[str, Any]]) -> None:
    _update_config("custom_apps", custom)


def get_app_types() -> dict[str, list[str]]:
    """The file types Open In offers an app for, where they've been set by hand."""
    value = _load_raw_config().get("app_types")
    if not isinstance(value, dict):
        return {}
    return {k: [t for t in v if isinstance(t, str)] for k, v in value.items() if isinstance(k, str) and isinstance(v, list)}


def set_app_types(types: dict[str, list[str]]) -> None:
    _update_config("app_types", types)


def get_board_link(board: str) -> dict[str, Any] | None:
    """The Artbook page a PureRef board opens as: {"page", "modified"} (None: it hasn't opened as one)."""
    links = _load_raw_config().get("board_pages")
    link = links.get(board) if isinstance(links, dict) else None
    return link if isinstance(link, dict) and isinstance(link.get("page"), str) else None


def set_board_link(board: str, page_id: str, modified: float) -> None:
    config = _load_raw_config()
    links = config.get("board_pages")
    links = links if isinstance(links, dict) else {}
    links[board] = {"page": page_id, "modified": modified}
    config["board_pages"] = links
    _save_raw_config(config)


def category_extensions(category: dict[str, Any]) -> set[str]:
    """All extensions shown by a category, including those of its subcategories."""
    exts = set(category.get("extensions", []))
    for sub in category.get("subcategories", []):
        exts.update(sub.get("extensions", []))
    return exts


def import_category_icon(source: Path | str) -> Path:
    """Copy an icon image into the app's icon folder and return the copy's path."""
    source = Path(source)
    if source.stat().st_size > MAX_CATEGORY_ICON_BYTES:
        raise ValueError(f"Icon files must be {MAX_CATEGORY_ICON_BYTES // 1024} KB or smaller")
    CATEGORY_ICONS_DIR.mkdir(parents=True, exist_ok=True)
    target = CATEGORY_ICONS_DIR / f"{uuid.uuid4().hex}{source.suffix.lower()}"
    shutil.copyfile(source, target)
    return target


def remove_unused_category_icons(categories: list[dict[str, Any]]) -> None:
    """Delete copied icon files that no category references any more (categories: the ones being
    saved; every profile's saved ones are kept too)."""
    if not CATEGORY_ICONS_DIR.is_dir():
        return
    used = set()
    from .profiles import every_profiles_categories  # (it imports this module)

    for main in [*categories, *(c for listed in every_profiles_categories() for c in listed)]:
        for entry in [main, *main.get("subcategories", [])]:
            if entry.get("icon"):
                used.add(Path(entry["icon"]).resolve())
    for file in CATEGORY_ICONS_DIR.iterdir():
        if file.is_file() and file.resolve() not in used:
            try:
                file.unlink()
            except OSError:
                pass


# ----------------------------------------------------------------------
# Appearance
# ----------------------------------------------------------------------
def get_theme() -> str:
    theme = _load_raw_config().get("theme")
    return theme if theme in THEME_IDS else DEFAULT_THEME


def set_theme(theme_id: str) -> None:
    _update_config("theme", theme_id if theme_id in THEME_IDS else DEFAULT_THEME)


MOTION_MODES = ("auto", "on", "off")  # auto: follow Windows' "Animation effects" setting


def get_motion() -> str:
    mode = _load_raw_config().get("motion")
    return mode if mode in MOTION_MODES else "auto"


def set_motion(mode: str) -> None:
    _update_config("motion", mode if mode in MOTION_MODES else "auto")


# ui.backgrounds.TEXTURES has their names and drawings
TEXTURE_IDS = ("none", "grain", "dots", "grid", "diagonal", "hexagon", "carbon", "topo")


def get_background_texture() -> str:
    texture = _load_raw_config().get("background_texture")
    return texture if texture in TEXTURE_IDS else "none"


def set_background_texture(texture_id: str) -> None:
    _update_config("background_texture", texture_id if texture_id in TEXTURE_IDS else "none")


DEFAULT_GLASS_FROST = 70


def get_glass_frost() -> int:
    frost = _load_raw_config().get("glass_frost")
    return max(0, min(100, frost)) if isinstance(frost, int) and not isinstance(frost, bool) else DEFAULT_GLASS_FROST


def set_glass_frost(percent: int) -> None:
    _update_config("glass_frost", max(0, min(100, int(percent))))


def get_artbook_page() -> str:
    page = _load_raw_config().get("artbook_page")
    return page if isinstance(page, str) else ""


def set_artbook_page(page_id: str) -> None:
    _update_config("artbook_page", page_id)


def get_pages_dir() -> str:
    """The folder the Artbook's pages are kept in, or "" for the default one (core.artbook_store)."""
    folder = _load_raw_config().get("artbook_pages_dir")
    return folder if isinstance(folder, str) else ""


def set_pages_dir(folder: str) -> None:
    _update_config("artbook_pages_dir", folder)


def get_last_import_dir() -> str:
    """Where boards were last imported from (for the file picker), or ""."""
    folder = _load_raw_config().get("artbook_import_dir")
    return folder if isinstance(folder, str) else ""


def set_last_import_dir(folder: str) -> None:
    _update_config("artbook_import_dir", folder)


def get_last_export_dir() -> str:
    """Where pages and pictures were last exported to, or ""."""
    folder = _load_raw_config().get("artbook_export_dir")
    return folder if isinstance(folder, str) else ""


def set_last_export_dir(folder: str) -> None:
    _update_config("artbook_export_dir", folder)


VIEW_MODES = ("asset", "artbook", "split", "separate")


def get_view_state() -> dict[str, Any]:
    """{"mode": one of VIEW_MODES, "split": asset share of a split (0.2-0.8), "window": geometry (base64)}."""
    raw = _load_raw_config().get("view")
    raw = raw if isinstance(raw, dict) else {}
    split = raw.get("split")
    return {
        "mode": raw.get("mode") if raw.get("mode") in VIEW_MODES else "asset",
        "split": min(0.8, max(0.2, float(split))) if isinstance(split, (int, float)) and not isinstance(split, bool) else 0.5,
        "window": raw.get("window") if isinstance(raw.get("window"), str) else "",
    }


def set_view_state(**changes: Any) -> None:
    """Update some of mode / split / window."""
    state = get_view_state()
    state.update({k: v for k, v in changes.items() if k in state})
    if state["mode"] not in VIEW_MODES:
        state["mode"] = "asset"
    state["split"] = round(min(0.8, max(0.2, float(state["split"]))), 3)
    _update_config("view", state)


ARTBOOK_OPACITY_RANGE = (20, 100)  # percent: below 20 the window is too faint to find again


def get_artbook_window_state() -> dict[str, Any]:
    """{"on_top": bool, "opacity": int percent, "click_through_tip": bool} of the separate Artbook window."""
    raw = _load_raw_config().get("artbook_window")
    raw = raw if isinstance(raw, dict) else {}
    opacity = raw.get("opacity")
    lo, hi = ARTBOOK_OPACITY_RANGE
    return {
        "on_top": raw.get("on_top") if isinstance(raw.get("on_top"), bool) else False,
        "opacity": max(lo, min(hi, opacity)) if isinstance(opacity, int) and not isinstance(opacity, bool) else hi,
        "click_through_tip": raw.get("click_through_tip") if isinstance(raw.get("click_through_tip"), bool) else True,
    }


def set_artbook_window_state(**changes: Any) -> None:
    """Update on_top, opacity and / or click_through_tip."""
    state = get_artbook_window_state()
    state.update({k: v for k, v in changes.items() if k in state})
    lo, hi = ARTBOOK_OPACITY_RANGE
    state["on_top"], state["click_through_tip"] = bool(state["on_top"]), bool(state["click_through_tip"])
    state["opacity"] = max(lo, min(hi, int(state["opacity"])))
    _update_config("artbook_window", state)


SIDEBAR_WIDTH_RANGE = (180, 480)
DEFAULT_SIDEBAR_WIDTH = 240


def get_sidebar_state(key: str = "sidebar") -> dict[str, Any]:
    """{"open": bool, "pinned": bool, "width": int}: pinned docks it beside the grid, else it floats over it.

    key: "sidebar" (the library's) or "artbook_sidebar" (the Pages list)."""
    raw = _load_raw_config().get(key)
    raw = raw if isinstance(raw, dict) else {}
    width = raw.get("width")
    lo, hi = SIDEBAR_WIDTH_RANGE
    return {
        "open": raw.get("open") if isinstance(raw.get("open"), bool) else True,
        "pinned": raw.get("pinned") if isinstance(raw.get("pinned"), bool) else True,
        "width": max(lo, min(hi, width)) if isinstance(width, int) else DEFAULT_SIDEBAR_WIDTH,
    }


ALPHA_PLANE_SIDES = (512, 1024)  # the 3D preview of an alpha: points along its longest side
TILE_GRIDS = (2, 3)  # the image viewer's Tile: 2 x 2 or 3 x 3
HDRI_EXPOSURE_RANGE = (-3.0, 3.0)  # stops, either side of the HDRI's automatic exposure


def get_model_view() -> dict[str, Any]:
    """The 3D viewer's look, kept between viewings: {"color": "#rrggbb", "material", "lighting"}
    (core.model_look's names), whether models get rendered thumbnails ("thumbnails"), and for images:
    alphas shown as a lit relief in the grid ("alpha_relief"), the detail of an alpha's 3D preview
    ("alpha_plane": ALPHA_PLANE_SIDES) and the Tile grid ("tile": TILE_GRIDS).
    An HDRI as the lighting: its path ("hdri": "" for none, "lighting" then says which preset), its exposure in
    stops ("hdri_exposure": within HDRI_EXPOSURE_RANGE) and whether it's shown behind the model ("hdri_backdrop").
    Metal's roughness ("metal_roughness": 0-1)."""
    from core.model_look import DEFAULT_COLOR, DEFAULT_LIGHTING, DEFAULT_MATERIAL, DEFAULT_ROUGHNESS, LIGHTINGS, MATERIALS

    raw = _load_raw_config().get("model_view")
    raw = raw if isinstance(raw, dict) else {}
    color, hdri, exposure, roughness = raw.get("color"), raw.get("hdri"), raw.get("hdri_exposure"), raw.get("metal_roughness")
    low, high = HDRI_EXPOSURE_RANGE
    return {
        "color": color if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color) else DEFAULT_COLOR,
        "material": raw.get("material") if raw.get("material") in MATERIALS else DEFAULT_MATERIAL,
        "lighting": raw.get("lighting") if raw.get("lighting") in LIGHTINGS else DEFAULT_LIGHTING,
        "thumbnails": raw.get("thumbnails") if isinstance(raw.get("thumbnails"), bool) else True,
        "alpha_relief": raw.get("alpha_relief") if isinstance(raw.get("alpha_relief"), bool) else True,
        "alpha_plane": raw.get("alpha_plane") if raw.get("alpha_plane") in ALPHA_PLANE_SIDES else ALPHA_PLANE_SIDES[0],
        "tile": raw.get("tile") if raw.get("tile") in TILE_GRIDS else TILE_GRIDS[0],
        "hdri": hdri if isinstance(hdri, str) else "",
        "hdri_exposure": float(exposure) if type(exposure) in (int, float) and low <= exposure <= high else 0.0,
        "hdri_backdrop": raw.get("hdri_backdrop") if isinstance(raw.get("hdri_backdrop"), bool) else False,
        "metal_roughness": float(roughness) if type(roughness) in (int, float) and 0 <= roughness <= 1 else DEFAULT_ROUGHNESS,
    }


def set_model_view(**changes: Any) -> None:
    """Update some of get_model_view's settings (anything invalid reads back as the default)."""
    state = get_model_view()
    state.update({k: v for k, v in changes.items() if k in state})
    _update_config("model_view", state)


def get_render_settings() -> dict[str, Any]:
    """The Render window's choices: which renderer ("engine": "luxcore" or "viewer"), "resolution" and "quality"
    (core.render_scene's names), "denoise", and the "folder" last saved to."""
    from core.render_scene import DEFAULT_QUALITY, DEFAULT_RESOLUTION, QUALITIES, RESOLUTIONS

    raw = _load_raw_config().get("render")
    raw = raw if isinstance(raw, dict) else {}
    folder = raw.get("folder")
    return {
        "engine": raw.get("engine") if raw.get("engine") in ("luxcore", "viewer") else "luxcore",
        "resolution": raw.get("resolution") if raw.get("resolution") in RESOLUTIONS else DEFAULT_RESOLUTION,
        "quality": raw.get("quality") if raw.get("quality") in QUALITIES else DEFAULT_QUALITY,
        "denoise": raw.get("denoise") if isinstance(raw.get("denoise"), bool) else True,
        "folder": folder if isinstance(folder, str) else "",
    }


def set_render_settings(**changes: Any) -> None:
    state = get_render_settings()
    state.update({k: v for k, v in changes.items() if k in state})
    _update_config("render", state)


def get_artbook_value_tones() -> int:
    """How many flat greys Values Board shows (2 to 5; 3 to start with)."""
    value = _load_raw_config().get("artbook_value_tones")
    return value if value in (2, 3, 4, 5) else 3


def set_artbook_value_tones(tones: int) -> None:
    _update_config("artbook_value_tones", int(tones))


def get_artbook_custom_grid() -> tuple[int, int]:
    """The last custom grid asked for (columns, rows): 6 x 6 to start with."""
    value = _load_raw_config().get("artbook_custom_grid")
    if isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) and 1 <= v <= 32 for v in value):
        return value[0], value[1]
    return 6, 6


def set_artbook_custom_grid(grid: tuple[int, int]) -> None:
    _update_config("artbook_custom_grid", [int(grid[0]), int(grid[1])])


def get_unpacked_limit_gb() -> int:
    """How many GB the copies unpacked from archives may take (Settings > Storage)."""
    from core.storage import DEFAULT_UNPACKED_LIMIT_GB, UNPACKED_LIMITS_GB

    value = _load_raw_config().get("unpacked_limit_gb")
    return value if value in UNPACKED_LIMITS_GB else DEFAULT_UNPACKED_LIMIT_GB


def set_unpacked_limit_gb(gb: int) -> None:
    _update_config("unpacked_limit_gb", int(gb))


def get_upscale_settings() -> dict[str, Any]:
    """The Artbook's Upscale choices: "model" (core.upscaler.MODELS) and "factor" (2 or 4)."""
    from core.upscaler import DEFAULT_FACTOR, DEFAULT_MODEL, FACTORS, MODELS

    raw = _load_raw_config().get("upscale")
    raw = raw if isinstance(raw, dict) else {}
    return {
        "model": raw.get("model") if raw.get("model") in MODELS else DEFAULT_MODEL,
        "factor": raw.get("factor") if raw.get("factor") in FACTORS else DEFAULT_FACTOR,
    }


def set_upscale_settings(**changes: Any) -> None:
    state = get_upscale_settings()
    state.update({k: v for k, v in changes.items() if k in state})
    _update_config("upscale", state)


def set_sidebar_state(key: str = "sidebar", **changes: Any) -> None:
    """Update some of open / pinned / width."""
    state = get_sidebar_state(key)
    state.update({k: v for k, v in changes.items() if k in state})
    lo, hi = SIDEBAR_WIDTH_RANGE
    state["width"] = max(lo, min(hi, int(state["width"])))
    state["open"], state["pinned"] = bool(state["open"]), bool(state["pinned"])
    _update_config(key, state)


def get_thumbnail_size() -> int:
    size = _load_raw_config().get("thumbnail_size", DEFAULT_THUMBNAIL_SIZE)
    if not isinstance(size, int):
        return DEFAULT_THUMBNAIL_SIZE
    lo, hi = THUMBNAIL_SIZE_RANGE
    return max(lo, min(hi, size))


def set_thumbnail_size(size: int) -> None:
    lo, hi = THUMBNAIL_SIZE_RANGE
    _update_config("thumbnail_size", max(lo, min(hi, int(size))))


# ----------------------------------------------------------------------
# Video
# ----------------------------------------------------------------------
MEDIA_BACKENDS = ("auto", "ffmpeg", "windows")


def get_ffmpeg_path() -> str:
    """User-configured ffmpeg executable, or "" to search the system PATH."""
    path = _load_raw_config().get("ffmpeg_path", "")
    return path if isinstance(path, str) else ""


def set_ffmpeg_path(path: str) -> None:
    _update_config("ffmpeg_path", path.strip())


def get_media_backend() -> str:
    backend = _load_raw_config().get("media_backend")
    return backend if backend in MEDIA_BACKENDS else "auto"


def set_media_backend(backend: str) -> None:
    _update_config("media_backend", backend if backend in MEDIA_BACKENDS else "auto")


VIDEO_SKIP_OPTIONS = (1, 5, 10, 15, 30)  # seconds
DEFAULT_VIDEO_SKIP = 5


def get_video_skip_seconds() -> int:
    """Jump time for the player's skip buttons and arrow keys."""
    seconds = _load_raw_config().get("video_skip_seconds")
    return seconds if seconds in VIDEO_SKIP_OPTIONS else DEFAULT_VIDEO_SKIP


def set_video_skip_seconds(seconds: int) -> None:
    _update_config("video_skip_seconds", seconds if seconds in VIDEO_SKIP_OPTIONS else DEFAULT_VIDEO_SKIP)


VIDEO_SPEED_RANGE = (0.25, 3.0)


def get_video_speed() -> float:
    speed = _load_raw_config().get("video_speed", 1.0)
    lo, hi = VIDEO_SPEED_RANGE
    return min(hi, max(lo, float(speed))) if isinstance(speed, (int, float)) else 1.0


def set_video_speed(speed: float) -> None:
    lo, hi = VIDEO_SPEED_RANGE
    _update_config("video_speed", round(min(hi, max(lo, float(speed))), 2))


# Where each video was left, so it resumes there: {normalized path: milliseconds}
MAX_REMEMBERED_POSITIONS = 200


def _position_key(path: Path | str) -> str:
    return str(Path(path).resolve()).casefold()


def get_video_position(path: Path | str) -> int:
    positions = _load_raw_config().get("video_positions")
    value = positions.get(_position_key(path)) if isinstance(positions, dict) else None
    return value if isinstance(value, int) and value > 0 else 0


def set_video_position(path: Path | str, position_ms: int) -> None:
    """Remember where a video was left (0 forgets it). Only the most recent ones are kept."""
    config = _load_raw_config()
    positions = config.get("video_positions")
    positions = dict(positions) if isinstance(positions, dict) else {}
    key = _position_key(path)
    positions.pop(key, None)
    if position_ms > 0:
        positions[key] = int(position_ms)  # re-inserted last: newest
    while len(positions) > MAX_REMEMBERED_POSITIONS:
        positions.pop(next(iter(positions)))
    if positions != config.get("video_positions"):
        config["video_positions"] = positions
        _save_raw_config(config)
