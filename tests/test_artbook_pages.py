"""Board cards Batch I: the page's header (its name, a line about it, what's on the page), search on a page and
across pages, templates for new pages, and exporting (the Export dialog, the quick menu, PDF with clickable cards)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from core import artbook_store as store
from tests.test_artbook import pump  # noqa: F401  (shared helpers)
from tests.test_artbook_cards import make_note, make_table, make_todo, no_motion, panel, wait_for  # noqa: F401


def double_click(qapp, board, scene_point) -> None:
    point = board.mapFromScene(scene_point)
    QTest.mouseMove(board.viewport(), point + QPointF(40, 40).toPoint())  # (the first move only enters the view)
    QTest.mouseMove(board.viewport(), point)
    QTest.mouseDClick(board.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qapp.processEvents()


# ----------------------------------------------------------------------
# The page's header
# ----------------------------------------------------------------------
def test_a_new_page_has_a_header_with_its_name_and_whats_on_it(qapp, panel):
    board = panel.board
    page_id = panel.new_page(name="Mage costume")
    title = board.title_note()
    assert title is not None and title.style == "title" and title.toPlainText() == "Mage costume"
    assert title.stats == "Nothing on it yet" and title.toolTip().startswith("The page's name")
    assert not board.undo_stack.canUndo()  # (a new page's header isn't an undo step)
    make_todo(qapp, board, "Hood", ("Rim", "Lining", "Silhouette"))
    make_note(qapp, board, QPointF(400, 400), "Straps: two thin ones")
    hood = next(n for n in board.items_list() if getattr(n, "check_rows", None) and n.check_rows())
    board.toggle_check(hood, hood.check_rows()[0].blockNumber())
    assert wait_for(qapp, lambda: title.stats == "2 cards · 1 of 3 to-dos done")
    panel.save_now()
    summary = store.page_summary(page_id)
    assert summary["counts"] == (0, 2, 1, 3)  # (page cards show the same: the header isn't a card)
    assert store.counts_text(summary["counts"]) == title.stats
    assert board.add_here_entries(QPointF(0, 0))[5][0] == "Page Title" and not board.add_here_entries(QPointF(0, 0))[5][3]
    assert board.add_page_title() is title  # (one per page: shown, not made again)


def test_typing_the_headers_name_renames_the_page_and_undo_renames_it_back(qapp, panel):
    board = panel.board
    page_id = panel.new_page(name="Page 7")
    title = board.title_note()
    double_click(qapp, board, title.mapToScene(title.boundingRect().topLeft() + QPointF(30, 25)))
    assert board.editing_note() is title
    QTest.keySequence(board, "Ctrl+A")
    QTest.keyClicks(board, "Mage   costume ")
    QTest.keyClick(board, Qt.Key.Key_Return)  # (Enter: done, one line)
    assert board.editing_note() is None and title.toPlainText() == "Mage costume"
    assert wait_for(qapp, lambda: panel.page["name"] == "Mage costume")
    assert store.load_page(page_id)["name"] == "Mage costume" and "Mage costume" in panel.lbl_page.text()
    board.undo_stack.undo()
    assert wait_for(qapp, lambda: panel.page["name"] == "Page 7") and title.toPlainText() == "Page 7"
    panel.rename_page(page_id, "Hunter armor")  # (renamed in Pages: the header follows)
    assert title.toPlainText() == "Hunter armor" and board.page_name == "Hunter armor"


def test_the_line_under_the_name_is_typed_in_place_and_saved(qapp, panel):
    board = panel.board
    page_id = panel.new_page(name="Mage costume")
    title = board.title_note()
    assert not title.about_shown()
    under = title.mapToScene(title.about_rect().center())
    double_click(qapp, board, under)
    assert board._renaming is not None and board._renaming["item"] is title and title.about_item.isVisible()
    QTest.keyClicks(board, "Mentorship project: week 3")
    QTest.keyClick(board, Qt.Key.Key_Return)
    assert title.about == "Mentorship project: week 3" and title.about_shown()
    board.undo_stack.undo()
    assert title.about == "" and not title.about_shown()
    board.undo_stack.redo()
    panel.save_now()
    data = next(d for d in store.load_page(page_id)["items"] if d.get("style") == "title")
    assert data["about"] == "Mentorship project: week 3"
    board.load_page(store.load_page(page_id))
    again = board.title_note()
    assert again.about == "Mentorship project: week 3" and again.about_item.toPlainText() == again.about


def test_a_header_added_to_an_old_page_goes_above_whats_there_and_stays_one(qapp, panel):
    from ui.artbook_board import NoteItem

    board = panel.board
    note = make_note(qapp, board, QPointF(200, 300), "Hood: keep the rim thin")
    title = board.add_page_title()
    assert board.title_note() is title and title.sceneBoundingRect().bottom() < note.sceneBoundingRect().top()
    assert abs(title.sceneBoundingRect().left() - note.sceneBoundingRect().left()) < 1
    board.undo_stack.undo()
    assert board.title_note() is None
    board.undo_stack.redo()
    title.setSelected(True)
    board.delete_selection()  # (into the Trash)
    board.add_page_title()
    board.restore_all_trash()  # (the old header back: a heading, as one page has one header)
    styles = sorted(n.style for n in board.items_list() if isinstance(n, NoteItem))
    assert styles.count("title") == 1 and "heading" in styles
    board.note_bar_target = lambda: title  # (the format bar can't turn the header into a card)
    board.set_note_style("card")
    assert title.style == "title"


# ----------------------------------------------------------------------
# Search: this page, every page, the Pages sidebar
# ----------------------------------------------------------------------
def test_searching_saved_pages_finds_every_kind_with_words_around_it():
    from core import artbook_search

    page = {"items": [
        {"kind": "note", "id": "n", "text": "Straps and belts: two thin ones", "html": "<p>x</p>"},
        {"kind": "note", "id": "t", "style": "title", "text": "Mage costume", "about": "Leather strap study"},
        {"kind": "note", "id": "d", "text": "Hood\nTop straps\nStrap buckle", "html": '<li class="unchecked">'},
        {"kind": "table", "id": "tb", "title": "Plan", "columns": [{"name": "Task"}], "rows": [["Strap buckle"], ["Hood"]]},
        {"kind": "link", "id": "l", "title": "Strap study", "about": "", "site": "Portfolio"},
        {"kind": "image", "id": "i", "name": "strap_detail.png", "source": {}},
        {"kind": "stroke", "id": "s"},
    ], "comments": [{"id": "c", "item": None, "notes": [{"text": "the straps read too thick"}]}]}
    hits = artbook_search.search_page(page, "STRAP")
    assert [(h["item"], h["kind"], h["count"]) for h in hits] == [
        ("n", "note", 1), ("t", "title", 1), ("d", "todo", 2), ("tb", "table", 1), ("l", "link", 1), ("i", "image", 1),
        (None, "comment", 1)]
    assert (hits[0]["before"], hits[0]["match"], hits[0]["after"]) == ("", "Strap", "s and belts: two thin ones")
    long = "word " * 30 + "strap" + " more" * 30
    before, match, after = artbook_search.snippet(long, long.index("strap"), 5)
    assert before.startswith("...") and after.endswith("...") and match == "strap" and len(before) < 45
    assert artbook_search.search_page(page, "   ") == []


def test_find_on_this_page_marks_the_matches_and_steps_through_them(qapp, panel):
    from ui.artbook_board import TableItem

    board = panel.board
    panel.new_page(name="Mage costume")
    straps = make_todo(qapp, board, "Straps", ("Top straps", "Strap buckle"))
    straps.setPos(-400, 0)
    hood = make_note(qapp, board, QPointF(300, 0), "Hood: thin rim")
    table = make_table(qapp, board, QPointF(0, 600), (("Strap buckle", "1.5 h"),))
    board.setFocus()
    QTest.keySequence(board, "Ctrl+F")
    bar = board.find_bar
    assert bar.isVisible() and bar.edit.hasFocus() and all(b.toolTip() for b in (bar.btn_next, bar.btn_previous, bar.btn_page, bar.btn_all))
    QTest.keyClicks(bar.edit, "strap")
    bar.run()
    assert bar.count.text() == "1 of 4" and board.find_position() == (1, 4)
    matches = board._find_matches
    assert [m["item"] for m in matches] == [straps, straps, straps, table] and matches[3]["cell"] == (0, 0)
    assert hood not in [m["item"] for m in matches]
    rects = board._text_rects(straps, matches[1]["start"], matches[1]["length"])
    from PySide6.QtGui import QFontMetricsF

    word = QFontMetricsF(straps.font()).horizontalAdvance("strap") * straps.scale()
    assert len(rects) == 1 and abs(rects[0].width() - word) < 2  # (around "strap" in "Top straps")
    board.viewport().grab()  # (draws the faded board, the marks and outlines)
    QTest.keyClick(bar.edit, Qt.Key.Key_Return)
    assert bar.count.text() == "2 of 4"
    QTest.keyClick(bar.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(bar.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert bar.count.text() == "4 of 4"  # (round to the last)
    view = board.mapToScene(board.viewport().rect()).boundingRect()
    assert view.intersects(table.sceneBoundingRect())  # (the board went there)
    table.setSelected(True)
    board.delete_selection()  # (changed: found again)
    assert wait_for(qapp, lambda: board.find_position()[1] == 3)
    QTest.keyClick(bar.edit, Qt.Key.Key_Escape)
    assert not bar.isVisible() and board.find_position() == (0, 0) and not isinstance(board.item(table.item_id), TableItem)


def test_find_on_every_page_lists_where_and_opens_the_page_at_the_match(qapp, panel):
    from ui.artbook_board import NoteItem

    board = panel.board
    other = panel.new_page(name="Armor references")
    rivets = make_note(qapp, board, QPointF(2000, 2000), "Shoulder strap rivets: brass")
    panel.save_now()
    here = panel.new_page(name="Mage costume")
    make_note(qapp, board, QPointF(0, 0), "Straps and belts")
    board.open_find()
    bar = board.find_bar
    bar.btn_all.click()
    assert bar.all_pages() and bar.edit.placeholderText() == "Find on every page"
    bar.edit.setText("strap")
    bar.run()
    assert wait_for(qapp, lambda: bar.results.isVisible() and bar.list.count() == 4)
    texts = [bar.list.item(n).text() for n in range(bar.list.count())]
    assert texts[0].startswith("MAGE COSTUME  ·  THIS PAGE") and texts[2].startswith("ARMOR REFERENCES")
    assert bar.list.item(3).data(Qt.ItemDataRole.UserRole) == (other, rivets.item_id, None)
    QTest.keyClick(bar.edit, Qt.Key.Key_Down)
    QTest.keyClick(bar.edit, Qt.Key.Key_Down)
    assert bar.list.currentRow() == 3  # (headers are skipped)
    QTest.keyClick(bar.edit, Qt.Key.Key_Return)
    assert panel.current_page_id() == other and panel.btn_back.isVisible()
    note = next(n for n in board.items_list() if isinstance(n, NoteItem) and n.style != "title")
    assert board._find_matches[board._find_index]["item"] is note
    view = board.mapToScene(board.viewport().rect()).boundingRect()
    assert view.intersects(note.sceneBoundingRect())
    panel.go_back()
    assert panel.current_page_id() == here


def test_the_pages_sidebar_finds_pages_by_whats_on_them(qapp, panel):
    board = panel.board
    other = panel.new_page(name="Armor references")
    make_note(qapp, board, QPointF(0, 0), "Shoulder strap rivets: brass")
    panel.save_now()
    panel.new_page(name="Mage costume")
    pages = panel.pages
    pages.search.setText("rivets")
    row = pages.item_for("page", other)
    assert row.isHidden()  # (its name doesn't have it: until what's on pages is searched)
    assert wait_for(qapp, lambda: not row.isHidden())
    assert "Found on the page: “Shoulder strap rivets: brass”" in row.toolTip(0)
    assert pages.item_for("page", panel.current_page_id()).isHidden()
    pages.search.clear()
    assert not pages.item_for("page", panel.current_page_id()).isHidden() and "Found" not in row.toolTip(0)


# ----------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------
def test_the_built_in_templates_make_real_pages(qapp, panel):
    from ui import artbook_templates
    from ui.artbook_board import ColumnItem, NoteItem, TableItem

    board = panel.board
    folder = store.create_folder("Mentorship")
    for kind, name, _about in artbook_templates.BUILT_INS:
        panel.create_page("built_in", kind, f"My {name}", folder)
        assert panel.page["name"] == f"My {name}" and store.page_folder(panel.page["id"]) == folder
        title = board.title_note()
        assert title is not None and title.toPlainText() == f"My {name}" and title.about
        columns = [i for i in board.items_list() if isinstance(i, ColumnItem)]
        assert columns and all(board.item(m) is not None for c in columns for m in c.members)
        for column in columns:  # (its cards laid out inside it)
            for card in board.column_cards(column):
                assert column.sceneBoundingRect().contains(card.sceneBoundingRect().center())
    board_kinds = {type(i).__name__ for i in board.items_list()}
    assert board_kinds == {"NoteItem", "ColumnItem"}  # (Review notes: columns of notes and to-dos)
    panel.create_page("built_in", "project", "Hunter", None)
    table = next(i for i in board.items_list() if isinstance(i, TableItem))
    assert [c[0] for c in table.columns] == ["Task", "Time", "Status"] and table.rows[0] == ["Blockout", "", "To do"]
    todos = [n for n in board.items_list() if isinstance(n, NoteItem) and n.check_rows()]
    assert [n.toPlainText().split("\n")[0] for n in todos] == ["Blockout", "Details", "Now"]
    assert board.title_note().stats == "4 cards \u00b7 0 of 5 to-dos done"


def test_the_new_page_window_shows_every_template_with_a_preview(qapp, panel):
    from core import artbook_templates as templates
    from ui.artbook_templates import NewPageDialog

    panel.new_page(name="Mage costume")
    make_note(qapp, panel.board, QPointF(0, 300), "Straps: two thin ones")
    panel.save_now()
    templates.save_template(panel.page["id"], "Costume layout")
    dialog = NewPageDialog(None, "Mentorship")
    try:
        tiles = [dialog.list.item(n) for n in range(dialog.list.count())]
        assert [t.text().split("\n")[0] for t in tiles] == ["Blank", "Project board", "Mood board", "Character sheet",
                                                              "Review notes", "Costume layout"]
        assert tiles[-1].text().endswith("Yours: from \u201cMage costume\u201d")
        assert all(not t.icon().isNull() for t in tiles) and dialog.choice() == ("blank", "")
        assert dialog.btn_create.toolTip() and dialog.name().startswith("Page ")
        dialog.list.setCurrentRow(5)
        dialog.edit.setText("  Hunter   armor ")
        assert dialog.choice() == ("yours", templates.list_templates()[0]["id"]) and dialog.name() == "Hunter armor"
    finally:
        dialog.deleteLater()


def test_a_page_saved_as_a_template_starts_new_pages(qapp, panel, tmp_path):
    from PIL import Image

    from core import artbook_templates as templates
    from ui.artbook_board import ImageItem, NoteItem

    board = panel.board
    page_id = panel.new_page(name="Mage costume")
    picture = tmp_path / "ref.png"
    Image.new("RGB", (40, 30), "red").save(picture)
    board.add_files([picture])
    straps = make_todo(qapp, board, "Straps", ("Top straps", "Buckle"))
    board.toggle_check(straps, straps.check_rows()[0].blockNumber())
    board.open_comments(straps)
    board.post_comment(board._open_thread["id"], "Too thick")
    board.close_comments()
    assert wait_for(qapp, lambda: any(isinstance(i, ImageItem) for i in board.items_list()))
    panel.save_now()
    entry = templates.save_template(page_id, "Costume layout", keep_pictures=False, untick=True)
    assert [e["name"] for e in templates.list_templates()] == ["Costume layout"] and templates.thumbnail(entry["id"])
    folder = store.create_folder("Mentorship")
    panel.create_page("yours", entry["id"], "Hunter armor", folder)
    assert wait_for(qapp, lambda: panel.page["name"] == "Hunter armor")
    assert store.page_folder(panel.page["id"]) == folder and panel.page["id"] != page_id
    assert not any(isinstance(i, ImageItem) for i in board.items_list())  # (pictures left out)
    todo = next(n for n in board.items_list() if isinstance(n, NoteItem) and n.check_rows())
    assert todo.progress() == (0, 2) and board.page_comments() == []  # (unticked; no comments)
    assert board.title_note().toPlainText() == "Hunter armor"
    templates.rename_template(entry["id"], "Costume")
    assert templates.list_templates()[0]["name"] == "Costume"
    templates.delete_template(entry["id"])
    assert templates.list_templates() == []


def test_new_page_asks_for_a_name_and_template_and_your_templates_move_with_your_pages(qapp, panel, monkeypatch, tmp_path):
    from core import artbook_templates as templates
    from ui import artbook_templates

    asked = []
    monkeypatch.setattr(artbook_templates, "ask_new_page", lambda parent, folder: asked.append(folder) or ("built_in", "mood", "Moods"))
    folder = store.create_folder("Refs")
    panel.pages.rebuild()
    panel.pages.new_page(folder)
    assert asked == ["Refs"] and panel.page["name"] == "Moods" and store.page_folder(panel.page["id"]) == folder
    monkeypatch.setattr(artbook_templates, "ask_new_page", lambda parent, folder: None)  # (cancelled)
    count = len(store.list_pages())
    panel.pages.new_page(None)
    assert len(store.list_pages()) == count
    panel.save_now()
    templates.save_template(panel.page["id"], "Moods")
    store.move_pages(tmp_path / "moved")
    assert [e["name"] for e in templates.list_templates()] == ["Moods"]
    assert templates.folder() == tmp_path / "moved" / store.TEMPLATES_FOLDER


# ----------------------------------------------------------------------
# Exporting
# ----------------------------------------------------------------------
def make_export_page(qapp, panel, tmp_path):
    """A page with a header, two notes, a link card, a file card and a comment."""
    board = panel.board
    panel.new_page(name="Mage costume")
    hood = make_note(qapp, board, QPointF(0, 300), "Hood")
    straps = make_note(qapp, board, QPointF(600, 300), "Straps")
    model = tmp_path / "Mage_Bust.fbx"
    model.write_bytes(b"fbx")
    board.add_link_entries([({"url": "https://example.org/strap-study", "title": "Strap study", "site": "example.org"}, None, None)],
                           QPointF(0, 700))
    board.add_files([model], QPointF(600, 700))
    assert wait_for(qapp, lambda: len(board.cards()) == 2)
    board.open_comments(hood)
    board.post_comment(board._open_thread["id"], "Rim too thick")
    board.close_comments()
    board._scene.clearSelection()
    return board, hood, straps


def test_what_an_export_takes_its_size_and_background(qapp, panel, tmp_path):
    board, hood, straps = make_export_page(qapp, panel, tmp_path)
    title = board.title_note()
    everything, bounds, density, _r = board.export_plan("page", "2")
    assert title in everything and len(everything) == 5 and density == 2.0
    no_title, _b, _d, _r = board.export_plan("page", "1", title=False)
    assert title not in no_title and len(no_title) == 4
    hood.setSelected(True)
    assert board.export_plan("auto")[0] == [hood] and board.export_plan("selection")[0] == [hood]
    assert len(board.export_plan("page")[0]) == 5  # (the whole page, though something's selected)
    board.set_view(1.0, hood.sceneBoundingRect().center())
    in_view, view_bounds, _d, _r = board.export_plan("view", "1")
    assert hood in in_view and view_bounds == board.mapToScene(board.viewport().rect()).boundingRect()
    board._scene.clearSelection()
    one = board.export_image({}, "page", "1", background="white")
    two = board.export_image({}, "page", "2", background="clear")
    assert abs(two.width() - 2 * one.width()) <= 2
    assert one.pixelColor(1, 1).name() == "#ffffff" and two.pixelColor(1, 1).alpha() == 0
    pinned = board.export_image({}, "page", "1", background="white", pins=True)
    assert pinned.width() == one.width() and pinned.height() > one.height()  # (the comments listed under it)
    assert board.export_comment_lines(board.export_threads(everything, bounds))[0][0] == "1"


def test_a_pdf_export_keeps_cards_clickable_and_lists_the_comments(qapp, panel, tmp_path):
    import re

    board, _hood, _straps = make_export_page(qapp, panel, tmp_path)
    path = tmp_path / "page.pdf"
    assert board.export_pdf(path, {}, "page", pins=True, links=True)
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    uris = re.findall(rb"/URI\s*\(([^)]*)\)", data)
    assert b"https://example.org/strap-study" in uris and any(u.endswith(b"Mage_Bust.fbx") for u in uris)
    assert len(re.findall(rb"/Type\s*/Page\b", data)) == 2  # (the comments on a second page)
    card = next(c for c in board.cards() if c.kind == "link")
    _items, bounds, _d, _r = board.export_plan("page")
    rects = [list(map(float, r.split())) for r in re.findall(rb"/Rect\s*\[([^\]]*)\]", data)]
    left = card.sceneBoundingRect().left() - bounds.left()
    assert any(abs(r[0] - left) < 3 and r[2] - r[0] > card.sceneBoundingRect().width() * 0.9 for r in rects)
    plain = tmp_path / "plain.pdf"
    board.export_pdf(plain, {}, "page", pins=False, links=False)
    assert b"/URI" not in plain.read_bytes() and len(re.findall(rb"/Type\s*/Page\b", plain.read_bytes())) == 1


def test_the_panel_exports_png_jpg_pdf_and_copies(qapp, panel, tmp_path, monkeypatch):
    from PIL import Image
    from PySide6.QtWidgets import QApplication

    from core.settings import set_artbook_export

    board, _hood, _straps = make_export_page(qapp, panel, tmp_path)
    asked = []
    monkeypatch.setattr(panel, "_ask_save_path", lambda title, stem, suffix, _filter: asked.append((title, stem)) or tmp_path / f"out{suffix}")
    set_artbook_export(background="clear", size="1")
    panel.export("png")
    assert wait_for(qapp, lambda: (tmp_path / "out.png").is_file()) and asked[0] == ("Export as PNG", "Mage costume")
    panel.export("jpg")
    assert wait_for(qapp, lambda: (tmp_path / "out.jpg").is_file())
    assert (tmp_path / "out.jpg").read_bytes()[:2] == b"\xff\xd8"
    assert Image.open(tmp_path / "out.jpg").getpixel((1, 1)) == (255, 255, 255)  # (see-through: white in a JPG)
    panel.export("pdf")
    assert wait_for(qapp, lambda: (tmp_path / "out.pdf").is_file())
    QApplication.clipboard().clear()
    panel.export("copy")
    assert wait_for(qapp, lambda: not QApplication.clipboard().image().isNull())


def test_the_export_dialog_remembers_its_choices(qapp, panel, tmp_path):
    from core.settings import get_artbook_export
    from ui.artbook_export import ExportDialog, settings_text

    board, hood, _straps = make_export_page(qapp, panel, tmp_path)
    dialog = ExportDialog(board)
    try:
        assert not dialog.rb_selection.isEnabled() and dialog.rb_page.isChecked()
        assert dialog.preview.pixmap() is not None and not dialog.preview.pixmap().isNull()
        assert dialog.size_note.text().endswith("px") and dialog.cb_title.isEnabled() and dialog.cb_pins.isEnabled()
        assert all(b.toolTip() for b in dialog.formats.buttons() + dialog.sizes.buttons() + dialog.backgrounds.buttons())
        dialog.set_choice(dialog.backgrounds, "clear")
        dialog.set_choice(dialog.formats, "jpg")
        clear = next(b for b in dialog.backgrounds.buttons() if b.property("value") == "clear")
        assert not clear.isEnabled() and dialog.options()["background"] == "white" and not dialog.cb_links.isEnabled()
        dialog.set_choice(dialog.formats, "pdf")
        dialog.refresh()
        assert dialog.cb_links.isEnabled() and " in: " in dialog.size_note.text() and dialog.btn_export.text() == "Export PDF..."
        dialog.set_choice(dialog.sizes, "4")
        dialog.cb_pins.setChecked(True)
        dialog._on_export()
        saved = get_artbook_export()
        assert (saved["format"], saved["size"], saved["background"], saved["pins"], saved["area"]) == ("pdf", "4", "white", True, "page")
        assert settings_text() == "Whole page · 4× · white · with the title · comment pins"
    finally:
        dialog.deleteLater()
    hood.setSelected(True)
    dialog = ExportDialog(board)
    try:
        assert dialog.rb_selection.isEnabled() and dialog.rb_selection.text() == "The selection (1 thing)"
    finally:
        dialog.deleteLater()


def test_the_quick_export_menu_and_keys(qapp, panel, tmp_path):
    from ui.artbook_export import settings_text

    board, _hood, _straps = make_export_page(qapp, panel, tmp_path)
    assert panel.btn_export.toolTip().startswith("Export:")
    panel._fill_export_menu()
    texts = [a.text() for a in panel.export_menu.actions() if not a.isSeparator()]
    assert texts[:4] == ["Export as PNG...", "Export as JPG...", "Export as PDF...", "Copy as Image"]
    assert texts[-3:] == [settings_text(), "Export Options...", "Export Page (.artbook)..."]
    asked = []
    board.export_requested.disconnect()
    board.export_requested.connect(lambda fmt, area: asked.append((fmt, area)))
    board.setFocus()
    QTest.keySequence(board, "Ctrl+E")
    QTest.keySequence(board, "Ctrl+Shift+E")
    QTest.keySequence(board, "Ctrl+Shift+C")
    assert asked == [("png", ""), ("options", ""), ("copy", "")]
