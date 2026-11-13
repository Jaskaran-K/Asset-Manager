"""Batch 5: texture sets (core.texture_sets), the material preview (core.material_maps, ui.material_view),
materials sent to Blender / Maya (core.app_helpers), batch rename (core.batch_rename) and batch convert
and resize (core.batch_convert)."""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from core import app_helpers, batch_convert, batch_rename, material_maps
from core.scanner import AssetItem, determine_asset_group
from core.tag_repository import TagRepository, default_tag_name
from core.texture_sets import files_of, find_sets, group_into_sets, read_name, set_card


def asset(path: Path, size: int = 1000) -> AssetItem:
    ext = path.suffix.lower()
    return AssetItem(path, path.name, ext, size, 1_700_000_000.0, determine_asset_group(ext), default_tag_name(ext))


def picture(path: Path, color=(200, 120, 80), size=(64, 64), mode="RGB") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color).save(path)
    return path


# ----------------------------------------------------------------------
# Finding sets
# ----------------------------------------------------------------------
@pytest.mark.parametrize("name, expected", [
    ("Body_Base_Color.png", ("color", "Base_Color", "Body", None)),
    ("Body_Normal_OpenGL.png", ("normal", "Normal_OpenGL", "Body", None)),
    ("Body_Mixed_AO.png", ("ao", "Mixed_AO", "Body", None)),
    ("head_BaseColor.1002.png", ("color", "BaseColor", "head", 1002)),
    ("head_Roughness_Raw.1001.png", ("roughness", "Roughness", "head", 1001)),
    ("RockNormal.jpg", ("normal", "Normal", "Rock", None)),
    ("snow-packed12-Base_Color.png", ("color", "Base_Color", "snow-packed12", None)),
    ("crate_ORM.tga", ("packed", "ORM", "crate", None)),
    ("Normal Map from Mesh Shoulder.png", ("normal", "Normal", "Map from Mesh Shoulder", None)),
])
def test_names(name, expected):
    assert read_name(name) == expected


def test_no_map_word():
    assert read_name("dragon_concept.jpg") is None
    assert read_name("Colorado.png") is None  # ("Color" isn't a word on its own here)


def test_sets_are_per_folder_and_need_two_kinds(tmp_path):
    files = [asset(tmp_path / "a" / n) for n in ("Rock_BaseColor.png", "Rock_Normal.png", "Rock_Roughness.png")]
    files += [asset(tmp_path / "b" / n) for n in ("Rock_BaseColor.png", "Rock_Normal.png")]  # (another folder: another set)
    files += [asset(tmp_path / "c" / "Lonely_Normal.png"), asset(tmp_path / "c" / "notes.pdf")]
    sets = find_sets(files)
    assert sorted((s.name, len(s.maps)) for s in sets) == [("Rock", 2), ("Rock", 3)]
    assert sets[0].roles == ["color", "normal", "roughness"]


def test_udim_tiles_make_one_set(tmp_path):
    files = [asset(tmp_path / f"head_{kind}.{tile}.png") for kind in ("BaseColor", "Normal") for tile in (1001, 1002, 1003)]
    files += [asset(tmp_path / f"teeth_Normal.{tile}.png") for tile in (1001, 1002)]  # (one kind, two tiles: a set too)
    sets = {s.name: s for s in find_sets(files)}
    assert sets["head"].tiles == [1001, 1002, 1003] and len(sets["head"].maps) == 6
    assert [m.tile for m in sets["head"].choice("color")] == [1001, 1002, 1003]
    assert sets["teeth"].roles == ["normal"]


