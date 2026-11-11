"""The 3D viewer (ui.viewer): loading in the background, the model's look and turning the lights."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from core.model_look import DEFAULT_COLOR, DEFAULT_ROUGHNESS
from core.scanner import AssetItem
from core.settings import get_model_view, set_model_view
from tests.test_thumbnail_cache import CUBE_OBJ


def pump(qapp, condition, seconds=15):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    return condition()


def model_asset(tmp_path, name="crate.obj", text=CUBE_OBJ):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return AssetItem(path, name, path.suffix, os.path.getsize(path), os.path.getmtime(path), "3D Models", "OBJ")


def open_viewer(qapp, asset, **kwargs):
    from ui.viewer import AssetViewerDialog

    dialog = AssetViewerDialog(asset, **kwargs)
    dialog.show()
    return dialog


def close(qapp, dialog):
    from PySide6.QtCore import QEvent

    dialog.close()
    dialog.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def drag(widget, start, end, modifiers=Qt.KeyboardModifier.NoModifier):
    """A left-button drag from start to end, as the viewport's mouse handlers get it."""
    def event(kind, at, buttons):
        return QMouseEvent(kind, QPointF(at), QPointF(widget.mapToGlobal(at)), Qt.MouseButton.LeftButton, buttons, modifiers)

    widget.mousePressEvent(event(QMouseEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton))
    widget.mouseMoveEvent(event(QMouseEvent.Type.MouseMove, end, Qt.MouseButton.LeftButton))
    widget.mouseReleaseEvent(event(QMouseEvent.Type.MouseButtonRelease, end, Qt.MouseButton.NoButton))


def test_the_model_loads_in_the_background(qapp, tmp_path):
    viewed = []
    dialog = open_viewer(qapp, model_asset(tmp_path), model_viewed=lambda asset, mesh: viewed.append((asset.name, mesh.face_count)))
    viewport = dialog.viewport
    assert viewport.status_text.startswith("Loading")  # (the window is up before the model is read)
    assert pump(qapp, lambda: viewport.face_count == 12)
    assert viewport.status_text == "" and viewed == [("crate.obj", 12)]
    texts = {label.text() for label in dialog.inspector_panel.findChildren(QLabel)}
    assert {"VERTICES", "POLYGONS", "QUADS", "TRIANGLES", "N-GONS"} <= texts  # (the model's details, once it's read)
    close(qapp, dialog)


def test_a_model_that_cant_be_read_says_so(qapp, tmp_path, monkeypatch):
    from ui import viewer

    def fail(_path, **_kwargs):
        raise MemoryError("not enough memory")

    monkeypatch.setattr(viewer.model_reader, "read_mesh", fail)
    dialog = open_viewer(qapp, model_asset(tmp_path))
    assert pump(qapp, lambda: "couldn't be opened" in dialog.viewport.status_text)
    close(qapp, dialog)


def test_colour_material_and_lighting_are_kept(qapp, tmp_path):
    dialog = open_viewer(qapp, model_asset(tmp_path))
    viewport = dialog.viewport
    assert viewport.color == QColor(DEFAULT_COLOR) and viewport.material_id == "phong" and viewport.lighting_id == "studio"
    dialog.cmb_material.setCurrentIndex(dialog.cmb_material.findData("metallic"))
    dialog.cmb_lighting.setCurrentIndex(dialog.cmb_lighting.findData("dramatic"))
    assert viewport.material_id == "metallic" and viewport.lighting_id == "dramatic"
    dialog.btn_model_color.click()
    from ui.color_popup import ColorPopup

    popup = next(w for w in qapp.topLevelWidgets() if isinstance(w, ColorPopup) and w.isVisible())
    popup.set_color(QColor("#ff8800"), emit=True)  # (dragging in it: the model follows)
    assert viewport.color == QColor("#ff8800") and get_model_view()["color"] == DEFAULT_COLOR  # (kept once it closes)
    popup.close()
    qapp.processEvents()
    assert get_model_view() == {
        "color": "#ff8800", "material": "metallic", "lighting": "dramatic", "thumbnails": True,
        "alpha_relief": True, "alpha_plane": 512, "tile": 2, "hdri": "", "hdri_exposure": 0.0, "hdri_backdrop": False,
        "metal_roughness": DEFAULT_ROUGHNESS,
    }
    close(qapp, dialog)
    again = open_viewer(qapp, model_asset(tmp_path))  # (the next model looks the same)
    assert again.viewport.color == QColor("#ff8800") and again.cmb_material.currentData() == "metallic"
    close(qapp, again)


def test_damaged_settings_fall_back(qapp):
    set_model_view(color="red; drop", material="velvet", lighting=5, thumbnails="yes", alpha_relief=1, alpha_plane=700, tile=9,
                   hdri=7, hdri_exposure=99, hdri_backdrop="yes", metal_roughness=4)
    assert get_model_view() == {
        "color": DEFAULT_COLOR, "material": "phong", "lighting": "studio", "thumbnails": True,
        "alpha_relief": True, "alpha_plane": 512, "tile": 2, "hdri": "", "hdri_exposure": 0.0, "hdri_backdrop": False,
        "metal_roughness": DEFAULT_ROUGHNESS,
    }
    set_model_view(hdri_exposure=True)
    assert get_model_view()["hdri_exposure"] == 0.0  # (not a number, whatever Python says)


def test_dragging_turns_the_model_or_the_lights(qapp, tmp_path):
    dialog = open_viewer(qapp, model_asset(tmp_path))
    viewport = dialog.viewport
    turn = viewport.rot_y
    drag(viewport, QPoint(100, 100), QPoint(140, 100))
    assert viewport.rot_y == turn + 20 and viewport.light_yaw == 0  # (the model)
    drag(viewport, QPoint(100, 100), QPoint(140, 80), Qt.KeyboardModifier.ShiftModifier)
    assert viewport.rot_y == turn + 20 and viewport.light_yaw == 20 and viewport.light_pitch == -10  # (Shift: the lights)
    viewport.setFocus()
    QTest.keyClick(viewport, Qt.Key.Key_L)  # (Turn Lights on)
    assert viewport.drag_lights and dialog.btn_drag_lights.isChecked()
    drag(viewport, QPoint(100, 100), QPoint(120, 100))
    assert viewport.light_yaw == 30 and viewport.rot_y == turn + 20
    dialog.btn_drag_lights.click()
    assert not viewport.drag_lights
    viewport.reset_lights()
    assert viewport.light_yaw == viewport.light_pitch == 0
    close(qapp, dialog)


def test_a_slow_model_doesnt_land_on_the_next_file(qapp, tmp_path, monkeypatch):
    """Moving on to the next file while a model loads: the old one is dropped when it arrives."""
    from ui import viewer
    from core.model_loader import load_3d_model

    first, second = model_asset(tmp_path, "first.obj"), model_asset(tmp_path, "second.obj")
    calls = []

    def slow(path, **kwargs):
        calls.append(path.name)
        if path.name == "first.obj":
            time.sleep(0.5)
        return load_3d_model(path, **kwargs)

    monkeypatch.setattr(viewer.model_reader, "read_mesh", slow)
    dialog = open_viewer(qapp, first)
    old = dialog.viewport
    dialog.show_target(viewer.ViewerTarget(second))
    assert pump(qapp, lambda: dialog.viewport.face_count == 12 and calls == ["first.obj", "second.obj"])
    time.sleep(0.6)
    qapp.processEvents()
    assert dialog.asset.name == "second.obj" and dialog.viewport is not old
    close(qapp, dialog)
