"""Background textures (Settings > Appearance > Background Texture).

Each texture is a small seamless tile drawn in code, in the theme's texture ink at low
opacity, so it reads as a faint pattern under any theme. Tiles are saved as PNGs (plus
@2x copies, which Qt's stylesheets pick on high-DPI screens) for ui.themes to tile behind
the window, dialogs and the asset grid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen, QPolygonF

from core.settings import TEXTURE_IDS

TEXTURES: dict[str, str] = {
    "none": "None",
    "grain": "Film Grain",
    "dots": "Dots",
    "grid": "Grid",
    "diagonal": "Diagonal Lines",
    "hexagon": "Hexagons",
    "carbon": "Carbon Fiber",
    "topo": "Topographic",
}
assert tuple(TEXTURES) == TEXTURE_IDS, "keep core.settings.TEXTURE_IDS in sync"

TILE_VERSION = 2  # bump when a tile's drawing changes, so cached PNGs are redrawn


def _ink(color: QColor, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def _canvas(width: int, height: int, scale: int) -> tuple[QImage, QPainter]:
    image = QImage(width * scale, height * scale, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(scale, scale)
    return image, painter


def _from_alpha(alpha: np.ndarray, ink: QColor) -> QImage:
    """Tile of the ink color with a per-pixel alpha (0-255)."""
    h, w = alpha.shape
    pixels = np.empty((h, w, 4), np.uint8)  # BGRA in memory = Format_ARGB32 on little-endian
    pixels[..., 0], pixels[..., 1], pixels[..., 2] = ink.blue(), ink.green(), ink.red()
    pixels[..., 3] = np.clip(alpha, 0, 255).astype(np.uint8)
    return QImage(pixels.tobytes(), w, h, 4 * w, QImage.Format.Format_ARGB32).copy()


def _grain(ink: QColor, scale: int) -> QImage:
    n = 128 * scale
    noise = np.random.default_rng(7).random((n, n))
    return _from_alpha(noise ** 3 * 22, ink)


def _dots(ink: QColor, scale: int) -> QImage:
    image, p = _canvas(18, 18, scale)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_ink(ink, 34))
    p.drawEllipse(QPointF(9, 9), 1.2, 1.2)
    p.end()
    return image


def _grid(ink: QColor, scale: int) -> QImage:
    size, cell = 64, 16
    image, p = _canvas(size, size, scale)
    for i in range(0, size, cell):
        p.setPen(QPen(_ink(ink, 22 if i == 0 else 10), 1.0 / scale if i else 1.0))
        p.drawLine(QPointF(i + 0.5 / scale, 0), QPointF(i + 0.5 / scale, size))
        p.drawLine(QPointF(0, i + 0.5 / scale), QPointF(size, i + 0.5 / scale))
    p.end()
    return image


def _diagonal(ink: QColor, scale: int) -> QImage:
    size = 12
    image, p = _canvas(size, size, scale)
    p.setPen(QPen(_ink(ink, 16), 1.0))
    for offset in (-size, 0, size):  # neighbours too, so the line runs through the corners seamlessly
        p.drawLine(QPointF(offset, size), QPointF(offset + size, 0))
    p.end()
    return image


def _hexagon(ink: QColor, scale: int) -> QImage:
    width, height = 28, 48  # a honeycomb repeats every 28 x 48 (pointy-top hexes, radius 16)
    radius = height / 3
    image, p = _canvas(width, height, scale)
    p.setPen(QPen(_ink(ink, 18), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    for cx, cy in ((0, 0), (width, 0), (width / 2, height / 2), (0, height), (width, height)):
        hexagon = QPolygonF([
            QPointF(cx, cy - radius), QPointF(cx + width / 2, cy - radius / 2),
            QPointF(cx + width / 2, cy + radius / 2), QPointF(cx, cy + radius),
            QPointF(cx - width / 2, cy + radius / 2), QPointF(cx - width / 2, cy - radius / 2),
        ])
        p.drawPolygon(hexagon)
    p.end()
    return image


def _carbon(ink: QColor, scale: int) -> QImage:
    cell = 4  # a 2 x 2 weave: each cell shaded along its own direction
    image, p = _canvas(cell * 2, cell * 2, scale)
    p.setPen(Qt.PenStyle.NoPen)
    for row in range(2):
        for col in range(2):
            rect = QRectF(col * cell, row * cell, cell, cell)
            across = (row + col) % 2 == 0
            gradient = QLinearGradient(rect.topLeft(), rect.topRight() if across else rect.bottomLeft())
            gradient.setColorAt(0.0, _ink(ink, 22))
            gradient.setColorAt(1.0, _ink(ink, 3))
            p.setBrush(gradient)
            p.drawRect(rect)
    p.end()
    return image


def _topo(ink: QColor, scale: int) -> QImage:
    size = 256
    n = size * scale
    t = np.arange(n) * (2 * np.pi / n)
    x, y = np.meshgrid(t, t)
    # Integer frequencies only, so the height field (and its contour lines) wraps seamlessly
    field = (
        np.sin(x + 0.6) * np.cos(2 * y) + 0.7 * np.sin(2 * x + y + 1.3)
        + 0.45 * np.cos(3 * x - 2 * y + 0.4) + 0.3 * np.sin(x - 3 * y + 2.1)
    ) * 3.2
    gy = (np.roll(field, -1, 0) - np.roll(field, 1, 0)) / 2
    gx = (np.roll(field, -1, 1) - np.roll(field, 1, 1)) / 2
    slope = np.hypot(gx, gy) + 1e-6
    distance = np.abs(field - np.round(field)) / slope  # pixels to the nearest contour
    line = np.clip(1.0 - distance / (0.75 * scale), 0.0, 1.0)
    return _from_alpha(line * 26, ink)


_PAINTERS: dict[str, Callable[[QColor, int], QImage]] = {
    "grain": _grain,
    "dots": _dots,
    "grid": _grid,
    "diagonal": _diagonal,
    "hexagon": _hexagon,
    "carbon": _carbon,
    "topo": _topo,
}


def render_tile(texture_id: str, ink: QColor, scale: int = 1) -> Optional[QImage]:
    """The texture's tile at 1x / 2x scale; None for "none" (or an unknown id)."""
    paint = _PAINTERS.get(texture_id)
    return paint(ink, scale) if paint else None


def texture_dir() -> Path:
    from core import video_tools  # CACHE_ROOT can be redirected (tests)

    return video_tools.CACHE_ROOT / "textures"


def tile_path(texture_id: str, ink: QColor) -> Optional[Path]:
    """PNG of the tile for the stylesheet (drawn once, then reused); None for no texture."""
    if texture_id not in _PAINTERS:
        return None
    folder = texture_dir()
    name = f"{texture_id}-{ink.name(QColor.NameFormat.HexRgb)[1:]}-v{TILE_VERSION}"
    path = folder / f"{name}.png"
    hidpi = folder / f"{name}@2x.png"
    if not (path.is_file() and hidpi.is_file()):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if not (render_tile(texture_id, ink, 1).save(str(path)) and render_tile(texture_id, ink, 2).save(str(hidpi))):
                return None
        except OSError:
            return None  # no texture is better than a broken stylesheet
    return path
