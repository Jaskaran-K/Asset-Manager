"""The Artbook's Pages: the index (folders, order, self-repair), page operations, thumbnails,
and the Pages sidebar (opening, renaming, moving, searching, deleting)."""

from __future__ import annotations

import json
import shutil
import time

import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView

from core import artbook_store as store
from ui.animations import set_motion_mode

Position = QAbstractItemView.DropIndicatorPosition


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
def trash(monkeypatch):
    """The Recycle Bin, faked: what was sent there (moved to a folder of the test's own)."""
    sent = []

    def move_to_trash(path):
        sent.append(path)
        shutil.rmtree(path)
        return True

    monkeypatch.setattr(store.QFile, "moveToTrash", staticmethod(move_to_trash))
    return sent


@pytest.fixture
def picture(tmp_path):
    path = tmp_path / "red.png"
    Image.new("RGB", (400, 200), "red").save(path)
    return path


@pytest.fixture
def panel(qapp, monkeypatch):
    from ui import artbook_pages
    from ui.artbook import ArtbookPanel

    monkeypatch.setattr(artbook_pages, "confirm", lambda *_args: True)
    widget = ArtbookPanel()
    widget.resize(1000, 700)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()


def names(panel):
    """The Pages list as text: folders as 'name/', children indented."""
    rows = []

    def visit(item, depth):
        kind = item.data(0, Qt.ItemDataRole.UserRole)[0]
        hidden = " (hidden)" if item.isHidden() else ""
        rows.append("  " * depth + item.text(0) + ("/" if kind == "folder" else "") + hidden)
        for i in range(item.childCount()):
            visit(item.child(i), depth + 1)

    root = panel.pages.tree.invisibleRootItem()
    for i in range(root.childCount()):
        visit(root.child(i), 0)
    return rows


def add_image(qapp, panel, path):
    before = len(panel.board.items_list())
    panel.board.add_files([path])
    assert pump(qapp, lambda: len(panel.board.items_list()) == before + 1)


# ----------------------------------------------------------------------
# The index
# ----------------------------------------------------------------------
def test_index_repairs_itself(qapp):
    a, b, c = store.new_page("A"), store.new_page("B"), store.new_page("C")  # not filed in the index
    folder = store.create_folder("Refs")
    tree = store.page_tree()
    assert tree["pages"] == [a["id"], b["id"], c["id"]]  # unlisted pages show at the top level, oldest first
    store.move_page(b["id"], folder)

    # A damaged index: a gone page, a page listed twice, junk, a folder listed twice
    index = json.loads(store.index_path().read_text(encoding="utf-8"))
    index["pages"] += ["gone", a["id"], 42]
    index["folders"].append(dict(index["folders"][0]))
    index["folders"].append("junk")
    store.index_path().write_text(json.dumps(index), encoding="utf-8")
    tree = store.page_tree()
    assert tree["pages"] == [a["id"], c["id"]]
    assert [f["id"] for f in tree["folders"]] == [folder] and tree["folders"][0]["pages"] == [b["id"]]

    store.index_path().write_text("not json", encoding="utf-8")  # unreadable: everything at the top level
    assert set(store.page_tree()["pages"]) == {a["id"], b["id"], c["id"]}


def test_folders_nest_move_and_delete_into_their_parent(qapp):
    outer = store.create_folder("Outer")
    inner = store.create_folder("Inner", outer)
    deep = store.create_folder("Deep", inner)
    page = store.create_page("In inner", inner)
    other = store.create_folder("Other")
    with pytest.raises(store.PageError):
        store.move_folder(outer, deep)  # not into its own sub-folder
    with pytest.raises(store.PageError):
        store.create_folder("  ")

    store.rename_folder(inner, "  Renamed   inner ")
    store.delete_folder(inner)  # its folder and page move up into Outer, where it was
    tree = store.page_tree()
    (outer_node, other_node) = tree["folders"]
    assert [f["name"] for f in outer_node["folders"]] == ["Deep"]
    assert outer_node["pages"] == [page["id"]]
    store.move_folder(other, None, 0)
    assert [f["id"] for f in store.page_tree()["folders"]] == [other, outer]
    store.move_folder(deep, other)
    assert store.page_tree()["folders"][0]["folders"][0]["id"] == deep


