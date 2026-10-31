"""Your marks on a PDF (ui.pdf_marks, in ui.pdf_viewer): highlights, pen drawings and typed notes,
the eraser, picking and moving with the Hand tool, undo, the Notes list, and keeping them (in the
app's notes, never in the PDF)."""

from __future__ import annotations

import hashlib

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtTest import QTest

from core import pdf_notes
from tests.test_pdf_viewer import drag, key, make_pdf, open_viewer, pump
from ui.pdf_marks import HighlightMark, InkMark, NoteMark


@pytest.fixture
def pdf(tmp_path):
    return make_pdf(tmp_path / "book.pdf")


@pytest.fixture
def viewer(qapp, pdf):
    widget = open_viewer(qapp, pdf)
    widget.view.set_zoom(1.0)
    widget.view.show_page(0)
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def marks(view, kind=None):
    return [item for _page, item in view.mark_items() if kind is None or isinstance(item, kind)]


def on_page(view, page, x, y):
    return view.mapFromScene(view.pages[page].pos() + QPointF(x, y))


def test_highlighting_selected_text_and_undo(qapp, viewer):
    view = viewer.view
    view.select_page_text(0)
    key(view, "pdf.highlight_tool")  # (with text selected: it's highlighted)
    (highlight,) = marks(view, HighlightMark)
    assert "Hello elbow world" in highlight.text and highlight.boxes and not view.selection
    assert highlight.color == view.colors["highlight"]
    assert viewer.notes_list.count() == 1 and "Highlight: Hello elbow world" in viewer.notes_list.item(0).text()
    key(view, "pdf.undo")
    assert marks(view) == [] and viewer.notes_list.count() == 0
    key(view, "pdf.redo")
    assert len(marks(view, HighlightMark)) == 1


def test_the_highlight_tool_highlights_what_you_drag_over(qapp, viewer):
    view = viewer.view
    key(view, "pdf.highlight_tool")
    assert view.mode == "highlight" and viewer.tool_buttons["highlight"].isChecked()
    drag(view, on_page(view, 0, 30, 25), on_page(view, 0, 290, 25))
    (highlight,) = marks(view, HighlightMark)
    assert highlight.text.strip() == "Hello elbow world"
    assert view.mode == "highlight"  # (stays on for the next one)


def test_drawing_with_the_pen(qapp, viewer):
    view = viewer.view
    key(view, "pdf.pen_tool")
    view.pen_width = 3.0
    viewer._set_tool_color("pen", "#1f6feb")
    start, end = on_page(view, 0, 40, 100), on_page(view, 0, 200, 160)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    for step in range(1, 11):
        QTest.mouseMove(view.viewport(), start + (end - start) * step / 10)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    (ink,) = marks(view, InkMark)
    assert ink.color == "#1f6feb" and ink.width == 3.0 and len(ink.points) > 5
    first, last = ink.points[0], ink.points[-1]
    assert abs(first.x() - 40) < 2 and abs(first.y() - 100) < 2 and abs(last.x() - 200) < 2 and abs(last.y() - 160) < 2
    assert ink.parentItem() is view.pages[0]  # (on its page: it zooms and scrolls with it)


def test_typing_a_note(qapp, viewer):
    view = viewer.view
    key(view, "pdf.note_tool")
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, on_page(view, 0, 60, 200))
    note = view.editing_note()
    assert note is not None
    QTest.keyClicks(view, "Check the ulna")
    key(view, "pdf.escape")  # (finishes typing)
    assert view.editing_note() is None
    (kept,) = marks(view, NoteMark)
    assert kept.toPlainText() == "Check the ulna" and abs(kept.pos().x() - 60) < 1
    assert "Note: Check the ulna" in viewer.notes_list.item(0).text()
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, on_page(view, 0, 60, 320))
    key(view, "pdf.escape")  # (a note left empty isn't kept)
    assert len(marks(view, NoteMark)) == 1
    key(view, "pdf.undo")
    assert marks(view, NoteMark) == []


def test_editing_a_note_is_one_undo_step(qapp, viewer):
    view = viewer.view
    view.change_marks([], [{"id": "n1", "kind": "note", "page": 0, "color": "#fff3a3", "x": 50, "y": 200, "text": "first"}], "note")
    note = marks(view, NoteMark)[0]
    QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, on_page(view, 0, 60, 210))
    assert view.editing_note() is note  # (double-click a note: type in it)
    QTest.keyClick(view, Qt.Key.Key_End)
    QTest.keyClicks(view, " and second")
    view.finish_note_edit()
    assert marks(view, NoteMark)[0].toPlainText() == "first and second"
    key(view, "pdf.undo")
    assert marks(view, NoteMark)[0].toPlainText() == "first"


