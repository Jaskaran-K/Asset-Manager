"""Alphas (core.alphas): telling them apart, their heights at full precision, the relief thumbnails,
the 3D surface, is:alpha, the database upgrade, and the image viewer's Tile and 3D."""

from __future__ import annotations

import io
import struct
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from core import alphas
from core.search_query import parse_query
from core.tag_repository import AssetMeta, NO_MARKS


def save(image: Image.Image, path: Path, **options) -> Path:
    image.save(path, **options)
    return path


def ramp16(size=(300, 200)) -> Image.Image:
    """A 16-bit grey ramp: 0 at the left to 65535 at the right, in steps of a few levels."""
    w, h = size
    row = np.linspace(0, 65535, w).astype(np.uint16)
    return Image.fromarray(np.tile(row, (h, 1)))  # (uint16: Pillow's I;16)


def psd16(path: Path, height: np.ndarray, color_mode: int = 1) -> Path:
    """A raw (uncompressed) 16-bit PSD: one plane (grayscale), or the same plane three times (RGB)."""
    planes = 1 if color_mode == 1 else 3
    h, w = height.shape
    data = np.repeat(height[None].astype(">u2"), planes, axis=0).tobytes()
    header = b"8BPS" + struct.pack(">H6xHIIHH", 1, planes, h, w, 16, color_mode)
    path.write_bytes(header + struct.pack(">III", 0, 0, 0) + struct.pack(">H", 0) + data)
    return path


# ----------------------------------------------------------------------
# Telling alphas apart
# ----------------------------------------------------------------------
@pytest.mark.parametrize("name, image, expected", [
    ("rock_alpha.png", Image.new("L", (32, 32), 90), True),
    ("deep.png", ramp16(), True),
    ("grey_as_rgb.png", Image.new("RGB", (32, 32), (120, 120, 120)), True),
    ("grey_with_alpha.png", Image.new("RGBA", (32, 32), (40, 40, 40, 255)), True),
    ("photo.png", Image.new("RGB", (32, 32), (200, 80, 30)), False),
    ("Wall_AO_4K.png", Image.new("L", (32, 32), 90), False),
    ("wall-roughness.png", Image.new("L", (32, 32), 90), False),
    ("Metal_02.png", Image.new("L", (32, 32), 90), False),
])
def test_what_counts_as_an_alpha(tmp_path, name, image, expected):
    path = save(image, tmp_path / name)
    assert alphas.detect(path, name) is expected
    assert (alphas.read_height(path, name, 64) is not None) is expected


def test_a_grey_jpeg_counts_despite_its_noise(tmp_path):
    rng = np.random.default_rng(1)
    grey = rng.integers(0, 255, (64, 64), dtype=np.uint8)
    path = save(Image.fromarray(np.dstack([grey] * 3)), tmp_path / "skin_pores.jpg", quality=85)
    assert alphas.detect(path, path.name)


def test_hdr_environments_are_not_alphas(tmp_path):
    assert not alphas.detect(tmp_path / "sky.hdr", "sky.hdr")


def test_detect_works_on_bytes_too():
    out = io.BytesIO()
    Image.new("L", (16, 16), 10).save(out, "PNG")
    assert alphas.detect_bytes(out.getvalue(), "brush.png")


# ----------------------------------------------------------------------
# Heights at full precision
# ----------------------------------------------------------------------
def test_16_bit_heights_keep_their_precision(tmp_path):
    path = save(ramp16((2000, 64)), tmp_path / "ramp.png")
    height = alphas.read_height(path, path.name, 1000)
    assert height.dtype == np.float32 and height.shape == (32, 1000)
    assert 0.0 <= height.min() < 0.01 and 0.99 < height.max() <= 1.0
    assert len(np.unique(height[0])) > 900  # (8 bits would leave at most 256)


def test_heights_are_fitted_to_the_size_asked_for(tmp_path):
    for size, expected in (((256, 256), (240, 240)), ((3000, 1000), (80, 240)), ((100, 50), (50, 100))):
        path = save(Image.new("L", size, 50), tmp_path / f"a{size[0]}.png")
        assert alphas.read_height(path, path.name, 240).shape == expected


