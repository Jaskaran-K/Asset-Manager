"""What the material preview (ui.material_view) shows: shapes with UVs and tangents, and a texture set's
maps read for the GPU.

Maps are read at up to PREVIEW_SIDE (a 7-map 4K set loads in about half a second on 4 threads): colour
and emissive maps as 8-bit sRGB with alpha, normal and packed maps as 8-bit RGB, single-channel maps
(roughness, metallic, AO, opacity, height) as 16-bit grey, so heights keep their gentle slopes. A UDIM
set shows its first tile.
"""

from __future__ import annotations

import contextlib
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ContextManager, Optional

import numpy as np
from PIL import Image

from .texture_sets import TextureMap, TextureSet

PREVIEW_SIDE = 2048
SHAPES = ("sphere", "cube", "plane")
SHAPE_NAMES = {"sphere": "Sphere", "cube": "Cube", "plane": "Plane"}
COLOR_ROLES = ("color", "emissive")  # sRGB
RGB_ROLES = ("normal", "packed")  # numbers in three channels


@dataclass
class Shape:
    positions: np.ndarray  # (n, 3) float32
    normals: np.ndarray  # (n, 3)
    uvs: np.ndarray  # (n, 2)
    tangents: np.ndarray  # (n, 4): direction of +u, and the handedness of +v in w
    indices: np.ndarray  # (m,) uint32, triangles


def _grid(columns: int, rows: int) -> np.ndarray:
    """Triangle indices of a (columns + 1) x (rows + 1) grid of points, row by row."""
    a = np.arange(rows)[:, None] * (columns + 1) + np.arange(columns)[None, :]
    b, c, d = a + 1, a + columns + 1, a + columns + 2
    return np.stack([a, c, b, b, c, d], axis=-1).reshape(-1).astype(np.uint32)


# How many mesh steps one copy of a map spans, per shape (the coarser way on the sphere: top to bottom). The
# height map is read blurred to that spacing: a point reading one sharp pixel of fine grain makes a spike.
SHAPE_STEPS = {"sphere": 256, "cube": 256, "plane": 512}
# How big one copy of a map is on each shape (the sphere: top to bottom, half way round). A height map's depth
# goes with it: the same bricks are as deep in proportion on every shape, and at any tiling.
SHAPE_SPAN = {"sphere": math.pi, "cube": 1.5, "plane": 2.0}


def height_level(map_side: int, tiling: int, shape: str) -> float:
    """The mipmap level the height map is read at on this shape: about one map pixel per mesh step."""
    pixels_per_step = map_side * max(1, tiling) / SHAPE_STEPS[shape]
    return max(0.0, math.log2(max(pixels_per_step, 1.0)))


def height_depth(strength: float, tiling: int, shape: str) -> float:
    """How far the height map's lowest and highest points are apart on the shape: strength is that depth
    as a share of one copy of the map (0.02: 2 cm for a map that covers 1 m)."""
    return strength * SHAPE_SPAN[shape] / max(1, tiling)


def sphere(columns: int = 2 * SHAPE_STEPS["sphere"], rows: int = SHAPE_STEPS["sphere"], radius: float = 1.0) -> Shape:
    """A UV sphere: u round it (one turn), v from the top to the bottom."""
    u = np.linspace(0.0, 1.0, columns + 1, dtype=np.float32)
    v = np.linspace(0.0, 1.0, rows + 1, dtype=np.float32)
    uu, vv = np.meshgrid(u, v)
    theta, phi = uu * 2 * math.pi, vv * math.pi
    normals = np.stack([-np.sin(phi) * np.sin(theta), np.cos(phi), -np.sin(phi) * np.cos(theta)], axis=-1)
    tangents = np.stack([-np.cos(theta), np.zeros_like(theta), np.sin(theta), np.ones_like(theta)], axis=-1)
    uvs = np.stack([uu, 1.0 - vv], axis=-1)  # (v up the picture: its top at the sphere's top)
    return Shape((normals * radius).reshape(-1, 3).astype(np.float32), normals.reshape(-1, 3).astype(np.float32),
                 uvs.reshape(-1, 2).astype(np.float32), tangents.reshape(-1, 4).astype(np.float32), _grid(columns, rows))


def plane(steps: int = SHAPE_STEPS["plane"], size: float = 2.0) -> Shape:
    """A square facing the camera (+z), the picture the right way up."""
    t = np.linspace(0.0, 1.0, steps + 1, dtype=np.float32)
    uu, vv = np.meshgrid(t, t)
    positions = np.stack([(uu - 0.5) * size, (0.5 - vv) * size, np.zeros_like(uu)], axis=-1)
    normals = np.broadcast_to(np.array([0, 0, 1], np.float32), positions.shape)
    tangents = np.broadcast_to(np.array([1, 0, 0, 1], np.float32), (*positions.shape[:2], 4))
    uvs = np.stack([uu, 1.0 - vv], axis=-1)
    return Shape(positions.reshape(-1, 3).copy(), normals.reshape(-1, 3).copy(), uvs.reshape(-1, 2).astype(np.float32),
                 tangents.reshape(-1, 4).copy(), _grid(steps, steps))


