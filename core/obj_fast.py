"""A fast reader for Wavefront OBJ geometry.

trimesh reads OBJ text line by line in Python, which takes about a minute for a 250 MB sculpt and
far longer (with many GB of memory) for a 2 GB one. Here the file is read in 64 MB blocks: the
"v", "vn" and "f" lines of a block are picked out and turned into numbers by numpy, and
polygons are split into triangles (fans) with array operations, so memory stays bounded and the
same 250 MB file loads in seconds. (Splitting the text and converting the pieces with np.array is
several times faster than np.fromstring's own parser.)

When the faces name the file's own normals (f v//vn or v/vt/vn), they're kept: a vertex with
different normals on different faces (a hard edge) is split, so hard-surface models keep sharp
edges. Otherwise the caller computes smooth normals.

Anything unusual (relative / negative indices, mixed vertex line lengths, no faces) raises
ObjError, and the caller falls back to trimesh. Groups, materials and texture coordinates are
ignored: the viewer shows the shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .model_stats import ModelStats

BLOCK_BYTES = 64 * 1024 * 1024
_TEX_AND_NORMAL = re.compile(rb"/\S*")  # "12/5/7" -> "12" (for faces whose corners differ in form)


class ObjError(Exception):
    """Not an OBJ this reader can read (the caller falls back to another loader)."""


class LoadCancelled(Exception):
    """should_stop() said to stop: nobody wants the model any more."""


def _parse(text: bytes, dtype) -> np.ndarray:
    try:
        return np.array(text.split(), dtype=dtype)
    except ValueError as exc:
        raise ObjError(f"Unreadable numbers: {exc}") from exc


def _numbers(lines: list[bytes], dtype, per_line: int, what: str) -> np.ndarray:
    values = _parse(b" ".join(lines), dtype)
    if values.size == per_line * len(lines):
        return values.reshape(-1, per_line)
    for width in (4, 6, 7):  # "v x y z w", "v x y z r g b", "v x y z w r g b": keep xyz
        if values.size == width * len(lines):
            return values.reshape(-1, width)[:, :per_line]
    raise ObjError(f"{what} lines with differing numbers of values")


def read_obj(
    path: Path, should_stop: Optional[Callable[[], bool]] = None, stats: Optional[ModelStats] = None,
) -> tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]:
    """(vertices float32 (N, 3), triangles int32 (M, 3), normals float32 (N, 3) or None) of an OBJ.
    stats, if given, is told what the file holds (the vertices returned can be more: see above)."""
    vertex_blocks: list[np.ndarray] = []
    normal_blocks: list[np.ndarray] = []
    index_blocks: list[np.ndarray] = []  # position index of every polygon corner (1-based, as in the file)
    corner_normal_blocks: list[np.ndarray] = []  # normal index of every corner, while every face names one
    count_blocks: list[np.ndarray] = []  # corners per polygon
    corner_normals = True
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
            if not data:
                break
            lines = data.split(b"\n")
            del data
            v_lines, vn_lines, f_lines = [], [], []
            for line in lines:
                head = line[:3]
                if head[:2] in (b"v ", b"v\t"):
                    v_lines.append(line[2:])
                elif head in (b"vn ", b"vn\t"):
                    vn_lines.append(line[3:])
                elif head[:2] in (b"f ", b"f\t"):
                    f_lines.append(line[2:])
            del lines
            if v_lines:
                vertex_blocks.append(_numbers(v_lines, np.float32, 3, "Vertex"))
            if vn_lines:
                normal_blocks.append(_numbers(vn_lines, np.float32, 3, "Normal"))
            if f_lines:
                counts = np.fromiter((len(line.split()) for line in f_lines), dtype=np.int32, count=len(f_lines))
                flat = b" ".join(f_lines)
                del f_lines
                positions, normals_named = _corners(flat, int(counts.sum()))
                del flat
                index_blocks.append(positions)
                count_blocks.append(counts)
                # The file's normals are kept only while every corner names one ("v/vt/vn" or "v//vn")
                corner_normals = corner_normals and normals_named is not None
                if corner_normals:
                    corner_normal_blocks.append(normals_named)
                else:
                    corner_normal_blocks.clear()
            if not block:
                break

    if not vertex_blocks or not index_blocks:
        raise ObjError("No faces")
    vertices = np.concatenate(vertex_blocks)
    del vertex_blocks
    corners = np.concatenate(index_blocks)
    del index_blocks
    counts = np.concatenate(count_blocks)
    if corners.min() < 1 or corners.max() > len(vertices):
        raise ObjError("Relative or out-of-range vertex indices")  # (trimesh follows relative ones)
    corners -= 1
    if stats is not None:
        stats.vertices += len(vertices)
        stats.add_polygons(counts)

    file_normals =np.concatenate(normal_blocks) if normal_blocks else None
    corner_normal_index = None
    if corner_normals and file_normals is not None and corner_normal_blocks:
        corner_normal_index = np.concatenate(corner_normal_blocks)
        if corner_normal_index.min() < 1 or corner_normal_index.max() > len(file_normals):
            corner_normal_index = None  # (the file's normals don't add up: smooth ones are computed)
        else:
            corner_normal_index -= 1
    del corner_normal_blocks

    normals = None
    if corner_normal_index is not None:
        # Files often number a normal per corner even where they're all the same: normals are
        # grouped by direction first, so only a real hard edge splits a vertex
        lengths = np.linalg.norm(file_normals, axis=1, keepdims=True)
        file_normals /= np.where(lengths > 0, lengths, 1)
        direction = _direction_keys(file_normals)
        directions, direction_of = np.unique(direction, return_inverse=True)
        del direction
        # One vertex per (position, direction) pair
        pairs = corners * len(directions) + direction_of[corner_normal_index]
        del corner_normal_index
        unique, corners = np.unique(pairs, return_inverse=True)
        del pairs
        positions, directions_used = np.divmod(unique, len(directions))
        first_normal = np.zeros(len(directions), dtype=np.int64)
        first_normal[direction_of[::-1]] = np.arange(len(direction_of))[::-1]  # (a normal with that direction)
        normals = file_normals[first_normal[directions_used]]
        vertices = vertices[positions]

    return vertices, _fan_triangles(corners, counts), normals


def _corners(flat: bytes, count: int) -> tuple[np.ndarray, Optional[np.ndarray]]:
    """(position index, normal index or None) of each face corner ("12", "12/5", "12//7", "12/5/7")."""
    slashes = flat.count(b"/")
    if slashes == 2 * count:  # every corner names a normal
        numbers = _parse(flat.replace(b"//", b"/0/").replace(b"/", b" "), np.int64)
        if numbers.size == 3 * count:
            table = numbers.reshape(-1, 3)
            return table[:, 0].copy(), table[:, 2].copy()
    elif slashes in (0, count):  # "12" or "12/5" throughout
        step = 1 if slashes == 0 else 2
        numbers = _parse(flat.replace(b"/", b" "), np.int64)
        if numbers.size == step * count:
            return numbers[::step].copy(), None
    positions = _parse(_TEX_AND_NORMAL.sub(b"", flat), np.int64)  # (corners written in different forms)
    if positions.size != count:
        raise ObjError("Face lines this reader can't follow")
    return positions, None


def _direction_keys(normals: np.ndarray) -> np.ndarray:
    """One int64 per unit normal, equal for directions within about 0.1 degree of each other."""
    q = np.rint((normals + 1.0) * 1000.0).astype(np.int64)  # 0..2000 per axis: 11 bits each
    return (q[:, 0] << 22) | (q[:, 1] << 11) | q[:, 2]


def _fan_triangles(corners: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """Polygons (their corners, one after another; counts: corners per polygon) as triangle fans."""
    starts = np.zeros(len(counts), dtype=np.int64)
    np.cumsum(counts[:-1], out=starts[1:])
    per_polygon = np.maximum(counts.astype(np.int64) - 2, 0)  # (a 2-corner "face" makes none)
    total = int(per_polygon.sum())
    if total == 0:
        raise ObjError("No faces")
    owner = np.repeat(np.arange(len(counts)), per_polygon)
    within = np.arange(total) - np.repeat(np.cumsum(per_polygon) - per_polygon, per_polygon)
    first = starts[owner]
    del owner
    triangles = np.empty((total, 3), dtype=np.int32)
    triangles[:, 0] = corners[first]
    triangles[:, 1] = corners[first + within + 1]
    triangles[:, 2] = corners[first + within + 2]
    return triangles
