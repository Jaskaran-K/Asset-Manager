"""The Artbook's Unsorted tray: things waiting to be put on a page, the same on every page. The browser extension
sends pictures and pages here (core.browser_link writes them, even while the app is closed); the asset grid's Send
to Artbook > Unsorted sends files. The tray (ui.artbook_tray) shows them; placing one takes it out.

Each entry is <stem>.json (what it is: {"kind": "image" | "link" | "file", "time", "name", ...}) and, for a picture
or a link's preview, <stem>.<ext>; a link's site icon is <stem>.icon.<ext>. No Qt here: the browser's helper uses it too.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

KINDS = ("image", "link", "file")
FIELDS = ("name", "source", "url", "title", "about", "site", "path")  # (what an entry may say, as text)
MAX_TEXT = 2000
MAX_ENTRIES = 500


def folder() -> Path:
    from . import browser_link  # (its DATA_ROOT: the app's data folder, beside the browser's inbox)

    return browser_link.DATA_ROOT / "artbook-unsorted"


def _ext(ext: str) -> str:
    return ext if ext.startswith(".") and len(ext) <= 6 and ext[1:].isalnum() else ".img"


def add(kind: str, meta: dict[str, Any], picture: Optional[tuple[bytes, str]] = None,
        icon: Optional[tuple[bytes, str]] = None) -> str:
    """Put something in the tray: its kind, what it says (FIELDS), its picture (data, ".png"...) and, for a link, its
    site's icon (the same); its id."""
    if kind not in KINDS:
        raise ValueError(f"Not something the tray holds: {kind}")
    place = folder()
    place.mkdir(parents=True, exist_ok=True)
    stem = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
    entry: dict[str, Any] = {k: str(meta[k])[:MAX_TEXT] for k in FIELDS if isinstance(meta.get(k), str) and meta[k]}
    entry.update(kind=kind, time=time.time())
    if picture is not None:
        data, ext = picture
        (place / f"{stem}{_ext(ext)}").write_bytes(data)
        entry["picture"] = f"{stem}{_ext(ext)}"
    if icon is not None and kind == "link":
        data, ext = icon
        (place / f"{stem}.icon{_ext(ext)}").write_bytes(data)
        entry["icon"] = f"{stem}.icon{_ext(ext)}"
    temp = place / f"{stem}.json.tmp"
    temp.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(temp, place / f"{stem}.json")  # (whole, or not there: the app may be reading the folder)
    return stem


def entries() -> list[dict[str, Any]]:
    """What's in the tray, oldest first: each its saved form plus "id", "picture_path" and "icon_path" (a Path, or
    None)."""
    place = folder()
    if not place.is_dir():
        return []
    out = []
    for meta in sorted(place.glob("*.json"))[-MAX_ENTRIES:]:
        try:
            entry = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
            continue
        def own(name: Any) -> Optional[Path]:  # (a file of this entry's, in the folder)
            path = place / name if isinstance(name, str) and Path(name).name == name and name.startswith(meta.stem) else None
            return path if path is not None and path.is_file() else None

        entry.update(id=meta.stem, picture_path=own(entry.get("picture")), icon_path=own(entry.get("icon")))
        out.append(entry)
    return out


def get(entry_id: str) -> Optional[dict[str, Any]]:
    return next((entry for entry in entries() if entry["id"] == entry_id), None)


def remove(entry_id: str) -> None:
    """Take an entry out of the tray (its picture too)."""
    if not entry_id or "/" in entry_id or "\\" in entry_id or ".." in entry_id:
        return
    for path in folder().glob(f"{entry_id}.*"):
        try:
            path.unlink()
        except OSError:
            pass


def clear() -> None:
    for entry in entries():
        remove(entry["id"])


def count() -> int:
    place = folder()
    return sum(1 for _ in place.glob("*.json")) if place.is_dir() else 0
