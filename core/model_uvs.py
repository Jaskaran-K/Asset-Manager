"""A model's UV layout, read only when it's asked for: the viewer's model loader skips UVs, which most
looks at a model never need.

OBJ and binary FBX are read here as polygons, so a quad shows as a quad and not as two triangles. Other
formats go through trimesh, which only has triangles. Edges shared by two polygons are kept once, so
a layout's lines are equally bright, and it takes half the memory. Everything here blocks: call it
on a worker thread.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import fbx_fast
from .obj_fast import BLOCK_BYTES, LoadCancelled

MAX_COORDINATE = 1e6  # (a stray UV a billion tiles away shouldn't decide what's framed)
MAX_CELLS = 400  # tiles drawn: a model tiled across thousands of them shows the first ones
UDIM_COLUMNS = 10
_KEEP_FBX = {
    "Objects", "Geometry", "Model", "PolygonVertexIndex", "LayerElementUV", "UV", "UVIndex", "Name",
    "MappingInformationType", "ReferenceInformationType", "Connections", "C",
}
_VT_ONLY = re.compile(rb"^(\d+|-\d+)/(-?\d*)(?:/-?\d*)?$")


class UvError(Exception):
    """The file's UVs can't be read (the message says why, for the user)."""


@dataclass
class UvLayout:
    uvs: Optional[np.ndarray]  # N x 2 float32 (let go once they're on the GPU)
    edges: Optional[np.ndarray]  # M x 2 uint32, each once
    edge_count: int
    polygons: int
    cells: np.ndarray  # K x 2 int32: the whole-number squares (u, v) that polygons have their middles in
    bounds: tuple[float, float, float, float]  # lowest u, v and highest u, v of the UVs
    mean_edge: float  # how long an edge is, on average, in UV units
    set_names: list[str] = field(default_factory=list)  # (more than one: the file has UV sets to choose from)
    set_index: int = 0

    @property
    def tiles(self) -> list[int]:
        """The UDIM numbers in use, in order."""
        return sorted(n for n in (udim_number(int(u), int(v)) for u, v in self.cells) if n is not None)

    @property
    def outside_tiles(self) -> int:
        """How many squares are in use that UDIM has no number for (a negative u or v, or u past 9)."""
        return len(self.cells) - len(self.tiles)


def udim_number(u: int, v: int) -> Optional[int]:
    """The UDIM number of the square whose low corner is (u, v): 1001 is the 0-1 square, 1002 to its
    right, 1011 above it. None where UDIM has no number."""
    if 0 <= u < UDIM_COLUMNS and 0 <= v:
        return 1001 + u + 10 * v
    return None


def tiles_text(layout: UvLayout, most: int = 6) -> str:
    """"1001", "1001, 1002", "1001 ... 1040 (40 tiles)"; and squares outside UDIM, if any."""
    tiles = layout.tiles
    if not tiles:
        text = "outside the 0-1 square"
    elif len(tiles) <= most:
        text = ", ".join(map(str, tiles))
    else:
        text = f"{tiles[0]} ... {tiles[-1]} ({len(tiles)} tiles)"
    if tiles and layout.outside_tiles:
        text += f", and {layout.outside_tiles} more outside UDIM"
    return text


def build_layout(uvs: np.ndarray, corners: np.ndarray, sizes: np.ndarray) -> Optional[UvLayout]:
    """A layout from polygons: corners holds each polygon's UV indices, one polygon after another
    (negative: this corner has no UV, and its polygon is left out); sizes, how many each has."""
    if len(uvs) == 0 or len(corners) == 0:
        return None
    uvs = np.nan_to_num(np.asarray(uvs, dtype=np.float32), nan=0.0, posinf=MAX_COORDINATE, neginf=-MAX_COORDINATE).clip(-MAX_COORDINATE, MAX_COORDINATE)
    corners = np.asarray(corners, dtype=np.int64)
    sizes = np.asarray(sizes, dtype=np.int64)
    missing = (corners < 0) | (corners >= len(uvs))
    if missing.any():
        owners = np.repeat(np.arange(len(sizes)), sizes)
        whole = np.ones(len(sizes), dtype=bool)
        whole[owners[missing]] = False
        corners = corners[np.repeat(whole, sizes)]
        sizes = sizes[whole]
        del owners
    sizes = sizes[sizes > 0]
    if len(corners) == 0 or len(sizes) == 0:
        return None
    ends = np.cumsum(sizes) - 1
    starts = np.concatenate(([0], ends[:-1] + 1))
    following = np.empty_like(corners)  # the next corner round each polygon
    following[:-1] = corners[1:]
    following[ends] = corners[starts]
    keep = np.repeat(sizes >= 2, sizes)  # (a one-corner "polygon" has no edges)
    low, high = np.minimum(corners, following)[keep], np.maximum(corners, following)[keep]
    del following, keep
    shared = np.unique(low * len(uvs) + high)  # each edge once
    del low, high
    edges = np.empty((len(shared), 2), dtype=np.uint32)
    edges[:, 0], edges[:, 1] = np.divmod(shared, len(uvs))
    del shared
    if len(edges) == 0:
        return None
    middles = np.add.reduceat(uvs[corners], starts, axis=0) / sizes[:, None]
    # (a polygon's middle: an edge lying along u = 1 doesn't put a model in tile 1002)
    cells = np.unique(np.floor(middles + 1e-6).astype(np.int32), axis=0)[:MAX_CELLS]
    del middles
    in_edges = uvs[edges[:, 0]] - uvs[edges[:, 1]]
    mean_edge = float(np.mean(np.hypot(in_edges[:, 0], in_edges[:, 1])))
    del in_edges
    in_polygons = np.zeros(len(uvs), dtype=bool)
    in_polygons[corners] = True
    used = uvs[in_polygons]  # (a file can hold UVs no polygon uses: they aren't framed)
    bounds = (float(used[:, 0].min()), float(used[:, 1].min()), float(used[:, 0].max()), float(used[:, 1].max()))
    return UvLayout(uvs, edges, len(edges), len(sizes), cells, bounds, mean_edge)


# ----------------------------------------------------------------------
# OBJ
# ----------------------------------------------------------------------
def _vt_numbers(lines: list[bytes]) -> np.ndarray:
    values = np.array(b" ".join(lines).split(), dtype=np.float32)
    for width in (2, 3):  # "vt u v" or "vt u v w"
        if values.size == width * len(lines):
            return values.reshape(-1, width)[:, :2]
    raise UvError("Its UV lines have differing numbers of values")


def _face_vt(flat: bytes, count: int) -> np.ndarray:
    """The UV index in each face corner, as in the file; 0 where it names none. Corners are
    "12" (none), "12//7" (none), "12/5" or "12/5/7"."""
    slashes = flat.count(b"/")
    if slashes == 0:
        return np.zeros(count, dtype=np.int64)
    if slashes == 2 * count:  # "12/5/7" and "12//7" throughout
        numbers = np.array(flat.replace(b"//", b"/0/").replace(b"/", b" ").split(), dtype=np.int64)
        if numbers.size == 3 * count:
            return numbers[1::3].copy()
    elif slashes == count:  # "12/5" throughout
        numbers = np.array(flat.replace(b"/", b" ").split(), dtype=np.int64)
        if numbers.size == 2 * count:
            return numbers[1::2].copy()
    out = np.zeros(count, dtype=np.int64)  # (corners written in different forms)
    for i, token in enumerate(flat.split()):
        match = _VT_ONLY.match(token)
        if match and match.group(2):
            out[i] = int(match.group(2))
    return out


def read_obj_uvs(path: Path, should_stop: Optional[Callable[[], bool]] = None) -> Optional[UvLayout]:
    """The layout of an OBJ, or None if it has no UVs. Faces that name none on the first read mean
    a sculpt without any: a file of gigabytes is left there, rather than read to the end to learn it."""
    uv_blocks: list[np.ndarray] = []
    corner_blocks: list[np.ndarray] = []
    size_blocks: list[np.ndarray] = []
    uv_count = 0
    rest = b""
    with open(path, "rb") as f:
        while True:
            if should_stop is not None and should_stop():
                raise LoadCancelled()
            block = f.read(BLOCK_BYTES)
            data = rest + block
            if block:
                cut = data.rfind(b"\n") + 1
                data, rest = data[:cut], data[cut:]
            if not data and not block:
                break
            if b"\nf" not in data and b"\nvt" not in data and not data.startswith((b"f", b"vt")):
                if not block:  # (only points and normals in this block, as in most of a sculpt: no need to look at its lines)
                    break
                continue
            lines = data.split(b"\n")
            del data
            vt_lines, f_lines = [], []
            for line in lines:
                head = line[:3]
                if head in (b"vt ", b"vt\t"):
                    vt_lines.append(line[3:])
                elif head[:2] in (b"f ", b"f\t"):
                    f_lines.append(line[2:])
            del lines
            if vt_lines:
                numbers = _vt_numbers(vt_lines)
                uv_blocks.append(numbers)
                uv_count += len(numbers)
            if f_lines:
                sizes = np.fromiter((len(line.split()) for line in f_lines), dtype=np.int64, count=len(f_lines))
                named = _face_vt(b" ".join(f_lines), int(sizes.sum()))
                del f_lines
                if not uv_blocks and not corner_blocks and not named.any():
                    return None
                # 1-based in the file; negative counts back from the UVs read so far; 0 is none
                corner_blocks.append(np.where(named > 0, named - 1, np.where(named < 0, uv_count + named, -1)))
                size_blocks.append(sizes)
            if not block:
                break
    if not uv_blocks or not corner_blocks:
        return None
    corners = np.concatenate(corner_blocks)
    if not (corners >= 0).any():
        return None
    return build_layout(np.concatenate(uv_blocks), corners, np.concatenate(size_blocks))


# ----------------------------------------------------------------------
# FBX
# ----------------------------------------------------------------------
def _text(node, name: str) -> str:
    child = node.child(name)
    return child.props[0] if child is not None and child.props and isinstance(child.props[0], str) else ""


def _layer_corners(layer, polygon_vertex_index: np.ndarray) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """(UVs N x 2, the UV index of every polygon corner) from an FBX UV layer; None if it can't be followed."""
    uv_node, index_node = layer.child("UV"), layer.child("UVIndex")
    if uv_node is None or not uv_node.props:
        return None
    uvs = np.asarray(uv_node.props[0], dtype=np.float32).reshape(-1, 2)
    index = np.asarray(index_node.props[0], dtype=np.int64) if index_node is not None and index_node.props else None
    mapping, reference = _text(layer, "MappingInformationType"), _text(layer, "ReferenceInformationType")
    direct = reference == "Direct" or index is None
    if mapping == "ByPolygonVertex":
        corners = np.arange(len(polygon_vertex_index), dtype=np.int64) if direct else index
    elif mapping in ("ByControlPoint", "ByVertice", "ByVertex"):  # one UV per point
        points = np.where(polygon_vertex_index < 0, ~polygon_vertex_index, polygon_vertex_index).astype(np.int64)
        if direct:
            corners = points
        else:
            corners = np.full(len(points), -1, dtype=np.int64)
            known = points < len(index)
            corners[known] = index[points[known]]
    else:  # (per polygon, or all alike: nothing that makes a layout)
        return None
    return (uvs, corners) if len(corners) == len(polygon_vertex_index) else None