def test_preferred_maps_and_green(tmp_path):
    files = [asset(tmp_path / n) for n in ("Body_Base_Color.png", "Body_Normal.png", "Body_Normal_OpenGL.png",
                                          "Body_Ambient_occlusion.png", "Body_Mixed_AO.png")]
    body = find_sets(files)[0]
    assert body.choice("normal")[0].asset.name == "Body_Normal_OpenGL.png"
    assert body.choice("ao")[0].asset.name == "Body_Mixed_AO.png"
    assert not body.flip_green()
    assert body.cover.name == "Body_Base_Color.png"
    only_plain = find_sets([asset(tmp_path / n) for n in ("Rock_Color.png", "Rock_Normal.png")])[0]
    assert not only_plain.flip_green()  # (a plain "Normal" alone: taken as OpenGL)
    directx = find_sets([asset(tmp_path / n) for n in ("Rock_Color.png", "Rock_Normal_DirectX.png")])[0]
    assert directx.flip_green()
    assert "Normal: Body_Normal.png, Body_Normal_OpenGL.png" in body.describe()


def test_height_prefers_displacement_and_the_precise_file(tmp_path):
    # A Megascans surface: Bump is fine grain for shading (it made spikes as height); of the two
    # Displacement files, the EXR is the precise one. Two types of one name are not two tiles.
    files = [asset(tmp_path / n) for n in ("si2ncdkb_4K_Albedo.jpg", "si2ncdkb_4K_Bump.jpg", "si2ncdkb_4K_Displacement.exr",
                                          "si2ncdkb_4K_Displacement.jpg", "si2ncdkb_4K_Normal.jpg")]
    brick = find_sets(files)[0]
    assert [m.asset.name for m in brick.choice("height")] == ["si2ncdkb_4K_Displacement.exr"]
    assert material_maps.map_choices(brick)["height"].asset.name == "si2ncdkb_4K_Displacement.exr"
    left_out = frozenset({str(files[2].path)})
    assert material_maps.map_choices(brick, None, left_out)["height"].asset.name == "si2ncdkb_4K_Displacement.jpg"
    only_bump = frozenset({str(files[2].path), str(files[3].path)})
    assert material_maps.map_choices(brick, None, only_bump)["height"].asset.name == "si2ncdkb_4K_Bump.jpg"
    png_or_jpg = find_sets([asset(tmp_path / n) for n in ("Rock_Color.jpg", "Rock_Color.png", "Rock_Normal.png")])[0]
    assert [m.asset.name for m in png_or_jpg.choice("color")] == ["Rock_Color.png"]


def test_grid_cards_and_files(tmp_path):
    files = [asset(tmp_path / n) for n in ("a.png", "Rock_Color.png", "b.png", "Rock_Normal.png")]
    sets = find_sets(files)
    shown = group_into_sets(files, sets)
    assert [a.name for a in shown] == ["a.png", "Rock", "b.png"]
    card = shown[1]
    assert card.texture_set is sets[0] and str(card.path).endswith("|set")
    assert [a.name for a in files_of(shown)] == ["a.png", "Rock_Color.png", "Rock_Normal.png", "b.png"]
    # A search that found only the normal map still shows the set
    assert [a.name for a in group_into_sets([files[3]], sets)] == ["Rock"]
    assert set_card(sets[0]).size_bytes == 2000


# ----------------------------------------------------------------------
# Batch rename
# ----------------------------------------------------------------------
def test_rename_patterns(tmp_path):
    files = [asset(tmp_path / n) for n in ("Body_Base_Color.png", "Body_Normal_OpenGL.png", "head_Roughness.1002.png")]
    sets_of = {str(a.path): s for s in find_sets(files[:2]) for a in s.assets}
    options = batch_rename.RenameOptions("{set}_{map}", map_style="short")
    assert [batch_rename.new_name(a, options, n, sets_of.get(str(a.path))) for n, a in enumerate(files, 1)] == [
        "Body_BC.png", "Body_N_OpenGL.png", "head_R.1002.png",  # (the variant and the UDIM tile kept)
    ]
    options = batch_rename.RenameOptions("Prop_{n:3}", find="prop", replace="Hero", case="upper", start=7)
    assert batch_rename.new_name(files[0], options, 7) == "HERO_007.png"
    assert batch_rename.new_name(asset(tmp_path / "Folder" / "x.png"), batch_rename.RenameOptions("{folder}-{name}"), 1) == "Folder-x.png"


