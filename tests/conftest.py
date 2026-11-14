"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path, monkeypatch):
    """Never touch the real config, library database, caches or logs from a test."""
    from core import artbook_store, crash_log, pdf_notes, settings, tag_repository, video_tools

    data = tmp_path / "user-data"
    data.mkdir()
    monkeypatch.setattr(settings, "CONFIG_FILE_PATH", data / "config.json")
    monkeypatch.setattr(settings, "CATEGORY_ICONS_DIR", data / "category_icons")
    monkeypatch.setattr(tag_repository, "DB_PATH", data / "library.db")
    monkeypatch.setattr(tag_repository, "_repository", None)
    monkeypatch.setattr(crash_log, "LOG_DIR", data)
    monkeypatch.setattr(artbook_store, "PAGES_DIR", data / "artbook")
    monkeypatch.setattr(pdf_notes, "NOTES_DIR", data / "pdf-notes")
    monkeypatch.setattr(video_tools, "CACHE_ROOT", data / "cache")
    monkeypatch.setattr(video_tools, "PROXY_DIR", data / "cache" / "proxies")
    monkeypatch.setattr(video_tools, "POSTER_DIR", data / "cache" / "posters")
    monkeypatch.setattr(video_tools, "THUMB_DIR", data / "cache" / "thumbnails")
    # The browser extension's link file, inbox and registration (a key of the test's own, removed by the test)
    from core import browser_link

    monkeypatch.setattr(browser_link, "DATA_ROOT", data / "cache")
    monkeypatch.setattr(browser_link, "REGISTRY_KEY", rf"Software\AssetManager-test-{tmp_path.name}")
    # Models are read in the test's own process (tests that replace the loader see their replacement);
    # tests/test_model_reader.py starts the real helper program
    from core import model_reader

    monkeypatch.setenv(model_reader.READER_ENV, "inprocess")
    monkeypatch.setattr(model_reader, "_failures", None)
    # "Show in Explorer" would open real Explorer windows on the desktop (tests replace this to check it)
    from ui import file_actions

    monkeypatch.setattr(file_actions, "_select_in_explorer", lambda path: True)
    # The apps on this computer play no part (a test that wants some sets them up)
    from core import app_icons, apps

    monkeypatch.setattr(apps, "_registered_programs", lambda: ())
    monkeypatch.setattr(apps, "_usual_programs", lambda: ())
    monkeypatch.setattr(apps, "_shortcut_folders", lambda: [])
    monkeypatch.setattr(apps, "_associated_program", lambda extension: None)
    apps.configure({}, {})
    app_icons.forget()
    # Helpers are never added to the real Maya or Blender
    monkeypatch.setenv("MAYA_APP_DIR", str(data / "maya"))
    monkeypatch.setenv("APPDATA", str(data / "appdata"))
    monkeypatch.delenv("BLENDER_USER_SCRIPTS", raising=False)
    from PySide6.QtWidgets import QApplication

    windows_before = {id(w) for w in QApplication.topLevelWidgets()} if QApplication.instance() else set()
    yield data
    # A closed Artbook panel still has its autosave timer, and background jobs may still be
    # running: they'd write into the *next* test's folders (paths are read when they run)
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
        _free_windows(app, windows_before)


def _free_windows(app, windows_before: set[int]) -> None:
    """Free the windows a test left behind (closed is not freed). Every later theme change
    restyled them all: with 32 main windows and 3,700 widgets alive by the end, the suite took
    over 4 minutes, not 1.5. Done here, while this test's data paths are still redirected (a
    window that closes now saves into this test's folders). Popups (menus, a search box's
    suggestions) are left to whatever owns them: freeing one twice crashes the process."""
    import shiboken6
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtWidgets import QApplication

    leftovers = [w for w in QApplication.topLevelWidgets()
                 if id(w) not in windows_before and w.windowType() in (Qt.WindowType.Window, Qt.WindowType.Dialog)]
    for window in leftovers:
        if shiboken6.isValid(window):  # (one may have gone with another)
            if window.isVisible():
                window.close()
            window.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(scope="session")
def qapp():
    """The one QApplication for the whole test run.

    Tests must use this rather than creating their own: if one test made a plain
    QCoreApplication, later tests building widgets would crash the process.
    """
    from PySide6.QtWidgets import QApplication

    from ui import main_thread_gc, scroll_bar_watch

    app = QApplication.instance() or QApplication([])
    main_thread_gc.install(app)  # as the app does: garbage is only collected on this thread
    scroll_bar_watch.install(app)  # (as the app does)
    yield app
    # Like the app does on quit: let background jobs finish before Python tears Qt down,
    # or the process can crash after all tests have passed
    from ui.tag_widgets import wait_for_background

    wait_for_background()
    app.processEvents()
