"""Notes in the theme's colours and their format bar; the 8 frame handles (side handles; a note's
width); resetting rotation, size, flips and crop one at a time."""

from __future__ import annotations

import time

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QFont, QImage, QKeyEvent, QMouseEvent, QPainter, QTextCursor
from PySide6.QtWidgets import QStyleOptionGraphicsItem
from PySide6.QtTest import QTest

from ui import shortcuts
from ui.animations import set_motion_mode
from ui.themes import apply_theme, theme_color


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

    apply_theme("dark", qapp)
    path = tmp_path / "img.png"
    Image.new("RGB", (400, 200), "red").save(path)
    widget = ArtbookPanel()
    widget.resize(1100, 750)
    widget.show()
    widget.sidebar.set_open(False, animate=False)
    qapp.processEvents()
    widget.board.add_files([path])
    assert pump(qapp, lambda: widget.board.items_list() and widget.board.items_list()[0].pixmap is not None)
    widget.board._scene.clearSelection()
    yield widget
    widget.close()
    widget.deleteLater()  # (its note bar lists every font: left alive, each later theme change restyles it)
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    apply_theme("dark", qapp)


def make_note(board, text="Rim light", at=QPointF(0, 400)):
    note = board.add_note(at)
    note.setPlainText(text)
    board.finish_note_edit()
    return note


def select_only(board, item):
    board._scene.clearSelection()
    item.setSelected(True)


def char_format_at(note, position):
    cursor = QTextCursor(note.document())
    cursor.setPosition(position)
    cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
    return cursor.charFormat()


# ----------------------------------------------------------------------
# Notes follow the theme
# ----------------------------------------------------------------------
def test_new_notes_take_the_themes_colours(qapp, panel):
    board = panel.board
    note = make_note(board)
    assert note.fill is None and note.defaultTextColor() == theme_color("text_strong")
    apply_theme("light", qapp)
    qapp.processEvents()
    assert note.defaultTextColor() == theme_color("text_strong")  # followed the theme change
    select_only(board, note)
    assert board.note_formats()["fill"] == theme_color("surface")  # the light theme's paper now


# ----------------------------------------------------------------------
# The format bar
# ----------------------------------------------------------------------
def test_bar_shows_above_the_one_selected_note(qapp, panel):
    board = panel.board
    note = make_note(board)
    image = board.items_list()[0]
    select_only(board, note)
    board.viewport().repaint()
    assert pump(qapp, lambda: board.note_bar.isVisible(), seconds=2)
    note_box = board.mapFromScene(note.sceneBoundingRect()).boundingRect()
    assert board.note_bar.geometry().bottom() < note_box.top() or board.note_bar.geometry().top() > note_box.bottom()
    image.setSelected(True)  # two selected: no bar
    board.viewport().repaint()
    assert pump(qapp, lambda: not board.note_bar.isVisible(), seconds=2)


def test_formatting_the_whole_note_is_one_undo_step_each_and_saved(qapp, panel):
    board = panel.board
    note = make_note(board, "Warm key light")
    select_only(board, note)
    steps = board.undo_stack.count()
    board.toggle_note_bold()
    board.set_note_text_color("#ef4444")
    board.set_note_highlight("#facc15")
    board.set_note_font_size(24)
    board.set_note_font_family("Arial")
    board.set_note_alignment(Qt.AlignmentFlag.AlignHCenter)
    board.set_note_fill("#3b82f6")
    assert board.undo_stack.count() == steps + 7
    fmt = char_format_at(note, 3)
    assert fmt.fontWeight() >= QFont.Weight.Bold.value and fmt.foreground().color() == QColor("#ef4444")
    assert fmt.background().color() == QColor("#facc15") and fmt.fontPointSize() == 24
    formats = board.note_formats()
    assert formats["bold"] and formats["family"] == "Arial" and formats["align"] == Qt.AlignmentFlag.AlignHCenter
    assert formats["fill"] == QColor("#3b82f6")

    panel.save_now()
    board.load_page(panel.page)
    from ui.artbook_board import NoteItem

    (again,) = [i for i in board.items_list() if isinstance(i, NoteItem)]
    assert again.fill == "#3b82f6" and char_format_at(again, 3).fontPointSize() == 24
    assert again.toPlainText() == "Warm key light"

    board.undo_stack.undo()  # (after reloading the undo history starts again: nothing to undo)
    select_only(board, again)
    board.toggle_note_bold()
    board.undo_stack.undo()
    assert char_format_at(again, 3).fontWeight() >= QFont.Weight.Bold.value  # bold again


