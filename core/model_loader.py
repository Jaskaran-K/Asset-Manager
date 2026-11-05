"""High-performance 3D model loader and data structures.

Supports loading Wavefront OBJ, Autodesk FBX (Binary & ASCII), glTF, GLB,
STL, PLY, and other 3D formats with hardware-accelerated vertex processing.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from .model_stats import ModelStats


def _trimesh():
    """trimesh, imported when a model is first read: it brings scipy with it, over half a
    second of the app's start-up otherwise."""
    import trimesh

    return trimesh


@dataclass
class Mesh3D:
    """Represents normalized 3D mesh geometry ready for hardware viewport rendering."""

    name: str
    vertices: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=np.float32))
    faces: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=np.uint32))
    normals: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=np.float32))
    bbox_min: tuple[float, float, float] = (-1.0, -1.0, -1.0)
    bbox_max: tuple[float, float, float] = (1.0, 1.0, 1.0)
    center: tuple[float, float, float] = (0.0, 0.0, 0.0)
    dimensions: tuple[float, float, float] = (2.0, 2.0, 2.0)
    ground_y: float = -0.9
    format_type: str = "Unknown"
    metadata: dict[str, str] = field(default_factory=dict)
    stand_in: bool = False  # the file couldn't be read: this is the placeholder cube
    stats: Optional[ModelStats] = None  # what the file holds (polygon kinds, units), if its reader could tell

    @property
    def vertex_count(self) -> int:
        return len(self.vertices) if self.vertices is not None else 0

    @property
    def face_count(self) -> int:
        return len(self.faces) if self.faces is not None else 0


_NORMALS_CHUNK = 2_000_000  # triangles at a time (a big sculpt has tens of millions)


