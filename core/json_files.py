"""JSON files that survive damage: Artbook pages and their index, PDF notes.

Saving writes a temporary file and swaps it in (a crash mid-save never leaves half a file), and
first copies the file it replaces to <name>.bak: the copy to fall back on if the file is
damaged later (a disk error, a sync tool, a hand edit). Only a good copy becomes the backup: a
damaged file is moved aside as <name>.damaged instead (kept, in case something can be saved
from it). Reading falls back to the backup, and a damaged file is logged once per session.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

Check = Callable[[Any], bool]  # whether what a file holds is usable (e.g. a dict with "items")


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def damaged_path(path: Path) -> Path:
    return path.with_name(path.name + ".damaged")


def read(path: Path, valid: Check = lambda data: True) -> Optional[Any]:
    """What the file holds, or None if it's missing or damaged."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if valid(data) else None


_reported: set[str] = set()


def read_with_backup(path: Path, valid: Check = lambda data: True) -> tuple[Optional[Any], bool]:
    """(what the file holds, or else its backup; whether it came from the backup). (None, False)
    if neither can be read. A file there but damaged is logged (once per session)."""
    data = read(path, valid)
    if data is not None:
        return data, False
    data = read(backup_path(path), valid)
    if path.exists() and str(path) not in _reported:
        _reported.add(str(path))
        from core import crash_log

        what = "opened its backup instead" if data is not None else "no readable backup either"
        crash_log.log_error(f"File couldn't be read ({path}): {what}")
    return data, data is not None


def write_temp(path: Path, data: Any) -> Path:
    """data written to a temporary file beside path (unique: two saves never share one)."""
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return temp


def replace(temp: Path, path: Path, valid: Check = lambda data: True, trusted: bool = False) -> None:
    """Swap temp in for path, keeping path's current contents as the backup first (see the
    module notes). trusted: path is known to be good (written by this session and untouched
    since), so it isn't read to check."""
    if path.exists():
        try:
            if trusted or read(path, valid) is not None:
                shutil.copyfile(path, backup_path(path))  # (copied, not moved: path is never missing)
            else:
                os.replace(path, damaged_path(path))
        except OSError:
            pass  # the backup is only a fallback
    os.replace(temp, path)


def write(path: Path, data: Any, valid: Check = lambda data: True) -> None:
    """Save data to path (see the module notes)."""
    replace(write_temp(path, data), path, valid)
