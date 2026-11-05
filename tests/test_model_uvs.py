"""A model's UV layout (core.model_uvs, ui.uv_view) and metal's roughness (core.model_look)."""

from __future__ import annotations

import os
import time

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from core import model_uvs
from core.model_look import DEFAULT_ROUGHNESS, LIGHTINGS, MATERIALS, shade, with_roughness
from core.model_uvs import build_layout, read_fbx_uvs, read_obj_uvs, read_uvs, tiles_text, udim_number
from core.obj_fast import LoadCancelled
from core.scanner import AssetItem
from core.settings import get_model_view, set_model_view
from tests.test_model_loading import QUAD_AND_PENTAGON, write_fbx
from tests.test_model_viewer import close, drag, open_viewer, pump

QUAD_UVS = np.array([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]], dtype=np.float32)


def edges_of(layout):
    return {tuple(sorted(edge)) for edge in layout.edges.tolist()}


def write_obj(path, text):
    path.write_text(text, encoding="utf-8")
    return path


TWO_QUADS = """v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
v 2 0 0
v 2 1 0
vt 0.1 0.1
vt 0.5 0.1
vt 0.5 0.9
vt 0.1 0.9
vt 0.9 0.1
vt 0.9 0.9
f 1/1 2/2 3/3 4/4
f 2/2 5/5 6/6 3/3
"""


# ----------------------------------------------------------------------
# Building a layout
# ----------------------------------------------------------------------
def test_quads_are_shown_as_quads_with_shared_edges_once():
    layout = read_layout([[0, 1, 2, 3], [1, 4, 5, 2]], np.array([[.1, .1], [.5, .1], [.5, .9], [.1, .9], [.9, .1], [.9, .9]]))
    assert layout.polygons == 2 and layout.edge_count == 7  # (4 + 4, less the one they share: no triangle diagonals)
    assert edges_of(layout) == {(0, 1), (1, 2), (2, 3), (0, 3), (1, 4), (4, 5), (2, 5)}


def read_layout(polygons, uvs):
    corners = np.array([c for polygon in polygons for c in polygon])
    return build_layout(np.asarray(uvs, dtype=np.float32), corners, np.array([len(p) for p in polygons]))


def test_a_layout_knows_its_extent():
    layout = read_layout([[0, 1, 2, 3]], QUAD_UVS)
    assert layout.bounds == pytest.approx((0.1, 0.1, 0.9, 0.9))
    assert layout.mean_edge == pytest.approx(0.8)
    assert layout.cells.tolist() == [[0, 0]] and layout.tiles == [1001]


@pytest.mark.parametrize("u, v, number", [(0, 0, 1001), (1, 0, 1002), (9, 0, 1010), (0, 1, 1011), (3, 2, 1024), (10, 0, None), (-1, 0, None), (0, -1, None)])
def test_udim_numbers(u, v, number):
    assert udim_number(u, v) == number


def test_tiles_come_from_where_polygons_are():
    across = QUAD_UVS + np.array([1.0, 0.0], dtype=np.float32)
    above = QUAD_UVS + np.array([0.0, 1.0], dtype=np.float32)
    layout = read_layout([[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]], np.concatenate([QUAD_UVS, across, above]))
    assert layout.tiles == [1001, 1002, 1011] and tiles_text(layout) == "1001, 1002, 1011"


def test_a_polygon_filling_its_tile_isnt_counted_in_the_next():
    """Its edge lies on u = 1 and v = 1: it's still only in 1001."""
    layout = read_layout([[0, 1, 2, 3]], np.array([[0, 0], [1, 0], [1, 1], [0, 1]]))
    assert layout.tiles == [1001] and layout.outside_tiles == 0