def cube(steps: int = SHAPE_STEPS["cube"], size: float = 1.5) -> Shape:
    """Six squares, each with the whole picture."""
    faces = (  # (normal, right, up)
        ((0, 0, 1), (1, 0, 0), (0, 1, 0)), ((0, 0, -1), (-1, 0, 0), (0, 1, 0)),
        ((1, 0, 0), (0, 0, -1), (0, 1, 0)), ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),
        ((0, 1, 0), (1, 0, 0), (0, 0, -1)), ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
    )
    t = np.linspace(0.0, 1.0, steps + 1, dtype=np.float32)
    uu, vv = np.meshgrid(t, t)
    parts = []
    for normal, right, up in faces:
        n, r, w = (np.array(x, np.float32) for x in (normal, right, up))
        positions = (n[None, None] * 0.5 + r * (uu[..., None] - 0.5) + w * (0.5 - vv[..., None])) * size
        parts.append((positions.reshape(-1, 3), np.broadcast_to(n, positions.shape).reshape(-1, 3),
                      np.stack([uu, 1.0 - vv], axis=-1).reshape(-1, 2),
                      np.broadcast_to(np.append(r, 1.0).astype(np.float32), (*positions.shape[:2], 4)).reshape(-1, 4)))
    count = (steps + 1) ** 2
    indices = np.concatenate([_grid(steps, steps) + i * count for i in range(6)]).astype(np.uint32)
    return Shape(*(np.ascontiguousarray(np.concatenate([p[k] for p in parts]), dtype=np.float32) for k in range(4)), indices)


def make_shape(name: str) -> Shape:
    return {"sphere": sphere, "cube": cube, "plane": plane}[name]()


# ----------------------------------------------------------------------
# Maps
# ----------------------------------------------------------------------
@dataclass
class LoadedMap:
    role: str
    file: str  # str(asset.path)
    pixels: np.ndarray  # (h, w, 4) uint8 for colour / RGB roles; (h, w) uint16 for one-channel ones
    has_alpha: bool = False  # a colour map with transparency in it (used as opacity when there's no opacity map)


def _fit(values: np.ndarray, side: int) -> np.ndarray:
    """At most side on the longest side (floats, Lanczos per channel)."""
    height, width = values.shape[:2]
    if max(height, width) <= side:
        return values
    scale = side / max(height, width)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    channels = values[..., None] if values.ndim == 2 else values
    resized = [np.asarray(Image.fromarray(np.ascontiguousarray(channels[..., c], dtype=np.float32), "F")
                          .resize(size, Image.Resampling.LANCZOS)) for c in range(channels.shape[2])]
    out = np.clip(np.stack(resized, axis=-1), 0.0, 1.0)
    return out[..., 0] if values.ndim == 2 else out


def spread_height(grey: np.ndarray) -> np.ndarray:
    """A height map stretched over 0..1 (its lowest and highest 0.5% clipped): a Megascans displacement uses
    0.47..0.53, a baked height map all of 0..1, and the height slider should mean the same for both."""
    sample = grey[::4, ::4] if grey.size > 1 << 18 else grey
    low, high = (float(v) for v in np.percentile(sample, (0.5, 99.5)))
    if high - low < 1e-4:
        return grey  # (flat)
    return np.clip((grey - low) / (high - low), 0.0, 1.0)


def read_map(path: Path | str, name: str, role: str, side: int = PREVIEW_SIDE) -> LoadedMap:
    """One map, ready for the GPU (raises if it can't be read)."""
    from .picture_look import read_scaled

    rgb, opacity, _precise = read_scaled(path, name, side, data=role not in COLOR_ROLES)
    if role in COLOR_ROLES or role in RGB_ROLES:
        rgba = np.concatenate([rgb, (opacity if opacity is not None else np.ones(rgb.shape[:2], np.float32))[..., None]], axis=-1)
        rgba = _fit(rgba, side)
        pixels = np.round(rgba * 255).astype(np.uint8)
        has_alpha = opacity is not None and bool((pixels[..., 3] < 250).any())
        return LoadedMap(role, str(path), np.ascontiguousarray(pixels), has_alpha)
    grey = rgb[..., 0] if role != "opacity" or opacity is None else opacity  # (an opacity map's alpha, if it has one)
    grey = _fit(grey, side)
    if role == "height":
        grey = spread_height(grey)
    return LoadedMap(role, str(path), np.ascontiguousarray(np.round(grey * 65535).astype(np.uint16)))


def map_choices(texture_set: TextureSet, roles: Optional[dict[str, str]] = None, off: frozenset[str] = frozenset()) -> dict[str, TextureMap]:
    """The map used for each kind (its first tile): roles overrides what a file is ({str(path): role}),
    off leaves files out."""
    if not roles and not off:
        return {role: chosen[0] for role in texture_set.roles if (chosen := texture_set.choice(role))}
    adjusted = TextureSet(texture_set.name, texture_set.folder, [
        TextureMap(roles.get(str(m.asset.path), m.role) if roles else m.role, m.asset, m.word, m.tile)
        for m in texture_set.maps if str(m.asset.path) not in off
    ])
    return {role: chosen[0] for role in adjusted.roles if (chosen := adjusted.choice(role))}


def open_map(texture_map: TextureMap):
    """A real file with the map's content while the block runs (one inside an archive: unpacked)."""
    if texture_map.asset.in_archive:
        from .archives import local_file

        return local_file(texture_map.asset.archive)
    return contextlib.nullcontext(Path(texture_map.asset.path))


def load_maps(choices: dict[str, TextureMap], open_file: Callable[[TextureMap], ContextManager[Path]] = open_map,
              side: int = PREVIEW_SIDE, threads: int = 4) -> dict[str, LoadedMap]:
    """Read the chosen maps side by side (open_file: as open_map). Maps that can't be read are left out."""

    def one(item):
        role, texture_map = item
        try:
            with open_file(texture_map) as path:
                loaded = read_map(path, texture_map.asset.name, role, side)
            loaded.file = str(texture_map.asset.path)
            return role, loaded
        except Exception:  # pylint: disable=broad-except  (damaged, or gone)
            return role, None

    with ThreadPoolExecutor(threads, thread_name_prefix="material-map") as pool:
        results = list(pool.map(one, choices.items()))
    return {role: loaded for role, loaded in results if loaded is not None}
