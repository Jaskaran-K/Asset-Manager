"""Background textures: tiles, the stylesheet that uses them, and the setting."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor

from core import settings
from ui import backgrounds
from ui.backgrounds import TEXTURES, render_tile, tile_path
from ui.themes import THEMES, apply_theme, build_stylesheet, current_texture_id


def _alphas(image):
    return {image.pixelColor(x, y).alpha() for x in range(image.width()) for y in range(image.height())}


@pytest.mark.parametrize("texture", [t for t in TEXTURES if t != "none"])
def test_every_texture_draws_a_faint_tile(qapp, texture):
    tile = render_tile(texture, QColor("#ffffff"))
    hidpi = render_tile(texture, QColor("#ffffff"), scale=2)
    assert hidpi.width() == tile.width() * 2 and hidpi.height() == tile.height() * 2
    alphas = _alphas(tile)
    assert max(alphas) > 0  # something is drawn
    assert max(alphas) <= 60  # but only faintly: it's a background
    assert min(alphas) < max(alphas)  # a pattern, not a flat wash


def test_no_texture_has_no_tile(qapp):
    assert render_tile("none", QColor("#ffffff")) is None
    assert tile_path("none", QColor("#ffffff")) is None
    assert "background-image" not in build_stylesheet("dark", "none")


def test_tiles_are_cached_with_a_hidpi_copy(qapp, isolated_user_data):
    path = tile_path("dots", QColor("#ffffff"))
    assert path.is_file() and path.with_name(path.stem + "@2x.png").is_file()
    assert backgrounds.texture_dir() in path.parents  # in the (redirected) cache, not the real one
    assert tile_path("dots", QColor("#ffffff")) == path  # reused


@pytest.mark.parametrize("theme", list(THEMES))
def test_stylesheet_tiles_the_texture(qapp, theme):
    sheet = build_stylesheet(theme, "hexagon")
    assert 'background-image: url("' in sheet
    assert "background-attachment: fixed" in sheet  # the grid's texture doesn't scroll with the cards


def test_apply_theme_keeps_the_texture_unless_told(qapp):
    apply_theme("dark", qapp, "grid")
    apply_theme("light", qapp)
    assert current_texture_id() == "grid"
    assert "background-image" in qapp.styleSheet()
    apply_theme("dark", qapp, "none")
    assert "background-image" not in qapp.styleSheet()


def test_texture_setting(isolated_user_data):
    assert settings.get_background_texture() == "none"
    settings.set_background_texture("topo")
    assert settings.get_background_texture() == "topo"
    settings.set_background_texture("plaid")  # unknown: back to none
    assert settings.get_background_texture() == "none"


def test_one_texture_layer_in_the_main_window(qapp):
    # The main window itself gets no texture: under Dark Purple's see-through grid it showed as a
    # second copy, shifted by the sidebar's width
    sheet = build_stylesheet("dark_purple", "hexagon")
    rule = sheet[sheet.index("background-image") - 200:sheet.index("background-image")]
    selectors = rule.rsplit("}", 1)[-1]
    assert "QMainWindow #assetGrid" in selectors
    assert "QMainWindow," not in selectors
