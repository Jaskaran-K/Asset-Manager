"""The Artbook's board cards (the 2026-11 upgrade): the tool rail, right-click > Add Here, and pages keeping item
kinds a version doesn't know (a newer version's cards survive being opened and saved by an older one)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QDropEvent
from PySide6.QtWidgets import QApplication, QDialog

from core import artbook_store as store
from tests.test_artbook import all_actions, pump  # noqa: F401  (shared helpers)


@pytest.fixture(autouse=True)
def no_motion():
    from ui.animations import set_motion_mode

    set_motion_mode("off")
    yield
    set_motion_mode("auto")


@pytest.fixture
def panel(qapp):
    from ui.artbook import ArtbookPanel

    widget = ArtbookPanel()
    widget.resize(1000, 700)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()


def notes(board):
    from ui.artbook_board import NoteItem

    return [item for item in board.items_list() if isinstance(item, NoteItem)]


def note_centre(note) -> QPointF:
    return note.sceneBoundingRect().center()


# ----------------------------------------------------------------------
# The tool rail
# ----------------------------------------------------------------------
def test_the_rail_floats_down_the_boards_left_edge(qapp, panel):
    board = panel.board
    rail = board.rail
    assert rail.isVisible() and set(rail.buttons) == {"note", "heading", "todo", "column", "table", "pictures", "line", "draw"}
    assert all(b.toolTip() and b.focusPolicy() == Qt.FocusPolicy.NoFocus for b in rail.buttons.values())  # (the board keeps its keys)
    assert "(T)" in rail.buttons["note"].toolTip() and "(D)" in rail.buttons["draw"].toolTip()
    frame = rail.frame.geometry().translated(rail.pos())
    assert frame.left() <= 20 and abs(frame.center().y() - board.height() / 2) <= 4  # (left edge, half way down)
    board.resize(board.width(), board.height() - 100)
    qapp.processEvents()
    frame = rail.frame.geometry().translated(rail.pos())
    assert abs(frame.center().y() - board.height() / 2) <= 4  # (it stays there as the board resizes)


def test_the_rail_moves_out_from_under_the_floating_pages_sidebar(qapp, panel):
    sidebar, rail = panel.sidebar, panel.board.rail
    sidebar.set_pinned(False)
    sidebar.set_open(True, animate=False)
    qapp.processEvents()
    left = rail.mapTo(panel, QPoint(0, 0)).x() + rail.layout().contentsMargins().left()
    assert left >= sidebar.panel.mapTo(panel, QPoint(sidebar.panel.width(), 0)).x()
    sidebar.set_open(False, animate=False)
    qapp.processEvents()
    assert rail.x() + rail.layout().contentsMargins().left() <= 20


def test_clicking_a_tool_adds_it_in_the_middle(qapp, panel):
    board = panel.board
    board.rail.buttons["note"].click()
    added = notes(board)
    assert len(added) == 1 and board.editing_note() is added[0]  # (ready to type in)
    centre = note_centre(added[0])
    middle = board.view_center()
    assert abs(centre.x() - middle.x()) < 2 and abs(centre.y() - middle.y()) < 2
    board.finish_note_edit()


def test_draw_on_the_rail_shows_when_drawing_is_on(qapp, panel):
    board, button = panel.board, panel.board.rail.buttons["draw"]
    button.click()
    assert board.drawing() and button.isChecked()
    board.toggle_draw()  # (D, or Done on the draw bar)
    assert not board.drawing() and not button.isChecked()


def test_pictures_on_the_rail_picks_files(qapp, panel, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from core.settings import get_last_pictures_dir

    Image.new("RGB", (60, 40), "red").save(tmp_path / "a.png")
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *args, **kwargs: ([str(tmp_path / "a.png")], "")))
    panel.board.rail.buttons["pictures"].click()
    assert pump(qapp, lambda: len(panel.board.items_list()) == 1)
    assert Path(get_last_pictures_dir()) == tmp_path  # (the picker opens there next time)


def drop_tool(qapp, board, tool: str, at: QPoint) -> QDropEvent:
    from ui.artbook_rail import TOOL_MIME

    mime = QMimeData()
    mime.setData(TOOL_MIME, tool.encode("ascii"))
    event = QDropEvent(QPointF(at), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    board.dropEvent(event)
    qapp.processEvents()
    return event


def test_dragging_a_tool_onto_the_board_puts_it_where_its_let_go(qapp, panel):
    board = panel.board
    spot = QPoint(600, 200)
    event = drop_tool(qapp, board, "note", spot)
    assert event.isAccepted() and len(notes(board)) == 1
    centre, wanted = note_centre(notes(board)[0]), board.mapToScene(spot)
    assert abs(centre.x() - wanted.x()) < 2 and abs(centre.y() - wanted.y()) < 2
    board.finish_note_edit()
    assert not drop_tool(qapp, board, "draw", spot).isAccepted()  # (a mode, not something to put down)
    assert not drop_tool(qapp, board, "nonsense", spot).isAccepted()


def test_the_rail_drag_is_listed_with_the_shortcuts():
    from ui import shortcuts

    entry = shortcuts.get("artbook.rail_drag")
    assert entry.sequence is None and "rail" in entry.keys.lower()


# ----------------------------------------------------------------------
# Right-click > Add Here
# ----------------------------------------------------------------------
def test_add_here_puts_a_note_where_you_clicked(qapp, panel):
    board = panel.board
    at = QPointF(321, -123)
    menu = board.context_menu(at)
    add_here = next(a for a in menu.actions() if a.text() == "Add Here").menu()
    assert [a.text() for a in add_here.actions() if a.text()] == ["Note", "Heading", "To-do", "Column", "Table", "Page Title", "Page Link", "Pictures...", "Line", "Draw"]
    next(a for a in add_here.actions() if a.text() == "Note").trigger()
    centre = note_centre(notes(board)[0])
    assert abs(centre.x() - at.x()) < 2 and abs(centre.y() - at.y()) < 2
    board.finish_note_edit()
    menu.deleteLater()


# ----------------------------------------------------------------------
# Kinds this version doesn't know
# ----------------------------------------------------------------------
def test_a_newer_versions_cards_survive_being_opened_and_saved(qapp, panel):
    board = panel.board
    page = store.new_page("Newer")
    card = {"id": "c1", "kind": "todo", "x": 10, "y": 20, "rows": [{"text": "Hood", "done": True}], "group": "g1"}
    page["items"] = [
        {"id": "n1", "kind": "note", "text": "hello", "x": 0, "y": 0, "group": "g1"},
        card,
        {"id": "bad", "kind": "<script>"},  # (not a kind at all: dropped)
    ]
    page["groups"] = {"g1": {"fill": "#ff0000"}}
    store.save_page_now(page)
    board.load_page(store.load_page(page["id"]))
    assert [i["kind"] for i in board.page_items()] == ["note", "todo"]
    assert board.page_items()[1] == card  # (untouched)
    board.add_note(QPointF(50, 50))
    board.finish_note_edit()
    assert board.page_items()[-1] == card and "g1" in board.page_groups()  # (its group's colour stays too)
    board.load_page(store.load_page(store.new_page("Other")["id"]))
    assert all(i["kind"] != "todo" for i in board.page_items())  # (kept per page, not carried to the next)


def test_a_shared_page_only_brings_in_kinds_this_version_reads(qapp, tmp_path):
    from core import artbook_share

    assert set(artbook_share.KINDS) == {"image", "note", "stroke", "column", "line", "table", "link", "file", "page_card", "point"}


# ----------------------------------------------------------------------
# Batch B: notes as cards, headings, plain text; paragraphs and their typing shortcuts
# ----------------------------------------------------------------------
def render(board, item) -> "QImage":
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter

    box = item.sceneBoundingRect()
    image = QImage(int(box.width()), int(box.height()), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    board._scene.show_area = False  # (just the item: not the board's active area behind it)
    try:
        board._scene.render(painter, QRectF(0, 0, box.width(), box.height()), box)
    finally:
        board._scene.show_area = True
        painter.end()
    return image


def close_to(a, b, tolerance=40) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a.getRgb()[:3], b.getRgb()[:3]))


def test_a_heading_is_a_card_with_an_accent_line(qapp, panel):
    from ui.artbook_board import HEADING_PT, NOTE_PADDING, RULE_HEIGHT, RULE_WIDTH
    from ui.themes import theme_color

    from PySide6.QtTest import QTest

    board = panel.board
    board.rail.buttons["heading"].click()
    heading = notes(board)[0]
    assert heading.style == "heading" and board.editing_note() is heading
    assert heading.font().pointSize() == HEADING_PT and heading.font().bold()  # (typed text starts big and bold)
    QTest.keyClicks(board, "Sculpting")
    board.finish_note_edit()
    board._scene.clearSelection()
    qapp.processEvents()
    image = render(board, heading)
    scale = heading.scale()
    centre_x = image.width() / 2  # (a new heading is centred, its line too)
    line_y = image.height() - (NOTE_PADDING + RULE_HEIGHT / 2) * scale
    assert close_to(image.pixelColor(int(centre_x), int(line_y)), theme_color("accent"))
    assert not close_to(image.pixelColor(int(centre_x + RULE_WIDTH * scale), int(line_y)), theme_color("accent"))
    assert heading.to_dict()["style"] == "heading"


def test_shift_t_adds_a_heading(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    board.setFocus()
    QTest.keyClick(board, Qt.Key.Key_T, Qt.KeyboardModifier.ShiftModifier)
    assert [n.style for n in notes(board)] == ["heading"]
    board.finish_note_edit()


def test_a_notes_style_changes_with_undo_and_plain_has_no_card(qapp, panel):
    from ui.themes import theme_color

    board = panel.board
    note = board.add_note(QPointF(0, 0))
    note.setPlainText("Strap buckle")
    board.finish_note_edit()
    note.setSelected(True)
    qapp.processEvents()
    image = render(board, note)
    assert close_to(image.pixelColor(image.width() // 2, 3), theme_color("raised"), 12)  # (the card's own colour)
    board.set_note_style("plain")
    assert note.style == "plain" and not board.note_bar.btn_fill.isEnabled()  # (no card to colour)
    board._scene.clearSelection()
    qapp.processEvents()
    image = render(board, note)
    assert image.pixelColor(image.width() // 2, 3).alpha() == 0  # (just the text)
    note.setSelected(True)
    board.set_note_style("heading")
    assert note.style == "heading" and note.toPlainText() == "Strap buckle"
    board.undo_stack.undo()
    board.undo_stack.undo()
    assert note.style == "card" and board.note_bar.btn_style.text() == "Card"


def test_paragraphs_from_the_note_bar(qapp, panel):
    from PySide6.QtGui import QTextCursor

    from ui.artbook_board import PARAGRAPH_PT, paragraph_of

    board = panel.board
    note = board.add_note(QPointF(0, 0))
    note.setPlainText("Hood\nTop straps\nHood silhouette")
    board.finish_note_edit()
    note.setSelected(True)
    board.set_note_paragraph("bullet")  # (not typing: every line)
    lines = [note.document().findBlockByNumber(i) for i in range(3)]
    assert all(b.textList() is not None and b.textList() == lines[0].textList() for b in lines)
    board.set_note_paragraph("number")
    assert paragraph_of(QTextCursor(lines[0])) == "number"
    board.set_note_paragraph("text")
    assert all(b.textList() is None for b in lines)
    board.undo_stack.undo()
    assert lines[0].textList() is not None  # (one undo step each)

    board.edit_note(note)  # typing: just the line the cursor's on
    cursor = note.textCursor()
    cursor.setPosition(0)
    note.setTextCursor(cursor)
    board.set_note_paragraph("h1")
    first = QTextCursor(note.document().findBlockByNumber(0))
    assert paragraph_of(first) == "h1" and first.blockFormat().headingLevel() == 1
    first.movePosition(QTextCursor.MoveOperation.NextCharacter)
    assert first.charFormat().fontPointSize() == PARAGRAPH_PT["h1"]
    assert paragraph_of(QTextCursor(note.document().findBlockByNumber(1))) == "number"
    assert board.note_bar.btn_paragraph.text() == "Heading 1"
    board.finish_note_edit()


@pytest.mark.parametrize("typed, kind", [("# ", "h1"), ("## ", "h2"), ("- ", "bullet"), ("* ", "bullet"), ("1. ", "number")])
def test_typing_markdown_at_a_lines_start(qapp, panel, typed, kind):
    from PySide6.QtGui import QTextCursor
    from PySide6.QtTest import QTest

    from ui.artbook_board import paragraph_of

    board = panel.board
    board.setFocus()
    note = board.add_note(QPointF(0, 0))
    QTest.keyClicks(board, typed + "Hood")
    assert note.toPlainText() == "Hood"  # (the marker goes; the line takes its form)
    assert paragraph_of(QTextCursor(note.document().firstBlock())) == kind
    QTest.keyClicks(board, " - a dash later in a line stays")
    assert note.toPlainText().endswith("- a dash later in a line stays")
    board.finish_note_edit()


def test_note_styles_are_saved_and_older_notes_are_cards(qapp, panel):
    board = panel.board
    page = store.new_page("Styles")
    page["items"] = [
        {"id": "h", "kind": "note", "text": "Sculpting", "style": "heading", "x": 0, "y": 0},
        {"id": "p", "kind": "note", "text": "a caption", "style": "plain", "x": 0, "y": 200},
        {"id": "o", "kind": "note", "text": "from 0.6.25", "x": 0, "y": 400},
        {"id": "b", "kind": "note", "text": "nonsense", "style": "<b>", "x": 0, "y": 600},
    ]
    store.save_page_now(page)
    board.load_page(store.load_page(page["id"]))
    styles = {n.item_id: n.style for n in notes(board)}
    assert styles == {"h": "heading", "p": "plain", "o": "card", "b": "card"}
    saved = {d["id"]: d["style"] for d in board.page_items()}
    assert saved == styles


def test_the_markdown_is_listed_with_the_shortcuts():
    from ui import shortcuts

    assert shortcuts.get("artbook.note_markdown").sequence is None
    assert shortcuts.get("artbook.heading").sequence is not None


# ----------------------------------------------------------------------
# Batch C: to-do lists (checklist lines: done ones sink, a count and a bar, lines dragged by their dots)
# ----------------------------------------------------------------------
def lines(note) -> list[tuple[str, str]]:
    """Each line of a note: (text, what it is: "done" / "open" for a checklist line, else its paragraph)."""
    from PySide6.QtGui import QTextBlockFormat, QTextCursor

    from ui.artbook_board import is_check, paragraph_of

    out, block = [], note.document().firstBlock()
    while block.isValid():
        if is_check(block):
            kind = "done" if block.blockFormat().marker() == QTextBlockFormat.MarkerType.Checked else "open"
        else:
            kind = paragraph_of(QTextCursor(block))
        out.append((block.text(), kind))
        block = block.next()
    return out


def make_todo(qapp, board, title="Hood", rows=("Top straps", "Top strap hooks", "Hood silhouette")):
    from PySide6.QtTest import QTest

    board.setFocus()
    QTest.keyClick(board, Qt.Key.Key_X)
    note = board.editing_note()
    QTest.keyClicks(board, title)
    for i, row in enumerate(rows):
        QTest.keyClick(board, Qt.Key.Key_Return)  # (from the title: on to the first line, then a new line each time)
        QTest.keyClicks(board, row)
    board.finish_note_edit()
    board._scene.clearSelection()
    qapp.processEvents()
    return note


def click_box(qapp, board, note, row_text):
    from PySide6.QtTest import QTest

    row = next(b for b in note.check_rows() if b.text() == row_text)
    point = board.mapFromScene(note.mapToScene(note.box_rect(row).center()))
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qapp.processEvents()


def test_x_makes_a_to_do_typed_line_by_line(qapp, panel):
    board = panel.board
    note = make_todo(qapp, board)
    assert lines(note) == [("Hood", "h2"), ("Top straps", "open"), ("Top strap hooks", "open"), ("Hood silhouette", "open")]
    assert note.progress() == (0, 3) and note.style == "card"
    assert board.rail.buttons["todo"].toolTip().startswith("To-do (X)")


def test_ticking_sinks_a_line_and_unticking_brings_it_back(qapp, panel):
    board = panel.board
    note = make_todo(qapp, board)
    click_box(qapp, board, note, "Top straps")
    assert lines(note)[1:] == [("Top strap hooks", "open"), ("Hood silhouette", "open"), ("Top straps", "done")]
    assert note.progress() == (1, 3) and not note.isSelected()  # (a tick isn't a click on the card)
    click_box(qapp, board, note, "Hood silhouette")
    assert lines(note)[1:] == [("Top strap hooks", "open"), ("Top straps", "done"), ("Hood silhouette", "done")]
    click_box(qapp, board, note, "Hood silhouette")  # (unticked: back above the done ones)
    assert lines(note)[1:] == [("Top strap hooks", "open"), ("Hood silhouette", "open"), ("Top straps", "done")]
    board.undo_stack.undo()
    board.undo_stack.undo()
    board.undo_stack.undo()
    assert lines(note)[1:] == [("Top straps", "open"), ("Top strap hooks", "open"), ("Hood silhouette", "open")]


def test_a_new_line_goes_above_the_done_ones(qapp, panel):
    from PySide6.QtGui import QTextCursor
    from PySide6.QtTest import QTest

    board = panel.board
    note = make_todo(qapp, board, rows=("One", "Two"))
    click_box(qapp, board, note, "One")
    board.edit_note(note)
    cursor = note.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)  # (at the end of the done line)
    note.setTextCursor(cursor)
    QTest.keyClick(board, Qt.Key.Key_Return)
    QTest.keyClicks(board, "Three")
    board.finish_note_edit()
    assert lines(note)[1:] == [("Two", "open"), ("Three", "open"), ("One", "done")]


def test_checklists_from_typing_and_the_paragraph_menu(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    board.setFocus()
    note = board.add_note(QPointF(0, 0))
    QTest.keyClicks(board, "[] Buckle")
    assert lines(note) == [("Buckle", "open")]
    board.finish_note_edit()
    note.setSelected(True)
    assert board.note_bar.btn_paragraph.text() == "Checklist"
    board.set_note_paragraph("bullet")
    assert lines(note) == [("Buckle", "bullet")] and note.progress() == (0, 0)
    board.set_note_paragraph("check")
    assert lines(note) == [("Buckle", "open")]


def test_a_to_do_is_saved_with_its_ticks(qapp, panel):
    from PySide6.QtGui import QTextListFormat

    board = panel.board
    note = make_todo(qapp, board)
    click_box(qapp, board, note, "Top straps")
    data = next(d for d in board.page_items() if d["id"] == note.item_id)
    page = store.new_page("Ticks")
    page["items"] = [data]
    store.save_page_now(page)
    board.load_page(store.load_page(page["id"]))
    again = notes(board)[0]
    assert lines(again) == lines(note) and again.progress() == (1, 3)
    listed = again.check_rows()[0].textList()
    assert listed.format().style() == QTextListFormat.Style.ListStyleUndefined  # (the note draws the boxes, not Qt)


def test_dragging_a_lines_dots_moves_it(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    note = make_todo(qapp, board, rows=("One", "Two", "Three"))
    rows = note.check_rows()
    grip = board.mapFromScene(note.mapToScene(note.grip_rect(rows[0]).center()))
    target = board.mapFromScene(note.mapToScene(note.grip_rect(rows[2]).center()))
    QTest.mouseMove(board.viewport(), grip)
    assert note.hover_row == rows[0].blockNumber()  # (hovering a line shows its dots)
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, pos=grip)
    QTest.mouseMove(board.viewport(), target)
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, pos=target)
    qapp.processEvents()
    assert [t for t, _k in lines(note)[1:]] == ["Two", "Three", "One"] and note.drop_row is None
    board.undo_stack.undo()
    assert [t for t, _k in lines(note)[1:]] == ["One", "Two", "Three"]


def test_the_count_and_the_bar_show_progress(qapp, panel):
    from ui.artbook_board import BAND, BAR_HEIGHT, NOTE_PADDING
    from ui.themes import theme_color

    board = panel.board
    note = make_todo(qapp, board, rows=("One", "Two"))
    click_box(qapp, board, note, "One")
    board._scene.clearSelection()
    qapp.processEvents()
    image = render(board, note)
    scale = note.scale()
    first = note._first_line(note.check_rows()[0])
    bar_y = (first.top() - BAND / 2 - 1) * scale
    left = (NOTE_PADDING + 4) * scale
    right_end = (note.boundingRect().right() - NOTE_PADDING - 4) * scale
    assert close_to(image.pixelColor(int(left), int(bar_y)), theme_color("accent"))  # (half done: the bar starts filled)
    assert not close_to(image.pixelColor(int(right_end), int(bar_y)), theme_color("accent"))  # (and ends empty)
    done_box = note.box_rect(note.check_rows()[1])
    corner = done_box.bottomLeft() + QPointF(3, -3)  # (inside the box, away from its tick)
    assert close_to(image.pixelColor(int(corner.x() * scale), int(corner.y() * scale)), theme_color("accent"))


def test_the_to_do_gestures_are_listed_with_the_shortcuts():
    from ui import shortcuts

    assert shortcuts.get("artbook.todo").sequence is not None
    assert shortcuts.get("artbook.todo_tick").sequence is None and shortcuts.get("artbook.todo_row_drag").sequence is None


# ----------------------------------------------------------------------
# Batch D: columns (cards stack in them, fold to a title, an x to delete them with or without their cards)
# ----------------------------------------------------------------------
def drag(qapp, board, start_scene, end_scene, steps=4):
    """A left-button drag on the board, from one board point to another."""
    from PySide6.QtTest import QTest

    start, end = board.mapFromScene(start_scene), board.mapFromScene(end_scene)
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, pos=start)
    for n in range(1, steps + 1):
        QTest.mouseMove(board.viewport(), start + (end - start) * (n / steps))
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, pos=end)
    qapp.processEvents()


def make_column(qapp, board, at=QPointF(0, 0), title="Character Concepts"):
    from PySide6.QtTest import QTest

    board.setFocus()
    column = board.add_column(at)
    QTest.keyClicks(board, title)
    QTest.keyClick(board, Qt.Key.Key_Return)
    board._scene.clearSelection()
    qapp.processEvents()
    return column


def make_note(qapp, board, at, text):
    from PySide6.QtTest import QTest

    board.setFocus()
    note = board.add_note(at)
    QTest.keyClicks(board, text)
    board.finish_note_edit()
    board._scene.clearSelection()
    qapp.processEvents()
    return note


def inside(column, y_share=0.9) -> QPointF:
    """A board point inside a column, this far down it."""
    body = column.mapRectToScene(column.crop_rect())
    return QPointF(body.center().x(), body.top() + body.height() * y_share)


def into(qapp, board, card, column, y_share=0.97):
    drag(qapp, board, card.sceneBoundingRect().center(), inside(column, y_share))


def test_shift_c_makes_a_column_and_types_its_title(qapp, panel):
    from PySide6.QtTest import QTest

    from ui.artbook_board import ColumnItem

    board = panel.board
    board.setFocus()
    QTest.keyClick(board, Qt.Key.Key_C, Qt.KeyboardModifier.ShiftModifier)
    column = next(i for i in board.items_list() if isinstance(i, ColumnItem))
    assert board.renaming_column() is column and column.title() == "Untitled"
    QTest.keyClicks(board, "Character Concepts")  # (the title starts selected: typing replaces it)
    QTest.keyClick(board, Qt.Key.Key_Return)
    assert board.renaming_column() is None and column.title() == "Character Concepts"
    assert column.chip_text() == "0 cards"
    board.undo_stack.undo()
    assert column.title() == "Untitled"
    board.undo_stack.undo()
    assert board.item(column.item_id) is None
    assert board.rail.buttons["column"].toolTip().startswith("Column (Shift+C)")


def test_cards_dragged_onto_a_column_stack_in_it_at_its_width(qapp, panel):
    board = panel.board
    column = make_column(qapp, board)
    hood = make_note(qapp, board, QPointF(600, 0), "Hood")
    torso = make_note(qapp, board, QPointF(600, 300), "Torso clothes: refine the folds on the crop top jacket")
    into(qapp, board, hood, column)
    assert column.members == [hood.item_id] and not column.hot and column.drop_y is None
    into(qapp, board, torso, column, 0.2)  # (above Hood: it goes first)
    assert column.members == [torso.item_id, hood.item_id]
    assert column.chip_text() == "2 cards"
    inner = column.inner_width()
    for card in (torso, hood):
        assert card.textWidth() == inner and card.scale() == column.scale() and not card.rotation()
        assert abs(card.sceneBoundingRect().left() - column.mapToScene(QPointF(10, 0)).x()) < 0.01
    assert torso.sceneBoundingRect().bottom() < hood.sceneBoundingRect().top()  # (stacked, in order)
    assert column.mapRectToScene(column.crop_rect()).bottom() > hood.sceneBoundingRect().bottom()  # (it grew round them)
    board.undo_stack.undo()
    assert column.members == [hood.item_id]
    board.undo_stack.redo()
    assert column.members == [torso.item_id, hood.item_id]


def test_while_dragging_a_line_shows_where_the_card_goes(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    column = make_column(qapp, board)
    hood = make_note(qapp, board, QPointF(600, 0), "Hood")
    start, end = board.mapFromScene(hood.sceneBoundingRect().center()), board.mapFromScene(inside(column))
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(board.viewport(), (start + end) / 2)
    QTest.mouseMove(board.viewport(), end)
    assert column.hot and column.drop_y is not None and hood.lifted  # (the card fades: the line shows through it)
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert not column.hot and column.members == [hood.item_id] and not hood.lifted


def test_cards_move_within_a_column_and_out_of_it(qapp, panel):
    board = panel.board
    column = make_column(qapp, board)
    cards = [make_note(qapp, board, QPointF(600, 200 * n), text) for n, text in enumerate(("One", "Two", "Three"))]
    for card in cards:
        into(qapp, board, card, column)
    assert column.members == [c.item_id for c in cards]
    one, two, three = cards
    drag(qapp, board, three.sceneBoundingRect().center(), one.sceneBoundingRect().topLeft() + QPointF(20, 2))  # (to the top)
    assert column.members == [three.item_id, one.item_id, two.item_id]
    away = column.mapRectToScene(column.crop_rect()).topRight() + QPointF(400, 0)
    drag(qapp, board, two.sceneBoundingRect().center(), away)  # (out: it stays where it is let go)
    assert column.members == [three.item_id, one.item_id] and abs(two.sceneBoundingRect().center().x() - away.x()) < 1
    assert board.column_of(two) is None and column.chip_text() == "2 cards"
    board.undo_stack.undo()
    assert column.members == [three.item_id, one.item_id, two.item_id]


def test_a_click_on_a_card_in_a_column_leaves_it_there(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    column = make_column(qapp, board)
    hood = make_note(qapp, board, QPointF(600, 0), "Hood")
    into(qapp, board, hood, column)
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, pos=board.mapFromScene(hood.sceneBoundingRect().center()))
    assert column.members == [hood.item_id] and hood.isSelected()
    corner = board.mapFromScene(hood.sceneBoundingRect().topLeft())
    assert board._handle_at(QPointF(corner)) is None  # (no handles of its own: the column sizes it)


def test_a_new_note_made_over_a_column_goes_in_it(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    column = make_column(qapp, board)
    note = make_note(qapp, board, inside(column, 0.8), "Straps and belts")
    assert column.members == [note.item_id] and note.textWidth() == column.inner_width()
    board.undo_stack.undo()  # (the note, and its place in the column: one step)
    assert board.item(note.item_id) is None and column.members == []
    board.undo_stack.redo()
    assert column.members == [note.item_id]
    board.add_tool("todo", inside(column, 0.97))  # (the rail's drop and Add Here make cards at a point too)
    QTest.keyClicks(board, "Arms")
    board.finish_note_edit()
    assert len(column.members) == 2


def test_moving_and_widening_a_column_takes_its_cards_along(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    column = make_column(qapp, board)
    hood = make_note(qapp, board, QPointF(600, 0), "Hood")
    into(qapp, board, hood, column)
    offset, start = hood.scenePos() - column.scenePos(), column.scenePos()
    title = column.mapToScene(column.title_rect().center())
    drag(qapp, board, title, title + QPointF(150, 80))
    assert (column.scenePos() - start - QPointF(150, 80)).manhattanLength() < 2
    assert (hood.scenePos() - column.scenePos() - offset).manhattanLength() < 0.01
    board._scene.clearSelection()
    column.setSelected(True)
    qapp.processEvents()
    body = column.mapRectToScene(column.crop_rect())
    right = board.mapFromScene(QPointF(body.right(), body.center().y()))
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, pos=right)
    QTest.mouseMove(board.viewport(), right + QPoint(120, 0))
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, pos=right + QPoint(120, 0))
    assert column.width > 300 and hood.textWidth() == column.inner_width()
    board.undo_stack.undo()
    assert column.width == 300 and hood.textWidth() == column.inner_width()


def test_folding_a_column_hides_its_cards(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    column = make_column(qapp, board)
    hood = make_note(qapp, board, QPointF(600, 0), "Hood")
    into(qapp, board, hood, column)
    arrow = board.mapFromScene(column.mapToScene(column.chevron_rect().center()))
    assert board.hover_cursor(QPointF(arrow)).shape() == Qt.CursorShape.PointingHandCursor
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, pos=arrow)
    assert column.folded and not hood.isVisible() and column.height == column.header_height()
    assert column.chip_text() == "1 card"
    board.load_page({"id": board.page_id, "items": board.page_items()})
    again = board.item(column.item_id)
    assert again.folded and not board.item(hood.item_id).isVisible()
    arrow = board.mapFromScene(again.mapToScene(again.chevron_rect().center()))
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, pos=arrow)
    assert not again.folded and board.item(hood.item_id).isVisible()
    board.undo_stack.undo()
    assert again.folded


def test_a_picture_in_a_column_takes_its_width(qapp, panel, tmp_path):
    from tests.test_artbook import add_and_wait

    Image.new("RGB", (400, 200), "red").save(tmp_path / "wide.png")
    board = panel.board
    column = make_column(qapp, board)
    picture, = add_and_wait(qapp, board, [tmp_path / "wide.png"])
    into(qapp, board, picture, column)
    assert column.members == [picture.item_id] and column.chip_text() == "1 image"
    width = column.inner_width() * column.scale()
    box = picture.sceneBoundingRect()
    assert abs(box.width() - width) < 0.01 and abs(box.height() - width / 2) < 0.01
    note = make_note(qapp, board, QPointF(900, 0), "Front view")
    into(qapp, board, note, column)
    assert column.chip_text() == "1 card, 1 image"


def test_hovering_a_column_shows_an_x_with_both_ways_to_delete_it(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    column = make_column(qapp, board)
    hood = make_note(qapp, board, inside(column, 0.8), "Hood")  # (made in it: test moves after a test drag don't arrive)
    assert board.column_of(hood) is column
    QTest.mouseMove(board.viewport(), board.mapFromScene(inside(column, 0.5) + QPointF(400, 0)))
    assert not column.hovered
    QTest.mouseMove(board.viewport(), board.mapFromScene(inside(column, 0.5)))
    assert column.hovered
    x = board.mapFromScene(column.mapToScene(column.close_rect().center()))
    QTest.mouseMove(board.viewport(), x)
    assert column.close_hot and board.hover_cursor(QPointF(x)).shape() == Qt.CursorShape.PointingHandCursor
    menu = board.column_delete_menu(column)
    assert [a.text() for a in menu.actions()] == ["Delete Column and Its Cards", "Delete Column, Keep the Cards"]
    menu.deleteLater()
    board.delete_column(column, keep_cards=True)
    assert board.item(column.item_id) is None and board.item(hood.item_id) is hood and hood.isVisible()
    assert board.column_of(hood) is None
    board.undo_stack.undo()
    assert board.column_of(hood) is column
    board.delete_column(column, keep_cards=False)
    assert board.item(column.item_id) is None and board.item(hood.item_id) is None
    board.undo_stack.undo()
    assert board.column_of(board.item(hood.item_id)) is column


def test_delete_on_a_selected_column_takes_its_cards(qapp, panel):
    board = panel.board
    column = make_column(qapp, board)
    hood = make_note(qapp, board, QPointF(600, 0), "Hood")
    into(qapp, board, hood, column)
    board._scene.clearSelection()
    column.setSelected(True)
    board.delete_selection()
    assert board.items_list() == []
    board.undo_stack.undo()
    assert board.column_of(board.item(hood.item_id)) is column


def test_right_click_on_a_column(qapp, panel):
    board = panel.board
    column = make_column(qapp, board)
    menu = board.context_menu(inside(column, 0.5))
    entries = [a.text() for a in next(a for a in menu.actions() if a.text() == "Column").menu().actions() if a.text()]
    assert entries == ["Rename", "Fold", "Delete Column and Its Cards", "Delete Column, Keep the Cards"]
    menu.deleteLater()
    menu = board.context_menu(QPointF(5000, 5000))
    assert "Column" not in [a.text() for a in menu.actions()]
    menu.deleteLater()


def test_a_column_is_saved_and_shared_with_its_cards(qapp, panel, tmp_path):
    from core import artbook_share
    from ui.artbook_board import ColumnItem

    board = panel.board
    column = make_column(qapp, board)
    hood = make_note(qapp, board, QPointF(600, 0), "Hood")
    into(qapp, board, hood, column)
    page = store.load_page(board.page_id)
    page["items"] = board.page_items()
    store.save_page_now(page)
    data = next(d for d in page["items"] if d["kind"] == "column")
    assert data["title"] == "Character Concepts" and data["members"] == [hood.item_id]
    artbook_share.export_page(page["id"], tmp_path / "shared.artbook")
    shared = artbook_share.import_page(tmp_path / "shared.artbook")
    columns = [d for d in shared["items"] if d["kind"] == "column"]
    cards = [d for d in shared["items"] if d["kind"] == "note"]
    assert columns[0]["members"] == [cards[0]["id"]] and cards[0]["id"] != hood.item_id  # (new ids, still its card)
    board.load_page(shared)
    again = next(i for i in board.items_list() if isinstance(i, ColumnItem))
    assert [c.toPlainText() for c in board.column_cards(again)] == ["Hood"]


def test_the_column_gestures_are_listed_with_the_shortcuts():
    from ui import shortcuts

    assert shortcuts.get("artbook.column").sequence is not None
    for gesture in ("column_drop", "column_fold", "column_rename", "column_delete"):
        assert shortcuts.get(f"artbook.{gesture}").sequence is None


# ----------------------------------------------------------------------
# Batch E: lines and arrows (from a hovered card's dots or the Line tool; several at once; a bar; labels)
# ----------------------------------------------------------------------
def lines_on(board):
    from ui.artbook_board import LineItem

    return [item for item in board.items_list() if isinstance(item, LineItem)]


def join(board, a, b):
    """A line from a to b, as a drag from a's dot let go on b makes it."""
    drag = {"mode": "line_draw", "source": a, "target": b, "press": board._line_box(a).center(),
            "point": board._line_box(b).center()}
    return board._finish_line(drag)


