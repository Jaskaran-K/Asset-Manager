"""ZBrush and other app files: finding the apps and starting them (with fake programs: nothing is
ever really started), the pictures files hold or have beside them, app icons on cards, Open In, the
viewer's page for them, and PureRef boards opening as Artbook pages."""

from __future__ import annotations

import gzip
import io
import os
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent

from core import app_icons, apps, archives, file_pictures
from core import artbook_store as store
from core.metadata import read_details
from core.scanner import AssetScanner, determine_asset_group
from core.settings import get_app_choices, get_board_link, get_found_apps, set_found_apps, set_scan_folders
from core.tag_repository import AssetMeta
from core.thumbnail_manager import ThumbnailManager, load_app_file_picture
from test_main_window import names_in_grid, open_window, pump
from test_pur_import import centered, make_board, png as board_png, qt_transform
from zip_maker import Entry, make_zip, png_bytes


def asset_for(path: Path):
    return next(a for a in AssetScanner().scan(path.parent, recursive=False) if a.name == path.name)


def fake_program(folder: Path, name: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"MZ")
    return str(path)


def blend_bytes(pixels: list[tuple[int, int, int, int]], width: int, height: int, extra_block: bool = True) -> bytes:
    """A .blend's start: its header, a REND block, then the TEST block with the picture (rows bottom first)."""
    def block(code: bytes, body: bytes) -> bytes:
        return code + struct.pack("<i", len(body)) + bytes(16) + body

    data = b"BLENDER-v400"
    if extra_block:
        data += block(b"REND", b"\x00" * 40)
    data += block(b"TEST", struct.pack("<ii", width, height) + bytes(v for p in pixels for v in p))
    return data + block(b"DATA", b"\x00" * 64)


RED, BLUE = (255, 0, 0, 255), (0, 0, 255, 255)


# ----------------------------------------------------------------------
# File types
# ----------------------------------------------------------------------
@pytest.mark.parametrize("extension", [".zbp", ".ztl", ".zpr", ".zmt", ".spp", ".sbsar", ".spsm", ".ma", ".mb", ".max", ".tbscene", ".pur"])
def test_app_file_types_are_a_group_of_their_own(extension, tmp_path):
    assert determine_asset_group(extension) == "App Files"
    path = tmp_path / f"a{extension.upper()}"  # (often in capitals: .ZBP)
    path.write_bytes(b"x")
    asset = asset_for(path)
    assert asset.is_app_file and asset.opens_in_app and not asset.is_3d_model


def test_a_blend_stays_a_model_but_opens_in_its_app_and_a_psb_is_an_image(tmp_path):
    (tmp_path / "a.blend").write_bytes(b"x")
    (tmp_path / "b.psb").write_bytes(b"x")
    blend, psb = asset_for(tmp_path / "a.blend"), asset_for(tmp_path / "b.psb")
    assert blend.is_3d_model and blend.opens_in_app and not blend.is_app_file
    assert psb.is_texture and not psb.opens_in_app


def test_app_files_have_no_details_to_read(tmp_path):
    (tmp_path / "a.ztl").write_bytes(b"ZBrush File")
    assert read_details(str(tmp_path / "a.ztl"), ".ztl") == AssetMeta()


# ----------------------------------------------------------------------
# Finding apps
# ----------------------------------------------------------------------
def shortcut_bytes(target: str, unicode_path: bool) -> bytes:
    """A Windows shortcut (Shell Link) to a local file."""
    header = bytearray(76)
    struct.pack_into("<I", header, 0, 0x4C)
    struct.pack_into("<I", header, 20, 0x02)  # (has link info)
    text = target.encode("utf-16-le") + b"\x00\x00" if unicode_path else target.encode("ascii") + b"\x00"
    info_header = 0x24 if unicode_path else 0x1C
    info = bytearray(info_header)
    struct.pack_into("<III", info, 4, info_header, 0x01, 0)
    if unicode_path:
        struct.pack_into("<I", info, 28, info_header)
    else:
        struct.pack_into("<I", info, 16, info_header)
    info += text
    struct.pack_into("<I", info, 0, len(info))
    return bytes(header) + bytes(info)


@pytest.mark.parametrize("unicode_path", [False, True])
def test_reading_a_shortcut(tmp_path, unicode_path):
    link = tmp_path / "App.lnk"
    link.write_bytes(shortcut_bytes(r"E:\Some Place\ZBrush.exe", unicode_path))
    assert apps.read_shortcut(link) == r"E:\Some Place\ZBrush.exe"


def test_a_damaged_shortcut_reads_as_nothing(tmp_path):
    for name, data in (("a.lnk", b""), ("b.lnk", b"not a shortcut" * 20), ("c.lnk", shortcut_bytes("x", True)[:90])):
        (tmp_path / name).write_bytes(data)
        assert apps.read_shortcut(tmp_path / name) is None
    assert apps.read_shortcut(tmp_path / "missing.lnk") is None