def test_how_tiles_are_described():
    layout = read_layout([[0, 1, 2, 3]], QUAD_UVS - np.array([2.0, 0.0], dtype=np.float32))
    assert layout.tiles == [] and layout.outside_tiles == 1 and tiles_text(layout) == "outside the 0-1 square"
    many = np.concatenate([QUAD_UVS + np.array([i, 0.0], dtype=np.float32) for i in range(8)] + [QUAD_UVS - np.array([3.0, 0.0], dtype=np.float32)])
    layout = read_layout([[i * 4, i * 4 + 1, i * 4 + 2, i * 4 + 3] for i in range(9)], many)
    assert tiles_text(layout) == "1001 ... 1008 (8 tiles), and 1 more outside UDIM"


def test_a_polygon_with_a_corner_missing_is_left_out():
    uvs = np.concatenate([QUAD_UVS, QUAD_UVS + 1])
    layout = build_layout(uvs.astype(np.float32), np.array([0, 1, 2, 3, 4, -1, 6, 99]), np.array([4, 4]))
    assert layout.polygons == 1 and layout.edge_count == 4 and layout.tiles == [1001]


def test_nothing_to_draw_is_no_layout():
    assert build_layout(np.zeros((0, 2), dtype=np.float32), np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)) is None
    assert build_layout(QUAD_UVS, np.array([-1, -1, -1]), np.array([3])) is None


def test_stray_coordinates_dont_break_anything():
    uvs = QUAD_UVS.copy()
    uvs[0] = np.nan
    uvs[1] = np.inf
    layout = read_layout([[0, 1, 2, 3]], uvs)
    assert np.isfinite(layout.uvs).all() and abs(layout.bounds[2]) <= model_uvs.MAX_COORDINATE


