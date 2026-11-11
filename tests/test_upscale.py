"""Upscaling Artbook images (core.upscaler, ui.artbook_upscale). Real-ESRGAN itself isn't run here: its
download is a zip made in the test, and the model is a plain resize (or a stand-in program for how a
run is watched). The real one was tried by hand on an RTX 4070."""

from __future__ import annotations

import hashlib
import sys
import threading
import time
import zipfile

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QWheelEvent

from core import artbook_store, upscaler
from core.archives import Cancelled
from ui.animations import set_motion_mode


def pump(qapp, condition=lambda: False, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# ----------------------------------------------------------------------
# Sizes
# ----------------------------------------------------------------------
def test_how_big_a_picture_becomes():
    assert upscaler.target_size((300, 168), 4) == (1200, 672)
    assert upscaler.target_size((300, 168), 2) == (600, 336)
    assert upscaler.target_size((2000, 1000), 4) == (6000, 3000)  # (at most MAX_SIDE)
    assert upscaler.target_size((4000, 10), 2) == (6000, 15)
    assert upscaler.target_size((5000, 100), 4) is None  # (less than MIN_GAIN bigger: not worth it)


# ----------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------
def fake_download(tmp_path, monkeypatch, damaged=False):
    """A zip like the real one (with the demo files that aren't kept), served from a file."""
    archive = tmp_path / "realesrgan.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name in sorted(upscaler._KEEP):
            z.writestr(name, f"contents of {name}")
        z.writestr("input.jpg", "a demo picture")
        z.writestr("onepiece_demo.mp4", "a demo video")
        z.writestr("models/realesr-animevideov3-x2.bin", "a model that isn't used")
    data = archive.read_bytes()
    digest = hashlib.sha256(data + (b"!" if damaged else b"")).hexdigest()
    monkeypatch.setattr(upscaler, "DOWNLOAD", upscaler.DOWNLOAD.__class__(upscaler.DOWNLOAD.name, archive.as_uri(), len(data), digest))


def test_install_checks_the_download_and_keeps_only_what_it_uses(tmp_path, monkeypatch):
    fake_download(tmp_path, monkeypatch)
    seen = []
    upscaler.install(lambda done, total: seen.append((done, total)))
    assert upscaler.is_installed() and seen[-1][0] == seen[-1][1]
    kept = {p.relative_to(upscaler.engine_folder()).as_posix() for p in upscaler.engine_folder().rglob("*") if p.is_file()}
    assert kept == upscaler._KEEP | {"installed.json"}
    assert not (upscaler._root() / "unpacking").exists()
    upscaler.remove()
    assert not upscaler.is_installed()


def test_a_download_that_isnt_exactly_the_file_is_refused(tmp_path, monkeypatch):
    fake_download(tmp_path, monkeypatch, damaged=True)
    with pytest.raises(upscaler.UpscaleError, match="didn't download correctly"):
        upscaler.install()
    assert not upscaler.is_installed() and not upscaler.engine_folder().exists()


# ----------------------------------------------------------------------
# Upscaling
# ----------------------------------------------------------------------
@pytest.fixture
def fake_model(monkeypatch):
    """Real-ESRGAN "installed", its model a 4x resize. Yields the sizes it was given."""
    folder = upscaler.engine_folder()
    folder.mkdir(parents=True)
    (folder / "installed.json").write_text("{}", encoding="utf-8")
    given = []

    def run(source, result, model, progress, should_stop):
        with Image.open(source) as im:
            given.append((im.size, model, im.mode))
            im.resize((im.width * 4, im.height * 4)).save(result)
        if progress is not None:
            progress(1.0)

    monkeypatch.setattr(upscaler, "_run_model", run)
    return given


def test_upscaling_keeps_transparency_and_sizes(fake_model):
    picture = Image.new("RGBA", (100, 50), (200, 30, 30, 255))
    picture.paste((0, 0, 0, 0), (0, 0, 50, 50))  # (the left half see-through)
    result = upscaler.upscale(picture, "illustration", 4)
    assert result.size == (400, 200) and result.mode == "RGBA"
    assert result.getpixel((10, 100))[3] == 0 and result.getpixel((390, 100))[3] == 255
    assert fake_model[-1] == ((100, 50), "illustration", "RGB")  # (the model only sees colour)
    assert upscaler.upscale(Image.new("RGB", (100, 50)), "photo", 2).size == (200, 100)
    big = upscaler.upscale(Image.new("RGB", (4000, 2000)), "photo", 4)
    assert big.size == (6000, 3000) and fake_model[-1][0] == (3000, 1500)  # (made smaller for the model first)
    with pytest.raises(upscaler.UpscaleError, match="big enough"):
        upscaler.upscale(Image.new("RGB", (5900, 100)), "photo", 4)
    assert not any(upscaler.jobs_folder().iterdir())  # (each picture's folder goes when it's done)


STAND_IN = r'''
import sys, time
mode, out = sys.argv[1], sys.argv[sys.argv.index("-o") + 1]
sys.stderr.write("[0 Stand-in GPU]  queueC=1[1]\n"); sys.stderr.flush()
if mode == "nogpu":
    sys.stderr.write("vkCreateInstance failed -9\n"); sys.exit(0)
for done in ("0.00%", "50.00%", "100.00%"):
    sys.stderr.write(done + "\n"); sys.stderr.flush()
if mode == "slow":
    time.sleep(30)
if mode != "nothing":
    open(out, "wb").write(b"made")
'''


@pytest.mark.parametrize("mode", ["works", "nogpu", "nothing", "slow"])
def test_a_run_is_watched_for_progress_errors_and_stop(tmp_path, monkeypatch, mode):
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN, encoding="utf-8")
    upscaler.engine_folder().mkdir(parents=True)
    monkeypatch.setattr(upscaler, "_command", lambda source, result, model: [sys.executable, str(script), mode, "-o", str(result)])
    result, shares = tmp_path / "out.png", []
    stop = threading.Event()
    if mode == "slow":
        threading.Timer(1.0, stop.set).start()
    start = time.monotonic()
    try:
        upscaler._run_model(tmp_path / "in.png", result, "photo", shares.append, stop.is_set)
        outcome = "done"
    except upscaler.UpscaleError as exc:
        outcome = str(exc)
    except Cancelled:
        outcome = "cancelled"
    if mode == "works":
        assert outcome == "done" and shares == [0.0, 0.5, 1.0]
    elif mode == "nogpu":
        assert "graphics card that supports Vulkan" in outcome
    elif mode == "nothing":
        assert "couldn't upscale it" in outcome  # (it said it finished, but there's no picture)
    else:
        assert outcome == "cancelled" and time.monotonic() - start < 10


