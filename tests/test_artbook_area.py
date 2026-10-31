"""The board's active area: a lighter panel behind everything on the page that grows, shrinks and
moves (gliding, with motion on) as items are added, spread out or gathered. Exports leave it out."""

from __future__ import annotations

import time

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPointF

from core import artbook_store as store
from ui.animations import set_motion_mode


def pump(qapp, condition=lambda: False, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def panel(qapp):
    from ui.artbook import ArtbookPanel

    set_motion_mode("off")
    widget = ArtbookPanel()
    widget.resize(1000, 700)
    widget.show()
    widget.sidebar.set_open(False, animate=False)
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    set_motion_mode("on")


def open_page(qapp, panel, tmp_path, positions):
    path = tmp_path / "red.png"
    Image.new("RGB", (200, 100), "red").save(path)
    page = store.create_page("Area")
    page["items"] = [
        {"id": f"i{n}", "kind": "image", "source": {"path": str(path), "blob": None}, "size": [200, 100],
         "x": x, "y": y, "scale": 1.0, "z": n}
        for n, (x, y) in enumerate(positions)
    ]
    page["view"] = {"zoom": 0.5, "x": 0.0, "y": 0.0}
    store.save_page(page)
    panel.open_page(page["id"])
    qapp.processEvents()
    return panel.board


def area(board):
    return board._scene.active_area[0] if board._scene.active_area else None


def test_an_empty_board_has_no_area(qapp, panel):
    qapp.processEvents()
    assert panel.board.items_list() == [] and area(panel.board) is None


def test_the_area_wraps_everything_with_room_to_spare(qapp, panel, tmp_path):
    board = open_page(qapp, panel, tmp_path, [(0, 0), (600, 400)])
    assert pump(qapp, lambda: area(board) is not None)
    box = area(board)
    for item in board.items_list():
        inner = item.sceneBoundingRect()
        assert box.contains(inner) and box.left() < inner.left() and box.bottom() > inner.bottom()
    assert board._scene.active_area[1] > 0  # (rounded)


def test_the_area_follows_an_image_that_moves_away(qapp, panel, tmp_path):
    board = open_page(qapp, panel, tmp_path, [(0, 0), (300, 0)])
    assert pump(qapp, lambda: area(board) is not None)
    before = area(board)
    set_motion_mode("on")
    far = board.item("i1")
    far.setPos(QPointF(3000, 1500))
    assert pump(qapp, lambda: board._area_target is not None and board._area_target[0].contains(far.sceneBoundingRect()))
    assert board._area_anim.state() == board._area_anim.State.Running  # (it glides there...)
    assert pump(qapp, lambda: area(board) == board._area_target[0])  # (...and arrives)
    assert area(board).width() > before.width() * 4
    far.setPos(QPointF(300, 0))  # gathered back: it shrinks again
    assert pump(qapp, lambda: area(board) is not None and area(board).width() == pytest.approx(before.width()))


def test_exports_and_thumbnails_leave_the_area_out(qapp, panel, tmp_path):
    from ui.themes import theme_color

    board = open_page(qapp, panel, tmp_path, [(0, 0), (600, 400)])
    assert pump(qapp, lambda: area(board) is not None and all(i.pixmap is not None for i in board.items_list()))
    exported = board.export_image({})
    assert exported.pixelColor(2, 2).rgb() == theme_color("grid_bg").rgb()  # (the board's own colour at the edge)
    assert board._scene.show_area  # (back on for the board)
    thumb = board.thumbnail()
    assert thumb.pixelColor(1, 1).alpha() == 0  # (transparent around the items, as before)
