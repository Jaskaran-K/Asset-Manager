"""Updater: exe swap safety, download verification and the install-finished handler's thread."""

import hashlib
import threading
import time

import pytest

from core import updater
from core.updater import UpdateError, UpdateInfo

NEW_EXE = b"MZ" + b"new version " * 200_000  # > 1 MB, starts like a Windows program


def _info(source, data=NEW_EXE, size=None, sha=None):
    return UpdateInfo(
        latest_version="9.9.9",
        current_version="0.0.1",
        source="release",
        name="v9.9.9",
        notes="",
        page_url="",
        download_url=source.as_uri(),
        download_size=len(data) if size is None else size,
        download_sha256=hashlib.sha256(data).hexdigest() if sha is None else sha,
    )


@pytest.fixture
def frozen_app(tmp_path, monkeypatch):
    """A fake packaged app: AssetManager.exe in its own folder, plus a 'release' file to download."""
    app_dir = tmp_path / "Desktop"
    app_dir.mkdir()
    exe = app_dir / "AssetManager.exe"
    exe.write_bytes(b"MZ old version")
    release = tmp_path / "release.exe"
    release.write_bytes(NEW_EXE)
    monkeypatch.setattr(updater, "IS_FROZEN", True)
    monkeypatch.setattr(updater, "EXECUTABLE_PATH", exe)
    return exe, release


def test_swap_installs_new_exe_and_keeps_old(frozen_app):
    exe, release = frozen_app
    updater.install_update(_info(release))
    assert exe.read_bytes() == NEW_EXE
    assert exe.with_name("AssetManager.exe.old").read_bytes() == b"MZ old version"
    assert not exe.with_name("AssetManager.exe.download").exists()


@pytest.mark.parametrize("kwargs", [{"sha": "0" * 64}, {"size": len(NEW_EXE) + 1}])
def test_damaged_or_incomplete_download_changes_nothing(frozen_app, kwargs):
    exe, release = frozen_app
    with pytest.raises(UpdateError):
        updater.install_update(_info(release, **kwargs))
    assert exe.read_bytes() == b"MZ old version"
    assert sorted(p.name for p in exe.parent.iterdir()) == ["AssetManager.exe"]


def test_locked_old_copy_uses_next_free_name(frozen_app):
    exe, release = frozen_app
    stale = exe.with_name("AssetManager.exe.old")
    stale.write_bytes(b"MZ stale")
    with open(stale, "rb"):  # Windows can't delete a file that's open, like a still-running old exe
        updater.install_update(_info(release))
    assert exe.read_bytes() == NEW_EXE
    assert exe.with_name("AssetManager.exe.old.1").read_bytes() == b"MZ old version"


def test_cleanup_removes_every_leftover(frozen_app):
    exe, _release = frozen_app
    for name in ("AssetManager.exe.old", "AssetManager.exe.old.1", "AssetManager.exe.download"):
        exe.with_name(name).write_bytes(b"x")
    updater.cleanup_after_update()
    assert sorted(p.name for p in exe.parent.iterdir()) == ["AssetManager.exe"]


def test_installed_app_downloads_and_runs_the_installer(frozen_app, tmp_path, monkeypatch):
    exe, release = frozen_app
    monkeypatch.setattr(updater, "IS_INSTALLED", True)
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    monkeypatch.setattr(updater, "_pending_installer", None)
    updater.install_update(_info(release))
    assert exe.read_bytes() == b"MZ old version"  # nothing replaced until the installer runs
    setup = tmp_path / "temp" / "AssetManagerUpdate" / "AssetManager-Setup-9.9.9.exe"
    assert setup.read_bytes() == NEW_EXE

    launched = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **kw: launched.append(args))
    updater.restart_application()
    assert launched == [[str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={exe.parent}"]]


def test_installed_app_rejects_a_damaged_installer(frozen_app, tmp_path, monkeypatch):
    _exe, release = frozen_app
    monkeypatch.setattr(updater, "IS_INSTALLED", True)
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    monkeypatch.setattr(updater, "_pending_installer", None)
    with pytest.raises(UpdateError):
        updater.install_update(_info(release, sha="0" * 64))
    assert updater._pending_installer is None
    assert not list((tmp_path / "temp" / "AssetManagerUpdate").iterdir())


def test_install_finished_handler_runs_on_ui_thread(monkeypatch, qapp):
    """Regression: a lambda receiver ran on the worker thread, which froze the app after installing."""
    from ui import settings_dialog

    app = qapp
    threads = []
    monkeypatch.setattr(settings_dialog, "install_update", lambda info, progress: False)
    # The handler's first step records the thread and stops it there: going on to touch
    # widgets from the wrong thread would hang the test instead of failing it
    def record_thread(self):
        threads.append(threading.current_thread())
        raise RuntimeError("stop the handler here")

    monkeypatch.setattr(settings_dialog.AboutPage, "_close_progress", record_thread)

    page = settings_dialog.AboutPage()
    page._start_install(UpdateInfo("9.9.9", "0.0.1", "release", "v9.9.9", "", "", ""))
    deadline = time.monotonic() + 10
    while not threads and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    page._thread.wait(5000)
    page._progress_dialog.close()
    page.deleteLater()

    assert threads == [threading.main_thread()]
