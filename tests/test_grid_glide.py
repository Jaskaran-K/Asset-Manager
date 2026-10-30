"""The asset grid's cards glide to their new places when a resize changes the rows (ui.asset_grid.GridView)."""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QEvent, QSize, QStringListModel
from PySide6.QtWidgets import QListView

from ui.animations import set_motion_mode
from ui.asset_grid import GridView


def pump(qapp, condition=lambda: False, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def grid(qapp):
    set_motion_mode("on")
    view = GridView()
    view.setViewMode(QListView.ViewMode.IconMode)
    view.setResizeMode(QListView.ResizeMode.Adjust)
    view.setMovement(QListView.Movement.Static)
    view.setUniformItemSizes(True)
    view.setGridSize(QSize(100, 100))
    view.setModel(QStringListModel([f"card {n}" for n in range(30)], view))
    view.resize(530, 400)
    view.show()
    assert pump(qapp, lambda: view._columns == 5)
    yield view
    view.close()
    view.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    set_motion_mode("on")


def test_cards_glide_when_the_rows_change(qapp, grid):
    card = grid.model().index(7, 0)  # 5 to a row: row 1, column 2
    grid.resize(330, 400)  # 3 to a row: row 2, column 1
    assert pump(qapp, lambda: grid._columns == 3)
    offset = grid.glide_offset(card)
    assert offset is not None
    assert offset.x() > 0 and offset.y() < 0  # drawn back toward where it was (right and up)...
    assert offset.x() <= 100 and offset.y() >= -100
    assert pump(qapp, lambda: grid.glide_offset(card) is None)  # ...and it arrives


def test_a_resize_that_keeps_the_rows_doesnt_glide(qapp, grid):
    grid.resize(560, 420)  # (still 5 to a row)
    qapp.processEvents()
    assert grid._columns == 5 and grid.glide_offset(grid.model().index(7, 0)) is None


def test_motion_off_moves_cards_at_once(qapp, grid):
    set_motion_mode("off")
    grid.resize(330, 400)
    assert pump(qapp, lambda: grid._columns == 3)
    assert grid.glide_offset(grid.model().index(7, 0)) is None
