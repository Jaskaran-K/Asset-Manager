"""What the app keeps on disk (Settings > Storage): each area's size, and what can be done about it.

Caches can be cleared (they're made again when needed), the downloaded tools removed (downloaded again when
asked for), and the folders that hold your work or its backups only opened: nothing here deletes those.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import archives, artbook_store, crash_log, render_engine, tag_repository, upscaler, video_tools

GB = 1024 ** 3
UNPACKED_LIMITS_GB = (1, 2, 5, 10, 20)  # the choices for files unpacked from archives
DEFAULT_UNPACKED_LIMIT_GB = 5


@dataclass(frozen=True)
class Area:
    key: str
    name: str
    about: str
    action: str  # "clear" (made again when needed), "remove" (downloaded again when needed) or "open"


AREAS = (
    Area("unpacked", "Files unpacked from archives",
         "Models, PDFs and videos inside archives, unpacked to be opened. Kept so they open at once next time; "
         "the oldest go first past the limit.", "clear"),
    Area("previews", "Thumbnails and preview copies",
         "The grid's thumbnails, video posters and preview copies, and what's inside archives. Made again when needed.",
         "clear"),
    Area("artbook_small", "Artbook small copies",
         "Small copies of the Artbook's images, so big pages open quickly. Made again when needed.", "clear"),
    Area("luxcore", "LuxCore (the Render button)", "Downloaded when you first rendered. Downloaded again if you render.",
         "remove"),
    Area("upscaler", "Real-ESRGAN (Upscale)", "Downloaded when you first upscaled. Downloaded again if you upscale.",
         "remove"),
    Area("backups", "Library backups",
         "Copies of the tag database: one a day (the last 7) and before each update (the last 10).", "open"),
    Area("pages", "Artbook pages",
         "Your pages, their images, and their earlier versions. Your work: never cleared from here.", "open"),
)


def folder_bytes(folder: Path) -> int:
    """Everything in a folder (0 if it isn't there). Safe off the UI thread."""
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _preview_folders() -> list[Path]:
    return [video_tools.THUMB_DIR, video_tools.POSTER_DIR, video_tools.PROXY_DIR, archives.index_dir()]


def _backup_dir() -> Path:
    return Path(tag_repository.DB_PATH).parent / "backups"


def folder_of(key: str) -> Path:
    """The folder an area is in (to open it)."""
    return {
        "unpacked": archives.extract_dir(),
        "previews": video_tools.THUMB_DIR,
        "artbook_small": video_tools.artbook_cache_dir(),
        "luxcore": render_engine._root(),  # pylint: disable=protected-access
        "upscaler": upscaler._root(),  # pylint: disable=protected-access
        "backups": _backup_dir(),
        "pages": artbook_store.pages_dir(),
    }[key]


def measure() -> dict[str, int]:
    """Each area's size in bytes, plus "versions" (the pages' earlier versions, within "pages") and
    "logs". Reads every folder: run it off the UI thread."""
    sizes = {
        "unpacked": archives.extracted_bytes(),
        "previews": sum(folder_bytes(folder) for folder in _preview_folders()),
        "artbook_small": folder_bytes(video_tools.artbook_cache_dir()),
        "luxcore": folder_bytes(folder_of("luxcore")),
        "upscaler": folder_bytes(folder_of("upscaler")),
        "backups": folder_bytes(_backup_dir()),
        "pages": folder_bytes(artbook_store.pages_dir()),
        "versions": artbook_store.history_bytes(),
    }
    logs = [crash_log.crash_log_path(), *Path(crash_log.LOG_DIR).glob("session-*.log")]
    sizes["logs"] = sum(path.stat().st_size for path in logs if path.is_file())
    return sizes


def _empty(folder: Path) -> None:
    """Delete what's in a folder (files in use are skipped)."""
    if not folder.is_dir():
        return
    for entry in folder.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
        except OSError:
            pass


def free(key: str) -> None:
    """Clear a cache or remove a downloaded tool (an "open" area is left alone)."""
    def previews() -> None:
        for folder in _preview_folders():
            _empty(folder)

    actions: dict[str, Callable[[], None]] = {
        "unpacked": archives.clear_extracted,
        "previews": previews,
        "artbook_small": lambda: _empty(video_tools.artbook_cache_dir()),
        "luxcore": render_engine.remove,
        "upscaler": upscaler.remove,
    }
    action = actions.get(key)
    if action is not None:
        action()


def size_text(size: Optional[int]) -> str:
    if size is None:
        return "..."
    if size >= GB:
        return f"{size / GB:.1f} GB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.0f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} bytes" if size else "Nothing"
