"""Sending a PDF's page to the Artbook and exporting a copy with your marks drawn in (ui.pdf_viewer).
The PDF itself is only ever read."""

from __future__ import annotations

import hashlib

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor, QImage
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QMenu

from tests.test_pdf_viewer import make_pdf, open_viewer, pump

INK = {"id": "i", "kind": "ink", "page": 0, "color": "#1f6feb", "width": 6, "points": [[40, 200], [260, 200]]}
NOTE = {"id": "n", "kind": "note", "page": 2, "color": "#c8f7c5", "x": 40, "y": 200, "text": "remember"}


@pytest.fixture
def pdf(tmp_path):
    return make_pdf(tmp_path / "book.pdf")


class FakeArtbook:
    """Stands in for the main window: records what's sent."""

    def __init__(self):
        self.sent = []

    def fill_artbook_menu(self, menu, on_pick):
        menu.addAction("New Page").triggered.connect(lambda: on_pick(None))
        menu.addAction("Refs").triggered.connect(lambda: on_pick("refs"))

    def send_image_to_artbook(self, image, name, page_id):
        self.sent.append((image, name, page_id))
        return f'Sent 1 image to "{page_id}".'


@pytest.fixture
def viewer(qapp, pdf):
    from ui.pdf_viewer import PdfViewer

    widget = PdfViewer(pdf, artbook=FakeArtbook())
    widget.resize(1000, 700)
    widget.show()
    assert pump(qapp, lambda: widget._restored)
    widget.view.change_marks([], [INK, NOTE], "marks")
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_a_page_picture_has_your_marks_drawn_in(qapp, viewer):
    view = viewer.view
    with_marks = view.page_picture(0, 2.0)
    plain = view.page_picture(0, 2.0, marks=False)
    assert with_marks.size() == plain.size() and with_marks.width() == 600  # (300 points at 2 px each)
    on_line = with_marks.pixelColor(300, 400)  # (the pen line at y 200 points)
    assert on_line.blue() > 150 and on_line.red() < 100
    assert plain.pixelColor(300, 400) == QColor("#ffffff")
    assert view.chosen is None  # (drawing it didn't pick anything)
    card = view.page_picture(2, 1.0).pixelColor(45, 205)
    assert card.name() == "#c8f7c5"  # (the note's card)


def test_sending_a_page_to_the_artbook(qapp, viewer):
    viewer.send_page(0, "refs", marks=True)
    image, name, page_id = viewer.artbook.sent[-1]
    assert page_id == "refs" and name == "book, p. 1"
    assert max(image.width(), image.height()) == round(400 * 3.0)  # (sharp: 216 dpi)
    assert viewer.toast.isVisible() and "refs" in viewer.toast.text()


def test_the_page_menu_offers_both_kinds_of_send(qapp, viewer):
    menu = QMenu()
    viewer._page_menu(menu, 0)  # (has a mark)
    titles = [action.text() for action in menu.actions()]
    assert titles == ["Send Page to Artbook", "Send Page to Artbook (without your marks)", "Export a Copy..."]
    plain_menu = menu.actions()[1].menu()
    plain_menu.actions()[0].trigger()  # New Page
    assert viewer.artbook.sent[-1][2] is None and viewer.artbook.sent[-1][0].pixelColor(150 * 3, 200 * 3) == QColor("#ffffff")  # (no pen line)
    menu.deleteLater()
    unmarked = QMenu()
    viewer._page_menu(unmarked, 1)
    assert [action.text() for action in unmarked.actions()] == ["Send Page to Artbook", "Export a Copy..."]
    unmarked.deleteLater()


def test_exporting_the_marked_pages_as_a_pdf(qapp, viewer, pdf, tmp_path):
    before = hashlib.sha256(pdf.read_bytes()).hexdigest()
    target = tmp_path / "out" / "copy.pdf"
    target.parent.mkdir()
    viewer.export_pages([0, 2], target, "pdf", 150, True)
    assert pump(qapp, lambda: not viewer.exporting(), seconds=20)
    copy = QPdfDocument()
    copy.load(str(target))
    assert copy.status() == QPdfDocument.Status.Ready and copy.pageCount() == 2
    size = copy.pagePointSize(0)
    assert abs(size.width() - 300) < 1 and abs(size.height() - 400) < 1  # (each page its own size)
    first = copy.render(0, copy.pagePointSize(0).toSize() * 2)
    assert first.pixelColor(300, 400).blue() > 150  # (the pen line came along)
    copy.close()
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == before
    assert "Exported 2 pages" in viewer.toast.text()


def test_exporting_every_page_as_pngs(qapp, viewer, tmp_path):
    folder = tmp_path / "pngs"
    viewer.export_pages([0, 1, 2], folder, "png", 150, False)
    assert pump(qapp, lambda: not viewer.exporting(), seconds=20)
    files = sorted(p.name for p in folder.iterdir())
    assert files == ["book p1.png", "book p2.png", "book p3.png"]
    landscape = QImage(str(folder / "book p2.png"))
    assert landscape.width() > landscape.height() and landscape.width() == round(400 * 150 / 72)


def test_a_cancelled_pdf_export_leaves_nothing_behind(qapp, viewer, tmp_path):
    target = tmp_path / "copy.pdf"
    viewer.export_pages([0, 1, 2], target, "pdf", 150, True)
    viewer._export["progress"].cancel()
    assert pump(qapp, lambda: not viewer.exporting(), seconds=20)
    assert not target.exists() and "cancelled" in viewer.toast.text()


def test_it_wont_export_over_the_pdf_itself(qapp, viewer, pdf, monkeypatch):
    from ui import pdf_viewer

    warned = []
    monkeypatch.setattr(pdf_viewer.QMessageBox, "warning", lambda *args: warned.append(args))
    before = pdf.read_bytes()
    viewer.export_pages([0], pdf, "pdf", 150, True)
    assert warned and not viewer.exporting() and pdf.read_bytes() == before


def test_the_main_window_puts_a_sent_page_on_an_artbook_page(qapp):
    import main

    window = main.MainWindow()
    try:
        menu = QMenu()
        picked = []
        window.fill_artbook_menu(menu, picked.append)
        assert menu.actions()[0].text() == "New Page"
        menu.actions()[0].trigger()
        assert picked == [None]
        menu.deleteLater()
        picture = QImage(120, 80, QImage.Format.Format_RGB32)
        picture.fill(QColor("#336699"))
        message = window.send_image_to_artbook(picture, "book, p. 4", None)
        assert message.startswith("Sent 1 image")
        board = window.artbook_panel.board
        assert pump(qapp, lambda: len(board.items_list()) == 1)
        (item,) = board.items_list()
        assert item.name == "book, p. 4" and item.natural == (120, 80)
    finally:
        window.close()
        window.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
