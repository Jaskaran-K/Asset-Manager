"""Phase 4, notes and colour picker: typing notes on the board (undo, saving, keys go to the note
while typing) and picking a colour's hex code from the screen."""

from __future__ import annotations

import time

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

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

    path = tmp_path / "red.png"
    Image.new("RGB", (400, 300), (255, 0, 0)).save(path)
    widget = ArtbookPanel()
    widget.resize(1000, 700)
    widget.show()
    widget.sidebar.set_open(False, animate=False)
    qapp.processEvents()
    widget.board.add_files([path])
    assert pump(qapp, lambda: widget.board.items_list() and widget.board.items_list()[0].pixmap is not None)
    widget.board._scene.clearSelection()
    yield widget
    widget.close()


def key(board, shortcut_id):
    combo = shortcuts.sequence(shortcut_id)[0]
    board.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, combo.key(), combo.keyboardModifiers()))


def notes(board):
    from ui.artbook_board import NoteItem

    return [item for item in board.items_list() if isinstance(item, NoteItem)]


# ----------------------------------------------------------------------
# Notes
# ----------------------------------------------------------------------
def test_t_adds_a_note_to_type_in_and_esc_keeps_it(qapp, panel):
    board = panel.board
    board.setFocus()
    steps = board.undo_stack.count()
    image = board.items_list()[0]
    image_pos = image.pos()
    key(board, "artbook.note")
    (note,) = notes(board)
    assert board.editing_note() is note
    QTest.keyClicks(board, "acg here")  # A, C and G are tools, but this is typing
    assert note.toPlainText() == "acg here"
    assert image.pos() == image_pos and not image.gray and board.crop_item() is None
    key(board, "artbook.deselect")  # Esc: done
    assert board.editing_note() is None
    assert board.undo_stack.count() == steps + 1  # adding it, with its text: one step
    board.undo_stack.undo()
    assert notes(board) == []
    board.undo_stack.redo()
    assert notes(board)[0].toPlainText() == "acg here"


def test_a_note_never_typed_in_leaves_nothing(qapp, panel):
    board = panel.board
    steps = board.undo_stack.count()
    board.add_note(QPointF(0, 0))
    board.finish_note_edit()
    assert notes(board) == [] and board.undo_stack.count() == steps


def test_typing_takes_every_key_from_the_window(qapp, panel):
    board = panel.board
    board.add_note(QPointF(0, 0))
    override = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert board.event(override) and override.isAccepted()  # Ctrl+Z undoes typing, not the library
    board.finish_note_edit()


def test_editing_a_note_is_one_undo_step_and_emptying_deletes_it(qapp, panel):
    board = panel.board
    note = board.add_note(QPointF(0, 0))
    note.setPlainText("first")
    board.finish_note_edit()
    middle = board.mapFromScene(note.mapToScene(note.boundingRect().center()))
    QTest.mouseDClick(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, middle)
    assert board.editing_note() is note
    note.setPlainText("second")
    board.finish_note_edit()
    board.undo_stack.undo()
    assert note.toPlainText() == "first"
    board.undo_stack.redo()

    board.edit_note(note)
    note.setPlainText("   ")
    board.finish_note_edit()
    assert notes(board) == []  # emptied: gone
    board.undo_stack.undo()
    assert notes(board)[0].toPlainText() == "second"


def test_clicking_elsewhere_finishes_typing(qapp, panel):
    board = panel.board
    note = board.add_note(QPointF(0, 0))
    note.setPlainText("hello")
    empty = board.mapFromScene(QPointF(9000, 9000))
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, empty)
    assert board.editing_note() is None and notes(board) == [note]


def test_notes_are_saved_and_arrange_with_images_but_dont_flip(qapp, panel):
    board = panel.board
    note = board.add_note(QPointF(800, 800))
    note.setPlainText("remember the rim light")
    board.finish_note_edit()
    data = next(d for d in board.page_items() if d["kind"] == "note")
    assert data["text"] == "remember the rim light"

    image = board.items_list()[0]
    image.setSelected(True)
    note.setSelected(True)
    board.flip(True)
    assert image.state().flip_h and not note.state().flip_h
    board.align("top")
    assert note.sceneBoundingRect().top() == pytest.approx(image.sceneBoundingRect().top())
    board.copy_selection()
    assert QApplication.clipboard().text() == "remember the rim light"  # the note is on top

    panel.save_now()
    board.load_page(panel.page)
    (again,) = notes(board)
    assert again.toPlainText() == "remember the rim light" and again.pos() == note.pos()


def test_damaged_note_data_is_tolerated(qapp):
    from ui.artbook_board import NoteItem

    note = NoteItem.from_dict({"kind": "note", "text": 42, "width": "wide", "opacity": None})
    assert note.toPlainText() == "" and note.textWidth() > 0 and note.opacity() == 1.0


# ----------------------------------------------------------------------
# Colour picker
# ----------------------------------------------------------------------
def test_picker_copies_the_colour_under_the_cursor_until_esc(qapp, panel):
    board = panel.board
    image = board.items_list()[0]
    board.frame([image])
    on_red = board.mapFromScene(image.sceneBoundingRect().center())
    key(board, "artbook.picker")
    assert board.picking()
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, on_red)
    assert QApplication.clipboard().text() == "#FF0000"
    assert board.picking()  # stays on for more picks
    assert not image.isSelected()  # picking doesn't select
    key(board, "artbook.deselect")  # Esc
    assert not board.picking()


def test_right_click_stops_the_picker_without_the_menu(qapp, panel):
    from PySide6.QtGui import QContextMenuEvent

    board = panel.board
    board.start_picker()
    QTest.mousePress(board.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, board.viewport().rect().center())
    assert not board.picking()
    board.start_picker()
    center = board.viewport().rect().center()
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, center, board.viewport().mapToGlobal(center))
    board.contextMenuEvent(event)  # no menu while picking (it would wait for a click here)
    assert event.isAccepted()
    board.stop_picker()
