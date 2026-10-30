"""Batch 4: exporting the board as a PNG, and sharing pages as .artbook files (core.artbook_share)."""

from __future__ import annotations

import hashlib
import json
import time
import zipfile

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPointF
from PySide6.QtGui import QImage, QKeyEvent
from PySide6.QtWidgets import QFileDialog

from core import artbook_share
from core import artbook_store as store
from ui import shortcuts
from ui.animations import set_motion_mode
from ui.themes import theme_color


def pump(qapp, condition=lambda: False, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture(autouse=True)
def no_motion():
    set_motion_mode("off")
    yield
    set_motion_mode("on")


@pytest.fixture
def pictures(tmp_path):
    folder = tmp_path / "pictures"
    folder.mkdir()
    Image.new("RGB", (400, 200), "red").save(folder / "wide.png")
    Image.new("RGB", (100, 300), "blue").save(folder / "tall.png")
    return folder


@pytest.fixture
def panel(qapp):
    from ui.artbook import ArtbookPanel

    widget = ArtbookPanel()
    widget.resize(900, 700)
    widget.show()
    widget.sidebar.set_open(False, animate=False)
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def add_and_wait(qapp, board, paths):
    before = len(board.items_list())
    count = board.add_files(paths)
    assert pump(qapp, lambda: len(board.items_list()) == before + count and all(i.pixmap is not None for i in board.items_list()))
    return board.items_list()[before:]


def save_as(monkeypatch, path):
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))


# ----------------------------------------------------------------------
# PNG
# ----------------------------------------------------------------------
def test_ctrl_e_exports_the_board_or_the_selection_as_png(qapp, panel, pictures, tmp_path, monkeypatch):
    board = panel.board
    wide, tall = add_and_wait(qapp, board, [pictures / "wide.png", pictures / "tall.png"])
    board._scene.clearSelection()
    target = tmp_path / "out" / "board.png"
    target.parent.mkdir()
    save_as(monkeypatch, target)
    combo = shortcuts.sequence("artbook.export_png")[0]
    board.setFocus()
    board.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, combo.key(), combo.keyboardModifiers()))
    assert pump(qapp, lambda: target.is_file() and "Exported" in board.toast.text())
    image = QImage(str(target))
    bounds = wide.sceneBoundingRect().united(tall.sceneBoundingRect())
    assert image.width() == pytest.approx(bounds.width() * (1 + 2 * 0.02 * max(1, bounds.height() / bounds.width())), rel=0.05)
    assert image.pixelColor(2, 2) == theme_color("grid_bg")  # the board's colour around them
    center = wide.sceneBoundingRect().center() - bounds.topLeft()
    margin = max(bounds.width(), bounds.height()) * 0.02
    assert image.pixelColor(int(center.x() + margin), int(center.y() + margin)).red() > 240  # the red image

    tall.setSelected(True)  # only the selection
    only = board.export_image()
    assert only.width() < image.width() and only.height() == pytest.approx(300 * 1.04, abs=2)
    assert wide.isVisible()  # (hidden only while it was drawn)


def test_export_keeps_the_detail_of_scaled_down_images_within_limits(qapp, panel, pictures):
    from ui import artbook_board

    board = panel.board
    (wide,) = add_and_wait(qapp, board, [pictures / "wide.png"])
    wide.setScale(0.25)  # shown small, but its pixels are all there
    assert board.export_image().width() == pytest.approx(400 * 1.04, abs=2)
    wide.setScale(400.0)  # huge: the picture stays within the size limit
    image = board.export_image()
    assert max(image.width(), image.height()) <= artbook_board.EXPORT_MAX_SIDE


