"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path, monkeypatch):
    """Never touch the real config, library database, caches or logs from a test."""
    from core import crash_log, settings, tag_repository, video_tools

    data = tmp_path / "user-data"
    data.mkdir()
    monkeypatch.setattr(settings, "CONFIG_FILE_PATH", data / "config.json")
    monkeypatch.setattr(settings, "CATEGORY_ICONS_DIR", data / "category_icons")
    monkeypatch.setattr(tag_repository, "DB_PATH", data / "library.db")
    monkeypatch.setattr(tag_repository, "_repository", None)
    monkeypatch.setattr(crash_log, "LOG_DIR", data)
    monkeypatch.setattr(video_tools, "CACHE_ROOT", data / "cache")
    monkeypatch.setattr(video_tools, "PROXY_DIR", data / "cache" / "proxies")
    monkeypatch.setattr(video_tools, "POSTER_DIR", data / "cache" / "posters")
    monkeypatch.setattr(video_tools, "THUMB_DIR", data / "cache" / "thumbnails")
    return data


@pytest.fixture(scope="session")
def qapp():
    """The one QApplication for the whole test run.

    Tests must use this rather than creating their own: if one test made a plain
    QCoreApplication, later tests building widgets would crash the process.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    # Like the app does on quit: let background jobs finish before Python tears Qt down,
    # or the process can crash after all tests have passed
    from ui.tag_widgets import wait_for_background

    wait_for_background()
    app.processEvents()
