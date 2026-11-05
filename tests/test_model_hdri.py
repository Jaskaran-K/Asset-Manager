"""The 3D viewer with an HDRI as its lighting (core.environment, ui.model_lighting). Without OpenGL
here: this is the controls and what they keep. tests/test_model_shaders.py draws."""

from __future__ import annotations

import os
import time

import numpy as np
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from core import environment
from core.scanner import AssetItem
from core.settings import get_model_view, set_model_view
from tests.test_hdr_image import write_hdr
from tests.test_model_stats import write_obj
from tests.test_model_viewer import close, drag, model_asset, open_viewer, pump


def write_panorama(tmp_path, name="noon.hdr", sun=True):
    pixels = np.full((256, 512, 3), 0.4, dtype=np.float32)
    if sun:
        pixels[40:44, 100:104] = 5000.0
    return write_hdr(tmp_path / name, pixels)


def picks(combo):
    return [combo.itemText(i) for i in range(combo.count()) if combo.itemText(i)]


def choose(dialog, text):
    dialog.cmb_lighting.setCurrentIndex(dialog.cmb_lighting.findText(text))


def hdr_asset(path):
    return AssetItem(path, path.name, path.suffix, os.path.getsize(path), os.path.getmtime(path), "Textures", "HDR")


def viewer_under(qapp, tmp_path, **panorama):
    """A viewer whose remembered HDRI has arrived."""
    set_model_view(hdri=str(write_panorama(tmp_path, **panorama)))
    dialog = open_viewer(qapp, model_asset(tmp_path))
    assert pump(qapp, lambda: dialog.viewport.environment is not None)
    return dialog


def test_the_lighting_list_offers_the_librarys_hdris(qapp, tmp_path):
    noon = write_panorama(tmp_path)
    render = write_hdr(tmp_path / "render.hdr", np.zeros((64, 64, 3), dtype=np.float32))  # (not a panorama)
    library = [model_asset(tmp_path), hdr_asset(noon), hdr_asset(render)]
    dialog = open_viewer(qapp, model_asset(tmp_path), hdri_assets=lambda: library)
    combo = dialog.cmb_lighting
    assert pump(qapp, lambda: combo.findText("noon") >= 0)
    assert picks(combo)[:5] == ["Studio", "Soft", "Dramatic", "Top", "Headlight"]
    assert picks(combo)[5:] == ["noon", "Browse for an HDRI..."]  # (presets, the library's HDRIs, Browse)
    assert combo.currentText() == "Studio"
    close(qapp, dialog)


def test_picking_an_hdri_lights_the_model_with_it(qapp, tmp_path):
    noon = write_panorama(tmp_path)
    dialog = open_viewer(qapp, model_asset(tmp_path), hdri_assets=lambda: [hdr_asset(noon)])
    viewport, combo = dialog.viewport, dialog.cmb_lighting
    bar = dialog.lighting_controls.bar
    assert pump(qapp, lambda: combo.findText("noon") >= 0)
    assert viewport.environment is None and bar.isHidden()
    choose(dialog, "noon")
    assert viewport.hdri_key == str(noon) and "Loading" in viewport.notice  # (the window keeps working meanwhile)
    assert pump(qapp, lambda: viewport.environment is not None)
    assert viewport.environment.name == "noon" and viewport.notice == "" and not bar.isHidden()
    assert get_model_view()["hdri"] == str(noon) and get_model_view()["lighting"] == "studio"  # (thumbnails stay on the preset)
    assert combo.currentText() == "noon"

    choose(dialog, "Soft")  # (back to a preset)
    assert viewport.environment is None and viewport.hdri_key == "" and bar.isHidden()
    assert get_model_view()["hdri"] == "" and get_model_view()["lighting"] == "soft"
    assert viewport.light_yaw == 0  # (the preset's lights aren't turned by how the HDRI was)
    close(qapp, dialog)


def test_an_hdri_named_like_a_preset_says_which_it_is(qapp, tmp_path):
    studio = write_panorama(tmp_path, "Studio.hdr")
    dialog = open_viewer(qapp, model_asset(tmp_path), hdri_assets=lambda: [hdr_asset(studio)])
    combo = dialog.cmb_lighting
    assert pump(qapp, lambda: combo.findText("Studio (HDRI)") >= 0)
    assert combo.currentText() == "Studio"  # (the preset's still the one in use)
    choose(dialog, "Studio (HDRI)")
    assert dialog.viewport.hdri_key == str(studio)
    assert pump(qapp, lambda: dialog.viewport.environment is not None)
    close(qapp, dialog)


