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
        if ext == ".exr":
            from .hdr_image import image_size as exr_size

            return exr_size(path)
        from PIL import Image  # header only: Image.open doesn't decode pixels

        with Image.open(path) as im:
            return im.size
    except Exception:  # pylint: disable=broad-except
        return None


_HEAD_BYTES = 1024 * 1024  # enough of an image inside an archive for its header


def _member_image_size(path: str) -> Optional[tuple[int, int]]:
    """An image inside an archive: its size from the start of it (only that much is unpacked)."""
    import io

    from .archives import ArchiveError, iter_chunks, member_for_path

    member = member_for_path(path)
    if member is None:
        return None
    head = bytearray()
    try:
        for piece in iter_chunks(member):
            head += piece
            if len(head) >= _HEAD_BYTES:
                break
    except ArchiveError:
        return None
    if head[:4] == b"8BPS":
        height, width = struct.unpack(">II", bytes(head[14:22]))
        return width, height
    from PIL import Image

    with Image.open(io.BytesIO(bytes(head))) as im:
        return im.size


MAX_MEMBER_CHECK_BYTES = 20 * 1024 * 1024  # an image inside an archive is unpacked up to this to tell if it's an alpha


def _member_alpha(path: str) -> bool:
    """Whether an image inside an archive is an alpha (core.alphas): read in memory, or a PSD / EXR
    (their readers want a file) from a temporary copy; bigger ones only when they're one channel."""
    import io

    from . import alphas
    from .archives import ArchiveError, iter_chunks, local_file, member_for_path, read_bytes

    member = member_for_path(path)
    if member is None or alphas.named_like_map(member.base_name):
        return False
    try:
        if member.size > MAX_MEMBER_CHECK_BYTES:
            head = bytearray()
            for piece in iter_chunks(member):
                head += piece
                if len(head) >= _HEAD_BYTES:
                    break
            if member.base_name.lower().endswith((".psd", ".psb")):
                return head[:4] == b"8BPS" and struct.unpack(">H", bytes(head[24:26]))[0] in (1, 8)
            from PIL import Image

            with Image.open(io.BytesIO(bytes(head))) as im:
                return im.mode in alphas.SINGLE_CHANNEL_MODES
        if member.base_name.lower().endswith((".psd", ".psb", ".exr")):
            with local_file(member) as copy:
                return alphas.detect(copy, member.base_name)
        return alphas.detect_bytes(read_bytes(member), member.base_name)
    except (ArchiveError, OSError, ValueError):
        return False


def read_details(path: str, extension: str) -> Optional[AssetMeta]:
    """The file's details (EMPTY if there's nothing to read), or None to try again later
    (a video while ffmpeg isn't installed). Inside an archive, only images have theirs read."""
    ext = extension.lower()
    try:
        if "|" in path:  # (core.archives.SEPARATOR: a file inside an archive)
            if ext not in TEXTURE_EXTENSIONS:
                return EMPTY
            size = _member_image_size(path)
            return AssetMeta(width=size[0] if size else None, height=size[1] if size else None, alpha=_member_alpha(path))
        if ext in TEXTURE_EXTENSIONS:
            from .alphas import detect

            size = image_size(path)
            if not size:
                return EMPTY  # (unreadable)
            return AssetMeta(width=size[0], height=size[1], alpha=detect(path, os.path.basename(path)))
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
            if mesh.stand_in:  # the loader fell back to a placeholder cube
                return EMPTY
            return AssetMeta(poly_count=mesh.face_count)
    except Exception:  # pylint: disable=broad-except
        pass
    return EMPTY
