"""Tags follow files that are moved or renamed outside the app (core.tag_repository relinking)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from core.scanner import AssetScanner
from core.tag_repository import TagRepository


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "library"
    (root / "props").mkdir(parents=True)
    (root / "props" / "chair.fbx").write_bytes(b"chair mesh " * 50_000)  # > 128 KB: hashed by its edges
    (root / "props" / "lamp.fbx").write_bytes(b"lamp mesh")
    (root / "props" / "table.fbx").write_bytes(b"table mesh " * 10)
    return root


def scan(root: Path):
    return AssetScanner().scan(root)


def sync(repo, root):
    """What ScanWorker does after each scan; returns ({file name: id}, relinked count)."""
    assets = scan(root)
    relinked = repo.relink_moved_assets(assets)
    ids = repo.sync_assets(assets)
    repo.fill_missing_hashes()
    return {Path(p).name: i for p, i in ids.items()}, relinked


def custom_tags(repo, asset_id):
    return [t.name for t in repo.tags_by_asset([asset_id])[asset_id] if not t.is_default]


@pytest.fixture
def tagged(lib):
    repo = TagRepository(":memory:")
    ids, _ = sync(repo, lib)
    hero = repo.create_custom_tag("Hero")
    repo.add_tags([ids["chair.fbx"], ids["lamp.fbx"]], [hero.id])
    assert repo.fill_missing_hashes() == 2  # as main.py does right after tagging
    return repo, ids


def test_moved_and_renamed_files_keep_their_tags(lib, tagged):
    repo, ids = tagged
    (lib / "furniture").mkdir()
    shutil.move(lib / "props" / "chair.fbx", lib / "furniture" / "Chair_v2.fbx")  # moved and renamed
    os.rename(lib / "props" / "lamp.fbx", lib / "props" / "lamp_old.fbx")  # renamed in place

    new_ids, relinked = sync(repo, lib)
    assert relinked == 2
    assert new_ids["Chair_v2.fbx"] == ids["chair.fbx"]  # same row, so tags, favorite and rating follow
    assert custom_tags(repo, new_ids["Chair_v2.fbx"]) == ["Hero"]
    assert custom_tags(repo, new_ids["lamp_old.fbx"]) == ["Hero"]
    assert "FBX" in [t.name for t in repo.tags_by_asset([new_ids["Chair_v2.fbx"]])[new_ids["Chair_v2.fbx"]]]


def test_untagged_or_edited_files_are_not_relinked(lib, tagged):
    repo, _ids = tagged
    os.rename(lib / "props" / "table.fbx", lib / "props" / "desk.fbx")  # no custom tags: nothing to carry
    moved = lib / "props" / "lamp2.fbx"
    os.rename(lib / "props" / "lamp.fbx", moved)
    moved.write_bytes(b"lamp mesh, edited")  # content changed: can't be sure it's the same file
    new_ids, relinked = sync(repo, lib)
    assert relinked == 0
    assert custom_tags(repo, new_ids["lamp2.fbx"]) == []


def test_two_identical_copies_are_ambiguous(lib, tagged):
    repo, _ids = tagged
    shutil.copy(lib / "props" / "lamp.fbx", lib / "lamp_a.fbx")
    shutil.move(lib / "props" / "lamp.fbx", lib / "lamp_b.fbx")
    new_ids, relinked = sync(repo, lib)
    assert relinked == 0  # left untagged rather than guessing which copy is "the" lamp
    assert custom_tags(repo, new_ids["lamp_a.fbx"]) == custom_tags(repo, new_ids["lamp_b.fbx"]) == []


def test_files_on_offline_drives_are_not_treated_as_moved(lib, tagged, monkeypatch):
    repo, ids = tagged
    # Pretend the library's drive is offline: the old path and its drive root both "don't exist"
    import core.tag_repository as tr
    monkeypatch.setattr(tr.os.path, "exists", lambda p: False)
    assert repo.relink_moved_assets([]) == 0


def test_v1_database_upgrades_in_place(tmp_path):
    import sqlite3

    from core.tag_repository import MIGRATIONS

    db = tmp_path / "library.db"
    conn = sqlite3.connect(db)
    conn.executescript(f"BEGIN; {MIGRATIONS[0]} PRAGMA user_version = 1; COMMIT;")
    conn.execute("INSERT INTO assets (path_key, path, name, name_fold, extension) VALUES ('k', 'C:\\a.png', 'a.png', 'a.png', '.png')")
    conn.commit()
    conn.close()
    repo = TagRepository(db)
    assert repo.schema_version() == len(MIGRATIONS)
    row = repo._conn().execute("SELECT name, size, mtime, quick_hash FROM assets").fetchone()
    assert tuple(row) == ("a.png", 0, 0, None)
    repo.close_thread_connection()


def test_editing_a_file_refreshes_its_fingerprint(lib, tagged):
    repo, ids = tagged
    chair = lib / "props" / "chair.fbx"
    before = repo._conn().execute("SELECT quick_hash FROM assets WHERE id = ?", (ids["chair.fbx"],)).fetchone()[0]
    chair.write_bytes(b"new chair " * 60_000)
    os.utime(chair, (1_700_000_000, 1_700_000_000))
    sync(repo, lib)
    after = repo._conn().execute("SELECT quick_hash FROM assets WHERE id = ?", (ids["chair.fbx"],)).fetchone()[0]
    assert after and after != before
