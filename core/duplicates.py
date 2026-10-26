"""Finds files with identical content (runs on a background thread).

Files are compared by size first, then by quick_hash (first / last 64 KB), then by
a full sha256 for files up to FULL_HASH_LIMIT. Bigger files that agree on size and
quick hash are reported as "likely" duplicates rather than read in full.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .fingerprint import full_hash, quick_hash
from .scanner import AssetItem

FULL_HASH_LIMIT = 1024 * 1024 * 1024  # 1 GB


@dataclass
class DuplicateGroup:
    files: list[AssetItem]  # oldest first
    size: int
    confirmed: bool  # full content compared (False: likely, by size and quick hash)

    @property
    def wasted(self) -> int:
        """Space the extra copies take."""
        return self.size * (len(self.files) - 1)


def _group(items: Iterable[AssetItem], key: Callable[[AssetItem], Optional[str]]) -> list[list[AssetItem]]:
    groups: dict[str, list[AssetItem]] = {}
    for item in items:
        value = key(item)
        if value is not None:
            groups.setdefault(value, []).append(item)
    return [g for g in groups.values() if len(g) > 1]


def find_duplicates(assets: Iterable[AssetItem], cancelled: Callable[[], bool] = lambda: False) -> list[DuplicateGroup]:
    """Groups of identical files, biggest waste first. Empty files are ignored."""
    by_size: dict[int, list[AssetItem]] = {}
    seen: set[str] = set()
    for asset in assets:
        key = str(asset.path).lower()
        if asset.size_bytes > 0 and key not in seen:
            seen.add(key)
            by_size.setdefault(asset.size_bytes, []).append(asset)

    result: list[DuplicateGroup] = []
    for size, same_size in by_size.items():
        if len(same_size) < 2:
            continue
        for candidates in _group(same_size, lambda a: quick_hash(a.path)):
            if cancelled():
                return []
            if size > FULL_HASH_LIMIT:
                groups, confirmed = [candidates], False
            else:
                groups, confirmed = _group(candidates, lambda a: full_hash(a.path)), True
            for files in groups:
                files.sort(key=lambda a: (a.modified_timestamp, str(a.path).lower()))
                result.append(DuplicateGroup(files, size, confirmed))
    result.sort(key=lambda g: g.wasted, reverse=True)
    return result