def test_moving_pages_counts_positions_like_the_drop_indicator(qapp):
    ids = [store.create_page(name)["id"] for name in "ABCD"]
    store.move_page(ids[0], None, 3)  # A dropped above D (row 3): lands just before D
    assert store.page_tree()["pages"] == [ids[1], ids[2], ids[0], ids[3]]
    store.move_page(ids[3], None, 0)
    assert store.page_tree()["pages"] == [ids[3], ids[1], ids[2], ids[0]]
    folder = store.create_folder("F")
    store.move_page(ids[2], folder)
    assert store.page_folder(ids[2]) == folder
    assert store.ordered_page_ids() == [ids[2], ids[3], ids[1], ids[0]]  # folders come first


def test_duplicate_copies_images_and_sits_after_the_original(qapp):
    first = store.create_page("Mood")
    second = store.create_page("Other")
    blob = store.store_blob(first["id"], b"image data", ".png")
    first["items"] = [{"id": "x", "kind": "image", "source": {"path": None, "blob": blob}}]
    first["modified"] = time.time()
    store.save_page(first)
    copy = store.duplicate_page(first["id"])
    assert copy["name"] == "Mood copy" and copy["id"] != first["id"]
    assert store.blob_path(copy["id"], blob).read_bytes() == b"image data"
    assert store.load_page(copy["id"])["items"][0]["id"] == "x"
    assert store.page_tree()["pages"] == [first["id"], copy["id"], second["id"]]


def test_trashed_page_stays_gone(qapp, trash):
    page = store.create_page("Bye")
    stale = dict(page, modified=time.time())
    store.trash_page(page["id"])
    assert trash and not store.page_dir(page["id"]).exists()
    store.save_page(stale)  # an autosave that was in flight
    with pytest.raises(store.PageError):
        store.store_blob(page["id"], b"late image", ".png")
    store.save_thumbnail(page["id"], b"png")
    assert not store.page_dir(page["id"]).exists()
    assert page["id"] not in store.ordered_page_ids()


def test_an_older_save_never_replaces_a_newer_one(qapp):
    page = store.create_page("Order")
    newer = dict(page, name="Newer", modified=time.time() + 10)
    older = dict(page, name="Older", modified=time.time())
    store.save_page(newer)
    store.save_page(older)  # e.g. an autosave finishing after a save made on switching page
    assert store.load_page(page["id"])["name"] == "Newer"


def test_unique_page_names(qapp):
    assert store.unique_page_name() == "Page 1"
    store.create_page("Page 1")
    store.create_page("page 3")
    assert store.unique_page_name() == "Page 2"


# ----------------------------------------------------------------------
# The Pages sidebar
# ----------------------------------------------------------------------
def test_sidebar_lists_folders_and_pages(qapp, panel):
    folder = store.create_folder("Characters")
    store.create_page("Hero", folder)
    store.create_page("Loose")
    panel.pages.rebuild()
    assert names(panel) == ["Characters/", "  Hero", "Page 1", "Loose"]
    current = panel.pages.item_for("page", panel.page["id"])
    assert current.font(0).bold() and panel.pages.tree.currentItem() is current
    assert panel.sidebar.is_open() and panel.btn_pages.isChecked()


def test_clicking_a_page_saves_the_current_one_and_opens_it(qapp, panel, picture):
    first = panel.page["id"]
    other = store.create_page("Other")
    panel.pages.rebuild()
    add_image(qapp, panel, picture)
    panel.pages.tree.itemClicked.emit(panel.pages.item_for("page", other["id"]), 0)
    assert panel.page["id"] == other["id"] and panel.board.items_list() == []
    assert len(store.load_page(first)["items"]) == 1  # saved on the way out, with no autosave wait
    assert store.thumbnail_path(first).exists()  # and its picture for the list
    assert panel.pages.item_for("page", other["id"]).font(0).bold()

    panel.pages.tree.itemClicked.emit(panel.pages.item_for("page", first), 0)
    assert len(panel.board.items_list()) == 1  # the edit survived the switch


