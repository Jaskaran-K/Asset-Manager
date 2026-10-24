"""Minimal reader for the flattened composite image stored in PSD / PSB files.

Pillow only decodes 8-bit PSDs, but many texture and alpha packs (e.g. ZBrush
alphas) are saved as 16-bit grayscale. This reads the "Image Data" section,
which Photoshop always writes as a fully merged copy of the document, and
returns it as an 8-bit Pillow image.

Supports 8/16/32-bit Grayscale, RGB, CMYK and Duotone documents with raw,
RLE (PackBits) or ZIP compression.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

_MODE_GRAYSCALE = 1
_MODE_RGB = 3
_MODE_CMYK = 4
_MODE_DUOTONE = 8

_COMPRESSION_RAW = 0
_COMPRESSION_RLE = 1
_COMPRESSION_ZIP = 2
_COMPRESSION_ZIP_PREDICTION = 3


def read_psd_composite(file_path: Path | str, max_size: Optional[int] = None) -> Image.Image:
    """Read the merged composite of a PSD/PSB file as an 8-bit L, RGB, RGBA or CMYK image.

    If max_size is given, raw (uncompressed) files are subsampled while reading so
    thumbnails don't need to decode every pixel; the result is at least max_size on
    its longest side and still needs a final resize.
    """
    with open(file_path, "rb") as f:
        signature, version, _reserved, channels, height, width, depth, color_mode = struct.unpack(
            ">4sH6sHIIHH", f.read(26)
        )
        if signature != b"8BPS" or version not in (1, 2):
            raise ValueError("Not a PSD/PSB file")
        if depth not in (8, 16, 32):
            raise ValueError(f"Unsupported PSD bit depth: {depth}")
        if color_mode not in (_MODE_GRAYSCALE, _MODE_RGB, _MODE_CMYK, _MODE_DUOTONE):
            raise ValueError(f"Unsupported PSD color mode: {color_mode}")

        is_psb = version == 2
        # Skip color mode data, image resources, then layer & mask info
        f.seek(struct.unpack(">I", f.read(4))[0], 1)
        f.seek(struct.unpack(">I", f.read(4))[0], 1)
        layer_len = struct.unpack(">Q", f.read(8))[0] if is_psb else struct.unpack(">I", f.read(4))[0]
        f.seek(layer_len, 1)

        compression = struct.unpack(">H", f.read(2))[0]
        data_offset = f.tell()

        base_channels = 3 if color_mode == _MODE_RGB else 4 if color_mode == _MODE_CMYK else 1
        # One extra channel after the color channels is treated as transparency (matches Pillow)
        used_channels = min(channels, base_channels + 1)
        dtype = {8: np.dtype(">u1"), 16: np.dtype(">u2"), 32: np.dtype(">f4")}[depth]
        row_bytes = width * dtype.itemsize

        if compression == _COMPRESSION_RAW:
            step = 1
            if max_size:
                step = max(1, max(width, height) // max_size)
            planes = np.memmap(
                file_path, dtype=dtype, mode="r", offset=data_offset, shape=(channels, height, width)
            )
            pixels = np.array(planes[:used_channels, ::step, ::step])
            del planes  # release the file mapping (Windows keeps the file locked otherwise)

        elif compression == _COMPRESSION_RLE:
            count_type = ">u4" if is_psb else ">u2"
            row_counts = np.frombuffer(
                f.read(channels * height * np.dtype(count_type).itemsize), dtype=count_type
            ).reshape(channels, height)
            pixels = np.empty((used_channels, height, width), dtype=dtype)
            for ch in range(used_channels):
                packed = f.read(int(row_counts[ch].sum()))
                # PackBits runs never cross rows, so the whole plane decodes in one pass
                plane = Image.frombytes("L", (row_bytes, height), packed, "packbits", "L")
                pixels[ch] = np.frombuffer(plane.tobytes(), dtype=dtype).reshape(height, width)

        elif compression in (_COMPRESSION_ZIP, _COMPRESSION_ZIP_PREDICTION):
            raw = np.frombuffer(zlib.decompress(f.read()), dtype=np.uint8)
            if compression == _COMPRESSION_ZIP_PREDICTION and depth != 32:
                # Rows are stored as deltas between neighboring samples
                samples = raw.view(dtype).reshape(channels, height, width).astype(dtype.newbyteorder("="))
                pixels = np.cumsum(samples, axis=2, dtype=samples.dtype)[:used_channels]
            elif compression == _COMPRESSION_ZIP_PREDICTION:
                raise ValueError("32-bit ZIP-with-prediction PSDs are not supported")
            else:
                pixels = raw.view(dtype).reshape(channels, height, width)[:used_channels]
        else:
            raise ValueError(f"Unsupported PSD compression: {compression}")

    return _planes_to_image(pixels, depth, color_mode, base_channels)


def _planes_to_image(pixels: np.ndarray, depth: int, color_mode: int, base_channels: int) -> Image.Image:
    """Convert planar (channels, h, w) samples to an 8-bit Pillow image."""
    if depth == 16:
        pixels8 = (pixels >> 8).astype(np.uint8)
    elif depth == 32:
        pixels8 = (np.clip(pixels, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    else:
        pixels8 = pixels.astype(np.uint8)

    if color_mode == _MODE_CMYK:
        # Photoshop stores CMYK inverted (0 = full ink)
        pixels8 = 255 - pixels8
        return Image.fromarray(np.ascontiguousarray(pixels8[:4].transpose(1, 2, 0)), "CMYK")

    has_alpha = pixels8.shape[0] > base_channels
    if base_channels == 1:
        if has_alpha:
            return Image.fromarray(np.ascontiguousarray(pixels8[:2].transpose(1, 2, 0)), "LA")
        return Image.fromarray(np.ascontiguousarray(pixels8[0]), "L")

    mode = "RGBA" if has_alpha else "RGB"
    return Image.fromarray(np.ascontiguousarray(pixels8.transpose(1, 2, 0)), mode)
