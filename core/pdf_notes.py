"""What the PDF viewer remembers about each PDF: where you were (page, zoom) and, later, your notes
and drawings. The PDF itself is never written to.

Each PDF gets one JSON file in NOTES_DIR, named after the file's contents (core.fingerprint
quick_hash), so it follows the PDF through renames and moves. Writes go to a temporary file that
is swapped in, so a crash mid-save never leaves a broken file, and the version replaced is kept
as <key>.json.bak: a damaged notes file opens from that instead of reading as "no notes" (which
the next mark would then have saved over). See core.json_files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from core import json_files
from core.fingerprint import quick_hash

NOTES_DIR: Path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AssetManager" / "pdf-notes"
FORMAT_VERSION = 1


def notes_dir() -> Path:
    return NOTES_DIR  # read at call time, so tests can point it elsewhere


def key_for(path: Path) -> Optional[str]:
    """The name a PDF's notes are kept under (None if the file can't be read)."""
    return quick_hash(path)


def _is_notes(data: Any) -> bool:
    return isinstance(data, dict)


def load(key: Optional[str]) -> dict[str, Any]:
    """What's kept for a PDF ({} for one never opened; its backup if the file is damaged)."""
    if not key:
        return {}
    data, _from_backup = json_files.read_with_backup(notes_dir() / f"{key}.json", _is_notes)
    return data if data is not None else {}


def save(key: Optional[str], data: dict[str, Any]) -> None:
    if not key:
        return
    folder = notes_dir()
    folder.mkdir(parents=True, exist_ok=True)
    json_files.write(folder / f"{key}.json", {**data, "version": FORMAT_VERSION}, _is_notes)
