"""The PDF viewer (ui.pdf_viewer): pages rendered at the zoom they're seen at, zooming and fitting,
the contents list and page thumbnails, links, and reopening where you left off."""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtTest import QTest

from core import pdf_notes
from ui import shortcuts


def pump(qapp, condition=lambda: False, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def _text_stream(lines, top):
    """A page's content: lines of Helvetica text, the first at top (PDF points up from the bottom)."""
    body = f"BT /F1 16 Tf 40 {top} Td " + " 0 -24 Td ".join(f"({line}) Tj" for line in lines) + " ET"
    return f"<< /Length {len(body)} >>\nstream\n{body}\nendstream"


def make_pdf(path):
    """Three pages (the middle one landscape) with a little text, two chapters in its contents, a
    link on the first page to the third, and a web link."""
    font = "/Resources << /Font << /F1 12 0 R >> >>"
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /Outlines 7 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 >>",
        3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 6 0 R {font} /Annots [10 0 R 11 0 R] >>",
        4: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Contents 13 0 R {font} >>",
        5: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 14 0 R {font} >>",
        6: _text_stream(["Hello elbow world", "second line here"], 370),
        12: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        13: _text_stream(["Nothing to find on this one"], 270),
        14: _text_stream(["Another ELBOW at last", "Upper", "Limb notes"], 370),
        7: "<< /Type /Outlines /First 8 0 R /Last 9 0 R /Count 2 >>",
        8: "<< /Title (Chapter One) /Parent 7 0 R /Next 9 0 R /Dest [4 0 R /XYZ 0 300 0] >>",
        9: "<< /Title (Chapter Two) /Parent 7 0 R /Prev 8 0 R /Dest [5 0 R /XYZ 0 400 0] >>",
        10: "<< /Type /Annot /Subtype /Link /Rect [50 300 150 330] /Border [0 0 0] /Dest [5 0 R /XYZ 0 400 0] >>",
        11: "<< /Type /Annot /Subtype /Link /Rect [50 200 150 230] /Border [0 0 0] /A << /S /URI /URI (https://example.com/) >> >>",
    }
    out = b"%PDF-1.4\n"
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for number in sorted(objects):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)
    return path


@pytest.fixture
def pdf(tmp_path):
    return make_pdf(tmp_path / "book.pdf")


def open_viewer(qapp, path):
    from ui.pdf_viewer import PdfViewer

    viewer = PdfViewer(path)
    viewer.resize(1000, 700)
    viewer.show()
    assert pump(qapp, lambda: viewer._restored and viewer.view.zoom() > 0)
    qapp.processEvents()
    return viewer


@pytest.fixture
def viewer(qapp, pdf):
    widget = open_viewer(qapp, pdf)
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def key(widget, shortcut_id):
    combo = shortcuts.sequence(shortcut_id)[0]
    event = QKeyEvent(QEvent.Type.KeyPress, combo.key(), combo.keyboardModifiers())
    widget.keyPressEvent(event)
    return event


def test_pages_are_laid_out_and_rendered_sharp_for_the_zoom(qapp, viewer):
    view = viewer.view
    assert len(view.pages) == 3
    assert view.pages[1].size.width() == 400 and view.pages[1].pos().y() > view.pages[0].pos().y()
    first = view.pages[0]
    assert pump(qapp, lambda: first.image is not None)  # (a quick picture first...)
    assert pump(qapp, lambda: first.image_scale == pytest.approx(view.wanted_scale(first)))  # (...then a sharp one)
    view.set_zoom(3.0)
    assert pump(qapp, lambda: first.image_scale == pytest.approx(view.wanted_scale(first)) and first.image is not None)
    assert first.image.width() >= 300 * 3.0 * 96 / 72 * 0.99  # (as sharp as it shows)


