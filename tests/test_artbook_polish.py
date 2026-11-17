"""Board cards Batch J (polish): guides and equal gaps while dragging, a colour for every card, a quick page switcher,
double-click to add, duplicate / copy / move cards (to this page or another), arrow keys, headers for older pages."""

from __future__ import annotations

import json

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleOptionGraphicsItem

from core import artbook_store as store
from tests.test_artbook import pump  # noqa: F401  (shared helpers)
from tests.test_artbook_cards import cards_on, make_column, make_note, make_table, make_todo, no_motion, panel, png_bytes, wait_for  # noqa: F401


def items_of(board, kind):
    return [i for i in board.items_list() if isinstance(i, kind)]


# ----------------------------------------------------------------------
# Duplicate, copy and paste, move and copy to another page
# ----------------------------------------------------------------------
def test_duplicate_copies_the_cards_and_the_lines_between_them(qapp, panel):
    from ui.artbook_board import LineItem, NoteItem

    board = panel.board
    hood = make_note(qapp, board, QPointF(0, 0), "Hood")
    straps = make_note(qapp, board, QPointF(400, 0), "Straps")
    import uuid

    from ui.artbook_board import _AddCommand

    board.undo_stack.push(_AddCommand(board, [LineItem(uuid.uuid4().hex, (hood.item_id, 0.0, 0.0), (straps.item_id, 0.0, 0.0), 1.0)], "line"))
    board.lay_out_lines()
    line = items_of(board, LineItem)[0]
    hood.setSelected(True)
    straps.setSelected(True)
    board.setFocus()
    QTest.keySequence(board, "Ctrl+D")
    notes = items_of(board, NoteItem)
    lines = items_of(board, LineItem)
    assert len(notes) == 4 and len(lines) == 2
    copies = [n for n in notes if n not in (hood, straps)]
    assert sorted(n.toPlainText() for n in copies) == ["Hood", "Straps"] and all(n.isSelected() for n in copies)
    copy_of_hood = next(n for n in copies if n.toPlainText() == "Hood")
    step = 24 / board.zoom()
    assert abs(copy_of_hood.pos().x() - hood.pos().x() - step) < 0.01
    new_line = next(l for l in lines if l is not line)
    assert new_line.attached() == {c.item_id for c in copies}  # (joins the copies)
    board.undo_stack.undo()
    assert len(items_of(board, NoteItem)) == 2 and len(items_of(board, LineItem)) == 1


def test_copied_cards_paste_on_another_page_with_their_pictures(qapp, panel, tmp_path):
    from PIL import Image

    from ui.artbook_board import ImageItem, PACKAGE_MIME, RefCardItem

    board = panel.board
    first = panel.page["id"]
    picture = tmp_path / "ref.png"
    Image.new("RGB", (60, 40), "red").save(picture)
    board.add_image(QImage(str(picture)))
    board.add_link_entries([({"url": "https://example.org/a", "title": "Strap study"}, png_bytes(), png_bytes(16, 16))])
    assert wait_for(qapp, lambda: items_of(board, ImageItem) and cards_on(board))
    image, card = items_of(board, ImageItem)[0], cards_on(board)[0]
    board.open_comments(card)
    board.post_comment(board._open_thread["id"], "Use this stitching")
    board.close_comments()
    image.setSelected(True)
    card.setSelected(True)
    assert board.copy_selection()
    mime = QApplication.clipboard().mimeData()
    assert mime.hasFormat(PACKAGE_MIME) and mime.text() == "https://example.org/a"  # (and the address, for other apps)
    package = json.loads(bytes(mime.data(PACKAGE_MIME)).decode())
    assert package["page"] == first and len(package["items"]) == 2 and len(package["comments"]) == 1
    second = panel.new_page(name="Other")
    assert board.can_paste() and board.paste(QPointF(500, 500))
    pasted_image, pasted_card = items_of(board, ImageItem)[0], cards_on(board)[0]
    assert pasted_image.item_id != image.item_id and pasted_card.data["title"] == "Strap study"
    assert store.blob_path(second, pasted_image.source["blob"]).is_file()  # (its picture copied into this page)
    assert store.blob_path(second, pasted_card.data["picture"]).is_file() and store.blob_path(second, pasted_card.data["favicon"]).is_file()
    assert [t["item"] for t in board.page_comments()] == [pasted_card.item_id]
    assert wait_for(qapp, lambda: pasted_image.pixmap is not None)


