"""Look and feel: the stylesheet's drawn pictures (dropdown arrow, tick), readable text in the
built-in themes, title bars in the theme's colours, and the toolbar's shortened path."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog, QMenu

from core import video_tools
from ui import themes, title_bar
from ui.themes import THEMES, apply_theme, build_stylesheet, current_theme_id, parse_color, style_images


@pytest.fixture
def restore_theme(qapp):
    before = current_theme_id()
    yield
    apply_theme(before, qapp)


# ----------------------------------------------------------------------
# The stylesheet's pictures
# ----------------------------------------------------------------------
def test_arrow_and_tick_are_drawn_in_the_themes_colours(qapp):
    images = style_images(THEMES["light"])
    assert set(images) == {"arrow", "arrow_disabled", "tick"}
    for path in images.values():
        assert path.is_file() and path.with_name(path.stem + "@2x.png").is_file()
        assert video_tools.CACHE_ROOT in path.parents  # (the redirected cache, not the real one)
    assert images["arrow"] != images["arrow_disabled"]  # (greyer when disabled)
    assert style_images(THEMES["light"]) == images  # drawn once, then reused
    sheet = build_stylesheet("light")
    assert images["arrow"].as_posix() in sheet and images["tick"].as_posix() in sheet
    assert "QComboBox::drop-down" in sheet


def test_without_the_pictures_qt_draws_those_parts(qapp, monkeypatch):
    monkeypatch.setattr(themes, "_draw_style_image", lambda *_args: _Unsaveable())
    assert style_images(THEMES["dark"]) is None
    sheet = build_stylesheet("dark")
    assert "QComboBox::drop-down" not in sheet and "QCheckBox::indicator:checked { image" not in sheet


class _Unsaveable:
    def save(self, _path):
        return False


# ----------------------------------------------------------------------
# Readable text (WCAG contrast) in the built-in Light and Dark themes
# ----------------------------------------------------------------------
def _luminance(color: QColor) -> float:
    def channel(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(color.redF()) + 0.7152 * channel(color.greenF()) + 0.0722 * channel(color.blueF())


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(parse_color(a)), _luminance(parse_color(b))), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme_id", ["dark", "light"])
@pytest.mark.parametrize("text, needs", [("text", 4.5), ("text_muted", 4.5), ("text_faint", 3.0)])
@pytest.mark.parametrize("background", ["window_bg", "surface", "sidebar_bg", "grid_bg", "card"])
def test_text_is_readable(theme_id, text, needs, background):
    tokens = THEMES[theme_id]
    assert _contrast(tokens[text], tokens[background]) >= needs


def test_faint_text_is_quieter_than_muted_text_and_muted_than_normal():
    for theme_id in ("dark", "light"):
        tokens = THEMES[theme_id]
        on = tokens["surface"]
        assert _contrast(tokens["text"], on) > _contrast(tokens["text_muted"], on) > _contrast(tokens["text_faint"], on)


# ----------------------------------------------------------------------
# Title bars
# ----------------------------------------------------------------------
@pytest.fixture
def dwm(monkeypatch):
    """What each window's title bar was told: {hwnd: {attribute: value}}."""
    calls: dict[int, dict[int, int]] = {}
    monkeypatch.setattr(title_bar, "_native", lambda: True)
    monkeypatch.setattr(title_bar, "_set_attribute", lambda hwnd, attr, value: calls.setdefault(hwnd, {}).update({attr: value}))
    title_bar.install()
    return calls


def _colorref(hex_color: str) -> int:
    c = QColor(hex_color)
    return c.red() | (c.green() << 8) | (c.blue() << 16)


def test_title_bars_take_the_themes_colours(qapp, dwm, restore_theme):
    apply_theme("light", qapp)
    dialog = QDialog()
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    dialog.show()
    qapp.processEvents()
    told = dwm[int(dialog.winId())]
    assert told[title_bar.DWMWA_USE_IMMERSIVE_DARK_MODE] == 0  # light bar in Light Mode
    assert told[title_bar.DWMWA_CAPTION_COLOR] == _colorref(THEMES["light"]["surface"])
    assert told[title_bar.DWMWA_TEXT_COLOR] == _colorref(THEMES["light"]["text_strong"])

    apply_theme("dark", qapp)  # open windows follow a theme change
    told = dwm[int(dialog.winId())]
    assert told[title_bar.DWMWA_USE_IMMERSIVE_DARK_MODE] == 1
    assert told[title_bar.DWMWA_CAPTION_COLOR] == _colorref(THEMES["dark"]["surface"])
    dialog.close()
    dialog.deleteLater()


def test_windows_without_a_title_bar_are_left_alone(qapp, dwm):
    menu = QMenu()
    frameless = QDialog(None, Qt.WindowType.FramelessWindowHint)
    for window in (menu, frameless):
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.show()
        qapp.processEvents()
        assert int(window.winId()) not in dwm
        window.close()
        window.deleteLater()


def test_see_through_and_gradient_bars_are_made_solid(qapp, restore_theme):
    apply_theme("dark_purple", qapp)  # (an rgba() surface)
    bar, _text = title_bar.title_bar_colors()
    assert bar.alpha() == 255 and bar.isValid()
    apply_theme("neon_cyan", qapp)  # (a gradient surface)
    bar, _text = title_bar.title_bar_colors()
    assert bar.alpha() == 255 and bar.isValid()


# ----------------------------------------------------------------------
# The toolbar's path
# ----------------------------------------------------------------------
def test_a_long_path_is_shortened_in_the_middle(qapp):
    from ui.toolbar import PathField

    field = PathField()
    field.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    field.resize(250, 30)
    field.show()  # (a hidden widget gets no resize events)
    field.set_path_text(r"C:\Short")
    assert field.text() == r"C:\Short"
    long_path = "D:\\Assets\\" + "\\".join(f"folder {i}" for i in range(8)) + "\\Library"
    field.set_path_text(long_path)
    assert field.path_text() == long_path
    assert field.text().startswith("D:\\") and field.text().endswith("Library") and "…" in field.text()
    field.resize(field.fontMetrics().horizontalAdvance(long_path) + 80, 30)  # room again: all of it
    qapp.processEvents()
    assert field.text() == long_path
    field.deleteLater()
