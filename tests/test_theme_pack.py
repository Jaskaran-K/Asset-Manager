"""Theme files (ui.theme_pack) and the background tiles they describe (ui.pattern_tiles)."""

from __future__ import annotations

import json

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter

from ui import pattern_tiles, theme_pack
from ui.themes import THEMES, stylesheet_from

INK = QColor("#ffffff")


def alpha(image: QImage) -> np.ndarray:
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    data = np.frombuffer(image.constBits(), np.uint8, count=image.bytesPerLine() * image.height())
    return data.reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4].reshape(image.height(), image.width(), 4)[..., 3].astype(float)


def wrap_jump(a: np.ndarray) -> float:
    """How much the tile changes across its wrapped edges, compared with inside it."""
    inside = max(np.abs(np.diff(a, axis=1)).max(), np.abs(np.diff(a, axis=0)).max(), 1.0)
    edges = max(np.abs(a[:, 0] - a[:, -1]).max(), np.abs(a[0, :] - a[-1, :]).max())
    return edges / inside


@pytest.mark.parametrize("shape", list(pattern_tiles.SHAPES))
def test_every_pattern_wraps_without_a_seam(qapp, shape):
    for extra in ({}, {"cell": 7, "thickness": 2.5}, {"cell": 33, "stagger": True, "direction": "steep"}):
        params = {**pattern_tiles.PATTERN_DEFAULTS, "shape": shape, "opacity": 60, **extra}
        for scale in (1, 2):
            tile = pattern_tiles.render_pattern(params, INK, scale)
            width, height = pattern_tiles.pattern_size(params)
            assert (tile.width(), tile.height()) == (width * scale, height * scale)
            a = alpha(tile)
            assert a.max() > 0  # (something is drawn)
            if shape != "noise":  # (noise is no smoother inside than across its edges)
                assert wrap_jump(a) <= 1.0 + 1e-6, (shape, extra, scale)


def test_line_directions_all_wrap(qapp):
    for direction in pattern_tiles.DIRECTIONS:
        params = {**pattern_tiles.PATTERN_DEFAULTS, "shape": "lines", "direction": direction, "opacity": 60}
        assert wrap_jump(alpha(pattern_tiles.render_pattern(params, INK))) <= 1.0 + 1e-6, direction


def _photo(width=64, height=48):
    """An image with a hard seam: bright on the left, dark on the right."""
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    for x in range(width):
        shade = round(255 * (1 - x / (width - 1)))
        painter.fillRect(x, 0, 1, height, QColor(shade, shade, shade))
    painter.end()
    return image


def test_making_an_image_seamless_hides_its_seam(qapp):
    photo = _photo()
    before = pattern_tiles.seam_score(photo)
    after = pattern_tiles.seam_score(pattern_tiles.make_seamless(photo))
    assert before > 3.0 and after < 1.6 and pattern_tiles.seam_words(after) == "No visible seam"


def test_an_image_tile_tinted_or_in_its_own_colours(qapp):
    photo = _photo()
    tinted = pattern_tiles.render_image_tile(photo, {"mode": "tint", "opacity": 50, "scale": 50}, QColor("#ff0000"))
    assert (tinted.width(), tinted.height()) == (32, 24)
    left = tinted.pixelColor(1, 10)
    assert left.red() == 255 and left.green() == 0 and 100 < left.alpha() < 140  # (light parts show, at half)
    assert tinted.pixelColor(30, 10).alpha() < 20  # (dark parts don't)
    inverted = pattern_tiles.render_image_tile(photo, {"mode": "tint", "invert": True, "opacity": 100}, INK)
    assert inverted.pixelColor(62, 10).alpha() > 240
    colours = pattern_tiles.render_image_tile(photo, {"mode": "color", "opacity": 100}, INK)
    assert colours.pixelColor(1, 10).red() > 240 and colours.pixelColor(1, 10).alpha() == 255


def test_values_are_checked_strictly(qapp):
    good = ["#fff", "#17181e", "#b81a1b23", "rgba(36, 22, 60, 0.92)", "transparent", "rgba(0,0,0,1)"]
    bad = ["red; } QWidget { background: url(x)", "#12345", "rgb(1,2,3)", "url(evil.png)", "", None, 5, "#fff;"]
    for value in good:
        assert theme_pack.valid_value("border", value), value
    for value in bad:
        assert not theme_pack.valid_value("border", value), value
    gradient = THEMES["dark_purple"]["window_bg"]
    assert theme_pack.valid_value("window_bg", gradient) and theme_pack.valid_value("border", gradient)
    assert not theme_pack.valid_value("text", gradient)  # (surfaces only: text stays one colour)
    assert [color for _at, color in theme_pack.gradient_stops(gradient)] == ["#241338", "#160d26", "#0c0716"]
    assert theme_pack.first_color(gradient) == QColor("#241338")