def test_renaming_in_place(qapp, panel):
    other = store.create_page("Other")
    panel.pages.rebuild()
    item = panel.pages.item_for("page", panel.page["id"])
    item.setText(0, "  My   board ")  # what the editor commits
    qapp.processEvents()
    assert panel.page["name"] == "My board" and "My board" in panel.lbl_page.text()
    assert store.load_page(panel.page["id"])["name"] == "My board"
    panel.pages.item_for("page", other["id"]).setText(0, "Renamed")
    qapp.processEvents()
    assert store.load_page(other["id"])["name"] == "Renamed"
    assert "Renamed" in names(panel)

    panel.pages.item_for("page", other["id"]).setText(0, "   ")  # an empty name is refused
    qapp.processEvents()
    assert store.load_page(other["id"])["name"] == "Renamed" and "Renamed" in names(panel)


def test_autosave_keeps_a_rename_and_refreshes_the_thumbnail(qapp, panel, picture):
    page_id = panel.page["id"]
    add_image(qapp, panel, picture)
    panel.rename_page(page_id, "Named")
    panel.board.flip(True)
    panel._save_in_background()
    assert pump(qapp, lambda: not panel._saving)
    assert store.load_page(page_id)["name"] == "Named"
    assert store.thumbnail_path(page_id).exists()
    thumb = panel.pages.item_for("page", page_id).icon(0)
    assert not thumb.isNull()


def test_new_page_and_folder_go_in_the_selected_folder(qapp, panel):
    folder = store.create_folder("Refs")
    panel.pages.rebuild()
    panel.pages.tree.setCurrentItem(panel.pages.item_for("folder", folder))
    panel.pages.btn_new_page.click()
    assert store.page_folder(panel.page["id"]) == folder  # created there and opened
    assert panel.page["name"] == "Page 2"
    assert panel.pages.tree.state() == QAbstractItemView.State.EditingState  # its name is ready to type
    panel.pages.tree.closePersistentEditor(panel.pages.tree.currentItem())

    panel.pages.btn_new_folder.click()  # the selection is now the new page: in its folder
    (sub,) = store.page_tree()["folders"][0]["folders"]
    assert sub["name"] == "New Folder"


def test_dragging_pages_and_folders(qapp, panel):
    folder = store.create_folder("Refs")
    second = store.create_folder("More")
    a = store.create_page("A")
    panel.pages.rebuild()
    pages = panel.pages
    pages.move_item(pages.item_for("page", a["id"]), pages.item_for("folder", folder), Position.OnItem)
    pump(qapp, seconds=0.1)
    assert store.page_folder(a["id"]) == folder
    assert names(panel) == ["Refs/", "  A", "More/", "Page 1"]

    pages.move_item(pages.item_for("page", a["id"]), pages.item_for("page", panel.page["id"]), Position.BelowItem)
    pump(qapp, seconds=0.1)
    assert names(panel) == ["Refs/", "More/", "Page 1", "A"]

    pages.move_item(pages.item_for("folder", second), pages.item_for("folder", folder), Position.AboveItem)
    pump(qapp, seconds=0.1)
    assert names(panel)[:2] == ["More/", "Refs/"]
    pages.move_item(pages.item_for("folder", folder), pages.item_for("folder", second), Position.OnItem)
    pump(qapp, seconds=0.1)
    assert names(panel)[:2] == ["More/", "  Refs/"]
    pages.move_item(pages.item_for("folder", second), pages.item_for("folder", folder), Position.OnItem)
    pump(qapp, seconds=0.1)  # into its own sub-folder: refused, nothing changes
    assert names(panel)[:2] == ["More/", "  Refs/"]


def test_search_filters_pages_and_keeps_folders_of_matches(qapp, panel):
    folder = store.create_folder("Characters")
    store.create_page("Hero sketch", folder)
    store.create_page("Villain", folder)
    store.create_page("Hero colors")
    panel.pages.rebuild()
    panel.pages.search.setText("hero")
    assert names(panel) == ["Characters/", "  Hero sketch", "  Villain (hidden)", "Page 1 (hidden)", "Hero colors"]
    panel.pages.search.setText("characters")  # a folder's name: all of it
    assert names(panel) == ["Characters/", "  Hero sketch", "  Villain", "Page 1 (hidden)", "Hero colors (hidden)"]
    panel.pages.search.clear()
    assert all("(hidden)" not in row for row in names(panel))


def test_folders_remember_being_collapsed(qapp, panel):
    folder = store.create_folder("Refs")
    store.create_page("Inside", folder)
    panel.pages.rebuild()
    panel.pages.item_for("folder", folder).setExpanded(False)
    assert store.page_tree()["folders"][0]["open"] is False
    panel.pages.rebuild()
    assert not panel.pages.item_for("folder", folder).isExpanded()