def test_apps_outside_program_files_are_found_newest_first(tmp_path, monkeypatch):
    old = fake_program(tmp_path / "Pixologic" / "ZBrush 2022", "ZBrush.exe")
    new = fake_program(tmp_path / "Maxon ZBrush 2024", "ZBrush.exe")
    toolbag = fake_program(tmp_path / "Marmoset" / "Toolbag 4", "toolbag.exe")
    maya_new = fake_program(tmp_path / "Autodesk" / "Maya2027" / "bin", "maya.exe")
    maya_old = fake_program(tmp_path / "Autodesk" / "Maya2024" / "bin", "maya.exe")
    fake_program(tmp_path / "Elsewhere", "notepad.exe")
    start_menu = tmp_path / "start"
    start_menu.mkdir()
    (start_menu / "ZBrush 2022.lnk").write_bytes(shortcut_bytes(old, True))
    (start_menu / "toolbag.lnk").write_bytes(shortcut_bytes(toolbag, True))
    (start_menu / "other.lnk").write_bytes(shortcut_bytes(str(tmp_path / "Elsewhere" / "notepad.exe"), True))
    (start_menu / "gone.lnk").write_bytes(shortcut_bytes(str(tmp_path / "gone" / "ZBrush.exe"), True))
    monkeypatch.setattr(apps, "_shortcut_folders", lambda: [start_menu])
    monkeypatch.setattr(apps, "_registered_programs", lambda: [new, new, maya_old, maya_new])

    found = apps.detect()
    assert found["zbrush"] == [new, old]
    assert found["maya"] == [maya_new, maya_old]
    assert found["toolbag"] == [toolbag]
    assert set(found) == {"zbrush", "maya", "toolbag"}


def test_blender_is_started_through_its_launcher(tmp_path, monkeypatch):
    folder = tmp_path / "Blender 4.0"
    plain = fake_program(folder, "blender.exe")
    launcher = fake_program(folder, "blender-launcher.exe")
    monkeypatch.setattr(apps, "_registered_programs", lambda: [plain, launcher, plain])
    assert apps.detect()["blender"] == [launcher]


def test_the_one_picked_wins_until_its_gone(tmp_path):
    new = fake_program(tmp_path / "2024", "ZBrush.exe")
    old = fake_program(tmp_path / "2022", "ZBrush.exe")
    apps.configure({"zbrush": [new, old]}, {})
    assert apps.program("zbrush") == new
    apps.configure({"zbrush": [new, old]}, {"zbrush": old})
    assert apps.program("zbrush") == old
    os.remove(old)
    assert apps.program("zbrush") == new
    os.remove(new)
    assert apps.program("zbrush") is None and apps.apps_for(".ztl") == []


def test_which_apps_fit_which_files(tmp_path):
    apps.configure({app.id: [fake_program(tmp_path / app.id, app.exe_names[0])] for app in apps.APPS}, {})
    names = lambda extension: [app.id for app in apps.apps_for(extension)]  # noqa: E731
    assert names(".ztl") == names(".ZPR") == ["zbrush"]
    assert names(".spp") == ["painter"]
    assert names(".tbscene") == ["toolbag"]
    assert names(".blend") == ["blender"]
    assert names(".mb") == ["maya"]
    assert names(".obj") == ["blender", "maya", "zbrush"]
    assert names(".fbx") == ["blender", "maya"]
    assert names(".psd") == names(".jpg") == ["photoshop"]  # (ZBrush starts, but doesn't load images given to it)
    assert names(".pur") == names(".max") == names(".mp4") == []


# ----------------------------------------------------------------------
# Starting them
# ----------------------------------------------------------------------
def app_named(app_id: str) -> apps.App:
    return next(app for app in apps.APPS if app.id == app_id)


def test_the_commands(tmp_path):
    awkward = r"D:\my files\it's a & test (1)"
    assert apps.command(app_named("zbrush"), "z.exe", awkward + ".ztl") == ["z.exe", awkward + ".ztl"]
    assert apps.command(app_named("maya"), "m.exe", awkward + ".mb") == ["m.exe", "-file", awkward + ".mb"]
    assert apps.command(app_named("blender"), "b.exe", awkward + ".blend") == ["b.exe", awkward + ".blend"]

    # An OBJ or FBX: an import command, with the file's name outside the command's own text
    blender = apps.command(app_named("blender"), "b.exe", awkward + ".obj")
    assert blender[0] == "b.exe" and blender[1] == "--python-expr" and "wm.obj_import" in blender[2]
    assert blender[3:] == ["--", awkward + ".obj"] and awkward not in blender[2]
    assert "import_scene.fbx" in apps.command(app_named("blender"), "b.exe", "a.FBX")[2]

    maya = apps.command(app_named("maya"), "m.exe", r"D:\my files\a.fbx")
    assert maya[:2] == ["m.exe", "-command"]
    assert 'loadPlugin -quiet "fbxmaya"' in maya[2] and 'file -import -type "FBX" "D:/my files/a.fbx"' in maya[2]
    assert 'file -import -type "OBJ"' in apps.command(app_named("maya"), "m.exe", "a.obj")[2]


def test_starting_an_app(tmp_path, monkeypatch):
    exe = fake_program(tmp_path / "ZBrush 2024", "ZBrush.exe")
    apps.configure({"zbrush": [exe]}, {})
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: started.append((args, kw)))
    monkeypatch.setenv("QT_PLUGIN_PATH", "this app's own")
    monkeypatch.setenv("QML2_IMPORT_PATH", "this app's own")
    monkeypatch.setenv("PYTHONHOME", "this app's own")
    monkeypatch.setenv("KEEP_ME", "yes")

    apps.launch(app_named("zbrush"), "D:/a.ztl")
    args, options = started[0]
    assert args == [exe, "D:/a.ztl"]
    assert not options.get("shell")
    assert options["cwd"] == os.path.dirname(exe)
    assert options["stdout"] == subprocess.DEVNULL
    env = options["env"]
    assert env["KEEP_ME"] == "yes"
    assert not {"QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "PYTHONHOME"} & set(env)
    if sys.platform == "win32":
        assert options["creationflags"] & subprocess.DETACHED_PROCESS


def test_the_built_app_keeps_its_own_folder_from_programs_it_starts(tmp_path, monkeypatch):
    own = tmp_path / "AssetManager"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(own / "AssetManager.exe"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(own), str(own / "_internal"), r"C:\Windows"]))
    assert apps._child_environment()["PATH"] == r"C:\Windows"


