"""Archives in the main window: looking inside a zip and coming back, the viewer unpacking a file
inside one, dragging one out, and extracting (never the real config, library or caches)."""

from __future__ import annotations

import os

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from core import archives
from core.settings import set_archive_contents, set_scan_folders
from test_main_window import names_in_grid, open_window, pump, select
from zip_maker import DEFLATE64, Entry, make_zip, png_bytes


@pytest.fixture
def library(tmp_path):
    archives.forget_listings()
    root = tmp_path / "library"
    root.mkdir()
    (root / "loose.png").write_bytes(png_bytes(color="red"))
    make_zip(root / "pack.zip", [
        Entry("textures/rock.png", png_bytes(color="gray")),
        Entry("textures/moss.png", png_bytes(color="green"), DEFLATE64),
        Entry("lesson.mp4", b"not a real video"),
    ])
    set_scan_folders([root])
    yield root
    archives.forget_listings()


def asset_named(window, name):
    return next(a for a in window.grid_model.assets() if a.name == name)


def scanned(window):
    return window.library.scan_done_once and not window.library.is_scanning()


def test_files_inside_show_among_the_library_and_can_be_left_out(library, qapp):
    window = open_window(qapp)
    assert names_in_grid(window) == ["lesson.mp4", "loose.png", "moss.png", "pack.zip", "rock.png"]
    assert asset_named(window, "rock.png").in_archive
    tooltip = window._asset_tooltip(asset_named(window, "rock.png"))
    assert "Inside: pack.zip &gt; textures" in tooltip

    window.action_archive_contents.setChecked(False)
    assert pump(qapp, lambda: names_in_grid(window) == ["loose.png", "pack.zip"])
    window.close()


def test_looking_inside_a_zip_and_back(library, qapp):
    set_archive_contents(False)
    window = open_window(qapp)
    window.search_bar.set_query_text("pack")
    assert pump(qapp, lambda: names_in_grid(window) == ["pack.zip"])

    window._on_item_double_clicked(window.grid_model.index(0))
    assert pump(qapp, lambda: scanned(window) and names_in_grid(window) == ["lesson.mp4", "moss.png", "rock.png"])
    assert window.btn_back.isVisible() and window.txt_path.toolTip().startswith("Inside")
    assert window.search_bar.text() == ""
    assert "pack.zip" in window._asset_tooltip(asset_named(window, "rock.png"))

    # Backspace in the grid goes back: the search and the archive's card selected again
    window.list_grid.setFocus()
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Backspace, Qt.KeyboardModifier.NoModifier)  # (a key press, as Qt sends it)
    assert window.eventFilter(window.list_grid, event)
    assert names_in_grid(window) == ["pack.zip"]  # straight away, before the rescan
    assert window.search_bar.text() == "pack" and not window.btn_back.isVisible()
    assert window.list_grid.currentIndex().data(Qt.ItemDataRole.UserRole).name == "pack.zip"
    assert pump(qapp, lambda: scanned(window))
    assert names_in_grid(window) == ["pack.zip"]
    window.close()


def test_a_damaged_zip_says_so_when_opened(library, qapp):
    (library / "broken.zip").write_bytes(b"PK\x03\x04 not really a zip")
    window = open_window(qapp)
    window._open_viewer(asset_named(window, "broken.zip"))
    assert pump(qapp, lambda: scanned(window) and window.lbl_empty_state.isVisible())
    assert window.lbl_empty_state.text().startswith("Couldn't look inside broken.zip")
    window._leave_archive()
    assert "broken.zip" in names_in_grid(window)
    window.close()


def test_the_viewer_unpacks_a_file_inside_then_shows_it(library, qapp):
    from ui.viewer import AssetViewerDialog

    window = open_window(qapp)
    rock = asset_named(window, "rock.png")
    dialog = AssetViewerDialog(rock, window)
    assert pump(qapp, lambda: archives.cached_copy(rock.archive) is not None and dialog._file is not None)
    assert pump(qapp, lambda: dialog.tex_viewer.pixmap is not None and not dialog.tex_viewer.pixmap.isNull())
    assert dialog._metadata["Location"] == "Inside pack.zip > textures"
    assert dialog.unpacked == {str(rock.path)}
    dialog.close()
    dialog.deleteLater()
    window.close()


def test_dragging_a_file_out_of_a_zip_unpacks_it(library, qapp, monkeypatch):
    window = open_window(qapp)
    select(window, "rock.png")
    row = window.grid_model.row_of(str(asset_named(window, "rock.png").path))
    urls = window.grid_model.mimeData([window.grid_model.index(row)]).urls()
    assert len(urls) == 1
    dragged = urls[0].toLocalFile()
    assert os.path.isfile(dragged) and dragged.endswith("rock.png")

    monkeypatch.setattr(archives, "MAX_DRAG_BYTES", 10)  # too big to wait for: left out, with a word why
    row = window.grid_model.row_of(str(asset_named(window, "moss.png").path))
    assert window.grid_model.mimeData([window.grid_model.index(row)]).urls() == []
    assert "Extract" in window.status_label.text()
    window.close()


def test_extract_to_a_folder_named_after_the_zip(library, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    window = open_window(qapp)
    target = tmp_path / "out"
    target.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(target)))
    window._extract(asset_named(window, "pack.zip"))
    assert pump(qapp, lambda: "Extracted 3 files" in window.status_label.text())
    assert (target / "pack" / "textures" / "moss.png").read_bytes() == png_bytes(color="green")

    # Again: the files are there, so it asks; skipping leaves them as they are
    from PySide6.QtWidgets import QMessageBox

    (target / "pack" / "lesson.mp4").write_bytes(b"mine")
    asked = []

    def skip(box):
        asked.append(box.text())
        box.buttons()[1].click()  # "Skip Them"

    monkeypatch.setattr(QMessageBox, "exec", skip)
    window._extract(asset_named(window, "pack.zip"))
    assert asked and "3 files" in asked[0]
    assert pump(qapp, lambda: "skipped 3" in window.status_label.text())
    assert (target / "pack" / "lesson.mp4").read_bytes() == b"mine"
    window.close()


def test_the_right_click_menu_of_a_zip_and_of_a_file_inside(library, qapp):
    from ui.asset_menu import AssetMenuActions, build_asset_menu

    window = open_window(qapp)
    noop = lambda *a: None  # noqa: E731
    actions = AssetMenuActions(noop, noop, noop, noop, noop, extract=noop)
    for name, expected in (("pack.zip", "Extract To..."), ("rock.png", "Extract...")):
        asset = asset_named(window, name)
        menu = build_asset_menu(window, asset, [asset], [], actions, window.artbook_sender)
        texts = [a.text() for a in menu.actions()]
        assert expected in texts
        if name == "pack.zip":
            assert texts[0] == "Look Inside"
        else:
            assert "Show Archive in File Explorer" in texts
        menu.deleteLater()
    window.close()
