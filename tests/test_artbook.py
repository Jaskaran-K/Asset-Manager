"""The Artbook board and its pages: storage, getting images in, editing, undo, autosave."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QImage, QKeyEvent
from PySide6.QtWidgets import QApplication

from core import artbook_store as store
from ui.animations import set_motion_mode


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
    set_motion_mode("off")  # zooms and fits land at once
    yield
    set_motion_mode("on")


@pytest.fixture
def pictures(tmp_path):
    folder = tmp_path / "pictures"
    folder.mkdir()
    Image.new("RGB", (400, 200), "red").save(folder / "wide.png")
    Image.new("RGBA", (100, 300), (0, 0, 255, 128)).save(folder / "tall.png")
    (folder / "model.fbx").write_bytes(b"not an image")
    return folder


@pytest.fixture
def panel(qapp):
    from ui.artbook import ArtbookPanel

    widget = ArtbookPanel()
    widget.resize(900, 700)
    widget.show()  # opens (here: creates) the page
    qapp.processEvents()
    yield widget
    widget.close()


def add_and_wait(qapp, board, paths):
    before = len(board.items_list())
    count = board.add_files(paths)
    assert pump(qapp, lambda: len(board.items_list()) == before + count)
    return board.items_list()[before:]


# ----------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------
def test_pages_round_trip_and_list(qapp):
    page = store.new_page("Mood")
    page["items"].append({"id": "a", "kind": "image"})
    store.save_page(page)
    assert store.load_page(page["id"])["items"] == [{"id": "a", "kind": "image"}]
    assert [p["name"] for p in store.list_pages()] == ["Mood"]
    folder = store.page_dir(page["id"])
    assert not list(folder.glob("*.tmp"))  # written whole, then swapped in
    store.delete_page(page["id"])
    assert store.list_pages() == []


def test_damaged_page_is_reported(qapp):
    page = store.new_page("Broken")
    (store.page_dir(page["id"]) / "page.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(store.PageError):
        store.load_page(page["id"])


def test_images_are_stored_once(qapp):
    page = store.new_page("Blobs")
    first = store.store_blob(page["id"], b"same bytes", ".png")
    assert store.store_blob(page["id"], b"same bytes", ".png") == first
    assert len(list(store.images_dir(page["id"]).iterdir())) == 1


def test_fallback_copies_are_small_and_keep_transparency(qapp):
    opaque = QImage(3000, 1500, QImage.Format.Format_RGB32)
    opaque.fill(QColor("red"))
    data, ext = store.encode_fallback(opaque)
    assert ext == ".jpg"
    image, _ = store.decode_bytes(data)
    assert max(image.width(), image.height()) == store.FALLBACK_SIDE
    seethrough = QImage(64, 64, QImage.Format.Format_ARGB32)
    seethrough.fill(QColor(0, 0, 255, 100))
    assert store.encode_fallback(seethrough)[1] == ".png"


# ----------------------------------------------------------------------
# Getting images in
# ----------------------------------------------------------------------
def test_files_are_added_side_by_side_linked_with_a_fallback(qapp, panel, pictures):
    board = panel.board
    wide, tall = add_and_wait(qapp, board, [pictures / "wide.png", pictures / "tall.png", pictures / "model.fbx"])
    assert "skipped 1" in board.toast.text()  # the .fbx isn't an image
    items = sorted(board.items_list(), key=lambda i: i.pos().x())
    assert [i.natural for i in items] == [(400, 200), (100, 300)]
    left, right = items
    assert right.sceneBoundingRect().left() > left.sceneBoundingRect().right()  # lined up, not stacked
    for item in items:
        assert item.source["path"].endswith(".png")
        assert store.blob_path(board.page_id, item.source["blob"]).is_file()  # copy in case the file moves
    assert not board.hint.isVisible()


def test_pasting_image_data_and_files(qapp, panel, pictures):
    board = panel.board
    image = QImage(50, 40, QImage.Format.Format_RGB32)
    image.fill(QColor("green"))
    QApplication.clipboard().setImage(image)
    assert board.paste()
    assert pump(qapp, lambda: len(board.items_list()) == 1)
    pasted = board.items_list()[0]
    assert pasted.source["path"] is None and pasted.natural == (50, 40)  # no file: stored in full

    # Copied files: what paste() does with the clipboard's contents. (Not through the real
    # clipboard: handing it a QMimeData from Python frees the object twice when the process exits.)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(pictures / "wide.png"))])
    assert board._take(mime, None)
    assert pump(qapp, lambda: len(board.items_list()) == 2)
    QApplication.clipboard().clear()


def test_dropping_from_the_asset_grid(qapp, panel, pictures):
    from ui.asset_grid import AssetListModel
    from core.scanner import AssetScanner

    assets = AssetScanner().scan(pictures)
    model = AssetListModel(lambda a: None, lambda a: "")
    model.set_assets([a for a in assets if a.name == "wide.png"])
    mime = model.mimeData([model.index(0)])  # what the grid hands over when dragged
    assert panel.board._take(mime, QPointF(500, 500))
    assert pump(qapp, lambda: len(panel.board.items_list()) == 1)
    assert panel.board.items_list()[0].pos() == QPointF(500, 500)  # where it was dropped


# ----------------------------------------------------------------------
# Editing
# ----------------------------------------------------------------------
def test_flip_restack_reset_delete_with_undo(qapp, panel, pictures):
    board = panel.board
    wide, tall = add_and_wait(qapp, board, [pictures / "wide.png", pictures / "tall.png"])
    board._scene.clearSelection()
    wide.setSelected(True)
    board.flip(True)
    assert wide.state().flip_h
    board.restack(True)
    assert wide.zValue() > tall.zValue()
    board.reset_transform()
    assert not wide.state().flip_h
    board.undo_stack.undo()
    assert wide.state().flip_h
    board.delete_selection()
    assert board.items_list() == [tall]
    board.undo_stack.undo()
    assert set(board.items_list()) == {wide, tall}
    board.undo_stack.redo()
    assert board.items_list() == [tall]


def test_corner_scaling_keeps_the_opposite_corner_and_the_shape(qapp, panel, pictures):
    board = panel.board
    (item,) = add_and_wait(qapp, board, [pictures / "wide.png"])
    board._scene.clearSelection()
    item.setSelected(True)
    corners = board._frame_corners()
    board._begin_transform(("scale", 2), board.mapFromScene(corners[2]))  # bottom-right handle
    fixed = corners[0]
    board._drag_scale(board._drag, corners[0] + (corners[2] - corners[0]) * 2)  # drag twice as far
    assert abs(item.scale() - 2.0) < 1e-6
    new_corners = board._frame_corners()
    assert abs(new_corners[0].x() - fixed.x()) < 1e-6 and abs(new_corners[0].y() - fixed.y()) < 1e-6
    board._push_states(board._drag["before"], "scale")
    board.undo_stack.undo()
    assert abs(item.scale() - 1.0) < 1e-6


def test_rotation_snaps_with_shift(qapp, panel, pictures):
    board = panel.board
    (item,) = add_and_wait(qapp, board, [pictures / "wide.png"])
    board._scene.clearSelection()
    item.setSelected(True)
    center = item.scenePos()
    board._drag = {"mode": "rotate", "before": board._snapshot([item]), "center": center,
                   "press": center + QPointF(100, 0)}
    board._drag_rotate(board._drag, center + QPointF(100, 40), snap=True)  # about 21.8 degrees
    assert item.rotation() == 15.0


def test_zoom_keeps_the_point_under_the_cursor(qapp, panel):
    board = panel.board
    anchor = QPointF(200, 150)
    before = board.mapToScene(anchor.toPoint())
    board.zoom_at(3.0, anchor)
    after = board.mapToScene(anchor.toPoint())
    assert abs(board.zoom() - 3.0) < 1e-9
    assert abs(after.x() - before.x()) < 1 and abs(after.y() - before.y()) < 1
    board.zoom_at(1000, anchor)
    assert board.zoom() == 32.0  # clamped


def test_frame_fits_everything(qapp, panel, pictures):
    board = panel.board
    items = add_and_wait(qapp, board, [pictures / "wide.png", pictures / "tall.png"])
    board.zoom_at(0.05, QPointF(10, 10))
    board._scene.clearSelection()
    board.frame()
    visible = board.mapToScene(board.viewport().rect()).boundingRect()
    for item in items:
        assert visible.contains(item.sceneBoundingRect())


def test_board_keys_beat_window_shortcuts(qapp, panel, pictures):
    board = panel.board
    add_and_wait(qapp, board, [pictures / "wide.png"])
    override = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert board.event(override) and override.isAccepted()  # the library's Ctrl+Z doesn't get it
    board.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier))
    assert board.items_list() == []  # undid the add
    board.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " "))
    assert board._space_down  # Space held: the left button pans


# ----------------------------------------------------------------------
# Saving
# ----------------------------------------------------------------------
def test_autosave_and_reopen_with_a_missing_file(qapp, pictures):
    from ui.artbook import ArtbookPanel

    first = ArtbookPanel()
    first.show()
    qapp.processEvents()
    (item,) = add_and_wait(qapp, first.board, [pictures / "wide.png"])
    item.setRotation(30)
    first.board.flip(True)  # an undoable change: schedules the autosave
    assert first._save_timer.isActive()
    first._save_in_background()
    assert pump(qapp, lambda: not first._saving)
    saved = json.loads((store.page_dir(first.page["id"]) / "page.json").read_text(encoding="utf-8"))
    assert saved["items"][0]["flip_h"] is True and saved["items"][0]["rotation"] == 30
    first.close()

    (pictures / "wide.png").unlink()  # the linked file is gone: the page's own copy shows instead
    second = ArtbookPanel()
    second.show()
    qapp.processEvents()
    assert second.page["id"] == first.page["id"]  # reopens the page that was open
    (reopened,) = second.board.items_list()
    assert pump(qapp, lambda: reopened.pixmap is not None)
    assert reopened.state().flip_h and reopened.rotation() == 30
    second.close()


def test_page_loads_only_when_first_shown(qapp):
    from ui.artbook import ArtbookPanel

    hidden = ArtbookPanel()
    assert hidden.page is None and store.list_pages() == []  # nothing read or created yet
    hidden.show()
    qapp.processEvents()
    assert hidden.page is not None and len(store.list_pages()) == 1
    hidden.close()


# ----------------------------------------------------------------------
# Web images (Google Images and the like)
# ----------------------------------------------------------------------
@pytest.fixture
def web(tmp_path):
    """A local web server: a real image, one sent with the wrong type, a page with a preview, a non-image."""
    import http.server
    import io as io_lib
    import threading

    buffer = io_lib.BytesIO()
    Image.new("RGB", (321, 123), "purple").save(buffer, "PNG")
    png = buffer.getvalue()
    routes = {
        "/real.png": (png, "image/png"),
        "/mislabeled": (png, "text/plain"),
        "/page.html": (b'<html><head><meta property="og:image" content="/real.png"></head></html>', "text/html"),
        "/notimage": (b"hello", "text/plain"),
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body, kind = routes.get(self.path.split("?")[0], (b"missing", "text/plain"))
            self.send_response(200 if self.path.split("?")[0] in routes else 404)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", png
    server.shutdown()
    server.server_close()


def test_web_sources_prefer_the_real_image_over_the_result_link():
    from ui.artbook_board import web_image_sources

    link = "https://www.google.com/imgres?imgurl=https%3A%2F%2Fcdn.example.com%2Fcat.jpg&imgrefurl=https%3A%2F%2Fexample.com"
    html = '<img alt="cat" src="data:image/jpeg;base64,AAAA"><img src="https://encrypted-tbn0.gstatic.com/t.jpg">'
    assert web_image_sources([link], html) == [
        "https://cdn.example.com/cat.jpg",  # the full image the result points to
        "https://encrypted-tbn0.gstatic.com/t.jpg",  # the picture that was dragged
        "data:image/jpeg;base64,AAAA",  # its embedded copy: works even offline
        link,
    ]


def test_web_images_come_from_whatever_works(qapp, web):
    from ui.artbook_board import _fetch_image, _prepare_web

    base, png = web
    assert _fetch_image(f"{base}/mislabeled") == (png, ".png")  # the server's type isn't trusted
    assert _fetch_image(f"{base}/page.html")[0] == png  # a page: its preview image
    import base64
    assert _fetch_image("data:image/png;base64," + base64.b64encode(png).decode())[0] == png
    with pytest.raises(store.PageError):
        _fetch_image(f"{base}/notimage")
    page = store.new_page("Web")
    result = _prepare_web(page["id"], [f"{base}/notimage", f"{base}/real.png"])  # first one fails: next
    assert result["size"] == (321, 123) and result["source"]["path"] is None


def test_dragging_a_google_images_result(qapp, panel, web):
    import base64
    from urllib.parse import quote

    base, png = web
    mime = QMimeData()
    # What a browser hands over: the result's link (a page, not the image) plus the picture as HTML
    mime.setUrls([QUrl(f"{base}/notimage?imgurl={quote(base + '/real.png', safe='')}")])
    mime.setHtml(f'<img src="data:image/png;base64,{base64.b64encode(png).decode()}">')
    assert panel.board._take(mime, QPointF(0, 0))
    assert pump(qapp, lambda: len(panel.board.items_list()) == 1)
    assert panel.board.items_list()[0].natural == (321, 123)


# ----------------------------------------------------------------------
# Hover cursors
# ----------------------------------------------------------------------
def test_cursor_says_what_each_spot_does(qapp, panel, pictures):
    board = panel.board
    (item,) = add_and_wait(qapp, board, [pictures / "wide.png"])
    board._scene.clearSelection()
    item.setSelected(True)
    corners = [board.mapFromScene(c) for c in board._frame_corners()]
    shape = lambda pos: board.hover_cursor(QPointF(pos)).shape()  # noqa: E731
    assert shape(corners[0]) == Qt.CursorShape.SizeFDiagCursor  # top-left: scales along \
    assert shape(corners[1]) == Qt.CursorShape.SizeBDiagCursor  # top-right: along /
    handle = board.mapFromScene(board._rotate_handle(board._frame_corners()))
    assert shape(handle) == Qt.CursorShape.BitmapCursor  # the rotate arrow
    assert shape(board.mapFromScene(item.scenePos())) == Qt.CursorShape.SizeAllCursor  # move
    assert shape(QPointF(2, 2)) == Qt.CursorShape.ArrowCursor
    item.setRotation(45)  # the corners now point straight sideways / up and down
    corners = [board.mapFromScene(c) for c in board._frame_corners()]
    assert {shape(c) for c in corners} <= {Qt.CursorShape.SizeHorCursor, Qt.CursorShape.SizeVerCursor,
                                           Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeBDiagCursor}
    assert shape(corners[0]) != Qt.CursorShape.SizeFDiagCursor  # follows the rotation


def test_hover_actually_sets_the_cursor(qapp, panel, pictures):
    from PySide6.QtGui import QMouseEvent

    board = panel.board
    (item,) = add_and_wait(qapp, board, [pictures / "wide.png"])
    board._scene.clearSelection()
    item.setSelected(True)
    corner = QPointF(board.mapFromScene(board._frame_corners()[2]))
    move = QMouseEvent(QEvent.Type.MouseMove, corner, board.viewport().mapToGlobal(corner),
                       Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(board.viewport(), move)
    assert board.viewport().cursor().shape() == Qt.CursorShape.SizeFDiagCursor  # not reset by Qt afterwards


def all_actions(menu):
    """Every action in a menu and its submenus (submenus themselves included)."""
    for action in menu.actions():
        yield action
        if action.menu():
            yield from all_actions(action.menu())


def menu_layout(menu, depth=0):
    rows = []
    for action in menu.actions():
        if action.isSeparator():
            rows.append("  " * depth + "---")
            continue
        rows.append("  " * depth + action.text() + (" >" if action.menu() else ""))
        if action.menu():
            rows += menu_layout(action.menu(), depth + 1)
    return rows


def test_menu_layout(qapp, panel):
    menu = panel.board.context_menu(QPointF(0, 0))
    assert menu_layout(menu) == [
        "Undo", "Redo", "---", "Copy >", "  Image", "  File Path", "Paste", "Note", "Colour Picker", "Draw", "---",
        "Canvas >", "  Frame Everything", "  Zoom to 100%", "  Grayscale Board", "  ---", "  Export as PNG...",
        "Images >", "  Select All", "  Image List",
        "  Selection >",
        "    Crop", "    Grayscale", "    Opacity >", "      ", "    Pixelated", "    Caption...", "    ---",
        "    Group", "    Ungroup",
        "    Group Colour >", "      Theme Default", "      White", "      Black", "      ---", "      Custom...", "    Lock Group", "    ---",
        "    Flip Horizontally", "    Flip Vertically",
        "    Reset >", "      All", "      ---", "      Rotation", "      Size", "      Flips", "      Crop", "    ---",
        "    Bring to Front", "    Send to Back", "    ---", "    Delete",
        "  Arrange >", "    Auto-Arrange",
        "  Align >", "    Left", "    Centers", "    Right", "    Top", "    Middles", "    Bottom", "    ---",
        "    Distribute Horizontally", "    Distribute Vertically", "    ---", "    Stack",
        "  Match Size >", "    Same Height", "    Same Width",
        "  File >", "    Show in Explorer", "    Open Source File", "    ---", "    Replace Image...",
        "    Relink Missing Images...",
        "Pages >", "  New Page", "  Duplicate Page", "  Rename Page...", "  ---", "  Export Page (.artbook)...",
    ]
    submenus = [a for a in all_actions(menu) if a.menu()]
    assert submenus and all(a.isEnabled() for a in submenus)  # always open: greyed items show what's there
    menu.deleteLater()


def test_menu_greys_out_what_cant_be_done(qapp, panel, pictures):
    board = panel.board

    def open_menu():
        menu = board.context_menu(QPointF(0, 0))
        shown = {a.text(): a.isEnabled() for a in all_actions(menu) if a.text()}
        menu.deleteLater()
        return shown

    QApplication.clipboard().setText("just some words")
    menu = open_menu()
    assert not menu["Undo"] and not menu["Redo"]
    assert not menu["Paste"] and not menu["Select All"] and not menu["Frame Everything"]
    assert not any(menu[name] for name in ("Image", "File Path", "Flip Horizontally", "Bring to Front", "Delete"))

    QApplication.clipboard().setText(str(pictures / "wide.png"))  # a copied file path can be pasted
    (item,) = add_and_wait(qapp, board, [pictures / "wide.png"])
    board._scene.clearSelection()
    menu = open_menu()
    assert menu["Undo"]  # the image was added
    assert menu["Paste"] and menu["Select All"] and menu["Frame Everything"]
    assert not menu["Delete"] and not menu["Image"]  # nothing selected yet
    item.setSelected(True)
    menu = open_menu()
    assert all(menu[name] for name in ("Image", "File Path", "Show in Explorer", "Flip Horizontally", "Bring to Front", "Delete", "Frame Selection"))


def test_frame_from_the_menu_matches_the_f_key(qapp, panel, pictures):
    board = panel.board
    first, second = add_and_wait(qapp, board, [pictures / "wide.png", pictures / "tall.png"])
    board._scene.clearSelection()
    second.setSelected(True)
    board.frame()  # F
    expected = (board.zoom(), board.view_center())
    board.set_view(0.3, QPointF(5000, -5000))
    menu = board.context_menu(QPointF(0, 0))
    next(a for a in all_actions(menu) if a.text() == "Frame Selection").trigger()
    menu.deleteLater()
    assert board.zoom() == pytest.approx(expected[0])
    assert board.view_center().x() == pytest.approx(expected[1].x(), abs=1)
    assert board.view_center().y() == pytest.approx(expected[1].y(), abs=1)
    assert board.view_center().x() == pytest.approx(second.sceneBoundingRect().center().x(), abs=1)