def test_rename_plan_problems(tmp_path):
    a, b = picture(tmp_path / "a.png"), picture(tmp_path / "b.png")
    picture(tmp_path / "taken.png")
    files = [asset(a), asset(b)]
    clash = batch_rename.plan(files, batch_rename.RenameOptions("same"))
    assert all("2 files would get this name" in row.problem for row in clash.rows)
    taken = batch_rename.plan(files[:1], batch_rename.RenameOptions("taken"))
    assert taken.rows[0].problem.startswith("A file with that name")
    bad = batch_rename.plan(files[:1], batch_rename.RenameOptions("a:b"))
    assert "can't have" in bad.rows[0].problem
    reserved = batch_rename.plan(files[:1], batch_rename.RenameOptions("con"))
    assert "Windows" in reserved.rows[0].problem
    swap = batch_rename.plan(files, batch_rename.RenameOptions("{n}"))  # a -> 1, b -> 2: fine
    assert not swap.problems and len(swap.renames) == 2


def test_rename_swaps_names_and_undoes(tmp_path):
    tmp_path = tmp_path / "files"
    a, b = picture(tmp_path / "a.png", (255, 0, 0)), picture(tmp_path / "b.png", (0, 0, 255))
    rows = [batch_rename.RenameRow(asset(a), "b.png"), batch_rename.RenameRow(asset(b), "a.png")]
    done = batch_rename.apply(rows)
    assert len(done) == 2
    assert Image.open(tmp_path / "a.png").getpixel((0, 0)) == (0, 0, 255)  # (swapped)
    back = batch_rename.undo(done)
    assert len(back) == 2 and Image.open(tmp_path / "a.png").getpixel((0, 0)) == (255, 0, 0)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.png", "b.png"]  # (no temporary names left)


def test_renamed_files_keep_their_tags(tmp_path):
    repo = TagRepository(tmp_path / "lib.db")
    old = asset(picture(tmp_path / "rock.png"))
    ids = repo.sync_assets([old])
    tag = repo.create_custom_tag("Hero")
    repo.add_tags([ids[str(old.path)]], [tag.id])
    done = batch_rename.apply([batch_rename.RenameRow(old, "Hero_Rock.png")])
    assert repo.move_asset_paths(done) == 1
    new = asset(tmp_path / "Hero_Rock.png")
    ids = repo.sync_assets([new])
    assert {t.name for t in repo.tags_by_asset(list(ids.values()))[ids[str(new.path)]]} >= {"Hero"}


# ----------------------------------------------------------------------
# Batch convert and resize
# ----------------------------------------------------------------------
def test_sizes():
    assert batch_convert.target_size(4096, 2048, 2048, False) == (2048, 1024)
    assert batch_convert.target_size(1000, 500, 2048, False) == (1000, 500)  # (never bigger)
    assert batch_convert.target_size(3000, 1500, 0, True) == (2048, 1024)
    assert batch_convert.size_name(2048) == "2K" and batch_convert.size_name(0) == "Full Size"


def test_convert_plan(tmp_path):
    png = asset(picture(tmp_path / "rock.png"))
    jpg = asset(picture(tmp_path / "rock.jpg"))
    doc = asset(tmp_path / "notes.pdf")
    plan = batch_convert.plan([png, jpg, doc], batch_convert.ConvertOptions("png", 512, subfolder="512"))
    assert plan.rows[0].target == tmp_path / "512" / "rock.png"
    assert plan.rows[1].problem == "Another file would get this name"
    assert plan.rows[2].problem == "Not a picture"
    same_place = batch_convert.plan([png], batch_convert.ConvertOptions("png", folder=str(tmp_path)))
    assert same_place.rows[0].problem == "It would replace the original"
    picture(tmp_path / "512" / "rock.png")
    assert batch_convert.plan([png], batch_convert.ConvertOptions("png", subfolder="512")).rows[0].problem.startswith("Already there")
    assert not batch_convert.plan([png], batch_convert.ConvertOptions("png", subfolder="512", replace=True)).rows[0].problem