def test_theme_default_colour_and_clear_formatting(qapp, panel):
    board = panel.board
    note = make_note(board, "abc")
    select_only(board, note)
    board.set_note_text_color("#22c55e")
    board.set_note_text_color(None)  # back to the theme's
    assert board.note_formats()["color"] == theme_color("text_strong")
    board.toggle_note_italic()
    board.set_note_fill("#ec4899")
    board.clear_note_format()
    assert note.fill is None and not board.note_formats()["italic"] and note.toPlainText() == "abc"


def test_while_typing_formatting_goes_to_the_selected_text_and_ctrl_b_bolds(qapp, panel):
    board = panel.board
    note = make_note(board, "one two")
    board.edit_note(note)
    cursor = note.textCursor()
    cursor.setPosition(4)
    cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)  # "two"
    note.setTextCursor(cursor)
    combo = shortcuts.sequence("artbook.note_bold")[0]
    board.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, combo.key(), combo.keyboardModifiers()))
    assert char_format_at(note, 5).fontWeight() >= QFont.Weight.Bold.value
    assert char_format_at(note, 1).fontWeight() < QFont.Weight.Bold.value  # "one" untouched
    steps = board.undo_stack.count()
    board.finish_note_edit()
    assert board.undo_stack.count() == steps + 1  # the whole typing session: one step


def test_ctrl_b_is_the_pages_sidebar_when_not_typing(qapp, panel):
    board = panel.board
    combo = shortcuts.sequence("pages.sidebar")[0]
    override = QKeyEvent(QEvent.Type.ShortcutOverride, combo.key(), combo.keyboardModifiers())
    assert not board.event(override) or not override.isAccepted()  # left for the Pages sidebar


def test_pictures_in_saved_note_html_are_dropped(qapp):
    from ui.artbook_board import NoteItem

    note = NoteItem.from_dict({"kind": "note", "text": "hi", "html": '<p>hi<img src="C:/secret.png"></p>'})
    assert "img" not in note.toHtml().lower() and note.toPlainText() == "hi"


# ----------------------------------------------------------------------
# The 8 handles
# ----------------------------------------------------------------------
def drag(board, start_scene, end_scene):
    start = board.mapFromScene(start_scene)
    end = board.mapFromScene(end_scene)
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(board.viewport(), end)
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)


def test_eight_handles_and_a_side_scales_from_the_opposite_side(qapp, panel):
    board = panel.board
    image = board.items_list()[0]
    image.setPos(0, 0)
    board.set_view(1.0, QPointF(0, 0))
    select_only(board, image)
    handles = board._frame_handles(board._frame_corners())
    assert len(handles) == 8
    left_before = image.sceneBoundingRect().left()
    drag(board, handles[5], handles[5] + QPointF(200, 0))  # the right side, 200 px further right
    box = image.sceneBoundingRect()
    assert box.left() == pytest.approx(left_before, abs=1)  # the left side stayed
    assert box.width() == pytest.approx(600, abs=2) and box.height() == pytest.approx(300, abs=2)  # shape kept


def test_a_notes_sides_set_its_width(qapp, panel):
    board = panel.board
    note = make_note(board, "a long line of text that wraps", at=QPointF(0, 0))
    note.setScale(1.0)
    board.set_view(1.0, note.sceneBoundingRect().center())
    select_only(board, note)
    width, right = note.textWidth(), note.sceneBoundingRect().right()
    handles = board._frame_handles(board._frame_corners())
    drag(board, handles[7], handles[7] + QPointF(-100, 0))  # the left side, 100 px out
    assert note.textWidth() == pytest.approx(width + 100, abs=2)
    assert note.sceneBoundingRect().right() == pytest.approx(right, abs=2)  # the right side stayed
    assert note.scale() == 1.0
    board.undo_stack.undo()
    assert note.textWidth() == pytest.approx(width)


