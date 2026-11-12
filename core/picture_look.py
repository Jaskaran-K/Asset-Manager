"""What a picture looks like, for searching by colour (color:red) and finding look-alikes (Find Similar).

Read once per image, in the background after its other details (ui.library_state), and kept in the
database until the file changes:

- colours: its 5 main colours and the share of the picture each covers (transparent parts left out).
- hash: a 63-bit perceptual hash (the low frequencies of a 32 x 32 grey copy, as in "pHash"). Resizing,
  re-saving and small edits barely change it.
- grey: a 16 x 16 grey copy, contrast stretched. Two pictures whose hashes are close also need these to
  agree (MAX_GREY_DISTANCE), which drops the hash's typical mistake: two different mostly-black alphas
  with a thin white shape in the middle.

Measured on 148 of the user's pictures and edited copies of them: found as look-alikes were 100% of the
half-size copies, 97% of low-quality 8-bit JPEG re-saves, 99% of 15% brighter ones and 90% of 5% crops;
of 1,454 unrelated pictures, 0.064% matched (often relatives anyway: frames of one turntable, alphas of
one pack).

A picture of one flat colour (a blank normal map) has no hash or grey copy: there's nothing to compare.
16-bit greys (displacement alphas) keep their precision, so a nearly flat one still has its shapes.
"""

from __future__ import annotations

import colorsys
import contextlib
import io
import math
import os
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

import numpy as np
from PIL import Image

SIDE = 256  # pictures are read at about this size (at least this on the longest side, then averaged)
COLOR_SIDE = 64  # the main colours are found on a copy this big
MAIN_COLORS = 5
FLAT_RANGE = 0.02  # the grey spread (1st to 99th percentile, 0..1) below which an 8-bit picture is flat
FLAT_RANGE_PRECISE = 0.001  # ... and a 16 or 32-bit one (about 65 of 65,536 levels: a gentle displacement isn't flat)
MIN_SHARE = 0.02  # main colours covering less than this are left out (edges between colours)
MAX_HASH_DISTANCE = 10  # bits that may differ between look-alikes (of 63)
MAX_GREY_DISTANCE = 0.15  # 1 - correlation of their 16 x 16 grey copies
MAX_MEMBER_BYTES = 100 * 1024 * 1024  # an image inside an archive is read up to this size
# Big pictures are read one at a time (two 8K TGAs or PSDs side by side took 2 GB): over this many pixels,
# or a file this big. Small ones carry on beside them.
HEAVY_PIXELS = 32_000_000
HEAVY_FILE_BYTES = 64 * 1024 * 1024
MAX_EXR_BYTES = 300 * 1024 * 1024  # EXRs are read whole, every layer (an 813 MB one took 2.9 GB): bigger are skipped
_HEAVY = threading.Lock()
COLOR_SHARE = 0.15  # color:x matches when that colour covers this much of the picture
COLOR_DISTANCE = 18.0  # color:#hex: how close (CIE Lab, delta E) a main colour must be
LOOK_VERSION = 1  # part of the key the look is stored under: raising it reads every picture again

COLOR_NAMES = ("red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink", "brown", "skin",
               "black", "white", "grey")
_NAME_ALIASES = {"gray": "grey", "violet": "purple", "teal": "cyan", "magenta": "pink", "tan": "skin"}
MID_GREY = 0.5


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)
    matrix = np.sqrt(2 / n) * np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
    matrix[0] /= np.sqrt(2)
    return matrix


_DCT = _dct_matrix(32)


