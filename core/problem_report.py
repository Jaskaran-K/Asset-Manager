"""Report a Problem (Settings > About): a zip of what helps find a bug, for you to send or keep.

In it: about.txt (the app's and Windows' versions, the computer's processor, memory and graphics card, and
how big the library is: counts only, no names), the crash log and the session logs, and only if asked, the
settings file (it has your library folders' paths). Nothing is uploaded.
"""

from __future__ import annotations

import ctypes
import json
import platform
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

from . import artbook_store, crash_log, settings, tag_repository
from .version import APP_NAME, APP_VERSION, GITHUB_URL

_GPU_TIMEOUT = 15


def _memory_gb() -> Optional[float]:
    if sys.platform != "win32":
        return None

    class Status(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
            ("free", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong), ("page_free", ctypes.c_ulonglong),
            ("virtual_total", ctypes.c_ulonglong), ("virtual_free", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong),
        ]

    status = Status()
    status.length = ctypes.sizeof(Status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.total / 1024 ** 3


def graphics_cards() -> list[str]:
    """The graphics cards Windows knows (empty if it couldn't be asked)."""
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + ' (driver ' + $_.DriverVersion + ')' }"],
            capture_output=True, text=True, timeout=_GPU_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def library_counts() -> dict[str, int]:
    """How big the library is: numbers only (read without changing the database)."""
    counts: dict[str, int] = {}
    db = Path(tag_repository.DB_PATH)
    if db.is_file():
        try:
            connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
            try:
                for name, table in (("files", "assets"), ("tags", "tags"), ("tagged", "asset_tags"), ("collections", "saved_searches")):
                    counts[name] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 (fixed names)
                counts["database version"] = connection.execute("PRAGMA user_version").fetchone()[0]
            finally:
                connection.close()
        except sqlite3.Error:
            pass
    counts["library folders"] = len(settings.get_scan_folders())
    try:
        counts["Artbook pages"] = len(artbook_store.ordered_page_ids())
    except Exception:  # pylint: disable=broad-except
        pass
    return counts


def about_text(cards: Optional[list[str]] = None) -> str:
    """The report's about.txt."""
    from PySide6 import QtCore, __version__ as pyside_version

    memory = _memory_gb()
    lines = [
        f"{APP_NAME} {APP_VERSION} ({'installed build' if getattr(sys, 'frozen', False) else 'from source'})",
        f"Made {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"Windows: {platform.platform()}",
        f"Processor: {platform.processor() or 'unknown'}",
        f"Memory: {memory:.1f} GB" if memory else "Memory: unknown",
    ]
    cards = graphics_cards() if cards is None else cards
    lines += [f"Graphics: {card}" for card in cards] or ["Graphics: unknown"]
    lines += [f"Python {platform.python_version()}, PySide6 {pyside_version}, Qt {QtCore.qVersion()}", "", "Library:"]
    lines += [f"  {name}: {count:,}" for name, count in library_counts().items()]
    return "\n".join(lines) + "\n"


def make_report(target: Path, include_settings: bool = False, cards: Optional[list[str]] = None) -> Path:
    """Write the report to target (a .zip). Safe off the UI thread."""
    target = Path(target)
    temp = target.with_name(f"{target.name}.tmp")
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("about.txt", about_text(cards))
            logs = [crash_log.crash_log_path(), *sorted(Path(crash_log.LOG_DIR).glob("session-*.log"))]
            for log in logs:
                if log.is_file():
                    archive.write(log, f"logs/{log.name}")
            if include_settings:
                config = settings._load_raw_config()  # pylint: disable=protected-access
                archive.writestr("settings.json", json.dumps(config, indent=1, ensure_ascii=False))
        temp.replace(target)
    except OSError:
        temp.unlink(missing_ok=True)
        raise
    return target


def contents(target: Path) -> list[str]:
    with zipfile.ZipFile(target) as archive:
        return archive.namelist()


def issue_url() -> str:
    """A new GitHub issue with the version filled in (the zip is attached by hand)."""
    from urllib.parse import urlencode

    body = (
        f"**App version:** {APP_VERSION}\n**Windows:** {platform.platform()}\n\n"
        "**What happened:**\n\n\n**What you were doing:**\n\n\n"
        "(Attach the report zip from Settings > About > Report a Problem, if you made one.)\n"
    )
    return f"{GITHUB_URL}/issues/new?{urlencode({'body': body})}"
