"""Value and proportion tools in the Artbook (Values, Guides, Mirror Board: ui.artbook_board) and a video's
frame to the Artbook, the clipboard or a file (ui.video_player)."""

from __future__ import annotations

import time

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest

from core import settings
from ui.animations import set_motion_mode
from ui.artbook_board import ImageItem, custom_grid, guide_lines, _clean_guides, _values_image


def pump(qapp, condition=lambda: False, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def gradient(width=256, height=8, alpha=False) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    for x in range(width):
        for y in range(height):
            image.setPixelColor(x, y, Qt.GlobalColor.black)
            color = image.pixelColor(x, y)
            color.setRgb(x, x, x, 0 if alpha and x < 10 else 255)
            image.setPixelColor(x, y, color)
    return image


def greys(image: QImage) -> set[int]:
    return {image.pixelColor(x, 4).red() for x in range(image.width()) if image.pixelColor(x, 4).alpha() == 255}


# ----------------------------------------------------------------------
# Values and guides, on their own
# ----------------------------------------------------------------------
@pytest.mark.parametrize("tones", [2, 3, 4, 5])
def test_values_are_that_many_flat_greys_from_black_to_white(tones):
    shown = greys(_values_image(gradient(), tones))
    assert len(shown) == tones and min(shown) == 0 and max(shown) == 255


def test_values_keep_transparency():
    image = _values_image(gradient(alpha=True), 3)
    assert image.pixelColor(2, 4).alpha() == 0 and image.pixelColor(200, 4).alpha() == 255


def test_guide_lines():
    rect = QRectF(0, 0, 300, 120)
    thirds = guide_lines("thirds", rect)
    assert len(thirds) == 4 and sorted(line.x1() for line in thirds if line.dx() == 0) == [100, 200]
    assert len(guide_lines("grid:4:4", rect)) == 6 and len(guide_lines("grid:8:8", rect)) == 14
    custom = guide_lines("grid:6:3", rect)
    assert len([line for line in custom if line.dx() == 0]) == 5 and len([line for line in custom if line.dy() == 0]) == 2
    golden = sorted(line.x1() for line in guide_lines("golden", rect) if line.dx() == 0)
    assert golden[0] == pytest.approx(300 * 0.381966) and golden[1] == pytest.approx(300 * 0.618034)
    assert len(guide_lines("center", rect)) == 4 and guide_lines("", rect) == []
    assert custom_grid("grid:6:3") == (6, 3) and custom_grid("grid:0:3") is None and custom_grid("grid:40:2") is None
    assert _clean_guides("grid:12:5") == "grid:12:5" and _clean_guides("spiral") == "" and _clean_guides(7) == ""


# ----------------------------------------------------------------------
# On the board
# ----------------------------------------------------------------------
@pytest.fixture
def panel(qapp, tmp_path):
    from ui.artbook import ArtbookPanel
    from ui.themes import apply_theme

    set_motion_mode("off")
    apply_theme("dark", qapp)
    array = np.tile(np.arange(256, dtype=np.uint8), (128, 1))
    Image.fromarray(np.stack([array] * 3, axis=-1)).save(tmp_path / "ramp.png")
    widget = ArtbookPanel()
    widget.resize(1100, 750)
    widget.show()
    widget.sidebar.set_open(False, animate=False)
    widget.board.add_files([tmp_path / "ramp.png"])
    assert pump(qapp, lambda: widget.board.items_list() and widget.board.items_list()[0].pixmap is not None)
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    set_motion_mode("on")


def only(board) -> ImageItem:
    item = board.items_list()[0]
    board._scene.clearSelection()
    item.setSelected(True)
    return item


def test_values_and_guides_on_an_image_are_one_undo_step_each_and_saved(qapp, panel):
    board = panel.board
    item = only(board)
    steps = board.undo_stack.count()
    board.set_values(3)
    board.set_guides("thirds")
    assert board.undo_stack.count() == steps + 2 and item.values == 3 and item.guides == "thirds"
    assert len(greys(item._shown_pixmap().toImage())) == 3
    saved = item.to_dict()
    assert saved["values"] == 3 and saved["guides"] == "thirds"
    again = ImageItem.from_dict(saved)
    assert again.values == 3 and again.guides == "thirds"
    assert ImageItem.from_dict({**saved, "values": 9, "guides": "spiral"}).values == 0
    board.undo_stack.undo()
    board.undo_stack.undo()
    assert item.values == 0 and item.guides == ""
    assert len(greys(item._shown_pixmap().toImage())) > 100  # (its own colours again)


def test_custom_grid_asks_and_remembers(qapp, panel, monkeypatch):
    from ui import artbook_board

    board = panel.board
    item = only(board)
    assert settings.get_artbook_custom_grid() == (6, 6)

    def answer(dialog):
        dialog.spin_columns.setValue(5)
        dialog.spin_rows.setValue(2)
        return artbook_board.QDialog.DialogCode.Accepted

    monkeypatch.setattr(artbook_board.CustomGridDialog, "exec", answer)
    board.ask_custom_grid()
    assert item.guides == "grid:5:2" and settings.get_artbook_custom_grid() == (5, 2)
    labels = [entry[0] for entry in board._guides_menu([item]) if entry]
    assert "Custom Grid (5x2)..." in labels


def test_guides_are_drawn_over_the_image(qapp, panel):
    board = panel.board
    item = only(board)
    board.set_guides("grid:2:2")
    board._scene.clearSelection()
    board.frame([item])
    qapp.processEvents()
    middle = board.mapFromScene(item.mapToScene(item.crop_rect().center()))
    image = board.viewport().grab().toImage()
    line = image.pixelColor(middle.x(), middle.y() - 20)  # (on the vertical line, above the crossing)
    beside = image.pixelColor(middle.x() + 15, middle.y() - 20)
    assert line.red() > beside.red() + 40  # (a white line over the mid-grey of the ramp)


def test_values_board_with_its_tones_is_saved_with_the_page(qapp, panel):
    board = panel.board
    item = only(board)
    board.toggle_board_values()
    assert board._scene.values_all == 3 and item.shown_values() == 3  # (3 to start with)
    board.pick_board_tones(5)
    assert settings.get_artbook_value_tones() == 5 and len(greys(item._shown_pixmap().toImage())) == 5
    assert board.view_state()["values"] == 5
    panel.save_now()
    page_id = panel.current_page_id()
    panel.page = None
    panel.open_page(page_id)
    assert panel.board._scene.values_all == 5
    panel.board.toggle_board_values()
    assert panel.board._scene.values_all == 0


def test_mirror_board_flips_only_the_view(qapp, panel):
    board = panel.board
    item = only(board)
    board.frame([item])
    qapp.processEvents()
    zoom, center = board.zoom(), board.view_center()
    before = item.state()
    steps = board.undo_stack.count()
    board.toggle_mirror()
    assert board.mirrored and board.zoom() == pytest.approx(zoom) and board.transform().m11() < 0
    assert (board.view_center() - center).manhattanLength() < 2
    assert item.state() == before and board.undo_stack.count() == steps  # (nothing on the page changed)
    # Dragging still moves things the way the mouse goes on screen
    start = board.mapFromScene(item.mapToScene(item.crop_rect().center()))
    QTest.mousePress(board.viewport(), Qt.MouseButton.LeftButton, pos=start)
    for step in range(1, 6):
        QTest.mouseMove(board.viewport(), start + QPoint(step * 20, 0))
    QTest.mouseRelease(board.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(100, 0))
    end = board.mapFromScene(item.mapToScene(item.crop_rect().center()))
    assert end.x() - start.x() == pytest.approx(100, abs=3)
    assert item.state().x < before.x  # (right on a mirrored screen is left on the board)
    board.toggle_mirror()
    assert not board.mirrored and board.transform().m11() > 0


# ----------------------------------------------------------------------
# A video's frame
# ----------------------------------------------------------------------
from tests.test_video_player import clip, loaded, player  # noqa: E402,F401  (fixtures)
from core.video_tools import find_ffmpeg  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg isn't installed (it makes the test clip)")


@needs_ffmpeg
def test_a_frame_goes_to_the_artbook_the_clipboard_or_a_file(qapp, player, clip, tmp_path):
    from PySide6.QtWidgets import QApplication

    sent = []

    class Sender:
        def fill_artbook_menu(self, menu, on_pick):
            menu.addAction("New Page").triggered.connect(lambda: on_pick(None))

        def send_image_to_artbook(self, image, name, page_id):
            sent.append((image.size().toTuple(), name, page_id))
            return "Added 1 image to the Artbook."

    player.artbook = Sender()
    player.load(clip)
    assert pump(qapp, loaded(player))
    player.player.setPosition(12_000)
    player.player.pause()
    assert pump(qapp, lambda: player.current_frame() is not None and abs(player.player.position() - 12_000) < 500)
    assert player.current_frame().size().toTuple() == (320, 240)  # (the video's own size, not the screen's)
    player.send_frame(None)
    assert sent == [((320, 240), "test clip ü 0:12", None)]
    assert "Added 1 image" in player.lbl_toast.text()
    player.copy_frame()
    assert QApplication.clipboard().image().size().toTuple() == (320, 240)
    QApplication.clipboard().clear()
    player.save_frame(tmp_path / "frame")
    assert QImage(str(tmp_path / "frame.png")).size().toTuple() == (320, 240)
