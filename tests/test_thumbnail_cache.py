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


def test_models_are_not_disk_cached(tmp_path):
    (tmp_path / "hero.fbx").write_bytes(b"fbx")
    asset = AssetScanner().scan(tmp_path)[0]
    assert tm.disk_cache_path(asset, 110) is None


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