def test_an_alphas_3d_view_gets_the_hdri_too(qapp, tmp_path):
    from PIL import Image

    gradient = np.tile(np.linspace(0, 255, 64, dtype=np.uint8), (64, 1))
    path = tmp_path / "alpha_brush.png"
    Image.fromarray(gradient, "L").save(path)
    asset = AssetItem(path, path.name, ".png", os.path.getsize(path), os.path.getmtime(path), "Textures", "PNG")
    set_model_view(hdri=str(write_panorama(tmp_path)))
    dialog = open_viewer(qapp, asset)
    assert pump(qapp, lambda: not dialog.btn_3d.isHidden())
    dialog.btn_3d.click()
    viewport = dialog._alpha_viewport
    assert pump(qapp, lambda: viewport.environment is not None and viewport.face_count > 0)
    assert dialog.cmb_lighting.currentText() == "noon" and not dialog.lighting_controls.bar.isHidden()
    close(qapp, dialog)


def test_the_last_hdri_and_its_settings_are_kept(qapp, tmp_path):
    set_model_view(hdri_exposure=1.5, hdri_backdrop=True)
    dialog = viewer_under(qapp, tmp_path)
    viewport, controls = dialog.viewport, dialog.lighting_controls
    assert dialog.cmb_lighting.currentText() == "noon" and not controls.bar.isHidden()  # (it's listed, library or not)
    assert viewport.exposure_stops == 1.5 and controls.slider_exposure.value() == 15 and controls.lbl_exposure.text() == "+1.5"
    assert viewport.show_backdrop and controls.btn_backdrop.isChecked()
    close(qapp, dialog)


def test_the_hdri_bar_never_opens_a_window_of_its_own(qapp, tmp_path):
    """Shown from the start under a remembered HDRI, before the toolbar is in anything: a widget
    without a parent would be a window."""
    from PySide6.QtWidgets import QApplication

    dialog = viewer_under(qapp, tmp_path)
    bar = dialog.lighting_controls.bar
    assert bar.parent() is not None and bar not in QApplication.topLevelWidgets()
    close(qapp, dialog)


def test_exposure_and_backdrop_controls(qapp, tmp_path):
    dialog = viewer_under(qapp, tmp_path)
    viewport, controls = dialog.viewport, dialog.lighting_controls
    controls.slider_exposure.setValue(-20)
    assert viewport.exposure_stops == -2.0 and controls.lbl_exposure.text() == "-2.0" and get_model_view()["hdri_exposure"] == -2.0
    assert not viewport.show_backdrop  # (off until asked for)
    controls.btn_backdrop.click()
    assert viewport.show_backdrop and get_model_view()["hdri_backdrop"] is True
    viewport.setFocus()
    QTest.keyClick(viewport, Qt.Key.Key_B)
    assert not viewport.show_backdrop and not controls.btn_backdrop.isChecked()
    close(qapp, dialog)


def test_b_does_nothing_under_a_preset(qapp, tmp_path):
    dialog = open_viewer(qapp, model_asset(tmp_path))
    dialog.viewport.setFocus()
    QTest.keyClick(dialog.viewport, Qt.Key.Key_B)
    assert not dialog.viewport.show_backdrop and not get_model_view()["hdri_backdrop"]
    close(qapp, dialog)


def test_an_image_that_cant_light_a_scene_says_so_and_changes_nothing(qapp, tmp_path):
    bake = write_hdr(tmp_path / "bake.hdr", np.zeros((64, 64, 3), dtype=np.float32))
    dialog = open_viewer(qapp, model_asset(tmp_path))
    viewport = dialog.viewport
    viewport.use_hdri(str(bake))
    assert pump(qapp, lambda: "panorama" in viewport.notice)
    assert viewport.hdri_key == "" and viewport.environment is None
    assert dialog.cmb_lighting.currentText() == "Studio" and dialog.lighting_controls.bar.isHidden()
    assert get_model_view()["hdri"] == ""
    close(qapp, dialog)


def test_a_failed_hdri_leaves_the_one_in_use(qapp, tmp_path):
    bake = write_hdr(tmp_path / "bake.hdr", np.zeros((64, 64, 3), dtype=np.float32))
    dialog = viewer_under(qapp, tmp_path)
    viewport = dialog.viewport
    good = viewport.environment
    viewport.use_hdri(str(bake))
    assert pump(qapp, lambda: "panorama" in viewport.notice)
    assert viewport.environment is good and viewport.hdri_key == good.key and dialog.cmb_lighting.currentText() == "noon"
    assert get_model_view()["hdri"] == good.key
    close(qapp, dialog)


def test_a_remembered_hdri_on_an_unplugged_drive_is_kept_for_later(qapp, tmp_path):
    gone = str(tmp_path / "unplugged" / "noon.hdr")
    set_model_view(hdri=gone)
    dialog = open_viewer(qapp, model_asset(tmp_path))
    assert pump(qapp, lambda: "can't be used" in dialog.viewport.notice)
    assert dialog.cmb_lighting.currentText() == "Studio" and get_model_view()["hdri"] == gone
    close(qapp, dialog)