def view_point(board, scene_point) -> QPoint:
    return board.mapFromScene(scene_point)


def test_dragging_a_hovered_cards_dot_draws_an_arrow(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    hood = make_note(qapp, board, QPointF(-300, 0), "Hood")
    rim = make_note(qapp, board, QPointF(200, 150), "Hood rim thickness")
    QTest.mouseMove(board.viewport(), view_point(board, hood.sceneBoundingRect().center()))
    assert board._dot_card is hood
    dot = dict(board._line_dots(hood))["right"]
    assert board.hover_cursor(QPointF(view_point(board, dot))).shape() == Qt.CursorShape.CrossCursor
    start, end = view_point(board, dot), view_point(board, rim.sceneBoundingRect().center())
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(board.viewport(), (start + end) / 2)
    QTest.mouseMove(board.viewport(), end)
    assert board._drag["target"] is rim  # (lit up while the mouse is over it)
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, pos=end)
    line, = lines_on(board)
    assert line.start[0] == hood.item_id and line.end[0] == rim.item_id and line.isSelected()
    assert (line.style, line.heads) == ("curve", "end")
    (tail, _d), (tip, _d2) = line.tips
    assert abs(tail.x() - hood.sceneBoundingRect().right()) < 10 and abs(tip.x() - rim.sceneBoundingRect().left()) < 10
    board.undo_stack.undo()
    assert lines_on(board) == []


