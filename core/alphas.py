"""Alphas: grey height images (ZBrush alphas, displacement and height maps) and what's drawn from them.

- An image is an alpha when it's grey (one channel, or colour channels that are all the same) and its
  name doesn't say it's another kind of grey map (AO, gloss, roughness, metal, mask ...). On the
  user's library that's 2,609 of 2,824 images; leaving map names out drops 15.
- Heights are read at full precision (16-bit PNG, TIF and PSD; 32-bit float EXR) as 0..1 floats:
  at 256 px a 16-bit alpha keeps thousands of levels where 8 bits keep about 230, which shows as
  steps on gentle slopes.
- relief() shades heights as a lit relief, for the grid's thumbnails; height_mesh() makes the
  displaced plane the image viewer shows in 3D.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import BinaryIO, Optional, Union

import numpy as np
from PIL import Image

# Grey maps that aren't heights, by a word in the file name (_AO_, -Roughness, gloss.png ...)
MAP_NAME = re.compile(
    r"(^|[^a-z])(ao|ambient.?occlusion|occlusion|gloss|glossiness|rough|roughness|spec|specular|metal|metallic|"
    r"metalness|opacity|transparency|cavity|curvature|thickness|translucency|sss|mask|id|refl|reflection)([^a-z]|$)",
    re.IGNORECASE,
)
SINGLE_CHANNEL_MODES = ("1", "L", "LA", "I", "F", "I;16", "I;16L", "I;16B", "I;16N")
GREY_TOLERANCE = 2 / 255  # colour channels this close count as the same (JPEG noise)
SAMPLE_SIDE = 64  # an RGB image is checked for grey at this size
NOT_IMAGES = (".hdr",)  # (Radiance files are environments, not heights)
LIGHT = np.array([-1.0, -1.0, 1.2]) / np.linalg.norm([-1.0, -1.0, 1.2])  # from the top left, (x right, y down, z out)
CLAY = np.array([0.80, 0.78, 0.75])  # the relief's colour: neutral clay grey, as ZBrush shows alphas
RELIEF_DEPTH = 0.12  # the relief's depth, as a share of the picture's longest side

Source = Union[str, Path, BinaryIO]


def named_like_map(name: str) -> bool:
    """The file name says it's a grey map that isn't a height (AO, gloss, roughness ...)."""
    return bool(MAP_NAME.search(Path(name).stem))


def _is_grey(channels: np.ndarray) -> bool:
    """channels: (h, w, 3+) floats in 0..1, the colour channels all the same (within GREY_TOLERANCE)."""
    rgb = channels[..., :3]
    return bool(np.abs(rgb[..., 0] - rgb[..., 1]).max() <= GREY_TOLERANCE
                and np.abs(rgb[..., 1] - rgb[..., 2]).max() <= GREY_TOLERANCE)


def _shrink(pixels: np.ndarray, max_side: int) -> np.ndarray:
    """At most max_side on the longest side, as float32: whole blocks averaged (after skipping rows
    and columns of a huge image first, so a 23,000 px TIF isn't averaged at full size), then a
    height map resized the rest of the way (still in floats: no precision lost)."""
    longest = max(pixels.shape[:2])
    skip = max(1, longest // (max_side * 4))
    if skip > 1:
        pixels = pixels[::skip, ::skip]
    factor = max(1, max(pixels.shape[:2]) // max_side)  # (leaves it between 1 and 2 x max_side)
    if factor > 1:
        h, w = (pixels.shape[0] // factor) * factor, (pixels.shape[1] // factor) * factor
        blocks = pixels[:h, :w].reshape(h // factor, factor, w // factor, factor, *pixels.shape[2:])
        pixels = blocks.mean(axis=(1, 3), dtype=np.float32)
    pixels = pixels.astype(np.float32, copy=False)
    longest = max(pixels.shape[:2])
    if pixels.ndim == 2 and longest > max_side:
        scale = max_side / longest
        size = (max(1, round(pixels.shape[1] * scale)), max(1, round(pixels.shape[0] * scale)))
        pixels = np.asarray(Image.fromarray(pixels).resize(size, Image.Resampling.BOX), dtype=np.float32)  # (mode F)
    return pixels


def read_height(source: Source, name: str, max_side: int) -> Optional[np.ndarray]:
    """The image's heights (h, w) as float32 0..1, at most max_side on its longest side, if it's an
    alpha; None if it isn't. source: a file, or (not for PSD / EXR, whose readers want a file)
    its bytes as a file object. Raises if it can't be read."""
    if named_like_map(name):
        return None
    ext = Path(name).suffix.lower()
    if ext in NOT_IMAGES:
        return None
    if ext in (".psd", ".psb"):
        return _psd_height(source, max_side)
    if ext == ".exr":
        return _exr_height(source, max_side)
    with Image.open(source) as im:
        mode = im.mode
        if mode.startswith("I;16"):
            return _shrink(np.asarray(im), max_side) / 65535.0
        if mode in ("I", "F"):
            return _normalized(_shrink(np.asarray(im), max_side))
        if mode in ("1", "L", "LA"):
            return _shrink(np.asarray(im.convert("L")), max_side) / 255.0
        if mode not in ("RGB", "RGBA", "P", "RGBX"):
            return None  # (CMYK, YCbCr ...)
        im.draft("RGB", (max_side, max_side))  # (a JPEG decodes at a fraction of its size)
        rgb = _shrink(np.asarray(im.convert("RGB")), max_side) / 255.0
    return _shrink(rgb[..., 0], max_side) if _is_grey(rgb) else None


def _normalized(height: np.ndarray) -> np.ndarray:
    low, high = float(height.min()), float(height.max())
    return (height - low) / (high - low) if high > low else np.zeros_like(height)


def _psd_height(source: Source, max_side: int) -> Optional[np.ndarray]:
    from .psd_reader import read_psd_planes

    planes, depth, color_mode, base_channels = read_psd_planes(source, max_size=max_side)
    scale = {8: 255.0, 16: 65535.0, 32: 1.0}[depth]
    if color_mode in (1, 8):  # grayscale, duotone
        return np.clip(_shrink(planes[0], max_side) / scale, 0.0, 1.0)
    if color_mode != 3:
        return None
    rgb = np.clip(_shrink(np.moveaxis(planes[:3], 0, -1), max_side) / scale, 0.0, 1.0)
    return _shrink(rgb[..., 0], max_side) if _is_grey(rgb) else None


def _exr_height(source: Source, max_side: int) -> Optional[np.ndarray]:
    from .hdr_image import _read_exr

    pixels = np.asarray(_read_exr(Path(source)), dtype=np.float32)
    if pixels.ndim == 3:
        small = _shrink(pixels, max_side)
        spread = float(np.abs(small).max()) or 1.0
        if not _is_grey(small / spread):
            return None
        return _normalized(_shrink(small[..., 0], max_side))
    return _normalized(_shrink(pixels, max_side))


def detect(source: Source, name: str) -> bool:
    """Whether the image is an alpha, as cheaply as possible: from its header when it has one
    channel (a PSD's or a PNG's), else from a small decoded copy. False if it can't be read."""
    if named_like_map(name) or Path(name).suffix.lower() in NOT_IMAGES:
        return False
    ext = Path(name).suffix.lower()
    try:
        if ext in (".psd", ".psb"):
            header = _psd_header(source)
            if header is not None and header[1] in (1, 8):
                return True
        elif ext != ".exr":
            with Image.open(source) as im:
                if im.mode in SINGLE_CHANNEL_MODES:
                    return True
                if im.mode not in ("RGB", "RGBA", "P", "RGBX"):
                    return False
            if hasattr(source, "seek"):
                source.seek(0)
        return read_height(source, name, SAMPLE_SIDE) is not None
    except Exception:  # pylint: disable=broad-except
        return False


def _psd_header(source: Source) -> Optional[tuple[int, int]]:
    """(bit depth, colour mode) from a PSD's header."""
    import struct

    if hasattr(source, "read"):
        head = source.read(26)
        source.seek(0)
    else:
        with open(source, "rb") as f:
            head = f.read(26)
    if len(head) < 26 or head[:4] != b"8BPS":
        return None
    depth, mode = struct.unpack(">HH", head[22:26])
    return depth, mode


def detect_bytes(data: bytes, name: str) -> bool:
    return detect(io.BytesIO(data), name)


# ----------------------------------------------------------------------
# Drawing them
# ----------------------------------------------------------------------
def relief(height: np.ndarray, depth: float = RELIEF_DEPTH) -> np.ndarray:
    """The heights as a lit relief: (h, w, 3) uint8, clay grey, light from the top left. depth: how
    deep the relief is, as a share of the longest side (the same look at any size)."""
    scale = depth * max(height.shape)
    if min(height.shape) >= 2:
        gy, gx = np.gradient(height.astype(np.float32) * scale)
    else:
        gy = gx = np.zeros_like(height, dtype=np.float32)
    normal = np.stack([-gx, -gy, np.ones_like(gx)], axis=-1)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    lit = np.clip(normal @ LIGHT.astype(np.float32), 0.0, 1.0)
    shade = 0.32 + 0.68 * lit  # (some light everywhere: shadowed sides keep their detail)
    color = shade[..., None] * CLAY.astype(np.float32)
    return np.clip(color * 255.0 + 0.5, 0, 255).astype(np.uint8)


def relief_image(height: np.ndarray) -> Image.Image:
    return Image.fromarray(relief(height))  # (h, w, 3 uint8: RGB)


def height_mesh(height: np.ndarray, strength: float = 0.15, invert: bool = False, name: str = "alpha"):
    """The heights as a displaced plane (a Mesh3D for the 3D viewer): the image lies flat, its top
    edge away from the viewer, raised by strength x its longest side where it's white (black,
    inverted). Normals come from the heights' slopes."""
    from .model_loader import mesh_from_arrays

    h = (1.0 - height if invert else height).astype(np.float32)
    rows, cols = h.shape
    side = float(max(rows, cols))
    ys = h * np.float32(strength)
    xs = (np.arange(cols, dtype=np.float32) - (cols - 1) / 2) / side
    zs = (np.arange(rows, dtype=np.float32) - (rows - 1) / 2) / side
    grid_x, grid_z = np.meshgrid(xs, zs)
    vertices = np.stack([grid_x, ys, grid_z], axis=-1).reshape(-1, 3)

    first = (np.arange(rows - 1)[:, None] * cols + np.arange(cols - 1)[None, :]).ravel().astype(np.uint32)
    faces = np.concatenate([
        np.stack([first, first + cols, first + 1], axis=1),  # (wound to face up, +y)
        np.stack([first + 1, first + cols, first + cols + 1], axis=1),
    ])
    slope_z, slope_x = np.gradient(ys, 1.0 / side)
    normals = np.stack([-slope_x, np.ones_like(ys), -slope_z], axis=-1).reshape(-1, 3)
    normals /= np.linalg.norm(normals, axis=1, keepdims=True)
    return mesh_from_arrays(name, vertices, faces, normals.astype(np.float32), "Alpha (3D preview)", {
        "Plane": f"{cols} × {rows} points",
    })