def read_fbx_uvs(path: Path, set_index: int = 0, should_stop: Optional[Callable[[], bool]] = None) -> Optional[UvLayout]:
    """The layout of a binary FBX: every mesh's UV set number set_index (a mesh with fewer sets has
    none to show in that one). None if it has no UVs. Raises fbx_fast.FbxError if it isn't readable."""
    with open(path, "rb") as file:
        data = memoryview(file.read())
    try:
        tree = {node.name: node for node in fbx_fast._read_tree(data, _KEEP_FBX)}  # pylint: disable=protected-access
    except (struct.error, zlib.error, IndexError, ValueError) as exc:
        raise fbx_fast.FbxError(f"damaged FBX: {exc}") from exc
    del data
    objects, connections = tree.get("Objects"), tree.get("Connections")
    if objects is None or connections is None:
        raise fbx_fast.FbxError("no objects")
    models = {int(n.props[0]) for n in objects.children if n.name == "Model" and n.props}
    # (a mesh that isn't in the scene isn't shown as a model either)
    placed = {int(c.props[1]) for c in connections.children if c.name == "C" and len(c.props) >= 3 and c.props[0] == "OO" and int(c.props[2]) in models}

    uv_parts, corner_parts, size_parts, set_names, offset = [], [], [], [], 0
    for geometry in objects.children:
        if should_stop is not None and should_stop():
            raise LoadCancelled()
        if geometry.name != "Geometry" or not geometry.props or int(geometry.props[0]) not in placed:
            continue
        layers = [c for c in geometry.children if c.name == "LayerElementUV"]
        index_node = geometry.child("PolygonVertexIndex")
        for number, layer in enumerate(layers):  # (a set's name: from the first mesh that has it)
            if number == len(set_names):
                set_names.append(_text(layer, "Name") or f"UV Set {number + 1}")
        if set_index >= len(layers) or index_node is None or not index_node.props:
            continue
        polygon_vertex_index = np.asarray(index_node.props[0], dtype=np.int64)
        found = _layer_corners(layers[set_index], polygon_vertex_index)
        if found is None or len(polygon_vertex_index) == 0:
            continue
        uvs, corners = found
        ends = np.flatnonzero(polygon_vertex_index < 0)
        if len(ends) == 0:
            continue
        corners = corners[: int(ends[-1]) + 1]  # (a last polygon without its end marker: left out)
        uv_parts.append(uvs)
        corner_parts.append(np.where(corners >= 0, corners + offset, -1))
        size_parts.append(np.diff(ends, prepend=-1))
        offset += len(uvs)
        index_node.props, layers[set_index].children = [], []  # (let each array go once it's used)
    if not uv_parts:
        return None
    layout = build_layout(np.concatenate(uv_parts), np.concatenate(corner_parts), np.concatenate(size_parts))
    if layout is not None:
        layout.set_names, layout.set_index = set_names, set_index
    return layout