def test_an_app_that_cant_start_says_so(tmp_path, monkeypatch):
    with pytest.raises(apps.AppError, match="wasn't found"):
        apps.launch(app_named("toolbag"), "a.tbscene")
    apps.configure({"toolbag": [fake_program(tmp_path, "toolbag.exe")]}, {})

    def refuse(*_args, **_kw):
        raise OSError("not a program")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    with pytest.raises(apps.AppError, match="couldn't be started"):
        apps.launch(app_named("toolbag"), "a.tbscene")


# ----------------------------------------------------------------------
# Icons
# ----------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows' icons")
def test_a_programs_icon_is_read_from_its_file(qapp):
    icon = app_icons.read_icon(sys.executable)
    assert icon is not None and icon.width() > 16 and not icon.isNull()


def test_a_file_without_an_icon_reads_as_none(qapp, tmp_path):
    (tmp_path / "plain.exe").write_bytes(b"not a program")
    assert app_icons.read_icon(str(tmp_path / "plain.exe")) is None
    assert app_icons.read_icon(str(tmp_path / "missing.exe")) is None


def test_which_program_stands_for_a_file_type(tmp_path, monkeypatch):
    zbrush = fake_program(tmp_path / "z", "ZBrush.exe")
    apps.configure({"zbrush": [zbrush]}, {})
    assert apps.icon_program(".ZTL") == zbrush
    assert apps.icon_program(".spp") is None  # (Painter isn't there)

    max_program = fake_program(tmp_path / "m", "3dsmax.exe")
    monkeypatch.setattr(apps, "_associated_program", lambda extension: max_program)
    apps.configure({"zbrush": [zbrush]}, {})
    assert apps.icon_program(".max") == max_program  # (no app of ours: the one Windows opens it with)
    assert apps.icon_program(".pur") is None  # never PureRef's


def test_icons_are_read_once_and_again_when_the_apps_change(qapp, tmp_path, monkeypatch):
    exe = fake_program(tmp_path / "z", "ZBrush.exe")
    apps.configure({"zbrush": [exe]}, {})
    reads = []
    image = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
    monkeypatch.setattr(app_icons, "read_icon", lambda program: reads.append(program) or image)
    assert app_icons.known_icon(".ztl") is None  # (not read yet: and this doesn't read it)
    assert app_icons.icon_for(".ztl") is image and app_icons.icon_for(".zbp") is image
    assert reads == [exe] and app_icons.known_icon(".zpr") is image
    app_icons.forget()
    app_icons.icon_for(".ztl")
    assert reads == [exe, exe]


# ----------------------------------------------------------------------
# Pictures in the files
# ----------------------------------------------------------------------
@pytest.mark.parametrize("packing", ["plain", "gzip", "zstd"])
def test_the_picture_a_blend_holds(packing):
    data = blend_bytes([RED, RED, BLUE, BLUE], 2, 2)  # (rows bottom first: red below, blue above)
    if packing == "gzip":
        data = gzip.compress(data)
    elif packing == "zstd":
        from compression import zstd

        data = zstd.compress(data)
    picture = file_pictures.blend_picture(data)
    assert picture.size == (2, 2)
    assert picture.getpixel((0, 0)) == BLUE and picture.getpixel((1, 1)) == RED


def test_a_zstd_blend_in_many_frames_and_cut_short():
    from compression import zstd

    data = blend_bytes([RED] * 64, 8, 8)
    frames = zstd.compress(data[:60]) + zstd.compress(data[60:])
    assert file_pictures.blend_picture(frames).size == (8, 8)
    assert file_pictures.blend_picture(zstd.compress(data)[:20]) is None  # (not enough to hold it)


@pytest.mark.parametrize("data", [
    b"",
    b"not a blend at all",
    b"BLENDER-v400",  # (nothing after the header)
    b"BLENDER-v400DATA" + struct.pack("<i", 8) + bytes(24),  # (no picture before the data)
    b"BLENDER-v400TEST" + struct.pack("<i", 8) + bytes(16) + struct.pack("<ii", 5, 5),  # (cut short)
    b"BLENDER-v400TEST" + struct.pack("<i", 8) + bytes(16) + struct.pack("<ii", 100000, 100000),  # (a damaged size)
    b"BLENDER-v400REND" + struct.pack("<i", -8) + bytes(16),  # (a damaged length)
    b"\x1f\x8bnot gzip",
    b"\x28\xb5\x2f\xfdnot zstd",
])
def test_a_blend_without_a_readable_picture(data):
    assert file_pictures.blend_picture(data) is None


def jpeg_bytes(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "JPEG", quality=95)
    return out.getvalue()


def board_head(picture: Image.Image) -> bytes:
    return bytes(108) + jpeg_bytes(picture) + b"more of the board"