def test_move_and_copy_to_another_page_and_undo(qapp, panel):
    from ui.artbook_board import NoteItem

    board = panel.board
    other = panel.new_page(name="Armor references")
    make_note(qapp, board, QPointF(0, 0), "Already here")
    panel.save_now()
    here = panel.new_page(name="Mage costume")
    hood = make_note(qapp, board, QPointF(0, 300), "Hood")
    straps = make_note(qapp, board, QPointF(400, 300), "Straps")
    assert "Armor references" in [e[0] for e in board.send_menu_entries(True) if isinstance(e, tuple)]
    assert "Mage costume" not in [e[0] for e in board.send_menu_entries(True) if isinstance(e, tuple)]  # (not this page)
    hood.setSelected(True)
    assert board.send_selection(other, move=True) == 1
    assert board.item(hood.item_id) is None and board.toast.isVisible()
    texts = [d.get("text") for d in store.load_page(other)["items"] if d.get("kind") == "note" and d.get("style") != "title"]
    assert sorted(texts) == ["Already here", "Hood"]
    board.undo_stack.undo()
    assert board.item(hood.item_id) is not None
    assert [d.get("text") for d in store.load_page(other)["items"] if d.get("kind") == "note" and d.get("style") != "title"] == ["Already here"]
    board._scene.clearSelection()
    straps.setSelected(True)
    board.send_selection(other, move=False)
    assert board.item(straps.item_id) is not None  # (copied: still here too)
    panel.open_page(other)
    notes = sorted(n.toPlainText() for n in items_of(board, NoteItem) if n.style != "title")
    assert notes == ["Already here", "Straps"]
    assert panel.page["id"] == other and here != other


