"""A fast reader for binary FBX geometry: every mesh in the file, placed where the file puts it.

The fbxloader package reads each number of an array with its own struct.unpack call, which
takes most of a minute for a big sculpt (a 300 MB hair model: 7 million points). Here arrays are
read with numpy straight from their (decompressed) bytes and polygons are split into triangles
with array operations, so the same file loads in seconds. Transforms follow fbxloader (and three.js,
which it follows): fbxloader.utils.get_transform for each Model, parents' first.

Only what's needed for geometry is kept: the Objects' Geometry and Model nodes and the
Connections; other nodes (animation, materials, textures) are skipped without reading their arrays.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .model_stats import ModelStats

MAGIC =b"Kaydara FBX Binary  \x00"
_ARRAY_TYPES = {"f": np.float32, "d": np.float64, "l": np.int64, "i": np.int32, "b": np.uint8, "c": np.uint8}
_SCALARS = {"Y": ("<h", 2), "C": ("<?", 1), "I": ("<i", 4), "F": ("<f", 4), "D": ("<d", 8), "L": ("<q", 8)}
# Nodes whose contents are needed (others are skipped whole, by their end offset)
_KEEP = {"GlobalSettings", "Objects", "Geometry", "Model", "Vertices", "PolygonVertexIndex", "Properties70", "P", "Connections", "C"}


class FbxError(Exception):
    """Not a binary FBX this reader can read (the caller falls back to another loader)."""


@dataclass
class _Node:
    name: str
    props: list[Any]
    children: list["_Node"] = field(default_factory=list)

    def child(self, name: str) -> Optional["_Node"]:
        return next((c for c in self.children if c.name == name), None)


def _read_tree(data: memoryview, keep: Optional[set[str]] = None) -> list[_Node]:
    """The file's nodes; of those not named in keep (default: what geometry needs), only their names."""
    keep = _KEEP if keep is None else keep
    if bytes(data[:21]) != MAGIC:
        raise FbxError("not a binary FBX")
    version = struct.unpack_from("<I", data, 23)[0]
    wide = version >= 7500
    head = struct.Struct("<QQQ" if wide else "<III")
    null_size = head.size + 1

    def read_props(offset: int, count: int) -> list[Any]:
        props = []
        for _ in range(count):
            kind = chr(data[offset])
            offset += 1
            if kind in _SCALARS:
                fmt, size = _SCALARS[kind]
                props.append(struct.unpack_from(fmt, data, offset)[0])
                offset += size
            elif kind in ("S", "R"):
                (length,) = struct.unpack_from("<I", data, offset)
                raw = bytes(data[offset + 4: offset + 4 + length])
                props.append(raw.decode("utf-8", "replace") if kind == "S" else raw)
                offset += 4 + length
            elif kind in _ARRAY_TYPES:
                length, encoding, size = struct.unpack_from("<III", data, offset)
                raw = data[offset + 12: offset + 12 + size]
                if encoding == 1:
                    raw = zlib.decompress(raw)
                elif encoding != 0:
                    raise FbxError(f"unknown array encoding {encoding}")
                props.append(np.frombuffer(raw, dtype=_ARRAY_TYPES[kind], count=length))
                offset += 12 + size
            else:
                raise FbxError(f"unknown property type {kind!r}")
        return props

    def read_node(offset: int) -> tuple[Optional[_Node], int]:
        end, count, props_size = head.unpack_from(data, offset)
        if end == 0:
            return None, offset + null_size
        name_len = data[offset + head.size]
        name = bytes(data[offset + head.size + 1: offset + head.size + 1 + name_len]).decode("ascii", "replace")
        props_at = offset + head.size + 1 + name_len
        if name not in keep:
            return _Node(name, []), end  # (skipped, arrays and all)
        node = _Node(name, read_props(props_at, count))
        at = props_at + props_size
        while at < end:
            child, at = read_node(at)
            if child is None:
                break
            node.children.append(child)
        return node, end

    nodes, at = [], 27
    while at + null_size <= len(data):
        node, at = read_node(at)
        if node is None:
            break
        nodes.append(node)
    return nodes


def _properties(model: _Node) -> dict[str, Any]:
    """A Model's Properties70, as fbxloader's get_transform wants them."""
    from fbxloader.utils import EULER_ORDERS

    values: dict[str, list] = {}
    block = model.child("Properties70")
    for p in block.children if block is not None else []:
        if len(p.props) >= 5 and isinstance(p.props[0], str):
            values[p.props[0]] = p.props[4:]
    data: dict[str, Any] = {}
    names = {
        "Lcl Translation": "translation", "PreRotation": "preRotation", "Lcl Rotation": "rotation",
        "PostRotation": "postRotation", "Lcl Scaling": "scale", "ScalingOffset": "scalingOffset",
        "ScalingPivot": "scalingPivot", "RotationOffset": "rotationOffset", "RotationPivot": "rotationPivot",
    }
    for fbx_name, key in names.items():
        if fbx_name in values and len(values[fbx_name]) >= 3:
            data[key] = [float(v) for v in values[fbx_name][:3]]
    if "RotationOrder" in values and values["RotationOrder"]:
        data["eulerOrder"] = EULER_ORDERS[int(values["RotationOrder"][0]) % len(EULER_ORDERS)]
    if "InheritType" in values and values["InheritType"]:
        data["inheritType"] = int(values["InheritType"][0])
    geometric = {}
    for fbx_name, key in (("GeometricTranslation", "translation"), ("GeometricRotation", "rotation"), ("GeometricScaling", "scale")):
        if fbx_name in values and len(values[fbx_name]) >= 3:
            geometric[key] = [float(v) for v in values[fbx_name][:3]]
    if "eulerOrder" in data:
        geometric["eulerOrder"] = data["eulerOrder"]
    return {"local": data, "geometric": geometric}


