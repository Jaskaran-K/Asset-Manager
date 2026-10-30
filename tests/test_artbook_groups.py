"""Batch 5A: groups, Stack, captions, pixelated images, and the Pages submenu of the board's menu."""

from __future__ import annotations

import time

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog

from core import artbook_store as store
from ui import artbook_layout, shortcuts
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
    for name, size, color in (("a", (200, 100), "red"), ("b", (100, 100), "green"), ("c", (150, 80), "blue"), ("d", (80, 160), "white")):
        Image.new("RGB", size, color).save(folder / f"{name}.png")
    pixels = Image.new("RGB", (2, 1))
    pixels.putpixel((0, 0), (255, 0, 0))
    pixels.putpixel((1, 0), (0, 0, 255))
    pixels.save(folder / "pixels.png")
    return folder


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


def add(qapp, board, paths):
    before = len(board.items_list())
    count = board.add_files(paths)
    assert pump(qapp, lambda: len(board.items_list()) == before + count and all(i.pixmap is not None for i in board.items_list() if hasattr(i, "pixmap")))
    return board.items_list()[before:]


def press(board, shortcut_id):
    combo = shortcuts.sequence(shortcut_id)[0]
    board.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, combo.key(), combo.keyboardModifiers()))


def spread(items, gap=400):
    for n, item in enumerate(items):
        item.setPos(n * gap, (n % 2) * 150)


def select(board, items):
    board._scene.clearSelection()
    for item in items:
        item.setSelected(True)


# ----------------------------------------------------------------------
# Groups
# ----------------------------------------------------------------------
def test_ctrl_g_groups_and_a_group_is_picked_up_as_one(qapp, panel, pictures):
    board = panel.board
    a, b, c = add(qapp, board, [pictures / "a.png", pictures / "b.png", pictures / "c.png"])
    spread([a, b, c])
    note = board.add_note(QPointF(0, 400))
    note.setPlainText("Pose ideas")
    board.finish_note_edit()
    select(board, [a, b, note])
    press(board, "artbook.group")
    assert a.group and a.group == b.group == note.group and c.group is None
    assert len(board.group_backdrops()) == 1

    select(board, [b])  # one member: the whole group
    assert set(board.selected()) == {a, b, note}

    board.viewport().repaint()
    at = board.mapFromScene(b.sceneBoundingRect().center())
    board._scene.clearSelection()
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier, at)
    assert board.selected() == [b]  # Alt+click: just this one
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier, at)

    select(board, [c])
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, at)
    assert set(board.selected()) == {a, b, note, c}  # Shift+click adds the whole group...
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, at)
    assert board.selected() == [c]  # ...and takes it away again

    board.undo_stack.undo()  # the group came apart
    assert a.group is None and b.group is None
    board.undo_stack.redo()
    group = a.group
    panel.save_now()
    board.load_page(panel.page)
    again = {i.item_id: i for i in board.items_list()}
    assert again[a.item_id].group == group == again[note.item_id].group  # saved with the page

    select(board, [again[a.item_id]])
    press(board, "artbook.ungroup")
    assert all(i.group is None for i in board.items_list()) and board.group_backdrops() == []


def test_arranging_moves_a_group_as_one_block(qapp, panel, pictures):
    board = panel.board
    a, b, c, d = add(qapp, board, [pictures / f"{n}.png" for n in "abcd"])
    spread([a, b, c, d], gap=700)
    select(board, [a, b])
    board.group_selection()
    offset = b.pos() - a.pos()
    board._scene.clearSelection()
    board.auto_arrange()
    assert b.pos() - a.pos() == offset  # the group kept its own layout

    select(board, [a, c, d])  # (a brings b with it)
    board.align("top")
    tops = {round(box.top(), 3) for _key, box in board._unit_boxes(board._units(board.selected()))}
    assert len(tops) == 1 and b.pos() - a.pos() == offset

    select(board, [a])  # only one group selected: aligning lines up its members
    board.align("left")
    assert a.sceneBoundingRect().left() == pytest.approx(b.sceneBoundingRect().left())


def test_stack_piles_the_selection_up(qapp, panel, pictures):
    board = panel.board
    items = add(qapp, board, [pictures / f"{n}.png" for n in "abc"])
    spread(items)
    for z, item in enumerate(items):
        item.setZValue(z)
    first = items[0].sceneBoundingRect().center()
    select(board, items)
    board.stack()
    centers = [item.sceneBoundingRect().center() for item in items]
    assert centers[0] == first  # the back-most one stays
    steps = [centers[n + 1] - centers[n] for n in range(2)]
    assert steps[0].x() > 0 and steps[0] == steps[1]  # each a little down and right of the last
    board.undo_stack.undo()
    assert items[1].sceneBoundingRect().center() != centers[1]  # one undo step

    moves = artbook_layout.stack([("x", QRectF(0, 0, 10, 10)), ("y", QRectF(100, 100, 10, 10))])
    assert moves["x"] == QPointF(0, 0) and moves["y"] == QPointF(-100 + 0.5, -100 + 0.5)