# ----------------------------------------------------------------------
# Everything else
# ----------------------------------------------------------------------
def _read_with_trimesh(path: Path) -> Optional[UvLayout]:
    """glTF and the rest: what trimesh finds, as triangles."""
    import trimesh

    loaded = trimesh.load(str(path), force="mesh")
    uvs = getattr(getattr(loaded, "visual", None), "uv", None)
    faces = getattr(loaded, "faces", None)
    if uvs is None or faces is None or len(uvs) == 0 or len(faces) == 0:
        return None
    return build_layout(np.asarray(uvs, dtype=np.float32), np.asarray(faces, dtype=np.int64).reshape(-1), np.full(len(faces), 3, dtype=np.int64))


def read_uvs(path: Path, set_index: int = 0, should_stop: Optional[Callable[[], bool]] = None) -> Optional[UvLayout]:
    """The model's UV layout, or None if it has no UVs. Raises UvError if they can't be read,
    LoadCancelled if should_stop() says to stop."""
    extension = path.suffix.lower()
    try:
        if extension == ".obj":
            try:
                return read_obj_uvs(path, should_stop)
            except (UvError, ValueError):  # (not what the quick reader follows: trimesh may)
                return _read_with_trimesh(path)
        if extension == ".fbx":
            try:
                return read_fbx_uvs(path, set_index, should_stop)
            except fbx_fast.FbxError as exc:
                raise UvError("Its UVs can only be read from binary FBX files.") from exc
        return _read_with_trimesh(path)
    except (LoadCancelled, UvError, MemoryError):
        raise
    except Exception as exc:  # pylint: disable=broad-except
        raise UvError(f"Its UVs couldn't be read: {exc}") from exc
