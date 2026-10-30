"""Phase 4, layout: auto-arrange, align, distribute and same size (ui.artbook_layout and the board)."""

from __future__ import annotations

import time
from itertools import combinations

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent

from ui import artbook_layout as layout
from ui.animations import set_motion_mode


def pump(qapp, condition=lambda: False, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def moved(boxes, moves):
    return {item_id: box.translated(moves[item_id]) for item_id, box in boxes}


# ----------------------------------------------------------------------
# The maths
# ----------------------------------------------------------------------
def test_reading_order_is_rows_then_left_to_right():
    boxes = [
        ("b", QRectF(300, 12, 100, 100)),  # a little lower than "a", same row
        ("a", QRectF(0, 0, 100, 100)),
        ("d", QRectF(150, 200, 100, 100)),
        ("c", QRectF(0, 210, 100, 100)),
    ]
    assert layout.reading_order(boxes) == ["a", "b", "c", "d"]


def test_arrange_packs_rows_without_overlaps_in_order():
    sizes = [(400, 300), (200, 300), (300, 200), (500, 250), (250, 400), (300, 300), (150, 150)]
    boxes = [(n, QRectF(n * 1000.0, (n % 2) * 37.0, w, h)) for n, (w, h) in enumerate(sizes)]  # one long, uneven row
    result = moved(boxes, layout.arrange(boxes, gap=10))
    for (a, box_a), (b, box_b) in combinations(result.items(), 2):
        assert not box_a.adjusted(1, 1, -1, -1).intersects(box_b), (a, b)  # gaps, no overlaps
    assert min(box.left() for box in result.values()) == 0  # starts where they were
    assert min(box.top() for box in result.values()) == 0
    rows = sorted({box.top() for box in result.values()})
    assert 2 <= len(rows) < len(sizes)  # several images per row, several rows
    assert layout.reading_order(list(result.items())) == list(range(len(sizes)))  # order kept
    sizes_after = [(box.width(), box.height()) for box in result.values()]
    assert sizes_after == [(float(w), float(h)) for w, h in sizes]  # sizes kept


@pytest.mark.parametrize("edge, check", [
    ("left", lambda box: box.left()), ("right", lambda box: box.right()), ("center", lambda box: box.center().x()),
    ("top", lambda box: box.top()), ("bottom", lambda box: box.bottom()), ("middle", lambda box: box.center().y()),
])
def test_align_lines_up_one_edge(edge, check):
    boxes = [("a", QRectF(0, 0, 100, 50)), ("b", QRectF(300, 120, 60, 200)), ("c", QRectF(150, 400, 80, 80))]
    result = moved(boxes, layout.align(boxes, edge))
    assert len({round(check(box), 6) for box in result.values()}) == 1
    horizontal = edge in ("left", "right", "center")
    for item_id, box in boxes:  # only that direction moves
        after = result[item_id]
        assert (after.top() == box.top()) if horizontal else (after.left() == box.left())


def test_distribute_makes_equal_gaps_and_keeps_the_ends():
    boxes = [("a", QRectF(0, 0, 100, 50)), ("b", QRectF(130, 0, 50, 50)), ("c", QRectF(160, 0, 40, 50)),
             ("d", QRectF(500, 0, 100, 50))]
    result = moved(boxes, layout.distribute(boxes, horizontal=True))
    ordered = sorted(result.values(), key=lambda box: box.left())
    gaps = [round(b.left() - a.right(), 6) for a, b in zip(ordered, ordered[1:])]
    assert len(set(gaps)) == 1 and gaps[0] > 0
    assert ordered[0].left() == 0 and ordered[-1].right() == 600


def test_same_size_is_the_middle_size():
    assert layout.same_size([("a", 100.0), ("b", 300.0), ("c", 200.0)]) == 200.0


# ----------------------------------------------------------------------
# On the board
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def no_motion():
    set_motion_mode("off")
    yield
    set_motion_mode("on")


@pytest.fixture
def board(qapp, tmp_path):
    from ui.artbook import ArtbookPanel

    paths = []
    for n, (w, h) in enumerate([(400, 200), (100, 300), (250, 250), (300, 120)]):
        path = tmp_path / f"img{n}.png"
        Image.new("RGB", (w, h), "gray").save(path)
        paths.append(path)
    panel = ArtbookPanel()
    panel.resize(900, 700)
    panel.show()
    qapp.processEvents()
    panel.board.add_files(paths)
    assert pump(qapp, lambda: len(panel.board.items_list()) == len(paths))
    yield panel.board
    panel.close()


def key(board, key_code, text=""):
    board.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key_code, Qt.KeyboardModifier.NoModifier, text))


def test_a_arranges_everything_in_one_undo_step(qapp, board):
    items = board.items_list()
    for n, item in enumerate(items):
        item.setPos(n * 900.0, n * 37.0)  # scattered
    before = {item.item_id: item.pos() for item in items}
    steps = board.undo_stack.count()
    key(board, Qt.Key.Key_A, "a")
    assert board.undo_stack.count() == steps + 1
    boxes = [item.sceneBoundingRect() for item in items]
    for a, b in combinations(boxes, 2):
        assert not a.adjusted(1, 1, -1, -1).intersects(b)
    assert any(item.pos() != before[item.item_id] for item in items)
    board.undo_stack.undo()
    assert {item.item_id: item.pos() for item in items} == before


def test_align_distribute_and_same_size_work_on_the_selection(qapp, board):
    items = board.items_list()
    for n, item in enumerate(items):
        item.setPos(n * 500.0, n * 100.0)
        item.setSelected(n < 3)
    board.align("top")
    tops = {round(item.sceneBoundingRect().top(), 6) for item in items[:3]}
    assert len(tops) == 1
    assert items[3].pos() == QPointF(1500, 300)  # not selected: untouched

    board.same_size(True)
    heights = {round(item.sceneBoundingRect().height(), 6) for item in items[:3]}
    assert heights == {250.0}  # the middle of 200, 300 and 250

    board.distribute(True)
    ordered = sorted((item.sceneBoundingRect() for item in items[:3]), key=lambda box: box.left())
    gaps = {round(b.left() - a.right(), 4) for a, b in zip(ordered, ordered[1:])}
    assert len(gaps) == 1


def test_arrange_menu_greys_out_what_needs_more_images(qapp, board):
    def arrange_menu():
        menu = board.context_menu(QPointF(0, 0))
        images = next(a.menu() for a in menu.actions() if a.text() == "Images")
        state = {}
        for group in ("Arrange", "Align", "Match Size"):
            submenu = next(a.menu() for a in images.actions() if a.text() == group)
            state.update({a.text(): a.isEnabled() for a in submenu.actions() if a.text()})
        menu.deleteLater()
        return state

    items = board.items_list()
    board._scene.clearSelection()
    state = arrange_menu()
    assert state["Auto-Arrange"] and not state["Left"] and not state["Distribute Horizontally"]
    items[0].setSelected(True)
    items[1].setSelected(True)
    state = arrange_menu()
    assert state["Left"] and state["Same Height"] and not state["Distribute Horizontally"]
    items[2].setSelected(True)
    assert arrange_menu()["Distribute Horizontally"]
