"""Frosted glass panels (ui.glass) and the Glass Frost setting."""

from __future__ import annotations

from PySide6.QtGui import QColor, QImage, QPainter

from core import settings
from ui import glass
from ui.glass import GlassPanel, blur_radius, set_frost, tint_color


def stripes():
    image = QImage(120, 120, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#000000"))
    painter = QPainter(image)
    for x in range(0, 120, 20):
        painter.fillRect(x, 0, 10, 120, QColor("#ffffff"))
    painter.end()
    return image


def contrast(pixmap):
    image = pixmap.toImage()
    values = [image.pixelColor(x, 60).lightness() for x in range(30, 90)]
    return max(values) - min(values)


def test_frost_strength_changes_every_panel_live(qapp):
    try:
        panel = GlassPanel()
        panel.set_backdrop(stripes())
        set_frost(0)
        clear = contrast(panel.backdrop)
        set_frost(100)
        frosted = contrast(panel.backdrop)
        assert clear > 200 and frosted < clear / 3  # the stripes blur away as the frost goes up
    finally:
        set_frost(settings.DEFAULT_GLASS_FROST)


def test_more_frost_is_more_milky(qapp):
    assert blur_radius(0.0) == 0.0 < blur_radius(0.5) < blur_radius(1.0)
    assert tint_color(0.0).alpha() < tint_color(1.0).alpha()


def test_frost_is_clamped():
    try:
        set_frost(250)
        assert glass.frost() == 1.0
        set_frost(-5)
        assert glass.frost() == 0.0
    finally:
        set_frost(settings.DEFAULT_GLASS_FROST)


def test_frost_setting(isolated_user_data):
    assert settings.get_glass_frost() == settings.DEFAULT_GLASS_FROST
    settings.set_glass_frost(35)
    assert settings.get_glass_frost() == 35
    settings.set_glass_frost(900)
    assert settings.get_glass_frost() == 100
    settings._update_config("glass_frost", "lots")
    assert settings.get_glass_frost() == settings.DEFAULT_GLASS_FROST