def _compute_vertex_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Compute per-vertex smooth normals from triangle faces (in float32 and a chunk of triangles
    at a time, so a big sculpt doesn't need gigabytes for it)."""
    if len(vertices) == 0 or len(faces) == 0:
        return np.zeros_like(vertices, dtype=np.float32)

    vertices = np.asarray(vertices, dtype=np.float32)
    vert_normals = np.zeros((len(vertices), 3), dtype=np.float32)
    for start in range(0, len(faces), _NORMALS_CHUNK):
        part = faces[start: start + _NORMALS_CHUNK]
        v0 = vertices[part[:, 0]]
        face_normals = np.cross(vertices[part[:, 1]] - v0, vertices[part[:, 2]] - v0)
        norms = np.linalg.norm(face_normals, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        face_normals /= norms
        # Accumulate onto vertices (bincount: many times quicker than np.add.at on big meshes)
        corners = part.reshape(-1)
        for axis in range(3):
            weights = np.repeat(face_normals[:, axis], 3)
            vert_normals[:, axis] += np.bincount(corners, weights=weights, minlength=len(vertices)).astype(np.float32)

    v_norms = np.linalg.norm(vert_normals, axis=1, keepdims=True)
    v_norms[v_norms == 0] = 1.0
    vert_normals /= v_norms

    return vert_normals


def _normalize_geometry(
    name: str,
    raw_verts: np.ndarray,
    raw_faces: np.ndarray,
    raw_normals: Optional[np.ndarray],
    format_type: str,
    metadata: dict[str, str],
    stats: Optional[ModelStats] = None,
) -> Mesh3D:
    """Normalize vertices to fit nicely within standard viewport view bounds. stats: what the file
    holds, if its reader could tell (otherwise only the triangles drawn are known)."""
    mesh = Mesh3D(name=name, format_type=format_type, stats=stats)

    if raw_verts is None or len(raw_verts) == 0:
        mesh.vertices, mesh.faces, mesh.normals = _get_default_preview_cube()
        mesh.stand_in = True
        mesh.metadata = metadata
        return mesh

    verts = np.asarray(raw_verts, dtype=np.float32)

    # Flatten or reshape faces into triangle list (M, 3)
    if raw_faces is not None and len(raw_faces) > 0:
        faces = np.asarray(raw_faces, dtype=np.uint32)
        if faces.ndim != 2 or faces.shape[1] != 3:
            faces = faces.reshape(-1, 3)
    else:
        faces = np.zeros((0, 3), dtype=np.uint32)

    # Compute bounding box
    min_b = verts.min(axis=0)
    max_b = verts.max(axis=0)
    cx, cy, cz = (min_b + max_b) / 2.0
    dx, dy, dz = max_b - min_b
    max_dim = float(max(dx, dy, dz, 1e-5))
    scale = 1.8 / max_dim

    # Center model at origin (in place when the array is our own: a sculpt's is hundreds of MB)
    norm_verts = verts if verts is not raw_verts or raw_verts.flags.owndata and raw_verts.flags.writeable else verts.copy()
    norm_verts -= np.array([cx, cy, cz], dtype=np.float32)
    norm_verts *= np.float32(scale)

    ground_y = float((min_b[1] - cy) * scale)

    # Normals
    if raw_normals is not None and len(raw_normals) == len(verts):
        normals = np.asarray(raw_normals, dtype=np.float32)
    else:
        normals = _compute_vertex_normals(norm_verts, faces)

    mesh.vertices = norm_verts
    mesh.faces = faces
    mesh.normals = normals
    mesh.bbox_min = (float(min_b[0]), float(min_b[1]), float(min_b[2]))
    mesh.bbox_max = (float(max_b[0]), float(max_b[1]), float(max_b[2]))
    mesh.center = (float(cx), float(cy), float(cz))
    mesh.dimensions = (float(dx), float(dy), float(dz))
    mesh.ground_y = ground_y

    meta = {"Format": format_type}
    if stats is not None and stats.polygons > 0:
        meta.update(stats.rows(len(faces)))
    else:
        meta.update({"Vertices": f"{len(verts):,}", "Triangles / Faces": f"{len(faces):,}"})
    size, unit = _real_size((dx, dy, dz), format_type, stats)
    meta["Dimensions (X×Y×Z)"] = f"{size[0]:.2f} × {size[1]:.2f} × {size[2]:.2f} {unit}"
    meta.update(metadata)
    mesh.metadata = meta
    return mesh


def _real_size(size: tuple[float, float, float], format_type: str, stats: Optional[ModelStats]) -> tuple[tuple[float, float, float], str]:
    """The model's size and its unit, where its format says: an FBX states its own, glTF is always
    in metres, and the numbers of the others (OBJ, STL...) mean whatever whoever exported it meant."""
    if stats is not None and stats.unit_cm is not None:
        return (size[0] * stats.unit_cm, size[1] * stats.unit_cm, size[2] * stats.unit_cm), "cm"
    return size, "m" if format_type == "glTF" else "units"


def mesh_from_arrays(name: str, vertices: np.ndarray, faces: np.ndarray, normals: Optional[np.ndarray],
                     format_type: str, metadata: dict[str, str]) -> Mesh3D:
    """A Mesh3D made from arrays (an alpha's 3D preview: core.alphas), centred and scaled like a loaded model."""
    return _normalize_geometry(name, vertices, faces, normals, format_type, metadata)


def parse_fbx_file(file_path: Path) -> Mesh3D:
    """Parse Autodesk FBX (Binary or ASCII) into normalized Mesh3D geometry."""
    metadata: dict[str, str] = {
        "Format": "Autodesk FBX",
        "File Size": f"{file_path.stat().st_size / (1024*1024):.2f} MB",
    }

    # 1. Binary FBX: the quick reader (core.fbx_fast), then fbxloader for what it can't read
    try:
        from core.fbx_fast import FbxError, read_fbx

        stats = ModelStats()
        verts, faces, meshes = read_fbx(file_path, stats=stats)
        metadata["Subtype"] = "Binary FBX"
        if meshes > 1:
            metadata["Meshes"] = f"{meshes:,}"
        return _normalize_geometry(
            name=file_path.name, raw_verts=verts, raw_faces=faces, raw_normals=None, format_type="FBX", metadata=metadata, stats=stats,
        )
    except FbxError as exc:
        fast_note = str(exc)  # (shown only if nothing else can read it either)
    except MemoryError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        fast_note = f"{type(exc).__name__}: {exc}"
    try:
        import fbxloader

        fbx = fbxloader.FBXLoader(str(file_path))
        tm = fbx.export_trimesh()
        if tm is not None and hasattr(tm, "vertices") and len(tm.vertices) > 0:
            normals = getattr(tm, "vertex_normals", None)
            metadata["Subtype"] = "Binary FBX"
            return _normalize_geometry(
                name=file_path.name,
                raw_verts=np.asarray(tm.vertices, dtype=np.float32),
                raw_faces=np.asarray(tm.faces, dtype=np.uint32),
                raw_normals=np.asarray(normals, dtype=np.float32) if normals is not None else None,
                format_type="FBX",
                metadata=metadata,
            )
    except Exception as exc:
        metadata["Binary Loader Note"] = str(exc)

    # 2. Try ASCII FBX parser if it's text-based
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(5000000)  # Read up to 5MB for ASCII header/geometry
        if "Vertices:" in content and "PolygonVertexIndex:" in content:
            v_match = re.search(r"Vertices:\s*\*\d+\s*\{\s*a:\s*([^}]+)\}", content)
            p_match = re.search(r"PolygonVertexIndex:\s*\*\d+\s*\{\s*a:\s*([^}]+)\}", content)
            if v_match and p_match:
                raw_v = [float(x.strip()) for x in v_match.group(1).replace("\n", "").split(",") if x.strip()]
                raw_p = [int(x.strip()) for x in p_match.group(1).replace("\n", "").split(",") if x.strip()]
                verts = np.array(raw_v, dtype=np.float32).reshape(-1, 3)

                stats = ModelStats(vertices=len(verts))
                stats.add_fbx_polygons(np.array(raw_p, dtype=np.int64))
                faces = []
                current_poly: list[int] = []
                for idx in raw_p:
                    if idx < 0:
                        current_poly.append(-idx - 1)
                        if len(current_poly) >= 3:
                            for i in range(1, len(current_poly) - 1):
                                faces.append([current_poly[0], current_poly[i], current_poly[i + 1]])
                        current_poly = []
                    else:
                        current_poly.append(idx)

                metadata["Subtype"] = "ASCII FBX"
                return _normalize_geometry(
                    name=file_path.name,
                    raw_verts=verts,
                    raw_faces=np.array(faces, dtype=np.uint32),
                    raw_normals=None,
                    format_type="FBX",
                    metadata=metadata,
                    stats=stats,
                )
    except Exception:
        pass

    # 3. Fallback preview cube with extracted header metadata
    try:
        with open(file_path, "rb") as f:
            header = f.read(27)
            if header.startswith(b"Kaydara FBX Binary  \x00"):
                version = struct.unpack("<I", header[23:27])[0]
                metadata["FBX Version"] = str(version)
    except Exception:
        pass

    metadata.setdefault("Binary Loader Note", fast_note)
    mesh = Mesh3D(name=file_path.name, format_type="FBX")
    mesh.vertices, mesh.faces, mesh.normals = _get_default_preview_cube()
    mesh.stand_in = True
    mesh.metadata = metadata
    return mesh


def parse_obj_file(file_path: Path, should_stop: Optional[Callable[[], bool]] = None) -> Mesh3D:
    """Parse Wavefront OBJ file into normalized Mesh3D geometry. should_stop() is asked between
    blocks of a big file: True raises LoadCancelled (nobody wants the model any more)."""
    metadata: dict[str, str] = {
        "Format": "Wavefront OBJ",
        "File Size": f"{file_path.stat().st_size / (1024*1024):.2f} MB",
    }

    # 1. The quick reader (core.obj_fast), keeping the file's own normals
    from core.obj_fast import LoadCancelled, ObjError, read_obj

    try:
        stats = ModelStats()
        verts, faces, normals = read_obj(file_path, should_stop, stats=stats)
        return _normalize_geometry(
            name=file_path.name, raw_verts=verts, raw_faces=faces, raw_normals=normals, format_type="OBJ",
            metadata=metadata, stats=stats,
        )
    except (LoadCancelled, MemoryError):
        raise
    except ObjError as exc:
        metadata["Quick Reader Note"] = str(exc)  # (what it couldn't follow: trimesh reads it instead)
    except Exception as exc:  # pylint: disable=broad-except
        metadata["Quick Reader Note"] = f"{type(exc).__name__}: {exc}"

    # 2. trimesh
    try:
        trimesh = _trimesh()
        loaded = trimesh.load(str(file_path), force="mesh")
        if isinstance(loaded, trimesh.Scene):
            loaded = loaded.to_geometry() if hasattr(loaded, "to_geometry") else trimesh.util.concatenate(loaded.dump())

        if hasattr(loaded, "vertices") and len(loaded.vertices) > 0:
            normals = getattr(loaded, "vertex_normals", None)
            return _normalize_geometry(
                name=file_path.name,
                raw_verts=np.asarray(loaded.vertices, dtype=np.float32),
                raw_faces=np.asarray(loaded.faces, dtype=np.uint32),
                raw_normals=np.asarray(normals, dtype=np.float32) if normals is not None else None,
                format_type="OBJ",
                metadata=metadata,
            )
    except Exception as exc:
        metadata["Trimesh Load Note"] = str(exc)

    # 3. Pure Python fallback parser
    raw_verts: list[tuple[float, float, float]] = []
    raw_normals: list[tuple[float, float, float]] = []
    faces: list[list[int]] = []

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                tag = parts[0]
                if tag == "v" and len(parts) >= 4:
                    try:
                        raw_verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
                    except ValueError:
                        continue
                elif tag == "vn" and len(parts) >= 4:
                    try:
                        raw_normals.append((float(parts[1]), float(parts[2]), float(parts[3])))
                    except ValueError:
                        continue
                elif tag == "f" and len(parts) >= 4:
                    face_indices = []
                    num_verts = len(raw_verts)
                    for token in parts[1:]:
                        v_str = token.split("/")[0]
                        if v_str:
                            try:
                                v_idx = int(v_str)
                                v_idx = num_verts + v_idx if v_idx < 0 else v_idx - 1
                                face_indices.append(v_idx)
                            except ValueError:
                                continue
                    if len(face_indices) >= 3:
                        # Triangulate polygon fans
                        for i in range(1, len(face_indices) - 1):
                            faces.append([face_indices[0], face_indices[i], face_indices[i + 1]])

        if raw_verts:
            return _normalize_geometry(
                name=file_path.name,
                raw_verts=np.array(raw_verts, dtype=np.float32),
                raw_faces=np.array(faces, dtype=np.uint32) if faces else np.zeros((0, 3), dtype=np.uint32),
                raw_normals=np.array(raw_normals, dtype=np.float32) if len(raw_normals) == len(raw_verts) else None,
                format_type="OBJ",
                metadata=metadata,
            )
    except Exception as exc:
        metadata["Error"] = str(exc)

    mesh = Mesh3D(name=file_path.name, format_type="OBJ")
    mesh.vertices, mesh.faces, mesh.normals = _get_default_preview_cube()
    mesh.stand_in = True
    mesh.metadata = metadata
    return mesh


def parse_gltf_file(file_path: Path) -> Mesh3D:
    """Parse glTF or GLB into normalized Mesh3D geometry."""
    metadata: dict[str, str] = {
        "Format": "glTF / GLB",
        "File Size": f"{file_path.stat().st_size / (1024*1024):.2f} MB",
    }

    try:
        trimesh = _trimesh()
        loaded = trimesh.load(str(file_path), force="mesh")
        if isinstance(loaded, trimesh.Scene):
            loaded = loaded.to_geometry() if hasattr(loaded, "to_geometry") else trimesh.util.concatenate(loaded.dump())

        if hasattr(loaded, "vertices") and len(loaded.vertices) > 0:
            normals = getattr(loaded, "vertex_normals", None)
            return _normalize_geometry(
                name=file_path.name,
                raw_verts=np.asarray(loaded.vertices, dtype=np.float32),
                raw_faces=np.asarray(loaded.faces, dtype=np.uint32),
                raw_normals=np.asarray(normals, dtype=np.float32) if normals is not None else None,
                format_type="glTF",
                metadata=metadata,
            )
    except Exception as exc:
        metadata["Error"] = str(exc)

    mesh = Mesh3D(name=file_path.name, format_type="glTF")
    mesh.vertices, mesh.faces, mesh.normals = _get_default_preview_cube()
    mesh.stand_in = True
    mesh.metadata = metadata
    return mesh


def parse_generic_mesh_file(file_path: Path, format_name: str) -> Mesh3D:
    """Parse generic 3D geometry file (STL, PLY, 3DS, DAE, etc.)."""
    metadata: dict[str, str] = {
        "Format": format_name,
        "File Size": f"{file_path.stat().st_size / (1024*1024):.2f} MB",
    }

    try:
        trimesh = _trimesh()
        loaded = trimesh.load(str(file_path), force="mesh")
        if isinstance(loaded, trimesh.Scene):
            loaded = loaded.to_geometry() if hasattr(loaded, "to_geometry") else trimesh.util.concatenate(loaded.dump())

        if hasattr(loaded, "vertices") and len(loaded.vertices) > 0:
            normals = getattr(loaded, "vertex_normals", None)
            return _normalize_geometry(
                name=file_path.name,
                raw_verts=np.asarray(loaded.vertices, dtype=np.float32),
                raw_faces=np.asarray(loaded.faces, dtype=np.uint32),
                raw_normals=np.asarray(normals, dtype=np.float32) if normals is not None else None,
                format_type=format_name,
                metadata=metadata,
            )
    except Exception as exc:
        metadata["Error"] = str(exc)

    mesh = Mesh3D(name=file_path.name, format_type=format_name)
    mesh.vertices, mesh.faces, mesh.normals = _get_default_preview_cube()
    mesh.stand_in = True
    mesh.metadata = metadata
    return mesh


def load_3d_model(file_path: Path, should_stop: Optional[Callable[[], bool]] = None) -> Mesh3D:
    """Load and parse any 3D model by its extension with fallback. should_stop: asked while a big
    OBJ is read; True stops it (core.obj_fast.LoadCancelled)."""
    ext = file_path.suffix.lower()
    if ext == ".fbx":
        return parse_fbx_file(file_path)
    if ext == ".obj":
        return parse_obj_file(file_path, should_stop)
    if ext in {".gltf", ".glb"}:
        return parse_gltf_file(file_path)
    if ext in {".stl", ".ply", ".3ds", ".dae", ".off"}:
        return parse_generic_mesh_file(file_path, ext.lstrip(".").upper())

    # Fallback
    return parse_generic_mesh_file(file_path, ext.lstrip(".").upper())


def _get_default_preview_cube() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns normalized vertices, faces, and normals for an isometric cube placeholder."""
    verts = np.array(
        [
            [-0.7, -0.7, 0.7],
            [0.7, -0.7, 0.7],
            [-0.7, 0.7, 0.7],
            [0.7, 0.7, 0.7],
            [-0.7, 0.7, -0.7],
            [0.7, 0.7, -0.7],
            [-0.7, -0.7, -0.7],
            [0.7, -0.7, -0.7],
        ],
        dtype=np.float32,
    )

    faces = np.array(
        [
            [0, 1, 3],
            [0, 3, 2],  # Front
            [2, 3, 5],
            [2, 5, 4],  # Top
            [4, 5, 7],
            [4, 7, 6],  # Back
            [6, 7, 1],
            [6, 1, 0],  # Bottom
            [1, 7, 5],
            [1, 5, 3],  # Right
            [6, 0, 2],
            [6, 2, 4],  # Left
        ],
        dtype=np.uint32,
    )

    normals = _compute_vertex_normals(verts, faces)
    return verts, faces, normals