def _png_bit_depth(path: Path) -> int:
    data = path.read_bytes()
    return data[24]  # (IHDR: the byte after width and height)


def test_convert_keeps_16_bits_and_drops_alpha_for_jpg(tmp_path):
    heights = (np.mgrid[0:256, 0:256][1] * 200).astype(np.uint16)
    Image.fromarray(heights).save(tmp_path / "Rock_Height.png")
    picture(tmp_path / "leaf.png", (10, 200, 10, 128), (300, 200), "RGBA")
    files = [asset(tmp_path / "Rock_Height.png"), asset(tmp_path / "leaf.png")]
    options = batch_convert.ConvertOptions("png", 128, subfolder="small")
    for row in batch_convert.plan(files, options).jobs:
        batch_convert.convert(row, options)
    height = Image.open(tmp_path / "small" / "Rock_Height.png")
    assert height.size == (128, 128) and _png_bit_depth(tmp_path / "small" / "Rock_Height.png") == 16
    assert np.asarray(height).max() > 25000  # (the slope kept, not cut to 8 bits)
    leaf = Image.open(tmp_path / "small" / "leaf.png")
    assert leaf.size == (128, 85) and leaf.mode == "RGBA"
    options = batch_convert.ConvertOptions("jpg", subfolder="jpg")
    row = batch_convert.plan(files[1:], options).jobs[0]
    batch_convert.convert(row, options)
    assert Image.open(row.target).mode == "RGB"


def test_deep_colour_png_is_16_bit(tmp_path):
    pixels = batch_convert.Pixels(np.random.default_rng(1).random((32, 48, 3), dtype=np.float32), True)
    data = batch_convert.encode(pixels, ".png")
    (tmp_path / "deep.png").write_bytes(data)
    assert _png_bit_depth(tmp_path / "deep.png") == 16
    assert Image.open(io.BytesIO(data)).size == (48, 32)


def test_convert_from_an_archive(tmp_path):
    from core import archives
    from zip_maker import DEFLATED, Entry, make_zip

    buffer = io.BytesIO()
    Image.new("RGB", (100, 50), (1, 2, 3)).save(buffer, "PNG")
    archive = make_zip(tmp_path / "pack.zip", [Entry("maps/rock.png", buffer.getvalue(), DEFLATED)])
    archives.forget_listings()
    try:
        inside = archives.entry_assets(archive, [".png"])[0]
        options = batch_convert.ConvertOptions("tga")
        row = batch_convert.plan([inside], options).jobs[0]
        assert row.target == tmp_path / "converted" / "rock.tga"  # (beside the archive)
        batch_convert.convert(row, options)
        assert Image.open(row.target).size == (100, 50)
    finally:
        archives.forget_listings()


# ----------------------------------------------------------------------
# The material preview's data
# ----------------------------------------------------------------------
def test_shapes():
    for name in material_maps.SHAPES:
        shape = material_maps.make_shape(name)
        assert shape.indices.max() < len(shape.positions)
        assert np.allclose(np.linalg.norm(shape.normals, axis=1), 1.0, atol=1e-4)
        assert shape.uvs.min() >= 0 and shape.uvs.max() <= 1
        # Tangents run along +u, at right angles to the normals
        assert np.allclose((shape.tangents[:, :3] * shape.normals).sum(axis=1), 0.0, atol=1e-4)
    # The height map is read blurred to the mesh's spacing (about one pixel per step), and its depth goes with
    # the size of one copy of the map: the same in proportion on every shape and at any tiling
    assert material_maps.height_level(material_maps.SHAPE_STEPS["plane"], 1, "plane") == 0.0
    assert material_maps.height_level(2048, 1, "plane") == pytest.approx(2.0)
    assert material_maps.height_level(2048, 4, "plane") == pytest.approx(4.0)
    assert material_maps.height_depth(0.02, 1, "plane") == pytest.approx(0.04)
    assert material_maps.height_depth(0.02, 2, "plane") == pytest.approx(0.02)