def test_16_bit_psd_heights_keep_their_precision(tmp_path):
    from core.psd_reader import read_psd_composite, read_psd_planes

    values = np.tile(np.linspace(0, 65535, 512).astype(np.uint16), (8, 1))
    path = psd16(tmp_path / "alpha.psd", values)
    planes, depth, mode, _channels = read_psd_planes(path)
    assert depth == 16 and mode == 1 and planes.dtype.itemsize == 2
    assert np.array_equal(planes[0], values)
    assert read_psd_composite(path).mode == "L"  # (the 8-bit picture is as before)
    height = alphas.read_height(path, path.name, 512)
    assert len(np.unique(np.round(height[0] * 65535))) > 400

    rgb = psd16(tmp_path / "grey_rgb.psd", values, color_mode=3)
    assert alphas.detect(rgb, rgb.name) and alphas.read_height(rgb, rgb.name, 64) is not None


def test_float_heights_are_stretched_to_their_range(tmp_path):
    values = np.linspace(-2.0, 6.0, 64 * 64, dtype=np.float32).reshape(64, 64)
    path = save(Image.fromarray(values), tmp_path / "disp.tif")  # (float32: mode F)
    height = alphas.read_height(path, path.name, 64)
    assert height.min() == pytest.approx(0.0) and height.max() == pytest.approx(1.0)


# ----------------------------------------------------------------------
# The relief and the 3D surface
# ----------------------------------------------------------------------
def test_relief_light_comes_from_the_top_left():
    flat = alphas.relief(np.full((32, 32), 0.5, np.float32))
    assert (flat == flat[0, 0]).all()  # (even shading)
    assert flat[0, 0, 0] > flat[0, 0, 2]  # clay: a touch warmer than grey

    ramp = np.tile(np.linspace(0, 1, 32, dtype=np.float32), (32, 1))
    facing_light = alphas.relief(ramp)  # rising to the right: faces the left, where the light is
    facing_away = alphas.relief(ramp[:, ::-1].copy())
    assert facing_light[16, 16, 0] > flat[16, 16, 0] > facing_away[16, 16, 0]


def test_height_mesh_is_a_plane_raised_where_its_white():
    height = np.zeros((20, 30), np.float32)
    height[10, 15] = 1.0
    mesh = alphas.height_mesh(height, strength=0.2)
    assert mesh.vertex_count == 20 * 30 and mesh.face_count == 2 * 19 * 29
    ys = mesh.vertices[:, 1]
    assert ys.argmax() == 10 * 30 + 15
    v = mesh.vertices[mesh.faces[0]]
    assert np.cross(v[1] - v[0], v[2] - v[0])[1] > 0  # (faces turned up)
    flat = alphas.height_mesh(np.zeros((4, 4), np.float32))
    assert np.allclose(flat.normals, [0, 1, 0])
    inverted = alphas.height_mesh(height, strength=0.2, invert=True)
    assert inverted.vertices[:, 1].argmin() == 10 * 30 + 15


# ----------------------------------------------------------------------
# Search, the database, the file details
# ----------------------------------------------------------------------
def test_is_alpha_search():
    from core.scanner import AssetItem

    asset = AssetItem(Path("a.png"), "a.png", ".png", 1, 0, "Textures", "PNG")
    query = parse_query("is:alpha rock")
    assert query.words == ["rock"] and query.uses_meta
    assert query.matches(asset, NO_MARKS, AssetMeta(alpha=True))
    assert not query.matches(asset, NO_MARKS, AssetMeta(alpha=False))
    assert not query.matches(asset, NO_MARKS, None)  # (not read yet: no match either way)
    negated = parse_query("-is:alpha")
    assert negated.matches(asset, NO_MARKS, AssetMeta(alpha=False))
    assert not negated.matches(asset, NO_MARKS, None)