def test_the_picture_a_board_holds_without_its_empty_margins():
    board = Image.new("RGB", (256, 256), (32, 32, 32))
    board.paste((240, 200, 40), (100, 60, 200, 180))
    picture = file_pictures.board_picture(board_head(board))
    assert abs(picture.width - 100) <= 4 and abs(picture.height - 120) <= 4
    assert picture.getpixel((picture.width // 2, picture.height // 2))[0] > 200


def test_a_board_thats_all_margin_or_a_speck_is_shown_whole():
    empty = Image.new("RGB", (256, 256), (32, 32, 32))
    assert file_pictures.board_picture(board_head(empty)).size == (256, 256)
    speck = empty.copy()
    speck.paste((255, 255, 255), (100, 100, 110, 110))
    assert file_pictures.board_picture(board_head(speck)).size == (256, 256)


def test_a_board_without_a_picture():
    assert file_pictures.board_picture(bytes(5000)) is None  # (a board from before PureRef 2)
    assert file_pictures.board_picture(bytes(108) + b"\xff\xd8\xff not really a jpeg") is None
    assert file_pictures.board_picture(bytes(5000) + jpeg_bytes(Image.new("RGB", (8, 8)))) is None  # (a picture on the board)


# ----------------------------------------------------------------------
# Pictures beside the files: cases from the library
# ----------------------------------------------------------------------
@pytest.mark.parametrize("file_name, folder, names, expected", [
    ("Carve.ZBP", "XMD", ["Carve.PNG", "Carve_alpha.psd"], "Carve.PNG"),
    ("XMD_Hair.ZBP", "XMD", ["XMD_Hair_ico.png", "XMD_Hair_alpha.psd"], "XMD_Hair_ico.png"),
    ("XMD_Carve.ZBP", "XMD", ["XMD_Carve_alpha.PSD", "XMD_Other_alpha.psd"], "XMD_Carve_alpha.PSD"),
    ("IMM BRUSH.ZBP", "Freds 5", ["Preview.jpg"], "Preview.jpg"),
    ("IMM-Center.ZBP", "Vol7.5", ["Prev.jpg"], "Prev.jpg"),
    ("Pack 01.zbp", "Pack", ["Pack.png", "other.png"], "Pack.png"),
    ("Scene.ma", "Scene", ["Scene.jpg", "Scene_diffuse.png"], "Scene.jpg"),
    # Clearly not of it: no picture
    ("30_Hair_Brush.ZBP", "30 IMM Hair", ["Brush_settings.jpg"], None),  # (the only image, but a screenshot)
    ("Superheroine.ztl", "ZBrush", ["view_me.jpg"], None),  # (a screenshot of export settings)
    ("cg-cloth-1.ZBP", "Cloth", ["Hair_Alpha.jpg"], None),
    ("eyeball.ZTL", "Eye", ["eye_texture.jpg", "sclera.jpg"], None),
    ("Rock.ma", "Rock", ["Rock_diffuse.png"], None),  # (a scene's textures aren't pictures of it)
    ("Carve.ZBP", "XMD", ["._Carve.png", "notes.txt", "Carve.pdf"], None),
    ("a.zbp", "A", ["thumbnailsheet.png", "reviewer.jpg", "covered.png"], None),
])
def test_which_picture_beside_a_file_is_of_it(file_name, folder, names, expected):
    assert file_pictures.pack_picture_name(file_name, folder, names) == expected


def test_a_picture_beside_a_file_and_inside_an_archive(tmp_path):
    archives.forget_listings()
    folder = tmp_path / "Pack"
    folder.mkdir()
    (folder / "Tool.ztl").write_bytes(b"x")
    (folder / "Preview.jpg").write_bytes(b"x")
    assert file_pictures.pack_picture(folder / "Tool.ztl") == folder / "Preview.jpg"
    (folder / "Preview.jpg").unlink()
    assert file_pictures.pack_picture(folder / "Tool.ztl") is None

    make_zip(tmp_path / "pack.zip", [
        Entry("Brush/Carve.ZBP", b"x"),
        Entry("Brush/Carve_ico.png", png_bytes()),
        Entry("Other/Lone.ZBP", b"x"),
        Entry("Preview.png", png_bytes()),  # (a folder up: not used)
    ])
    members = {m.name: m for m in archives.listing(tmp_path / "pack.zip").members}
    assert file_pictures.member_pack_picture(members["Brush/Carve.ZBP"]).name == "Brush/Carve_ico.png"
    assert file_pictures.member_pack_picture(members["Other/Lone.ZBP"]) is None


def test_a_picture_too_big_to_unpack_for_a_card_isnt_used(tmp_path, monkeypatch):
    archives.forget_listings()
    make_zip(tmp_path / "pack.zip", [Entry("Carve.ZBP", b"x"), Entry("Carve_ico.png", png_bytes())])
    monkeypatch.setattr(archives, "MAX_CARD_PICTURE_BYTES", 5)
    members = {m.name: m for m in archives.listing(tmp_path / "pack.zip").members}
    assert file_pictures.member_pack_picture(members["Carve.ZBP"]) is None


# ----------------------------------------------------------------------
# Cards
# ----------------------------------------------------------------------
def card_of(qapp, manager: ThumbnailManager, asset, size: int = 128) -> QImage:
    manager.get_thumbnail(asset, size)
    assert pump(qapp, lambda: manager.cached(asset) is not None)
    return manager.cached(asset).toImage().convertToFormat(QImage.Format.Format_RGB32)


def color_share(card: QImage, match) -> float:
    pixels = np.frombuffer(card.constBits(), np.uint8).reshape(card.height(), card.bytesPerLine() // 4, 4)
    return float(np.mean(match(pixels[..., 2].astype(int), pixels[..., 1].astype(int), pixels[..., 0].astype(int))))


def is_red(r, g, b):
    return (r > 200) & (g < 60) & (b < 60)


def big_png(color: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (96, 96), color).save(out, "PNG")
    return out.getvalue()


def is_green(r, g, b):
    return (g > 90) & (r < 60) & (b < 60)


@pytest.fixture
def icons(qapp, tmp_path, monkeypatch):
    """ZBrush is on this computer, with a red icon."""
    exe = fake_program(tmp_path / "apps" / "ZBrush 2024", "ZBrush.exe")
    apps.configure({"zbrush": [exe]}, {})
    image = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(255, 0, 0))
    monkeypatch.setattr(app_icons, "read_icon", lambda program: image)


def test_a_file_without_a_picture_shows_its_apps_icon(qapp, tmp_path, icons):
    (tmp_path / "a.ztl").write_bytes(b"ZBrush File")
    manager = ThumbnailManager()
    assert color_share(card_of(qapp, manager, asset_for(tmp_path / "a.ztl")), is_red) > 0.2
    manager.shutdown()


def test_without_its_app_a_file_shows_a_plain_sheet(qapp, tmp_path):
    (tmp_path / "a.spp").write_bytes(b"\x89HDF")
    manager = ThumbnailManager()
    card = card_of(qapp, manager, asset_for(tmp_path / "a.spp"))
    assert color_share(card, is_red) == 0
    manager.shutdown()


def test_a_picture_beside_it_comes_before_the_icon(qapp, tmp_path, icons):
    (tmp_path / "a.ztl").write_bytes(b"ZBrush File")
    Image.new("RGB", (64, 64), (0, 128, 0)).save(tmp_path / "a_ico.png")
    manager = ThumbnailManager()
    card = card_of(qapp, manager, asset_for(tmp_path / "a.ztl"))
    assert color_share(card, is_green) > 0.2 and color_share(card, is_red) == 0
    manager.shutdown()


def test_a_blend_shows_the_picture_it_holds_and_reads_only_its_start(qapp, tmp_path, icons, monkeypatch):
    from core import model_loader

    monkeypatch.setattr(model_loader, "load_3d_model", lambda *a, **k: pytest.fail("a .blend can't be read as a model"))
    (tmp_path / "a.blend").write_bytes(blend_bytes([RED] * 16, 4, 4) + bytes(file_pictures.HEAD_BYTES))
    manager = ThumbnailManager()
    card = card_of(qapp, manager, asset_for(tmp_path / "a.blend"))
    assert color_share(card, is_red) > 0.3  # (made bigger to fill the card)
    manager.shutdown()


def test_cards_of_files_inside_an_archive(qapp, tmp_path, icons):
    archives.forget_listings()
    blend = blend_bytes([RED] * 4, 2, 2)
    board = Image.new("RGB", (256, 256), (32, 32, 32))
    board.paste((0, 128, 0), (40, 40, 200, 200))
    make_zip(tmp_path / "pack.zip", [
        Entry("a.blend", blend),
        Entry("b.pur", board_head(board)),
        Entry("c.ztl", b"ZBrush File"),
        Entry("d/e.ztl", b"ZBrush File"),
        Entry("d/e_ico.png", big_png("green")),
    ])
    assets = {a.name: a for a in archives.entry_assets(tmp_path / "pack.zip", [".blend", ".pur", ".ztl"])}
    manager = ThumbnailManager()
    assert color_share(card_of(qapp, manager, assets["a.blend"]), is_red) > 0.3
    assert color_share(card_of(qapp, manager, assets["b.pur"]), is_green) > 0.2
    assert color_share(card_of(qapp, manager, assets["c.ztl"]), is_red) > 0.2
    assert color_share(card_of(qapp, manager, assets["e.ztl"]), is_green) > 0.2
    manager.shutdown()


def test_a_boards_card_never_shows_an_icon_of_pureref(qapp, tmp_path, monkeypatch):
    exe = fake_program(tmp_path / "PureRef", "PureRef.exe")
    monkeypatch.setattr(apps, "_associated_program", lambda extension: exe)
    apps.configure({}, {})
    monkeypatch.setattr(app_icons, "read_icon", lambda program: pytest.fail("PureRef.exe must not be touched"))
    (tmp_path / "old.pur").write_bytes(bytes(5000))  # (a board with no picture)
    manager = ThumbnailManager()
    card_of(qapp, manager, asset_for(tmp_path / "old.pur"))
    manager.shutdown()


def test_a_damaged_blend_falls_back_to_the_icon(qapp, tmp_path, monkeypatch):
    exe = fake_program(tmp_path / "Blender 4.0", "blender-launcher.exe")
    apps.configure({"blender": [exe]}, {})
    image = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(255, 0, 0))
    monkeypatch.setattr(app_icons, "read_icon", lambda program: image)
    (tmp_path / "a.blend").write_bytes(b"BLENDER-v400 damaged")
    manager = ThumbnailManager()
    assert color_share(card_of(qapp, manager, asset_for(tmp_path / "a.blend")), is_red) > 0.2
    manager.shutdown()


def test_when_the_apps_change_the_cards_follow(qapp, tmp_path, icons, monkeypatch):
    (tmp_path / "a.ztl").write_bytes(b"ZBrush File")
    Image.new("RGB", (8, 8)).save(tmp_path / "keep.png")
    manager = ThumbnailManager()
    asset, other = asset_for(tmp_path / "a.ztl"), asset_for(tmp_path / "keep.png")
    card_of(qapp, manager, asset)
    card_of(qapp, manager, other)
    manager.forget_app_cards()
    assert manager.cached(asset) is None and manager.cached(other) is not None
    manager.shutdown()


# ----------------------------------------------------------------------
# The right-click menu
# ----------------------------------------------------------------------
@pytest.fixture
def library(tmp_path):
    archives.forget_listings()
    root = tmp_path / "library"
    root.mkdir()
    (root / "tool.ztl").write_bytes(b"ZBrush File")
    (root / "scene.blend").write_bytes(blend_bytes([RED] * 4, 2, 2))
    (root / "model.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    (root / "photo.png").write_bytes(png_bytes(color="red"))
    make_zip(root / "pack.zip", [Entry("inside.ztl", b"ZBrush File")])
    set_scan_folders([root])
    yield root
    archives.forget_listings()


@pytest.fixture
def no_starting(monkeypatch):
    """What would have been started."""
    started = []
    monkeypatch.setattr(apps, "launch", lambda app, file: started.append((app.id, str(file))))
    return started


def asset_named(window, name):
    return next(a for a in window.grid_model.assets() if a.name == name)


def menu_of(window, asset, actions):
    from ui.asset_menu import build_asset_menu

    return build_asset_menu(window, asset, [asset], [], actions, window.artbook_sender)


def open_in_entries(menu) -> list[str]:
    action = next((a for a in menu.actions() if a.text() == "Open In"), None)
    return [a.text() for a in action.menu().actions()] if action is not None else []


def test_open_in_lists_the_apps_that_fit(library, qapp):
    from ui.asset_menu import AssetMenuActions

    window = open_window(qapp)
    apps.configure(*every_app_config(library.parent))
    picked = []
    noop = lambda *a: None  # noqa: E731
    actions = AssetMenuActions(noop, noop, noop, noop, noop, open_in=picked.append)
    for name, expected in (
        ("tool.ztl", ["ZBrush"]),
        ("scene.blend", ["Blender"]),
        ("model.obj", ["Blender", "Maya", "ZBrush"]),
        ("photo.png", ["Photoshop"]),
        ("inside.ztl", ["ZBrush"]),
        ("pack.zip", []),
    ):
        menu = menu_of(window, asset_named(window, name), actions)
        assert open_in_entries(menu) == expected, name
        if expected:
            action = next(a for a in menu.actions() if a.text() == "Open In")
            action.menu().actions()[0].trigger()
        assert "Open with External App" in [a.text() for a in menu.actions()]  # (always, as before)
        menu.deleteLater()
    assert [app.id for app in picked] == ["zbrush", "blender", "blender", "photoshop", "zbrush"]
    window.close()


def every_app_config(root: Path):
    return {app.id: [fake_program(root / "apps" / app.id, app.exe_names[0])] for app in apps.APPS}, {}


def test_no_open_in_without_an_app(library, qapp):
    from ui.asset_menu import AssetMenuActions

    window = open_window(qapp)
    noop = lambda *a: None  # noqa: E731
    menu = menu_of(window, asset_named(window, "tool.ztl"), AssetMenuActions(noop, noop, noop, noop, noop, open_in=noop))
    assert open_in_entries(menu) == []
    menu.deleteLater()
    window.close()


def test_opening_from_the_menu(library, qapp, no_starting):
    window = open_window(qapp)
    apps.configure(*every_app_config(library.parent))
    window._open_in(asset_named(window, "tool.ztl"), app_named("zbrush"))
    assert no_starting == [("zbrush", str(library / "tool.ztl"))]
    assert "Opened tool.ztl in ZBrush" in window.status_label.text()

    # Inside an archive: unpacked first, and it says it's a copy
    no_starting.clear()
    inside = asset_named(window, "inside.ztl")
    window._open_in(inside, app_named("zbrush"))
    assert pump(qapp, lambda: bool(no_starting))
    assert Path(no_starting[0][1]).read_bytes() == b"ZBrush File"
    assert "unpacked copy" in window.status_label.text()

    # Gone since the scan: says so, and nothing is started
    no_starting.clear()
    tool = asset_named(window, "tool.ztl")
    (library / "tool.ztl").unlink()
    window._open_in(tool, app_named("zbrush"))
    assert no_starting == [] and "isn't there" in window.status_label.text()
    window.close()


# ----------------------------------------------------------------------
# The viewer's page
# ----------------------------------------------------------------------
def viewer_buttons(dialog) -> list[str]:
    from PySide6.QtWidgets import QPushButton

    return [b.text() for b in dialog.findChildren(QPushButton) if b.text().startswith("Open in") and b.text() != "Open in External App"]


def press(dialog, key) -> None:
    dialog.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


def test_the_viewer_offers_the_app_and_opens_nothing_until_asked(library, qapp, no_starting):
    from ui.viewer import AssetViewerDialog

    window = open_window(qapp)
    apps.configure(*every_app_config(library.parent))
    dialog = AssetViewerDialog(asset_named(window, "tool.ztl"), window)
    assert viewer_buttons(dialog) == ["Open in ZBrush"]
    assert no_starting == []

    press(dialog, Qt.Key.Key_O)
    assert no_starting == [("zbrush", str(library / "tool.ztl"))]
    assert dialog._app_status.text() == "Opened in ZBrush."
    dialog.close()
    dialog.deleteLater()
    window.close()


def test_the_viewer_shows_a_blends_picture_without_reading_it_as_a_model(library, qapp, no_starting, monkeypatch):
    import ui.viewer
    from ui.viewer import AssetViewerDialog

    monkeypatch.setattr(ui.viewer.model_reader, "read_mesh", lambda *a, **k: pytest.fail("a .blend can't be read as a model"))
    window = open_window(qapp)
    apps.configure(*every_app_config(library.parent))
    dialog = AssetViewerDialog(asset_named(window, "scene.blend"), window)
    assert dialog.viewport is None and viewer_buttons(dialog) == ["Open in Blender"]
    assert pump(qapp, lambda: any(
        label.pixmap() is not None and not label.pixmap().isNull() and label.pixmap().width() > 100
        for label in dialog.findChildren(ui.viewer.QLabel)
    ))
    dialog.close()
    dialog.deleteLater()
    window.close()


def test_without_an_app_the_viewer_says_where_to_set_it(library, qapp):
    from ui.viewer import AssetViewerDialog

    window = open_window(qapp)
    dialog = AssetViewerDialog(asset_named(window, "tool.ztl"), window)
    assert viewer_buttons(dialog) == [] and "Settings > Apps" in dialog._app_status.text()
    press(dialog, Qt.Key.Key_O)  # (nothing to press)
    dialog.close()
    dialog.deleteLater()
    window.close()


def test_the_viewer_unpacks_a_file_inside_an_archive_only_when_asked(library, qapp, no_starting):
    from ui.viewer import AssetViewerDialog

    window = open_window(qapp)
    apps.configure(*every_app_config(library.parent))
    inside = asset_named(window, "inside.ztl")
    dialog = AssetViewerDialog(inside, window)
    assert archives.cached_copy(inside.archive) is None and viewer_buttons(dialog) == ["Open in ZBrush"]
    press(dialog, Qt.Key.Key_O)
    assert pump(qapp, lambda: bool(no_starting))
    assert no_starting[0][0] == "zbrush" and Path(no_starting[0][1]).read_bytes() == b"ZBrush File"
    assert "unpacked copy" in dialog._app_status.text()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # (frees the page it replaced)
    assert viewer_buttons(dialog) == ["Open in ZBrush"]  # (back on its page)
    assert len(no_starting) == 1  # (not started twice)
    dialog.close()
    dialog.deleteLater()
    window.close()


def test_the_viewers_failure_to_start_an_app_is_shown(library, qapp, monkeypatch):
    from ui.viewer import AssetViewerDialog

    def refuse(app, file):
        raise apps.AppError("ZBrush couldn't be started: no")

    monkeypatch.setattr(apps, "launch", refuse)
    window = open_window(qapp)
    apps.configure(*every_app_config(library.parent))
    dialog = AssetViewerDialog(asset_named(window, "tool.ztl"), window)
    press(dialog, Qt.Key.Key_O)
    assert "couldn't be started" in dialog._app_status.text()
    dialog.close()
    dialog.deleteLater()
    window.close()


# ----------------------------------------------------------------------
# PureRef boards
# ----------------------------------------------------------------------
def write_board(path: Path) -> Path:
    make_board(
        path,
        images=[(1, "png", board_png(4, 4), 4, 4)],
        items=[(1, -1, qt_transform(), 1.0, 1.0)],
        item_images=[(1, 1, centered(4, 4), None)],
    )
    return path


@pytest.fixture
def boards(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    write_board(root / "Hands.pur")
    set_scan_folders([root])
    return root


def page_names() -> list[str]:
    return [p.get("name") for p in store.list_pages()]


def opened(window, name: str) -> bool:
    return not window.artbook_panel.importing() and (window.artbook_panel.page or {}).get("name") == name


def test_double_clicking_a_board_opens_one_page_however_often(boards, qapp):
    window = open_window(qapp)
    window._on_item_double_clicked(window.grid_model.index(names_in_grid(window).index("Hands.pur")))
    assert pump(qapp, lambda: opened(window, "Hands"))
    assert window.view_modes.mode == "artbook"
    page_id = window.artbook_panel.page["id"]
    board = os.path.normcase(str(boards / "Hands.pur"))
    assert get_board_link(board)["page"] == page_id
    count = len(store.list_pages())

    other = store.create_page("Another")
    window.artbook_panel.open_page(other["id"])
    window._open_viewer(asset_named(window, "Hands.pur"))
    assert not window.artbook_panel.importing()
    assert window.artbook_panel.page["id"] == page_id and len(store.list_pages()) == count + 1  # (only "Another" is new)
    window.close()


def test_a_page_imported_by_hand_earlier_is_used_and_linked(boards, qapp):
    earlier = store.create_page("Hands")
    window = open_window(qapp)
    count = len(store.list_pages())
    window._open_viewer(asset_named(window, "Hands.pur"))
    assert window.artbook_panel.page["id"] == earlier["id"] and len(store.list_pages()) == count
    assert get_board_link(os.path.normcase(str(boards / "Hands.pur")))["page"] == earlier["id"]
    window.close()


def test_a_changed_board_keeps_its_page_and_says_so_and_import_again_makes_a_new_one(boards, qapp, monkeypatch):
    window = open_window(qapp)
    window._open_viewer(asset_named(window, "Hands.pur"))
    assert pump(qapp, lambda: opened(window, "Hands"))
    page_id = window.artbook_panel.page["id"]
    messages = []
    monkeypatch.setattr(window.artbook_panel.board, "show_message", messages.append)

    # Changed since: the same page, with a word about it (a page's edits are never overwritten)
    asset = asset_named(window, "Hands.pur")
    asset.modified_timestamp += 100
    window._open_viewer(asset)
    assert window.artbook_panel.page["id"] == page_id and not window.artbook_panel.importing()
    assert messages and "Import Again" in messages[-1]

    # Import Again: a new page, linked from now on; the old one stays
    window._open_board(asset, again=True)
    assert pump(qapp, lambda: not window.artbook_panel.importing() and window.artbook_panel.page["id"] != page_id)
    new_id = window.artbook_panel.page["id"]
    assert get_board_link(os.path.normcase(str(boards / "Hands.pur")))["page"] == new_id
    assert {page_id, new_id} <= {p["id"] for p in store.list_pages()}
    window.close()


def test_a_board_whose_page_was_deleted_is_imported_again(boards, qapp):
    window = open_window(qapp)
    window._open_viewer(asset_named(window, "Hands.pur"))
    assert pump(qapp, lambda: opened(window, "Hands"))
    first = window.artbook_panel.page["id"]
    window.artbook_panel.new_page()
    store.delete_page(first)
    window._open_viewer(asset_named(window, "Hands.pur"))
    assert pump(qapp, lambda: opened(window, "Hands"))
    assert window.artbook_panel.page["id"] != first
    window.close()


def test_a_board_inside_an_archive_is_unpacked_then_opened(tmp_path, qapp):
    archives.forget_listings()
    root = tmp_path / "library"
    root.mkdir()
    board = write_board(tmp_path / "Inside.pur")
    make_zip(root / "refs.zip", [Entry("Inside.pur", board.read_bytes())])
    set_scan_folders([root])
    window = open_window(qapp)
    window._open_viewer(asset_named(window, "Inside.pur"))
    assert pump(qapp, lambda: opened(window, "Inside"))
    page_id, count = window.artbook_panel.page["id"], len(store.list_pages())
    window._open_viewer(asset_named(window, "Inside.pur"))
    pump(qapp, seconds=0.5)  # (the unpacked copy is there: nothing more to wait for)
    assert window.artbook_panel.page["id"] == page_id and len(store.list_pages()) == count
    window.close()
    archives.forget_listings()


def test_the_viewers_button_opens_the_board_after_closing(boards, qapp, monkeypatch):
    from ui.viewer import AssetViewerDialog, ViewerTarget

    (boards / "other.ztl").write_bytes(b"ZBrush File")
    window = open_window(qapp)
    # Going through the grid with the viewer lands on a board: it isn't imported until asked
    dialog = AssetViewerDialog(asset_named(window, "Hands.pur"), window)
    assert viewer_buttons(dialog) == ["Open in Artbook"] and page_names().count("Hands") == 0
    press(dialog, Qt.Key.Key_O)
    assert dialog.board_wanted and not dialog.isVisible() and page_names().count("Hands") == 0
    dialog.deleteLater()

    # In the window: from another file's viewer, moved along to the board; it closes, then the board opens
    board = asset_named(window, "Hands.pur")

    def moved_to_the_board_and_asked(self):
        self.show_target(ViewerTarget(board, None, None, ""))
        self._btn_open_app.click()
        return 0

    monkeypatch.setattr(AssetViewerDialog, "exec", moved_to_the_board_and_asked)
    window._open_viewer(asset_named(window, "other.ztl"))
    assert pump(qapp, lambda: opened(window, "Hands"))
    window.close()


def test_the_board_menu(boards, qapp):
    from ui.asset_menu import AssetMenuActions

    window = open_window(qapp)
    noop = lambda *a: None  # noqa: E731
    again = []
    menu = menu_of(window, asset_named(window, "Hands.pur"), AssetMenuActions(
        noop, noop, noop, noop, noop, open_in=noop, import_board=lambda: again.append(True),
    ))
    texts = [a.text() for a in menu.actions()]
    assert texts[0] == "Open in Artbook" and "Import Again" in texts
    assert "Open In" not in texts  # (only Open with External App reaches PureRef, as Windows has it)
    assert "Open with External App" in texts
    next(a for a in menu.actions() if a.text() == "Import Again").trigger()
    assert again == [True]
    menu.deleteLater()
    window.close()


# ----------------------------------------------------------------------
# Settings > Apps, and looking for apps
# ----------------------------------------------------------------------
def test_settings_lists_whats_found_and_keeps_only_a_real_choice(qapp, tmp_path):
    from ui.settings_dialog import SettingsDialog

    new = fake_program(tmp_path / "2024", "ZBrush.exe")
    old = fake_program(tmp_path / "2022", "ZBrush.exe")
    set_found_apps({"zbrush": [new, old]})
    apps.configure(get_found_apps(), {})

    dialog = SettingsDialog()
    box = dialog.page_apps._boxes["zbrush"]
    assert [box.itemData(i) for i in range(box.count())] == [new, old]
    assert dialog.page_apps._boxes["toolbag"].currentText().startswith("Not found")
    dialog._on_save()
    assert get_app_choices() == {} and not dialog.apps_changed  # (the newest: nothing to remember)
    dialog.deleteLater()

    dialog = SettingsDialog()
    dialog.page_apps._boxes["zbrush"].setCurrentIndex(1)
    dialog._on_save()
    assert get_app_choices() == {"zbrush": old} and dialog.apps_changed
    assert apps.program("zbrush") == old
    dialog.deleteLater()

    # Shown picked next time
    dialog = SettingsDialog()
    assert dialog.page_apps._boxes["zbrush"].currentData() == old
    dialog.deleteLater()


def test_browsing_to_a_program_and_finding_again(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from ui.settings_dialog import SettingsDialog

    mine = fake_program(tmp_path / "mine", "toolbag.exe")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (mine, "")))
    dialog = SettingsDialog()
    page = dialog.page_apps
    page._on_browse(app_named("toolbag"))
    assert page._boxes["toolbag"].currentData() == os.path.normpath(mine) or page._boxes["toolbag"].currentData() == mine

    found = fake_program(tmp_path / "Blender 4.0", "blender-launcher.exe")
    monkeypatch.setattr(apps, "_registered_programs", lambda: [found])
    page._on_find()
    assert not page.btn_find.isEnabled()
    assert pump(qapp, lambda: page.btn_find.isEnabled())
    assert page._boxes["blender"].currentData() == found
    assert page._boxes["toolbag"].currentData() == os.path.normpath(mine) or page._boxes["toolbag"].currentData() == mine  # (kept)
    dialog._on_save()
    assert get_found_apps()["blender"] == [found] and apps.program("blender") == found
    assert apps.program("toolbag") in (mine, os.path.normpath(mine))
    dialog.deleteLater()


def test_the_window_looks_for_apps_and_the_cards_follow(library, qapp, tmp_path, monkeypatch):
    window = open_window(qapp)
    exe = fake_program(tmp_path / "ZBrush 2024", "ZBrush.exe")
    monkeypatch.setattr(apps, "_registered_programs", lambda: [exe])
    forgotten = []
    monkeypatch.setattr(window.thumbnail_manager, "forget_app_cards", lambda: forgotten.append(True))
    window._find_apps()
    assert pump(qapp, lambda: bool(forgotten))
    assert get_found_apps() == {"zbrush": [exe]} and apps.program("zbrush") == exe

    # Nothing changed next time: no redrawing, nothing saved
    forgotten.clear()
    window._on_apps_found({"zbrush": [exe]})
    assert forgotten == []
    window.close()
