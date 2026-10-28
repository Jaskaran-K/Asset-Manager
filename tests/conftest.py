"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path, monkeypatch):
    """Never touch the real config, library database, caches or logs from a test."""
    from core import artbook_store, crash_log, settings, tag_repository, video_tools

    data = tmp_path / "user-data"
    data.mkdir()
    monkeypatch.setattr(settings, "CONFIG_FILE_PATH", data / "config.json")
    monkeypatch.setattr(settings, "CATEGORY_ICONS_DIR", data / "category_icons")
    monkeypatch.setattr(tag_repository, "DB_PATH", data / "library.db")
    monkeypatch.setattr(tag_repository, "_repository", None)
    monkeypatch.setattr(crash_log, "LOG_DIR", data)
    monkeypatch.setattr(artbook_store, "PAGES_DIR", data / "artbook")
    monkeypatch.setattr(video_tools, "CACHE_ROOT", data / "cache")
    monkeypatch.setattr(video_tools, "PROXY_DIR", data / "cache" / "proxies")
    monkeypatch.setattr(video_tools, "POSTER_DIR", data / "cache" / "posters")
    monkeypatch.setattr(video_tools, "THUMB_DIR", data / "cache" / "thumbnails")
    yield data
    # A closed Artbook panel still has its autosave timer, and background jobs may still be
    # running: they'd write into the *next* test's folders (paths are read when they run)
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if isinstance(app, QApplication):
        from ui.artbook import ArtbookPanel
        from ui.tag_widgets import wait_for_background

        for widget in app.allWidgets():
            if isinstance(widget, ArtbookPanel):
                widget._save_timer.stop()
        wait_for_background()
        app.processEvents()  # delivers the results, which can start one more save
        wait_for_background()


@pytest.fixture(scope="session")
def qapp():
    """The one QApplication for the whole test run.

    Tests must use this rather than creating their own: if one test made a plain
    QCoreApplication, later tests building widgets would crash the process.
    """
    from PySide6.QtWidgets import QApplication

    from ui import main_thread_gc

    app = QApplication.instance() or QApplication([])
    main_thread_gc.install(app)  # as the app does: garbage is only collected on this thread
    yield app
    # Like the app does on quit: let background jobs finish before Python tears Qt down,
    # or the process can crash after all tests have passed
    from ui.tag_widgets import wait_for_background

    wait_for_background()
    app.processEvents()