def test_a_line_follows_its_cards_about(qapp, panel):
    board = panel.board
    hood = make_note(qapp, board, QPointF(-300, 0), "Hood")
    rim = make_note(qapp, board, QPointF(200, 0), "Hood rim thickness")
    line, = join(board, hood, rim)
    board._move_by({rim.item_id: QPointF(0, 400)}, "move")
    tip = line.tips[1][0]
    assert abs(tip.y() - rim.sceneBoundingRect().top()) < 10  # (now below: it arrives at the top)
    board.undo_stack.undo()
    assert abs(line.tips[1][0].x() - rim.sceneBoundingRect().left()) < 10


def test_several_selected_cards_get_a_line_each(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    arms = make_note(qapp, board, QPointF(-400, 0), "Arms")
    cards = [make_note(qapp, board, QPointF(300, y), text) for y, text in ((-200, "Glove"), (0, "Guard"), (200, "Strap"))]
    for card in cards:
        card.setSelected(True)
    board.setFocus()
    QTest.keyClick(board, Qt.Key.Key_L, Qt.KeyboardModifier.ShiftModifier)  # (the Line tool: drag from anywhere on a card)
    assert board.line_tool_on() and board.rail.buttons["line"].isChecked()
    start, end = view_point(board, arms.sceneBoundingRect().center()), view_point(board, cards[1].sceneBoundingRect().center())
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(board.viewport(), end)
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, pos=end)
    made = lines_on(board)
    assert sorted(line.end[0] for line in made) == sorted(card.item_id for card in cards)
    assert {line.start[0] for line in made} == {arms.item_id}
    board.undo_stack.undo()  # (all of them: one step)
    assert lines_on(board) == []
    QTest.keyClick(board, Qt.Key.Key_Escape)
    assert not board.line_tool_on() and not board.rail.buttons["line"].isChecked()


