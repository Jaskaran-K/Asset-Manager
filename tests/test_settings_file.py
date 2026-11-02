"""The settings file: saved so a crash mid-save can't lose it, and read back from the last good
copy if it's damaged anyway."""

from __future__ import annotations

import json

import pytest

from core import crash_log, settings


@pytest.fixture
def logged(monkeypatch):
    errors = []
    monkeypatch.setattr(crash_log, "log_error", lambda title, details="": errors.append(title))
    monkeypatch.setattr(settings, "_reported_damaged", set())
    return errors


def test_a_save_that_fails_halfway_leaves_the_settings_as_they_were(monkeypatch):
    settings.set_theme("light")
    settings.set_thumbnail_size(150)

    def crash(*_args, **_kwargs):
        raise OSError("disk full")  # (as a crash or power cut midway through writing)

    with monkeypatch.context() as patch:  # (not monkeypatch.undo(): that would undo conftest's config path too)
        patch.setattr(settings.json, "dump", crash)
        settings.set_theme("dark_purple")
    assert settings.get_theme() == "light" and settings.get_thumbnail_size() == 150
    assert json.loads(settings.CONFIG_FILE_PATH.read_text(encoding="utf-8"))["theme"] == "light"


def test_a_damaged_file_falls_back_to_the_last_good_copy(logged):
    settings.set_scan_folders([settings.CONFIG_FILE_PATH.parent])
    settings.set_theme("light")  # (the copy before this one is now the backup)
    settings.CONFIG_FILE_PATH.write_text('{"theme": "li', encoding="utf-8")  # (cut off mid-save)
    assert settings.get_scan_folders() == [settings.CONFIG_FILE_PATH.parent]  # (from the backup)
    assert settings.get_theme() == "dark"  # (the backup is one save older)
    assert len(logged) == 1 and "couldn't be read" in logged[0]
    settings.get_theme()
    assert len(logged) == 1  # (reported once)
    # The next save writes a whole file again, keeping what the backup had
    settings.set_theme("dark_purple")
    data = json.loads(settings.CONFIG_FILE_PATH.read_text(encoding="utf-8"))
    assert data["theme"] == "dark_purple" and data["scan_folders"]
    backup = json.loads(settings._backup_path().read_text(encoding="utf-8"))
    assert "scan_folders" in backup  # (the damaged file never becomes the backup)
    assert not settings.CONFIG_FILE_PATH.with_name(settings.CONFIG_FILE_PATH.name + ".partial").exists()


def test_both_copies_damaged_starts_from_the_defaults(logged):
    settings.CONFIG_FILE_PATH.write_text("{", encoding="utf-8")
    settings._backup_path().write_text("[1, 2]", encoding="utf-8")  # (valid JSON, but not settings)
    assert settings.get_theme() == settings.DEFAULT_THEME and len(logged) == 1
    settings.set_theme("light")
    assert settings.get_theme() == "light"
