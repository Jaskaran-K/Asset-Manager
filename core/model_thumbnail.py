"""Pictures of 3D models for the grid, drawn on the CPU (no OpenGL: it runs on worker threads).

Each triangle is covered with points no more than a pixel apart (one for a triangle smaller than a
pixel), the nearest point at each pixel is kept, and it's shaded with the viewer's default look
(core.model_look), from the viewer's starting angle. It's drawn at twice the size and scaled
down, which smooths its edges. A dense model's triangles are mostly smaller than a pixel: for those
its corners (drawn as dots) are enough, so only bigger triangles are filled in.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage

from .model_look import DEFAULT_COLOR, DEFAULT_LIGHTING, DEFAULT_MATERIAL, LIGHTINGS, MATERIALS, shade

CHUNK_POINTS = 4_000_000  # points made at once (memory: about 100 bytes each)
CHUNK_TRIANGLES = 2_000_000  # triangles measured at once
VIEW_ANGLES = (20.0, -35.0)  # (tilt, turn): the 3D viewer's starting view
MARGIN = 0.06  # of the picture, around the model


def _view_rotation(tilt: float, turn: float) -> np.ndarray:
    a, b = math.radians(tilt), math.radians(turn)
    rx = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    ry = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]])
    return rx @ ry


def _grid(k: int) -> np.ndarray:
    """Barycentric weights (G x 3) of points k steps apart along each edge, corners included."""
    i, j = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing="ij")
    keep = i + j <= k
    w1, w2 = i[keep] / k, j[keep] / k
    return np.stack([1 - w1 - w2, w1, w2], axis=1)


class _DepthBuffer:
    """The nearest point at each pixel, and its normal."""

    def __init__(self, res: int):
        self.res = res
        self.nearest = np.full(res * res, -np.inf, dtype=np.float32)
        self.normal = np.zeros((res * res, 3), dtype=np.float32)

    def add(self, xy: np.ndarray, z: np.ndarray, n: np.ndarray) -> None:
        res = self.res
        x, y = xy[:, 0].astype(np.int32), xy[:, 1].astype(np.int32)
        inside = (x >= 0) & (x < res) & (y >= 0) & (y < res)
        pixel, z, n = y[inside] * res + x[inside], z[inside], n[inside]
        order = np.lexsort((-z, pixel))  # by pixel, the nearest (largest z) first
        pixel, z, n = pixel[order], z[order], n[order]
        first = np.ones(len(pixel), dtype=bool)
        first[1:] = pixel[1:] != pixel[:-1]
        pixel, z, n = pixel[first], z[first], n[first]
        nearer = z > self.nearest[pixel]
        self.nearest[pixel[nearer]] = z[nearer]
        self.normal[pixel[nearer]] = n[nearer]


def render_model(vertices: np.ndarray, faces: np.ndarray, normals: Optional[np.ndarray], size: int,
                 color: str = DEFAULT_COLOR, material: str = DEFAULT_MATERIAL, lighting: str = DEFAULT_LIGHTING) -> Optional[QImage]:
    """The model as a size x size picture with a see-through background (None if there's nothing to draw).
    normals: per vertex (worked out if None)."""
    if vertices is None or faces is None or len(vertices) == 0 or len(faces) == 0:
        return None
    if normals is None or len(normals) != len(vertices):
        from .model_loader import _compute_vertex_normals

        normals = _compute_vertex_normals(vertices, faces)
    turn = _view_rotation(*VIEW_ANGLES).astype(np.float32)
    points = np.asarray(vertices, dtype=np.float32) @ turn.T
    view_normals = np.asarray(normals, dtype=np.float32) @ turn.T
    res = size * 2
    low, high = points[:, :2].min(axis=0), points[:, :2].max(axis=0)
    scale = res * (1 - 2 * MARGIN) / max(float((high - low).max()), 1e-9)
    middle = (low + high) / 2
    screen = np.empty((len(points), 2), dtype=np.float32)
    screen[:, 0] = (points[:, 0] - middle[0]) * scale + res / 2
    screen[:, 1] = res / 2 - (points[:, 1] - middle[1]) * scale
    depth = points[:, 2]
    del points

    buffer = _DepthBuffer(res)
    # Every corner, as a dot: all a triangle smaller than a pixel needs
    for start in range(0, len(screen), CHUNK_POINTS):
        part = slice(start, start + CHUNK_POINTS)
        buffer.add(screen[part], depth[part], view_normals[part])
    # Bigger triangles are filled with points a pixel apart, grouped by how many it takes
    faces = np.asarray(faces)
    for start in range(0, len(faces), CHUNK_TRIANGLES):
        tris = faces[start: start + CHUNK_TRIANGLES]
        corners = screen[tris]
        longest = np.maximum.reduce([np.abs(corners[:, a] - corners[:, b]).max(axis=1) for a, b in ((0, 1), (1, 2), (2, 0))])
        big = longest > 1.0
        tris, steps = tris[big], np.minimum(np.ceil(longest[big]), res).astype(np.int32)
        for k in np.unique(steps):
            weights = _grid(int(k)).astype(np.float32)
            group = tris[steps == k]
            per_chunk = max(1, CHUNK_POINTS // len(weights))
            for at in range(0, len(group), per_chunk):
                some = group[at: at + per_chunk]
                buffer.add(
                    np.einsum("gc,tcd->tgd", weights, screen[some]).reshape(-1, 2),
                    np.einsum("gc,tc->tg", weights, depth[some]).reshape(-1),
                    np.einsum("gc,tcd->tgd", weights, view_normals[some]).reshape(-1, 3),
                )

    covered = np.isfinite(buffer.nearest)
    if not covered.any():
        return None
    n = buffer.normal[covered].astype(np.float64)
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    n = np.where(n[:, 2:3] < 0, -n, n)  # (both sides lit, as in the viewer)
    to_eye = np.zeros_like(n)
    to_eye[:, 2] = 1.0  # (straight on: the picture is drawn without perspective)
    base = QColor(color)
    rgb = shade(n, to_eye, (base.redF(), base.greenF(), base.blueF()), MATERIALS[material], LIGHTINGS[lighting])
    pixels = np.zeros((res * res, 4), dtype=np.uint8)
    pixels[covered, :3] = np.round(rgb * 255).astype(np.uint8)
    pixels[covered, 3] = 255
    image = QImage(pixels.tobytes(), res, res, res * 4, QImage.Format.Format_RGBA8888).copy()
    return image.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