def test_height_maps_are_spread_over_their_range():
    narrow = np.linspace(0.47, 0.53, 10000, dtype=np.float32).reshape(100, 100)  # (a Megascans displacement)
    spread = material_maps.spread_height(narrow)
    assert spread.min() == 0.0 and spread.max() == 1.0 and abs(float(spread.mean()) - 0.5) < 0.01
    flat = np.full((50, 50), 0.5, np.float32)
    assert np.array_equal(material_maps.spread_height(flat), flat)


def test_maps_read_for_the_gpu(tmp_path):
    picture(tmp_path / "Rock_Color.png", (200, 100, 50), (4096, 2048))
    heights = (np.mgrid[0:300, 0:300][0] * 200).astype(np.uint16)
    Image.fromarray(heights).save(tmp_path / "Rock_Height.png")
    picture(tmp_path / "Rock_Normal_DirectX.png", (128, 128, 255))
    files = [asset(tmp_path / n) for n in ("Rock_Color.png", "Rock_Height.png", "Rock_Normal_DirectX.png")]
    rock = find_sets(files)[0]
    maps = material_maps.load_maps(material_maps.map_choices(rock))
    assert maps["color"].pixels.shape == (1024, 2048, 4) and maps["color"].pixels.dtype == np.uint8
    assert maps["height"].pixels.dtype == np.uint16 and maps["height"].pixels.max() > 50000
    assert maps["normal"].pixels.shape[2] == 4
    # A map's kind corrected by hand, and one left out
    choices = material_maps.map_choices(rock, {str(files[1].path): "roughness"}, frozenset({str(files[2].path)}))
    assert sorted(choices) == ["color", "roughness"]


# ----------------------------------------------------------------------
# Materials in Blender / Maya (the helpers)
# ----------------------------------------------------------------------
def helper_namespace(app_id: str) -> dict:
    namespace = {"__name__": "asset_manager_helper"}
    source = app_helpers.helper_source(app_id)
    exec(compile(source.split("\n\nimport threading")[0].split("\n\nimport bpy")[0], "helper", "exec"), namespace)
    return namespace


def test_material_requests_are_checked(tmp_path):
    color, normal = picture(tmp_path / "Rock_Color.png"), picture(tmp_path / "Rock_Normal.png")
    checked = helper_namespace("blender")["checked_material"]
    good = {"name": "Rock", "maps": [{"file": str(color), "role": "color"}, {"file": str(normal), "role": "normal"}],
            "flip_green": True, "shader": "standard"}
    result = checked(good)
    assert [m["role"] for m in result["maps"]] == ["color", "normal"] and result["flip_green"] and not result["arnold"]
    script = tmp_path / "evil.py"
    script.write_text("print('no')")
    for bad in (
        {**good, "maps": [{"file": str(script), "role": "color"}]},  # not an image
        {**good, "maps": [{"file": str(tmp_path / "gone.png"), "role": "color"}]},  # not there
        {**good, "maps": [{"file": str(color), "role": "script"}]},  # not a kind of map
        {**good, "maps": [{"file": str(color), "role": "packed", "channels": ["ao", "os.system", "metal"]}]},
        {"name": 3, "maps": []}, "not a dict",
    ):
        assert checked(bad) is None