# ----------------------------------------------------------------------
# A picture's look
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Look:
    """colors: ((r, g, b), share 0..1) biggest first; hash / grey: None for a flat picture."""

    colors: tuple[tuple[tuple[int, int, int], float], ...] = ()
    hash: Optional[int] = None
    grey: Optional[bytes] = None
    names: Mapping[str, float] = field(default_factory=dict, compare=False, repr=False)  # colour name: share
    labs: tuple[tuple[float, float, float], ...] = field(default=(), compare=False, repr=False)
    normal_map: bool = field(default=False, compare=False)

    @staticmethod
    def make(colors: Sequence[tuple[tuple[int, int, int], float]], hash_: Optional[int], grey: Optional[bytes]) -> "Look":
        """A look with what searching needs worked out (colour names, Lab colours)."""
        colors = tuple((tuple(int(c) for c in rgb), float(share)) for rgb, share in colors)
        names: dict[str, float] = {}
        for rgb, share in colors:
            name = color_name(rgb)
            names[name] = names.get(name, 0.0) + share
        return Look(colors, hash_, grey, names, tuple(_lab(rgb) for rgb, _share in colors), _looks_like_normal_map(colors))

    @property
    def comparable(self) -> bool:
        return self.hash is not None and self.grey is not None

    def matches_color(self, want: "ColorWant") -> bool:
        return want.matches(self)

    # Stored in the database (core.tag_repository): "rrggbb:share%,..."
    def colors_text(self) -> str:
        return ",".join(f"{r:02x}{g:02x}{b:02x}:{round(share * 100)}" for (r, g, b), share in self.colors)

    @staticmethod
    def from_stored(colors_text: Optional[str], hash_: Optional[int], grey: Optional[bytes]) -> "Look":
        colors = []
        for part in (colors_text or "").split(","):
            hex_color, _sep, share = part.partition(":")
            if len(hex_color) == 6:
                try:
                    colors.append(((int(hex_color[:2], 16), int(hex_color[2:4], 16), int(hex_color[4:], 16)), int(share) / 100))
                except ValueError:
                    continue
        return Look.make(colors, hash_, bytes(grey) if grey is not None else None)


UNREADABLE = Look()  # read, but nothing could be made of it (stored, so it isn't tried again)


# ----------------------------------------------------------------------
# Colours
# ----------------------------------------------------------------------
def color_name(rgb: Sequence[int]) -> str:
    """The everyday name of a colour (COLOR_NAMES). Tuned on the user's textures and references:
    skin tones are "skin", not red or orange; dark oranges and yellows are brown."""
    r, g, b = (c / 255 for c in rgb)
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    if light < 0.12:
        return "black"
    if light > 0.97 or (light > 0.9 and sat < 0.5):
        return "white"
    if sat < 0.15 or (sat < 0.25 and (light < 0.2 or light > 0.85)):
        return "grey"
    deg = hue * 360
    if 5 <= deg < 32 and 0.38 <= light <= 0.85 and 0.22 <= sat <= 0.75:
        return "skin"
    if deg < 12 or deg >= 345:
        return "brown" if light < 0.3 and sat < 0.6 else "red"
    if deg < 42:
        return "brown" if light < 0.42 else "orange"
    if deg < 68:
        return "yellow" if light >= 0.3 else "brown"
    if deg < 165:
        return "green"
    if deg < 195:
        return "cyan"
    if deg < 255:
        return "blue"
    if deg < 290:
        return "purple"
    return "pink"


def _looks_like_normal_map(colors: Sequence[tuple[tuple[int, int, int], float]]) -> bool:
    """Tangent-space normal maps are mostly lavender (about 128, 128, 255): left out of colour names,
    or color:blue would be mostly normal maps."""
    lavender = sum(share for (r, g, b), share in colors if 80 <= r <= 190 and 80 <= g <= 190 and b >= 190 and b - max(r, g) >= 35)
    return lavender >= 0.6


