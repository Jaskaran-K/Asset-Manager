"""The --self-test run (used on every release build) passes and never touches the real library."""

from __future__ import annotations

from core import tag_repository


def test_self_test_passes_without_opening_the_library(qapp, tmp_path, monkeypatch):
    import main

    opened = []
    real_connect = tag_repository.sqlite3.connect
    monkeypatch.setattr(tag_repository.sqlite3, "connect", lambda path, *a, **k: opened.append(str(path)) or real_connect(path, *a, **k))
    log = tmp_path / "self-test.log"
    assert main.run_self_test(str(log)) == 0, log.read_text(encoding="utf-8")
    assert "RESULT: PASS" in log.read_text(encoding="utf-8")
    library = str(tag_repository.DB_PATH)
    assert library not in opened, "the self-test opened the library database"