def test_fit_width_and_page_and_zoom_keys(qapp, viewer):
    view = viewer.view
    key(view, "pdf.fit_page")
    assert view.fit_mode() == "page"
    page = view.page_rect(view.current_page())
    shown = view.mapFromScene(page).boundingRect()
    assert shown.height() <= view.viewport().height() and shown.width() <= view.viewport().width()
    key(view, "pdf.fit_width")
    assert view.fit_mode() == "width"
    shown = view.mapFromScene(view.page_rect(view.current_page())).boundingRect()
    assert shown.left() >= 0 and shown.right() <= view.viewport().width()  # (this page's width fits; a wider one can scroll)
    assert shown.width() > view.viewport().width() * 0.9
    before = view.zoom()
    key(view, "pdf.zoom_in")
    assert view.zoom() == pytest.approx(before * 1.25) and view.fit_mode() is None
    key(view, "pdf.actual_size")
    assert view.zoom() == pytest.approx(1.0)
    assert viewer.zoom_box.currentText() == "100%"
    viewer.zoom_box.setEditText("150")
    viewer._on_zoom_typed()
    assert view.zoom() == pytest.approx(1.5)


def test_ctrl_wheel_zooms_toward_the_pointer(qapp, viewer):
    view = viewer.view
    view.set_zoom(1.0)
    at = QPointF(300, 200)
    under = view.mapToScene(at.toPoint())
    wheel = QWheelEvent(at, view.viewport().mapToGlobal(at), QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(wheel)
    assert view.zoom() == pytest.approx(1.15)
    after = view.mapToScene(at.toPoint())
    assert abs(after.y() - under.y()) < 2  # (the point stays under the pointer; sideways the pages stay centred)


def test_pages_turn_with_the_keys_and_the_page_box(qapp, viewer):
    view = viewer.view
    view.set_zoom(1.0)
    view.show_page(0)
    key(view, "pdf.next_page")
    assert view.current_page() == 1 and viewer.page_box.text() == "2"
    key(view, "pdf.last_page")
    assert view.current_page() == 2
    key(view, "pdf.first_page")
    assert view.current_page() == 0
    viewer.page_box.setText("3")
    viewer._on_page_typed()
    assert view.current_page() == 2


def test_the_file_viewers_keys_pass_through(qapp, viewer):
    for shortcut_id in ("viewer.next", "viewer.previous", "viewer.next_alt", "viewer.exit_fullscreen"):
        assert not key(viewer.view, shortcut_id).isAccepted()  # (Page Down etc.: the next file, as before)


def test_contents_and_thumbnails_go_to_their_page(qapp, viewer):
    assert not viewer.sidebar.isHidden() and viewer.current_tab() == "contents"  # (it has contents: shown)
    model = viewer.contents_model
    assert [model.index(r, 0).data() for r in range(model.rowCount())] == ["Chapter One", "Chapter Two"]
    viewer.view.set_zoom(1.0)
    viewer._on_contents_clicked(model.index(1, 0))
    assert viewer.view.current_page() == 2
    viewer.show_tab("pages")
    thumbs = viewer.thumb_model
    assert thumbs.rowCount() == 3
    thumbs.data(thumbs.index(1, 0), Qt.ItemDataRole.DecorationRole)
    assert pump(qapp, lambda: 1 in thumbs._pictures)
    viewer._on_thumb_clicked(thumbs.index(1, 0))
    assert viewer.view.current_page() == 1
    key(viewer, "pdf.sidebar")
    assert viewer.sidebar.isHidden()


def test_links_go_to_their_page_or_open_the_browser(qapp, viewer, monkeypatch):
    from ui import pdf_viewer

    opened = []
    monkeypatch.setattr(pdf_viewer.QDesktopServices, "openUrl", lambda url: opened.append(url))
    view = viewer.view
    view.set_zoom(1.0)
    view.show_page(0)
    first = view.pages[0]
    to_page = view.mapFromScene(first.pos() + QPointF(100, 85))  # (the link's middle: 400 - 330 + 15 down the page)
    assert view.link_at(to_page) is not None and view.link_text(view.link_at(to_page)) == "Go to page 3"
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, to_page)
    assert view.current_page() == 2
    view.show_page(0)
    to_web = view.mapFromScene(first.pos() + QPointF(100, 185))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, to_web)
    assert opened == [QUrl("https://example.com/")]


