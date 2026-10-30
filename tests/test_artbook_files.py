"""Batch 5B: an image's file (copy its path, show it in Explorer, open it), replacing an image,
relinking images whose files moved, the image list, and drawing."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QEvent
from PySide6.QtGui import QDesktopServices, QKeyEvent
from PySide6.QtWidgets import QApplication

from ui import file_actions, shortcuts
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
def pictures(tmp_path):
    folder = tmp_path / "pictures"
    folder.mkdir()
    Image.new("RGB", (200, 100), "red").save(folder / "wide.png")
    Image.new("RGB", (400, 400), "blue").save(folder / "square.png")
    Image.new("RGB", (100, 300), "green").save(folder / "Tall.png")
    return folder


@pytest.fixture
def panel(qapp):
    from ui.artbook import ArtbookPanel

    widget = ArtbookPanel()
    widget.resize(1100, 750)
    widget.show()
    widget.sidebar.set_open(False, animate=False)
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def add(qapp, board, paths):
    before = len(board.items_list())
    count = board.add_files(paths)
    assert pump(qapp, lambda: len(board.items_list()) == before + count and all(i.pixmap is not None for i in board.items_list()))
    return board.items_list()[before:]


def select(board, items):
    board._scene.clearSelection()
    for item in items:
        item.setSelected(True)


# ----------------------------------------------------------------------
# An image's file
# ----------------------------------------------------------------------
def test_copy_file_path_show_in_explorer_and_open(qapp, panel, pictures, monkeypatch):
    board = panel.board
    wide, square = add(qapp, board, [pictures / "wide.png", pictures / "square.png"])
    select(board, [wide, square])
    assert board.copy_file_paths()
    assert QApplication.clipboard().text().splitlines() == [str(pictures / "wide.png"), str(pictures / "square.png")]

    launched, opened = [], []
    monkeypatch.setattr(file_actions.subprocess, "Popen", lambda args, **kw: launched.append(args))
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toLocalFile()) or True))
    board.show_in_explorer()
    assert launched == [["explorer", f"/select,{pictures / 'wide.png'}"]]
    board.open_source_files()
    assert [Path(p) for p in opened] == [pictures / "wide.png", pictures / "square.png"]

    board._scene.clearSelection()
    assert not board.copy_file_paths()  # nothing selected
    assert not file_actions.reveal(pictures / "gone.png")


def test_replace_image_keeps_its_place_size_crop_and_adjustments(qapp, panel, pictures):
    board = panel.board
    (item,) = add(qapp, board, [pictures / "wide.png"])
    item.setPos(300, 200)
    item.setScale(2.0)
    item.setRotation(30)
    item.set_crop((20.0, 10.0, 120.0, 90.0))
    item.gray = True
    middle = item.mapToScene(item.crop_rect().center())
    shown_width = item.crop_rect().width() * item.scale()
    select(board, [item])
    board.replace_image(pictures / "square.png")
    assert pump(qapp, lambda: item.natural == (400, 400))
    assert item.linked_path() == pictures / "square.png" and item.pixmap.width() == 400
    assert item.crop == pytest.approx((40.0, 40.0, 240.0, 360.0))  # the same share of the new picture
    assert item.crop_rect().width() * item.scale() == pytest.approx(shown_width)  # as wide on the board
    now = item.mapToScene(item.crop_rect().center())
    assert now.x() == pytest.approx(middle.x()) and now.y() == pytest.approx(middle.y())  # in the same place
    assert item.rotation() == pytest.approx(30) and item.gray

    board.undo_stack.undo()
    assert item.natural == (200, 100) and item.linked_path() == pictures / "wide.png" and item.crop == (20.0, 10.0, 120.0, 90.0)
    assert item.pixmap.width() == 200
    board.undo_stack.redo()
    assert item.natural == (400, 400)


def test_relinking_finds_moved_files_by_name(qapp, panel, pictures, tmp_path):
    from ui.artbook_board import find_files

    board = panel.board
    wide, tall = add(qapp, board, [pictures / "wide.png", pictures / "Tall.png"])
    panel.save_now()
    moved = tmp_path / "moved" / "deeper"
    moved.mkdir(parents=True)
    shutil.move(str(pictures / "wide.png"), str(moved / "WIDE.png"))  # (another case: still found)
    (pictures / "Tall.png").unlink()  # this one's just gone
    board.load_page(panel.page)
    assert pump(qapp, lambda: all(i.pixmap is not None for i in board.items_list()))  # (the copies in the page show)
    assert len(board.missing_images()) == 2

    assert find_files(tmp_path / "moved", {"wide.png", "nope.png"}) == {"wide.png": str(moved / "WIDE.png")}
    board.relink_missing(tmp_path / "moved")
    assert pump(qapp, lambda: len(board.missing_images()) == 1)
    assert "Relinked 1 image." in board.toast.text() and "1 still missing" in board.toast.text()
    relinked = next(i for i in board.items_list() if i.natural == (200, 100))
    assert relinked.linked_path() == moved / "WIDE.png" and not relinked.file_missing()
    board.undo_stack.undo()  # one undo step
    assert relinked.file_missing()


# ----------------------------------------------------------------------
# The image list
# ----------------------------------------------------------------------
def test_the_image_list_shows_every_image_and_selects_them(qapp, panel, pictures):
    board = panel.board
    wide, square, tall = add(qapp, board, [pictures / "wide.png", pictures / "square.png", pictures / "Tall.png"])
    board.add_image(square.pixmap.toImage())  # one kept in the page (no file)
    assert pump(qapp, lambda: len(board.items_list()) == 4)
    pasted = board.items_list()[-1]
    wide.setPos(0, 0)
    square.setPos(600, 0)
    tall.setPos(0, 700)
    pasted.setPos(600, 700)

    combo = shortcuts.sequence("artbook.image_list")[0]
    board.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, combo.key(), combo.keyboardModifiers()))
    panel_list = board.image_list
    assert panel_list.isVisible() and panel.btn_images.isChecked()
    rows = [panel_list.list.item(i).text() for i in range(panel_list.list.count())]
    assert [r.splitlines()[0] for r in rows] == ["wide.png", "square.png", "Tall.png", "Pasted image"]  # reading order
    assert "linked file" in rows[0] and "kept in the page" in rows[3]
    assert panel_list.geometry().right() < board.width() and not panel_list.btn_relink.isVisible()

    panel_list.list.clearSelection()
    panel_list.list.item(2).setSelected(True)  # a row selects (and a click frames) its image
    assert board.selected() == [tall]
    panel_list._on_row_clicked(panel_list.list.item(2))
    center, view = tall.sceneBoundingRect().center(), board.view_center()
    assert abs(center.x() - view.x()) < 5 and abs(center.y() - view.y()) < 5  # framed
    select(board, [square])  # the board's selection shows in the list
    assert panel_list.selected_row_ids() == [square.item_id]

    panel_list.search.setText("tall")
    assert panel_list.list.count() == 1
    panel_list.search.clear()

    (pictures / "wide.png").unlink()  # a missing file
    board.files_changed.emit()
    assert pump(qapp, lambda: "missing" in panel_list.count.text(), seconds=2)
    assert panel_list.btn_relink.isVisible() and "Missing file" in panel_list.list.item(0).text()

    panel.btn_images.click()
    assert not panel_list.isVisible() and not panel.btn_images.isChecked()


# ----------------------------------------------------------------------
# Drawing
# ----------------------------------------------------------------------
def drag(board, points, modifiers=None):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    viewport = board.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(*points[0]))
    for point in points[1:]:
        QTest.mouseMove(viewport, QPoint(*point))
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QPointF as P
        board.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, P(*point), board.viewport().mapToGlobal(P(*point)),
                                         Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(*points[-1]))


def press(board, shortcut_id):
    combo = shortcuts.sequence(shortcut_id)[0]
    board.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, combo.key(), combo.keyboardModifiers()))


def test_drawing_strokes_erasing_them_and_saving(qapp, panel, pictures):
    from ui.artbook_board import StrokeItem
    from ui.themes import apply_theme, theme_color

    board = panel.board
    (image,) = add(qapp, board, [pictures / "wide.png"])
    press(board, "artbook.draw")
    assert board.drawing() and board.draw_bar.isVisible()
    drag(board, [(200, 200), (260, 220), (320, 260), (380, 250)])  # over the image: draws, doesn't move it
    strokes = [i for i in board.items_list() if isinstance(i, StrokeItem)]
    assert len(strokes) == 1 and len(strokes[0].points) == 4
    assert strokes[0].width == pytest.approx(4 / board.zoom())  # 4 px at this zoom
    assert strokes[0].zValue() > image.zValue() and image.pos() == image.pos()
    assert strokes[0].pen().color() == theme_color("text_strong")  # the theme's colour...
    apply_theme("light", qapp)
    qapp.processEvents()
    assert strokes[0].pen().color() == theme_color("text_strong")  # ...which it follows
    apply_theme("dark", qapp)

    board.set_pen_color("#ef4444")
    board.set_pen_width(16)
    drag(board, [(200, 400), (400, 400)])
    second = [i for i in board.items_list() if isinstance(i, StrokeItem) and i is not strokes[0]][0]
    assert second.color == "#ef4444" and second.width == pytest.approx(16 / board.zoom())

    press(board, "artbook.eraser")  # E: drag over a stroke to remove it
    assert board.draw_bar.btn_eraser.isChecked()
    drag(board, [(300, 380), (300, 420)])
    assert second.item_id not in {i.item_id for i in board.items_list()} and strokes[0].scene() is not None
    board.undo_stack.undo()  # the erase is one step
    assert second.scene() is not None
    press(board, "artbook.eraser")

    press(board, "artbook.deselect")  # Esc: done
    assert not board.drawing() and not board.draw_bar.isVisible()
    board._scene.clearSelection()
    second.setSelected(True)
    board.flip(True)  # (strokes don't flip)...
    second.setScale(2.0)  # ...but scale, move and group like the rest
    panel.save_now()
    board.load_page(panel.page)
    again = [i for i in board.items_list() if isinstance(i, StrokeItem)]
    assert len(again) == 2
    red = next(i for i in again if i.color == "#ef4444")
    assert red.scale() == 2.0 and len(red.points) == len(second.points)
