"""Where the Artbook's pages are kept: moving them to another folder (copy first, then delete),
showing the pages already in a folder, and Settings > Artbook."""

from __future__ import annotations

import shutil
import time

import pytest

from core import artbook_store as store
from core import settings


def pump(qapp, condition=lambda: False, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def page_folders(folder):
    return sorted(c.name for c in folder.iterdir() if (c / "page.json").is_file()) if folder.is_dir() else []


def test_move_pages_copies_then_switches(tmp_path):
    first = store.create_page("One")
    second = store.create_page("Two")
    source = store.pages_dir()
    target = tmp_path / "elsewhere" / "pages"
    assert store.move_pages(target) == 2
    assert store.pages_dir() == target
    assert page_folders(target) == sorted([first["id"], second["id"]])
    assert (target / "index.json").is_file()
    assert page_folders(source) == [] and not (source / "index.json").exists()
    assert [p["name"] for p in store.list_pages()] == ["One", "Two"]
    assert store.ordered_page_ids() == [first["id"], second["id"]]


def test_move_refuses_a_folder_with_pages_and_the_same_folder(tmp_path):
    store.create_page("One")
    other = tmp_path / "other"
    (other / "abc").mkdir(parents=True)
    (other / "abc" / "page.json").write_text("{}", encoding="utf-8")
    with pytest.raises(store.PageError):
        store.move_pages(other)
    with pytest.raises(store.PageError):
        store.move_pages(store.pages_dir())
    assert len(store.list_pages()) == 1


def test_a_failed_copy_leaves_the_pages_where_they_were(tmp_path, monkeypatch):
    pages = [store.create_page(name)["id"] for name in ("One", "Two", "Three")]
    source = store.pages_dir()
    calls = []
    real_copytree = shutil.copytree

    def flaky_copytree(src, dst, **kwargs):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("disk full")
        return real_copytree(src, dst, **kwargs)

    monkeypatch.setattr(store.shutil, "copytree", flaky_copytree)
    target = tmp_path / "full-disk"
    with pytest.raises(store.PageError, match="disk full"):
        store.move_pages(target)
    assert store.pages_dir() == source
    assert page_folders(source) == sorted(pages)
    assert page_folders(target) == []  # the copy that made it was cleaned up


def test_pages_target_keeps_pages_out_of_a_busy_folder(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert store.pages_target(empty) == empty
    assert store.pages_target(tmp_path / "new") == tmp_path / "new"
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("mine", encoding="utf-8")
    assert store.pages_target(busy) == busy / store.PAGES_SUBFOLDER
    with_pages = tmp_path / "with-pages"
    with_pages.mkdir()
    (with_pages / "index.json").write_text("{}", encoding="utf-8")
    (with_pages / "notes.txt").write_text("mine", encoding="utf-8")
    assert store.pages_target(with_pages) == with_pages


@pytest.fixture
def panel(qapp):
    from ui.artbook import ArtbookPanel

    widget = ArtbookPanel()
    widget.resize(900, 600)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()


def test_settings_page_moves_the_pages_and_keeps_the_open_one(qapp, panel, tmp_path):
    from ui.settings_dialog import ArtbookPage

    other = panel.new_page()
    panel.open_page(other)
    page = ArtbookPage(panel)
    target = tmp_path / "moved"
    page.change_to(target, ask=False)
    assert pump(qapp, lambda: not page.is_busy())
    assert store.pages_dir() == target
    assert settings.get_pages_dir() == str(target)
    assert panel.current_page_id() == other
    assert other in page_folders(target)
    assert "Moved 2 pages" in page.lbl_status.text()
    page.deleteLater()


def test_settings_page_shows_the_pages_already_in_a_folder(qapp, panel, tmp_path):
    from ui.settings_dialog import ArtbookPage

    home = store.pages_dir()
    mine = panel.current_page_id()
    store.set_pages_dir(tmp_path / "shared")
    theirs = store.create_page("Theirs")["id"]
    store.set_pages_dir(home)
    page = ArtbookPage(panel)
    page.change_to(tmp_path / "shared", ask=False)
    assert store.pages_dir() == tmp_path / "shared"
    assert panel.current_page_id() == theirs  # (the open page isn't there: the first one shows)
    assert mine in page_folders(home)  # the old pages stay put
    page.change_to(home, ask=False)  # and switching back shows them again
    assert panel.current_page_id() == mine
    page.deleteLater()


def test_use_default_stores_no_folder(qapp, panel, tmp_path, monkeypatch):
    from ui.settings_dialog import ArtbookPage

    monkeypatch.setattr(store, "DEFAULT_PAGES_DIR", store.pages_dir())  # (the test's own "default")
    page = ArtbookPage(panel)
    assert not page.btn_default.isEnabled()
    page.change_to(tmp_path / "away", ask=False)
    assert pump(qapp, lambda: not page.is_busy())
    assert page.btn_default.isEnabled()
    page.change_to(store.DEFAULT_PAGES_DIR, ask=False)
    assert pump(qapp, lambda: not page.is_busy())
    assert settings.get_pages_dir() == ""
    assert store.pages_dir() == store.DEFAULT_PAGES_DIR
    page.deleteLater()
