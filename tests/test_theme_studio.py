"""The Theme Studio window (ui.theme_studio): editing colours and backgrounds, the live preview,
and saving / opening theme files."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QMessageBox

from ui import theme_pack, theme_studio
from ui.themes import THEMES


@pytest.fixture
def studio(qapp, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: shown.append(("information", args[2])))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: shown.append(("warning", args[2])))
    window = theme_studio.ThemeStudio()
    window.shown = shown
    window.resize(1300, 800)
    window.show()
    qapp.processEvents()
    yield window
    window.dirty = False
    window.close()
    window.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_every_token_has_a_field_and_the_preview_starts_as_the_base(qapp, studio):
    assert set(studio.fields) == set(theme_pack.token_keys())
    assert studio.fields["accent"].value() == THEMES["dark"]["accent"]
    assert THEMES["dark"]["accent"] in studio.preview.styleSheet()
    assert not studio.dirty


def test_typing_a_colour_restyles_the_preview(qapp, studio):
    field = studio.fields["accent"]
    field.edit.setText("#e11d48")
    field.edit.textEdited.emit("#e11d48")
    studio.refresh_preview()
    assert "#e11d48" in studio.preview.styleSheet() and studio.dirty and "*" in studio.windowTitle()
    assert studio.preview.rows.color("accent") == QColor("#e11d48")  # (the sidebar's row highlight too)
    assert field.btn_reset.isEnabled()
    field.reset()
    assert studio.tokens()["accent"] == THEMES["dark"]["accent"] and not field.btn_reset.isEnabled()


def test_a_colour_that_isnt_one_is_not_used(qapp, studio):
    field = studio.fields["border"]
    field.edit.setText("red; } * { color: red")
    field.edit.textEdited.emit(field.edit.text())
    assert studio.tokens()["border"] == THEMES["dark"]["border"] and "ef4444" in field.edit.styleSheet()


def test_contrast_badges(qapp, studio):
    studio.set_token("text_faint", studio.tokens()["surface"])  # (text the colour of what it's on)
    assert studio.fields["text_faint"].contrast.text() == "1.0:1"
    assert theme_studio.contrast_ratio(QColor("#000000"), QColor("#ffffff")) == pytest.approx(21.0)


def test_accent_shades_follow_the_accent(qapp, studio):
    studio.set_token("accent", "#16a34a")
    studio.accent_shades()
    tokens = studio.tokens()
    for key in ("accent_border", "accent_hover", "accent_soft", "accent_text"):
        assert abs(QColor(tokens[key]).hslHue() - QColor("#16a34a").hslHue()) <= 3, key


def test_every_surface_can_be_a_gradient(qapp, studio):
    surfaces = [key for key, _name, _where in theme_studio.TOKEN_GROUPS[0][1]]
    assert all(studio.fields[key].chk_gradient is not None for key in surfaces)
    assert studio.fields["text"].chk_gradient is None  # (text stays one colour)
    field = studio.fields["surface"]
    field.chk_gradient.setChecked(True)
    value = studio.tokens()["surface"]
    assert len(theme_pack.gradient_stops(value)) == 2 and field.gradient_row.isVisibleTo(field)
    assert value in studio.preview.styleSheet() or (studio.refresh_preview() or value in studio.preview.styleSheet())
    field.chk_middle.setChecked(True)
    field.cmb_direction.setCurrentText("Down")
    value = studio.tokens()["surface"]
    assert len(theme_pack.gradient_stops(value)) == 3 and "x2:0, y2:1" in value
    field.chk_gradient.setChecked(False)
    assert theme_pack.valid_value("text", studio.tokens()["surface"]) and not field.gradient_row.isVisibleTo(field)


def test_the_purple_window_gradient_opens_as_a_gradient(qapp, studio):
    studio.load_pack(theme_pack.from_theme("dark_purple"), None)
    field = studio.fields["window_bg"]
    assert field.is_gradient() and field.chk_middle.isChecked() and field.cmb_direction.currentText() == "Diagonal"
    assert [color.name() for color in field.stops] == ["#241338", "#160d26", "#0c0716"]
    assert field.gradient_value() == THEMES["dark_purple"]["window_bg"].replace("stop:0.55", "stop:0.5")


def test_the_colour_picker_changes_the_theme_live(qapp, studio):
    field = studio.fields["accent"]
    field.btn_swatch.click()
    picker = theme_studio._picker
    assert picker is not None and picker.isVisible() and picker.channels is not None
    picker.set_color(QColor("#10b981"), emit=True)  # (as dragging in the square does)
    assert studio.tokens()["accent"] == "#10b981" and field.value() == "#10b981"
    # Number sliders: R, G, B and opacity, in the picker
    picker.channels.boxes[0].setValue(255)
    assert QColor(studio.tokens()["accent"]).red() == 255
    picker.channels.boxes[3].setValue(50)
    assert QColor(studio.tokens()["accent"]).alpha() in (127, 128)
    picker.mode_box.setCurrentText("HSL")
    assert [label.text() for label in picker.channels.labels] == ["H", "S", "L", "A"]
    picker.channels.boxes[2].setValue(20)  # (lightness)
    assert round(QColor(studio.tokens()["accent"]).toHsl().lightnessF() * 100) == 20
    picker.default_picked.emit()  # (its first swatch: back to the base theme's)
    assert studio.tokens()["accent"] == THEMES["dark"]["accent"]
    # One picker at a time: a gradient stop opens its own
    studio.fields["surface"].chk_gradient.setChecked(True)
    studio.fields["surface"].stop_buttons[2].click()
    assert not picker.isVisible() and theme_studio._picker is not picker
    theme_studio._picker.set_color(QColor("#ff0000"), emit=True)
    assert theme_pack.gradient_stops(studio.tokens()["surface"])[-1][1] == "#ff0000"
    theme_studio._picker.close()


def test_the_window_colour_shows_in_the_preview(qapp, studio):
    studio.set_token("window_bg", "#3a1d1d")
    studio.refresh_preview()
    shot = studio.preview.grab().toImage()
    assert shot.pixelColor(4, shot.height() // 2) == QColor("#3a1d1d")  # (the gap beside the main window)
    dialog = studio.preview.findChild(theme_studio.QFrame, "studioDialog")
    corner = dialog.mapTo(studio.preview, dialog.rect().center())
    assert dialog.isVisible() and corner.y() > 0


def test_a_pattern_background_shows_in_the_preview(qapp, studio):
    panel = studio.background
    panel.cmb_kind.setCurrentIndex(panel.cmb_kind.findData("pattern"))
    panel.cmb_shape.setCurrentIndex(panel.cmb_shape.findData("lines"))
    assert not panel.pattern_form.isRowVisible(panel.pattern_controls["size"])  # (only what lines use)
    assert panel.pattern_form.isRowVisible(panel.cmb_direction)
    studio.refresh_preview()
    assert studio.pack.background["shape"] == "lines"
    assert "background-image" in studio.preview.styleSheet() and "Tile:" in panel.lbl_seam.text()


def _seamy_image():
    image = QImage(40, 40, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    for x in range(40):
        shade = 255 - x * 6
        painter.fillRect(x, 0, 1, 40, QColor(shade, shade, shade))
    painter.end()
    return image


def test_an_image_tile_with_its_seam_check(qapp, studio):
    panel = studio.background
    panel.set_image(_seamy_image(), "scan.png")
    assert studio.pack.background["kind"] == "image" and "scan.png" in panel.lbl_image.text()
    assert panel.lbl_seam.text().startswith("A visible seam")
    panel.chk_seamless.setChecked(True)
    assert panel.lbl_seam.text() == "No visible seam" and studio.pack.background["seamless"] is True


def test_saving_and_opening_a_theme(qapp, studio, tmp_path):
    studio.edit_name.setText("Forest")
    studio.edit_name.textEdited.emit("Forest")
    studio.set_token("accent", "#16a34a")
    studio.background.set_image(_seamy_image(), "scan.png")
    path = tmp_path / "forest.amtheme"
    assert studio.save_to(path) and not studio.dirty and "forest.amtheme" in studio.windowTitle()
    other = theme_studio.ThemeStudio()
    try:
        assert other.open_path(path)
        assert other.edit_name.text() == "Forest" and other.tokens()["accent"] == "#16a34a"
        assert other.background.kind() == "image" and other.background.lbl_image.text() == "From the theme file"
        assert not other.dirty
    finally:
        other.close()
        other.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_a_file_that_isnt_a_theme(qapp, studio, tmp_path):
    path = tmp_path / "x.amtheme"
    path.write_text("{}", encoding="utf-8")
    assert not studio.open_path(path)
    assert studio.shown and studio.shown[-1][0] == "warning"


def test_copy_for_the_app(qapp, studio):
    studio.edit_name.setText("Deep Sea")
    studio.edit_name.textEdited.emit("Deep Sea")
    studio.copy_for_app()
    text = qapp.clipboard().text()
    assert text.startswith('    "deep_sea": {') and '"label": "Deep Sea"' in text


def test_closing_with_changes_asks_first(qapp, studio, monkeypatch):
    studio.set_token("accent", "#16a34a")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: asked.append(args) or QMessageBox.StandardButton.Cancel)
    assert not studio.close() and studio.isVisible() and asked
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard)
    assert studio.close()


def test_it_starts_on_its_own(qapp, monkeypatch, tmp_path):
    """main.py --theme-studio: the studio's window, opening a theme file given with it."""
    path = tmp_path / "given.amtheme"
    theme_pack.save(theme_pack.from_theme("light", "Given"), path)
    made = []
    monkeypatch.setattr(type(qapp), "exec", lambda self: 0)
    real = theme_studio.ThemeStudio

    def keep(*args, **kwargs):
        window = real(*args, **kwargs)
        made.append(window)
        return window

    monkeypatch.setattr(theme_studio, "ThemeStudio", keep)
    assert theme_studio.run(["AssetManager.exe", "--theme-studio", str(path)]) == 0
    (window,) = made
    assert window.edit_name.text() == "Given"
    window.close()
    window.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # (or it lives on, restyled by every later test)


