"""Decoding an image for the viewer: on a worker thread (safe there: it makes a QImage, not a
QPixmap), at most MAX_SIDE pixels on its longest side, and one image at a time.

Big files are read at a reduced size where the format allows (PSD, EXR / HDR skip pixels while
reading; JPEG decodes small), and otherwise shrunk right after decoding, before the conversions
and copies that used to multiply a 400-megapixel TIFF into several GB. Images Pillow refuses as
"decompression bombs" (over ~180 megapixels: e.g. 23000 x 17400 displacement maps) are opened
anyway up to MAX_PIXELS: Pillow's check is skipped on the thread decoding for the viewer only, so
the grid's thumbnails (other threads) keep the limit and their "Too large to preview" card.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, QImageReader

from .metadata import image_size
from .thumbnail_manager import _safe_convert_pil_to_rgba, open_image

MAX_SIDE = 8192  # shown at most this big (the GPU and memory both like it that way)
MAX_PIXELS = 1_000_000_000  # bigger files aren't opened: decoding one would take many GB
_DECODES = threading.Semaphore(1)  # one big decode at a time (stepping through files quickly)

# Pillow refuses images over ~180 megapixels in Image.open and again while loading (TIFF), reading
# one global limit each time; this skips the check while _unlimited.on is set, on that thread only
_unlimited = threading.local()
_pillow_size_check = Image._decompression_bomb_check  # pylint: disable=protected-access


def _size_check(size: tuple[int, int]) -> None:
    if not getattr(_unlimited, "on", False):
        _pillow_size_check(size)


Image._decompression_bomb_check = _size_check  # pylint: disable=protected-access


class ImageTooLarge(Exception):
    def __init__(self, size: tuple[int, int]):
        super().__init__(
            f"It's {size[0]:,} × {size[1]:,} pixels ({size[0] * size[1] / 1e6:,.0f} megapixels), more than "
            f"the viewer opens ({MAX_PIXELS / 1e6:,.0f}). Open it in another app instead."
        )
        self.size = size


@dataclass(frozen=True)
class LoadedImage:
    image: QImage  # what's shown (at most MAX_SIDE on its longest side)
    has_alpha: bool  # some pixels are transparent
    size: tuple[int, int]  # the file's own width and height


def load_full_image(path: Path, still_wanted: Optional[Callable[[], bool]] = None) -> Optional[LoadedImage]:
    """The image to show (None if still_wanted() said no when its turn came). Raises with a message
    for the user if it can't be shown."""
    with _DECODES:
        if still_wanted is not None and not still_wanted():
            return None
        size = image_size(str(path))  # (from the header: the real size, also of files read reduced)
        if size is not None and size[0] * size[1] > MAX_PIXELS:
            raise ImageTooLarge(size)
        try:
            return _decode_with_pillow(path, size)
        except ImageTooLarge:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            pillow_error = exc
        loaded = _decode_with_qt(path)
        if loaded is None:
            raise ValueError(str(pillow_error) or type(pillow_error).__name__)
        return loaded


def _decode_with_pillow(path: Path, size: Optional[tuple[int, int]]) -> LoadedImage:
    _unlimited.on = True  # (MAX_PIXELS was checked from the header; again below if it had no size)
    try:
        return _decode_unlimited(path, size)
    finally:
        _unlimited.on = False


def _decode_unlimited(path: Path, size: Optional[tuple[int, int]]) -> LoadedImage:
    with open_image(path, max_size=MAX_SIDE) as im:
        size = size or im.size
        if size[0] * size[1] > MAX_PIXELS:
            raise ImageTooLarge(size)
        small = _shrink(im, MAX_SIDE)
        if small.mode in ("RGB", "RGBA", "L", "LA") and max(small.size) > MAX_SIDE:
            small.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.BILINEAR)  # (before the RGBA copy: smaller)
        # Then RGBA: normalizes 16-bit / float images (BILINEAR fails on I;16, so those resize after)
        rgba = _safe_convert_pil_to_rgba(small)
        del small
        if max(rgba.size) > MAX_SIDE:
            rgba.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.BILINEAR)
        has_alpha = rgba.getchannel("A").getextrema()[0] < 255
        image = QImage(rgba.tobytes(), rgba.width, rgba.height, QImage.Format.Format_RGBA8888).copy()
    return LoadedImage(image, has_alpha, size)


def _shrink(im: Image.Image, max_side: int) -> Image.Image:
    """The image at most about max_side (within 2x) big, by a whole-number factor: cheap, and done
    before any conversion so the full-size picture exists only once, in its own (smallest) form."""
    factor = max(im.size) // max_side  # (rounded down: the final resize takes it the rest of the way)
    if factor <= 1:
        return im
    if im.mode == "P":
        im = im.convert("RGBA")  # (palette indices can't be averaged)
    try:
        return im.reduce(factor)
    except (ValueError, NotImplementedError):  # e.g. 16-bit grayscale: every factor-th pixel instead
        return Image.fromarray(np.ascontiguousarray(np.asarray(im)[::factor, ::factor]))


def _decode_with_qt(path: Path) -> Optional[LoadedImage]:
    """Formats Pillow can't read: Qt's image plugins, scaled down while reading where they can."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    reader.setAllocationLimit(2048)
    full = reader.size()
    if full.isValid() and full.width() * full.height() > MAX_PIXELS:
        raise ImageTooLarge((full.width(), full.height()))
    if full.isValid() and max(full.width(), full.height()) > MAX_SIDE:
        scale = MAX_SIDE / max(full.width(), full.height())
        reader.setScaledSize(QSize(max(1, round(full.width() * scale)), max(1, round(full.height() * scale))))
    image = reader.read()
    if image.isNull():
        return None
    size = (full.width(), full.height()) if full.isValid() else (image.width(), image.height())
    return LoadedImage(image, image.hasAlphaChannel(), size)
