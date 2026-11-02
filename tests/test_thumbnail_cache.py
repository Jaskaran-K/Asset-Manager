"""Thumbnails: cached on disk (without their card, so theme changes don't reread files) and in
memory (the most recently used), made newest first, and never stale after edits or theme changes."""

from __future__ import annotations

import os
import time

from PIL import Image
from PySide6.QtGui import QColor

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


def test_cache_key_changes_with_the_file_and_size_but_not_the_theme(tmp_path, qapp):
    asset = make_png(tmp_path)
    key = tm.disk_cache_path(asset, 110)
    asset.modified_timestamp += 1
    edited = tm.disk_cache_path(asset, 110)
    assert edited != key
    assert tm.disk_cache_path(asset, 128) != edited
    original = dict(tm._CARD_STYLE)
    try:
        tm.set_thumbnail_card_style(original["card"].lighter(150), original["border"], original["badge"], original["badge_text"])
        assert tm.disk_cache_path(asset, 110) == edited  # (the picture is stored without its card)
    finally:
        tm._CARD_STYLE.update(original)


def card_color(pixmap):
    return pixmap.toImage().pixelColor(5, pixmap.height() // 2).name()


def test_a_theme_change_reuses_the_pictures_in_new_colors(tmp_path, qapp, monkeypatch):
    asset = make_png(tmp_path)
    original = dict(tm._CARD_STYLE)
    try:
        tm.set_thumbnail_card_style(QColor("#442222"), QColor("#553333"), QColor("#221111"), QColor("#ffffff"))
        manager = tm.ThumbnailManager()
        manager.get_thumbnail(asset, 110)
        assert wait_for(qapp, lambda: manager.cached(asset) is not None)
        assert card_color(manager.cached(asset)) == "#442222"

        reads = []
        monkeypatch.setattr(tm, "load_texture_picture", lambda *args: reads.append(1))
        tm.set_thumbnail_card_style(QColor("#224422"), QColor("#335533"), QColor("#112211"), QColor("#ffffff"))
        manager.clear_cache()
        manager.get_thumbnail(asset, 110)
        assert wait_for(qapp, lambda: manager.cached(asset) is not None)
        assert card_color(manager.cached(asset)) == "#224422" and reads == []  # (from the disk cache)
        manager.shutdown()
    finally:
        tm._CARD_STYLE.update(original)


def slow_pictures(monkeypatch, seconds=0.05, order=None):
    real = tm.load_texture_picture

    def slow(path, size=110):
        if order is not None:
            order.append(path.name)
        time.sleep(seconds)
        return real(path, size)

    monkeypatch.setattr(tm, "load_texture_picture", slow)


def make_pngs(tmp_path, count):
    for i in range(count):
        Image.new("RGB", (32, 32), (i * 10, 80, 80)).save(tmp_path / f"img{i:02}.png")
    return AssetScanner().scan(tmp_path)


def test_newest_requests_are_made_first(tmp_path, qapp, monkeypatch):
    order = []
    slow_pictures(monkeypatch, order=order)
    assets = make_pngs(tmp_path, 8)
    manager = tm.ThumbnailManager()
    manager._thread_pool.setMaxThreadCount(1)
    for asset in assets:  # (scrolling down: each card asked for after the one before)
        manager.get_thumbnail(asset, 110)
    assert wait_for(qapp, lambda: len(manager._cache) == 8)
    assert order[1:] == [a.name for a in reversed(assets[1:])]  # (the first had already started)
    manager.shutdown()


def test_cards_that_scrolled_away_are_skipped(tmp_path, qapp, monkeypatch):
    slow_pictures(monkeypatch, seconds=0.1)
    monkeypatch.setattr(tm, "STALE_SECONDS", 0.05)
    assets = make_pngs(tmp_path, 6)
    manager = tm.ThumbnailManager()
    manager._thread_pool.setMaxThreadCount(1)
    skipped = []
    manager.thumbnail_skipped.connect(skipped.append)
    for asset in assets:
        manager.get_thumbnail(asset, 110)
    assert wait_for(qapp, lambda: not manager._pending)
    assert len(manager._cache) < 6 and len(skipped) == 6 - len(manager._cache)
    manager.get_thumbnail(assets[0], 110)  # back on screen: asked for again, and made
    assert wait_for(qapp, lambda: manager.cached(assets[0]) is not None)
    manager.shutdown()


def test_memory_keeps_the_most_recently_used(tmp_path, qapp):
    assets = make_pngs(tmp_path, 5)
    manager = tm.ThumbnailManager()
    manager.max_memory_bytes = 3 * 110 * 110 * 4  # room for three cards
    for asset in assets[:3]:
        manager.get_thumbnail(asset, 110)
    assert wait_for(qapp, lambda: len(manager._cache) == 3)
    manager.get_thumbnail(assets[0], 110)  # used again: now the most recent
    for asset in assets[3:]:
        manager.get_thumbnail(asset, 110)
    assert wait_for(qapp, lambda: manager.cached(assets[4]) is not None and manager.cached(assets[3]) is not None)
    assert [a.name for a in assets if manager.cached(a) is not None] == ["img00.png", "img03.png", "img04.png"]
    assert manager.memory_used() <= manager.max_memory_bytes
    manager.shutdown()


def test_results_from_before_a_reset_are_dropped(tmp_path, qapp, monkeypatch):
    slow_pictures(monkeypatch, seconds=0.1)
    asset = make_png(tmp_path)
    manager = tm.ThumbnailManager()
    manager.get_thumbnail(asset, 110)
    time.sleep(0.02)  # (its job is running)
    manager.clear_cache()  # e.g. the thumbnail size changed
    manager._thread_pool.waitForDone()
    qapp.processEvents()
    assert manager.cached(asset) is None  # (an old-size picture never lands)
    manager.get_thumbnail(asset, 144)
    assert wait_for(qapp, lambda: manager.cached(asset) is not None)
    assert manager.cached(asset).width() == 144
    manager.shutdown()


def same_picture(pixmap, image):
    return pixmap.toImage() == image.convertToFormat(pixmap.toImage().format())


def test_unreadable_and_huge_images_say_so(tmp_path, qapp, monkeypatch):
    big = make_png(tmp_path, "huge.png")
    (tmp_path / "broken.png").write_bytes(b"not an image")
    broken = next(a for a in AssetScanner().scan(tmp_path) if a.name == "broken.png")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # (64 x 64 is now "too large")
    manager = tm.ThumbnailManager()
    for asset in (big, broken):
        manager.get_thumbnail(asset, 110)
    assert wait_for(qapp, lambda: manager.cached(big) is not None and manager.cached(broken) is not None)
    assert same_picture(manager.cached(big), tm.render_placeholder_image(big, 110, note="Too large to preview"))
    assert same_picture(manager.cached(broken), tm.render_placeholder_image(broken, 110, note="No preview"))
    assert not manager._pending  # (not left waiting: they aren't asked for again this session)
    manager.shutdown()


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
    # (not a picture of the stand-in cube)
    assert same_picture(manager.cached(broken), tm.render_placeholder_image(broken, 110, note="No preview"))
    manager.shutdown()


def test_pruning_drops_old_versions_and_least_recently_used(tmp_path):
    video_tools.THUMB_DIR.mkdir(parents=True)
    for i in range(10):
        f = video_tools.THUMB_DIR / f"{tm._CACHE_PREFIX}{i}.png"
        f.write_bytes(b"x" * 1000)
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    (video_tools.THUMB_DIR / "0123abcd.png").write_bytes(b"x")  # an older version's
    assert tm.prune_disk_cache(max_bytes=20_000) == 1  # under the limit: only the old version goes
    removed = tm.prune_disk_cache(max_bytes=5_000)
    left = sorted(p.name.removeprefix(tm._CACHE_PREFIX) for p in video_tools.THUMB_DIR.iterdir())
    assert removed == 6 and left == ["6.png", "7.png", "8.png", "9.png"]


def test_placeholders_are_drawn_once_per_type(tmp_path, qapp):
    (tmp_path / "a.fbx").write_bytes(b"1")
    (tmp_path / "b.fbx").write_bytes(b"2")
    a, b = AssetScanner().scan(tmp_path)
    manager = tm.ThumbnailManager()
    assert manager.get_thumbnail(a, 110).cacheKey() == manager.get_thumbnail(b, 110).cacheKey()
