"""Phase 4, adjust: crop mode, per-image grayscale and opacity, the grayscale board; and the board
claiming its keys so a key shared with another window acts on the one you're working in."""

from __future__ import annotations

import time

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QSlider

from ui import shortcuts
from ui.animations import set_motion_mode


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
def panel(qapp, tmp_path):
    from ui.artbook import ArtbookPanel

    paths = []
    for n, color in enumerate(["red", "blue"]):
        path = tmp_path / f"img{n}.png"
        Image.new("RGB", (400, 200), color).save(path)
        paths.append(path)
    widget = ArtbookPanel()
    widget.resize(1000, 700)
    widget.show()
    widget.sidebar.set_open(False, animate=False)
    qapp.processEvents()
    widget.board.add_files(paths)
    assert pump(qapp, lambda: len(widget.board.items_list()) == 2 and all(i.pixmap for i in widget.board.items_list()))
    yield widget
    widget.close()


def key(board, shortcut_id):
    sequence = shortcuts.sequence(shortcut_id)[0]
    event = QKeyEvent(QEvent.Type.KeyPress, sequence.key(), sequence.keyboardModifiers())
    board.keyPressEvent(event)


def only(board, item):
    board._scene.clearSelection()
    item.setSelected(True)


def view_point(board, scene_point):
    return board.mapFromScene(scene_point)


# ----------------------------------------------------------------------
# Crop
# ----------------------------------------------------------------------
def test_crop_mode_drags_a_handle_and_enter_keeps_it(qapp, panel):
    board = panel.board
    item = board.items_list()[0]
    item.setPos(0, 0)
    board.set_view(1.0, QPointF(0, 0))
    only(board, item)
    steps = board.undo_stack.count()
    key(board, "artbook.crop")
    assert board.crop_item() is item and item.boundingRect() == item.full_rect()  # all of it shows while cropping

    right_handle = view_point(board, board._crop_handles()[5])  # the right edge's handle
    target = view_point(board, item.mapToScene(QPointF(100, 0)))  # 100 px right of the middle
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, right_handle)
    QTest.mouseMove(board.viewport(), target)
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target)
    key(board, "artbook.crop_apply")

    assert board.crop_item() is None
    assert item.crop == pytest.approx((0, 0, 300, 200), abs=2)  # the right quarter cut away
    assert item.boundingRect().width() == pytest.approx(300, abs=2)
    assert item.image().width() == pytest.approx(300, abs=2)  # Copy Image copies what shows
    assert board.undo_stack.count() == steps + 1
    board.undo_stack.undo()
    assert item.crop is None


def test_esc_cancels_and_clicking_elsewhere_keeps(qapp, panel):
    board = panel.board
    item = board.items_list()[0]
    only(board, item)
    board.start_crop()
    board._drag_crop(0, item.mapToScene(QPointF(-100, -50)))  # the top-left corner, inward
    key(board, "artbook.deselect")  # Esc
    assert item.crop is None and board.crop_item() is None

    board.start_crop()
    board._drag_crop(0, item.mapToScene(QPointF(-100, -50)))
    empty = view_point(board, item.mapToScene(QPointF(5000, 5000)))
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, empty)
    assert item.crop == pytest.approx((100, 50, 400, 200))


def test_crop_needs_one_image_and_reset_crop_undoes_it(qapp, panel):
    board = panel.board
    first, second = board.items_list()
    first.setSelected(True)
    second.setSelected(True)
    assert not board.start_crop()  # two selected
    only(board, first)
    board.start_crop()
    board._drag_crop(2, first.mapToScene(QPointF(50, 50)))
    board.apply_crop()
    assert first.crop is not None
    board.reset_crop()
    assert first.crop is None


def test_cropped_image_turns_around_what_shows(qapp, panel):
    board = panel.board
    item = board.items_list()[0]
    only(board, item)
    board.start_crop()
    board._drag_crop(3, item.mapToScene(QPointF(0, 0)))  # keep only the top-right quarter
    board.apply_crop()
    middle = item.mapToScene(item.crop_rect().center())
    board._begin_transform(("rotate", -1), view_point(board, middle + QPointF(0, -300)))
    board._drag_rotate(board._drag, middle + QPointF(300, 0), snap=False)  # a quarter turn
    board._drag = None
    after = item.mapToScene(item.crop_rect().center())
    assert after.x() == pytest.approx(middle.x(), abs=0.5) and after.y() == pytest.approx(middle.y(), abs=0.5)
    assert item.rotation() == pytest.approx(90, abs=0.5)