def test_drawn_from_a_selected_card_a_line_comes_from_each(qapp, panel):
    board = panel.board
    a, b = make_note(qapp, board, QPointF(-300, -100), "One"), make_note(qapp, board, QPointF(-300, 100), "Two")
    target = make_note(qapp, board, QPointF(300, 0), "Belt")
    a.setSelected(True)
    b.setSelected(True)
    made = join(board, a, target)
    assert sorted(line.start[0] for line in made) == sorted([a.item_id, b.item_id]) and {l.end[0] for l in made} == {target.item_id}


def test_the_line_tool_draws_free_lines_and_a_click_draws_none(qapp, panel):
    board = panel.board
    board.start_line_tool()
    click = {"mode": "line_draw", "source": None, "target": None, "press": QPointF(0, 0), "point": QPointF(1, 0)}
    assert board._finish_line(click) == []  # (pressed and let go in one spot: nothing)
    drag = {"mode": "line_draw", "source": None, "target": None, "press": QPointF(0, 0), "point": QPointF(300, 0)}
    line, = board._finish_line(drag)
    assert line.start == (None, 0.0, 0.0) and line.end == (None, 300.0, 0.0)
    assert line.tips[1][0] == QPointF(300, 0)


def test_deleting_a_card_takes_its_lines(qapp, panel):
    board = panel.board
    hood = make_note(qapp, board, QPointF(-300, 0), "Hood")
    rim = make_note(qapp, board, QPointF(200, 0), "Hood rim thickness")
    line, = join(board, hood, rim)
    board._scene.clearSelection()
    hood.setSelected(True)
    board.delete_selection()
    assert board.item(line.item_id) is None and board.item(rim.item_id) is rim
    board.undo_stack.undo()
    assert board.item(line.item_id) is line and line.isVisible()


def test_the_line_bar_changes_the_selected_lines(qapp, panel):
    board = panel.board
    hood = make_note(qapp, board, QPointF(-300, 0), "Hood")
    others = [make_note(qapp, board, QPointF(300, y), t) for y, t in ((-150, "Rim"), (150, "Strap"))]
    made = [join(board, hood, other)[0] for other in others]
    board._scene.clearSelection()
    for line in made:
        line.setSelected(True)
    board._place_note_bar()
    bar = board.line_bar
    assert bar.isVisible() and bar.btn_shape.text() == "Curved" and bar.btn_ends.text() == "Arrow at the End"
    assert not bar.btn_label.isEnabled()  # (one line's label at a time)
    board.set_line_shape("elbow")
    board.set_line_heads("both")
    board.set_line_width(5)
    board.toggle_line_dashed()
    board.set_line_color("#ff0000")
    assert all((l.style, l.heads, l.width, l.dashed, l.color) == ("elbow", "both", 5, True, "#ff0000") for l in made)
    assert bar.btn_shape.text() == "Elbow" and bar.btn_dashed.isChecked()
    for _ in range(5):
        board.undo_stack.undo()
    assert all((l.style, l.heads, l.width, l.dashed, l.color) == ("curve", "end", 2, False, None) for l in made)
    board.delete_selection()
    assert lines_on(board) == [] and not bar.isVisible() or board._place_note_bar() is None


