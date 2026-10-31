"""What the PDF viewer remembers about each PDF: where you were (page, zoom) and, later, your notes
and drawings. The PDF itself is never written to.

Each PDF gets one JSON file in NOTES_DIR, named after the file's contents (core.fingerprint
quick_hash), so it follows the PDF through renames and moves. Writes go to a temporary file that
is swapped in, so a crash mid-save never leaves a broken file.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from core.fingerprint import quick_hash

NOTES_DIR: Path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AssetManager" / "pdf-notes"
FORMAT_VERSION = 1


def notes_dir() -> Path:
    return NOTES_DIR  # read at call time, so tests can point it elsewhere


def key_for(path: Path) -> Optional[str]:
    """The name a PDF's notes are kept under (None if the file can't be read)."""
    return quick_hash(path)


def load(key: Optional[str]) -> dict[str, Any]:
    """What's kept for a PDF ({} for one never opened, or a damaged file)."""
    if not key:
        return {}
    try:
        data = json.loads((notes_dir() / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(key: Optional[str], data: dict[str, Any]) -> None:
    if not key:
        return
    folder = notes_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{key}.json"
    temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps({**data, "version": FORMAT_VERSION}, indent=1), encoding="utf-8")
    os.replace(temp, target)