def test_browsing_for_an_hdri(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    noon = write_panorama(tmp_path)
    dialog = open_viewer(qapp, model_asset(tmp_path))
    viewport, combo = dialog.viewport, dialog.cmb_lighting
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    choose(dialog, "Browse for an HDRI...")
    assert combo.currentText() == "Studio" and viewport.hdri_key == ""  # (cancelled: back where it was)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(noon), ""))
    choose(dialog, "Browse for an HDRI...")
    assert pump(qapp, lambda: viewport.environment is not None)
    assert combo.currentText() == "noon" and get_model_view()["hdri"] == str(noon)
    close(qapp, dialog)


def test_a_slow_hdri_doesnt_land_after_another_choice(qapp, tmp_path, monkeypatch):
    noon = write_panorama(tmp_path)
    real, arrived = environment.load, []

    def slow(key):
        time.sleep(0.3)
        arrived.append(key)
        return real(key)

    monkeypatch.setattr(environment, "load", slow)
    dialog = open_viewer(qapp, model_asset(tmp_path))
    viewport = dialog.viewport
    viewport.use_hdri(str(noon))
    choose(dialog, "Top")  # (a preset, while it's being read)
    assert pump(qapp, lambda: arrived)
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        qapp.processEvents()
    assert viewport.environment is None and viewport.hdri_key == "" and viewport.lighting_id == "top"
    assert get_model_view()["hdri"] == ""
    close(qapp, dialog)


def test_shift_drag_turns_an_hdri_round_the_vertical_only(qapp, tmp_path):
    dialog = viewer_under(qapp, tmp_path)
    viewport = dialog.viewport
    start = viewport.light_yaw
    assert start == environment.key_light_yaw(viewport.environment, viewport.rot_y)  # (its key light where it shows the form)
    drag(viewport, QPoint(100, 100), QPoint(140, 60), Qt.KeyboardModifier.ShiftModifier)
    assert viewport.light_yaw == (start + 20) % 360 and viewport.light_pitch == 0  # (its horizon stays level)
    viewport.reset_lights()
    assert viewport.light_yaw == start
    close(qapp, dialog)


def test_orbiting_moves_the_view_of_the_hdri(qapp, tmp_path):
    """Fixed to the scene: orbiting changes what's seen of it, and doesn't turn it."""
    dialog = viewer_under(qapp, tmp_path)
    viewport = dialog.viewport
    yaw, before = viewport.light_yaw, viewport._view_to_env()
    drag(viewport, QPoint(100, 100), QPoint(160, 100))
    assert viewport.light_yaw == yaw and not np.allclose(viewport._view_to_env(), before)
    close(qapp, dialog)


def test_auto_rotate_spins_the_model_in_a_still_hdri(qapp, tmp_path):
    dialog = viewer_under(qapp, tmp_path)
    viewport = dialog.viewport
    before, turned = viewport._view_to_env(), viewport.rot_y
    viewport._on_turntable_tick()
    assert viewport.rot_y != turned  # (the model has turned, ...)
    assert np.allclose(viewport._view_to_env(), before)  # (... what's reflected and behind it hasn't)
    close(qapp, dialog)


def test_auto_rotate_under_a_preset_is_as_it_was(qapp, tmp_path):
    dialog = open_viewer(qapp, model_asset(tmp_path))
    dialog.viewport._on_turntable_tick()
    assert dialog.viewport.light_yaw == 0
    close(qapp, dialog)


def test_the_light_dial_follows_the_hdri(qapp, tmp_path):
    set_model_view(hdri=str(write_panorama(tmp_path, sun=False)))
    dialog = open_viewer(qapp, model_asset(tmp_path))
    viewport = dialog.viewport
    assert viewport._key_light_direction() is not None  # (a preset's, until the HDRI is here)
    assert pump(qapp, lambda: viewport.environment is not None)
    assert viewport._key_light_direction() is None  # (an even sky: no light to point at)
    close(qapp, dialog)


def test_the_viewer_says_quads_on_a_quad_model(qapp, tmp_path):
    path = write_obj(tmp_path / "quads.obj", [(1, 2, 3, 4), (5, 6, 7, 8)])
    asset = AssetItem(path, path.name, ".obj", os.path.getsize(path), os.path.getmtime(path), "3D Models", "OBJ")
    dialog = open_viewer(qapp, asset)
    viewport = dialog.viewport
    assert pump(qapp, lambda: viewport.face_count == 4)
    assert viewport.mesh.stats.summary() == "2 quads"
    close(qapp, dialog)


def test_use_as_viewer_lighting_is_on_hdr_images(qapp, tmp_path):
    from PySide6.QtWidgets import QWidget

    from ui.asset_menu import AssetMenuActions, build_asset_menu

    def noop(*_args):
        pass

    used = []
    actions = AssetMenuActions(noop, noop, noop, noop, noop, use_as_lighting=lambda: used.append(True))
    parent = QWidget()

    class NoArtbook:
        add_send_menu = staticmethod(noop)

    def entries(asset):
        menu = build_asset_menu(parent, asset, [asset], [], actions, NoArtbook())
        return {action.text(): action for action in menu.actions()}

    assert "Use as Viewer Lighting" not in entries(model_asset(tmp_path))
    entries(hdr_asset(write_panorama(tmp_path)))["Use as Viewer Lighting"].trigger()
    assert used == [True]
    parent.deleteLater()
