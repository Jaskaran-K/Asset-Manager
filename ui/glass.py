"""Frosted glass: panels whose background is a blurred, tinted copy of what's behind them.

GlassPanel is the reusable widget: whoever owns it hands it the image of what's behind it
(set_backdrop) and it paints that frosted. How frosted is one app-wide setting, Settings >
Appearance > Glass Frost (set_frost): it sets the blur, how milky the tint is and a fine
frosted grain, and every glass panel follows changes to it live.

ui.sidebar uses it for the slide-out sidebar; future panels / windows can use it too.
"""

from __future__ import annotations

import weakref
from typing import Optional

import shiboken6
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QGraphicsBlurEffect, QGraphicsScene, QWidget

from core.settings import DEFAULT_GLASS_FROST
from ui.backgrounds import render_tile
from ui.themes import theme_color

BLUR_SCALE = 0.25  # blur a quarter-size copy: far quicker, and detail is blurred away anyway
MAX_BLUR_RADIUS = 12.0  # at quarter size, so about 48 px on screen at full frost

_frost = DEFAULT_GLASS_FROST / 100.0
_panels: "weakref.WeakSet[GlassPanel]" = weakref.WeakSet()
_grain: dict[str, QPixmap] = {}


def frost() -> float:
    """Current frost strength, 0 (clear glass) to 1 (fully frosted)."""
    return _frost


def set_frost(percent: int) -> None:
    """Change the frost strength (0-100) of every glass panel."""
    global _frost
    _frost = max(0, min(100, int(percent))) / 100.0
    for panel in list(_panels):
        if shiboken6.isValid(panel):
            panel.refresh_frost()
        else:
            _panels.discard(panel)  # its window was closed: the Python side outlived the widget


def blur_radius(strength: float) -> float:
    return MAX_BLUR_RADIUS * strength


def tint_color(strength: float) -> QColor:
    """The theme's glass tint, more see-through the clearer the glass."""
    tint = theme_color("glass_tint")
    tint.setAlphaF(tint.alphaF() * (0.35 + 0.65 * strength))
    return tint


def frosted(image: QImage, radius: float, scale: float = BLUR_SCALE) -> QImage:
    """Blurred copy of image at the same size."""
    if radius <= 0.0:
        return QImage(image)
    w, h = max(1, round(image.width() * scale)), max(1, round(image.height() * scale))
    small = image.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    # Blur with a margin of stretched edge pixels, so the edges don't fade toward transparent
    pad = int(radius * 2) + 1
    canvas = QImage(w + 2 * pad, h + 2 * pad, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage(QRectF(canvas.rect()), small)
    painter.drawImage(pad, pad, small)
    painter.end()

    scene = QGraphicsScene()
    item = scene.addPixmap(QPixmap.fromImage(canvas))
    blur = QGraphicsBlurEffect()
    blur.setBlurRadius(radius)
    blur.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(blur)
    out = QImage(canvas.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    scene.render(painter, QRectF(out.rect()), QRectF(canvas.rect()))
    painter.end()
    return out.copy(pad, pad, w, h).scaled(
        image.width(), image.height(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )


def _grain_pixmap() -> QPixmap:
    """Fine speckle in the theme's ink: what makes glass look frosted even over a plain background."""
    ink = theme_color("texture_ink")
    key = ink.name()
    if key not in _grain:
        _grain[key] = QPixmap.fromImage(render_tile("grain", ink))
    return _grain[key]


class GlassPanel(QWidget):
    """Paints a frosted backdrop, the glass tint, a sheen and an edge highlight; children go on top."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        # Paints every pixel itself, so the widgets beneath needn't repaint whenever the panel does
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._source: Optional[QImage] = None
        self.backdrop: Optional[QPixmap] = None  # the source, frosted
        self.offset_x = 0  # draw the backdrop shifted by this (a sliding panel's backdrop stays put)
        _panels.add(self)

    def set_backdrop(self, image: QImage) -> None:
        """What's behind the panel (unblurred); the panel frosts it."""
        self._source = image
        self.refresh_frost()

    def refresh_frost(self) -> None:
        if self._source is not None:
            self.backdrop = QPixmap.fromImage(frosted(self._source, blur_radius(_frost)))
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().window())
        if self.backdrop is not None:
            painter.drawPixmap(-self.offset_x, 0, self.backdrop)
        painter.fillRect(self.rect(), tint_color(_frost))
        if _frost > 0.0:
            painter.setOpacity(0.25 + 0.75 * _frost)
            painter.fillRect(self.rect(), QBrush(_grain_pixmap()))
            painter.setOpacity(1.0)
        # Sheen toward the top, like light catching the glass
        sheen = QLinearGradient(0, 0, 0, self.height())
        sheen.setColorAt(0.0, QColor(255, 255, 255, round(8 + 18 * _frost)))
        sheen.setColorAt(0.4, QColor(255, 255, 255, 0))
        painter.fillRect(self.rect(), sheen)
        painter.setPen(theme_color("glass_edge"))
        painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