def test_group_colour_from_the_menu_and_the_custom_picker(qapp, panel, pictures):
    from PySide6.QtGui import QColor

    board = panel.board
    a, b, c = add(qapp, board, [pictures / f"{n}.png" for n in "abc"])
    spread([a, b, c])
    select(board, [a, b])
    board.group_selection()
    group = a.group
    board._scene.clearSelection()

    def colour_menu():
        menu = board.context_menu(QPointF(0, 0))
        selection = next(x.menu() for x in next(x.menu() for x in menu.actions() if x.text() == "Images").actions() if x.text() == "Selection")
        return menu, {x.text(): x for x in next(x.menu() for x in selection.actions() if x.text() == "Group Colour").actions()}

    menu, entries = colour_menu()
    assert not entries["White"].isEnabled()  # nothing grouped is selected
    menu.deleteLater()

    select(board, [a])  # (brings b)
    menu, entries = colour_menu()
    steps = board.undo_stack.count()
    entries["White"].trigger()
    assert board.group_fill(group) == "#ffffff" and board.group_backdrops()[0][1] == "#ffffff"
    entries["Theme Default"].trigger()
    assert board.group_fill(group) is None
    assert board.undo_stack.count() == steps + 2
    menu.deleteLater()

    entries_custom = colour_menu()
    entries_custom[1]["Custom..."].trigger()  # (the group bar's picker)
    picker = board.group_bar.btn_color.popup
    assert picker is not None and picker.isVisible() and picker.title.text() == "Select group colour"
    backdrop = board.mapFromScene(board.group_backdrops()[0][0]).boundingRect().intersected(board.viewport().rect())
    on_screen = backdrop.translated(board.viewport().mapToGlobal(QPoint(0, 0)))
    assert not picker.frameGeometry().intersects(on_screen)  # beside the group, not over it
    picker._on_hue(0.3)
    picker._on_square(0.7, 0.6)
    picker.opacity_box.setValue(50)  # live, and see-through
    fill = QColor(board.group_fill(group))
    assert fill.alpha() == round(0.5 * 255) and fill.rgba() == picker.color().rgba()
    picker.close()
    assert pump(qapp, lambda: board.group_bar.btn_color.popup is None, seconds=2)
    assert board.undo_stack.count() == steps + 3  # the whole pick: one step
    entries_custom[0].deleteLater()

    panel.save_now()
    assert panel.page["groups"] == {group: {"fill": board.group_fill(group)}}  # saved with the page
    board.undo_stack.undo()
    assert board.group_fill(group) is None


def test_the_group_bar_shows_above_a_selected_group(qapp, panel, pictures):
    board = panel.board
    a, b, c = add(qapp, board, [pictures / f"{n}.png" for n in "abc"])
    spread([a, b, c])
    select(board, [a, b])
    board.group_selection()
    board.frame()
    board.viewport().repaint()
    assert pump(qapp, lambda: board.group_bar.isVisible(), seconds=2)
    bar = board.group_bar.geometry()
    box = board.group_bar_box()
    assert bar.bottom() < box.top() or bar.top() > box.bottom()  # above it (below if there's no room)
    assert abs(bar.center().x() - box.center().x()) <= 2 or bar.left() == 4 or bar.right() >= board.width() - 5

    select(board, [a, b, c])  # a group and something else: no bar
    board.viewport().repaint()
    assert pump(qapp, lambda: not board.group_bar.isVisible(), seconds=2)
    board._scene.clearSelection()
    board.viewport().repaint()
    assert pump(qapp, lambda: not board.group_bar.isVisible(), seconds=2)


def test_a_locked_group_stays_put(qapp, panel, pictures):
    from PySide6.QtGui import QColor  # noqa: F401

    board = panel.board
    a, b, c = add(qapp, board, [pictures / f"{n}.png" for n in "abc"])
    spread([a, b, c])
    select(board, [a, b])
    board.group_selection()
    board.frame()
    board.viewport().repaint()
    assert pump(qapp, lambda: board.group_bar.isVisible(), seconds=2)
    board.group_bar.btn_lock.click()  # the lock in the bar
    assert board.group_locked(a.group) and board.group_bar.btn_lock.isChecked()
    where = {i.item_id: (i.pos(), i.scale(), i.rotation()) for i in (a, b)}

    board.flip(True)
    board.reset_transform()
    board.same_size(True)
    board.auto_arrange()
    assert not board.start_crop()
    board.delete_selection()
    assert "locked" in board.toast.text() and len(board.items_list()) == 3
    assert board._handle_at(board.mapFromScene(board._frame_corners()[0])) is None  # no handles to drag
    at = board.mapFromScene(a.sceneBoundingRect().center())
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at)
    QTest.mouseMove(board.viewport(), at + QPoint(80, 40))
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at + QPoint(80, 40))
    assert {i.item_id: (i.pos(), i.scale(), i.rotation()) for i in (a, b)} == where  # nothing moved

    panel.save_now()
    assert panel.page["groups"][a.group]["locked"] is True  # saved with the page
    board.undo_stack.undo()  # the lock is one undo step
    assert not board.group_locked(a.group)
    select(board, [a])
    board.flip(True)
    assert a.state().flip_h