def test_enter_on_a_selected_line_types_its_label(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    hood = make_note(qapp, board, QPointF(-300, 0), "Hood")
    rim = make_note(qapp, board, QPointF(200, 120), "Hood rim thickness")
    line, = join(board, hood, rim)
    board._scene.clearSelection()
    line.setSelected(True)
    board.setFocus()
    QTest.keyClick(board, Qt.Key.Key_Return)  # (double-clicking the line itself puts a point on it: test_artbook_line_points)
    assert board._renaming is not None and board._renaming["item"] is line and line.has_pill()
    QTest.keyClicks(board, "Strap buckle")
    QTest.keyClick(board, Qt.Key.Key_Return)
    assert line.label() == "Strap buckle" and line.has_pill()
    assert line.pill_rect().contains(line.mid)  # (on the line's middle)
    board.undo_stack.undo()
    assert line.label() == "" and not line.has_pill()


def test_dragging_a_selected_lines_end_or_middle(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    hood = make_note(qapp, board, QPointF(-300, 0), "Hood")
    rim = make_note(qapp, board, QPointF(200, -150), "Rim")
    strap = make_note(qapp, board, QPointF(200, 200), "Strap")
    line, = join(board, hood, rim)
    tip = view_point(board, line.tips[1][0])
    assert board._line_handle_at(QPointF(tip)) == "end"
    end = view_point(board, strap.sceneBoundingRect().center())
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, pos=tip)
    QTest.mouseMove(board.viewport(), end)
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert line.end[0] == strap.item_id
    board.undo_stack.undo()
    assert line.end[0] == rim.item_id
    middle = line.handle_point()
    board._begin_line_handle("bend", QPointF(view_point(board, middle)))
    board._drag_line_handle(board._drag, QPointF(view_point(board, middle + QPointF(0, 80))))
    drag, board._drag = board._drag, None
    board._push_states(drag["before"], "bend line")
    assert line.bend.y() > 70 and abs(line.handle_point().y() - (middle.y() + 80)) < 2
    board.undo_stack.undo()
    assert line.bend.isNull()


def test_a_line_to_a_card_in_a_folded_column_ends_at_the_column(qapp, panel):
    board = panel.board
    column = make_column(qapp, board)
    hood = make_note(qapp, board, inside(column, 0.8), "Hood")
    away = make_note(qapp, board, QPointF(700, 0), "Strap")
    line, = join(board, away, hood)
    board.toggle_fold(column)
    assert line.isVisible() and abs(line.tips[1][0].x() - column.sceneBoundingRect().right()) < 20


def test_lines_are_saved_and_shared(qapp, panel, tmp_path):
    from core import artbook_share
    from ui.artbook_board import LineItem

    board = panel.board
    hood = make_note(qapp, board, QPointF(-300, 0), "Hood")
    rim = make_note(qapp, board, QPointF(200, 0), "Rim")
    line, = join(board, hood, rim)
    board.set_line_shape("straight")
    line.set_label("Leather")
    page = store.load_page(board.page_id)
    page["items"] = board.page_items()
    store.save_page_now(page)
    data = next(d for d in page["items"] if d["kind"] == "line")
    assert data["start"]["item"] == hood.item_id and data["shape"] == "straight" and data["label"] == "Leather"
    artbook_share.export_page(page["id"], tmp_path / "shared.artbook")
    shared = artbook_share.import_page(tmp_path / "shared.artbook")
    ids = {d["text"]: d["id"] for d in shared["items"] if d["kind"] == "note"}
    shared_line = next(d for d in shared["items"] if d["kind"] == "line")
    assert shared_line["start"]["item"] == ids["Hood"] and shared_line["end"]["item"] == ids["Rim"]
    board.load_page(shared)
    again, = [i for i in board.items_list() if isinstance(i, LineItem)]
    assert again.label() == "Leather" and again.isVisible()


def test_the_line_gestures_are_listed_with_the_shortcuts():
    from ui import shortcuts

    assert shortcuts.get("artbook.line").sequence is not None
    for gesture in ("line_dots", "line_edit", "line_label"):
        assert shortcuts.get(f"artbook.{gesture}").sequence is None


def test_a_card_in_a_column_keeps_its_dots_and_shows_the_columns_too(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    column = make_column(qapp, board)
    hood = make_note(qapp, board, inside(column, 0.8), "Hood")
    QTest.mouseMove(board.viewport(), view_point(board, hood.sceneBoundingRect().center()))
    assert board._dot_targets() == [hood, column]  # (its own dots, and its column's)
    for side, dot in board._line_dots(hood):  # heading for each of its dots (over the column): they stay
        QTest.mouseMove(board.viewport(), view_point(board, dot))
        assert board._dot_card is hood, side
        assert board._dot_at(QPointF(view_point(board, dot))) == (hood, side)
    column_dot = dict(board._line_dots(column))["right"]
    QTest.mouseMove(board.viewport(), view_point(board, column_dot))
    assert board._dot_at(QPointF(view_point(board, column_dot))) == (column, "right")


def test_a_group_has_dots_and_lines_join_it(qapp, panel):
    from PySide6.QtTest import QTest

    from ui.artbook_board import GroupTarget

    board = panel.board
    a, b = make_note(qapp, board, QPointF(-300, -60), "Glove"), make_note(qapp, board, QPointF(-300, 60), "Guard")
    away = make_note(qapp, board, QPointF(300, 0), "Arms")
    a.setSelected(True)
    b.setSelected(True)
    board.group_selection()
    board._scene.clearSelection()
    gid = a.group
    QTest.mouseMove(board.viewport(), view_point(board, a.sceneBoundingRect().center()))
    assert board._dot_targets() == [a, GroupTarget(gid)]  # (a card in a group: its dots, and the group's)
    group_dot = dict(board._line_dots(GroupTarget(gid)))["right"]
    QTest.mouseMove(board.viewport(), view_point(board, group_dot))
    assert board._dot_at(QPointF(view_point(board, group_dot))) == (GroupTarget(gid), "right")
    start, end = view_point(board, group_dot), view_point(board, away.sceneBoundingRect().center())
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(board.viewport(), end)
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, pos=end)
    line, = lines_on(board)
    assert line.start[0] == "group:" + gid and line.end[0] == away.item_id
    panel_box = board.group_backdrop_boxes()[gid]
    assert abs(line.tips[0][0].x() - panel_box.right()) < 10  # (it leaves the group's panel)
    data = next(d for d in board.page_items() if d["kind"] == "line")
    assert data["start"]["group"] == gid
    board.load_page({"id": board.page_id, "items": board.page_items(), "groups": board.page_groups()})
    again, = lines_on(board)
    assert again.start[0] == "group:" + gid and again.isVisible()
    for member in board.group_members(gid):  # (the whole group deleted: its lines go too)
        member.setSelected(True)
    board.delete_selection()
    assert lines_on(board) == []


def test_a_line_can_be_let_go_on_a_groups_panel(qapp, panel):
    from ui.artbook_board import GroupTarget

    board = panel.board
    a, b = make_note(qapp, board, QPointF(-300, -60), "Glove"), make_note(qapp, board, QPointF(-300, 60), "Guard")
    away = make_note(qapp, board, QPointF(300, 0), "Arms")
    a.setSelected(True)
    b.setSelected(True)
    board.group_selection()
    board._scene.clearSelection()
    margin = board.group_backdrop_boxes()[a.group].topLeft() + QPointF(2, 2)  # (the panel, past its members)
    assert board._line_target_at(QPointF(view_point(board, margin))) == GroupTarget(a.group)
    line, = join(board, away, GroupTarget(a.group))
    assert line.end[0] == "group:" + a.group and line.isVisible()


# ----------------------------------------------------------------------
# Batch F: tables (typed in place; text, number and status columns; a + to add rows and columns; a bar)
# ----------------------------------------------------------------------
def tables_on(board):
    from ui.artbook_board import TableItem

    return [item for item in board.items_list() if isinstance(item, TableItem)]


def make_table(qapp, board, at=QPointF(0, 0), rows=(("Block out the torso", "2 h"), ("Strap buckle", "1.5 h"))):
    """Shift+B's table, typed in: each row's Task and Time (Tab between them and on to the next row)."""
    from PySide6.QtTest import QTest

    board.setFocus()
    table = board.add_table(at)
    for r, cells in enumerate(rows):
        for c, text in enumerate(cells):
            QTest.keyClicks(board, text)
            if not (r == len(rows) - 1 and c == len(cells) - 1):
                QTest.keyClick(board, Qt.Key.Key_Tab)
    QTest.keyClick(board, Qt.Key.Key_Escape)
    board._scene.clearSelection()
    qapp.processEvents()
    return table


def cell_point(board, table, row, column) -> QPoint:
    return view_point(board, table.mapToScene(table.cell_rect(row, column).center()))


def test_the_table_gestures_are_listed_with_the_shortcuts():
    from ui import shortcuts

    assert shortcuts.get("artbook.table").sequence is not None
    assert shortcuts.get("artbook.table_next").sequence and shortcuts.get("artbook.table_prev").sequence
    for gesture in ("table_cell", "table_down", "table_status", "table_add", "table_row_drag", "table_column_width"):
        assert shortcuts.get(f"artbook.{gesture}").sequence is None


def test_shift_b_makes_a_table_typed_with_tab_and_enter(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    board.setFocus()
    QTest.mouseMove(board.viewport(), board.viewport().rect().center())
    QTest.keyClick(board, Qt.Key.Key_B, Qt.KeyboardModifier.ShiftModifier)
    table, = tables_on(board)
    assert [c[0] for c in table.columns] == ["Task", "Time", "Status"]
    assert [c[1] for c in table.columns] == ["text", "number", "status"] and len(table.rows) == 3
    assert board.editing_table() is table and table.editing_cell == (0, 0) and table.isSelected()
    QTest.keyClicks(board, "Hood")
    QTest.keyClick(board, Qt.Key.Key_Tab)
    assert table.editing_cell == (0, 1)
    QTest.keyClicks(board, "1 h")
    QTest.keyClick(board, Qt.Key.Key_Tab)  # (past the status column: it's picked, not typed)
    assert table.editing_cell == (1, 0)
    QTest.keyClicks(board, "Gloves")
    QTest.keyClick(board, Qt.Key.Key_Return)  # (the cell below)
    assert table.editing_cell == (2, 0)
    QTest.keyClick(board, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)  # (back)
    assert table.editing_cell == (1, 1)
    QTest.keyClick(board, Qt.Key.Key_Escape)
    assert board.editing_table() is None and table.editing_cell is None
    assert table.rows == [["Hood", "1 h", ""], ["Gloves", "", ""], ["", "", ""]]
    board.undo_stack.undo()  # (the typing: one step)
    assert table.rows == [["", "", ""]] * 3
    board.undo_stack.undo()  # (the table)
    assert tables_on(board) == []


def test_tab_past_the_last_cell_and_enter_under_the_last_row_add_rows(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    table = make_table(qapp, board, rows=(("a", "1"), ("b", "2"), ("c", "3")))
    assert len(table.rows) == 3
    board.edit_table_cell(table, (2, 1))
    QTest.keyClick(board, Qt.Key.Key_Tab)
    assert len(table.rows) == 4 and table.editing_cell == (3, 0)
    QTest.keyClick(board, Qt.Key.Key_Return)
    assert len(table.rows) == 5 and table.editing_cell == (4, 0)
    QTest.keyClick(board, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)  # (a new line in the cell, not a row)
    assert len(table.rows) == 5 and table.editing_cell == (4, 0)
    board.finish_renaming()
    board.undo_stack.undo()
    assert len(table.rows) == 3  # (the rows came with the typing: one step)


def test_typing_a_columns_name_in_the_header(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    table = make_table(qapp, board)
    board.edit_table_cell(table, (0, 0))
    QTest.keyClick(board, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)  # (from the first cell: the last column's name)
    assert table.editing_cell == (-1, 2)
    QTest.keyClicks(board, "State")
    QTest.keyClick(board, Qt.Key.Key_Escape)
    assert table.columns[2][0] == "State"
    board.edit_table_cell(table, (-1, 0))
    QTest.keyClick(board, Qt.Key.Key_Delete)
    board.finish_renaming()
    assert table.columns[0][0] == "Column 1"  # (a name can't be empty)


def test_number_columns_add_up_in_their_unit(qapp, panel):
    from ui.artbook_board import cell_number, format_total

    assert cell_number("1.5 h") == (1.5, "h") and cell_number("2,5h") == (2.5, "h") and cell_number("soon") is None
    assert format_total([(2.0, "h"), (1.5, "h"), (3.0, "")]) == "6.5 h" and format_total([(1.0, ""), (2.0, "")]) == "3"
    table = make_table(qapp, panel.board, rows=(("a", "2 h"), ("b", "1.5 h"), ("c", "tbc")))
    assert table.column_total(1) == "3.5 h" and table._total is not None
    panel.board.set_table_column_kind("text", table, 1)
    assert table._total is None  # (no number column: no total row)


def test_status_chips_are_picked_renamed_and_recoloured(qapp, panel):
    board = panel.board
    table = make_table(qapp, board)
    board.set_table_status(table, 0, 2, "Done")
    board.set_table_status(table, 1, 2, "In progress")
    assert [row[2] for row in table.rows] == ["Done", "In progress", ""]
    assert table.status_counts(2) == [("In progress", 1), ("Done", 1)]
    menu = board.status_menu(table, 0, 2)
    texts = [a.text() for a in menu.actions() if a.text()]
    assert texts == ["To do", "In progress", "Done", "Blocked", "No Status", "Edit Statuses..."]
    assert next(a for a in menu.actions() if a.text() == "Done").isChecked()
    next(a for a in menu.actions() if a.text() == "Blocked").trigger()
    assert table.rows[0][2] == "Blocked"
    assert board.add_table_status(table, "Review") and not board.add_table_status(table, " Review ")
    assert dict(table.statuses)["Review"] not in [c for n, c in table.statuses if n != "Review"]  # (a colour of its own)
    assert board.rename_table_status(table, "Blocked", "Waiting")
    assert table.rows[0][2] == "Waiting"  # (the cells follow)
    board.set_table_status_color(table, "Waiting", "#3b82f6")
    assert dict(table.statuses)["Waiting"] == "#3b82f6"
    board.delete_table_status(table, "Waiting")
    assert table.rows[0][2] == "" and "Waiting" not in dict(table.statuses)
    board.undo_stack.undo()
    assert table.rows[0][2] == "Waiting"
    menu.deleteLater()


def test_the_bar_adds_moves_and_deletes_rows_and_columns(qapp, panel):
    board = panel.board
    table = make_table(qapp, board, rows=(("a", "1"), ("b", "2"), ("c", "3")))
    table.setSelected(True)
    qapp.processEvents()
    board._place_note_bar()
    bar = board.table_bar
    assert bar.isVisible() and all(b.toolTip() for b in (bar.btn_row, bar.btn_column, bar.btn_kind, bar.btn_rows,
                                                            bar.btn_header, bar.btn_title, bar.btn_delete))
    bar.btn_row.click()
    assert len(table.rows) == 4 and table.rows[3] == ["", "", ""]
    bar.btn_column.click()
    assert len(table.columns) == 4 and all(len(row) == 4 for row in table.rows)
    board.move_table_row(table, 1, 0)
    assert [row[0] for row in table.rows[:3]] == ["b", "a", "c"]
    board.move_table_column(table, 0, 1)
    assert [c[0] for c in table.columns[:2]] == ["Time", "Task"] and table.rows[0][:2] == ["2", "b"]
    board.delete_table_row(table, 0)
    board.delete_table_column(table, 3)
    assert len(table.rows) == 3 and len(table.columns) == 3
    board.set_table_column_kind("status", table, 1)
    assert table.columns[1][1] == "status" and table.rows[0][1] == "a"  # (the cells keep their text)
    table.current = (0, 1)
    bar._fill_column_menu()
    assert [a.text() for a in bar.btn_kind.menu().actions() if a.isChecked()] == ["Status"]
    for _ in range(7):
        board.undo_stack.undo()
    assert [row[:2] for row in table.rows] == [["a", "1"], ["b", "2"], ["c", "3"]] and len(table.columns) == 3


def test_the_header_and_the_title_come_and_go(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    table = make_table(qapp, board)
    height = table.height
    board.toggle_table_header(table)
    assert not table.header and table.height < height
    board.toggle_table_header(table)
    board.toggle_table_title(table)  # (typed straight away)
    assert board.editing_table() is table and table.title() == "Untitled"
    QTest.keyClicks(board, "Mentorship schedule")
    QTest.keyClick(board, Qt.Key.Key_Return)
    assert table.title() == "Mentorship schedule" and table.title_item.isVisible() and table.height > height
    board.undo_stack.undo()
    assert table.title() == "" and not table.title_item.isVisible()


def test_a_click_on_a_selected_tables_cell_types_in_it(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    table = make_table(qapp, board, at=QPointF(0, 0))
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, pos=cell_point(board, table, 1, 0))
    assert table.isSelected() and board.editing_table() is None  # (the first click picks the table)
    assert table.current == (1, 0)
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, pos=cell_point(board, table, 1, 0))
    assert board.editing_table() is table and table.editing_cell == (1, 0)
    QTest.keyClicks(board, " (left)")
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, pos=cell_point(board, table, 0, 1))
    assert table.rows[1][0] == "Strap buckle (left)" and table.editing_cell == (0, 1)  # (on to the cell clicked)
    QTest.keyClick(board, Qt.Key.Key_Escape)
    board._scene.clearSelection()
    QTest.mouseDClick(board.viewport(), Qt.MouseButton.LeftButton, pos=cell_point(board, table, 0, 0))
    assert table.editing_cell == (0, 0)


def test_clicking_a_status_chip_opens_its_picker(qapp, panel, monkeypatch):
    from PySide6.QtTest import QTest

    board = panel.board
    table = make_table(qapp, board)
    asked = []
    monkeypatch.setattr(board, "ask_table_status", lambda t, r, c, at: asked.append((t, r, c)))
    chip = table.chip_rect(0, 2)
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, pos=view_point(board, table.mapToScene(chip.center())))
    assert asked == [(table, 0, 2)]


def test_hovering_a_table_shows_its_plus_bars_and_they_add(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    table = make_table(qapp, board)
    QTest.mouseMove(board.viewport(), QPoint(5, 5))  # (the first move only enters the view)
    assert not table.hovered
    QTest.mouseMove(board.viewport(), cell_point(board, table, 1, 0))
    assert table.hovered and table.hover_row == 1
    plus = view_point(board, table.mapToScene(table.plus_rect("row").center()))
    QTest.mouseMove(board.viewport(), plus)
    assert table.hovered and table.plus_hot == "row"  # (still on it, out past its edge)
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, pos=plus)
    assert len(table.rows) == 4
    column_plus = view_point(board, table.mapToScene(table.plus_rect("column").center()))
    QTest.mouseMove(board.viewport(), cell_point(board, table, 1, 2))
    QTest.mouseMove(board.viewport(), column_plus)
    QTest.mouseClick(board.viewport(), Qt.MouseButton.LeftButton, pos=column_plus)
    assert len(table.columns) == 4


def test_dragging_a_columns_edge_sizes_it(qapp, panel):
    board = panel.board
    table = make_table(qapp, board)
    width = table.columns[0][2]
    edge = table.mapToScene(QPointF(table.column_left(1), table.cell_rect(0, 0).center().y()))
    assert board._table_control_at(QPointF(view_point(board, edge)))[1:] == ("edge", 0)
    drag(qapp, board, edge, edge + QPointF(60 * table.scale(), 0))
    assert abs(table.columns[0][2] - (width + 60)) < 2 and table.edge_hot is None
    board.undo_stack.undo()
    assert table.columns[0][2] == width


def test_dragging_a_rows_dots_moves_it(qapp, panel):
    board = panel.board
    table = make_table(qapp, board, rows=(("a", "1"), ("b", "2"), ("c", "3")))
    grip = table.mapToScene(table.grip_rect(0).center())
    assert board._table_control_at(QPointF(view_point(board, grip)))[1:] == ("grip", 0)
    below = table.mapToScene(QPointF(table.width / 2, table.cell_rect(2, 0).bottom() - 2))
    drag(qapp, board, grip, below)
    assert [row[0] for row in table.rows] == ["b", "c", "a"] and table.drop_row is None


def test_a_table_goes_in_a_column_and_fits_its_width(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    column = make_column(qapp, board)
    board.setFocus()
    table = board.add_table(inside(column, 0.8))  # (made over a column: it goes in)
    board.finish_renaming()
    assert board.column_cards(column) == [table]
    assert abs(table.mapRectToScene(table.full_rect()).width() - column.inner_width() * column.scale()) < 1
    text_width, time_width = table.columns[0][2], table.columns[1][2]
    board.edit_table_cell(table, (0, 1))
    QTest.keyClicks(board, "12.5 hours")
    board.finish_renaming()
    assert table.columns[1][2] > time_width and table.columns[0][2] < text_width  # (room from the text)
    assert abs(table.mapRectToScene(table.full_rect()).width() - column.inner_width() * column.scale()) < 1


def test_a_table_is_saved_shared_copied_and_joined_by_lines(qapp, panel, tmp_path):
    from core import artbook_share
    from ui.artbook_board import TableItem

    board = panel.board
    table = make_table(qapp, board)
    board.set_table_status(table, 0, 2, "Done")
    board.toggle_table_title(table)
    board.finish_renaming()
    hood = make_note(qapp, board, QPointF(700, 0), "Hood")
    join(board, table, hood)
    page = store.load_page(board.page_id)
    page["items"] = board.page_items()
    store.save_page_now(page)
    data = next(d for d in page["items"] if d["kind"] == "table")
    assert data["title"] == "Untitled" and data["rows"][0] == ["Block out the torso", "2 h", "Done"]
    assert [c["kind"] for c in data["columns"]] == ["text", "number", "status"] and data["statuses"][2]["name"] == "Done"
    board.load_page(page)
    again, = tables_on(board)
    assert again.rows == table.rows and again.title() == "Untitled" and again.column_total(1) == "3.5 h"
    assert lines_on(board)[0].start[0] == again.item_id
    artbook_share.export_page(page["id"], tmp_path / "shared.artbook")
    shared = artbook_share.import_page(tmp_path / "shared.artbook")
    assert [d["kind"] for d in shared["items"]].count("table") == 1
    again.setSelected(True)
    assert board.copy_selection()
    assert QApplication.clipboard().text().splitlines()[1] == "Block out the torso\t2 h\tDone"
    junk = TableItem.from_dict({"id": "t", "kind": "table", "columns": [{"kind": "spreadsheet", "width": "wide"}, 5],
                                "rows": [["a", 2, "b", "c"], "x"], "statuses": [{"name": ""}, {"name": "Ok", "color": "nope"}]})
    assert [c[1] for c in junk.columns] == ["text", "text"] and junk.rows == [["a", ""]]
    assert junk.statuses == [("Ok", "#8b8b9e")]


def test_right_click_on_a_table(qapp, panel):
    board = panel.board
    table = make_table(qapp, board)
    menu = board.context_menu(table.mapToScene(table.cell_rect(1, 1).center()))
    assert table.current == (1, 1)
    sub = next(a for a in menu.actions() if a.text() == "Table").menu()
    assert [a.text() for a in sub.actions() if a.text()] == ["Column", "Row", "Header Row", "Title"]
    menu.deleteLater()


def test_a_locked_tables_cells_arent_typed_in(qapp, panel):
    board = panel.board
    table = make_table(qapp, board)
    hood = make_note(qapp, board, QPointF(700, 0), "Hood")
    table.setSelected(True)
    hood.setSelected(True)
    board.group_selection()
    board.toggle_group_lock()
    board.edit_table_cell(table, (0, 0))
    assert board.editing_table() is None
    board.add_table_row(table)
    assert len(table.rows) == 3


def test_an_exported_table_has_no_hover_or_outline_marks(qapp, panel):
    from PySide6.QtGui import QImage, QPainter

    board = panel.board
    table = make_table(qapp, board)
    box = table.mapRectToScene(table.boundingRect())

    def picture():
        image = QImage(int(box.width()) + 2, int(box.height()) + 2, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        board._scene.render(painter, QRectF(image.rect()), box)
        painter.end()
        return image

    plain = picture()
    table.setSelected(True)
    table.current = (1, 0)
    table.set_hover(True, "row", 1, 0)
    assert picture() == plain


def test_the_plus_bars_stay_while_the_mouse_crosses_the_gap_to_them(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    table = make_table(qapp, board)
    QTest.mouseMove(board.viewport(), QPoint(5, 5))
    for which, start in (("row", table.cell_rect(1, 0).center()), ("column", table.cell_rect(1, 2).center())):
        plus = table.plus_rect(which).center()
        start = QPointF(plus.x(), start.y()) if which == "row" else QPointF(start.x(), plus.y())
        for n in range(31):  # (a slow move out to it, a few pixels at a time)
            QTest.mouseMove(board.viewport(), view_point(board, table.mapToScene(start + (plus - start) * (n / 30))))
            assert table.hovered, (which, n)
        assert table.plus_hot == which


def test_a_column_aligns_left_center_or_right(qapp, panel):
    board = panel.board
    table = make_table(qapp, board)
    assert [table.align_of(c) for c in range(3)] == ["left", "right", "left"]  # (numbers right, the rest left)
    table.setSelected(True)
    table.current = (0, 0)
    qapp.processEvents()
    board._place_note_bar()
    bar = board.table_bar
    assert bar.align_buttons["left"].isChecked() and all(b.toolTip() for b in bar.align_buttons.values())
    bar.align_buttons["center"].click()
    assert table.align_of(0) == "center" and bar.align_buttons["center"].isChecked()
    board.set_table_column_align("left", table, 1)
    assert table.align_of(1) == "left"
    left, right = table.chip_rect(0, 2), None
    board.set_table_column_align("right", table, 2)
    right = table.chip_rect(0, 2)
    assert right.right() > left.right()  # (an empty status chip moves over too)
    board.edit_table_cell(table, (0, 0))
    assert table.editor.document().defaultTextOption().alignment() == Qt.AlignmentFlag.AlignHCenter
    board.finish_renaming()
    data = table.to_dict()
    assert [c["align"] for c in data["columns"]] == ["center", "left", "right"]
    from ui.artbook_board import TableItem

    assert [TableItem.from_dict(data).align_of(c) for c in range(3)] == ["center", "left", "right"]
    board.undo_stack.undo()
    assert table.align_of(2) == "left"


def test_edit_statuses_opens_them_all_in_a_dialog(qapp, panel, monkeypatch):
    from ui.artbook_table_bar import StatusDialog

    board = panel.board
    table = make_table(qapp, board, rows=(("a", "1"), ("b", "2"), ("c", "3")))
    for row, status in enumerate(("Done", "Blocked", "To do")):
        board.set_table_status(table, row, 2, status)
    menu = board.status_menu(table, 0, 2)
    assert [a.text() for a in menu.actions() if a.text()][-1] == "Edit Statuses..."
    menu.deleteLater()
    dialog = StatusDialog(table.statuses, (("Green", "#16a34a"), ("Blue", "#3b82f6"), ("Pink", "#ec4899")), board)
    assert [row["edit"].text() for row in dialog.rows] == ["To do", "In progress", "Done", "Blocked"]
    assert all(row["swatch"].toolTip() for row in dialog.rows) and dialog.btn_add.toolTip()
    dialog.rows[2]["edit"].setText("Finished")  # (rename Done)
    blue = next(a for a in dialog.rows[2]["swatch"].menu().actions() if a.text() == "Blue")
    blue.trigger()
    assert dialog.rows[2]["color"] == "#3b82f6"
    dialog.remove_row(dialog.rows[3])  # (remove Blocked)
    dialog.btn_add.click()
    dialog.rows[-1]["edit"].setText("Review")
    dialog.add_row()  # (a new one left without a name: not added)
    monkeypatch.setattr(StatusDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    entries = dialog.statuses()
    monkeypatch.setattr(StatusDialog, "statuses", lambda self: entries)  # (as this dialog was left)
    board.edit_table_statuses(table)
    assert [name for name, _color in table.statuses] == ["To do", "In progress", "Finished", "Review"]
    assert dict(table.statuses)["Finished"] == "#3b82f6"
    assert [row[2] for row in table.rows] == ["Finished", "", "To do"]
    board.undo_stack.undo()  # (all of it: one step)
    assert [row[2] for row in table.rows] == ["Done", "Blocked", "To do"] and "Blocked" in dict(table.statuses)
    dialog.deleteLater()


def test_the_bars_checked_buttons_use_the_text_colour_made_for_them():
    from ui import themes

    assert "color: $card_selected_text" in next(line for line in themes.APP_QSS_TEMPLATE.template.splitlines()
                                                 if line.startswith("#noteBar QToolButton:checked"))


# ----------------------------------------------------------------------
# Batch G: link, file and page cards (big or compact; the look last used is remembered)
# ----------------------------------------------------------------------
WEB_PAGE = (b'<html><head><title>Mage | ArtStation</title><meta property="og:title" content="Hooded mage &amp; cloth">'
            b'<meta content="Layering cloth, with fold studies." name="description">'
            b'<meta property="og:site_name" content="ArtStation"><meta property="og:image" content="/cover.png"></head></html>')


def png_bytes(width=64, height=40) -> bytes:
    import io

    data = io.BytesIO()
    Image.new("RGB", (width, height), (120, 80, 200)).save(data, "PNG")
    return data.getvalue()


def ico_bytes(color=(230, 40, 40)) -> bytes:
    """A site icon: an .ico holding a 16 and a 48 px picture."""
    import io

    data = io.BytesIO()
    Image.new("RGBA", (48, 48), color + (255,)).save(data, "ICO", sizes=[(16, 16), (48, 48)])
    return data.getvalue()


@pytest.fixture
def fake_web(monkeypatch):
    """No network: a made-up web page (with a preview picture) and an image at made-up addresses."""
    from core import web_images

    asked = []

    def download(url, referer="", **_options):
        asked.append(url)
        if url.endswith(".png"):
            return png_bytes(), "image/png"
        if url.endswith("/favicon.ico"):
            return ico_bytes(), "image/x-icon"
        if "broken" in url:
            raise web_images.WebImageError("The site answered 500.")
        return WEB_PAGE, "text/html"

    monkeypatch.setattr(web_images, "download", download)
    return asked


def cards_on(board):
    from ui.artbook_board import RefCardItem

    return [item for item in board.items_list() if isinstance(item, RefCardItem)]


def wait_for(qapp, condition, seconds=10.0):
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_the_card_gestures_are_listed_with_the_shortcuts():
    from ui import shortcuts

    for gesture in ("card_open", "card_buttons", "page_card_drag"):
        assert shortcuts.get(f"artbook.{gesture}").sequence is None


def test_a_web_pages_details_are_read_from_its_tags():
    from core.web_images import page_details

    details = page_details("https://www.artstation.com/a/1", WEB_PAGE.decode())
    assert details == {"title": "Hooded mage & cloth", "about": "Layering cloth, with fold studies.", "site": "ArtStation",
                       "picture": "https://www.artstation.com/cover.png", "icon": "https://www.artstation.com/favicon.ico"}
    bare = page_details("https://www.example.com/x", "<title> Just a   title </title>")
    assert bare["title"] == "Just a title" and bare["site"] == "example.com" and bare["picture"] == ""


def test_pasting_a_web_pages_address_makes_a_link_card(qapp, panel, fake_web, monkeypatch):
    from PySide6.QtCore import QMimeData

    board = panel.board
    mime = QMimeData()
    mime.setText("https://www.artstation.com/artwork/mage")
    assert board._take(mime, QPointF(0, 0))
    assert wait_for(qapp, lambda: len(cards_on(board)) == 1)
    card, = cards_on(board)
    assert card.kind == "link" and card.title() == "Hooded mage & cloth" and card.data["site"] == "ArtStation"
    assert card.data["about"] == "Layering cloth, with fold studies." and card.image is not None
    assert store.blob_path(board.page_id, card.data["picture"]).is_file()  # (its picture is kept in the page)
    opened = []
    monkeypatch.setattr("ui.artbook_board.QDesktopServices.openUrl", lambda url: opened.append(url.toString()))
    board.open_card(card)
    assert opened == ["https://www.artstation.com/artwork/mage"]
    mime = QMimeData()
    mime.setText("https://example.com/picture.png")  # (an image's address: still the image)
    board._take(mime, QPointF(400, 0))
    assert wait_for(qapp, lambda: len(board.items_list()) == 2)
    assert len(cards_on(board)) == 1
    mime = QMimeData()
    mime.setText("https://broken.example.com/page")  # (a page that can't be read: a card with its address)
    board._take(mime, QPointF(800, 0))
    assert wait_for(qapp, lambda: len(cards_on(board)) == 2)
    assert cards_on(board)[-1].title() == "page" and cards_on(board)[-1].data["site"] == "broken.example.com"


def test_a_file_that_isnt_a_picture_makes_a_file_card(qapp, panel, tmp_path, monkeypatch):
    from core import apps

    board = panel.board
    tool = tmp_path / "Mage_Head.ztl"
    tool.write_bytes(b"ZTL" * 400)
    assert board.add_files([tool], QPointF(0, 0)) == 1
    assert wait_for(qapp, lambda: len(cards_on(board)) == 1)
    card, = cards_on(board)
    assert card.kind == "file" and card.title() == "Mage_Head.ztl" and card.chips() == ["ZTL", "1 KB"]
    assert not card.missing and card.subtitle() == str(tmp_path)
    maya = apps.App("maya", "Maya", (), (), frozenset({".ztl"}))
    zbrush = apps.App("zbrush", "ZBrush", (), (), frozenset({".ztl"}))
    monkeypatch.setattr(apps, "apps_for", lambda ext: [zbrush, maya])
    launched = []
    monkeypatch.setattr(apps, "launch", lambda app, path: launched.append((app.id, Path(path).name)) or False)
    board._load_card(card)
    assert [a for a, _name in card.apps] == ["maya", "zbrush"]  # (Maya first: the main app)
    board.open_card(card)  # (a double-click: its main app)
    board.open_card(card, "zbrush")
    assert launched == [("maya", "Mage_Head.ztl"), ("zbrush", "Mage_Head.ztl")]
    card.set_hover(True)
    controls = [what for what, _box, _label in card.controls()]
    assert controls[0] == "app:maya" and controls[-1] == "more"  # (the main app filled; the next too if it fits)
    assert [label for _what, _box, label in card.controls()][0] == "Open in Maya"
    more = [e[0] for e in board.card_entries(card) if e is not None]
    assert "Open in Maya" in more and "Open in ZBrush" in more and "Show in the Library" in more
    tool.unlink()  # (moved away: the card says so, and offers Relink)
    board._load_card(card)
    assert card.missing and card.subtitle().startswith("File not found") and card.controls()[0][0] == "relink"
    moved = tmp_path / "moved" / "Mage_Head.ztl"
    moved.parent.mkdir()
    moved.write_bytes(b"ZTL" * 10)
    monkeypatch.setattr("ui.artbook_board.QFileDialog.getOpenFileName", lambda *a, **k: (str(moved), ""))
    board.relink_card(card)
    assert wait_for(qapp, lambda: card.data.get("path") == str(moved))
    assert not card.missing and card.data["size"] == 30
    board.undo_stack.undo()
    assert card.data["path"] == str(tool)


def test_a_cards_look_is_big_or_compact_and_new_ones_come_the_last_way(qapp, panel, tmp_path):
    from core.settings import get_artbook_card_style

    board = panel.board
    one, two = tmp_path / "a.ztl", tmp_path / "b.ztl"
    one.write_bytes(b"a")
    two.write_bytes(b"b")
    assert get_artbook_card_style("file") == "big"
    board.add_files([one], QPointF(0, 0))
    assert wait_for(qapp, lambda: len(cards_on(board)) == 1)
    card, = cards_on(board)
    assert card.style == "big"
    big = card.height
    card.setSelected(True)
    qapp.processEvents()
    board._place_note_bar()
    bar = board.card_bar
    assert bar.isVisible() and bar.btn_big.isChecked()
    assert all(b.toolTip() for b in (bar.btn_big, bar.btn_compact, bar.btn_open, bar.btn_more, bar.btn_delete))
    bar.btn_compact.click()
    assert card.style == "compact" and card.height < big and bar.btn_compact.isChecked()
    assert get_artbook_card_style("file") == "compact" and get_artbook_card_style("link") == "big"  # (per kind)
    board.add_files([two], QPointF(600, 0))
    assert wait_for(qapp, lambda: len(cards_on(board)) == 2)
    assert cards_on(board)[-1].style == "compact"  # (the last look used)
    board.undo_stack.undo()
    board.undo_stack.undo()
    assert card.style == "big"


def test_a_page_card_opens_its_page_and_back_returns(qapp, panel):
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QDropEvent

    board = panel.board
    first = panel.current_page_id()
    other = store.create_page("Mage: props")
    other["items"] = [{"id": "n", "kind": "note", "text": "Staff"}, {"id": "i", "kind": "image"}]
    store.save_page_now(other)
    assert board.add_page_card(first) is None  # (not this page)
    mime = QMimeData()
    from ui.artbook_board import PAGE_MIME

    mime.setData(PAGE_MIME, other["id"].encode())
    drop = QDropEvent(QPointF(board.viewport().rect().center()), Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    board.dropEvent(drop)  # (dragged from the Pages list)
    card, = cards_on(board)
    assert card.kind == "page_card" and card.title() == "Mage: props" and card.page_info == "1 image · 1 card"
    board.set_view(2.0, QPointF(123, 45))
    board.open_card(card)
    assert panel.current_page_id() == other["id"] and panel.btn_back.isVisible()
    assert panel.btn_back.text() == f"Back to {store.page_summary(first)['name']}"
    panel.go_back()
    assert panel.current_page_id() == first and not panel.btn_back.isVisible()
    assert abs(board.zoom() - 2.0) < 1e-6  # (where it was left)
    store.rename_page(other["id"], "Mage: staff")
    board.refresh_page_cards()
    card, = cards_on(board)
    assert card.title() == "Mage: staff"
    store.delete_page(other["id"])
    board.refresh_page_cards()
    assert card.missing and card.subtitle() == "This page is gone"
    entries = [e[0] for e in board.add_here_entries(QPointF(0, 0)) if e is not None]
    assert "Page Link" in entries


def test_a_page_dragged_from_the_pages_list_carries_its_id(qapp, panel):
    from ui.artbook_board import PAGE_MIME
    from ui.artbook_pages import KIND_ROLE

    tree = panel.pages.tree
    items = [tree.topLevelItem(n) for n in range(tree.topLevelItemCount())]
    page = next(item for item in items if item.data(0, KIND_ROLE)[0] == "page")
    mime = tree.mimeData([page])
    assert bytes(mime.data(PAGE_MIME)).decode() == page.data(0, KIND_ROLE)[1]


def test_cards_are_saved_shared_copied_and_go_in_columns(qapp, panel, tmp_path, fake_web):
    from PySide6.QtCore import QMimeData

    from core import artbook_share

    board = panel.board
    mime = QMimeData()
    mime.setText("https://www.artstation.com/artwork/mage")
    board._take(mime, QPointF(0, 0))
    assert wait_for(qapp, lambda: len(cards_on(board)) == 1)
    card, = cards_on(board)
    card.setSelected(True)
    assert board.copy_selection() and QApplication.clipboard().text() == "https://www.artstation.com/artwork/mage"
    board.set_card_style("compact", [card])
    page = store.load_page(board.page_id)
    page["items"] = board.page_items()
    store.save_page_now(page)
    data = next(d for d in page["items"] if d["kind"] == "link")
    assert data["style"] == "compact" and data["title"] == "Hooded mage & cloth" and data["picture"]
    board.load_page(page)
    again, = cards_on(board)
    assert again.style == "compact" and again.image is not None and again.data == card.data
    artbook_share.export_page(page["id"], tmp_path / "shared.artbook")
    shared = artbook_share.import_page(tmp_path / "shared.artbook")
    link = next(d for d in shared["items"] if d["kind"] == "link")
    assert store.blob_path(shared["id"], link["picture"]).is_file()  # (its picture came along)
    column = make_column(qapp, board, QPointF(900, 0))
    into(qapp, board, again, column)
    assert board.column_cards(column) == [again]
    assert abs(again.mapRectToScene(again.full_rect()).width() - column.inner_width() * column.scale()) < 1


def test_a_page_that_cant_be_read_is_named_from_its_address():
    from ui.artbook_board import link_name

    assert link_name("https://www.artstation.com/danieljakobi") == "danieljakobi"
    assert link_name("https://example.com/a/fold_studies/") == "fold studies"
    assert link_name("https://www.artstation.com") == "artstation.com"


def test_a_link_cards_picture_can_be_set_pasted_and_removed(qapp, panel, fake_web, tmp_path, monkeypatch):
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QImage

    board = panel.board
    mime = QMimeData()
    mime.setText("https://broken.example.com/danieljakobi")  # (a site that refuses the app: no preview)
    board._take(mime, QPointF(0, 0))
    assert wait_for(qapp, lambda: len(cards_on(board)) == 1)
    card, = cards_on(board)
    assert card.title() == "danieljakobi" and card.image is None
    labels = [e[0] for e in board.card_entries(card) if e is not None]
    assert {"Set Picture...", "Paste Picture", "Remove Picture"} <= set(labels)
    picture = tmp_path / "cover.png"
    Image.new("RGB", (300, 200), (200, 40, 40)).save(picture)
    monkeypatch.setattr("ui.artbook_board.QFileDialog.getOpenFileName", lambda *a, **k: (str(picture), ""))
    board.choose_card_picture(card)
    assert card.image is not None and store.blob_path(board.page_id, card.data["picture"]).is_file()
    red = card.data["picture"]
    pasted = QImage(120, 80, QImage.Format.Format_RGB32)
    pasted.fill(QColor("#2040c0"))
    QApplication.clipboard().setImage(pasted)
    board.paste_card_picture(card)  # (a copied picture, or a screenshot)
    assert card.data["picture"] != red and card.image.pixelColor(10, 10).blue() > 150
    board.undo_stack.undo()
    assert card.data["picture"] == red and card.image.pixelColor(10, 10).red() > 150  # (the red one shows again)
    board.set_card_picture(card, None)
    assert "picture" not in card.data and card.image is None
    board.undo_stack.undo()
    assert card.image is not None


# ----------------------------------------------------------------------
# Batch H: the page's Trash, comments (badges and pins), the Unsorted tray, the browser extension's cards
# ----------------------------------------------------------------------
def test_the_comment_keys_are_listed_with_the_shortcuts():
    from ui import shortcuts

    assert shortcuts.get("artbook.comment").sequence == "M"
    for gesture in ("comment_open", "comment_pin_drag"):
        assert shortcuts.get(f"artbook.{gesture}").sequence is None


def test_deleted_things_go_to_the_pages_trash_and_come_back(qapp, panel):
    board = panel.board
    hood = make_note(qapp, board, QPointF(0, 0), "Hood: keep the rim thin")
    arms = make_note(qapp, board, QPointF(400, 0), "Arms")
    join(board, hood, arms)
    where = hood.pos()
    hood.setSelected(True)
    board.delete_selection()
    entry, = board.trash_entries()
    assert entry["label"] == "Note: “Hood: keep the rim thin”"
    assert sorted(d["kind"] for d in entry["items"]) == ["line", "note"]  # (its line went with it)
    assert hood.item_id not in [i.item_id for i in board.items_list()]
    board.undo_stack.undo()  # (undo: back, and out of the Trash)
    assert board.trash_entries() == [] and len(lines_on(board)) == 1
    board.undo_stack.redo()
    assert board.restore_trash(entry["id"]) == 2
    back = board.item(hood.item_id)
    assert back is not None and back.pos() == where and back.toPlainText() == "Hood: keep the rim thin"
    assert len(lines_on(board)) == 1 and board.trash_entries() == []
    board.undo_stack.undo()  # (the restore undone: in the Trash again)
    assert len(board.trash_entries()) == 1


def test_the_trash_is_saved_for_thirty_days(qapp, panel):
    import time

    from ui.artbook_board import TRASH_DAYS, clean_trash

    board = panel.board
    note = make_note(qapp, board, QPointF(0, 0), "Belt")
    note.setSelected(True)
    board.delete_selection()
    panel.save_now()
    page = store.load_page(board.page_id)
    assert [d["text"] for d in page["trash"][0]["items"]] == ["Belt"]
    old = dict(page["trash"][0], time=time.time() - (TRASH_DAYS + 1) * 86400)
    assert clean_trash([old]) == []  # (older than 30 days: gone)
    page["trash"].append(dict(old, id="old"))
    board.load_page(page)
    assert len(board.trash_entries()) == 1
    board.empty_trash()
    assert board.trash_entries() == []


def test_comments_on_a_card_and_on_a_pin(qapp, panel):
    from PySide6.QtTest import QTest

    board = panel.board
    hood = make_note(qapp, board, QPointF(0, 0), "Hood")
    hood.setSelected(True)
    board.setFocus()
    QTest.keyClick(board, Qt.Key.Key_M)  # (M: on the selected card)
    thread = board.comment_thread
    assert thread.isVisible() and thread.edit.hasFocus()
    QTest.keyClicks(thread.edit, "Rim reads too thick")
    QTest.keyClick(thread.edit, Qt.Key.Key_Return)
    QTest.keyClicks(thread.edit, "Pull the hood back")
    QTest.keyClick(thread.edit, Qt.Key.Key_Return)
    on_card, = board.page_comments()
    assert on_card["item"] == hood.item_id and [n["text"] for n in on_card["notes"]] == ["Rim reads too thick", "Pull the hood back"]
    board.set_comment_done(on_card["id"], 0, True)
    assert board.page_comments()[0]["notes"][0]["done"]
    board.close_comments()
    badge = view_point(board, hood.mapToScene(hood.crop_rect().topRight()))
    assert board._comment_at(QPointF(badge))["id"] == on_card["id"]  # (its badge: a click opens it)
    board.open_comments(None, QPointF(500, 300))  # (a pin, anywhere)
    QTest.keyClicks(thread.edit, "Lantern goes here")
    QTest.keyClick(thread.edit, Qt.Key.Key_Return)
    pin = next(t for t in board.page_comments() if t["item"] is None)
    assert (pin["x"], pin["y"]) == (500, 300) and board._numbered(pin)["number"] == 1
    board.move_pin(pin["id"], QPointF(600, 320))
    assert (board.page_comments()[1]["x"], board.page_comments()[1]["y"]) == (600, 320)
    board.undo_stack.undo()
    assert board.page_comments()[1]["x"] == 500
    board.delete_comment(pin["id"], 0)  # (its last comment: the pin goes)
    assert [t["item"] for t in board.page_comments()] == [hood.item_id]


def test_comments_are_saved_follow_their_card_and_travel_when_shared(qapp, panel, tmp_path):
    from core import artbook_share

    board = panel.board
    hood = make_note(qapp, board, QPointF(0, 0), "Hood")
    board.open_comments(hood)
    board.post_comment(board._open_thread["id"], "Thinner rim")
    board.close_comments()
    hood.setSelected(True)
    board.delete_selection()
    assert board.comment_threads() == []  # (its card is in the Trash: its badge isn't shown)
    assert board.page_comments(saving=True)  # (but kept: it comes back with it)
    board.restore_trash(board.trash_entries()[0]["id"])
    assert len(board.comment_threads()) == 1
    panel.save_now()
    page = store.load_page(board.page_id)
    assert page["comments"][0]["notes"][0]["text"] == "Thinner rim"
    artbook_share.export_page(page["id"], tmp_path / "shared.artbook")
    shared = artbook_share.import_page(tmp_path / "shared.artbook")
    note = next(d for d in shared["items"] if d["kind"] == "note")
    assert shared["comments"][0]["item"] == note["id"] and "trash" not in shared  # (the Trash stays here)


def test_the_tray_shows_unsorted_and_the_pages_trash(qapp, panel, tmp_path):
    from PySide6.QtTest import QTest

    from core import artbook_unsorted
    from ui import shortcuts
    from ui.artbook_board import ImageItem, RefCardItem

    assert shortcuts.get("artbook.tray").sequence == "U" and shortcuts.get("artbook.unsorted_drag").sequence is None
    board = panel.board
    artbook_unsorted.add("image", {"name": "Hooded mage", "source": "artstation.com"}, (png_bytes(), ".png"))
    artbook_unsorted.add("link", {"url": "https://www.artstation.com/a", "title": "Cloth folds", "site": "ArtStation"},
                         (png_bytes(), ".png"))
    model = tmp_path / "Belt_buckle.ztl"
    model.write_bytes(b"ZTL")
    artbook_unsorted.add("file", {"path": str(model), "name": model.name, "source": "the asset grid"})
    board.unsorted_changed.emit()
    assert panel.btn_tray.text() == "3"
    board.setFocus()
    QTest.keyClick(board, Qt.Key.Key_U)
    tray = board.tray
    assert tray.isVisible() and panel.btn_tray.isChecked() and tray.btn_unsorted.text() == "Unsorted  3"
    assert all(b.toolTip() for b in (tray.btn_unsorted, tray.btn_trash, tray.btn_place, tray.btn_remove, tray.btn_restore,
                                     tray.btn_restore_all, tray.btn_empty))
    rows = [tray.list.item(n).text().split("\n")[0] for n in range(tray.list.count())]
    assert rows == ["Belt_buckle.ztl", "Cloth folds", "Hooded mage"]  # (newest first)
    tray.list.item(1).setSelected(True)
    mime = tray.list.mimeData([tray.list.item(1)])
    from ui.artbook_tray import UNSORTED_MIME

    from PySide6.QtGui import QDropEvent

    drop = QDropEvent(QPointF(board.viewport().rect().center()), Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    assert bytes(mime.data(UNSORTED_MIME)).decode() == artbook_unsorted.entries()[1]["id"]
    board.dropEvent(drop)  # (dragged onto the page: placed where it's let go, out of Unsorted)
    assert wait_for(qapp, lambda: len(cards_on(board)) == 1)
    assert cards_on(board)[0].title() == "Cloth folds" and cards_on(board)[0].image is not None
    assert artbook_unsorted.count() == 2 and panel.btn_tray.text() == "2" and tray.list.count() == 2
    tray.btn_place.click()  # (Place All)
    assert wait_for(qapp, lambda: len(board.items_list()) == 3)
    assert {type(i) for i in board.items_list()} == {ImageItem, RefCardItem} and artbook_unsorted.count() == 0
    assert panel.btn_tray.text() == ""
    hood = make_note(qapp, board, QPointF(-600, 0), "Hood")
    hood.setSelected(True)
    board.delete_selection()
    tray.show_tab("trash")
    assert tray.btn_trash.isChecked() and tray.list.item(0).text().startswith("Note: “Hood”\ndeleted today")
    assert tray.btn_restore_all.isVisible() and not tray.btn_place.isVisible()
    tray.list.item(0).setSelected(True)
    tray.btn_restore.click()
    assert board.item(hood.item_id) is not None and tray.list.count() == 0
    board.toggle_image_list()  # (the image list and the tray share the right side: one at a time)
    assert not tray.isVisible() and not panel.btn_tray.isChecked()


def test_send_to_artbook_unsorted_from_the_asset_grid(qapp, panel, tmp_path):
    from PySide6.QtWidgets import QMenu

    from core import artbook_unsorted
    from ui.asset_menu import ArtbookSender

    sender = ArtbookSender(panel, lambda text: None)
    model = tmp_path / "Mage_Bust.fbx"
    model.write_bytes(b"fbx")
    menu = QMenu()
    sender.add_send_menu(menu, [model])
    send = menu.actions()[0].menu()
    assert send.isEnabled() and [a.text() for a in send.actions() if a.text()][0] == "Unsorted (the Artbook's tray)"
    send.actions()[0].trigger()
    entry, = artbook_unsorted.entries()
    assert entry["kind"] == "file" and entry["path"] == str(model) and panel.btn_tray.text() == "1"
    menu.deleteLater()


# ----------------------------------------------------------------------
# A link card shows its site's icon (sent by the browser extension, or read from the page)
# ----------------------------------------------------------------------
def test_a_link_card_shows_its_sites_icon(qapp, panel, tmp_path, fake_web):
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionGraphicsItem

    from core import artbook_share, artbook_unsorted

    board = panel.board
    mime = QMimeData()
    mime.setText("https://www.artstation.com/artwork/mage")
    board._take(mime, QPointF(0, 0))
    assert wait_for(qapp, lambda: len(cards_on(board)) == 1)
    card, = cards_on(board)
    assert "https://www.artstation.com/favicon.ico" in fake_web  # (the page lists none: the usual place)
    assert card.has_favicon() and card.favicon.width() == 48  # (the .ico's biggest)
    assert store.blob_path(board.page_id, card.data["favicon"]).is_file()

    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" fill="#1e90ff"/></svg>'
    artbook_unsorted.add("link", {"url": "https://example.org/a", "title": "No preview"}, None, (svg, ".svg"))
    entry, = artbook_unsorted.entries()
    board.place_unsorted([entry["id"]], QPointF(900, 0))
    assert wait_for(qapp, lambda: len(cards_on(board)) == 2)
    other = next(c for c in cards_on(board) if c is not card)
    assert other.has_favicon() and other.favicon.width() == 128 and other.image is None  # (an SVG: drawn big, crisp)
    other.set_style("big")
    picture = QImage(int(other.width), int(other.height), QImage.Format.Format_ARGB32)
    picture.fill(0)
    painter = QPainter(picture)
    other.paint(painter, QStyleOptionGraphicsItem(), None)
    painter.end()

    def blue(x, y):
        color = QColor(picture.pixel(int(x), int(y)))
        return color.blue() > 200 and color.red() < 80

    middle = other.picture_rect().center()
    assert blue(middle.x(), middle.y())  # (no preview: its site's icon in the picture's place)
    left = other.text_rect().left()
    assert any(blue(left + dx, y) for dx in range(2, 14) for y in range(int(other.height) - 40, int(other.height) - 8))  # (by its site)

    page = store.load_page(board.page_id)
    page["items"] = board.page_items()
    store.save_page_now(page)
    board.load_page(page)
    assert all(c.has_favicon() for c in cards_on(board))  # (kept in the page)
    artbook_share.export_page(page["id"], tmp_path / "shared.artbook")
    shared = artbook_share.import_page(tmp_path / "shared.artbook")
    links = [d for d in shared["items"] if d["kind"] == "link"]
    assert len(links) == 2 and all(store.blob_path(shared["id"], d["favicon"]).is_file() for d in links)  # (shared with it)