def test_the_database_upgrade_reads_images_details_again(tmp_path):
    import sqlite3

    from core.tag_repository import MIGRATIONS, TagRepository, meta_key

    db = tmp_path / "library.db"
    conn = sqlite3.connect(db)
    for version, step in enumerate(MIGRATIONS[:5], start=1):
        conn.executescript(f"BEGIN; {step} PRAGMA user_version = {version}; COMMIT;")
    for name, ext in (("a.png", ".png"), ("b.fbx", ".fbx")):  # (both with their details read, in v5)
        conn.execute(
            "INSERT INTO assets (path_key, path, name, name_fold, extension, size, mtime, meta_key) VALUES (?, ?, ?, ?, ?, 1, 2.0, ?)",
            (name, name, name, name, ext, meta_key(1, 2.0)),
        )
    conn.commit()
    conn.close()
    repo = TagRepository(db)
    ids = {row["name"]: row["id"] for row in repo._conn().execute("SELECT id, name FROM assets")}
    assert [row[1] for row in repo.assets_needing_meta(list(ids.values()))] == ["a.png"]
    repo.store_meta({ids["a.png"]: (AssetMeta(width=4, height=4, alpha=True), meta_key(1, 2.0))})
    assert repo.meta_by_asset([ids["a.png"]])[ids["a.png"]].alpha is True
    repo.close_thread_connection()


def test_images_inside_archives_are_told_apart_too(tmp_path):
    from core import archives
    from core.metadata import read_details
    from zip_maker import Entry, make_zip

    def png(image):
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()

    archive = make_zip(tmp_path / "pack.zip", [
        Entry("alphas/rock.png", png(ramp16((64, 64)))),
        Entry("previews/render.png", png(Image.new("RGB", (64, 64), (200, 80, 30)))),
    ])
    found = {a.name: a for a in archives.entry_assets(archive, [".png"])}
    assert read_details(str(found["rock.png"].path), ".png").alpha is True
    assert read_details(str(found["render.png"].path), ".png").alpha is False
    archives.forget_listings()


# ----------------------------------------------------------------------
# Thumbnails
# ----------------------------------------------------------------------
def thumbnail(asset, relief=True, alpha=None):
    from core.thumbnail_manager import _Request, _ThumbnailRunnable, card_style, disk_cache_path

    runnable = _ThumbnailRunnable(_Request("x"), asset, 110, disk_cache_path(asset, 110), card_style(),
                                  relief=relief, alpha=alpha)
    pictures = []
    runnable.signals.ready.connect(lambda _request, image: pictures.append(image))
    runnable.run()
    return pictures[0], runnable


def test_alphas_get_a_relief_thumbnail_cached_apart(tmp_path, qapp):
    from core.scanner import AssetScanner
    from core.thumbnail_manager import disk_cache_path

    save(ramp16((128, 128)), tmp_path / "rock_alpha.png")
    save(Image.new("RGB", (128, 128), (30, 90, 220)), tmp_path / "photo.png")
    assets = {a.name: a for a in AssetScanner(supported_extensions=[".png"]).scan(tmp_path)}

    card, job = thumbnail(assets["rock_alpha.png"])
    centre = card.pixelColor(55, 45)
    assert job._made_relief and centre.red() > centre.blue()  # (clay, not the grey ramp)
    assert disk_cache_path(assets["rock_alpha.png"], 110, relief=True).is_file()
    assert not disk_cache_path(assets["rock_alpha.png"], 110).is_file()

    _card, job = thumbnail(assets["photo.png"])
    assert not job._made_relief and disk_cache_path(assets["photo.png"], 110).is_file()

    _card, job = thumbnail(assets["rock_alpha.png"], relief=False)  # (the setting off: the flat picture)
    assert not job._made_relief and disk_cache_path(assets["rock_alpha.png"], 110).is_file()


def test_an_alpha_inside_an_archive_gets_a_relief(tmp_path, qapp):
    from core import archives
    from zip_maker import Entry, make_zip

    out = io.BytesIO()
    ramp16((64, 64)).save(out, "PNG")
    archive = make_zip(tmp_path / "pack.zip", [Entry("rock.png", out.getvalue())])
    (asset,) = archives.entry_assets(archive, [".png"])
    _card, job = thumbnail(asset)
    assert job._made_relief
    archives.forget_listings()