def test_an_enormous_spread_of_tiles_is_capped():
    uvs = np.concatenate([QUAD_UVS + np.array([i, 0.0], dtype=np.float32) for i in range(model_uvs.MAX_CELLS + 50)])
    corners = np.arange(len(uvs))
    layout = build_layout(uvs, corners, np.full(len(uvs) // 4, 4))
    assert len(layout.cells) == model_uvs.MAX_CELLS


# ----------------------------------------------------------------------
# OBJ
# ----------------------------------------------------------------------
def test_reading_an_obj(tmp_path):
    layout = read_obj_uvs(write_obj(tmp_path / "m.obj", TWO_QUADS))
    assert layout.polygons == 2 and layout.edge_count == 7 and layout.tiles == [1001]


@pytest.mark.parametrize("corner", ["{v}/{t}/{v}", "{v}/{t}"])
def test_the_ways_an_obj_names_uvs(tmp_path, corner):
    faces = " ".join(corner.format(v=i, t=i) for i in (1, 2, 3, 4))
    text = "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvn 0 0 1\nvn 0 0 1\nvn 0 0 1\nvn 0 0 1\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\n" + f"f {faces}\n"
    layout = read_obj_uvs(write_obj(tmp_path / "m.obj", text))
    assert layout.polygons == 1 and layout.edge_count == 4


def test_three_number_uvs_and_corners_of_different_forms(tmp_path):
    text = "v 0 0 0\nv 1 0 0\nv 1 1 0\nvt 0.1 0.1 0\nvt 0.5 0.1 0\nvt 0.5 0.5 0\nf 1/1 2/2/1 3/3\n"
    layout = read_obj_uvs(write_obj(tmp_path / "m.obj", text))
    assert layout.polygons == 1 and layout.edge_count == 3


def test_relative_uv_numbers(tmp_path):
    text = "v 0 0 0\nv 1 0 0\nv 1 1 0\nvt 0.1 0.1\nvt 0.5 0.1\nvt 0.5 0.5\nf -3/-3 -2/-2 -1/-1\n"
    assert read_obj_uvs(write_obj(tmp_path / "m.obj", text)).polygons == 1


@pytest.mark.parametrize("face", ["f 1 2 3\n", "f 1//1 2//1 3//1\n"])
def test_an_obj_without_uvs(tmp_path, face):
    assert read_uvs(write_obj(tmp_path / "m.obj", f"v 0 0 0\nv 1 0 0\nv 1 1 0\nvn 0 0 1\n{face}")) is None


def test_a_sculpt_without_uvs_isnt_read_to_the_end(tmp_path, monkeypatch):
    """Over a block of points, then faces that name no UVs: there's no need to go through the rest."""
    monkeypatch.setattr(model_uvs, "BLOCK_BYTES", 256)
    path = tmp_path / "sculpt.obj"
    points = "".join(f"v {i} 0 0\n" for i in range(200))
    faces = "".join("f 1 2 3\n" for _ in range(2000))
    path.write_text(points + faces, encoding="utf-8")
    blocks = []
    real = model_uvs._face_vt

    def counted(flat, count):
        blocks.append(count)
        return real(flat, count)

    monkeypatch.setattr(model_uvs, "_face_vt", counted)
    assert read_obj_uvs(path) is None
    assert len(blocks) == 1


def test_reading_can_be_stopped(tmp_path):
    path = write_obj(tmp_path / "m.obj", TWO_QUADS)
    with pytest.raises(LoadCancelled):
        read_uvs(path, should_stop=lambda: True)


def test_a_model_with_faces_before_its_uvs(tmp_path):
    text = "v 0 0 0\nv 1 0 0\nv 1 1 0\nf 1/1 2/2 3/3\nvt 0.1 0.1\nvt 0.5 0.1\nvt 0.5 0.5\n"
    assert read_obj_uvs(write_obj(tmp_path / "m.obj", text)).polygons == 1


# ----------------------------------------------------------------------
# FBX
# ----------------------------------------------------------------------
def uv_layer(name, uvs, index=None, mapping="ByPolygonVertex", reference="IndexToDirect"):
    return name, np.asarray(uvs, dtype=float), None if index is None else np.asarray(index), mapping, reference


POLYGONS = [[0, 1, 2, 3], [4, 5, 6]]


def test_reading_an_fbx(tmp_path):
    uvs = [[.1, .1], [.4, .1], [.4, .4], [.1, .4], [.6, .1], [.9, .1], [.8, .4]]
    path = write_fbx(tmp_path / "m.fbx", [(1, QUAD_AND_PENTAGON, POLYGONS, (0, 0, 0), 0)], uv_sets={1: [uv_layer("map1", uvs, range(7))]})
    layout = read_uvs(path)
    assert layout.polygons == 2 and layout.edge_count == 7 and layout.tiles == [1001] and layout.set_names == ["map1"]


@pytest.mark.parametrize("mapping, reference, index", [
    ("ByPolygonVertex", "Direct", None),
    ("ByControlPoint", "Direct", None),
    ("ByControlPoint", "IndexToDirect", range(7)),
])
def test_the_ways_an_fbx_can_store_uvs(tmp_path, mapping, reference, index):
    uvs = [[.1, .1], [.4, .1], [.4, .4], [.1, .4], [.6, .1], [.9, .1], [.8, .4]]
    path = write_fbx(tmp_path / "m.fbx", [(1, QUAD_AND_PENTAGON, POLYGONS, (0, 0, 0), 0)],
                     uv_sets={1: [uv_layer("map1", uvs, index, mapping, reference)]})
    assert read_fbx_uvs(path).polygons == 2


def test_a_kind_of_mapping_that_makes_no_layout(tmp_path):
    path = write_fbx(tmp_path / "m.fbx", [(1, QUAD_AND_PENTAGON, POLYGONS, (0, 0, 0), 0)],
                     uv_sets={1: [uv_layer("map1", [[.1, .1], [.2, .2]], None, "AllSame", "Direct")]})
    assert read_fbx_uvs(path) is None


def test_every_meshes_uvs_are_shown(tmp_path):
    uvs = [[.1, .1], [.4, .1], [.4, .4], [.1, .4]]
    far = [[1.1, .1], [1.4, .1], [1.4, .4], [1.1, .4]]
    meshes = [(1, QUAD_AND_PENTAGON, [[0, 1, 2, 3]], (0, 0, 0), 0), (3, QUAD_AND_PENTAGON, [[0, 1, 2, 3]], (0, 0, 0), 0)]
    path = write_fbx(tmp_path / "m.fbx", meshes, uv_sets={1: [uv_layer("map1", uvs, range(4))], 3: [uv_layer("map1", far, range(4))]})
    layout = read_uvs(path)
    assert layout.polygons == 2 and layout.tiles == [1001, 1002] and layout.edge_count == 8  # (each mesh's own: not welded)


def test_uv_sets_can_be_chosen(tmp_path):
    first = [[.1, .1], [.4, .1], [.4, .4], [.1, .4]]
    second = [[1.1, .1], [1.4, .1], [1.4, .4], [1.1, .4]]
    path = write_fbx(tmp_path / "m.fbx", [(1, QUAD_AND_PENTAGON, [[0, 1, 2, 3]], (0, 0, 0), 0)],
                     uv_sets={1: [uv_layer("map1", first, range(4)), uv_layer("lightmap", second, range(4))]})
    assert read_uvs(path).set_names == ["map1", "lightmap"] and read_uvs(path).tiles == [1001]
    other = read_uvs(path, set_index=1)
    assert other.tiles == [1002] and other.set_index == 1


def test_an_fbx_without_uvs(tmp_path):
    assert read_uvs(write_fbx(tmp_path / "m.fbx", [(1, QUAD_AND_PENTAGON, POLYGONS, (0, 0, 0), 0)])) is None


def test_a_mesh_that_isnt_in_the_scene_isnt_shown(tmp_path):
    uvs = [[.1, .1], [.4, .1], [.4, .4], [.1, .4]]
    path = write_fbx(tmp_path / "m.fbx", [(1, QUAD_AND_PENTAGON, [[0, 1, 2, 3]], (0, 0, 0), 0)], uv_sets={1: [uv_layer("map1", uvs, range(4))]}, orphan_geometry=True)
    assert read_uvs(path) is None


def test_text_fbx_says_so(tmp_path):
    path = tmp_path / "ascii.fbx"
    path.write_text("; FBX 7.4.0 project file\nVertices: *3 {\n a: 0,0,0\n}\n", encoding="utf-8")
    with pytest.raises(model_uvs.UvError, match="binary"):
        read_uvs(path)


def test_other_formats_go_through_trimesh(tmp_path):
    import trimesh

    vertices = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float)
    faces = np.array([[0, 1, 2], [0, 2, 3]])
    material = trimesh.visual.material.SimpleMaterial()
    mesh = trimesh.Trimesh(vertices, faces, visual=trimesh.visual.TextureVisuals(uv=QUAD_UVS, material=material), process=False)
    path = tmp_path / "m.glb"
    mesh.export(path)
    layout = read_uvs(path)
    assert layout.polygons == 2 and layout.edge_count == 5  # (triangles: glTF has no quads)
    stl = tmp_path / "m.stl"
    trimesh.Trimesh(vertices, faces).export(stl)
    assert read_uvs(stl) is None


# ----------------------------------------------------------------------
# In the viewer
# ----------------------------------------------------------------------
def obj_asset(tmp_path, text=TWO_QUADS, name="m.obj"):
    path = write_obj(tmp_path / name, text)
    return AssetItem(path, path.name, ".obj", os.path.getsize(path), os.path.getmtime(path), "3D Models", "OBJ")


def test_uvs_are_only_read_when_asked_for(qapp, tmp_path, monkeypatch):
    from ui import viewer

    calls = []
    real = viewer.read_uvs

    def counted(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(viewer, "read_uvs", counted)
    dialog = open_viewer(qapp, obj_asset(tmp_path))
    assert pump(qapp, lambda: dialog.viewport.face_count > 0)
    assert calls == [] and dialog.uv_view is None
    dialog.btn_uv.click()
    assert pump(qapp, lambda: dialog.uv_view.layout_shown is not None)
    assert len(calls) == 1
    dialog.btn_uv.click()  # (back, and again: not read twice)
    dialog.btn_uv.click()
    assert len(calls) == 1 and dialog.uv_view.layout_shown is not None
    close(qapp, dialog)


def test_u_switches_between_the_model_and_its_uvs(qapp, tmp_path):
    dialog = open_viewer(qapp, obj_asset(tmp_path))
    assert pump(qapp, lambda: dialog.viewport.face_count > 0)
    dialog.viewport.setFocus()
    QTest.keyClick(dialog.viewport, Qt.Key.Key_U)
    assert dialog.btn_uv.isChecked() and dialog._model_stack.currentWidget() is dialog.uv_view
    assert not dialog.cmb_material.isEnabled() and not dialog.btn_model_color.isEnabled()  # (nothing to do with a flat layout)
    assert pump(qapp, lambda: dialog.uv_view.layout_shown is not None)
    texts = {label.text() for label in dialog.inspector_panel.findChildren(QLabel)}
    assert "UV TILES" in texts and "1001" in texts
    QTest.keyClick(dialog.uv_view, Qt.Key.Key_U)
    assert not dialog.btn_uv.isChecked() and dialog._model_stack.currentWidget() is dialog.viewport
    assert dialog.cmb_material.isEnabled()
    close(qapp, dialog)


def test_a_model_without_uvs_says_so(qapp, tmp_path):
    dialog = open_viewer(qapp, obj_asset(tmp_path, "v 0 0 0\nv 1 0 0\nv 1 1 0\nf 1 2 3\n"))
    dialog.btn_uv.click()
    assert pump(qapp, lambda: "no UVs" in dialog.uv_view.status_text)
    close(qapp, dialog)


def test_uvs_that_cant_be_read_say_so(qapp, tmp_path, monkeypatch):
    from ui import viewer

    def fail(*_args, **_kwargs):
        raise model_uvs.UvError("It's damaged.")

    monkeypatch.setattr(viewer, "read_uvs", fail)
    dialog = open_viewer(qapp, obj_asset(tmp_path))
    dialog.btn_uv.click()
    assert pump(qapp, lambda: "It's damaged." in dialog.uv_view.status_text)
    close(qapp, dialog)


def test_several_uv_sets_can_be_picked_in_the_viewer(qapp, tmp_path):
    first = [[.1, .1], [.4, .1], [.4, .4], [.1, .4]]
    second = [[1.1, .1], [1.4, .1], [1.4, .4], [1.1, .4]]
    path = write_fbx(tmp_path / "m.fbx", [(1, QUAD_AND_PENTAGON, [[0, 1, 2, 3]], (0, 0, 0), 0)],
                     uv_sets={1: [uv_layer("DiffuseUV", first, range(4)), uv_layer("DiffuseUV", second, range(4))]})
    asset = AssetItem(path, path.name, ".fbx", os.path.getsize(path), os.path.getmtime(path), "3D Models", "FBX")
    dialog = open_viewer(qapp, asset)
    dialog.btn_uv.click()
    assert pump(qapp, lambda: dialog.uv_view.layout_shown is not None)
    combo = dialog.cmb_uv_set
    assert not combo.isHidden() and [combo.itemText(i) for i in range(combo.count())] == ["1: DiffuseUV", "2: DiffuseUV"]
    combo.setCurrentIndex(1)
    combo.activated.emit(1)
    assert pump(qapp, lambda: dialog.uv_view.layout_shown is not None and dialog.uv_view.layout_shown.tiles == [1002])
    assert combo.currentIndex() == 1
    dialog.btn_uv.click()
    assert combo.isHidden()  # (back on the model)
    close(qapp, dialog)


def test_one_uv_set_needs_no_picker(qapp, tmp_path):
    dialog = open_viewer(qapp, obj_asset(tmp_path))
    dialog.btn_uv.click()
    assert pump(qapp, lambda: dialog.uv_view.layout_shown is not None)
    assert dialog.cmb_uv_set.isHidden()
    close(qapp, dialog)


def test_slow_uvs_dont_land_on_the_next_file(qapp, tmp_path, monkeypatch):
    from ui import viewer

    real = viewer.read_uvs

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(viewer, "read_uvs", slow)
    dialog = open_viewer(qapp, obj_asset(tmp_path, name="first.obj"))
    dialog.btn_uv.click()
    old_view = dialog.uv_view
    dialog.show_target(viewer.ViewerTarget(obj_asset(tmp_path, name="second.obj")))
    assert dialog.uv_view is None and not dialog.btn_uv.isChecked()  # (the next file starts on its model)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        qapp.processEvents()
    assert dialog.uv_view is None and "UV Tiles" not in dialog._metadata
    assert old_view is not None
    close(qapp, dialog)


def test_an_alphas_3d_view_has_no_uv_button(qapp, tmp_path):
    from PIL import Image

    path = tmp_path / "alpha_brush.png"
    Image.fromarray(np.tile(np.linspace(0, 255, 64, dtype=np.uint8), (64, 1)), "L").save(path)
    asset = AssetItem(path, path.name, ".png", os.path.getsize(path), os.path.getmtime(path), "Textures", "PNG")
    dialog = open_viewer(qapp, asset)
    assert pump(qapp, lambda: not dialog.btn_3d.isHidden())
    dialog.btn_3d.click()
    assert dialog.btn_uv is None
    dialog.setFocus()
    QTest.keyClick(dialog, Qt.Key.Key_U)  # (nothing happens, and nothing breaks)
    close(qapp, dialog)


def wheel(widget, at, up=True):
    return QWheelEvent(QPointF(at), QPointF(widget.mapToGlobal(at)), QPoint(0, 0), QPoint(0, 120 if up else -120),
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)


def shown_view(qapp, tmp_path):
    dialog = open_viewer(qapp, obj_asset(tmp_path))
    dialog.btn_uv.click()
    assert pump(qapp, lambda: dialog.uv_view.layout_shown is not None)
    return dialog, dialog.uv_view


def test_the_layout_is_framed_with_the_0_1_square(qapp, tmp_path):
    dialog, view = shown_view(qapp, tmp_path)
    for u, v in ((0, 0), (1, 1)):
        at = view.to_screen(u, v)
        assert 0 <= at.x() <= view.width() and 0 <= at.y() <= view.height()
    assert view.to_screen(0, 0).y() > view.to_screen(0, 1).y()  # (v goes up)
    close(qapp, dialog)


def test_zooming_keeps_the_uv_under_the_pointer(qapp, tmp_path):
    dialog, view = shown_view(qapp, tmp_path)
    at = QPoint(200, 120)
    u = view.center_x + (at.x() - view.width() / 2) / view.pixels_per_unit
    v = view.center_y - (at.y() - view.height() / 2) / view.pixels_per_unit
    before = view.pixels_per_unit
    view.wheelEvent(wheel(view, at))
    assert view.pixels_per_unit > before
    assert view.to_screen(u, v).x() == pytest.approx(at.x()) and view.to_screen(u, v).y() == pytest.approx(at.y())
    close(qapp, dialog)


def test_dragging_moves_the_layout_with_the_pointer(qapp, tmp_path):
    dialog, view = shown_view(qapp, tmp_path)
    start = view.to_screen(0.5, 0.5)
    drag(view, QPoint(100, 100), QPoint(130, 80))
    moved = view.to_screen(0.5, 0.5)
    assert moved.x() == pytest.approx(start.x() + 30) and moved.y() == pytest.approx(start.y() - 20)
    close(qapp, dialog)


def test_r_and_reset_view_fit_the_layout(qapp, tmp_path):
    dialog, view = shown_view(qapp, tmp_path)
    fitted = (view.center_x, view.center_y, view.pixels_per_unit)
    drag(view, QPoint(100, 100), QPoint(160, 160))
    view.wheelEvent(wheel(view, QPoint(50, 50)))
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_R)
    assert (view.center_x, view.center_y, view.pixels_per_unit) == pytest.approx(fitted)
    drag(view, QPoint(100, 100), QPoint(160, 160))
    dialog._fit_uvs()
    assert (view.center_x, view.center_y) == pytest.approx(fitted[:2])
    close(qapp, dialog)


def test_lines_fade_where_edges_are_tiny():
    from ui.uv_view import edge_opacity

    assert edge_opacity(0.1) < edge_opacity(3) < edge_opacity(100)
    assert 0 < edge_opacity(0.0) and edge_opacity(1000) <= 1


def test_the_squares_drawn():
    from ui.uv_view import cell_geometry, cell_label

    fills, lines = cell_geometry(np.array([[1, 0]]))
    assert len(fills) == 12 and len(lines) == 16  # (that square, and always the 0-1 one)
    assert cell_label(0, 0) == "1001" and cell_label(-1, 2) == "-1, 2"


# ----------------------------------------------------------------------
# Metal's roughness
# ----------------------------------------------------------------------
def sphere_points():
    from tests.test_model_loading import _sphere_points

    return _sphere_points()


def test_only_metal_has_roughness():
    for name, material in MATERIALS.items():
        if material.metal == 0:
            assert with_roughness(material, 0.9) is material, name


def test_rougher_metal():
    metal = MATERIALS["metallic"]
    mirror, soft = with_roughness(metal, 0.0), with_roughness(metal, 1.0)
    assert mirror.reflection_blur < soft.reflection_blur and mirror.shininess > soft.shininess
    assert mirror.diffuse < soft.diffuse and mirror.specular > soft.specular and soft.rim == 0
    assert with_roughness(metal, 7.0).roughness == 1.0 and with_roughness(metal, -1.0).roughness == 0.0


def test_metal_isnt_chrome_by_default():
    assert MATERIALS["metallic"].roughness == DEFAULT_ROUGHNESS > 0.2


def test_roughness_softens_highlights_under_a_preset():
    n = sphere_points()
    eye = np.tile([0.0, 0.0, 1.0], (len(n), 1))
    metal = MATERIALS["metallic"]
    mirror = shade(n, eye, (0.6, 0.6, 0.6), with_roughness(metal, 0.0), LIGHTINGS["studio"])
    soft = shade(n, eye, (0.6, 0.6, 0.6), with_roughness(metal, 1.0), LIGHTINGS["studio"])
    assert soft.max() < mirror.max()  # (no sharp, bright peak)
    assert (soft > 0.95).mean() < 0.01  # (and it doesn't glow: a wider highlight is a dimmer one)
    assert not np.allclose(mirror, soft)


def test_the_slider_shows_on_metal_and_is_remembered(qapp, tmp_path):
    dialog = open_viewer(qapp, obj_asset(tmp_path))
    viewport, bar = dialog.viewport, dialog.roughness_bar
    assert bar.isHidden()  # (Phong: nothing to do)
    dialog.cmb_material.setCurrentIndex(dialog.cmb_material.findData("metallic"))
    assert not bar.isHidden() and bar.slider.value() == round(DEFAULT_ROUGHNESS * 100)
    bar.slider.setValue(80)
    assert viewport.roughness == 0.8 and bar.value.text() == "80" and get_model_view()["metal_roughness"] == 0.8
    assert viewport.material().roughness == 0.8 and viewport.material().metal == 1.0
    dialog.cmb_material.setCurrentIndex(dialog.cmb_material.findData("clay"))
    assert bar.isHidden() and viewport.material() is MATERIALS["clay"]
    close(qapp, dialog)
    set_model_view(material="metallic")
    again = open_viewer(qapp, obj_asset(tmp_path))
    assert again.viewport.roughness == 0.8 and again.roughness_bar.slider.value() == 80 and not again.roughness_bar.isHidden()
    close(qapp, again)


def test_the_slider_stays_out_of_the_way_of_uvs(qapp, tmp_path):
    set_model_view(material="metallic")
    dialog = open_viewer(qapp, obj_asset(tmp_path))
    dialog.btn_uv.click()
    assert dialog._look_details.isHidden()
    dialog.btn_uv.click()
    assert not dialog._look_details.isHidden() and not dialog.roughness_bar.isHidden()
    close(qapp, dialog)
