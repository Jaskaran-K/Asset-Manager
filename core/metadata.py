"""Reads file details for searching: image resolution, video length / resolution, model face count.

Everything here blocks and runs on background threads (see MetadataIndexer in main.py).
Images only have their header read; videos are probed with ffmpeg (skipped without it);
3D models are loaded fully, so files over MAX_MODEL_BYTES are skipped.
"""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path
from typing import Optional

from PySide6.QtGui import QImageReader

from .scanner import MODEL_EXTENSIONS, TEXTURE_EXTENSIONS, VIDEO_EXTENSIONS
from .tag_repository import AssetMeta

MAX_MODEL_BYTES = 60 * 1024 * 1024

# Cheapest first: images (header only), then videos (ffmpeg), then models (full load)
_COST = {**{e: 0 for e in TEXTURE_EXTENSIONS}, **{e: 1 for e in VIDEO_EXTENSIONS}, **{e: 2 for e in MODEL_EXTENSIONS}}
EMPTY = AssetMeta()


def read_cost(extension: str) -> int:
    """Sort key: how expensive reading a file's details is (3 = nothing to read)."""
    return _COST.get(extension.lower(), 3)


def _psd_size(path: str) -> Optional[tuple[int, int]]:
    """Width / height from a PSD / PSB header (works for 16 and 32-bit files Qt can't read)."""
    with open(path, "rb") as f:
        header = f.read(26)
    if len(header) < 26 or header[:4] != b"8BPS":
        return None
    height, width = struct.unpack(">II", header[14:22])
    return width, height


def _hdr_size(path: str) -> Optional[tuple[int, int]]:
    """Radiance .hdr: the resolution line ("-Y 512 +X 1024") follows the header."""
    with open(path, "rb") as f:
        head = f.read(4096).decode("latin-1", errors="replace")
    match = re.search(r"\n[-+]Y (\d+) [-+]X (\d+)", head)
    return (int(match.group(2)), int(match.group(1))) if match else None


def image_size(path: str) -> Optional[tuple[int, int]]:
    reader = QImageReader(path)
    size = reader.size()
    if size.isValid() and size.width() > 0:
        return size.width(), size.height()
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".psd", ".psb"):
            return _psd_size(path)
        if ext == ".hdr":
            return _hdr_size(path)
        from PIL import Image  # header only: Image.open doesn't decode pixels

        with Image.open(path) as im:
            return im.size
    except Exception:  # pylint: disable=broad-except
        return None


def read_details(path: str, extension: str) -> Optional[AssetMeta]:
    """The file's details (EMPTY if there's nothing to read), or None to try again later
    (a video while ffmpeg isn't installed)."""
    ext = extension.lower()
    try:
        if ext in TEXTURE_EXTENSIONS:
            size = image_size(path)
            return AssetMeta(width=size[0], height=size[1]) if size else EMPTY
        if ext in VIDEO_EXTENSIONS:
            from .video_tools import find_ffmpeg, probe_video

            if find_ffmpeg() is None:
                return None
            probed = probe_video(Path(path))
            if probed is None:
                return EMPTY
            duration, width, height = probed
            return AssetMeta(width=width or None, height=height or None, duration_ms=duration)
        if ext in MODEL_EXTENSIONS and os.path.getsize(path) <= MAX_MODEL_BYTES:
            from .model_loader import load_3d_model

            mesh = load_3d_model(Path(path))
            if "Error" in mesh.metadata:  # the loader fell back to a placeholder cube
                return EMPTY
            return AssetMeta(poly_count=mesh.face_count)
    except Exception:  # pylint: disable=broad-except
        pass
    return EMPTY
