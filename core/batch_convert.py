"""Batch convert and resize pictures: new files in another format and / or smaller, the originals never
touched.

- Formats: PNG, JPG, TGA, TIF. Pictures are read at full precision: 16-bit greys (heights, masks) stay
  16-bit in PNG and TIF, and 16 / 32-bit colour (PSD, EXR) stays 16-bit in PNG; JPG and TGA are 8-bit.
  EXR / HDR values are clipped to 0..1 (they're data maps here, not light: no tone mapping).
- Size: the longest side down to a size (never up), optionally each side down to a power of two.
- Where: a subfolder next to each file ("2K"), or one folder; named as they are, or by a batch rename
  pattern (core.batch_rename). A name that's already taken is left alone unless replacing is asked for,
  and an original is never written over.
- Files inside archives are read from them; the new files go outside.

convert() does one file, on a background thread; the dialog (ui.batch_dialogs) runs them in turn.
"""

from __future__ import annotations

import io
import os
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
from PIL import Image

from .scanner import AssetItem

FORMATS = {"png": ".png", "jpg": ".jpg", "tga": ".tga", "tif": ".tif"}
FORMAT_NAMES = {"png": "PNG", "jpg": "JPG", "tga": "TGA", "tif": "TIF"}
SIZES = (0, 512, 1024, 2048, 4096)  # longest side (0: as it is)
JPEG_QUALITY = 92
MAX_PIXELS = 400_000_000  # (a 20,000 px square scan: bigger ones are refused, they'd need gigabytes)


def size_name(side: int) -> str:
    return "Full Size" if not side else f"{side // 1024}K" if side % 1024 == 0 else f"{side} px"


@dataclass(frozen=True)
class ConvertOptions:
    format: str = "png"  # FORMATS key, or "same": the file's own (when it's one of them, else PNG)
    max_side: int = 0  # 0: as it is
    power_of_two: bool = False
    folder: str = ""  # one folder for everything; "" for a subfolder next to each file
    subfolder: str = "converted"
    replace: bool = False  # write over files of the same name (never the originals)


@dataclass
class ConvertRow:
    asset: AssetItem
    target: Path
    problem: str = ""


@dataclass
class ConvertPlan:
    rows: list[ConvertRow] = field(default_factory=list)

    @property
    def jobs(self) -> list[ConvertRow]:
        return [row for row in self.rows if not row.problem]


def target_extension(asset: AssetItem, options: ConvertOptions) -> str:
    if options.format == "same":
        ext = asset.extension.lower()
        return {".jpeg": ".jpg", ".tiff": ".tif"}.get(ext, ext) if ext in (".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff") else ".png"
    return FORMATS[options.format]


def plan(assets: Sequence[AssetItem], options: ConvertOptions, names: Optional[Sequence[str]] = None) -> ConvertPlan:
    """Where each picture goes, and what's in the way. names: new names (from a rename pattern), else theirs."""
    result = ConvertPlan()
    seen: dict[str, ConvertRow] = {}
    for index, asset in enumerate(assets):
        stem = os.path.splitext(names[index] if names else asset.name)[0]
        if asset.in_archive:
            beside = Path(str(asset.archive.archive)).parent
        else:
            beside = Path(asset.path).parent
        folder = Path(options.folder) if options.folder else beside / options.subfolder
        target = folder / (stem + target_extension(asset, options))
        row = ConvertRow(asset, target)
        key = os.path.normcase(str(target))
        if not asset.is_texture:
            row.problem = "Not a picture"
        elif not asset.in_archive and key == os.path.normcase(str(asset.path)):
            row.problem = "It would replace the original"
        elif key in seen:
            row.problem = "Another file would get this name"
        elif target.exists() and not options.replace:
            row.problem = "Already there (tick Replace to write over it)"
        seen.setdefault(key, row)
        result.rows.append(row)
    return result


# ----------------------------------------------------------------------
# Reading at full precision
# ----------------------------------------------------------------------
@dataclass
class Pixels:
    """(h, w, channels): 1 grey, 2 grey + alpha, 3 colour, 4 colour + alpha. deep: more than 8 bits were
    there, kept as float32 0..1; else uint8 (an 8K RGBA picture as floats would take 1 GB)."""

    values: np.ndarray
    deep: bool


def _from_pil(im: Image.Image) -> Pixels:
    if im.width * im.height > MAX_PIXELS:
        raise ValueError(f"{im.width} x {im.height} is too big to convert here")
    mode = im.mode
    if mode.startswith("I;16"):
        return Pixels(np.asarray(im, dtype=np.float32)[..., None] / 65535.0, True)
    if mode in ("I", "F"):
        values = np.asarray(im, dtype=np.float32)
        top = float(values.max()) if values.size else 1.0
        scale = (1.0 if top <= 1.0 else top) if mode == "F" else (255.0 if top <= 255 else 65535.0 if top <= 65535 else top)
        return Pixels(np.clip(values / scale, 0, 1)[..., None], scale != 255.0)
    if mode in ("1", "L"):
        return Pixels(np.asarray(im.convert("L"))[..., None], False)
    if mode == "LA":
        return Pixels(np.asarray(im), False)
    if mode in ("P", "PA"):
        im = im.convert("RGBA" if "transparency" in im.info or mode == "PA" else "RGB")
    elif mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")  # (CMYK, YCbCr ...)
    return Pixels(np.asarray(im), False)