def _colours_in(widget) -> set[str]:
    image = widget.grab().toImage()
    return {image.pixelColor(x, y).name() for x in range(0, image.width(), 2) for y in range(0, image.height(), 2)}


def test_accent_text_and_the_second_panels_show_in_the_preview(qapp, studio):
    studio.set_token("surface_alt", "#5a1d6e")
    studio.set_token("accent_text", "#ff00aa")
    studio.refresh_preview()
    toolbar = studio.preview.findChild(theme_studio.QWidget, "viewerToolbar")
    shot = toolbar.grab().toImage()
    assert shot.pixelColor(shot.width() // 3, 2) == QColor("#5a1d6e")  # (a viewer's toolbar: Panels (second))
    assert "#ff00aa" in _colours_in(toolbar)  # (its icons' details and the zoom value)
    header = studio.preview.findChild(theme_studio.QLabel, "inspectorHeader")
    assert "#ff00aa" in _colours_in(header)


def test_icons_follow_the_theme_being_made_not_the_apps(qapp, studio):
    studio.previews.setCurrentWidget(studio.icons_preview)
    studio.set_token("accent", "#00ff00")
    assert studio.previews.currentWidget() is studio.icons_preview  # (it shows the accent: it stays open)
    studio.refresh_preview()
    glyphs = studio.icons_preview.findChildren(theme_studio.DuotoneGlyph)
    assert len(glyphs) > len(theme_studio.ICONS)  # (every icon, and more in other tones)
    image = glyphs[0].grab().toImage()
    greens = [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())]
    assert any(c.green() > 200 and c.red() < 60 and c.blue() < 60 and c.alpha() > 0 for c in greens)


