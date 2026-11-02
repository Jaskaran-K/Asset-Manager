"""The image viewer (ui.viewer + core.full_image): decoding off the UI thread, big images shown
smaller, files too big or unreadable say so, and loads stop when the viewer moves on."""

from __future__ import annotations

import os
import threading
import time

from PIL import Image
from PySide6.QtWidgets import QLabel

from core import full_image
from core.scanner import AssetItem
from tests.test_model_viewer import close, model_asset, open_viewer, pump


def image_asset(path):
    return AssetItem(path, path.name, path.suffix, os.path.getsize(path), os.path.getmtime(path), "Textures", path.suffix[1:].upper())


def inspector_texts(dialog):
    return {label.text() for label in dialog.inspector_panel.findChildren(QLabel)}


def test_the_image_loads_in_the_background(qapp, tmp_path):
    path = tmp_path / "brick.png"
    Image.new("RGBA", (300, 100), (200, 50, 50, 128)).save(path)
    dialog = open_viewer(qapp, image_asset(path))
    viewer = dialog.tex_viewer
    assert viewer.pixmap.isNull() and viewer.message.startswith("Loading")  # (the window is up first)
    assert pump(qapp, lambda: not viewer.pixmap.isNull())
    assert viewer.has_alpha and viewer.show_checkerboard and dialog._btn_checker.isChecked()
    assert "300 × 100 px" in inspector_texts(dialog)
    close(qapp, dialog)


def test_big_images_are_shown_smaller_with_their_real_size(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(full_image, "MAX_SIDE", 64)
    path = tmp_path / "wide.jpg"
    Image.new("RGB", (300, 100), "gray").save(path)
    dialog = open_viewer(qapp, image_asset(path))
    assert pump(qapp, lambda: not dialog.tex_viewer.pixmap.isNull())
    assert dialog.tex_viewer.pixmap.width() == 64
    texts = inspector_texts(dialog)
    assert "300 × 100 px" in texts and any(t.startswith("64 × 21 px") for t in texts)
    close(qapp, dialog)


def test_too_big_or_unreadable_images_say_why(qapp, tmp_path, monkeypatch):
    huge = tmp_path / "huge.png"
    Image.new("L", (200, 100)).save(huge)
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a picture")
    monkeypatch.setattr(full_image, "MAX_PIXELS", 10_000)
    for path, says in ((huge, "more than the viewer opens"), (broken, "couldn't be opened")):
        dialog = open_viewer(qapp, image_asset(path))
        assert pump(qapp, lambda: says in dialog.tex_viewer.message), dialog.tex_viewer.message
        close(qapp, dialog)


def test_pillows_size_limit_is_lifted_for_the_viewer_only(qapp, tmp_path, monkeypatch):
    path = tmp_path / "bomb.png"
    Image.new("L", (200, 100)).save(path)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # (200 x 100 is a "decompression bomb" now)
    loaded = full_image.load_full_image(path)
    assert loaded.size == (200, 100)
    try:
        Image.open(path)  # elsewhere (the grid's thumbnails), Pillow's limit still applies
        raise AssertionError("Pillow's limit was lifted everywhere")
    except Image.DecompressionBombError:
        pass


def test_one_decode_at_a_time_and_unwanted_ones_are_skipped(tmp_path):
    path = tmp_path / "a.png"
    Image.new("RGB", (10, 10)).save(path)
    assert full_image.load_full_image(path, still_wanted=lambda: False) is None
    held = threading.Event()
    with full_image._DECODES:  # (another decode running)
        worker = threading.Thread(target=lambda: held.set() or full_image.load_full_image(path))
        worker.start()
        time.sleep(0.1)
        assert worker.is_alive()  # waiting its turn
    worker.join(5)
    assert not worker.is_alive()


def test_closing_the_viewer_stops_a_model_being_read(qapp, tmp_path, monkeypatch):
    from ui import viewer

    stopped = []

    def slow(path, should_stop=None):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not should_stop():
            time.sleep(0.01)
        stopped.append(should_stop())
        raise MemoryError("(stopped)")

    monkeypatch.setattr(viewer, "load_3d_model", slow)
    dialog = open_viewer(qapp, model_asset(tmp_path))
    started = time.monotonic()
    close(qapp, dialog)
    assert pump(qapp, lambda: stopped == [True]) and time.monotonic() - started < 2