def test_a_theme_file_round_trips(qapp, tmp_path):
    pack = theme_pack.from_theme("light", "Paper")
    pack.tokens["accent"] = "#ff6600"
    pack.background = {"kind": "pattern", **pattern_tiles.PATTERN_DEFAULTS, "shape": "waves", "cell": 20}
    path = tmp_path / "paper.amtheme"
    theme_pack.save(pack, path)
    loaded, notes = theme_pack.load(path)
    assert notes == [] and loaded.name == "Paper" and loaded.base == "light"
    assert loaded.full_tokens()["accent"] == "#ff6600" and loaded.full_tokens()["text"] == THEMES["light"]["text"]
    assert loaded.background["shape"] == "waves" and loaded.background["cell"] == 20
    # A full stylesheet comes out of it (every token the app's stylesheet needs is there)
    assert "#ff6600" in stylesheet_from(loaded.full_tokens())


def test_an_image_background_round_trips(qapp, tmp_path):
    pack = theme_pack.from_theme("dark")
    pack.background = {"kind": "image", "png": theme_pack.png_text(_photo()), **pattern_tiles.IMAGE_DEFAULTS, "seamless": True}
    path = tmp_path / "photo.amtheme"
    theme_pack.save(pack, path)
    loaded, notes = theme_pack.load(path)
    assert notes == [] and loaded.background["seamless"] is True
    tile = theme_pack.render_background(loaded.background, INK)
    assert tile is not None and tile.width() == 64
    file = theme_pack.tile_file(loaded.background, INK, tmp_path / "tiles")
    assert file.is_file() and file.with_name(file.stem + "@2x.png").is_file()


def test_a_damaged_file_keeps_what_it_can(qapp, tmp_path):
    path = tmp_path / "odd.amtheme"
    data = theme_pack.from_theme("dark", "Odd").to_json()
    data["tokens"]["accent"] = "red; } * { color: red"
    del data["tokens"]["border"]
    data["background"] = {"kind": "pattern", "shape": "spirals", "cell": 9999, "opacity": "lots"}
    path.write_text(json.dumps(data), encoding="utf-8")
    pack, notes = theme_pack.load(path)
    tokens = pack.full_tokens()
    assert tokens["accent"] == THEMES["dark"]["accent"] and tokens["border"] == THEMES["dark"]["border"]
    assert any("accent" in note for note in notes) and any("border" in note for note in notes)
    assert pack.background["shape"] == "dots" and pack.background["cell"] == 96 and pack.background["opacity"] == 12


@pytest.mark.parametrize("content, words", [
    ("not json", "isn't a theme file"),
    (json.dumps({"format": "something-else"}), "made with the Theme Studio"),
    (json.dumps({"format": theme_pack.FORMAT, "version": 99}), "newer version"),
])
def test_files_that_arent_themes_are_refused(qapp, tmp_path, content, words):
    path = tmp_path / "x.amtheme"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(theme_pack.ThemePackError, match=words):
        theme_pack.load(path)


def test_the_python_entry_for_the_app(qapp):
    pack = theme_pack.from_theme("dark", "Deep Ocean")
    pack.tokens["accent"] = "#0ea5e9"
    snippet = theme_pack.python_snippet(pack, theme_pack.theme_id_for(pack.name))
    namespace: dict = {}
    exec("THEMES = {\n" + snippet + "\n}", namespace)  # (it's valid Python, as it would go into ui/themes.py)
    entry = namespace["THEMES"]["deep_ocean"]
    assert entry["label"] == "Deep Ocean" and entry["accent"] == "#0ea5e9"
    assert set(entry) == set(THEMES["dark"])


def test_a_backgrounds_own_tint_is_checked(qapp):
    pattern = {"kind": "pattern", **pattern_tiles.PATTERN_DEFAULTS, "shape": "checker", "opacity": 100}
    assert theme_pack.clean_background({**pattern, "tint": "#f80"})["tint"] == "#ffff8800"
    for bad in ("red", "rgba(1,2,3,1)", "#12; } *", 5):
        assert "tint" not in theme_pack.clean_background({**pattern, "tint": bad}), bad
    tile = theme_pack.render_background({**pattern, "tint": "#ff00ff00"}, INK)
    assert tile.pixelColor(0, 0) == QColor("#00ff00") or tile.pixelColor(tile.width() - 1, 0) == QColor("#00ff00")


@pytest.mark.parametrize("theme_id", list(THEMES))
def test_every_built_in_theme_is_complete_and_valid(qapp, theme_id):
    """Each theme has every token, each a colour the studio accepts (a gradient only on surfaces),
    and it can be chosen in Settings."""
    from core.settings import THEME_IDS

    theme = THEMES[theme_id]
    assert set(theme) == set(THEMES["dark"]) and theme_id in THEME_IDS
    for key in theme_pack.token_keys():
        assert theme_pack.valid_value(key, theme[key]), (theme_id, key)
    stylesheet_from(theme)  # (every token the stylesheet needs is there)