def _from_psd(path: Path) -> Pixels:
    from .psd_reader import read_psd_composite, read_psd_planes

    planes, depth, color_mode, _base = read_psd_planes(path)
    scale = {8: 255.0, 16: 65535.0, 32: 1.0}[depth]
    if depth == 8 and color_mode in (1, 8):
        return Pixels(np.ascontiguousarray(planes[0])[..., None], False)
    if depth == 8 and color_mode == 3 and len(planes) >= 3:
        return Pixels(np.ascontiguousarray(np.moveaxis(planes[:3], 0, -1)), False)
    if color_mode in (1, 8):
        return Pixels(np.clip(planes[0].astype(np.float32) / scale, 0, 1)[..., None], True)
    if color_mode == 3 and len(planes) >= 3:
        return Pixels(np.clip(np.moveaxis(planes[:3], 0, -1).astype(np.float32) / scale, 0, 1), True)
    with read_psd_composite(path) as im:
        return _from_pil(im)


def _from_hdr(path: Path) -> Pixels:
    from .hdr_image import _read_exr, _read_radiance

    values = _read_exr(path) if path.suffix.lower() == ".exr" else _read_radiance(path, None)
    values = np.nan_to_num(np.asarray(values, dtype=np.float32), nan=0.0, posinf=1.0, neginf=0.0)
    if values.ndim == 2:
        values = values[..., None]
    return Pixels(np.clip(values[..., :4], 0, 1), True)


def read_pixels(path: Path, name: str) -> Pixels:
    ext = Path(name).suffix.lower()
    if ext in (".psd", ".psb"):
        return _from_psd(path)
    if ext in (".exr", ".hdr"):
        return _from_hdr(path)
    with Image.open(path) as im:
        im.load()
        return _from_pil(im)


def _read_asset(asset: AssetItem) -> Pixels:
    if not asset.in_archive:
        return read_pixels(Path(asset.path), asset.name)
    from .archives import local_file

    with local_file(asset.archive) as copy:
        return read_pixels(copy, asset.name)


# ----------------------------------------------------------------------
# Resizing and writing
# ----------------------------------------------------------------------
def target_size(width: int, height: int, max_side: int, power_of_two: bool) -> tuple[int, int]:
    """The new size: the longest side at most max_side (never bigger than now), each side then down to a
    power of two if asked."""
    scale = min(1.0, max_side / max(width, height)) if max_side else 1.0
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    if power_of_two:
        w, h = 1 << (w.bit_length() - 1), 1 << (h.bit_length() - 1)
    return w, h


def resize(values: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Resized with Lanczos: 8-bit pictures by Pillow as they are, deeper ones each channel in floats (no
    precision lost)."""
    if (values.shape[1], values.shape[0]) == size:
        return values
    if values.dtype == np.uint8:
        mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}[values.shape[2]]
        image = Image.fromarray(values[..., 0] if mode == "L" else values, mode)
        resized = np.asarray(image.resize(size, Image.Resampling.LANCZOS))
        return resized[..., None] if mode == "L" else resized
    channels = [
        np.asarray(Image.fromarray(np.ascontiguousarray(values[..., c]), "F").resize(size, Image.Resampling.LANCZOS))
        for c in range(values.shape[2])
    ]
    return np.clip(np.stack(channels, axis=-1), 0, 1)


def _png16(values: np.ndarray) -> bytes:
    """A 16-bit PNG of 1-4 channels (Pillow writes 16-bit greys only)."""
    height, width, channels = values.shape
    color_type = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    pixels = np.round(values * 65535).astype(">u2").reshape(height, width * channels)
    raw = np.concatenate([np.zeros((height, 1), dtype=np.uint8), pixels.view(np.uint8).reshape(height, -1)], axis=1)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 16, color_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b"")


def encode(pixels: Pixels, extension: str) -> bytes:
    """The picture as a file of this type."""
    values, channels = pixels.values, pixels.values.shape[2]
    out = io.BytesIO()
    if extension == ".png" and pixels.deep:
        if channels == 1:
            Image.fromarray(np.round(values[..., 0] * 65535).astype(np.uint16)).save(out, "PNG")
            return out.getvalue()
        return _png16(values)
    if extension == ".tif" and pixels.deep and channels == 1:
        Image.fromarray(np.round(values[..., 0] * 65535).astype(np.uint16)).save(out, "TIFF", compression="tiff_deflate")
        return out.getvalue()
    eight = values if values.dtype == np.uint8 else np.round(values * 255).astype(np.uint8)
    if extension == ".jpg" and channels in (2, 4):
        eight = eight[..., :channels - 1]  # (no transparency in a JPG)
    mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}[eight.shape[2]]
    image = Image.fromarray(eight[..., 0] if mode == "L" else eight, mode)
    if extension == ".jpg":
        image.convert("RGB" if mode != "L" else "L").save(out, "JPEG", quality=JPEG_QUALITY)
    elif extension == ".tga":
        (image.convert("RGBA") if mode == "LA" else image).save(out, "TGA", compression="tga_rle")
    elif extension == ".tif":
        image.save(out, "TIFF", compression="tiff_deflate")
    else:
        image.save(out, "PNG")
    return out.getvalue()


def convert(row: ConvertRow, options: ConvertOptions, stop: Callable[[], bool] = lambda: False) -> tuple[int, int]:
    """Make one new file (raises OSError / ValueError if it can't be read or written). Returns its size."""
    pixels = _read_asset(row.asset)
    height, width = pixels.values.shape[:2]
    size = target_size(width, height, options.max_side, options.power_of_two)
    if stop():
        raise InterruptedError
    pixels = Pixels(resize(pixels.values, size), pixels.deep)
    data = encode(pixels, row.target.suffix.lower())
    row.target.parent.mkdir(parents=True, exist_ok=True)
    partial = row.target.with_name(row.target.name + ".partial")
    partial.write_bytes(data)
    os.replace(partial, row.target)
    return size