# ----------------------------------------------------------------------
# Arrow keys
# ----------------------------------------------------------------------
def test_arrow_keys_nudge_the_selection_as_one_undo_step(qapp, panel):
    board = panel.board
    note = make_note(qapp, board, QPointF(0, 0), "Hood")
    start = QPointF(note.pos())
    note.setSelected(True)
    board.setFocus()
    QTest.keyClick(board, Qt.Key.Key_Right)
    QTest.keyClick(board, Qt.Key.Key_Right)
    QTest.keyClick(board, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    zoom = board.zoom()
    assert abs(note.pos().x() - start.x() - 2 / zoom) < 0.01 and abs(note.pos().y() - start.y() - 10 / zoom) < 0.01
    board.undo_stack.undo()
    assert note.pos() == start  # (the nudges in a row: one step)
    board._scene.clearSelection()
    centre = board.view_center()
    QTest.keyClick(board, Qt.Key.Key_Left)  # (nothing selected: about the board)
    assert board.view_center().x() < centre.x()


# ----------------------------------------------------------------------
# A card's colour
# ----------------------------------------------------------------------
def test_every_card_takes_a_colour_shown_as_a_strip(qapp, panel):
    from ui.artbook_board import STRIP_COLORS, STRIP_WIDTH

    board = panel.board
    panel.new_page(name="Mage costume")
    note = make_note(qapp, board, QPointF(0, 0), "Hood")
    table = make_table(qapp, board, QPointF(0, 400))
    column = make_column(qapp, board, QPointF(900, 0))
    plain = make_note(qapp, board, QPointF(600, 600), "words")
    plain.style = "plain"
    title = board.title_note()
    red = STRIP_COLORS[0][1]
    for item in (note, table, column, plain, title):
        item.setSelected(True)
    menu = board.context_menu(QPointF(0, 0))
    colour = next(a.menu() for a in menu.actions() if a.text() == "Card Colour")
    from ui.artbook_board import ColourSwatch

    swatches = colour.actions()[0].defaultWidget().findChildren(ColourSwatch)
    assert len(swatches) == len(STRIP_COLORS) + 1 and all(s.toolTip() for s in swatches)
    swatches[1].click()  # (red)
    menu.deleteLater()
    assert (note.strip, table.strip, column.strip, title.strip, plain.strip) == (red, red, red, red, None)
    panel.save_now()
    saved = {d["id"]: d.get("strip") for d in store.load_page(panel.page["id"])["items"]}
    assert saved[note.item_id] == red and saved[table.item_id] == red and saved[column.item_id] == red
    board.load_page(store.load_page(panel.page["id"]))
    again = board.item(note.item_id)
    assert again.strip == red
    picture = QImage(int(again.boundingRect().width()), int(again.boundingRect().height()), QImage.Format.Format_ARGB32)
    picture.fill(0)
    painter = QPainter(picture)
    again.paint(painter, QStyleOptionGraphicsItem(), None)
    painter.end()
    assert QColor(picture.pixel(round(STRIP_WIDTH / 2), picture.height() // 2)).name() == red  # (the strip down its edge)
    again.setSelected(True)
    board.set_colour(None)
    assert again.strip is None
    board.undo_stack.undo()
    assert again.strip == red


# ----------------------------------------------------------------------
# Guides while dragging
# ----------------------------------------------------------------------
def drag_by(qapp, board, item, dx, dy, alt=False):
    board.toast.hide()
    start = board.mapFromScene(item.sceneBoundingRect().center())
    assert board.viewport().rect().contains(start)
    end = start + QPoint(dx, dy)
    mods = Qt.KeyboardModifier.AltModifier if alt else Qt.KeyboardModifier.NoModifier
    QTest.mouseMove(board.viewport(), start + QPoint(30, 30))
    QTest.mouseMove(board.viewport(), start)
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, mods, start)
    for n in range(1, 5):
        point = start + (end - start) * (n / 4)
        if alt:  # (QTest's moves can't hold Alt)
            from PySide6.QtCore import QEvent
            from PySide6.QtGui import QMouseEvent

            move = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(board.viewport().mapToGlobal(point)),
                               Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, mods)
            QApplication.sendEvent(board.viewport(), move)
        else:
            QTest.mouseMove(board.viewport(), point)
    guides = board._guides
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, mods, end)
    qapp.processEvents()
    return guides


def test_a_dragged_card_snaps_into_line_and_to_equal_gaps(qapp, panel):
    board = panel.board
    board.set_view(1.0, QPointF(300, 200))
    hood = make_note(qapp, board, QPointF(0, 0), "Hood")
    straps = make_note(qapp, board, QPointF(0, 300), "Straps")
    hood.setPos(0, 0)
    straps.setPos(3, 300)  # (3 off its left edge)
    board._scene.clearSelection()
    guides = drag_by(qapp, board, straps, 0, 20)
    assert straps.pos().x() == hood.pos().x()  # (snapped into line)
    assert guides and guides["lines"]
    assert board._guides is None  # (let go: the lines go)
    straps.setPos(3, 300)
    drag_by(qapp, board, straps, 0, 20, alt=True)
    assert abs(straps.pos().x() - 3) < 0.01  # (Alt: no snapping)
    # equal gaps: a third note between two others snaps to the middle
    left = make_note(qapp, board, QPointF(0, 0), "L")
    right = make_note(qapp, board, QPointF(0, 0), "R")
    middle = make_note(qapp, board, QPointF(0, 0), "M")
    for note, x in ((left, 0), (right, 800), (middle, 397)):
        note.setPos(x, 900)
        note.setTextWidth(200)
    hood.setPos(-2000, 0)
    straps.setPos(-2000, 400)
    board._scene.clearSelection()
    board.set_view(1.0, QPointF(500, 950))
    guides = drag_by(qapp, board, middle, 0, 20)
    gap_left = middle.sceneBoundingRect().left() - left.sceneBoundingRect().right()
    gap_right = right.sceneBoundingRect().left() - middle.sceneBoundingRect().right()
    assert abs(gap_left - gap_right) < 0.6 and guides["gaps"]