def test_crop_opacity_and_grayscale_are_saved(qapp, panel):
    from ui.artbook_board import ImageItem

    board = panel.board
    item = board.items_list()[0]
    only(board, item)
    board.start_crop()
    board._drag_crop(1, item.mapToScene(QPointF(100, -50)))
    board.apply_crop()
    board.toggle_gray()
    board.set_opacity(40, board._snapshot([item]))
    data = item.to_dict()
    again = ImageItem.from_dict(data)
    assert again.crop == item.crop and again.gray and again.opacity() == pytest.approx(0.4)
    damaged = ImageItem.from_dict(dict(data, crop=["x", 1], opacity="?"))
    assert damaged.crop is None and damaged.opacity() == 1.0


# ----------------------------------------------------------------------
# Grayscale and opacity
# ----------------------------------------------------------------------
def gray_pixel(item):
    image = item._shown_pixmap().toImage()
    color = QColor(image.pixel(image.width() // 2, image.height() // 2))
    return color.red() == color.green() == color.blue()


def test_grayscale_per_image_is_an_undo_step(qapp, panel):
    board = panel.board
    first, second = board.items_list()
    only(board, first)
    key(board, "artbook.gray")  # G
    assert first.gray and gray_pixel(first) and not gray_pixel(second)
    key(board, "artbook.gray")  # all selected are gray: back off
    assert not first.gray
    board.undo_stack.undo()
    assert first.gray


def test_ctrl_g_shows_the_board_in_grayscale_without_an_undo_step(qapp, panel):
    board = panel.board
    first, _second = board.items_list()
    steps = board.undo_stack.count()
    key(board, "artbook.gray_board")
    assert gray_pixel(first) and board.view_state()["gray"] is True
    assert board.undo_stack.count() == steps
    thumb = board.thumbnail()
    middle = QColor(thumb.pixel(thumb.width() // 4, thumb.height() // 2))
    assert middle.red() > middle.blue()  # the Pages list still shows it in colour
    panel.save_now()
    board.load_page(panel.page)  # remembered with the page
    assert board._scene.gray_all


def test_opacity_slider_is_one_undo_step(qapp, panel):
    board = panel.board
    item = board.items_list()[0]
    only(board, item)
    menu = board.context_menu(QPointF(0, 0))
    images = next(a.menu() for a in menu.actions() if a.text() == "Images")
    selection = next(a.menu() for a in images.actions() if a.text() == "Selection")
    opacity = next(a.menu() for a in selection.actions() if a.text() == "Opacity")
    slider = opacity.actions()[0].defaultWidget().findChild(QSlider)
    steps = board.undo_stack.count()
    slider.setSliderDown(True)
    for value in (90, 70, 50):
        slider.setValue(value)
    assert item.opacity() == pytest.approx(0.5)  # live while dragging
    slider.setSliderDown(False)
    slider.sliderReleased.emit()
    assert board.undo_stack.count() == steps + 1
    board.undo_stack.undo()
    assert item.opacity() == 1.0
    menu.deleteLater()


def test_same_height_counts_the_crop(qapp, panel):
    board = panel.board
    first, second = board.items_list()
    only(board, first)
    board.start_crop()
    board._drag_crop(6, first.mapToScene(QPointF(0, 0)))  # the bottom edge up to the middle: 100 tall
    board.apply_crop()
    first.setSelected(True)
    second.setSelected(True)
    board.same_size(True)
    heights = sorted(round(item.sceneBoundingRect().height(), 3) for item in (first, second))
    assert heights[0] == heights[1] == 150  # the middle of 100 and 200


# ----------------------------------------------------------------------
# Keys act on the side you're working in
# ----------------------------------------------------------------------
def test_every_artbook_key_is_claimed_by_the_board():
    """While the board has the focus it takes its own keys before any window shortcut, so a key
    shared with another window (G: the 3D viewer's grid) acts on the one you're working in."""
    from ui.artbook_board import BoardView

    artbook_keys = {s.id for s in shortcuts.SHORTCUTS if s.category == shortcuts.ARTBOOK and s.sequence}
    # Typing keys (Ctrl+B / I / U) are claimed only while typing in a note, when the board takes every key
    assert artbook_keys - set(BoardView.KEYS) - set(BoardView.TYPING_KEYS) == set()


def test_shared_key_goes_to_the_focused_board(qapp, panel):
    board = panel.board
    board.setFocus()
    sequence = shortcuts.sequence("artbook.gray")[0]  # G: also the 3D viewer's grid
    override = QKeyEvent(QEvent.Type.ShortcutOverride, sequence.key(), sequence.keyboardModifiers())
    assert board.event(override) and override.isAccepted()  # claimed before window shortcuts see it