def test_material_request_from_a_set(tmp_path):
    files = [asset(picture(tmp_path / n)) for n in ("Crate_BaseColor.png", "Crate_Normal_DirectX.png", "Crate_ORM.png",
                                                   "Crate_Normal.png")]
    crate = find_sets(files)[0]
    request = app_helpers.material_request(crate, {str(a.path): a.path for a in files}, "arnold")
    assert request["name"] == "Crate" and request["shader"] == "arnold"
    roles = {m["role"]: m for m in request["maps"]}
    assert sorted(roles) == ["color", "normal", "packed"]
    assert roles["packed"]["channels"] == ["ao", "roughness", "metal"]
    assert Path(roles["normal"]["file"]).name == "Crate_Normal.png" and not request["flip_green"]


def test_helpers_are_versioned_and_refreshed(tmp_path, monkeypatch):
    monkeypatch.setenv("BLENDER_USER_SCRIPTS", str(tmp_path / "blender"))
    assert "VERSION = %d" % app_helpers.HELPER_VERSION in app_helpers.helper_source("blender")
    path = tmp_path / "blender" / "startup" / app_helpers.HELPER_FILE
    path.parent.mkdir(parents=True)
    path.write_text("# an old helper")
    assert not app_helpers.is_current("blender", None)
    assert app_helpers.refresh("blender", None) and app_helpers.is_current("blender", None)
    assert not app_helpers.refresh("blender", None)  # (up to date)
    # An open window says which version it runs (1: from before versions)
    folder = app_helpers.mailbox("blender")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{app_helpers.ALIVE_PREFIX}{os.getpid()}").write_text("2")
    monkeypatch.setattr(app_helpers, "_PROGRAMS", {"blender": (os.path.basename(sys.executable).lower(),)})
    assert app_helpers.open_window_version("blender") == 2
    (folder / f"{app_helpers.ALIVE_PREFIX}{os.getpid()}").write_text("")
    assert app_helpers.open_window_version("blender") == 1
    app_helpers.send_material("blender", {"name": "x", "maps": []})
    requests = [p for p in folder.iterdir() if p.name.endswith(app_helpers.REQUEST_SUFFIX)]
    assert json.loads(requests[0].read_text())["material"]["name"] == "x"


# ----------------------------------------------------------------------
# The window's parts
# ----------------------------------------------------------------------
def test_rename_dialog_preview(qapp, tmp_path):
    from ui.batch_dialogs import RenameDialog

    files = [asset(picture(tmp_path / n)) for n in ("Rock_Color.png", "Rock_Normal.png", "readme.png")]
    sets_of = {str(a.path): s for s in find_sets(files) for a in s.assets}
    dialog = RenameDialog(files, sets_of)
    assert dialog.txt_pattern.text() == "{set}_{map}"
    dialog.cmb_map.setCurrentIndex(dialog.cmb_map.findData("substance"))
    dialog.update_preview()
    assert [dialog.tree.topLevelItem(i).text(1) for i in range(3)] == ["Rock_BaseColor.png", "Rock_Normal.png", "readme.png"]
    assert len(dialog.plan().renames) == 1 and dialog.btn_ok.isEnabled()
    dialog.deleteLater()


def test_convert_dialog_makes_files(qapp, tmp_path):
    import time

    from ui.batch_dialogs import ConvertDialog

    files = [asset(picture(tmp_path / f"p{n}.png", size=(300, 300))) for n in range(3)]
    dialog = ConvertDialog(files)
    dialog.cmb_format.setCurrentIndex(dialog.cmb_format.findData("jpg"))
    dialog.cmb_size.setCurrentIndex(dialog.cmb_size.findData(512))
    dialog.update_preview()
    assert dialog.txt_sub.text() == "512 px"
    dialog._start()
    deadline = time.monotonic() + 20
    while dialog._running and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert len(dialog.created) == 3 and all(p.exists() and p.suffix == ".jpg" for p in dialog.created)
    assert "Made 3 new files" in dialog.lbl_summary.text()
    dialog.deleteLater()


