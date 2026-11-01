"""Custom background tiles for the Theme Studio (ui.theme_studio): patterns built from settings,
and tiles made from an image of your own.

Patterns are worked out per pixel from the tile's own coordinates (lines, grids, dots, waves...
whose counts across the tile are whole numbers), so every one wraps seamlessly. They're drawn
in the theme's texture ink at an opacity, like the built-in textures (ui.backgrounds).

Image tiles are scaled, optionally made seamless (blended with a half-offset copy of
themselves) and either tinted with the ink (light parts show) or kept in their own colours.
seam_score() says how visible the tile's edges are when it repeats.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
from PySide6.QtCore import QByteArray, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF

SHAPES: dict[str, str] = {
    "dots": "Dots",
    "grid": "Grid",
    "lines": "Lines",
    "crosshatch": "Crosshatch",
    "checker": "Checker",
    "hexagon": "Hexagons",
    "waves": "Waves",
    "noise": "Noise",
}
# Directions lines can run in and still wrap: (across, down) steps, whole numbers
DIRECTIONS: dict[str, tuple[int, int]] = {
    "horizontal": (0, 1),
    "vertical": (1, 0),
    "diagonal": (1, 1),
    "diagonal_back": (1, -1),
    "steep": (2, 1),
    "shallow": (1, 2),
}
DIRECTION_LABELS = {
    "horizontal": "Horizontal", "vertical": "Vertical", "diagonal": "Diagonal /", "diagonal_back": "Diagonal \\",
    "steep": "Steep", "shallow": "Shallow",
}
PATTERN_DEFAULTS: dict[str, Any] = {
    "shape": "dots", "cell": 18, "thickness": 1.0, "size": 0.08, "opacity": 12,
    "direction": "diagonal", "stagger": False, "amplitude": 0.25, "contrast": 3.0, "seed": 7,
}
# (low, high) for each number: what a saved theme may hold (ui.theme_pack clamps to these)
PATTERN_RANGES: dict[str, tuple[float, float]] = {
    "cell": (4, 96), "thickness": (0.25, 8.0), "size": (0.02, 0.5), "opacity": (0, 100),
    "amplitude": (0.0, 1.0), "contrast": (1.0, 8.0), "seed": (0, 9999),
}
IMAGE_DEFAULTS: dict[str, Any] = {"mode": "tint", "opacity": 20, "scale": 100, "invert": False, "seamless": False}
IMAGE_RANGES: dict[str, tuple[float, float]] = {"opacity": (0, 100), "scale": (10, 400)}
IMAGE_MODES = {"tint": "Tint with the ink colour", "color": "Keep its own colours"}
MAX_IMAGE_SIDE = 1024  # an imported image is kept at most this big (it's stored in the theme file)
MAX_TILE_SIDE = 512  # a tile is drawn at most this big (at 1x)


def _alpha_image(alpha: np.ndarray, ink: QColor) -> QImage:
    """A tile of the ink colour with this alpha (0-255) per pixel."""
    height, width = alpha.shape
    pixels = np.empty((height, width, 4), np.uint8)  # BGRA in memory = Format_ARGB32 on little-endian
    pixels[..., 0], pixels[..., 1], pixels[..., 2] = ink.blue(), ink.green(), ink.red()
    pixels[..., 3] = np.clip(alpha, 0, 255).astype(np.uint8)
    return QImage(pixels.tobytes(), width, height, 4 * width, QImage.Format.Format_ARGB32).copy()


def _coords(width: int, height: int, scale: int) -> tuple[np.ndarray, np.ndarray]:
    """Each pixel's centre in 1x pixels."""
    xs = (np.arange(width * scale) + 0.5) / scale
    ys = (np.arange(height * scale) + 0.5) / scale
    return np.meshgrid(xs, ys)


def _line_cover(distance: np.ndarray, thickness: float, scale: int) -> np.ndarray:
    """How much of each pixel a line of this thickness covers (distance in 1x pixels)."""
    return np.clip(thickness / 2.0 + 0.5 / scale - distance, 0.0, 1.0 / scale) * scale


def _centred(phase: np.ndarray) -> np.ndarray:
    return np.abs(phase - np.round(phase))  # (0 on a line, 0.5 halfway between)


