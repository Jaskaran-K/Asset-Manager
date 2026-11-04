"""The main window end to end, on a small temporary library (never the real config or database)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import shiboken6
from PIL import Image
from PySide6.QtCore import QEvent, QItemSelectionModel, Qt
from PySide6.QtGui import QKeyEvent

from core.settings import get_scan_folders, set_scan_folders


def pump(qapp, condition=lambda: False, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    (root / "props").mkdir(parents=True)
    Image.new("RGB", (4096, 1024), "gray").save(root / "big_wall.png")
    Image.new("RGB", (256, 256), "red").save(root / "small_brick.png")
    (root / "props" / "chair.fbx").write_bytes(b"fbx")
    set_scan_folders([root])
    return root


def open_window(qapp):
    import main

    window = main.MainWindow()
    window.show()
    assert pump(qapp, lambda: window.library.scan_done_once and not window.library.is_scanning())
    return window


def names_in_grid(window):
    return sorted(a.name for a in window.grid_model.assets())


def select(window, name):
    row = next(r for r, a in enumerate(window.grid_model.assets()) if a.name == name)
    window.list_grid.selectionModel().select(window.grid_model.index(row), QItemSelectionModel.SelectionFlag.ClearAndSelect)


def press(window, key, text):
    window.list_grid.setFocus()
    event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text)
    assert window.eventFilter(window.list_grid, event)


def test_first_run_shows_the_welcome_screen(tmp_path, qapp, monkeypatch):
    import main
    from PySide6.QtWidgets import QFileDialog

    window = main.MainWindow()  # no library folders configured yet
    window.show()
    qapp.processEvents()
    assert window.welcome_panel.isVisible() and not window.lbl_empty_state.isVisible()

    folder = tmp_path / "first"
    folder.mkdir()
    (folder / "a.png").write_bytes(b"x")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(folder)))
    window._on_welcome_add_folder()
    assert pump(qapp, lambda: names_in_grid(window) == ["a.png"])
    assert not window.welcome_panel.isVisible()
    window.close()


def test_scan_fills_the_virtual_grid(library, qapp):
    window = open_window(qapp)
    assert names_in_grid(window) == ["big_wall.png", "chair.fbx", "small_brick.png"]
    assert "Path:" in window.grid_model.data(window.grid_model.index(0), Qt.ItemDataRole.ToolTipRole)
    window.close()


def test_favorite_and_rating_keys_and_detail_search(library, qapp):
    window = open_window(qapp)
    select(window, "small_brick.png")
    press(window, Qt.Key.Key_F, "f")
    press(window, Qt.Key.Key_3, "3")
    asset_id = window.library.ids[str((library / "small_brick.png").resolve())]
    assert window.library.marks[asset_id].favorite and window.library.marks[asset_id].rating == 3
    assert window.sidebar_tree.item_favorites.text(0) == "Favorites (1)"

    window.search_bar.edit.setText("rating:>=3")
    assert names_in_grid(window) == ["small_brick.png"]
    # res: needs the background reader; it fills in shortly
    window.search_bar.edit.setText("res:>=4k")
    assert pump(qapp, lambda: names_in_grid(window) == ["big_wall.png"])
    window.close()


def test_dragging_files_out_gives_file_urls(library, qapp):
    window = open_window(qapp)
    indexes = [window.grid_model.index(r) for r in range(window.grid_model.rowCount())]
    urls = window.grid_model.mimeData(indexes).urls()
    assert sorted(Path(u.toLocalFile()).name for u in urls) == ["big_wall.png", "chair.fbx", "small_brick.png"]
    assert window.grid_model.supportedDragActions() == Qt.DropAction.CopyAction
    window.close()


def test_live_updates_keep_the_selection(library, qapp):
    window = open_window(qapp)
    select(window, "chair.fbx")
    (library / "props" / "table.fbx").write_bytes(b"fbx2")
    assert pump(qapp, lambda: "table.fbx" in names_in_grid(window))
    assert [a.name for a in window._selected_assets()] == ["chair.fbx"]
    window.close()


def test_viewer_steps_through_the_grid(library, qapp, monkeypatch):
    import main

    window = open_window(qapp)
    window.search_bar.edit.setText(".png")  # the viewer follows the filtered grid
    seen = []

    def fake_exec(dialog):
        seen.append((dialog.asset.name, dialog._position))
        dialog.go(1)
        seen.append((dialog.asset.name, dialog._position))
        dialog.go(1)  # already at the end: stays
        seen.append((dialog.asset.name, dialog._position))
        return 0

    monkeypatch.setattr(main.AssetViewerDialog, "exec", fake_exec)
    window._open_viewer(window.grid_model.asset_at(0))
    assert seen == [("big_wall.png", "1 of 2"), ("small_brick.png", "2 of 2"), ("small_brick.png", "2 of 2")]
    assert [a.name for a in window._selected_assets()] == ["small_brick.png"]  # closing lands on it
    window.close()


def test_second_launch_shows_the_snapshot_before_scanning(library, qapp, monkeypatch):
    import main

    open_window(qapp).close()
    started = []
    real_start = main.MainWindow._start_scan

    def spy(self, folders, save_snapshot=False):
        started.append(names_in_grid(self))  # what's on screen when the scan begins
        real_start(self, folders, save_snapshot)

    monkeypatch.setattr(main.MainWindow, "_start_scan", spy)
    window = open_window(qapp)
    assert started == [["big_wall.png", "chair.fbx", "small_brick.png"]]
    window.close()


def test_sidebar_toggles_and_the_floating_one_closes_after_a_pick(library, qapp):
    from ui.animations import set_motion_mode

    set_motion_mode("off")
    try:
        window = open_window(qapp)
        assert window.sidebar.is_open() and window.btn_sidebar.isChecked()
        window.btn_sidebar.click()  # ☰ / Ctrl+B
        assert not window.sidebar.is_open() and not window.btn_sidebar.isChecked()
        window.btn_sidebar.click()
        window.sidebar.set_pinned(False)
        png = next(row for row, _name, exts in window.sidebar_tree.rows() if exts == frozenset({".png"}))
        window.sidebar_tree._on_item_clicked(png)
        assert names_in_grid(window) == ["big_wall.png", "small_brick.png"]
        assert not window.sidebar.is_open()  # floating: slides away after picking
        window.close()
    finally:
        set_motion_mode("on")


def test_library_split_button(library, qapp, tmp_path):
    window = open_window(qapp)
    toggle = window.action_library_toggle
    assert window.btn_library.property("active") is True  # showing the library
    assert not toggle.isEnabled()  # nothing browsed to add yet

    other = tmp_path / "other"
    other.mkdir()
    (other / "c.png").write_bytes(b"x")
    window.browse_folder = other
    window._update_library_controls()
    assert window.btn_library.property("active") is False
    assert toggle.isEnabled() and toggle.text() == "Add to Library"
    toggle.trigger()
    assert other.resolve() in [f.resolve() for f in get_scan_folders()]
    assert toggle.text() == "Remove from Library"
    toggle.trigger()
    assert other.resolve() not in [f.resolve() for f in get_scan_folders()]

    window.btn_library.click()  # the main part: back to the library
    assert window.browse_folder is None and window.btn_library.property("active") is True
    window.close()


def test_the_window_is_ready_for_the_3d_viewer_from_the_start(library, qapp, monkeypatch):
    """The 3D viewer's OpenGL widget made Qt remake the main window the first time one opened
    (it vanished and came back): a hidden one in the main window from the start prevents that."""
    import main
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    window = open_window(qapp)
    ready = window.findChildren(QOpenGLWidget)
    assert len(ready) == 1 and not ready[0].isVisible()
    # And the viewer is told when it has loaded a model, for the model's grid picture
    handed = []
    monkeypatch.setattr(main.AssetViewerDialog, "exec", lambda dialog: handed.append(dialog._model_viewed) or 0)
    select(window, "chair.fbx")
    window._open_viewer(window._selected_assets()[0])
    assert handed == [window.thumbnail_manager.model_viewed]
    window.close()


def test_a_new_scan_replaces_a_running_one_without_waiting(library, qapp, monkeypatch, tmp_path):
    """Starting a scan while one runs (changing folders in Settings) used to wait for the old
    one on the UI thread, freezing the window, and then applied its out-of-date results too."""
    import main
    from core.scanner import AssetScanner

    window = open_window(qapp)
    first_worker = window.library.scan_worker

    def first_scan_is_gone() -> bool:  # (it's connected to the method replaced below)
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return not shiboken6.isValid(first_worker)

    assert pump(qapp, first_scan_is_gone)
    original = AssetScanner.scan
    slow_root = library / "props"

    def scan(self, folder, *args, should_stop=None, **kwargs):
        if Path(folder) == slow_root:  # the old scan: slow, until it's told to stop
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not (should_stop and should_stop()):
                time.sleep(0.01)
        return original(self, folder, *args, should_stop=should_stop, **kwargs)

    monkeypatch.setattr(AssetScanner, "scan", scan)
    applied = []
    from ui.library_state import LibraryState

    real_finished = LibraryState._on_scan_finished

    def counting(self, assets, info):  # (a def, not a lambda: it's the receiver of a worker's signal)
        applied.append(len(assets))
        real_finished(self, assets, info)

    monkeypatch.setattr(LibraryState, "_on_scan_finished", counting)
    window._start_scan([slow_root])
    old_thread = window.library.scan_thread
    other = tmp_path / "other"
    other.mkdir()
    (other / "one.png").write_bytes(b"png")
    started = time.monotonic()
    window._start_scan([other])
    assert time.monotonic() - started < 0.5  # (no waiting for the old scan)
    assert pump(qapp, lambda: not window.library.is_scanning() and not window.library._stopping_scans)
    qapp.processEvents()
    assert applied == [1] and names_in_grid(window) == ["one.png"]  # (only the new scan's files)
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(old_thread)  # (the replaced scan's thread is freed)
    window.close()


def test_only_library_scans_forget_other_files(tmp_path):
    from core.scanner import AssetScanner
    from core.tag_repository import get_repository
    from ui.library_state import ScanWorker

    for folder in ("library", "browsed"):
        (tmp_path / folder).mkdir()
        Image.new("RGB", (4, 4)).save(tmp_path / folder / f"{folder}.png")
    scanner = AssetScanner(supported_extensions=[".png"])
    library, browsed = (scanner.scan(tmp_path / f) for f in ("library", "browsed"))
    repo = get_repository()

    def rows():
        return sorted(r[0] for r in repo._conn().execute("SELECT name FROM assets"))

    ScanWorker._sync_tags(library + browsed)
    ScanWorker._sync_tags(browsed)  # an Open Folder scan keeps the library's rows
    assert rows() == ["browsed.png", "library.png"]
    ScanWorker._sync_tags(library, forget_others=True, should_stop=lambda: True)  # replaced meanwhile
    assert rows() == ["browsed.png", "library.png"]
    info = ScanWorker._sync_tags(library, forget_others=True)
    assert rows() == ["library.png"] and info["error"] is None


def test_renaming_a_tag_updates_the_collections(library, qapp):
    window = open_window(qapp)
    tag = window.repo.create_custom_tag("Hero")
    window.repo.save_search("Heroes", "tag:Hero")
    window.sidebar_tree.reload_collections()
    window.repo.rename_custom_tag(tag.id, "Main Hero")  # (as Settings > Tags does)
    window._after_tags_changed()
    window.sidebar_tree._on_item_clicked(window.sidebar_tree.item_collections.child(0))
    assert [t.name for f in window.search_bar.filters() for t in f.tags] == ["Main Hero"]
    window.close()


def test_imported_favorites_and_ratings_show_at_once(library, qapp, monkeypatch, tmp_path):
    import json

    from PySide6.QtWidgets import QFileDialog, QMessageBox

    window = open_window(qapp)
    export = tmp_path / "tags.json"
    brick = str((library / "small_brick.png").resolve())
    export.write_text(json.dumps({
        "format": "asset-manager-tags", "version": 1, "tags": [],
        "files": [{"path": brick, "tags": [], "favorite": True, "rating": 4}],
    }), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(export), "")))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    window.library_tools.import_tags()
    assert window.sidebar_tree.item_favorites.text(0) == "Favorites (1)"
    assert window.library.marks[window.library.ids[brick]].rating == 4
    window.close()


def close_menu_when_open(qapp):
    """Close the next popup menu as soon as it's shown (QMenu.exec waits for it otherwise)."""
    from PySide6.QtCore import QTimer

    def close():
        popup = qapp.activePopupWidget()
        if popup is not None:
            popup.close()
        else:
            QTimer.singleShot(10, close)

    QTimer.singleShot(10, close)


