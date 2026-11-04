"""Pictures for files the app can't draw itself: the one a .blend or a PureRef board holds, or a
picture beside the file that's clearly of it.

- A .blend starts with a small render saved by Blender (its TEST block; files can be compressed
  with gzip or Zstandard).
- A PureRef board holds a 256 px JPEG of itself near its start. Only that much of the file is read,
  and its empty margins are cut off.
- A brush pack's pictures are matched by name only. In the library most brushes have none, and
  guessing from "the only image in the folder" or a folder up picked wrong ones (a settings
  screenshot, another pack's alpha), so a file gets a picture only if a name says it's that file's.
"""

from __future__ import annotations

import io
import os
import re
import struct
import zlib
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
from PIL import Image

from . import archives
from .scanner import AssetItem

HEAD_BYTES = 2 * 1024 * 1024  # of a file, for the picture it holds near its start
_MAX_UNPACKED_HEAD = 4 * 1024 * 1024
_MAX_THUMBNAIL_SIDE = 2048
_JPEG_START = b"\xff\xd8\xff"
_BOARD_PREVIEW_WITHIN = 4096  # the JPEG starts this near the start of a board (108 bytes in the files seen)
_BOARD_MARGIN_DIFFERENCE = 24  # out of 255: how far from the background a board's margin may be
_MIN_TRIMMED_SIDE = 32

_PICTURE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".tga", ".psd")
_PREVIEW_NAME = re.compile(r"(^|[^a-z])(preview|prev|cover|thumb|thumbnail|icon|ico)([^a-z]|$)", re.IGNORECASE)
# Brushes come with a picture named after them (Carve_ico.png, Carve_alpha.psd). A scene's or a
# project's are as likely to be its textures, so it needs the exact name.
_NAMED_AFTER_IT = (".zbp", ".ztl", ".zpr", ".zmt")


def file_head(asset: AssetItem, limit: int = HEAD_BYTES) -> bytes:
    """The start of a file, on disk or inside an archive (raises OSError, archives.ArchiveError)."""
    if asset.in_archive:
        collected = bytearray()
        for chunk in archives.iter_chunks(asset.archive):
            collected += chunk
            if len(collected) >= limit:
                break
        return bytes(collected)
    with open(asset.path, "rb") as f:
        return f.read(limit)


def _blend_data(head: bytes) -> bytes:
    """A .blend's start, unpacked if it's compressed."""
    if head[:2] == b"\x1f\x8b":
        return zlib.decompressobj(31).decompress(head, _MAX_UNPACKED_HEAD)
    if head[:4] == b"\x28\xb5\x2f\xfd":
        from compression import zstd

        unpacked, rest = b"", head
        while rest and len(unpacked) < _MAX_UNPACKED_HEAD:  # (Blender writes many short frames)
            frame = zstd.ZstdDecompressor()
            unpacked += frame.decompress(rest, _MAX_UNPACKED_HEAD - len(unpacked))
            if not frame.eof:
                break
            rest = frame.unused_data
        return unpacked
    return head


def blend_picture(head: bytes) -> Optional[Image.Image]:
    """The render a .blend holds, from the start of the file (None if it has none, or can't be read)."""
    try:
        data = _blend_data(head)
    except Exception:  # pylint: disable=broad-except
        return None
    if data[:7] != b"BLENDER" or data[7:8] not in (b"-", b"_") or data[8:9] not in (b"v", b"V"):
        return None
    order = "<" if data[8:9] == b"v" else ">"
    header = 16 + (8 if data[7:8] == b"-" else 4)  # code, length, old address, type, count
    at = 12
    try:
        while at + header <= len(data):
            code, length = data[at:at + 4], struct.unpack_from(order + "i", data, at + 4)[0]
            body = at + header
            if code == b"TEST":
                width, height = struct.unpack_from(order + "ii", data, body)
                if not (0 < width <= _MAX_THUMBNAIL_SIDE and 0 < height <= _MAX_THUMBNAIL_SIDE):
                    return None
                pixels = data[body + 8:body + 8 + width * height * 4]
                if len(pixels) != width * height * 4:
                    return None
                return Image.frombytes("RGBA", (width, height), pixels).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            if code not in (b"REND", b"GLOB") or length < 0:
                return None  # (the picture comes first, or not at all)
            at = body + length
    except struct.error:
        pass
    return None