def test_the_eraser_rubs_marks_out_in_one_step(qapp, viewer):
    view = viewer.view
    view.change_marks([], [
        {"id": "a", "kind": "ink", "page": 0, "color": "#e5484d", "width": 2, "points": [[40, 100], [200, 100]]},
        {"id": "b", "kind": "ink", "page": 0, "color": "#e5484d", "width": 2, "points": [[40, 140], [200, 140]]},
        {"id": "c", "kind": "ink", "page": 0, "color": "#e5484d", "width": 2, "points": [[40, 250], [200, 250]]},
    ], "draw")
    key(view, "pdf.eraser_tool")
    drag(view, on_page(view, 0, 120, 80), on_page(view, 0, 120, 160))  # (across the first two)
    assert sorted(item.mark_id for item in marks(view)) == ["c"]
    key(view, "pdf.undo")
    assert len(marks(view)) == 3


def test_picking_moving_and_deleting_with_the_hand(qapp, viewer):
    view = viewer.view
    view.change_marks([], [{"id": "a", "kind": "ink", "page": 0, "color": "#e5484d", "width": 2, "points": [[40, 100], [200, 100]]}], "draw")
    assert view.mode == "hand"
    drag(view, on_page(view, 0, 120, 100), on_page(view, 0, 120, 150))
    (ink,) = marks(view)
    assert view.chosen is ink
    assert abs(view.mark_dict(ink)["points"][0][1] - 150) < 1.5  # (moved 50 points down)
    key(view, "pdf.undo")
    assert abs(view.mark_dict(marks(view)[0])["points"][0][1] - 100) < 0.5
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, on_page(view, 0, 120, 100))
    assert view.chosen is not None
    key(view, "pdf.delete_mark")
    assert marks(view) == []


def test_esc_order_with_marks(qapp, viewer):
    view = viewer.view
    view.change_marks([], [{"id": "a", "kind": "ink", "page": 0, "color": "#e5484d", "width": 2, "points": [[40, 100], [200, 100]]}], "draw")
    view.choose(marks(view)[0])
    assert key(view, "pdf.escape").isAccepted() and view.chosen is None
    assert not key(view, "pdf.escape").isAccepted()  # (nothing left: the viewer's Esc)


def test_hiding_and_listing_marks(qapp, viewer):
    view = viewer.view
    view.change_marks([], [
        {"id": "h", "kind": "highlight", "page": 2, "color": "#ffd400", "text": "Another", "boxes": [[40, 20, 100, 16]]},
        {"id": "n", "kind": "note", "page": 0, "color": "#fff3a3", "x": 50, "y": 200, "text": "look"},
    ], "marks")
    assert [viewer.notes_list.item(i).text().split()[1] for i in range(2)] == ["1", "3"]  # (page order)
    viewer.btn_show_marks.click()
    assert not any(item.isVisible() for item in marks(view))
    viewer._on_note_listed_clicked(viewer.notes_list.item(1))  # (going to one shows them again)
    assert view.current_page() == 2 and view.chosen is not None and all(item.isVisible() for item in marks(view))


def test_marks_are_kept_by_the_app_and_the_pdf_is_never_touched(qapp, pdf):
    before = hashlib.sha256(pdf.read_bytes()).hexdigest()
    first = open_viewer(qapp, pdf)
    view = first.view
    view.select_page_text(0)
    view.highlight_selection()
    view.change_marks([], [
        {"id": "i", "kind": "ink", "page": 1, "color": "#1f6feb", "width": 3, "points": [[10, 10], [60, 40], [90, 20]]},
        {"id": "n", "kind": "note", "page": 2, "color": "#c8f7c5", "x": 30, "y": 60, "text": "remember"},
    ], "marks")
    first.save_state()
    kept = pdf_notes.load(first.key)["annotations"]
    assert sorted(d["kind"] for d in kept) == ["highlight", "ink", "note"]
    saved = view.marks_data()
    first.close()
    first.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    again = open_viewer(qapp, pdf)
    assert again.view.marks_data() == saved
    assert again.notes_list.count() == 3
    again.close()
    again.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == before


def test_damaged_saved_marks_are_skipped(qapp, pdf):
    from ui.pdf_viewer import PdfViewer

    key_name = pdf_notes.key_for(pdf)
    pdf_notes.save(key_name, {"annotations": [
        {"kind": "ink", "page": 0, "points": [[1, 2], [3, 4]], "color": "#123456", "width": 2},
        {"kind": "ink", "page": 99, "points": [[1, 2]]},  # (no such page)
        {"kind": "highlight", "page": 0, "boxes": "nonsense"},
        {"kind": "mystery", "page": 0},
        "not even a dict",
    ]})
    viewer = PdfViewer(pdf)
    assert len(viewer.view.marks_data()) == 1
    viewer.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