# ----------------------------------------------------------------------
# Double-click empty space
# ----------------------------------------------------------------------
def test_double_clicking_empty_space_offers_what_to_put_there(qapp, panel, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from ui.artbook_board import NoteItem

    board = panel.board
    spot = QPoint(300, 250)
    QTest.mouseMove(board.viewport(), spot + QPoint(20, 20))
    QTest.mouseMove(board.viewport(), spot)
    QTest.mouseDClick(board.viewport(), Qt.MouseButton.LeftButton, pos=spot)
    picker = board._quick_add
    assert picker is not None and picker.isVisible() and all(b.toolTip() for b in picker.buttons.values())
    assert list(picker.buttons) == ["note", "heading", "todo", "column", "table", "link", "pictures"]
    picker.buttons["note"].click()
    note = board.editing_note()
    assert isinstance(note, NoteItem) and not picker.isVisible()
    assert note.sceneBoundingRect().contains(board.mapToScene(spot))  # (where it was double-clicked)
    board.finish_note_edit()
    QTest.mouseDClick(board.viewport(), Qt.MouseButton.LeftButton, pos=spot)
    assert picker.isVisible()
    QTest.keyClick(board, Qt.Key.Key_Escape)
    assert not picker.isVisible()
    asked = []
    monkeypatch.setattr(board, "add_web_link", lambda url, at=None: asked.append(url))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("example.org/strap", True))
    picker.show_at(board.mapToScene(spot), spot)
    picker.buttons["link"].click()
    assert asked == ["https://example.org/strap"]


# ----------------------------------------------------------------------
# The page switcher
# ----------------------------------------------------------------------
def test_page_names_match_best_first():
    from ui.artbook_switcher import match, ranked

    assert match("Armor references", "ar") == (0, [0, 1])
    assert match("Hunter: leather armor", "ar")[0] == 1  # (a word starts with it)
    assert match("Character turnarounds", "ar")[0] == 2
    assert match("Mage costume", "mcs") == (3, [0, 5, 7])
    assert match("Mage costume", "zz") is None
    pages = [{"id": "a", "name": "Character turnarounds"}, {"id": "b", "name": "Armor references"},
             {"id": "c", "name": "Hunter: leather armor"}, {"id": "d", "name": "Mage costume"}]
    assert [p["id"] for p, _ in ranked(pages, "ar", [], None)] == ["b", "c", "a"]
    assert [p["id"] for p, _ in ranked(pages, "", ["d", "a"], "d")] == ["a", "b", "c"]  # (recent first; not this one)


def test_ctrl_p_goes_to_a_page(qapp, panel):
    board = panel.board
    armor = panel.new_page(name="Armor references")
    panel.new_page(name="Hunter: leather armor")
    here = panel.new_page(name="Mage costume")
    board.setFocus()
    QTest.keySequence(board, "Ctrl+P")
    switcher = panel._switcher
    assert switcher.isVisible() and switcher.edit.hasFocus()
    first = [switcher.list.item(n).data(Qt.ItemDataRole.UserRole) for n in range(switcher.list.count())]
    assert here not in first and first[0] != armor  # (recent first: the one before this)
    QTest.keyClicks(switcher.edit, "arm")
    rows = [switcher.list.item(n).data(Qt.ItemDataRole.UserRole) for n in range(switcher.list.count())]
    assert rows[0] == armor
    QTest.keyClick(switcher.edit, Qt.Key.Key_Return)
    assert panel.current_page_id() == armor and not switcher.isVisible() and panel.btn_back.isVisible()
    panel.go_back()
    assert panel.current_page_id() == here