def board_picture(head: bytes) -> Optional[Image.Image]:
    """The picture a PureRef board holds of itself, without its empty margins (None if it has none:
    boards from before PureRef 2). head: the start of the file."""
    at = head.find(_JPEG_START, 14, _BOARD_PREVIEW_WITHIN)
    if at < 0:
        return None
    try:
        with Image.open(io.BytesIO(head[at:])) as im:
            picture = im.convert("RGB")
    except Exception:  # pylint: disable=broad-except
        return None
    return _trimmed(picture)


def _trimmed(picture: Image.Image) -> Image.Image:
    """Without the margins of the colour in its corner."""
    pixels = np.asarray(picture, dtype=np.int16)
    different = np.abs(pixels - pixels[0, 0]).max(axis=2) > _BOARD_MARGIN_DIFFERENCE
    rows, columns = np.flatnonzero(different.any(axis=1)), np.flatnonzero(different.any(axis=0))
    if rows.size == 0:
        return picture
    box = (int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1)
    if box[2] - box[0] < _MIN_TRIMMED_SIDE or box[3] - box[1] < _MIN_TRIMMED_SIDE:
        return picture  # (only a speck: the whole board says more)
    return picture.crop(box)


def pack_picture_name(file_name: str, folder_name: str, names: Iterable[str]) -> Optional[str]:
    """Which of the names in a file's folder is a picture of it (None: none clearly is)."""
    stem, extension = os.path.splitext(file_name)
    stem = stem.casefold()
    pictures = sorted(
        (n for n in names if n.lower().endswith(_PICTURE_SUFFIXES) and not n.startswith("._")), key=str.casefold,
    )
    stems = {name: os.path.splitext(name)[0] for name in pictures}
    for name in pictures:
        if stems[name].casefold() == stem:
            return name
    previews = [name for name in pictures if _PREVIEW_NAME.search(stems[name]) or stems[name].casefold() == folder_name.casefold()]
    if extension.lower() in _NAMED_AFTER_IT and stem:
        # (Its whole name, as words: a brush called "a" isn't in every picture with an a in its name)
        whole_name = re.compile(rf"(^|[^a-z0-9]){re.escape(stem)}([^a-z0-9]|$)")
        named = [name for name in pictures if whole_name.search(stems[name].casefold())]
        if named:
            return next((name for name in named if name in previews), named[0])
    return previews[0] if previews else None


def pack_picture(path: Path) -> Optional[Path]:
    """The picture beside a file that's clearly of it (None: there's none)."""
    path = Path(path)
    with os.scandir(path.parent) as listing:
        names = [entry.name for entry in listing if entry.is_file()]
    name = pack_picture_name(path.name, path.parent.name, names)
    return path.with_name(name) if name is not None else None


def member_pack_picture(member: "archives.ArchiveMember") -> Optional["archives.ArchiveMember"]:
    """The same inside an archive, among the files in the member's folder there. Pictures bigger than
    an archive's card picture may be aren't used: they'd have to be unpacked just for this."""
    result = archives.listing(member.archive, member.archive_size, member.archive_mtime)
    beside = {m.base_name: m for m in result.members if m.folder == member.folder and not m.encrypted}
    folder_name = member.folder.rpartition("/")[2]
    name = pack_picture_name(member.base_name, folder_name, beside)
    if name is None or beside[name].size > archives.MAX_CARD_PICTURE_BYTES:
        return None
    return beside[name]