def test_menu_for_a_set(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QWidget

    from ui.asset_menu import ArtbookSender, AssetMenuActions, build_asset_menu

    monkeypatch.setattr(ArtbookSender, "add_send_menu", lambda self, menu, paths, resolve=None: None)

    files = [asset(tmp_path / n) for n in ("Rock_Color.png", "Rock_Normal.png")]
    card = set_card(find_sets(files)[0])
    sent = []
    actions = AssetMenuActions(
        open_viewer=lambda: sent.append("open"), reveal=lambda: None, tag=lambda: None, toggle_favorite=lambda: None,
        set_rating=lambda r: None, rename=lambda: sent.append("rename"), convert=lambda: sent.append("convert"),
        send_material=sent.append,
    )
    parent = QWidget()
    menu = build_asset_menu(parent, card, files, [], actions, ArtbookSender(None, sent.append))
    texts = {a.text(): a for a in menu.actions()}
    assert "Preview Material" in texts and "Open with External App" not in texts
    texts["Send to Maya as Material"].trigger()
    texts["Rename 2 Files..."].trigger()
    texts["Convert and Resize..."].trigger()
    assert sent == ["maya", "rename", "convert"]
    parent.deleteLater()


def test_grid_groups_sets(qapp, tmp_path):
    from core.settings import set_scan_folders
    from test_main_window import names_in_grid, open_window, pump

    root = tmp_path / "library"
    for name in ("Rock_BaseColor.png", "Rock_Normal.png", "Rock_Roughness.png", "photo.png"):
        picture(root / name)
    set_scan_folders([root])
    window = open_window(qapp)
    assert names_in_grid(window) == ["Rock", "photo.png"]
    card = next(a for a in window.grid_model.assets() if a.texture_set is not None)
    assert "Rock_Normal.png" in window._asset_tooltip(card)
    mime = window.grid_model.mimeData([window.grid_model.index(window.grid_model.row_of(str(card.path)))])
    assert sorted(Path(u.toLocalFile()).name for u in mime.urls()) == ["Rock_BaseColor.png", "Rock_Normal.png", "Rock_Roughness.png"]
    window.search_bar.set_query_text("roughness")  # (the search finds one map: the set shows)
    assert pump(qapp, lambda: names_in_grid(window) == ["Rock"])
    window.search_bar.set_query_text("")
    window.action_group_sets.setChecked(False)
    assert pump(qapp, lambda: len(names_in_grid(window)) == 4)
    window.close()


# ----------------------------------------------------------------------
# The material window's map list: ticking a box with the mouse (it once deleted the row being clicked: a crash)
# ----------------------------------------------------------------------
UNTICK_PROGRAM = r"""
import sys
from pathlib import Path
from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem
app = QApplication([])
from core.scanner import AssetItem, determine_asset_group
from core.texture_sets import find_sets
from ui.material_view import MaterialViewerDialog
if sys.argv[2:] == ["old"]:  # (the code that crashed: rebuild the list from the tick box's own signal)
    def old(self, item, column):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        (self._off.discard if item.checkState(0) == Qt.CheckState.Checked else self._off.add)(key)
        self._fill_maps()
        self._load()
    MaterialViewerDialog._on_item_changed = old
folder, files = Path(sys.argv[1]), []
for name in ("Rock_BaseColor.png", "Rock_Normal.png", "Rock_Roughness.png", "Rock_Rough.png"):
    Image.new("RGB", (16, 16), (90, 90, 90)).save(folder / name)
    files.append(AssetItem(folder / name, name, ".png", 100, 1.0, determine_asset_group(".png"), "PNG"))
dialog = MaterialViewerDialog(find_sets(files)[0])
dialog.show()
tree = dialog.tree

def click(row):
    item = tree.topLevelItem(row)
    option = QStyleOptionViewItem()
    option.rect = tree.visualItemRect(item)
    option.features |= QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
    box = tree.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, tree)
    QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, box.center())
    for _ in range(5):
        app.processEvents()
    return item.data(0, Qt.ItemDataRole.UserRole)

first = click(0)
assert first in dialog._off, "the click didn't untick the map"
assert click(0) == first and first not in dialog._off
second = click(1)
assert second in dialog._off and tree.topLevelItemCount() == 4
box = tree.itemWidget(tree.topLevelItem(2), 1)  # (changing what a map is keeps the list too)
box.setCurrentIndex(box.findData("other"))
for _ in range(5):
    app.processEvents()
assert tree.topLevelItemCount() == 4
# The Adjust sliders: one per kind of map shown (Normal is unticked, Rough is now "other"), this preview only
import time
deadline = time.monotonic() + 20
drawn = lambda: sorted(set(dialog.viewport.maps) - {"other"})  # (an "other" map is read, not drawn)
while time.monotonic() < deadline and drawn() != ["color", "roughness"]:
    app.processEvents(); time.sleep(0.02)
assert drawn() == ["color", "roughness"], drawn()
shown = [kind for kind, (_n, slider, _v) in dialog.strength_rows.items() if not slider.isHidden()]
assert shown == ["color", "roughness"], shown
_name, slider, value = dialog.strength_rows["roughness"]
slider.setValue(slider.maximum())
assert dialog.viewport.strengths["roughness"] == 2.0 and value.text() == "2.00×", value.text()
QTest.mouseDClick(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, slider.rect().center())
app.processEvents()
assert dialog.viewport.strengths["roughness"] == 1.0 and value.text() == "1.00×", value.text()
_name, height, value = dialog.strength_rows["height"]
height.setValue(height.maximum())
assert value.text() == "4.0%" and dialog.viewport.height_scale == 0.04
dialog.done(0)
from ui.tag_widgets import wait_for_background
wait_for_background()  # (the map loads it started)
print("ok")
"""


def test_unticking_a_map_with_the_mouse(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", LOCALAPPDATA=str(tmp_path))
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-c", UNTICK_PROGRAM, str(tmp_path)], cwd=root, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "ok" in result.stdout, result.stdout + result.stderr


# ----------------------------------------------------------------------
# The material shader on a real GPU (skipped without OpenGL 3.3, as on GitHub's machines)
# ----------------------------------------------------------------------
NO_OPENGL = 77
GL_PROGRAM = r"""
import sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PySide6.QtWidgets import QApplication
app = QApplication([])
fmt = QSurfaceFormat(); fmt.setVersion(3, 3); fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
surface = QOffscreenSurface(); surface.setFormat(fmt); surface.create()
context = QOpenGLContext(); context.setFormat(fmt)
if not context.create() or not context.makeCurrent(surface) or context.format().version() < (3, 3):
    sys.exit(77)
context.doneCurrent()
import numpy as np
from core.material_maps import LoadedMap
from ui.material_view import MaterialViewport
view = MaterialViewport(); view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen); view.resize(200, 200); view.show()
for _ in range(10): app.processEvents()
if view._material_program is None:
    print("no program:", view.status_text); sys.exit(1)
color = np.zeros((32, 32, 4), np.uint8); color[...] = (40, 200, 40, 255)
view.set_maps({"color": LoadedMap("color", "x", color)}, False, ("ao", "roughness", "metal"))
r, g, b, _a = view.grabFramebuffer().pixelColor(100, 100).getRgb()
print(r, g, b)
sys.exit(0 if g > r + 40 and g > b + 40 else 2)
"""


def test_material_shader_draws_the_colour_map(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    env["LOCALAPPDATA"] = str(tmp_path)  # (nothing of the real app's)
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-c", GL_PROGRAM], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    if result.returncode == NO_OPENGL:
        pytest.skip("No OpenGL 3.3 here")
    assert result.returncode == 0, result.stdout + result.stderr
