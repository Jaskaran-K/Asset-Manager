"""HDRIs as lighting for the 3D viewer: a panorama read in full and turned into what its shader needs.

The panorama becomes a floating-point texture (for reflections and the backdrop), up to three
directional lights (its sun or brightest lamps) and nine spherical-harmonic numbers per channel
for the soft light of all the rest. The brightest sources have to come out as real lights: left in
the spherical harmonics, a sun that holds 90% of the light in under 1% of the pixels shows up as
false dark and coloured bands across the model, which also looks flat.

Directions here are in the panorama's space: y up, and the middle of the image straight ahead
along -z. Everything blocks (a read takes up to a second): call it on a worker thread.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Callable, Iterable, Optional

import numpy as np

from . import archives, hdr_image

TEXTURE_WIDTH = 2048  # (wider only sharpens the backdrop: lights and reflections don't gain from it)
LIGHT_WIDTH = 256  # the small copy that lights and spherical harmonics are worked out from
MAX_LIGHTS = 3
MIN_PANORAMA_WIDTH = 512  # (renderers leave 256 x 128 thumbnails of their skies next to the real ones)
BRIGHT = 25.0  # above this many times the average brightness, light counts as a source's
LIGHT_REACH = math.radians(30.0)  # one source: all the bright light this close to its brightest point
MIN_LIGHT_SHARE = 0.03  # of the whole panorama's light: fainter sources stay in the soft light
KEY_LIGHT_AZIMUTH = 40.0  # an HDRI starts with its strongest light this far (degrees) to the right of the camera
MAX_VALUE = 60000.0  # the texture's half floats end at 65504
KEPT = 8  # panoramas kept in memory (about 13 MB each), so going back to one is instant

_LUMINANCE = np.array([0.2126, 0.7152, 0.0722])
_BAND = np.array([np.pi, *[2.094395] * 3, *[0.785398] * 5])  # what the cosine lobe keeps of each harmonic


class NotAPanorama(ValueError):
    """The image can't light a scene (the message says why, for the user)."""


@dataclass(frozen=True)
class EnvLight:
    direction: tuple[float, float, float]  # toward the light
    color: tuple[float, float, float]  # what a white surface facing it shows


@dataclass(frozen=True)
class Environment:
    key: str  # its path as given to load (a file inside an archive: its virtual path)
    name: str
    pixels: np.ndarray  # H x W x 3 float16, linear, the top row first
    harmonics: np.ndarray  # 9 x 3 float32: all the light but the lights'
    lights: tuple[EnvLight, ...]
    exposure: float  # brings its average light to a mid grey


@dataclass(frozen=True)
class Panorama:
    """An HDRI that can be picked."""

    key: str
    name: str
    width: int
    height: int


# ----------------------------------------------------------------------
# The sums
# ----------------------------------------------------------------------
def area_average(pixels: np.ndarray, width: int) -> np.ndarray:
    """The image shrunk to width x width / 2, every pixel the average of those it covers, so the sun
    keeps its light. (Never made bigger: a smaller image stays as it is.)"""
    height = width // 2
    if pixels.shape[1] <= width:
        return pixels
    return _average_axis(_average_axis(pixels, height, 0), width, 1)


def _average_axis(pixels: np.ndarray, count: int, axis: int) -> np.ndarray:
    size = pixels.shape[axis]
    if size <= count:
        return pixels
    factor, remainder = divmod(size, count)
    if remainder == 0:  # (nearly every HDRI, and seven times as quick)
        return pixels.reshape(*pixels.shape[:axis], count, factor, *pixels.shape[axis + 1:]).mean(axis=axis + 1)
    edges =np.round(np.linspace(0, size, count + 1)).astype(np.int64)
    shape = [1, 1, 1]
    shape[axis] = count
    return np.add.reduceat(pixels, edges[:-1], axis=axis) / np.diff(edges).reshape(shape).astype(np.float32)


def directions(height: int, width: int) -> tuple[np.ndarray, np.ndarray]:
    """For a panorama's pixels: (unit direction H x W x 3, solid angle H x W)."""
    theta = (np.arange(height) + 0.5) / height * np.pi
    phi = (np.arange(width) + 0.5) / width * 2 * np.pi - np.pi
    sin_t, cos_t = np.sin(theta)[:, None], np.cos(theta)[:, None]
    d = np.stack([sin_t * np.sin(phi)[None, :], np.repeat(cos_t, width, axis=1), -sin_t * np.cos(phi)[None, :]], axis=-1)
    return d, sin_t * (np.pi / height) * (2 * np.pi / width) * np.ones((1, width))


