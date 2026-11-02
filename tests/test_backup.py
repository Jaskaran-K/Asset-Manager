"""Library backups (automatic and manual), restore, and JSON tag export / import."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from core import tag_repository
from core.tag_repository import MIGRATIONS, TagError, TagRepository
from tests.test_tag_repository import make_asset


@pytest.fixture
def repo(tmp_path):
    repo = TagRepository(tmp_path / "library.db")
    yield repo
    repo.close_thread_connection()


def custom_names(repo, asset_id):
    return sorted(t.name for t in repo.tags_by_asset([asset_id])[asset_id] if not t.is_default)


def test_upgrading_an_old_database_backs_it_up_first(tmp_path):
    db = tmp_path / "library.db"
    conn = sqlite3.connect(db)
    conn.executescript(f"BEGIN; {MIGRATIONS[0]} PRAGMA user_version = 1; COMMIT;")
    conn.close()
    repo = TagRepository(db)
    assert repo.schema_version() == len(MIGRATIONS)
    backups = repo.list_backups()
    assert len(backups) == 1 and "before-upgrade-v1" in backups[0].name
    old = sqlite3.connect(backups[0])
    assert old.execute("PRAGMA user_version").fetchone()[0] == 1  # the copy is the untouched old version
    old.close()
    repo.close_thread_connection()


def test_new_database_needs_no_backup(repo):
    repo.create_custom_tag("Hero")
    assert repo.list_backups() == []


def test_backups_are_pruned(repo, monkeypatch):
    monkeypatch.setattr(tag_repository, "MAX_BACKUPS", 3)
    for i in range(5):
        repo.backup_database(f"n{i}")
    assert len(repo.list_backups()) == 3


def test_restore_brings_back_the_old_state(repo):
    ids = repo.sync_assets([make_asset(r"C:\lib\hero.fbx")])
    hero_id = ids[r"C:\lib\hero.fbx"]
    tag = repo.create_custom_tag("Hero")
    repo.add_tags([hero_id], [tag.id])
    backup = repo.backup_database("manual")
    repo.delete_custom_tag(tag.id)
    assert custom_names(repo, hero_id) == []

    repo.restore_database(backup)
    assert custom_names(repo, hero_id) == ["Hero"]
    assert any("before-restore" in p.name for p in repo.list_backups())  # undoable too


def _damage(repo):
    """Overwrite the middle of the database file (a bad sector, a sync tool)."""
    repo._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    repo.close_thread_connection()
    path = Path(repo.db_path)
    with open(path, "r+b") as f:
        f.seek(path.stat().st_size // 2)
        f.write(b"\xa5" * 8192)  # (the same damage every run)


def _filled(repo):
    ids = repo.sync_assets([make_asset(fr"C:\lib\file{i}.png") for i in range(400)])  # (several pages)
    tag = repo.create_custom_tag("Hero")
    repo.add_tags([ids[r"C:\lib\file0.png"]], [tag.id])
    return ids


def test_the_daily_backup_is_made_once_a_day_and_kept_apart(repo, monkeypatch):
    monkeypatch.setattr(tag_repository, "MAX_BACKUPS", 2)
    monkeypatch.setattr(tag_repository, "MAX_DAILY_BACKUPS", 2)
    repo.create_custom_tag("Hero")
    first = repo.backup_if_due()
    assert first is not None and first.stem.endswith("_daily")
    assert repo.backup_if_due() is None  # (not due again for a day)
    for i in range(3):
        repo.backup_database(f"manual{i}")
    assert first in repo.list_backups()  # manual ones don't push the daily one out...
    old = first.stat().st_mtime - tag_repository.DAILY_BACKUP_SECONDS - 1
    os.utime(first, (old, old))
    for _ in range(3):
        second = repo.backup_if_due()
        os.utime(second, (old, old))
    assert len([b for b in repo.list_backups() if b.stem.endswith("_daily")]) == 2
    assert len([b for b in repo.list_backups() if not b.stem.endswith("_daily")]) == 2  # ...nor the other way


def test_a_damaged_database_is_never_backed_up_and_its_good_backup_restores(repo):
    ids = _filled(repo)
    good = repo.backup_if_due()
    os.utime(good, (good.stat().st_mtime - tag_repository.DAILY_BACKUP_SECONDS - 1,) * 2)  # (due again)
    _damage(repo)
    with pytest.raises(sqlite3.DatabaseError) as error:  # (what a scan meets)
        repo.sync_assets([make_asset(fr"C:\lib\file{i}.png") for i in range(400)])
    assert tag_repository.is_damage_error(str(error.value))
    with pytest.raises(TagError):
        repo.backup_if_due()  # (a damaged copy would push out good ones)
    assert repo.newest_good_backup() == good
    repo.restore_database(good)
    assert custom_names(repo, ids[r"C:\lib\file0.png"]) == ["Hero"]
    assert not list(repo.backup_dir().glob("*-wal")) and not list(repo.backup_dir().glob("*-shm"))  # (checked read-only)


def test_a_badly_damaged_database_is_kept_aside_and_restored_into_a_fresh_one(repo):
    ids = _filled(repo)
    good = repo.backup_database("manual")
    repo._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    repo.close_thread_connection()
    path = Path(repo.db_path)
    data = path.read_bytes()
    path.write_bytes(data[:100] + b"\xa5" * (len(data) - 100))  # (SQLite can't even copy it now)
    repo.restore_database(good)
    assert custom_names(repo, ids[r"C:\lib\file0.png"]) == ["Hero"]
    kept = [b for b in repo.list_backups() if b.stem.endswith("_damaged")]
    assert len(kept) == 1 and kept[0].read_bytes()[100:200] == b"\xa5" * 100  # (as it was)


def test_damaged_backups_are_never_offered(repo):
    _filled(repo)
    backup = repo.backup_database("manual")
    with open(backup, "r+b") as f:
        f.seek(backup.stat().st_size // 2)
        f.write(b"\xa5" * 8192)  # (the same damage every run)
    assert repo.newest_good_backup() is None


def test_restore_rejects_other_files(repo, tmp_path):
    junk = tmp_path / "notes.db"
    junk.write_bytes(b"not a database")
    with pytest.raises(TagError):
        repo.restore_database(junk)


def test_export_then_import_into_a_fresh_library(repo, tmp_path):
    ids = repo.sync_assets([make_asset(r"C:\lib\hero.fbx"), make_asset(r"C:\lib\Ünïcode\bark.png")])
    hero = repo.create_custom_tag("Hero", "#ff0000")
    wip = repo.create_custom_tag("Work in progress")
    repo.add_tags([ids[r"C:\lib\hero.fbx"]], [hero.id, wip.id])
    repo.add_tags([ids[r"C:\lib\Ünïcode\bark.png"]], [wip.id])
    export = tmp_path / "tags.json"
    assert repo.export_tags(export) == 2
    data = json.loads(export.read_text(encoding="utf-8"))
    assert {t["name"] for t in data["tags"]} == {"Hero", "Work in progress"}
    assert "PNG" not in export.read_text(encoding="utf-8")  # file type tags aren't exported

    fresh = TagRepository(":memory:")
    fresh.create_custom_tag("hero")  # existing tag with different case is reused
    assert fresh.import_tags(export) == {"tags_created": 1, "files": 2, "skipped_tags": 0, "searches": 0}
    new_ids = fresh.sync_assets([make_asset(r"C:\lib\hero.fbx"), make_asset(r"C:\lib\Ünïcode\bark.png")])
    assert custom_names(fresh, new_ids[r"C:\lib\hero.fbx"]) == ["Work in progress", "hero"]
    assert custom_names(fresh, new_ids[r"C:\lib\Ünïcode\bark.png"]) == ["Work in progress"]
    assert fresh.import_tags(export)["tags_created"] == 0  # importing twice changes nothing


def test_import_skips_names_now_used_by_file_types(tmp_path):
    export = tmp_path / "tags.json"
    export.write_text(json.dumps({
        "format": "asset-manager-tags", "version": 1,
        "tags": [{"name": "PNG"}], "files": [{"path": r"C:\a.png", "tags": ["PNG"]}],
    }), encoding="utf-8")
    fresh = TagRepository(":memory:")
    fresh.sync_assets([make_asset(r"C:\a.png")])
    assert fresh.import_tags(export) == {"tags_created": 0, "files": 0, "skipped_tags": 1, "searches": 0}


def test_import_rejects_other_json(tmp_path):
    other = tmp_path / "other.json"
    other.write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(TagError):
        TagRepository(":memory:").import_tags(other)


def test_tests_never_touch_the_real_database(isolated_user_data):
    assert Path(tag_repository.get_repository().db_path).parent == isolated_user_data