def test_dragging_pans_without_following_links(qapp, viewer, monkeypatch):
    from ui import pdf_viewer

    opened = []
    monkeypatch.setattr(pdf_viewer.QDesktopServices, "openUrl", lambda url: opened.append(url))
    view = viewer.view
    view.set_zoom(2.0)
    view.show_page(0)
    start = view.mapFromScene(view.pages[0].pos() + QPointF(100, 185))
    before = view.verticalScrollBar().value()
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(view.viewport(), start - QPoint(0, 60))
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start - QPoint(0, 60))
    assert view.verticalScrollBar().value() == before + 60 and opened == []


def test_it_reopens_where_you_left_off(qapp, pdf):
    first = open_viewer(qapp, pdf)
    first.view.set_zoom(1.8)
    first.view.show_page(2, 40.0)
    first.toggle_sidebar()
    first.save_state()
    kept = pdf_notes.load(first.key)["view"]
    assert kept["page"] == 2 and kept["zoom"] == pytest.approx(1.8) and kept["sidebar"] is False
    first.close()
    first.deleteLater()
    again = open_viewer(qapp, pdf)
    assert again.view.zoom() == pytest.approx(1.8) and again.view.current_page() == 2
    assert again.view.page_offset() == pytest.approx(40.0, abs=1.0) and again.sidebar.isHidden()
    again.close()
    again.deleteLater()


def test_what_it_keeps_follows_the_file_when_its_moved(qapp, pdf, tmp_path):
    first = open_viewer(qapp, pdf)
    first.view.set_zoom(2.2)
    first.save_state()
    first.close()
    first.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # (it lets go of the file)
    moved = tmp_path / "elsewhere" / "renamed.pdf"
    moved.parent.mkdir()
    pdf.rename(moved)
    again = open_viewer(qapp, moved)
    assert again.view.zoom() == pytest.approx(2.2)
    again.close()
    again.deleteLater()


def test_a_file_that_isnt_a_pdf_says_so(qapp, tmp_path):
    from ui.pdf_viewer import PdfViewer

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    viewer = PdfViewer(broken)
    assert viewer.view is None and "Couldn't open this PDF" in viewer.error
    viewer.save_state()  # (nothing to keep, no error)
    viewer.deleteLater()


def test_enter_in_the_page_box_doesnt_press_a_button_of_the_viewer(qapp, pdf):
    """(Enter went on to the file viewer, which pressed its default button: the tag editor opened.)"""
    import os

    from core.scanner import AssetItem
    from ui.viewer import AssetViewerDialog

    asset = AssetItem(pdf, pdf.name, ".pdf", os.path.getsize(pdf), os.path.getmtime(pdf), "Documents", "PDF")
    edits = []
    dialog = AssetViewerDialog(asset, tags=[], edit_tags=lambda: edits.append(1))
    dialog.show()
    viewer = dialog.pdf_viewer
    assert pump(qapp, lambda: viewer._restored)
    viewer.view.set_zoom(1.0)
    for box in (viewer.page_box, viewer.zoom_box.lineEdit()):
        box.setFocus()
        box.selectAll()
        QTest.keyClicks(box, "3" if box is viewer.page_box else "150")
        QTest.keyClick(box, Qt.Key.Key_Return)
        qapp.processEvents()
    assert viewer.view.current_page() == 2 and viewer.view.zoom() == pytest.approx(1.5)
    assert edits == [] and dialog.isVisible()  # (no tag editor, and the viewer stays open)
    dialog.close()
    dialog.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)



def search(qapp, viewer, query):
    viewer.view.find(query)
    assert pump(qapp, lambda: viewer.view.search.done)
    qapp.processEvents()
    return viewer.view.search.matches


def test_find_highlights_every_match_and_steps_through_them(qapp, viewer):
    view = viewer.view
    view.set_zoom(1.0)
    view.show_page(0)
    matches = search(qapp, viewer, "elbow")
    assert [m[0] for m in matches] == [0, 2]  # (whatever its case: ELBOW on the last page)
    assert view.current_match == 0 and viewer.lbl_matches.text() == "1 of 2"
    boxes = view.match_boxes(0)
    assert boxes and all(view.pages[0].boundingRect().contains(box) for box in boxes)
    assert view.marks(0)[0] == [(boxes, True)]  # (the page draws it, as the current one)
    view.step_match(1)
    assert view.current_match == 1 and view.current_page() == 2 and viewer.lbl_matches.text() == "2 of 2"
    view.step_match(1)
    assert view.current_match == 0  # (round again)
    assert search(qapp, viewer, "upper limb")  # (across a line break)
    assert search(qapp, viewer, "giraffe") == [] and viewer.lbl_matches.text() == "No matches"


