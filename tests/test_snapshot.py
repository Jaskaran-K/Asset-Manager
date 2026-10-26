"""The last scan is remembered so the next launch can show the library instantly."""

from __future__ import annotations

from pathlib import Path

from core.library_snapshot import load_snapshot, save_snapshot, snapshot_path
from core.scanner import AssetScanner


def test_round_trip(tmp_path, isolated_user_data):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "Ünïcode rock.png").write_bytes(b"x")
    (tmp_path / "lib" / "hero.fbx").write_bytes(b"y")
    folders, exts = [tmp_path / "lib"], [".png", ".fbx"]
    assets = AssetScanner(supported_extensions=exts).scan(tmp_path / "lib")
    ids = {str(a.path): i for i, a in enumerate(assets, start=1)}
    save_snapshot(folders, exts, assets, ids)
    assert snapshot_path().parent == isolated_user_data

    loaded, loaded_ids = load_snapshot(folders, list(reversed(exts)))  # order doesn't matter
    assert loaded == assets and loaded_ids == ids


def test_other_folders_or_types_ignore_it(tmp_path):
    save_snapshot([tmp_path / "a"], [".png"], [], {})
    assert load_snapshot([tmp_path / "b"], [".png"]) is None
    assert load_snapshot([tmp_path / "a"], [".png", ".jpg"]) is None
    assert load_snapshot([tmp_path / "a"], [".png"]) == ([], {})


def test_damaged_snapshot_is_ignored():
    snapshot_path().parent.mkdir(parents=True, exist_ok=True)
    snapshot_path().write_text("{not json", encoding="utf-8")
    assert load_snapshot([Path("C:/x")], [".png"]) is None
