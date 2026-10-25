"""Application settings and configuration persistence.

Persists user preferences to ~/.3d_asset_manager_config.json:
- scan_folders:     library folders scanned on startup / refresh
- file_extensions:  file types the scanner picks up
- categories:       sidebar main categories, their subcategories, file types, colors and icons
- theme:            UI theme id ("dark", "light", "dark_purple")
- thumbnail_size:   grid thumbnail size in pixels
- ffmpeg_path:      ffmpeg executable for video proxies / poster frames (blank = search PATH)
- media_backend:    Qt multimedia backend ("auto", "ffmpeg", "windows"); applied on restart
- video_skip_seconds: jump time for the video player's skip buttons and arrow keys
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .scanner import DOCUMENT_EXTENSIONS, MODEL_EXTENSIONS, TEXTURE_EXTENSIONS, VIDEO_EXTENSIONS


CONFIG_FILE_PATH: Path = Path.home() / ".3d_asset_manager_config.json"
# Category icon images are copied here so they survive the original file moving
CATEGORY_ICONS_DIR: Path = Path.home() / ".3d_asset_manager" / "category_icons"
MAX_CATEGORY_ICON_BYTES = 1024 * 1024

THEME_IDS = ("dark", "light", "dark_purple")
DEFAULT_THEME = "dark"
THUMBNAIL_SIZE_RANGE = (64, 256)
DEFAULT_THUMBNAIL_SIZE = 110

# Extensions pre-filled in the "Files" settings page (ordered for display)
DEFAULT_FILE_EXTENSIONS: list[str] = [
    ".fbx", ".obj", ".gltf", ".glb", ".stl", ".dae", ".blend", ".3ds", ".ply",
    ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".tga", ".bmp", ".webp", ".exr",
    ".psd", ".hdr", ".dds",
    ".pdf",
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
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
            {"name": "Other Textures", "color": "#c084fc", "extensions": [".tga", ".bmp", ".webp", ".exr", ".psd", ".hdr", ".dds"]},
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
]

# Extensions the built-in viewer / thumbnailer knows how to preview
PREVIEWABLE_EXTENSIONS: set[str] = MODEL_EXTENSIONS | TEXTURE_EXTENSIONS | DOCUMENT_EXTENSIONS | VIDEO_EXTENSIONS

_EXTENSION_PATTERN = re.compile(r"^\.[a-z0-9_]+$")
_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


# ----------------------------------------------------------------------
# Raw config I/O
# ----------------------------------------------------------------------
def _load_raw_config() -> dict[str, Any]:
    """Load configuration dictionary from disk."""
    if not CONFIG_FILE_PATH.exists():
        return {}
    try:
        with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_raw_config(data: dict[str, Any]) -> None:
    """Save configuration dictionary to disk."""
    try:
        with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        print(f"Warning: Failed to persist config to {CONFIG_FILE_PATH}: {exc}")


def _update_config(key: str, value: Any) -> None:
    config = _load_raw_config()
    config[key] = value
    _save_raw_config(config)


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
    """Delete copied icon files that no category references any more."""
    if not CATEGORY_ICONS_DIR.is_dir():
        return
    used = set()
    for main in categories:
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