def test_a_new_search_starts_from_the_page_youre_on(qapp, viewer):
    viewer.view.set_zoom(1.0)
    viewer.view.show_page(2)
    search(qapp, viewer, "elbow")
    assert viewer.view.current_match == 1  # (the match on this page, not the first in the PDF)


def test_the_find_box(qapp, viewer):
    viewer.view.setFocus()
    key(viewer, "pdf.find")
    assert viewer.focusWidget() is viewer.find_box
    QTest.keyClicks(viewer.find_box, "elbow")
    QTest.keyClick(viewer.find_box, Qt.Key.Key_Return)  # (searches straight away, then steps)
    assert pump(qapp, lambda: viewer.view.search.done and viewer.view.current_match >= 0)
    first = viewer.view.current_match
    QTest.keyClick(viewer.find_box, Qt.Key.Key_Return)
    assert viewer.view.current_match == (first + 1) % 2
    QTest.keyClick(viewer.find_box, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert viewer.view.current_match == first
    QTest.keyClick(viewer.find_box, Qt.Key.Key_Escape)
    assert viewer.find_box.text() == "" and viewer.view.search.matches == [] and viewer.lbl_matches.text() == ""


def drag(view, start, end):
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(view.viewport(), (start + end) / 2)
    QTest.mouseMove(view.viewport(), end)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)


def test_selecting_and_copying_text(qapp, viewer):
    from PySide6.QtWidgets import QApplication

    view = viewer.view
    view.set_zoom(1.0)
    view.show_page(0)
    key(view, "pdf.select_tool")
    assert view.mode == "select" and viewer.tool_buttons["select"].isChecked()
    page = view.pages[0].pos()
    drag(view, view.mapFromScene(page + QPointF(30, 25)), view.mapFromScene(page + QPointF(290, 25)))  # (from the margin)
    assert view.selected_text().strip() == "Hello elbow world"
    assert view.marks(0)[1]  # (drawn on the page)
    key(view, "pdf.copy")
    assert QApplication.clipboard().text().strip() == "Hello elbow world"
    last = view.pages[2].pos()
    drag(view, view.mapFromScene(page + QPointF(30, 25)), view.mapFromScene(last + QPointF(100, 30)))  # (across pages)
    text = view.selected_text()
    assert text.startswith("Hello") and "Nothing to find" in text and "Another" in text
    view.show_page(0)
    key(view, "pdf.select_page")
    assert set(view.selection) == {0} and "second line here" in view.selected_text()


def test_esc_clears_the_selection_then_the_tool_then_closes(qapp, viewer):
    view = viewer.view
    key(view, "pdf.select_tool")
    view.select_page_text(0)
    assert view.selection
    assert key(view, "pdf.escape").isAccepted() and not view.selection and view.mode == "select"
    assert key(view, "pdf.escape").isAccepted() and view.mode == "hand"
    assert not key(view, "pdf.escape").isAccepted()  # (now the viewer's: it closes)


def test_space_drag_pans_with_the_select_tool(qapp, viewer):
    view = viewer.view
    view.set_zoom(2.0)
    view.show_page(0)
    key(view, "pdf.select_tool")
    key(view, "pdf.space_pan")
    before = view.verticalScrollBar().value()
    start = QPoint(300, 300)
    drag(view, start, start - QPoint(0, 50))
    assert view.verticalScrollBar().value() == before + 50 and not view.selection
    combo = shortcuts.sequence("pdf.space_pan")[0]
    view.keyReleaseEvent(QKeyEvent(QEvent.Type.KeyRelease, combo.key(), combo.keyboardModifiers()))
    assert not view._space
