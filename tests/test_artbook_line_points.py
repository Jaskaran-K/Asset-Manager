"""Points on lines (like reroute points): double-click a line to put a point on it (the line becomes two meeting there),
drag the point to route them, join other lines to it, Delete joins the two back into one. Double-click a line's label
(or Enter on a selected line) types the label."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest

from core import artbook_store as store
from tests.test_artbook import pump  # noqa: F401  (shared helpers)
from tests.test_artbook_cards import make_note, no_motion, panel  # noqa: F401


def join(board, a, b, heads="end"):
    from ui.artbook_board import LineItem, _AddCommand

    line = LineItem(uuid.uuid4().hex, (a.item_id, 0.0, 0.0), (b.item_id, 0.0, 0.0), 1.0)
    line.heads = heads
    board.undo_stack.push(_AddCommand(board, [line], "line"))
    board.lay_out_lines()
    return line


def kinds(board, kind):
    return [i for i in board.items_list() if isinstance(i, kind)]


def two_cards(qapp, board):
    a = make_note(qapp, board, QPointF(0, 0), "Concept")
    b = make_note(qapp, board, QPointF(700, 0), "Sculpt")
    board._scene.clearSelection()
    return a, b


def on_screen(board, scene_point) -> QPoint:
    return board.mapFromScene(scene_point)


def test_double_clicking_a_line_puts_a_point_on_it_as_one_undo_step(qapp, panel):
    from ui.artbook_board import LineItem, PointItem

    board = panel.board
    a, b = two_cards(qapp, board)
    line = join(board, a, b)
    board.frame()
    qapp.processEvents()
    spot = line.path.pointAtPercent(0.3)
    QTest.mouseDClick(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, on_screen(board, spot))
    points = kinds(board, PointItem)
    lines = kinds(board, LineItem)
    assert len(points) == 1 and len(lines) == 2
    point = points[0]
    assert point.isSelected() and (point.scenePos() - spot).manhattanLength() < 4 / board.zoom()
    into = next(l for l in lines if l.end[0] == point.item_id)
    out = next(l for l in lines if l.start[0] == point.item_id)
    assert into is line and into.start[0] == a.item_id and into.heads == "none"  # (the arrow stays at the far end)
    assert out.end[0] == b.item_id and out.heads == "end"
    # the line goes through in one sweep: both parts meet the point level, at its ring
    r = point.radius()
    assert (into.tips[1][0] - (point.scenePos() - QPointF(r, 0))).manhattanLength() < 0.01
    assert (out.tips[0][0] - (point.scenePos() + QPointF(r, 0))).manhattanLength() < 0.01
    board.undo_stack.undo()
    assert not kinds(board, PointItem) and kinds(board, LineItem) == [line]
    assert line.end[0] == b.item_id and line.heads == "end"
    board.undo_stack.redo()
    assert len(kinds(board, PointItem)) == 1 and len(kinds(board, LineItem)) == 2


def test_hovering_a_line_shows_where_the_point_would_go(qapp, panel):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    board = panel.board
    a, b = two_cards(qapp, board)
    line = join(board, a, b)
    board.frame()
    qapp.processEvents()
    at = QPointF(on_screen(board, line.path.pointAtPercent(0.25)))
    QApplication.sendEvent(board.viewport(), QMouseEvent(QEvent.Type.MouseMove, at, QPointF(board.viewport().mapToGlobal(at)),
                                                         Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert board._line_spot is not None
    assert (board._line_spot[0] - line.path.pointAtPercent(0.25)).manhattanLength() < 3 / board.zoom()


def test_a_point_routes_its_lines_and_others_join_it(qapp, panel):
    from ui.artbook_board import LineItem, PointItem

    board = panel.board
    a, b = two_cards(qapp, board)
    line = join(board, a, b)
    point = board.add_line_point(line, line.path.pointAtPercent(0.5))
    was = [QPointF(l.tips[0][0]) for l in kinds(board, LineItem)]
    point.setPos(point.pos() + QPointF(0, -300))  # (up, round something in the way)
    board.lay_out_lines()
    assert [l.tips[0][0] for l in kinds(board, LineItem)] != was
    out = next(l for l in kinds(board, LineItem) if l.start[0] == point.item_id)
    assert out.path.pointAtPercent(0).y() < was[0].y()
    # another card's line joins the point; a click on the point is the point's, not a line's
    c = make_note(qapp, board, QPointF(350, 400), "Review")
    third = join(board, c, point, "none")
    assert third.isVisible() and third.end[0] == point.item_id
    board.frame()
    qapp.processEvents()
    assert board._image_at(QPointF(on_screen(board, point.scenePos()))) is point
    assert isinstance(board._line_target_at(QPointF(on_screen(board, point.scenePos()))), PointItem)


def test_deleting_a_point_joins_its_two_lines_back_into_one(qapp, panel):
    from ui.artbook_board import LineItem, PointItem

    board = panel.board
    a, b = two_cards(qapp, board)
    line = join(board, a, b, "both")
    line.set_label("approved")
    board.lay_out_lines()
    first = board.add_line_point(line, line.path.pointAtPercent(0.3))
    middle = next(l for l in kinds(board, LineItem) if l.start[0] == first.item_id and l.end[0] == b.item_id)
    second = board.add_line_point(middle, middle.path.pointAtPercent(0.5))
    assert len(kinds(board, LineItem)) == 3 and len(kinds(board, PointItem)) == 2
    first.setSelected(True)
    second.setSelected(True)
    board.delete_selection()
    lines = kinds(board, LineItem)
    assert not kinds(board, PointItem) and len(lines) == 1
    joined = lines[0]
    assert {joined.start[0], joined.end[0]} == {a.item_id, b.item_id} and joined.heads == "both" and joined.label() == "approved"
    board.undo_stack.undo()  # (one step: both points and their lines back)
    assert len(kinds(board, LineItem)) == 3 and len(kinds(board, PointItem)) == 2


def test_a_point_with_more_lines_goes_with_them(qapp, panel):
    from ui.artbook_board import LineItem, PointItem

    board = panel.board
    a, b = two_cards(qapp, board)
    c = make_note(qapp, board, QPointF(350, 400), "Review")
    point = board.add_line_point(join(board, a, b), QPointF(350, 0))
    join(board, c, point, "none")
    board._scene.clearSelection()
    point.setSelected(True)
    board.delete_selection()
    assert not kinds(board, PointItem) and not kinds(board, LineItem)


def test_points_are_saved_and_copied_with_their_lines(qapp, panel):
    from ui.artbook_board import LineItem, PointItem

    board = panel.board
    a, b = two_cards(qapp, board)
    point = board.add_line_point(join(board, a, b), QPointF(350, 0))
    point.setPos(point.pos() + QPointF(0, -120))
    board.lay_out_lines()
    panel.save_now()
    page = store.load_page(panel.page["id"])
    saved = [d for d in page["items"] if d.get("kind") == "point"]
    assert len(saved) == 1 and abs(saved[0]["y"] - point.pos().y()) < 0.01
    panel.open_page(panel.page["id"])  # (loaded again)
    qapp.processEvents()
    board = panel.board
    assert len(kinds(board, PointItem)) == 1 and all(l.isVisible() for l in kinds(board, LineItem))
    for item in board.items_list():
        item.setSelected(not isinstance(item, LineItem) and item.item_id in (a.item_id, b.item_id) or isinstance(item, PointItem))
    board.duplicate_selection()
    assert len(kinds(board, PointItem)) == 2 and len(kinds(board, LineItem)) == 4
    assert all(l.isVisible() for l in kinds(board, LineItem))


def test_the_label_is_typed_by_double_clicking_it_or_enter(qapp, panel):
    from ui import shortcuts
    from ui.artbook_board import PointItem

    board = panel.board
    a, b = two_cards(qapp, board)
    line = join(board, a, b)
    line.setSelected(True)
    board.setFocus()
    QTest.keySequence(board, shortcuts.sequence("artbook.crop_apply"))  # (a selected line: Enter types its label)
    assert board._renaming and board._renaming["item"] is line
    board._renaming["text"].setPlainText("approved")
    board.finish_renaming()
    assert line.label() == "approved"
    board.frame()
    qapp.processEvents()
    QTest.mouseDClick(board.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                      on_screen(board, line.pill_rect().center()))
    assert board._renaming and board._renaming["item"] is line and not kinds(board, PointItem)


def test_the_line_bars_label_button_types_the_label_of_a_line_at_a_point(qapp, panel):
    from ui.artbook_board import LineItem

    board = panel.board
    a, b = two_cards(qapp, board)
    point = board.add_line_point(join(board, a, b), QPointF(350, 0))
    line = next(l for l in kinds(board, LineItem) if l.start[0] == point.item_id)
    board._scene.clearSelection()
    line.setSelected(True)
    qapp.processEvents()
    board.line_bar.btn_label.click()  # (its clicked signal carries a checked flag: not taken for the line)
    assert board._renaming and board._renaming["item"] is line
