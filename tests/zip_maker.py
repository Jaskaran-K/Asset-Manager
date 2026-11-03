"""Builds zip files for tests, written by hand: Python's zipfile can't write Deflate64 (method 9),
which 232 images in the user's library use, nor mark entries encrypted."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

STORED, DEFLATED, DEFLATE64 = 0, 8, 9
_DOS_DATE = ((2024 - 1980) << 9) | (5 << 5) | 17  # 2024-05-17
_DOS_TIME = (14 << 11) | (30 << 5)  # 14:30


@dataclass
class Entry:
    name: str
    data: bytes
    method: int = DEFLATED
    encrypted: bool = False


def _pack(data: bytes, method: int) -> bytes:
    if method == STORED:
        return data
    if method == DEFLATED:
        packer = zlib.compressobj(6, zlib.DEFLATED, -15)
        return packer.compress(data) + packer.flush()
    import inflate64

    deflater = inflate64.Deflater()
    return deflater.deflate(data) + deflater.flush()


def make_zip(path: Path, entries: list[Entry]) -> Path:
    body, central = bytearray(), bytearray()
    for entry in entries:
        name = entry.name.encode("utf-8")
        packed = _pack(entry.data, entry.method)
        crc = zlib.crc32(entry.data)
        flags = 0x800 | (1 if entry.encrypted else 0)  # UTF-8 names
        version = 21 if entry.method == DEFLATE64 else 20
        offset = len(body)
        body += struct.pack(
            "<4s5H3L2H", b"PK\x03\x04", version, flags, entry.method, _DOS_TIME, _DOS_DATE,
            crc, len(packed), len(entry.data), len(name), 0,
        ) + name + packed
        central += struct.pack(
            "<4s6H3L5H2L", b"PK\x01\x02", version, version, flags, entry.method, _DOS_TIME, _DOS_DATE,
            crc, len(packed), len(entry.data), len(name), 0, 0, 0, 0, 0, offset,
        ) + name
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, len(entries), len(entries), len(central), len(body), 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(body + central + end))
    return path


def png_bytes(size: tuple[int, int] = (32, 32), color: str = "red") -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()