# ----------------------------------------------------------------------
# Resetting one thing at a time
# ----------------------------------------------------------------------
def test_resets_one_at_a_time_keep_what_shows_in_place(qapp, panel):
    board = panel.board
    image = board.items_list()[0]
    select_only(board, image)
    board.start_crop()
    board._drag_crop(3, image.mapToScene(QPointF(0, 0)))  # keep the top-right quarter
    board.apply_crop()
    image.setRotation(30)
    image.setScale(2.0)
    board.flip(True)
    middle = image.mapToScene(image.crop_rect().center())

    board.reset_rotation()
    state = image.state()
    assert state.rotation == 0 and state.scale == 2.0 and state.flip_h and image.crop
    board.reset_flips()
    assert not image.state().flip_h and image.state().scale == 2.0
    board.reset_size()
    assert image.state().scale == 1.0
    after = image.mapToScene(image.crop_rect().center())
    assert after.x() == pytest.approx(middle.x(), abs=0.5) and after.y() == pytest.approx(middle.y(), abs=0.5)
    board.reset_crop()
    assert image.crop is None


# ----------------------------------------------------------------------
# Colour menus and the custom colour popup
# ----------------------------------------------------------------------
def open_custom(qapp, button):
    custom = [a for a in button.menu().actions() if a.text() == "Custom..."][0]
    custom.trigger()
    assert pump(qapp, lambda: button.popup is not None and button.popup.isVisible(), seconds=2)
    return button.popup


def test_colour_menus_offer_theme_default_white_black_and_custom(qapp, panel):
    bar = panel.board.note_bar
    for button in (bar.btn_color, bar.btn_highlight, bar.btn_fill):
        names = [a.text() for a in button.menu().actions() if not a.isSeparator()]
        assert names == ["Theme Default", "White", "Black", "Custom..."]


def test_custom_colour_changes_live_and_is_one_undo_step(qapp, panel):
    board = panel.board
    note = make_note(board, "Live")
    select_only(board, note)
    steps = board.undo_stack.count()
    popup = open_custom(qapp, board.note_bar.btn_fill)
    assert popup.title.text() == "Select note colour"
    popup._on_hue(0.6)
    first = note.fill
    popup._on_square(0.8, 0.7)  # (each change shows on the note straight away)
    assert note.fill != first and QColor(note.fill).rgba() == popup.color().rgba()
    popup.opacity_box.setValue(40)
    assert QColor(note.fill).alpha() == round(0.4 * 255) and len(note.fill) == 9  # #aarrggbb
    popup.close()
    assert pump(qapp, lambda: board.note_bar.btn_fill.popup is None, seconds=2)
    assert board.undo_stack.count() == steps + 1
    board.set_note_fill("#ffffff")  # a later change is its own step
    assert board.undo_stack.count() == steps + 2
    board.undo_stack.undo()
    board.undo_stack.undo()
    assert note.fill is None

    board.set_note_fill("#6633cc80")
    panel.save_now()
    board.load_page(panel.page)
    from ui.artbook_board import NoteItem

    (again,) = [i for i in board.items_list() if isinstance(i, NoteItem)]
    assert QColor(again.fill).alpha() == 0x66


def test_custom_text_colour_while_typing_goes_to_the_selected_text(qapp, panel):
    board = panel.board
    note = make_note(board, "one two")
    board.edit_note(note)
    cursor = note.textCursor()
    cursor.setPosition(4)
    cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
    note.setTextCursor(cursor)
    popup = open_custom(qapp, board.note_bar.btn_color)
    popup.set_color(QColor(255, 0, 0, 128), emit=True)
    assert char_format_at(note, 5).foreground().color() == QColor(255, 0, 0, 128)
    assert char_format_at(note, 1).foreground().color() != QColor(255, 0, 0, 128)
    popup.close()
    qapp.processEvents()
    assert note.editing()  # still typing