def test_clicking_a_groups_empty_space_selects_and_drags_it(qapp, panel, pictures):
    board = panel.board
    a, b, c = add(qapp, board, [pictures / f"{n}.png" for n in "abc"])
    a.setPos(0, 0)
    b.setPos(400, 300)  # a gap between them inside the group's panel
    c.setPos(2000, 0)
    select(board, [a, b])
    board.group_selection()
    board._scene.clearSelection()
    board.frame()
    gap = board.mapFromScene(QPointF(320, -40))  # inside the panel, on no image
    assert board._image_at(QPointF(gap)) is None and board._group_at(QPointF(320, -40)) == a.group
    start = (a.pos(), b.pos())
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, gap)
    assert set(board.selected()) == {a, b}  # the whole group
    QTest.mouseMove(board.viewport(), gap + QPoint(50, 0))
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, gap + QPoint(50, 0))
    moved = a.pos() - start[0]
    assert moved.x() > 0 and b.pos() - start[1] == moved  # dragged together
    board.undo_stack.undo()
    assert (a.pos(), b.pos()) == start  # one undo step

    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, board.mapFromScene(QPointF(1200, 1500)))
    assert board.selected() == []  # outside it: nothing


# ----------------------------------------------------------------------
# Captions and pixels
# ----------------------------------------------------------------------
def test_a_caption_sits_under_its_image_and_follows_it(qapp, panel, pictures, monkeypatch):
    board = panel.board
    (image,) = add(qapp, board, [pictures / "a.png"])
    shown = image.sceneBoundingRect()
    select(board, [image])
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("  Rim   light, warm ", True)))
    board.edit_caption()
    assert image.caption == "Rim light, warm"
    assert image.sceneBoundingRect().bottom() > shown.bottom()  # under it...
    assert image.sceneBoundingRect().top() == pytest.approx(shown.top())
    corners = board._frame_corners()
    assert corners[2].y() == pytest.approx(shown.bottom())  # ...outside the selection frame
    board.flip(False)  # upside down: the caption still ends up underneath
    assert image.sceneBoundingRect().bottom() > shown.bottom() + 1 and image.sceneBoundingRect().top() == pytest.approx(shown.top())
    board.undo_stack.undo()
    board.undo_stack.undo()
    assert image.caption == "" and image.sceneBoundingRect() == shown

    board.undo_stack.redo()
    panel.save_now()
    board.load_page(panel.page)
    assert board.items_list()[0].caption == "Rim light, warm"


def test_pixelated_images_keep_hard_edges(qapp, panel, pictures):
    board = panel.board
    (image,) = add(qapp, board, [pictures / "pixels.png"])
    image.setScale(100.0)  # two pixels, 200 wide on the board
    select(board, [image])

    def middle_color():
        picture = board.export_image()
        return picture.pixelColor(picture.width() // 2 - 3, picture.height() // 2)

    assert 0 < middle_color().blue() < 255  # smoothed: red blends into blue
    board.toggle_pixelated()
    assert image.pixelated
    assert middle_color().blue() == 0 and middle_color().red() == 255  # hard edges


# ----------------------------------------------------------------------
# Keys, and the Pages submenu
# ----------------------------------------------------------------------
def test_board_grayscale_is_shift_g_now(qapp, panel, pictures):
    board = panel.board
    (image,) = add(qapp, board, [pictures / "a.png"])
    select(board, [image])
    press(board, "artbook.gray_board")
    assert board._scene.gray_all and not image.gray
    press(board, "artbook.gray")
    assert image.gray
    assert shortcuts.get("artbook.gray_board").keys == "Shift+G" and shortcuts.get("artbook.group").keys == "Ctrl+G"


def test_pages_submenu_makes_duplicates_and_renames_pages(qapp, panel, pictures):
    board = panel.board
    add(qapp, board, [pictures / "a.png"])
    menu = board.context_menu(QPointF(0, 0))
    pages = next(a.menu() for a in menu.actions() if a.text() == "Pages")
    actions = {a.text(): a for a in pages.actions()}
    first = panel.page["id"]

    actions["Duplicate Page"].trigger()
    names = sorted(p["name"] for p in store.list_pages())
    assert names == ["Page 1", "Page 1 copy"] and panel.page["id"] == first

    actions["Rename Page..."].trigger()
    assert pump(qapp, lambda: panel.sidebar.is_open())
    assert panel.pages.tree.state() == panel.pages.tree.State.EditingState  # typing the new name

    actions["New Page"].trigger()
    assert len(store.list_pages()) == 3 and panel.page["id"] != first
    menu.deleteLater()