def _lab(rgb: Sequence[int]) -> tuple[float, float, float]:
    def linear(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


@dataclass(frozen=True)
class ColorWant:
    """What color:... asks for: a name (COLOR_NAMES), or an exact colour (its Lab)."""

    name: Optional[str] = None
    lab: Optional[tuple[float, float, float]] = None
    text: str = ""

    def share_in(self, look: Look) -> float:
        """How much of the picture is this colour."""
        if self.name is not None:
            return 0.0 if look.normal_map else look.names.get(self.name, 0.0)
        return sum(share for (_rgb, share), lab in zip(look.colors, look.labs) if math.dist(lab, self.lab) <= COLOR_DISTANCE)

    def matches(self, look: Look) -> bool:
        return self.share_in(look) >= COLOR_SHARE


def parse_color(text: str) -> ColorWant:
    """color:red, color:#c04020, color:c04020, color:#c42. Raises ValueError."""
    word = text.strip().lower()
    word = _NAME_ALIASES.get(word, word)
    if word in COLOR_NAMES:
        return ColorWant(name=word, text=word)
    hex_text = word.lstrip("#")
    if len(hex_text) == 3:
        hex_text = "".join(c * 2 for c in hex_text)
    if len(hex_text) == 6 and all(c in "0123456789abcdef" for c in hex_text):
        rgb = (int(hex_text[:2], 16), int(hex_text[2:4], 16), int(hex_text[4:], 16))
        return ColorWant(lab=_lab(rgb), text=f"#{hex_text}")
    raise ValueError(text)


def hex_of(rgb: Sequence[int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


# ----------------------------------------------------------------------
# Look-alikes
# ----------------------------------------------------------------------
def _grey_vector(grey: bytes) -> np.ndarray:
    vector = np.frombuffer(grey, dtype=np.uint8).astype(np.float64)
    vector -= vector.mean()
    norm = np.linalg.norm(vector)
    return vector / norm if norm > 1e-9 else vector


def grey_distance(a: bytes, b: bytes) -> float:
    """1 - the correlation of two grey copies: 0 for the same picture, about 1 for unrelated ones."""
    return 1.0 - float(_grey_vector(a) @ _grey_vector(b))


def closeness(a: Look, b: Look) -> Optional[float]:
    """How far apart two pictures look (smaller is closer), or None if they aren't look-alikes."""
    if not (a.comparable and b.comparable):
        return None
    bits = (a.hash ^ b.hash).bit_count()
    if bits > MAX_HASH_DISTANCE:
        return None
    grey = grey_distance(a.grey, b.grey)
    if grey > MAX_GREY_DISTANCE:
        return None
    return bits + grey * 20


def find_similar(target: Look, looks: Mapping[int, Look], exclude: int = -1) -> dict[int, float]:
    """{id: closeness} of the pictures that look like target (not exclude, its own id)."""
    if not target.comparable:
        return {}
    found: dict[int, float] = {}
    wanted = target.hash
    for asset_id, look in looks.items():
        h = look.hash
        if h is None or asset_id == exclude or (h ^ wanted).bit_count() > MAX_HASH_DISTANCE:
            continue
        score = closeness(target, look)
        if score is not None:
            found[asset_id] = score
    return found


def group_lookalikes(looks: Mapping[int, Look], cancelled=lambda: False) -> list[list[int]]:
    """Groups of look-alike pictures (ids, the group's first picture first, then closest first). Each
    picture is in one group at most, and every picture in a group looks like its first one (not only
    like a neighbour: chained, a turntable's 150 frames became one group)."""
    ids = [i for i, look in looks.items() if look.comparable]
    if len(ids) < 2:
        return []
    hashes = np.array([looks[i].hash for i in ids], dtype=np.uint64)
    left = np.ones(len(ids), dtype=bool)
    groups = []
    for n, asset_id in enumerate(ids):
        if not left[n]:
            continue
        if n % 256 == 0 and cancelled():
            return []
        near = np.nonzero(left & (np.bitwise_count(hashes ^ hashes[n]) <= MAX_HASH_DISTANCE))[0]
        if len(near) < 2:
            continue
        target = looks[asset_id]
        members = []
        for m in near:
            if m == n:
                continue
            score = closeness(target, looks[ids[m]])
            if score is not None:
                members.append((score, int(m)))
        if members:
            members.sort()
            left[n] = False
            left[[m for _score, m in members]] = False
            groups.append([asset_id] + [ids[m] for _score, m in members])
    return groups


# ----------------------------------------------------------------------
# Reading a picture
# ----------------------------------------------------------------------
def _shrink(pixels: np.ndarray, side: int = SIDE) -> np.ndarray:
    from .alphas import _shrink as shrink

    return shrink(pixels, side)


Pixels = tuple[np.ndarray, Optional[np.ndarray], bool]  # (rgb, opacity, precise: more than 8 bits)


def _from_pil(im: Image.Image) -> Pixels:
    """(rgb float 0..1 (h, w, 3), opacity 0..1 (h, w) or None, precise) at about SIDE, precision kept."""
    if im.format == "JPEG":
        im.draft("RGB", (SIDE, SIDE))  # (decoded at a fraction of its size)
    elif max(im.size) > SIDE * 4:
        try:  # whole blocks averaged by Pillow before any copy is made (a big 16-bit PNG took 1 GB as floats)
            im = im.reduce(max(im.size) // (SIDE * 2))
        except (ValueError, NotImplementedError):
            pass  # (modes reduce doesn't do: _shrink skips rows and columns instead)
    mode = im.mode
    if mode.startswith("I;16"):
        grey = _shrink(np.asarray(im)) / 65535.0
        return np.repeat(grey[..., None], 3, axis=2), None, True
    if mode in ("I", "F"):  # (a 16-bit grey PNG opens as "I")
        grey = _shrink(np.asarray(im, dtype=np.float32))
        top = float(grey.max())
        if mode == "F":
            scale = 1.0 if top <= 1.0 else top
        else:
            scale = 255.0 if top <= 255 else (65535.0 if top <= 65535 else top)
        grey = np.clip(grey / scale, 0.0, 1.0)
        return np.repeat(grey[..., None], 3, axis=2), None, scale != 255.0
    if mode in ("1", "L"):
        grey = _shrink(np.asarray(im.convert("L"))) / 255.0
        return np.repeat(grey[..., None], 3, axis=2), None, False
    if mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA") if mode in ("LA", "PA", "P", "RGBa", "La") or "transparency" in im.info else im.convert("RGB")
    pixels = _shrink(np.asarray(im)) / 255.0
    if pixels.shape[2] == 4:
        return pixels[..., :3], pixels[..., 3], False
    return pixels[..., :3], None, False


def _from_psd(source) -> Pixels:
    from .psd_reader import read_psd_composite, read_psd_planes

    try:
        planes, depth, color_mode, _base = read_psd_planes(source, max_size=SIDE)
    except Exception:  # pylint: disable=broad-except
        with read_psd_composite(source, max_size=SIDE) as im:
            return _from_pil(im)
    scale = {8: 255.0, 16: 65535.0, 32: 1.0}.get(depth, 255.0)
    if color_mode in (1, 8):  # grayscale, duotone
        grey = np.clip(_shrink(planes[0]) / scale, 0.0, 1.0)
        return np.repeat(grey[..., None], 3, axis=2), None, depth > 8
    if color_mode == 3 and len(planes) >= 3:
        return np.clip(_shrink(np.moveaxis(np.asarray(planes[:3]), 0, -1)) / scale, 0.0, 1.0), None, depth > 8
    with read_psd_composite(source, max_size=SIDE) as im:  # (CMYK ...)
        return _from_pil(im)


def _from_file(path: Path | str, name: str) -> Pixels:
    ext = Path(name).suffix.lower()
    if ext in (".psd", ".psb"):
        return _from_psd(path)
    if ext in (".exr", ".hdr"):
        from .hdr_image import read_hdr_image

        with read_hdr_image(path, max_size=SIDE) as im:
            return _from_pil(im)
    with Image.open(path) as im:
        return _from_pil(im)


def look_of_pixels(rgb: np.ndarray, opacity: Optional[np.ndarray] = None, precise: bool = False) -> Look:
    """The look of a picture: rgb float 0..1 (h, w, 3), opacity (h, w) or None; precise: read with more
    than 8 bits (a smaller spread of greys still has shapes)."""
    rgb = np.asarray(rgb, dtype=np.float32)
    if opacity is not None:
        opacity = np.asarray(opacity, dtype=np.float32)[..., None]
        on_grey = rgb * opacity + MID_GREY * (1 - opacity)  # (as the grid shows it: on a mid grey)
    else:
        on_grey = rgb
    grey = on_grey @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    low, high = np.percentile(grey, (1, 99))
    hash_value: Optional[int] = None
    grey_copy: Optional[bytes] = None
    if high - low >= (FLAT_RANGE_PRECISE if precise else FLAT_RANGE):
        square = np.asarray(Image.fromarray(np.ascontiguousarray(grey, dtype=np.float32), "F").resize((32, 32), Image.Resampling.BOX), dtype=np.float64)
        low_freq = (_DCT @ square @ _DCT.T)[:8, :8].flatten()[1:]  # (63: the overall brightness left out)
        median = np.median(low_freq)
        hash_value = sum(1 << i for i, v in enumerate(low_freq) if v > median)
        small = np.asarray(Image.fromarray(np.ascontiguousarray(grey, dtype=np.float32), "F").resize((16, 16), Image.Resampling.BOX))
        s_low, s_high = float(small.min()), float(small.max())
        stretched = (small - s_low) / (s_high - s_low) if s_high > s_low else small * 0
        grey_copy = np.clip(np.round(stretched * 255), 0, 255).astype(np.uint8).tobytes()
    return Look.make(_main_colors(rgb, opacity), hash_value, grey_copy)


def _main_colors(rgb: np.ndarray, opacity: Optional[np.ndarray]) -> list[tuple[tuple[int, int, int], float]]:
    """The MAIN_COLORS main colours and their shares, from the opaque part of the picture."""
    rgb8 = np.clip(np.round(rgb * 255), 0, 255).astype(np.uint8)
    picture = Image.fromarray(rgb8, "RGB")
    if max(picture.size) > COLOR_SIDE:
        picture.thumbnail((COLOR_SIDE, COLOR_SIDE), Image.Resampling.BOX)
    pixels = np.asarray(picture).reshape(-1, 3)
    if opacity is not None:
        mask = np.asarray(Image.fromarray(np.ascontiguousarray(opacity[..., 0] if opacity.ndim == 3 else opacity, dtype=np.float32), "F")
                          .resize(picture.size, Image.Resampling.BOX)).reshape(-1) >= 0.5
        if mask.any():
            pixels = pixels[mask]
    if len(pixels) == 0:
        return []
    strip = Image.fromarray(np.ascontiguousarray(pixels.reshape(1, -1, 3)), "RGB")
    quantized = strip.quantize(colors=MAIN_COLORS, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    counts = sorted(quantized.getcolors() or [], reverse=True)
    total = sum(count for count, _index in counts) or 1
    shares = [(tuple(palette[i * 3:i * 3 + 3]), count / total) for count, i in counts[:MAIN_COLORS]]
    return [(rgb, share) for rgb, share in shares if share >= MIN_SHARE]


def _pixel_count(source, name: str) -> int:
    """Width x height from the file's header (0 if unknown): nothing is decoded."""
    ext = Path(name).suffix.lower()
    try:
        if ext in (".psd", ".psb"):
            if isinstance(source, (bytes, bytearray)):
                head = bytes(source[:26])
            else:
                with open(source, "rb") as f:
                    head = f.read(26)
            height, width = struct.unpack(">II", head[14:22])
            return width * height
        if ext in (".exr", ".hdr"):
            return 0
        with Image.open(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source) as im:
            return im.size[0] * im.size[1]
    except Exception:  # pylint: disable=broad-except
        return 0


def _one_at_a_time(heavy: bool):
    return _HEAVY if heavy else contextlib.nullcontext()


def read_look(path: str) -> Look:
    """The look of an image file, or of an image inside an archive ("archive|member"). UNREADABLE if
    it can't be read. Blocks: run it on a background thread (big pictures wait for each other)."""
    from .archives import SEPARATOR

    try:
        if SEPARATOR in path:
            return _read_member(path)
        size = os.path.getsize(path)
        if path.lower().endswith(".exr") and size > MAX_EXR_BYTES:
            return UNREADABLE
        with _one_at_a_time(size > HEAVY_FILE_BYTES or _pixel_count(path, path) > HEAVY_PIXELS):
            return look_of_pixels(*_from_file(path, path))
    except Exception:  # pylint: disable=broad-except  (damaged, too big (decompression bomb), unknown kind)
        return UNREADABLE


def _read_member(path: str) -> Look:
    from .archives import local_file, member_for_path, read_bytes

    member = member_for_path(path)
    if member is None or member.size > MAX_MEMBER_BYTES:
        return UNREADABLE
    heavy = member.size > HEAVY_FILE_BYTES
    if member.base_name.lower().endswith((".psd", ".psb", ".exr", ".hdr")):  # (their readers want a file)
        with local_file(member) as copy:
            with _one_at_a_time(heavy or _pixel_count(copy, member.base_name) > HEAVY_PIXELS):
                return look_of_pixels(*_from_file(copy, member.base_name))
    data = read_bytes(member, limit=MAX_MEMBER_BYTES)
    with _one_at_a_time(heavy or _pixel_count(data, member.base_name) > HEAVY_PIXELS):
        with Image.open(io.BytesIO(data)) as im:
            return look_of_pixels(*_from_pil(im))


def needs_look(extension: str) -> bool:
    """Whether files of this type get a look (images)."""
    from .scanner import TEXTURE_EXTENSIONS

    return extension.lower() in TEXTURE_EXTENSIONS