def test_deleting_the_open_page_opens_the_next(qapp, panel, trash):
    first = panel.page["id"]
    second = store.create_page("Second")
    panel.pages.rebuild()
    panel.pages.delete_page(first)
    assert panel.page["id"] == second["id"]
    assert not store.page_dir(first).exists() and len(trash) == 1
    assert names(panel) == ["Second"]

    panel.pages.delete_page(second["id"])  # the last one: a fresh page takes its place
    assert panel.page is not None and panel.page["id"] != second["id"]
    assert names(panel) == ["Page 1"]


def test_deleting_asks_first(qapp, panel, trash, monkeypatch):
    from ui import artbook_pages

    monkeypatch.setattr(artbook_pages, "confirm", lambda *_args: False)
    panel.pages.delete_page(panel.page["id"])
    assert not trash and store.page_dir(panel.page["id"]).exists()


def test_duplicating_the_open_page_copies_what_is_on_the_board(qapp, panel, picture):
    add_image(qapp, panel, picture)  # not autosaved yet
    panel.pages._duplicate(panel.page["id"])
    copy_id = next(p for p in store.ordered_page_ids() if p != panel.page["id"])
    assert len(store.load_page(copy_id)["items"]) == 1
    assert names(panel) == ["Page 1", "Page 1 copy"]


def test_sidebar_remembers_its_own_state(qapp, panel):
    from core.settings import get_sidebar_state

    panel.sidebar.set_pinned(False)
    panel.sidebar.set_sidebar_width(300)
    assert get_sidebar_state("artbook_sidebar")["pinned"] is False
    assert get_sidebar_state("artbook_sidebar")["width"] == 300
    assert get_sidebar_state()["pinned"] is True  # the library's sidebar is separate


# ----------------------------------------------------------------------
# Ctrl+B in the main window: the side you're working in
# ----------------------------------------------------------------------
def test_ctrl_b_toggles_the_sidebar_of_the_focused_side(qapp):
    from PySide6.QtTest import QTest

    import main

    win = main.MainWindow()
    win.resize(1600, 800)
    win.show()
    win.activateWindow()
    QTest.qWaitForWindowActive(win)
    pump(qapp, seconds=0.2)
    win.view_modes.set_mode("split")
    pump(qapp, seconds=0.2)
    try:
        assets, pages = win.sidebar.is_open(), win.artbook_panel.sidebar.is_open()
        win.artbook_panel.board.setFocus()
        QTest.keyClick(win.artbook_panel.board, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
        assert win.artbook_panel.sidebar.is_open() != pages and win.sidebar.is_open() == assets
        win.btn_sidebar.setFocus()  # anything on the asset side
        QTest.keyClick(win.btn_sidebar, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
        assert win.sidebar.is_open() != assets and win.artbook_panel.sidebar.is_open() != pages

        win.view_modes.set_mode("artbook")  # the asset side is hidden: focus moves to the board
        pump(qapp, seconds=0.1)
        assert win.artbook_panel.isAncestorOf(qapp.focusWidget())
        win.view_modes.set_mode("asset")
        pump(qapp, seconds=0.1)
        assert win.asset_panel.isAncestorOf(qapp.focusWidget())
    finally:
        win.close()
        pump(qapp, seconds=0.2)


def test_the_pencil_renames_without_opening_or_closing(qapp, panel):
    from PySide6.QtTest import QTest

    folder = store.create_folder("Refs")
    store.create_page("Inside", folder)
    panel.pages.rebuild()
    tree = panel.pages.tree
    item = panel.pages.item_for("folder", folder)
    tree.setCurrentItem(item)  # the selected row shows the pencil
    index = tree.indexFromItem(item)
    spot = tree.pencil_rect(index).center()
    assert tree._pencil_at(spot) == index
    QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, spot)
    assert tree.state() == QAbstractItemView.State.EditingState
    assert item.isExpanded()  # not toggled
    tree.closePersistentEditor(item)

    # A plain click on the folder opens / closes it; a double-click doesn't rename it
    away = tree.visualItemRect(item).center()
    away.setX(60)
    QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, away)
    assert not item.isExpanded()
    panel.pages._on_item_double_clicked(item, 0)
    assert tree.state() != QAbstractItemView.State.EditingState
