"""High dynamic range images: OpenEXR (.exr, read with the OpenEXR package) and Radiance
(.hdr, read here). Pillow and Qt can read neither.

read_hdr_image() returns an ordinary 8-bit PIL image for showing on screen: HDRIs are tone
mapped (bright skies keep their detail), data maps that stay within 0-1 (normal maps) are shown
as they are, and one-channel maps (height / displacement) are stretched to their own range.
"""

from __future__ import annotations

import re
import struct
import threading
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

HDR_EXTENSIONS = (".exr", ".hdr")
# A 4k HDRI takes ~100 MB of floats while it's read, an 8k one ~400 MB: one file at a time
_DECODES = threading.Semaphore(1)
_RESOLUTION = re.compile(r"([-+])([XY]) (\d+) ([-+])([XY]) (\d+)")


def is_hdr(path: Path | str) -> bool:
    return Path(path).suffix.lower() in HDR_EXTENSIONS


def image_size(path: Path | str) -> Optional[tuple[int, int]]:
    """(width, height) from the file's header, without reading the pixels."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".exr":
            import OpenEXR

            with OpenEXR.File(str(path), header_only=True) as f:
                low, high = f.header()["dataWindow"]
                return int(high[0] - low[0] + 1), int(high[1] - low[1] + 1)
        with open(path, "rb") as f:
            head = f.read(8192)
        _header, (width, height, _flip_x, _flip_y), _start = _radiance_header(head)
        return width, height
    except Exception:  # pylint: disable=broad-except
        return None


def read_hdr_image(path: Path | str, max_size: Optional[int] = None) -> Image.Image:
    """The image as 8-bit RGB / RGBA / L for display. With max_size, every n-th pixel is kept so the
    result is still at least max_size on its longest side (it needs a final resize, as PSDs do)."""
    path = Path(path)
    with _DECODES:
        if path.suffix.lower() == ".exr":
            pixels = _subsample(_read_exr(path), max_size)
        else:
            pixels = _read_radiance(path, max_size)
    return Image.fromarray(_to_display(pixels))


def read_linear(path: Path | str) -> np.ndarray:
    """Every pixel as linear light (H x W x 3 float32), for lighting a scene: skipping pixels, as
    read_hdr_image may, can miss the sun, and with it nearly all of the light."""
    path = Path(path)
    with _DECODES:
        pixels = _read_exr(path) if path.suffix.lower() == ".exr" else _read_radiance(path, None)
    if pixels.ndim != 3 or pixels.shape[2] < 3:
        raise ValueError("It has no colour channels")
    pixels = np.nan_to_num(pixels[..., :3].astype(np.float32, copy=False), nan=0.0, posinf=65504.0, neginf=0.0)
    return pixels.clip(0, None)


def header_info(head: bytes, extension: str) -> Optional[tuple[int, int, bool]]:
    """(width, height, has colour) from the first bytes of an .exr / .hdr, or None if they can't
    be read. Takes bytes, so a file inside an archive needs no unpacking."""
    try:
        if extension.lower() == ".exr":
            return _exr_header_info(head)
        _header, (width, height, _flip_x, _flip_y), _start = _radiance_header(head)
        return width, height, True
    except (ValueError, IndexError, struct.error):
        return None


def _exr_header_info(head: bytes) -> Optional[tuple[int, int, bool]]:
    """EXR attributes are (name, type, size, value), ended by an empty name. Both wanted ones come
    before any preview image, which can be bigger than the bytes read."""
    if head[:4] != b"\x76\x2f\x31\x01":
        return None
    at, size, colour = 8, None, None
    while head[at] != 0 and (size is None or colour is None):
        end = head.index(b"\0", at)
        name = head[at:end]
        at = head.index(b"\0", end + 1) + 1
        (length,) = struct.unpack_from("<i", head, at)
        at += 4
        if name == b"dataWindow":
            x0, y0, x1, y1 = struct.unpack_from("<iiii", head, at)
            size = x1 - x0 + 1, y1 - y0 + 1
        elif name == b"channels":
            channels, pos = [], at
            while head[pos] != 0:  # (each: a name, then 16 bytes about it)
                end = head.index(b"\0", pos)
                channels.append(head[pos:end])
                pos = end + 17
            colour = any(c == b"R" or c.endswith(b".R") for c in channels)
        at += length
    return (size[0], size[1], bool(colour)) if size is not None and colour is not None else None


def _subsample(pixels: np.ndarray, max_size: Optional[int]) -> np.ndarray:
    step = max(1, max(pixels.shape[:2]) // max_size) if max_size else 1
    return pixels[::step, ::step] if step > 1 else pixels


# ----------------------------------------------------------------------
# OpenEXR
# ----------------------------------------------------------------------
def _read_exr(path: Path) -> np.ndarray:
    import OpenEXR

    with OpenEXR.File(str(path)) as f:  # (the pixels are only there while it's open)
        channels = f.channels()  # R, G, B (A) come grouped as "RGB" / "RGBA"
        for name in ("RGBA", "RGB"):
            if name in channels:
                return channels[name].pixels
        grouped = [c for name, c in channels.items() if name.endswith(("RGBA", "RGB"))]  # layers: "diffuse.RGB"
        if grouped:
            return grouped[0].pixels
        if not channels:
            raise ValueError("The file has no image channels")
        first = channels.get("Y") or next(iter(channels.values()))  # one channel: height, depth, luminance
        return first.pixels


# ----------------------------------------------------------------------
# Radiance .hdr (RGBE, usually run-length encoded scanlines)
# ----------------------------------------------------------------------
def _radiance_header(data: bytes) -> tuple[str, tuple[int, int, bool, bool], int]:
    """(header text, (width, height, flip x, flip y), offset of the pixels)."""
    if not data.startswith(b"#?"):
        raise ValueError("Not a Radiance HDR file")
    end = data.find(b"\n\n")
    if end < 0:
        raise ValueError("The HDR header is incomplete")
    header = data[:end].decode("latin-1")
    line_end = data.find(b"\n", end + 2)
    match = _RESOLUTION.fullmatch(data[end + 2:line_end].decode("latin-1").strip())
    if not match or match.group(2) != "Y" or match.group(5) != "X":
        raise ValueError("Unsupported HDR orientation")  # (rotated files are practically never seen)
    height, width = int(match.group(3)), int(match.group(6))
    return header, (width, height, match.group(4) == "-", match.group(1) == "+"), line_end + 1


def _read_radiance(path: Path, max_size: Optional[int]) -> np.ndarray:
    data = path.read_bytes()
    _header, (width, height, flip_x, flip_y), pos = _radiance_header(data)
    step = max(1, max(width, height) // max_size) if max_size else 1
    rows = []
    rle = 8 <= width < 0x8000
    for y in range(height):
        if rle and data[pos] == 2 and data[pos + 1] == 2 and (data[pos + 2] << 8 | data[pos + 3]) == width:
            pos, scanline = _rle_scanline(data, pos + 4, width, keep=y % step == 0)
        else:  # flat RGBE pixels
            scanline = np.frombuffer(data, np.uint8, width * 4, pos).reshape(width, 4) if y % step == 0 else None
            pos += width * 4
        if scanline is not None:
            rows.append(scanline[::step])
    rgbe = np.stack(rows)
    exponent = rgbe[..., 3].astype(np.int32)
    scale = np.where(exponent > 0, np.ldexp(1.0, exponent - 136), 0.0).astype(np.float32)
    rgb = (rgbe[..., :3].astype(np.float32) + 0.5) * scale[..., None]
    if flip_y:
        rgb = rgb[::-1]
    if flip_x:
        rgb = rgb[:, ::-1]
    return rgb


def _rle_scanline(data: bytes, pos: int, width: int, keep: bool) -> tuple[int, Optional[np.ndarray]]:
    """One run-length encoded scanline (R, G, B and E each stored as runs): (next offset, pixels)."""
    components = []
    for _component in range(4):
        out = bytearray(width) if keep else None
        x = 0
        while x < width:
            count = data[pos]
            run = count > 128  # a run of one value, else count literal values
            if run:
                count -= 128
            if count == 0 or count > width - x:
                raise ValueError("The HDR file is damaged")
            if run:
                if keep:
                    out[x:x + count] = bytes((data[pos + 1],)) * count
                pos += 2
            else:  # count literal values
                if keep:
                    out[x:x + count] = data[pos + 1:pos + 1 + count]
                pos += 1 + count
            x += count
        components.append(out)
    if not keep:
        return pos, None
    return pos, np.stack([np.frombuffer(c, np.uint8) for c in components], axis=1)


# ----------------------------------------------------------------------
# Display
# ----------------------------------------------------------------------
def _to_display(pixels: np.ndarray) -> np.ndarray:
    """Linear float pixels (H x W, or H x W x 3 / 4) as 8-bit sRGB."""
    pixels = np.nan_to_num(pixels.astype(np.float32, copy=False), nan=0.0, posinf=65504.0, neginf=0.0)
    if pixels.ndim == 2 or pixels.shape[2] == 1:  # height / displacement: stretch to its own range
        gray = pixels.reshape(pixels.shape[:2])
        low, high = np.percentile(gray[::4, ::4], (0.5, 99.5))
        span = high - low if high > low else 1.0
        return (np.clip((gray - low) / span, 0, 1) * 255 + 0.5).astype(np.uint8)
    rgb, alpha = pixels[..., :3].clip(0, None), pixels[..., 3:4] if pixels.shape[2] == 4 else None
    value = rgb.max(axis=2)  # (mapping each pixel's brightest channel keeps its hue: nothing clips to a different color)
    white = float(np.percentile(value[::4, ::4], 99.5))
    if white > 1.0:  # an HDRI: extended Reinhard keeps the sky's detail, with white at the 99.5th percentile
        mapped = value * (1 + value / (white * white)) / (1 + value)
        rgb = rgb * (mapped / np.maximum(value, 1e-6))[..., None]
    rgb = np.clip(rgb, 0, 1)
    srgb = np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * np.power(rgb, 1 / 2.4) - 0.055)
    out = srgb if alpha is None else np.concatenate([srgb, np.clip(alpha, 0, 1)], axis=2)
    return (out * 255 + 0.5).astype(np.uint8)
