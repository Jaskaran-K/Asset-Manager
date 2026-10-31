"""Motion on the board: images glide to where a layout (or an undo) puts them, and pictures fade
in when they arrive. Both follow the app's motion setting, and nothing reads a state mid-glide."""

from __future__ import annotations

import time

import pytest
from PIL import Image
from PySide6.QtCore import QEvent

from core import artbook_store as store
from ui.animations import set_motion_mode


def pump(qapp, condition=lambda: False, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def motion():
    set_motion_mode("on")
    yield
    set_motion_mode("on")


@pytest.fixture
def panel(qapp, motion):
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


def board_with(qapp, panel, tmp_path, positions):
    path = tmp_path / "red.png"
    Image.new("RGB", (200, 100), "red").save(path)
    page = store.create_page("Motion")
    page["items"] = [
        {"id": f"i{n}", "kind": "image", "source": {"path": str(path), "blob": None}, "size": [200, 100],
         "x": x, "y": y, "scale": 1.0, "z": n}
        for n, (x, y) in enumerate(positions)
    ]
    page["view"] = {"zoom": 0.5, "x": 0.0, "y": 0.0}
    store.save_page(page)
    panel.open_page(page["id"])
    board = panel.board
    assert pump(qapp, lambda: all(i.base_ok for i in board.items_list()))
    return board


def select_all(board):
    for item in board.items_list():
        item.setSelected(True)


def test_a_layout_glides_into_place(qapp, panel, tmp_path):
    board = board_with(qapp, panel, tmp_path, [(0, 0), (400, 300)])
    select_all(board)
    board.align("left")
    moving = board.item("i1")
    assert board._moves and moving.pos().x() == pytest.approx(400.0)  # it sets off from where it was...
    assert pump(qapp, lambda: not board._moves, seconds=2)
    assert moving.pos().x() == pytest.approx(0.0)  # ...and it ends exactly there


def test_reading_states_mid_glide_gets_where_its_going(qapp, panel, tmp_path):
    board = board_with(qapp, panel, tmp_path, [(0, 0), (400, 300)])
    select_all(board)
    board.align("top")
    assert board._moves
    states = {d["id"]: d for d in board.page_items()}  # (what's saved)
    assert states["i1"]["y"] == pytest.approx(0.0)
    assert not board._moves


def test_undo_glides_back(qapp, panel, tmp_path):
    board = board_with(qapp, panel, tmp_path, [(0, 0), (400, 300)])
    select_all(board)
    board.align("left")
    board.finish_moves()
    board.undo_stack.undo()
    assert board._moves
    assert pump(qapp, lambda: not board._moves, seconds=2)
    assert board.item("i1").pos().x() == pytest.approx(400.0)


def test_rotation_glides_the_short_way(qapp, panel, tmp_path):
    from dataclasses import replace

    board = board_with(qapp, panel, tmp_path, [(0, 0)])
    item = board.item("i0")
    item.setRotation(350.0)
    before = item.state()
    board.apply_states({"i0": replace(before, rotation=10.0)}, animate=True)
    board._on_move_step(0.5)
    assert item.rotation() == pytest.approx(360.0)  # (350 -> 370: through 0, not back through 180)
    board.finish_moves()
    assert item.rotation() == pytest.approx(10.0)


def test_motion_off_moves_at_once(qapp, panel, tmp_path):
    board = board_with(qapp, panel, tmp_path, [(0, 0), (400, 300)])
    set_motion_mode("off")
    select_all(board)
    board.align("left")
    assert not board._moves
    assert board.item("i1").pos().x() == pytest.approx(0.0)


def test_pictures_fade_in_when_they_arrive(qapp, panel, tmp_path):
    path = tmp_path / "blue.png"
    Image.new("RGB", (200, 100), "blue").save(path)
    page = store.create_page("Fade")
    page["items"] = [{"id": "a", "kind": "image", "source": {"path": str(path), "blob": None}, "size": [200, 100], "x": 0, "y": 0}]
    store.save_page(page)
    panel.open_page(page["id"])
    item = panel.board.item("a")
    assert pump(qapp, lambda: item.pixmap is not None)
    assert item.fade_from is not None and item.fade() < 1.0
    assert pump(qapp, lambda: item.fade_from is None, seconds=2)
    assert pump(qapp, lambda: not panel.board._fade_timer.isActive(), seconds=2)  # (it stops on its next tick)