def test_glass_colours_turn_to_the_glass_preview(qapp, studio):
    studio.set_token("glass_tint", "#ccff0000")
    assert studio.previews.currentWidget() is studio.glass_preview
    studio.refresh_preview()
    glass = studio.glass_preview.glass
    assert glass.tokens["glass_tint"] == "#ccff0000" and glass.backdrop is not None
    shot = glass.grab().toImage()
    middle = shot.pixelColor(shot.width() // 2, shot.height() - 10)
    assert middle.red() > 120 and middle.green() < 80  # (tinted red)
    studio.set_token("button", "#123456")  # (not shown here: back to the app)
    assert studio.previews.currentWidget() is studio.preview


def test_a_pattern_or_image_can_have_its_own_tint(qapp, studio):
    panel = studio.background
    panel.cmb_kind.setCurrentIndex(panel.cmb_kind.findData("pattern"))
    panel.cmb_shape.setCurrentIndex(panel.cmb_shape.findData("checker"))
    assert not panel.pattern_tint.btn_swatch.isEnabled()  # (the theme's texture ink until it's on)
    panel.pattern_tint.chk_own.setChecked(True)
    panel.pattern_tint._on_picked(QColor("#ff8800"))
    assert studio.pack.background["tint"] == "#ffff8800"
    tile = theme_pack.render_background(studio.pack.background, QColor("#ffffff"))
    painted = [tile.pixelColor(x, y) for x in range(tile.width()) for y in range(tile.height()) if tile.pixelColor(x, y).alpha()]
    assert painted and all(c.red() == 255 and 0x80 <= c.green() <= 0x90 and c.blue() == 0 for c in painted)
    # An image: its white parts take the tint; with its own colours there's no tint
    panel.set_image(_seamy_image(), "scan.png")
    panel.image_tint.chk_own.setChecked(True)
    panel.image_tint._on_picked(QColor("#00aaff"))
    assert studio.pack.background["tint"] == "#ff00aaff"
    tile = theme_pack.render_background(studio.pack.background, QColor("#ffffff"))
    light = tile.pixelColor(0, 5)
    assert (light.red(), light.green(), light.blue()) == (0, 0xaa, 0xff) and light.alpha() > 0
    panel.cmb_mode.setCurrentIndex(panel.cmb_mode.findData("color"))
    assert "tint" not in studio.pack.background and not panel.image_form.isRowVisible(panel.image_tint)