def test_typed_colours_and_the_dropper(qapp, panel, monkeypatch):
    from ui.color_popup import ColorPopup

    assert ColorPopup.parse("#3B82F6", "HEX") == QColor("#3b82f6")
    assert ColorPopup.parse("f00", "HEX") == QColor("#ff0000")
    assert ColorPopup.parse("12, 34, 56", "RGB") == QColor(12, 34, 56)
    assert ColorPopup.parse("0, 100%, 50%", "HSL") == QColor("#ff0000")
    assert ColorPopup.parse("blue-ish", "HEX") is None and ColorPopup.parse("1, 2", "RGB") is None

    board = panel.board
    note = make_note(board, "Drop")
    select_only(board, note)
    popup = open_custom(qapp, board.note_bar.btn_fill)
    popup.value_edit.setText("00ff00")
    popup._on_text()
    assert QColor(note.fill) == QColor("#00ff00")
    popup.mode_box.setCurrentText("RGB")
    assert popup.value_edit.text() == "0, 255, 0"

    popup.start_dropper()
    assert popup.overlays and all(o.isVisible() for o in popup.overlays)  # the screen, covered
    cover = popup.overlays[0]
    cover.image = QImage(cover.width(), cover.height(), QImage.Format.Format_RGB32)
    cover.image.fill(QColor("#123456"))
    QTest.mouseMove(cover, QPoint(30, 30))
    cover.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(30, 30), QPointF(30, 30), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert QColor(note.fill) == QColor("#123456")  # previewed
    cover.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    assert not popup.dropping() and not popup.overlays and QColor(note.fill) == QColor("#00ff00")  # Esc: put back
    assert popup.isVisible()  # (only the dropper ended)

    popup.start_dropper()
    cover = popup.overlays[0]
    cover.image = QImage(cover.width(), cover.height(), QImage.Format.Format_RGB32)
    cover.image.fill(QColor("#abcdef"))
    QTest.mouseClick(cover, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    assert not popup.dropping() and QColor(note.fill) == QColor("#abcdef")  # a click keeps it
    popup.close()


def test_the_picker_floats_beside_the_note_moves_and_closes_with_the_selection(qapp, panel):
    from PySide6.QtCore import QRect

    board = panel.board
    note = make_note(board, "Beside")
    select_only(board, note)
    board.viewport().repaint()
    assert pump(qapp, lambda: board.note_bar.isVisible(), seconds=2)
    popup = open_custom(qapp, board.note_bar.btn_fill)
    assert popup.windowFlags() & Qt.WindowType.Tool and not (popup.windowFlags() & Qt.WindowType.Popup & ~Qt.WindowType.Tool)
    box = board.mapFromScene(note.sceneBoundingRect()).boundingRect()
    note_on_screen = QRect(board.mapToGlobal(box.topLeft()), box.size())
    assert not popup.frameGeometry().intersects(note_on_screen)  # it doesn't cover the note
    assert not popup.frameGeometry().intersects(QRect(board.note_bar.mapToGlobal(QPoint(0, 0)), board.note_bar.size()))

    start = popup.pos()
    grab = QPoint(popup.width() // 2, 10)  # (the title)
    QTest.mousePress(popup, Qt.MouseButton.LeftButton, pos=grab)
    popup.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(grab), QPointF(popup.mapToGlobal(grab) + QPoint(-40, 25)), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    QTest.mouseRelease(popup, Qt.MouseButton.LeftButton, pos=grab)
    assert popup.pos() == start + QPoint(-40, 25)

    board._scene.clearSelection()  # nothing left to colour: the picker goes
    board.viewport().repaint()
    assert pump(qapp, lambda: board.note_bar.btn_fill.popup is None, seconds=2)


def test_highlights_have_rounded_corners(qapp, panel):
    from ui.artbook_board import highlight_boxes

    board = panel.board
    note = make_note(board, "soft rim light")
    select_only(board, note)
    board.set_note_highlight("#ff0000")
    cursor = QTextCursor(note.document())
    cursor.setPosition(5)
    cursor.setPosition(8, QTextCursor.MoveMode.KeepAnchor)
    fmt = cursor.charFormat()
    fmt.setFontWeight(QFont.Weight.Bold)
    cursor.mergeCharFormat(fmt)  # (a bold piece in the middle: still one highlight)
    note.setTextWidth(600)  # (one line)
    ((box, color),) = highlight_boxes(note.document())
    assert color == QColor("#ff0000") and box.width() > 50
    assert char_format_at(note, 2).background().color() == QColor("#ff0000")  # still in the formatting

    image = QImage(note.boundingRect().size().toSize(), QImage.Format.Format_ARGB32)
    image.fill(QColor("#00ff00"))
    painter = QPainter(image)
    note.paint(painter, QStyleOptionGraphicsItem(), None)
    painter.end()
    corner = box.adjusted(-2, 0, 2, 0).topLeft()
    assert image.pixelColor(int(corner.x()), int(corner.y())) != QColor("#ff0000")  # the corner is round
    middle = box.center()
    assert image.pixelColor(int(middle.x()), int(box.top()) + 1) == QColor("#ff0000")
