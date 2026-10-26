"""Undo: tag changes are undone exactly, Settings > Tags Cancel really cancels, Ctrl+Z / Ctrl+Y work."""

from __future__ import annotations

import pytest

from core.tag_repository import Marks, TagRepository
from tests.test_tag_repository import make_asset


@pytest.fixture
def lib():
    repo = TagRepository(":memory:")
    ids = repo.sync_assets([make_asset(p) for p in (r"C:\a.png", r"C:\b.png", r"C:\c.png")])
    return repo, [ids[r"C:\a.png"], ids[r"C:\b.png"], ids[r"C:\c.png"]]


def custom(repo, asset_id):
    return sorted(t.name for t in repo.tags_by_asset([asset_id])[asset_id] if not t.is_default)


def test_undo_restores_each_file_exactly(lib):
    repo, (a, b, c) = lib
    hero, wip = repo.create_custom_tag("Hero"), repo.create_custom_tag("WIP")
    repo.add_tags([a], [hero.id])        # a already had Hero
    repo.add_tags([b], [wip.id])
    change = repo.apply_tag_changes([a, b, c], [hero.id], [wip.id])  # bulk: Hero on all, WIP off all
    assert set(change.added) == {(b, hero.id), (c, hero.id)} and change.removed == ((b, wip.id),)

    repo.undo_tag_change(change)
    assert (custom(repo, a), custom(repo, b), custom(repo, c)) == (["Hero"], ["WIP"], [])
    repo.redo_tag_change(change)
    assert (custom(repo, a), custom(repo, b), custom(repo, c)) == (["Hero"], ["Hero"], ["Hero"])


def test_undo_skips_tags_deleted_since(lib):
    repo, (a, _b, _c) = lib
    hero = repo.create_custom_tag("Hero")
    change = repo.apply_tag_changes([a], [hero.id], [])
    repo.apply_tag_changes([a], [], [hero.id])
    repo.delete_custom_tag(hero.id)
    repo.redo_tag_change(change)  # the tag is gone: nothing to put back, and no error
    assert custom(repo, a) == []


def test_tags_page_cancel_undoes_everything(lib, qapp, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from ui.settings_dialog import TagsPage

    repo, (a, b, _c) = lib
    keep = repo.create_custom_tag("Keep", "#ff0000")
    old = repo.create_custom_tag("Old")
    repo.add_tags([a, b], [old.id])
    page = TagsPage(repo)

    answers = iter([("Brand new", True), ("Renamed", True)])
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    page._on_new()                                    # create "Brand new"
    page._current_custom = lambda: repo.get_tag(keep.id)
    page._on_rename()                                 # Keep -> Renamed
    repo.set_tag_color(keep.id, "#00ff00")            # (as _on_color would, plus its undo)
    page._undo.append(lambda: repo.set_tag_color(keep.id, "#ff0000"))
    page._current_custom = lambda: repo.get_tag(old.id)
    page._on_delete()                                 # delete Old (on 2 files)
    assert sorted(t.name for t in repo.list_tags(kind="custom")) == ["Brand new", "Renamed"]

    page.revert()                                     # Cancel
    tags = {t.name: t for t in repo.list_tags(kind="custom")}
    assert sorted(tags) == ["Keep", "Old"]
    assert tags["Keep"].color == "#ff0000" and tags["Old"].id == old.id and tags["Old"].file_count == 2
    assert custom(repo, a) == ["Old"]


def test_main_window_undo_redo(tmp_path, qapp):
    from tests.test_main_window import open_window, press, select
    from PySide6.QtCore import Qt
    from core.settings import set_scan_folders

    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "a.png").write_bytes(b"x")
    set_scan_folders([tmp_path / "lib"])
    window = open_window(qapp)
    select(window, "a.png")
    asset_id = window._asset_ids[str((tmp_path / "lib" / "a.png").resolve())]
    press(window, Qt.Key.Key_4, "4")
    press(window, Qt.Key.Key_F, "f")
    assert window.repo.marks_by_asset([asset_id]) == {asset_id: Marks(True, 4)}
    assert window.action_undo.text() == "Undo favorite on 1 file"

    window._on_undo()
    assert window.repo.marks_by_asset([asset_id]) == {asset_id: Marks(False, 4)}
    window._on_undo()
    assert window.repo.marks_by_asset([asset_id]) == {} and not window.action_undo.isEnabled()
    window._on_redo()
    assert window._asset_marks[asset_id] == Marks(False, 4)
    window.close()