# ----------------------------------------------------------------------
# Headers for older pages
# ----------------------------------------------------------------------
def test_older_pages_get_a_header_above_whats_on_them(qapp, panel, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from core.settings import get_artbook_headers_hint_shown

    board = panel.board
    old = store.create_page("Old refs")
    page = store.load_page(old["id"])
    page["items"] = [{"kind": "note", "id": "n1", "text": "Hood", "html": "", "x": 100.0, "y": 400.0, "width": 260.0}]
    store.save_page_now(page)
    current = panel.current_page_id()
    missing = panel.pages_without_header()
    assert old["id"] in missing and current in missing
    panel._offer_headers()
    assert wait_for(qapp, lambda: get_artbook_headers_hint_shown())
    assert "Add Headers to Every Page" in board.toast.text()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    assert panel.add_headers_to_all_pages() == len(missing)
    assert board.title_note() is not None and panel.pages_without_header() == []
    header = next(d for d in store.load_page(old["id"])["items"] if d.get("style") == "title")
    assert header["text"] == "Old refs" and header["x"] == 100.0 and header["y"] < 400.0
    assert panel.add_headers_to_all_pages() == 0  # (every page has one now)


# ----------------------------------------------------------------------
# The mouse controls in the bottom-right corner
# ----------------------------------------------------------------------
def test_the_mouse_controls_sit_bottom_right_and_brighten_on_hover(qapp, panel, no_motion):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMouseEvent

    from core.settings import get_artbook_nav_hints

    board = panel.board
    hints = board.nav_hints
    panel.resize(1900, 700)  # (offscreen letters are twice as wide as real ones)
    qapp.processEvents()
    assert hints.isVisible() and "MMB pan" in hints.text() and "Alt+LMB drag freely" in hints.text()
    box = hints.geometry()
    assert board.width() - 30 < box.right() <= board.width() and board.height() - 30 < box.bottom() <= board.height()
    assert hints.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # (clicks go to the board)

    def move_to(point):
        at = board.viewport().mapFrom(board, point)
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(at), QPointF(board.viewport().mapToGlobal(at)),
                            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(board.viewport(), event)

    resting = hints.colors()[1].alphaF()
    move_to(box.center())
    assert hints.hovered and hints.level == 1.0 and hints.colors()[1].alphaF() > resting
    move_to(QPoint(40, 40))
    assert not hints.hovered and hints.level == 0.0

    # left of the tray while it's open, back in the corner when it closes (by its own button too)
    board.toggle_tray()
    assert hints.geometry().right() < board.tray.x()
    board.tray._on_close()
    qapp.processEvents()
    assert hints.geometry().right() > board.width() - 30

    # right-click > Canvas > Navigation Hints turns them off (and remembers)
    menu = board.context_menu(QPointF(0, 0))
    canvas = next(a.menu() for a in menu.actions() if a.text() == "Canvas")
    toggle = next(a for a in canvas.actions() if a.text() == "Navigation Hints")
    assert toggle.isCheckable() and toggle.isChecked()
    toggle.trigger()
    menu.deleteLater()
    assert not hints.isVisible() and get_artbook_nav_hints() is False
    board.toggle_nav_hints()
    assert hints.isVisible() and get_artbook_nav_hints() is True

    # out of a message's way while it shows
    board.show_message("A long message across the bottom of the board, " * 3)
    board.toast.setGeometry(hints.geometry().adjusted(-40, 0, 0, 0))  # (wherever it lands: over the words)
    assert not hints.isVisible()
    board.toast.hide()
    assert hints.isVisible()

    # hidden where they don't fit
    panel.resize(500, 700)
    qapp.processEvents()
    assert not hints.isVisible()