def test_right_click_menus_are_freed(library, qapp):
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QMenu

    window = open_window(qapp)
    select(window, "small_brick.png")
    pos = window.list_grid.visualRect(window.grid_model.index(0)).center()

    def owned():
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return len(window.findChildren(QAction)), len(window.findChildren(QMenu))

    before = owned()
    for _ in range(3):
        close_menu_when_open(qapp)
        window._show_context_menu(pos)
    assert owned() == before
    window.close()


def test_settings_is_freed_when_it_closes(library, qapp):
    """Each Settings kept alive was restyled on every theme change (4 of them: 45 -> 721 ms)."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog

    window = open_window(qapp)

    def cancel_when_open():
        modal = QApplication.activeModalWidget()
        if modal is None:
            QTimer.singleShot(20, cancel_when_open)
        else:
            modal.reject()

    def widgets():
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return len(window.findChildren(QDialog)), len(QApplication.allWidgets())

    before = widgets()
    for _ in range(2):
        QTimer.singleShot(20, cancel_when_open)
        window._open_settings()
    assert widgets() == before
    window.close()


def test_a_good_scan_starts_the_daily_backup(library, qapp):
    window = open_window(qapp)
    assert pump(qapp, lambda: any(b.stem.endswith("_daily") for b in window.repo.list_backups()))
    window.close()


def test_a_damaged_database_offers_its_newest_good_backup(library, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from core import crash_log, tag_repository

    tag_repository.get_repository().create_custom_tag("Hero")  # (before the window: in its daily backup)
    window = open_window(qapp)
    assert pump(qapp, lambda: any(b.stem.endswith("_daily") for b in window.repo.list_backups()))
    window.repo._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    window.close()
    window.repo.close_thread_connection()  # (as quitting does: the next launch opens it afresh)
    path = Path(window.repo.db_path)
    data = path.read_bytes()
    path.write_bytes(data[:100] + b"\xa5" * (len(data) - 100))  # (all but the header: any query fails)
    monkeypatch.setattr(tag_repository, "_repository", None)  # (fresh connections, as on the next launch)
    monkeypatch.setattr(crash_log, "log_error", lambda *args: None)
    asked, warned = [], []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: asked.append(args[2]) or QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warned.append(args[2]))  # (a failure, not a hang)

    window = open_window(qapp)
    assert pump(qapp, lambda: asked) and "damaged" in asked[0] and "Restore the backup" in asked[0]
    assert not warned, warned  # (the restore happens as the offer is accepted)
    assert "Hero" in [t.name for t in window.repo.list_tags()]  # (restored)
    assert pump(qapp, lambda: not window.library.is_scanning())
    assert "tags unavailable" not in window.status_label.text()
    window.close()


def test_a_theme_change_recolours_the_thumbnails_straight_away(library, qapp):
    """Also while previewing themes in Settings (the cards bake in the theme's colours)."""
    from ui.themes import apply_theme, current_theme_id

    window = open_window(qapp)
    before = current_theme_id()
    assert pump(qapp, lambda: window.thumbnail_manager.memory_used() > 0)
    try:
        apply_theme("light" if before != "light" else "dark", qapp)
        assert window.thumbnail_manager.memory_used() == 0  # (made again, in the new colours)
        assert pump(qapp, lambda: window.thumbnail_manager.memory_used() > 0)
    finally:
        apply_theme(before, qapp)
    window.close()


def test_show_in_explorer_never_goes_through_the_command_prompt(library, qapp, monkeypatch):
    import re

    from ui import file_actions

    window = open_window(qapp)
    shown = []
    monkeypatch.setattr(file_actions, "_select_in_explorer", lambda path: shown.append(path) or True)
    window._reveal_in_explorer(library / "small_brick.png")
    assert shown == [str((library / "small_brick.png").resolve())]
    window._reveal_in_explorer(library / "gone.png")
    assert "isn't there" in window.status_label.text()
    window.close()
    root = Path(__file__).resolve().parent.parent
    sources = [root / "main.py", *(root / "ui").rglob("*.py"), *(root / "core").rglob("*.py")]
    shell = [p.name for p in sources if re.search(r"shell\s*=\s*True", p.read_text(encoding="utf-8"))]
    assert not shell  # (a name with %...% or & in it would be mangled or worse)


def test_replacing_a_scan_that_has_only_just_started(library, qapp):
    """A scan replaced before its thread had started running it: the old worker must stay alive
    until its thread is done (it was freed under the thread: an access violation)."""
    import gc

    window = open_window(qapp)
    for _ in range(40):
        window._start_scan([library])
        window._start_scan([library])  # (the first one's thread is usually still starting)
        gc.collect()
    assert pump(qapp, lambda: not window.library.is_scanning() and not window.library._stopping_scans)
    assert names_in_grid(window) == ["big_wall.png", "chair.fbx", "small_brick.png"]
    window.close()
