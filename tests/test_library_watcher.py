"""Live library updates: folder change detection (QFileSystemWatcher) and incremental rescans."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from core import library_watcher
from core.library_watcher import LibraryWatcher, path_key, rescan_folders
from core.scanner import AssetScanner
from core.tag_repository import TagRepository

EXTS = [".png", ".fbx"]


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "lib"
    (root / "textures").mkdir(parents=True)
    (root / "models" / "props").mkdir(parents=True)
    (root / "textures" / "brick.png").write_bytes(b"png")
    (root / "models" / "hero.fbx").write_bytes(b"fbx")
    (root / "models" / "props" / "crate.fbx").write_bytes(b"crate")
    return root


def full_scan(root):
    dirs: set[Path] = set()
    assets = AssetScanner(supported_extensions=EXTS).scan(root, directories=dirs)
    return assets, {path_key(d) for d in dirs}


def names(assets):
    return sorted(a.name for a in assets)


def test_full_scan_reports_every_folder(lib):
    _assets, dirs = full_scan(lib)
    assert dirs == {path_key(p) for p in (lib, lib / "textures", lib / "models", lib / "models" / "props")}


def test_added_and_removed_files(lib):
    assets, watched = full_scan(lib)
    (lib / "textures" / "moss.png").write_bytes(b"png")
    (lib / "models" / "hero.fbx").unlink()
    result = rescan_folders([lib / "textures", lib / "models"], EXTS, watched)
    assert names(result.merge_into(assets)) == ["brick.png", "crate.fbx", "moss.png"]


def test_new_and_deleted_subfolders(lib):
    assets, watched = full_scan(lib)
    shutil.rmtree(lib / "models" / "props")
    (lib / "textures" / "new" / "deep").mkdir(parents=True)
    (lib / "textures" / "new" / "deep" / "leaf.png").write_bytes(b"png")
    # Windows reports the parents; the deleted folder may report itself too
    result = rescan_folders([lib / "models", lib / "textures", lib / "models" / "props"], EXTS, watched)
    assert names(result.merge_into(assets)) == ["brick.png", "hero.fbx", "leaf.png"]
    assert {path_key(d) for d in result.new_dirs} == {path_key(lib / "textures" / "new"), path_key(lib / "textures" / "new" / "deep")}
    assert path_key(lib / "models" / "props") in {path_key(t) for t in result.removed_trees}


def test_rescan_syncs_tags_and_relinks(lib):
    repo = TagRepository(":memory:")
    assets, watched = full_scan(lib)
    ids = repo.sync_assets(assets)
    hero = repo.create_custom_tag("Hero")
    hero_id = ids[str(lib.resolve() / "models" / "hero.fbx")]
    repo.add_tags([hero_id], [hero.id])
    repo.fill_missing_hashes()
    (lib / "models" / "hero.fbx").rename(lib / "textures" / "hero_final.fbx")  # moved while the app is open
    result = rescan_folders([lib / "models", lib / "textures"], EXTS, watched, repo)
    assert result.relinked == 1
    new_id = result.ids[str(lib.resolve() / "textures" / "hero_final.fbx")]
    assert new_id == hero_id and "Hero" in [t.name for t in result.tags[new_id]]


def test_watcher_reports_changed_folders_once_per_burst(lib, monkeypatch, qapp):
    app = qapp
    monkeypatch.setattr(library_watcher, "DEBOUNCE_MS", 300)
    watcher = LibraryWatcher()
    watcher._timer.setInterval(300)
    _assets, watched = full_scan(lib)
    watcher.watch([Path(k) for k in watched])
    reports = []
    watcher.folders_changed.connect(reports.append)

    for i in range(5):  # a burst, like copying several files
        (lib / "textures" / f"t{i}.png").write_bytes(b"png")
    deadline = time.monotonic() + 5
    while not reports and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    for _ in range(30):  # make sure no second report follows for the same burst
        app.processEvents()
        time.sleep(0.02)
    assert len(reports) == 1
    assert path_key(lib / "textures") in {path_key(p) for p in reports[0]}


def test_watch_limit(monkeypatch, lib, qapp):
    monkeypatch.setattr(library_watcher, "MAX_WATCHED_DIRS", 2)
    watcher = LibraryWatcher()
    _assets, watched = full_scan(lib)
    watcher.watch([Path(k) for k in watched])
    assert len(watcher.watched_keys()) == 2 and not watcher.is_complete()


def test_library_inside_an_excluded_folder_name_is_still_scanned(tmp_path):
    root = tmp_path / "build" / "assets"  # "build" is normally skipped inside libraries
    (root / "dist").mkdir(parents=True)
    (root / "a.png").write_bytes(b"png")
    (root / "dist" / "skipped.png").write_bytes(b"png")
    assert names(AssetScanner(supported_extensions=EXTS).scan(root)) == ["a.png"]
