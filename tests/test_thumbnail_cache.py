"""Thumbnails are cached on disk, keyed so edits and theme changes never show a stale picture."""

from __future__ import annotations

import os
import time

from PIL import Image

from core import thumbnail_manager as tm
from core import video_tools
from core.scanner import AssetScanner


def wait_for(qapp, condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    return condition()


def make_png(tmp_path, name="brick.png", color="red"):
    path = tmp_path / name
    Image.new("RGB", (64, 64), color).save(path)
    return AssetScanner().scan(tmp_path)[0]


def test_second_load_comes_from_disk(tmp_path, qapp, monkeypatch):
    asset = make_png(tmp_path)
    manager = tm.ThumbnailManager()
    loaded = []
    manager.thumbnail_loaded.connect(lambda path, pixmap: loaded.append(path))
    manager.get_thumbnail(asset, 110)
    assert wait_for(qapp, lambda: loaded)
    cache_file = tm.disk_cache_path(asset, 110)
    assert cache_file.is_file() and cache_file.parent == video_tools.THUMB_DIR

    renders = []
    monkeypatch.setattr(tm._ThumbnailRunnable, "_render", lambda self: renders.append(1))
    fresh = tm.ThumbnailManager()  # a new session: nothing in memory
    fresh_loaded = []
    fresh.thumbnail_loaded.connect(lambda path, pixmap: fresh_loaded.append(pixmap))
    fresh.get_thumbnail(asset, 110)
    assert wait_for(qapp, lambda: fresh_loaded)
    assert renders == [] and fresh_loaded[0].width() == 110


def test_cache_key_changes_with_the_file_size_and_theme(tmp_path, qapp):
    asset = make_png(tmp_path)
    key = tm.disk_cache_path(asset, 110)
    asset.modified_timestamp += 1
    edited = tm.disk_cache_path(asset, 110)
    assert edited != key
    assert tm.disk_cache_path(asset, 128) != edited
    original = dict(tm._CARD_STYLE)
    try:
        tm.set_thumbnail_card_style(original["card"].lighter(150), original["border"], original["badge"], original["badge_text"])
        assert tm.disk_cache_path(asset, 110) != edited
    finally:
        tm._CARD_STYLE.update(original)


CUBE_OBJ = """v -1 -1 -1
v 1 -1 -1
v 1 1 -1
v -1 1 -1
v -1 -1 1
v 1 -1 1
v 1 1 1
v -1 1 1
f 1 3 2
f 1 4 3
f 5 6 7
f 5 7 8
f 1 2 6
f 1 6 5
f 4 8 7
f 4 7 3
f 2 3 7
f 2 7 6
f 1 5 8
f 1 8 4
"""


def make_model(tmp_path, name="crate.obj"):
    (tmp_path / name).write_text(CUBE_OBJ, encoding="utf-8")
    return next(a for a in AssetScanner().scan(tmp_path) if a.name == name)


def test_a_model_gets_a_rendered_picture_kept_on_disk(tmp_path, qapp):
    asset = make_model(tmp_path)
    manager = tm.ThumbnailManager()
    placeholder = manager.get_thumbnail(asset, 110)
    assert wait_for(qapp, lambda: manager.cached(asset) is not None)
    picture = manager.cached(asset).toImage()
    assert picture.cacheKey() != placeholder.toImage().cacheKey()
    model_pixels = [picture.pixelColor(x, 45) for x in range(30, 80)]
    assert any(abs(c.red() - c.blue()) > 20 for c in model_pixels)  # (the model's blue-grey, on the card)
    assert tm.disk_cache_path(asset, 110).is_file()
    manager.shutdown()


def test_a_picture_next_to_the_model_wins_and_isnt_cached(tmp_path, qapp):
    asset = make_model(tmp_path)
    Image.new("RGB", (64, 64), "red").save(tmp_path / "crate.png")
    manager = tm.ThumbnailManager()
    manager.get_thumbnail(asset, 110)
    assert wait_for(qapp, lambda: manager.cached(asset) is not None)
    assert manager.cached(asset).toImage().pixelColor(55, 45).red() > 200
    assert not tm.disk_cache_path(asset, 110).is_file()
    manager.shutdown()


def test_big_models_get_their_picture_when_viewed(tmp_path, qapp, monkeypatch):
    from core.model_loader import load_3d_model

    monkeypatch.setattr(tm, "MAX_RENDERED_MODEL_BYTES", 10)  # (the cube's file is "big")
    asset = make_model(tmp_path)
    manager = tm.ThumbnailManager()
    manager.get_thumbnail(asset, 110)
    qapp.processEvents()
    assert not manager._pending and manager.cached(asset) is None  # (not read just for the grid)
    manager.model_viewed(asset, load_3d_model(asset.path))  # (the viewer loaded it)
    assert wait_for(qapp, lambda: manager.cached(asset) is not None)
    assert tm.disk_cache_path(asset, 110).is_file()
    later = tm.ThumbnailManager()  # (the next time the app starts: from the disk)
    later.get_thumbnail(asset, 110)
    assert wait_for(qapp, lambda: later.cached(asset) is not None)
    manager.shutdown()
    later.shutdown()


def test_no_model_pictures_when_turned_off_or_unreadable(tmp_path, qapp):
    asset = make_model(tmp_path)
    (tmp_path / "broken.fbx").write_bytes(b"not a model")
    broken = next(a for a in AssetScanner().scan(tmp_path) if a.name == "broken.fbx")
    manager = tm.ThumbnailManager()
    manager.render_models = False
    manager.get_thumbnail(asset, 110)
    assert not manager._pending
    manager.render_models = True
    manager.get_thumbnail(broken, 110)
    manager._model_pool.waitForDone(10000)
    qapp.processEvents()
    assert manager.cached(broken) is None  # (no picture of the stand-in cube)
    manager.shutdown()


def test_pruning_drops_least_recently_used(tmp_path):
    video_tools.THUMB_DIR.mkdir(parents=True)
    for i in range(10):
        f = video_tools.THUMB_DIR / f"{i}.png"
        f.write_bytes(b"x" * 1000)
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    assert tm.prune_disk_cache(max_bytes=20_000) == 0  # under the limit: nothing to do
    removed = tm.prune_disk_cache(max_bytes=5_000)
    left = sorted(p.name for p in video_tools.THUMB_DIR.iterdir())
    assert removed == 6 and left == ["6.png", "7.png", "8.png", "9.png"]


def test_placeholders_are_drawn_once_per_type(tmp_path, qapp):
    (tmp_path / "a.fbx").write_bytes(b"1")
    (tmp_path / "b.fbx").write_bytes(b"2")
    a, b = AssetScanner().scan(tmp_path)
    manager = tm.ThumbnailManager()
    assert manager.get_thumbnail(a, 110).cacheKey() == manager.get_thumbnail(b, 110).cacheKey()
