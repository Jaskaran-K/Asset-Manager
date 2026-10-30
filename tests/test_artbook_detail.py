"""Level of detail: every image keeps a small copy, images in view get a copy as sharp as they're
seen (up to the whole picture), sharp copies go once an image is out of view, and exports and
copies are made from sharp pictures whatever is loaded."""

from __future__ import annotations

import os
import time

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPointF
from PySide6.QtWidgets import QApplication

from core import artbook_store as store
from ui import artbook_board
from ui.animations import set_motion_mode
from ui.artbook_board import BASE_SIDE, detail_side


def pump(qapp, condition=lambda: False, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture(autouse=True)
def no_motion():
    set_motion_mode("off")
    yield
    set_motion_mode("on")


@pytest.fixture
def panel(qapp):
    from ui.artbook import ArtbookPanel

    widget = ArtbookPanel()
    widget.resize(1000, 700)
    widget.show()
    widget.sidebar.set_open(False, animate=False)
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def picture(folder, name, size, color="red"):
    path = folder / name
    Image.new("RGB", size, color).save(path)
    return path


def open_board(qapp, panel, images, zoom=1.0, center=(0.0, 0.0)):
    """A page with these images ({"path", "x", "y", "size"}), shown at zoom around center."""
    page = store.create_page("Detail")
    page["items"] = [
        {"id": f"i{n}", "kind": "image", "source": {"path": str(image["path"]), "blob": None},
         "size": list(image["size"]), "x": image.get("x", 0.0), "y": image.get("y", 0.0), "scale": 1.0, "z": n}
        for n, image in enumerate(images)
    ]
    page["view"] = {"zoom": zoom, "x": center[0], "y": center[1]}
    store.save_page(page)
    panel.open_page(page["id"])
    return panel.board


def side(item):
    return max(item.pixmap.width(), item.pixmap.height()) if item.pixmap is not None else 0


def settled(board):
    """Nothing more to load for how the board is seen now."""
    board.update_detail()
    return board._loads == 0 and not board._detail_timer.isActive()


def test_detail_side_steps_and_caps():
    assert detail_side((3000, 2000), 100) == 0  # the small copy does
    assert detail_side((100, 80), 2000) == 0  # it's no bigger than the small copy
    assert detail_side((3000, 2000), 300) == BASE_SIDE * 2
    assert detail_side((3000, 2000), 700) == BASE_SIDE * 4  # (a little under is fine: DETAIL_STEP)
    assert detail_side((3000, 2000), 800) == BASE_SIDE * 8  # (powers of two: zooming doesn't decode every step)
    assert detail_side((3000, 2000), 100000) == 3000  # the whole picture, no more
    huge = detail_side((20000, 20000), 100000)
    assert huge * huge * 4 <= artbook_board.DETAIL_MAX_BYTES


def test_zoomed_out_images_keep_only_the_small_copy(qapp, panel, tmp_path):
    path = picture(tmp_path, "big.png", (3000, 2000))
    board = open_board(qapp, panel, [{"path": path, "size": (3000, 2000)}], zoom=0.05)
    item = board.item("i0")
    assert pump(qapp, lambda: item.pixmap is not None and settled(board))
    assert side(item) == BASE_SIDE and item.detail is None


def test_zooming_in_loads_the_whole_picture(qapp, panel, tmp_path):
    path = picture(tmp_path, "big.png", (3000, 2000))
    board = open_board(qapp, panel, [{"path": path, "size": (3000, 2000)}], zoom=0.05)
    item = board.item("i0")
    assert pump(qapp, lambda: item.pixmap is not None and settled(board))
    board.set_view(1.0, QPointF(0, 0))
    ratio = board.devicePixelRatioF()
    assert pump(qapp, lambda: settled(board) and side(item) == 3000), side(item)
    assert item.base is not None and max(item.base.width(), item.base.height()) == BASE_SIDE
    board.set_view(0.2 / ratio, QPointF(0, 0))  # (600 px on screen: a 640 copy does, the full one goes)
    assert pump(qapp, lambda: settled(board) and side(item) == BASE_SIDE * 4), side(item)


def test_images_out_of_view_let_their_sharp_copy_go(qapp, panel, tmp_path):
    path = picture(tmp_path, "big.png", (2000, 2000))
    far = 200000.0
    board = open_board(qapp, panel, [
        {"path": path, "size": (2000, 2000)},
        {"path": path, "size": (2000, 2000), "x": far},
    ], zoom=0.25)
    here, there = board.item("i0"), board.item("i1")
    assert pump(qapp, lambda: settled(board) and here.detail is not None and there.base_ok)
    assert there.detail is None  # loaded small: it's nowhere near the view
    board.set_view(0.25, QPointF(far, 0))
    assert pump(qapp, lambda: settled(board) and there.detail is not None)
    assert here.detail is None and here.base is not None  # what's left behind keeps its small copy


def test_pictures_load_from_the_middle_of_the_view_outwards(qapp, panel, tmp_path, monkeypatch):
    path = picture(tmp_path, "big.png", (1000, 1000))
    images = [{"path": path, "size": (1000, 1000), "x": x * 1200.0} for x in (6, -3, 0, 2, 9)]
    monkeypatch.setattr(artbook_board, "DETAIL_LOADS", 1)
    started = []
    real_load = artbook_board.BoardView._load

    def load(self, batch):
        started.extend(item.item_id for item, _side in batch)
        real_load(self, batch)

    monkeypatch.setattr(artbook_board.BoardView, "_load", load)
    board = open_board(qapp, panel, images, zoom=0.1)
    assert pump(qapp, lambda: settled(board) and all(i.base_ok for i in board.items_list()))
    assert started[:5] == ["i2", "i3", "i1", "i0", "i4"]


def test_export_and_copy_are_sharp_when_zoomed_out(qapp, panel, tmp_path):
    path = picture(tmp_path, "big.png", (3000, 2000), "blue")
    board = open_board(qapp, panel, [{"path": path, "size": (3000, 2000)}], zoom=0.03)
    item = board.item("i0")
    assert pump(qapp, lambda: item.pixmap is not None and settled(board))
    assert side(item) == BASE_SIDE
    exported = board.export_image()
    assert exported.width() >= 3000  # (the picture's own size, plus the margin)
    assert side(item) == BASE_SIDE  # (the sharp picture was only borrowed)
    plan = board.export_plan()
    assert plan[3] and plan[3][0][2] == 3000
    board._scene.clearSelection()
    item.setSelected(True)
    assert board.copy_selection()
    assert QApplication.clipboard().image().width() == 3000


def test_the_page_copy_is_used_for_small_sizes_while_its_current(qapp, tmp_path):
    page = store.create_page("Copies")
    path = picture(tmp_path, "linked.png", (2000, 1000), "red")
    data, ext = store.encode_fallback(store.decode_file(path)[0])
    blob = store.store_blob(page["id"], data, ext)
    copy = store.blob_path(page["id"], blob)
    source = {"path": str(path), "blob": blob}
    os.utime(path, (time.time() - 100, time.time() - 100))  # the file is older than its copy
    small = artbook_board._decode_saved(page["id"], source, BASE_SIDE)
    assert small.pixelColor(5, 5).red() > 240 and small.pixelColor(5, 5).green() < 15  # (the copy: a JPEG)
    Image.new("RGB", (2000, 1000), "green").save(path)  # edited after it was added: the copy is stale
    os.utime(copy, (time.time() - 100, time.time() - 100))
    small = artbook_board._decode_saved(page["id"], source, BASE_SIDE)
    assert small.pixelColor(5, 5).name() == "#008000"
    assert max(small.width(), small.height()) == BASE_SIDE


def test_small_copies_are_cached_so_a_page_opens_quickly(qapp, panel, tmp_path, monkeypatch):
    from core import video_tools

    path = picture(tmp_path, "big.png", (3000, 2000))
    board = open_board(qapp, panel, [{"path": path, "size": (3000, 2000)}], zoom=0.03)
    assert pump(qapp, lambda: board.item("i0").base_ok and settled(board))
    cached = list(video_tools.artbook_cache_dir().glob("*.png"))
    assert len(cached) == 1
    decoded = []
    real = artbook_board._decode_saved
    monkeypatch.setattr(artbook_board, "_decode_saved", lambda *args: decoded.append(args) or real(*args))
    page_id = panel.current_page_id()
    panel.new_page()
    panel.open_page(page_id)
    item = panel.board.item("i0")
    assert pump(qapp, lambda: item.base_ok and settled(panel.board))
    assert decoded == [] and side(item) == BASE_SIDE  # (read from the cache)
    os.utime(path, (time.time() + 10, time.time() + 10))  # the file changed: decoded again
    panel.new_page()
    panel.open_page(page_id)
    item = panel.board.item("i0")
    assert pump(qapp, lambda: item.base_ok and settled(panel.board))
    assert len(decoded) == 1
    video_tools.clear_video_cache()
    assert not list(video_tools.artbook_cache_dir().glob("*.png"))