# ----------------------------------------------------------------------
# The image viewer: Tile, and an alpha in 3D
# ----------------------------------------------------------------------
def pump(qapp, condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def press(dialog, key):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    dialog.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


def open_viewer(path: Path):
    from core.scanner import AssetScanner
    from ui.viewer import AssetViewerDialog

    (asset,) = [a for a in AssetScanner(supported_extensions=[path.suffix]).scan(path.parent) if a.path == path]
    return AssetViewerDialog(asset)


def test_tile_repeats_the_image_2x2_or_3x3(tmp_path, qapp):
    from PySide6.QtCore import Qt

    from core.settings import get_model_view

    dialog = open_viewer(save(Image.new("RGB", (64, 64), (200, 80, 30)), tmp_path / "brick.png"))
    viewer = dialog.tex_viewer
    assert viewer.tiles == 1
    press(dialog, Qt.Key.Key_T)
    assert dialog.btn_tile.isChecked() and viewer.tiles == 2
    dialog.tile_choice.button(3).click()
    assert viewer.tiles == 3 and get_model_view()["tile"] == 3
    press(dialog, Qt.Key.Key_T)
    assert viewer.tiles == 1
    assert pump(qapp, lambda: True) and not dialog.btn_3d.isVisibleTo(dialog)  # (not an alpha: no 3D)
    dialog.deleteLater()


def test_an_alpha_in_3d_with_height_invert_and_detail(tmp_path, qapp):
    from PySide6.QtCore import Qt

    from core.settings import get_model_view

    dialog = open_viewer(save(ramp16((96, 64)), tmp_path / "rock_alpha.png"))
    dialog.show()
    assert pump(qapp, lambda: dialog.btn_3d.isVisible())
    press(dialog, Qt.Key.Key_3)
    viewport = dialog._alpha_viewport
    assert pump(qapp, lambda: viewport.mesh is not None)
    assert viewport.mesh.vertex_count == 96 * 64  # (smaller than 512: kept as it is)
    top_before = viewport.mesh.vertices[:, 1].argmax()

    press(dialog, Qt.Key.Key_I)
    assert dialog.btn_alpha_invert.isChecked()
    assert pump(qapp, lambda: viewport.mesh.vertices[:, 1].argmax() != top_before)

    dialog.alpha_detail.button(1024).click()
    assert get_model_view()["alpha_plane"] == 1024
    press(dialog, Qt.Key.Key_3)
    assert dialog._image_stack.currentWidget() is dialog.tex_viewer
    dialog.close()
    dialog.deleteLater()


def test_settings_switches_alpha_thumbnails_between_relief_and_normal(tmp_path, qapp, monkeypatch):
    """Through the real window: Save redraws the grid's alphas, and each way is remembered."""
    import main
    from PySide6.QtWidgets import QDialog

    from core.settings import get_model_view, set_scan_folders
    from ui.settings_dialog import SettingsDialog

    library = tmp_path / "library"
    library.mkdir()
    save(ramp16((128, 128)), library / "rock_alpha.png")
    set_scan_folders([library])
    window = main.MainWindow()
    window.show()
    assert pump(qapp, lambda: window.grid_model.rowCount() == 1)
    asset = window.grid_model.asset_at(0)

    def picture_pixel():
        """The middle of its picture, once its thumbnail has been made."""
        assert pump(qapp, lambda: window.thumbnail_manager.get_thumbnail(asset, window.thumbnail_size) is not None
                    and window.thumbnail_manager.cached(asset) is not None)
        image = window.thumbnail_manager.cached(asset).toImage()
        return image.pixelColor(image.width() // 2, image.height() // 2 - 10)

    def choose(button_name):
        def fake_exec(dialog):
            page = dialog.page_appearance
            assert page.btn_alpha_relief.isChecked() == get_model_view()["alpha_relief"]
            assert page.btn_alpha_relief.toolTip() and page.btn_alpha_normal.toolTip()
            getattr(page, button_name).click()
            dialog._on_save()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(SettingsDialog, "exec", fake_exec)
        window._open_settings()

    relief = picture_pixel()
    assert relief.red() > relief.blue()  # clay

    choose("btn_alpha_normal")
    assert get_model_view()["alpha_relief"] is False
    normal = picture_pixel()
    assert normal.red() == normal.green() == normal.blue()  # the flat grey image

    choose("btn_alpha_relief")
    assert get_model_view()["alpha_relief"] is True
    again = picture_pixel()
    assert again.red() > again.blue()
    window.close()