def _basis(d: np.ndarray) -> np.ndarray:
    """The nine spherical harmonics, in the order the viewer's shader sums them."""
    x, y, z = d[..., 0], d[..., 1], d[..., 2]
    return np.stack([np.full_like(x, 0.282095), 0.488603 * y, 0.488603 * z, 0.488603 * x, 1.092548 * x * y, 1.092548 * y * z,
                     0.315392 * (3 * z * z - 1), 1.092548 * x * z, 0.546274 * (x * x - y * y)], axis=-1)


def project_harmonics(pixels: np.ndarray) -> np.ndarray:
    """The nine numbers per channel (9 x 3) that hold a panorama's soft light."""
    d, solid = directions(*pixels.shape[:2])
    return (_basis(d)[..., None] * pixels[..., None, :] * solid[..., None, None]).sum(axis=(0, 1))


def irradiance(harmonics: np.ndarray, normals: np.ndarray) -> np.ndarray:
    """The light reaching surfaces with these normals (the same sum as the viewer's shader)."""
    return (_basis(normals)[..., None] * (harmonics * _BAND[:, None])).sum(axis=-2)


def extract_lights(pixels: np.ndarray) -> tuple[tuple[EnvLight, ...], np.ndarray]:
    """(the panorama's brightest sources as directional lights, the panorama without them). Only
    the light above BRIGHT is taken, so what stays has no holes, and no light is lost."""
    pixels = np.asarray(pixels, dtype=np.float64)
    d, solid = directions(*pixels.shape[:2])
    luminance = pixels @ _LUMINANCE
    whole = float((luminance * solid).sum())
    threshold = BRIGHT * whole / float(solid.sum())
    over = np.where(luminance > threshold, 1.0 - threshold / np.maximum(luminance, 1e-12), 0.0)
    excess = pixels * over[..., None]
    rest = pixels - excess
    left = (excess @ _LUMINANCE) * solid  # the light not yet in a light
    lights: list[EnvLight] = []
    while len(lights) < MAX_LIGHTS and whole > 0 and float(left.max()) > 0:
        seed = d[np.unravel_index(int(left.argmax()), left.shape)]
        near = (d @ seed > math.cos(LIGHT_REACH)) & (left > 0)
        if float(left[near].sum()) < MIN_LIGHT_SHARE * whole:
            break
        direction = (d[near] * left[near][:, None]).sum(axis=0)
        direction /= max(float(np.linalg.norm(direction)), 1e-12)
        color = (excess[near] * solid[near][:, None]).sum(axis=0) / np.pi
        lights.append(EnvLight(tuple(float(v) for v in direction), tuple(float(v) for v in color)))
        left[near] = 0.0
    rest[left > 0] += excess[left > 0]  # (what no light took stays soft light)
    return tuple(lights), rest


def auto_exposure(harmonics: np.ndarray, lights: Iterable[EnvLight]) -> float:
    """What the light is multiplied by to bring it to a mid grey. From the light a surface actually
    gets, taken over every direction it could face: dark studios and blinding suns come out alike."""
    soft = float(harmonics[0] @ _LUMINANCE) * 0.282095  # (the average of the soft light)
    direct = sum(float(np.dot(light.color, _LUMINANCE)) for light in lights) * 0.5  # (half a sphere faces each light)
    return 0.45 / max(soft + direct, 1e-4)


def reflection_level(blur: float, width: int) -> float:
    """Which mipmap level of a panorama this wide shows reflections this blurred (a material's
    reflection_blur, which is the level for a TEXTURE_WIDTH panorama)."""
    return max(0.0, blur + math.log2(width / TEXTURE_WIDTH))


def reflection_spread(level: float, width: int) -> float:
    """How far apart (as a tangent) to take looks at this mipmap level, to hide its square pixels:
    half a pixel of it, which is 2 ** level pixels of the panorama at its widest."""
    return math.pi * (2.0 ** level) / width


def key_light_yaw(environment: Environment, rot_y: float, azimuth: float = KEY_LIGHT_AZIMUTH) -> float:
    """How far to turn the panorama (degrees) to bring its strongest light this far round to the
    right of a camera turned rot_y. No light to speak of: it isn't turned."""
    if not environment.lights:
        return 0.0
    x, _y, z = environment.lights[0].direction
    return (azimuth - rot_y - math.degrees(math.atan2(x, z))) % 360.0


def _turn_y(degrees: float) -> np.ndarray:
    a = math.radians(degrees)
    return np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0], [-math.sin(a), 0, math.cos(a)]])


def _turn_x(degrees: float) -> np.ndarray:
    a = math.radians(degrees)
    return np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])


def view_to_env(rot_x: float, rot_y: float, yaw: float) -> np.ndarray:
    """Turns a direction in view space into the panorama's, for a camera turned rot_x, rot_y (the
    viewport's orbit) and a panorama turned yaw round the vertical. Its transpose goes the other way."""
    return _turn_y(yaw).T @ (_turn_x(rot_x) @ _turn_y(rot_y)).T


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------
def display_name(key: str) -> str:
    return PurePath(key.partition(archives.SEPARATOR)[2].replace("\\", "/") or key).stem


