"""The main window end to end, on a small temporary library (never the real config or database)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
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
    assert pump(qapp, lambda: window._scan_done_once and not window.scan_thread.isRunning())
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
    asset_id = window._asset_ids[str((library / "small_brick.png").resolve())]
    assert window._asset_marks[asset_id].favorite and window._asset_marks[asset_id].rating == 3
    assert window.item_favorites.text(0) == "Favorites (1)"

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
        png = next(row for row, _name, exts in window._sidebar_rows if exts == frozenset({".png"}))
        window._on_sidebar_item_clicked(png, 0)
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
