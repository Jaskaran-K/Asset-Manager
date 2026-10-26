"""File content fingerprints for finding moved files and duplicates.

quick_hash reads at most 128 KB (the first and last 64 KB plus the size), so it's
cheap enough to run on every candidate; full_hash reads the whole file and is
only used to confirm duplicates.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

_EDGE_BYTES = 64 * 1024
_FULL_CHUNK = 1024 * 1024


def quick_hash(path: Path | str) -> Optional[str]:
    """sha1 of the file size plus its first and last 64 KB, or None if it can't be read."""
    try:
        size = os.path.getsize(path)
        digest = hashlib.sha1(str(size).encode())
        with open(path, "rb") as f:
            digest.update(f.read(_EDGE_BYTES))
            if size > 2 * _EDGE_BYTES:
                f.seek(-_EDGE_BYTES, os.SEEK_END)
                digest.update(f.read(_EDGE_BYTES))
            elif size > _EDGE_BYTES:
                digest.update(f.read())
        return digest.hexdigest()
    except OSError:
        return None


def full_hash(path: Path | str) -> Optional[str]:
    """sha256 of the whole file, or None if it can't be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(_FULL_CHUNK):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
