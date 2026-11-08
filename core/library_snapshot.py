"""The library as of the last full scan, so the grid can be shown instantly on the next launch.

Saved after every full library scan (on the scan thread) to
%LOCALAPPDATA%\\AssetManager\\library_snapshot.json (library_snapshot_<id>.json for profiles other than
Default: each has its own, core.profiles). It's only used when the library
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

SNAPSHOT_VERSION = 2  # 2: files inside archives


def snapshot_path(profile_id: Optional[str] = None) -> Path:
    """A profile's snapshot (default: the profile in use)."""
    from .tag_repository import DB_PATH  # same folder as the library database (patched in tests)

    if profile_id is None:
        from .profiles import current_profile

        profile_id = current_profile().id
    name = "library_snapshot.json" if profile_id == "default" else f"library_snapshot_{profile_id}.json"
    return Path(DB_PATH).parent / name


def _signature(folders: Sequence[Path], extensions: Sequence[str], archive_contents: bool) -> str:
    parts = sorted(os.path.normcase(os.path.abspath(str(f))) for f in folders) + ["|"] + sorted(e.lower() for e in extensions)
    if archive_contents:
        parts.append("+archive contents")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def _row(asset: AssetItem, asset_id: Optional[int]) -> list:
    """[path, size, mtime, asset id], plus for a file inside an archive what's needed to read it
    (core.archives.ArchiveMember). Name, type and group are derived again on load."""
    row = [str(asset.path), asset.size_bytes, asset.modified_timestamp, asset_id]
    member = asset.archive
    if member is not None:
        row.append([
            str(member.archive), member.name, member.compressed_size, member.method, member.crc, member.offset,
            int(member.encrypted), member.archive_size, member.archive_mtime, member.kind,
        ])
    return row


def save_snapshot(
    folders: Sequence[Path], extensions: Sequence[str], assets: Sequence[AssetItem], ids: dict[str, int],
    archive_contents: bool = False, profile_id: Optional[str] = None,
) -> None:
    """Remember a library scan for the profile (default: the one in use; a scan thread passes the
    one it scanned for, in case the profile was switched meanwhile)."""
    data = {
        "version": SNAPSHOT_VERSION,
        "signature": _signature(folders, extensions, archive_contents),
        "assets": [_row(a, ids.get(str(a.path))) for a in assets],
    }
    target = snapshot_path(profile_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial")
        partial.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(partial, target)
    except OSError:
        pass  # only a speed-up


def load_snapshot(
    folders: Sequence[Path], extensions: Sequence[str], archive_contents: bool = False
) -> Optional[tuple[list[AssetItem], dict[str, int]]]:
    """(assets, {str(path): id}) from the last scan of exactly these folders and file types (and
    with or without the files inside archives), else None."""
    try:
        data = json.loads(snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
        return None
    if data.get("signature") != _signature(folders, extensions, archive_contents):
        return None
    from .archives import ArchiveMember, member_asset

    assets, ids = [], {}
    try:
        for path_str, size, mtime, asset_id, *inside in data["assets"]:
            if inside:
                archive, name, csize, method, crc, offset, encrypted, archive_size, archive_mtime, kind = inside[0]
                asset = member_asset(ArchiveMember(
                    Path(archive), name, size, csize, method, crc, offset, bool(encrypted), mtime, archive_size, archive_mtime,
                    kind,
                ))
                path_str = str(asset.path)
            else:
                path = Path(path_str)
                ext = path.suffix.lower()
                asset = AssetItem(path, path.name, ext, size, mtime, determine_asset_group(ext), determine_category_name(ext))
            assets.append(asset)
            if asset_id is not None:
                ids[path_str] = asset_id
    except (KeyError, TypeError, ValueError):
        return None
    return assets, ids