# ----------------------------------------------------------------------
# The Artbook
# ----------------------------------------------------------------------
@pytest.fixture
def board(qapp, tmp_path):
    from ui.artbook import ArtbookPanel
    from ui.themes import apply_theme

    set_motion_mode("off")
    apply_theme("dark", qapp)
    files = []
    for name, size, color in (("small.png", (120, 80), "red"), ("tiny.jpg", (60, 90), "blue"), ("huge.png", (5000, 200), "gray")):
        path = tmp_path / name
        Image.new("RGB", size, color).save(path)
        files.append(path)
    panel = ArtbookPanel()
    panel.resize(1100, 750)
    panel.show()
    panel.board.add_files(files)
    assert pump(qapp, lambda: len(panel.board.items_list()) == 3 and all(i.base is not None for i in panel.board.items_list()))
    yield panel
    if panel.board.upscale_dialog is not None:
        panel.board.upscale_dialog.reject()
    panel.close()
    panel.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    set_motion_mode("on")


def by_name(board, name):
    return next(i for i in board.items_list() if i.display_name() == name)


def open_dialog(panel, names):
    board = panel.board
    board._scene.clearSelection()
    for name in names:
        by_name(board, name).setSelected(True)
    board.upscale_selection()
    return board.upscale_dialog


def test_upscaled_pictures_go_on_the_board_in_the_same_place_and_can_be_reverted(qapp, board, fake_model):
    small, tiny = by_name(board.board, "small.png"), by_name(board.board, "tiny.jpg")
    small.setScale(3.0)
    places = {i.item_id: i.sceneBoundingRect() for i in (small, tiny)}
    dialog = open_dialog(board, ["small.png", "tiny.jpg"])
    assert [row["eligible"] for row in dialog.rows] == [True, True]
    dialog.list.selectAll()
    assert dialog.btn_upscale.text() == "Upscale 2 Images" and not dialog.btn_apply.isEnabled()
    dialog.btn_upscale.click()
    assert pump(qapp, lambda: not dialog.busy() and all(r["result"] for r in dialog.rows))
    assert "Photo, 4x: 480 x 320" in dialog.list.item([r["item"] for r in dialog.rows].index(small)).text()
    tiny_row = [r["item"] for r in dialog.rows].index(tiny)
    dialog.list.item(tiny_row).setCheckState(Qt.CheckState.Unchecked)  # (not this one)
    assert dialog.btn_apply.text() == "Use 1 Upscaled Picture"
    steps = board.board.undo_stack.count()
    dialog.btn_apply.click()
    assert board.board.undo_stack.count() == steps + 1
    assert small.natural == (480, 320) and small.scale() == pytest.approx(0.75) and tiny.natural == (60, 90)
    assert small.sceneBoundingRect().toRect() == places[small.item_id].toRect()  # (the same on the board)
    assert small.original["size"] == [120, 80] and small.original["source"]["path"].endswith("small.png")
    assert small.display_name() == "small.png"  # (the name it had from its file stays)

    # Saved with the page, so Revert works after reopening it
    board.save_now()
    saved = next(i for i in artbook_store.load_page(board.board.page_id)["items"] if i["id"] == small.item_id)
    assert saved["original"]["size"] == [120, 80] and saved["source"]["path"] is None
    assert artbook_store.blob_path(board.board.page_id, saved["source"]["blob"]).is_file()

    board.board._scene.clearSelection()
    small.setSelected(True)
    board.board.revert_upscales()
    assert small.natural == (120, 80) and small.original is None and small.scale() == pytest.approx(3.0)
    assert small.source["path"].endswith("small.png")
    board.board.undo_stack.undo()
    assert small.natural == (480, 320) and small.original is not None


