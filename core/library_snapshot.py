"""The library as of the last full scan, so the grid can be shown instantly on the next launch.

Saved after every full library scan (on the scan thread) to
%LOCALAPPDATA%\\AssetManager\\library_snapshot.json. It's only used when the library
folders and file types are the same as when it was saved; the app still rescans in
the background right away, so the snapshot is just a head start.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional, Sequence

from .scanner import AssetItem, determine_asset_group, determine_category_name

SNAPSHOT_VERSION = 1


def snapshot_path() -> Path:
    from .tag_repository import DB_PATH  # same folder as the library database (patched in tests)

    return Path(DB_PATH).parent / "library_snapshot.json"


def _signature(folders: Sequence[Path], extensions: Sequence[str]) -> str:
    parts = sorted(os.path.normcase(os.path.abspath(str(f))) for f in folders) + ["|"] + sorted(e.lower() for e in extensions)
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def save_snapshot(folders: Sequence[Path], extensions: Sequence[str], assets: Sequence[AssetItem], ids: dict[str, int]) -> None:
    data = {
        "version": SNAPSHOT_VERSION,
        "signature": _signature(folders, extensions),
        # [path, size, mtime, asset id]: name, type and group are derived again on load
        "assets": [[str(a.path), a.size_bytes, a.modified_timestamp, ids.get(str(a.path))] for a in assets],
    }
    target = snapshot_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial")
        partial.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(partial, target)
    except OSError:
        pass  # only a speed-up


def load_snapshot(folders: Sequence[Path], extensions: Sequence[str]) -> Optional[tuple[list[AssetItem], dict[str, int]]]:
    """(assets, {str(path): id}) from the last scan of exactly these folders and file types, else None."""
    try:
        data = json.loads(snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
        return None
    if data.get("signature") != _signature(folders, extensions):
        return None
    assets, ids = [], {}
    try:
        for path_str, size, mtime, asset_id in data["assets"]:
            path = Path(path_str)
            ext = path.suffix.lower()
            assets.append(AssetItem(path, path.name, ext, size, mtime, determine_asset_group(ext), determine_category_name(ext)))
            if asset_id is not None:
                ids[path_str] = asset_id
    except (KeyError, TypeError, ValueError):
        return None
    return assets, ids