def build(pixels: np.ndarray, key: str = "", name: str = "") -> Environment:
    """An Environment from linear pixels (H x W x 3)."""
    texture = area_average(pixels, TEXTURE_WIDTH)
    lights, rest = extract_lights(area_average(texture, LIGHT_WIDTH))
    harmonics = project_harmonics(rest)
    return Environment(key, name or display_name(key), texture.clip(0, MAX_VALUE).astype(np.float16),
                       harmonics.astype(np.float32), lights, auto_exposure(harmonics, lights))


_kept: "OrderedDict[str, Environment]" = OrderedDict()
_kept_lock = threading.Lock()


def load(key: str, should_stop: Optional[Callable[[], bool]] = None) -> Environment:
    """The panorama at this path (a file inside an archive: its virtual path, unpacked if need be).
    Read once, then kept. Raises NotAPanorama, or OSError / ArchiveError if it can't be read."""
    with _kept_lock:
        known = _kept.get(key)
        if known is not None:
            _kept.move_to_end(key)
            return known
    path = _local_path(key, should_stop)
    extension = Path(path).suffix
    with open(path, "rb") as f:
        _require_panorama(display_name(key), hdr_image.header_info(f.read(HEADER_BYTES), extension))
    environment = build(hdr_image.read_linear(path), key)
    with _kept_lock:
        _kept[key] = environment
        while len(_kept) > KEPT:
            _kept.popitem(last=False)
    return environment


def _local_path(key: str, should_stop: Optional[Callable[[], bool]]) -> Path:
    if archives.SEPARATOR not in key:
        return Path(key)
    member = archives.member_for_path(key)
    if member is None:
        raise FileNotFoundError(f"{display_name(key)} is no longer in its archive")
    return archives.materialize(member, should_stop=should_stop)


def forget() -> None:
    with _kept_lock:
        _kept.clear()


# ----------------------------------------------------------------------
# Which images can be picked
# ----------------------------------------------------------------------
HEADER_BYTES = 256 * 1024
_sizes: dict[str, Optional[tuple[int, int]]] = {}  # what's known so far this session: most HDR files are renders and bakes


def _is_panorama(info: Optional[tuple[int, int, bool]]) -> bool:
    if info is None:
        return False
    width, height, colour = info
    return colour and width >= MIN_PANORAMA_WIDTH and abs(width / max(height, 1) - 2.0) < 0.02


def _require_panorama(name: str, info: Optional[tuple[int, int, bool]]) -> None:
    if info is None:
        raise NotAPanorama(f"{name} couldn't be read")
    if not _is_panorama(info):
        raise NotAPanorama(f"{name} isn't a colour 2:1 panorama, so it can't light a scene")


def _read_head(key: str) -> bytes:
    if archives.SEPARATOR not in key:
        with open(key, "rb") as f:
            return f.read(HEADER_BYTES)
    member = archives.member_for_path(key)
    if member is None:
        raise FileNotFoundError(key)
    chunks = archives.iter_chunks(member)
    try:
        return next(chunks, b"")  # (a chunk holds any header)
    finally:
        chunks.close()


def check(key: str) -> None:
    """Raises NotAPanorama if this image can't light a scene, from its header alone."""
    try:
        head = _read_head(key)
    except (OSError, archives.ArchiveError) as exc:
        raise NotAPanorama(f"{display_name(key)} couldn't be read") from exc
    _require_panorama(display_name(key), hdr_image.header_info(head, PurePath(key).suffix))


def _in_hidden_folder(key: str) -> bool:
    return any(part.startswith(".") and part not in (".", "..") for part in PurePath(key.replace(archives.SEPARATOR, "/")).parent.parts)


def find_panoramas(keys: Iterable[str], should_stop: Optional[Callable[[], bool]] = None) -> list[Panorama]:
    """The images among these that can light a scene, sorted by name. One HDRI saved as both .hdr
    and .exr is listed once."""
    found: dict[tuple[str, int, int], Panorama] = {}
    for key in keys:
        if should_stop is not None and should_stop():
            break
        if key not in _sizes:
            try:
                info = hdr_image.header_info(_read_head(key), PurePath(key).suffix)
            except (OSError, archives.ArchiveError, archives.Cancelled):
                info = None
            _sizes[key] = info[:2] if _is_panorama(info) and not _in_hidden_folder(key) else None
        size = _sizes[key]
        if size is None:
            continue
        name = display_name(key)
        same = (name.casefold(), *size)
        if same not in found or PurePath(key).suffix.lower() == ".hdr":  # (the quicker to read)
            found[same] = Panorama(key, name, *size)
    return sorted(found.values(), key=lambda p: (p.name.casefold(), -p.width))