def test_upscaling_again_starts_from_the_original(qapp, board, fake_model):
    dialog = open_dialog(board, ["small.png"])
    dialog.btn_upscale.click()
    assert pump(qapp, lambda: not dialog.busy() and dialog.rows[0]["result"])
    dialog.btn_apply.click()
    item = by_name(board.board, "small.png")
    item.setSelected(True)
    dialog = open_dialog(board, ["small.png"])
    assert dialog.rows[0]["size"] == (120, 80)
    dialog.factor_choice.buttons()[0].click()  # 2x
    dialog.btn_upscale.click()
    assert pump(qapp, lambda: not dialog.busy() and dialog.rows[0]["result"])
    assert fake_model[-1][0] == (120, 80)
    dialog.btn_apply.click()
    assert item.natural == (240, 160) and item.original["size"] == [120, 80]


def test_big_images_and_cancelling_change_nothing(qapp, board, fake_model):
    huge = by_name(board.board, "huge.png")
    dialog = open_dialog(board, ["huge.png"])
    assert not dialog.rows[0]["eligible"] and not dialog.btn_upscale.isEnabled()
    assert "Big enough already" in dialog.list.item(0).text()
    dialog.reject()
    dialog = open_dialog(board, ["small.png"])
    dialog.btn_upscale.click()
    assert pump(qapp, lambda: not dialog.busy() and dialog.rows[0]["result"])
    dialog.reject()
    assert by_name(board.board, "small.png").natural == (120, 80) and huge.natural == (5000, 200)


def test_the_window_asks_to_download_first(qapp, board):
    dialog = open_dialog(board, ["small.png"])
    assert dialog.btn_upscale.text().startswith("Download Real-ESRGAN (45 MB)")
    assert "downloads once" in dialog.status.text() and not dialog.btn_remove.isVisible()


def test_the_comparison_line_and_zoom(qapp):
    from ui.artbook_upscale import CompareView

    view = CompareView()
    view.resize(600, 400)
    view.show()
    before, after = QImage(100, 50, QImage.Format.Format_RGB32), QImage(400, 200, QImage.Format.Format_RGB32)
    before.fill(Qt.GlobalColor.red)
    after.fill(Qt.GlobalColor.blue)
    view.set_pictures(before, after)
    qapp.processEvents()
    assert view.grab().toImage().pixelColor(150, 200).red() > 200 and view.grab().toImage().pixelColor(450, 200).blue() > 200
    from PySide6.QtTest import QTest

    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(300, 200))  # (on the line)
    QTest.mouseMove(view, QPoint(120, 200))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(120, 200))
    assert view.split == pytest.approx(0.2, abs=0.01)
    wheel = QWheelEvent(QPointF(450, 200), QPointF(450, 200), QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(wheel)
    assert view.zoom == pytest.approx(1.25) and view.center.x() > 0.5  # (toward the cursor)
    view.mouseDoubleClickEvent(None)
    assert view.zoom == 1.0
    view.close()
    view.deleteLater()


def test_the_menu_offers_upscale_and_revert(qapp, board):
    from tests.test_artbook import menu_layout

    board.board._scene.clearSelection()
    by_name(board.board, "small.png").setSelected(True)
    menu = board.board.context_menu(QPointF(0, 0))
    images = next(a.menu() for a in menu.actions() if a.text() == "Images")
    actions = {a.text(): a for a in images.actions()}
    assert actions["Upscale..."].isEnabled() and not actions["Revert to Original"].isEnabled()
    assert "  Upscale..." in menu_layout(menu)
    menu.deleteLater()


def test_a_saved_original_is_checked_when_read(qapp):
    from ui.artbook_board import ImageItem

    good = {"id": "a", "kind": "image", "source": {"path": None, "blob": "b.png"}, "size": [400, 400],
            "original": {"source": {"path": "C:/x.png", "blob": "a.jpg"}, "size": [100, 100], "name": "x"}}
    assert ImageItem.from_dict(good).original == {"source": {"path": "C:/x.png", "blob": "a.jpg"}, "size": [100, 100], "name": "x"}
    for bad in ("nonsense", {"source": {}, "size": [1, 1]}, {"source": {"blob": "a"}, "size": [0, 5]}, {"source": {"blob": 3}, "size": [1, 1]}):
        assert ImageItem.from_dict({**good, "original": bad}).original is None
    assert "original" not in ImageItem.from_dict({**good, "original": None}).to_dict()