def triangulate(polygon_vertex_index: np.ndarray) -> np.ndarray:
    """Triangles (M x 3) from FBX polygons: indices where a negative one (~index) ends a polygon.
    Each polygon is a fan from its first corner, as fbxloader does. (32-bit throughout: a big
    sculpt has tens of millions of corners.)"""
    idx = np.asarray(polygon_vertex_index, dtype=np.int32)
    if idx.size == 0:
        return np.zeros((0, 3), dtype=np.uint32)
    ends = idx < 0
    corners = np.where(ends, ~idx, idx).view(np.uint32)
    # Where each corner's polygon starts: 0, and just after each end
    starts = np.empty(idx.size, dtype=bool)
    starts[0] = True
    starts[1:] = ends[:-1]
    first = np.arange(idx.size, dtype=np.int32)
    first[~starts] = 0
    np.maximum.accumulate(first, out=first)
    # A triangle (first, k, k + 1) for every corner k that is neither its polygon's first nor its last
    middle = ~starts & ~ends
    middle[-1] = False  # (a last polygon without its end marker)
    k = np.flatnonzero(middle).astype(np.int32)
    triangles = np.empty((len(k), 3), dtype=np.uint32)
    triangles[:, 0] = corners[first[k]]
    triangles[:, 1] = corners[k]
    triangles[:, 2] = corners[k + 1]
    return triangles


def _unit_cm(settings: Optional[_Node]) -> Optional[float]:
    """How many centimetres one of the file's units is (GlobalSettings' UnitScaleFactor)."""
    block = settings.child("Properties70") if settings is not None else None
    for p in block.children if block is not None else []:
        if len(p.props) >= 5 and p.props[0] == "UnitScaleFactor" and isinstance(p.props[4], (int, float)) and p.props[4] > 0:
            return float(p.props[4])
    return None


def read_fbx(path: Path, stats: Optional[ModelStats] = None) -> tuple[np.ndarray, np.ndarray, int]:
    """(vertices N x 3 float32, triangles M x 3 uint32, how many meshes) from a binary FBX.
    stats, if given, is told what the file holds. Raises FbxError if it isn't one this reader can read."""
    from fbxloader.utils import get_transform

    with open(path, "rb") as file:
        data = memoryview(file.read())
    try:
        tree = {node.name: node for node in _read_tree(data)}
    except (struct.error, zlib.error, IndexError, ValueError) as exc:
        raise FbxError(f"damaged FBX: {exc}") from exc
    del data  # (the arrays are copies, except uncompressed ones, which keep their part of it)
    objects, connections = tree.get("Objects"), tree.get("Connections")
    if objects is None or connections is None:
        raise FbxError("no objects")
    parents: dict[int, list[int]] = {}
    for c in connections.children:
        if c.name == "C" and len(c.props) >= 3 and c.props[0] == "OO":
            parents.setdefault(int(c.props[1]), []).append(int(c.props[2]))
    models = {int(n.props[0]): n for n in objects.children if n.name == "Model" and n.props}
    worlds: dict[int, np.ndarray] = {}

    def world(model_id: int, seen: frozenset = frozenset()) -> np.ndarray:
        if model_id in worlds:
            return worlds[model_id]
        local = get_transform(_properties(models[model_id])["local"])
        parent = next((p for p in parents.get(model_id, []) if p in models), None)
        matrix = local if parent is None or parent in seen else world(parent, seen | {model_id}) @ local
        worlds[model_id] = matrix
        return matrix

    vertex_parts, face_parts, offset = [], [], 0
    for geometry in objects.children:
        if geometry.name != "Geometry" or not geometry.props:
            continue
        vertices_node, index_node = geometry.child("Vertices"), geometry.child("PolygonVertexIndex")
        if vertices_node is None or index_node is None or not vertices_node.props or not index_node.props:
            continue
        model_id = next((p for p in parents.get(int(geometry.props[0]), []) if p in models), None)
        if model_id is None:
            continue
        vertices = np.asarray(vertices_node.props[0], dtype=np.float64).reshape(-1, 3)
        polygon_vertex_index = index_node.props[0]
        faces = triangulate(polygon_vertex_index)
        vertices_node.props, index_node.props = [], []  # (let each array go once it's used: a sculpt's are hundreds of MB)
        if len(vertices) == 0 or len(faces) == 0 or int(faces.max()) >= len(vertices):
            continue
        if stats is not None:
            stats.vertices += len(vertices)
            stats.add_fbx_polygons(polygon_vertex_index)
        del polygon_vertex_index
        matrix = world(model_id) @ get_transform(_properties(models[model_id])["geometric"])
        placed = vertices.astype(np.float32)
        del vertices
        placed @= matrix[:3, :3].T.astype(np.float32)
        placed += matrix[:3, 3].astype(np.float32)
        faces += np.uint32(offset)
        vertex_parts.append(placed)
        face_parts.append(faces)
        offset += len(placed)
    if not vertex_parts:
        raise FbxError("no meshes")
    if stats is not None:
        stats.unit_cm = _unit_cm(tree.get("GlobalSettings"))
    if len(vertex_parts) == 1:
        return vertex_parts[0], face_parts[0], 1
    count = len(vertex_parts)
    vertices = np.concatenate(vertex_parts)
    vertex_parts.clear()
    faces = np.concatenate(face_parts)
    return vertices, faces, count