# ----------------------------------------------------------------------
# .artbook
# ----------------------------------------------------------------------
def test_a_page_shared_and_opened_again_is_the_same_page(qapp, panel, pictures, tmp_path, monkeypatch):
    board = panel.board
    wide, tall = add_and_wait(qapp, board, [pictures / "wide.png", pictures / "tall.png"])
    wide.setPos(500, 120)
    wide.setRotation(30)
    tall.set_crop((10, 20, 90, 200))
    note = board.add_note(QPointF(-300, -200))
    note.setPlainText("Warm rim light")
    board.finish_note_edit()
    panel.save_now()

    shared = tmp_path / "Mood.artbook"
    save_as(monkeypatch, shared)
    board.export_page_requested.emit()
    assert pump(qapp, lambda: shared.is_file() and "Exported" in board.toast.text())
    with zipfile.ZipFile(shared) as archive:
        names = set(archive.namelist())
        page_json = json.loads(archive.read("page.json"))
    assert {"manifest.json", "page.json", "thumb.png"} <= names
    original = hashlib.sha256((pictures / "wide.png").read_bytes()).hexdigest() + ".png"
    assert f"images/{original}" in names  # linked files go in whole
    assert all(i.get("source", {}).get("path") is None for i in page_json["items"])  # no paths from this computer

    (pictures / "wide.png").unlink()  # the receiver doesn't have the files
    (pictures / "tall.png").unlink()
    page = artbook_share.import_page(shared)
    assert page["id"] != panel.page["id"] and page["name"] == panel.page["name"]
    before = {i["kind"] + str(round(i["x"])) + str(round(i["y"])): i for i in panel.page["items"]}
    after = {i["kind"] + str(round(i["x"])) + str(round(i["y"])): i for i in page["items"]}
    assert before.keys() == after.keys()
    for key, item in after.items():
        assert item["id"] != before[key]["id"]
        if item["kind"] == "image":
            assert item["crop"] == before[key]["crop"] and item["rotation"] == before[key]["rotation"]
            assert store.blob_path(page["id"], item["source"]["blob"]).is_file()
        else:
            assert item["text"] == "Warm rim light"
    assert store.thumbnail_path(page["id"]).is_file()

    panel.import_boards([shared])  # (as the Import button and dropping do)
    assert pump(qapp, lambda: not panel.importing())
    assert panel.page["name"] == page["name"] and len(board.items_list()) == 3


def make_archive(path, entries, manifest=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if manifest is not False:
            archive.writestr("manifest.json", json.dumps(manifest or {"format": "artbook-page", "version": 1}))
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def test_opening_untrusted_files_is_safe(qapp, tmp_path):
    good_page = json.dumps({"name": "Shared", "items": [
        {"kind": "image", "source": {"path": "C:/Users/someone/secret.png", "blob": "../../evil.png"}, "size": [10, 10]},
        {"kind": "script", "code": "nope"},
    ]})
    sneaky = make_archive(tmp_path / "sneaky.artbook", {
        "page.json": good_page, "../../outside.png": b"x", "images/../../escape.png": b"x",
    })
    page = artbook_share.import_page(sneaky)
    assert [i["kind"] for i in page["items"]] == ["image"]  # unknown kinds dropped
    assert page["items"][0]["source"] == {"path": None, "blob": None}  # no foreign paths, no escaping names
    assert not (tmp_path / "outside.png").exists() and not (tmp_path.parent / "escape.png").exists()
    assert not store.images_dir(page["id"]).exists()

    cases = {
        "no manifest": (make_archive(tmp_path / "a.artbook", {"page.json": good_page}, manifest=False), "isn't an Artbook page"),
        "newer": (make_archive(tmp_path / "b.artbook", {"page.json": good_page}, {"format": "artbook-page", "version": 99}), "newer version"),
        "bomb": (make_archive(tmp_path / "c.artbook", {"page.json": good_page, "images/" + "a" * 64 + ".png": b"\0" * (50 * 1024 * 1024)}), "damaged"),
        "not a zip": (tmp_path / "d.artbook", "isn't an Artbook page"),
    }
    cases["not a zip"][0].write_bytes(b"hello")
    for label, (path, message) in cases.items():
        with pytest.raises(artbook_share.ShareError, match=message):
            artbook_share.import_page(path)
    assert [p["name"] for p in store.list_pages()] == ["Shared"]  # the failures left nothing behind


def test_a_tampered_picture_is_stored_under_its_real_hash(qapp, tmp_path):
    claimed = "b" * 64 + ".png"
    path = make_archive(tmp_path / "t.artbook", {
        "page.json": json.dumps({"name": "T", "items": [{"kind": "image", "source": {"blob": claimed}, "size": [1, 1]}]}),
        f"images/{claimed}": b"real data",
    })
    page = artbook_share.import_page(path)
    blob = page["items"][0]["source"]["blob"]
    assert blob == hashlib.sha256(b"real data").hexdigest() + ".png"