def _lines(x: np.ndarray, y: np.ndarray, size: int, step: tuple[int, int], thickness: float, scale: int) -> np.ndarray:
    across, down = step
    phase = (across * x + down * y) / size  # (whole numbers of lines across the tile: it wraps)
    spacing = size / math.hypot(across, down)
    return _line_cover(_centred(phase) * spacing, thickness, scale)


def pattern_size(params: dict[str, Any]) -> tuple[int, int]:
    """The tile's size (1x pixels) for these settings."""
    shape, cell = params["shape"], int(round(params["cell"]))
    if shape == "lines":
        across, down = DIRECTIONS[params["direction"]]
        side = max(4, round(cell * math.hypot(across, down)))
        return side, side
    if shape == "crosshatch":
        side = max(4, round(cell * math.sqrt(2)))
        return side, side
    if shape == "dots":
        return cell, cell * (2 if params["stagger"] else 1)
    if shape == "checker":
        return cell * 2, cell * 2
    if shape == "hexagon":
        radius = cell / 2.0
        return max(4, round(math.sqrt(3) * radius)), max(4, round(3 * radius))
    if shape == "waves":
        rows = max(2, min(math.ceil(32 / cell), MAX_TILE_SIDE // cell))  # (whole rows down the tile: it wraps)
        return cell * rows, cell * rows
    if shape == "noise":
        side = min(MAX_TILE_SIDE, max(32, cell * 4))
        return side, side
    return cell, cell  # grid


def render_pattern(params: dict[str, Any], ink: QColor, scale: int = 1) -> QImage:
    """The pattern's tile at 1x / 2x."""
    params = {**PATTERN_DEFAULTS, **params}
    shape = params["shape"]
    width, height = pattern_size(params)
    alpha_max = 255.0 * float(params["opacity"]) / 100.0
    thickness = float(params["thickness"])
    if shape == "hexagon":
        return _hexagons(width, height, thickness, ink, alpha_max, scale)
    x, y = _coords(width, height, scale)
    cell = float(params["cell"])
    if shape == "grid":
        cover = np.maximum(_lines(x, y, width, (1, 0), thickness, scale), _lines(x, y, height, (0, 1), thickness, scale))
    elif shape == "lines":
        cover = _lines(x, y, width, DIRECTIONS[params["direction"]], thickness, scale)
    elif shape == "crosshatch":
        cover = np.maximum(_lines(x, y, width, (1, 1), thickness, scale), _lines(x, y, width, (1, -1), thickness, scale))
    elif shape == "dots":
        radius = max(0.3, float(params["size"]) * cell)
        row = np.floor(y / cell)
        shift = np.where(np.mod(row, 2) == 1, cell / 2.0, 0.0) if params["stagger"] else 0.0
        cx = np.mod(x + shift, cell) - cell / 2.0
        cy = np.mod(y, cell) - cell / 2.0
        cover = np.clip(radius + 0.5 / scale - np.hypot(cx, cy), 0.0, 1.0 / scale) * scale
    elif shape == "checker":
        cover = (np.mod(np.floor(x / cell) + np.floor(y / cell), 2) == 0).astype(float)
    elif shape == "waves":
        phase = y / cell + float(params["amplitude"]) * np.sin(2 * math.pi * x / width)
        cover = _line_cover(_centred(phase) * cell, thickness, scale)
    else:  # noise
        rng = np.random.default_rng(int(params["seed"]))
        noise = rng.random((height, width))
        if scale > 1:
            noise = np.kron(noise, np.ones((scale, scale)))
        cover = noise ** float(params["contrast"])
    return _alpha_image(cover * alpha_max, ink)


def _hexagons(width: int, height: int, thickness: float, ink: QColor, alpha_max: float, scale: int) -> QImage:
    """A honeycomb (pointy-top), each hexagon's corners on the tile's edges so it wraps."""
    image = QImage(width * scale, height * scale, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(scale, scale)
    color = QColor(ink)
    color.setAlpha(round(alpha_max))
    painter.setPen(QPen(color, thickness))
    radius = height / 3.0
    for cx, cy in ((0, 0), (width, 0), (width / 2, height / 2), (0, height), (width, height)):
        painter.drawPolygon(QPolygonF([
            QPointF(cx, cy - radius), QPointF(cx + width / 2, cy - radius / 2), QPointF(cx + width / 2, cy + radius / 2),
            QPointF(cx, cy + radius), QPointF(cx - width / 2, cy + radius / 2), QPointF(cx - width / 2, cy - radius / 2),
        ]))
    painter.end()
    return image


# ------------------ image tiles ------------------
def image_from_png(data: bytes) -> Optional[QImage]:
    image = QImage.fromData(QByteArray(data), "PNG")
    return None if image.isNull() else image.convertToFormat(QImage.Format.Format_ARGB32)


def prepare_import(image: QImage) -> QImage:
    """An image to keep in a theme: at most MAX_IMAGE_SIDE, with alpha."""
    if max(image.width(), image.height()) > MAX_IMAGE_SIDE:
        image = image.scaled(MAX_IMAGE_SIDE, MAX_IMAGE_SIDE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def _pixels(image: QImage) -> np.ndarray:
    """(height, width, 4) floats 0-1, BGRA."""
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    width, height = image.width(), image.height()
    data = np.frombuffer(image.constBits(), np.uint8, count=image.bytesPerLine() * height)
    return data.reshape(height, image.bytesPerLine())[:, : width * 4].reshape(height, width, 4).astype(float) / 255.0


def _from_pixels(pixels: np.ndarray) -> QImage:
    height, width, _ = pixels.shape
    data = np.clip(pixels * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return QImage(data.tobytes(), width, height, 4 * width, QImage.Format.Format_ARGB32).copy()


def make_seamless(image: QImage) -> QImage:
    """Its edges made to match: across, then down, it's blended with a copy of itself moved by half
    its size. The copy shows at the edges (where the original's middle meets itself), the original
    in the middle (where the copy's own seam is)."""
    pixels = _pixels(image)
    for axis in (1, 0):
        length = pixels.shape[axis]
        if length < 4:
            continue
        weight = np.sin(np.pi * np.arange(length) / (length - 1))  # (0 at both edges, 1 in the middle)
        weight = weight[None, :, None] if axis == 1 else weight[:, None, None]
        pixels = pixels * weight + np.roll(pixels, length // 2, axis=axis) * (1.0 - weight)
    return _from_pixels(pixels)


def seam_score(image: QImage) -> float:
    """How much more the tile changes across its edges (where it meets the next copy) than right
    next to them, inside it: about 1 means no visible seam."""
    pixels = _pixels(image)
    if pixels.shape[0] < 3 or pixels.shape[1] < 3:
        return 1.0
    shade = pixels[..., :3].mean(axis=2) * pixels[..., 3]
    across = np.abs(shade[:, 0] - shade[:, -1]).mean() + np.abs(shade[0, :] - shade[-1, :]).mean()
    beside = (
        np.abs(shade[:, 0] - shade[:, 1]).mean() + np.abs(shade[:, -1] - shade[:, -2]).mean()
        + np.abs(shade[0, :] - shade[1, :]).mean() + np.abs(shade[-1, :] - shade[-2, :]).mean()
    ) / 2.0
    return float(across / max(beside, 0.02))  # (a change under 2% of the shade range: not something you see)


def seam_words(score: float) -> str:
    if score < 1.6:
        return "No visible seam"
    if score < 3.0:
        return "A slight seam"
    return "A visible seam: try Make Seamless"


def render_image_tile(source: QImage, params: dict[str, Any], ink: QColor, scale: int = 1) -> QImage:
    """The image as a tile: scaled, made seamless if asked, tinted or in its own colours, at an opacity."""
    params = {**IMAGE_DEFAULTS, **params}
    factor = float(params["scale"]) / 100.0
    width = max(4, min(MAX_TILE_SIDE, round(source.width() * factor)))
    height = max(4, min(MAX_TILE_SIDE, round(source.height() * factor)))
    image = source.scaled(width * scale, height * scale, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    if params["seamless"]:
        image = make_seamless(image)
    pixels = _pixels(image)
    opacity = float(params["opacity"]) / 100.0
    if params["mode"] == "tint":
        light = pixels[..., 2] * 0.299 + pixels[..., 1] * 0.587 + pixels[..., 0] * 0.114
        if params["invert"]:
            light = 1.0 - light
        return _alpha_image(light * pixels[..., 3] * opacity * 255.0, ink)
    if params["invert"]:
        pixels[..., :3] = 1.0 - pixels[..., :3]
    pixels[..., 3] *= opacity
    return _from_pixels(pixels)
