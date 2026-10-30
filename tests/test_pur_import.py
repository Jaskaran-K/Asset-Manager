"""Importing PureRef boards (core.pur_import): layouts land exactly where they were, as Artbook pages.

The boards here are made by make_board: a small database laid out like a real board file (see the
pur_import docstring). Only the tests write that layout; the app only reads it.
"""

from __future__ import annotations

import io
import math
import sqlite3
import struct
import time

import pytest
from PIL import Image
from PySide6.QtCore import QPointF

from core import artbook_store as store
from core import pur_import

PIXELS = 5  # (an image's pixels sit in its item centered: see _image_row)


def _qt_text(data: bytes) -> str:
    return data.decode("latin-1")  # (saved as text: bytes above 127 become two UTF-8 bytes)


def qt_transform(m11=1.0, m12=0.0, m21=0.0, m22=1.0, dx=0.0, dy=0.0) -> str:
    return _qt_text(struct.pack(">IB9d", 80, 0, m11, m12, 0.0, m21, m22, 0.0, dx, dy, 1.0))


def qt_path(points) -> str:
    name = b"QPainterPath\x00"
    data = struct.pack(">IBI", 1024, 0, len(name)) + name + struct.pack(">i", len(points))
    for i, (x, y) in enumerate(points):
        data += struct.pack(">idd", 0 if i == 0 else 1, x, y)
    return _qt_text(data + struct.pack(">i", 0))


def png(w, h, color="red") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
    return out.getvalue()


def make_board(path, images=(), items=(), item_images=(), notes=(), groups=(), drawings=(), version="2.1"):
    """A board file: rows for each table, as (id, ...) tuples."""
    database = path.with_suffix(".db")
    con = sqlite3.connect(database)
    con.executescript("""
        CREATE TABLE images (id INTEGER PRIMARY KEY, source_type INTEGER, origin TEXT, source TEXT, format TEXT,
                             checksum TEXT, data BLOB, width INTEGER, height INTEGER);
        CREATE TABLE metadata (id INTEGER PRIMARY KEY, thumbnail BLOB);
        CREATE TABLE items (parent INTEGER, id INTEGER PRIMARY KEY, name TEXT, transform BLOB, sort_order BLOB,
                            z REAL, opacity REAL, locked INTEGER, comment INTEGER);
        CREATE TABLE items_images (image INTEGER, playback_speed REAL, id INTEGER PRIMARY KEY, playback_state INTEGER,
                                   image_transform BLOB, image_bounds BLOB, playback_frame INTEGER, flags INTEGER);
        CREATE TABLE items_drawings (id INTEGER PRIMARY KEY, strokes BLOB);
        CREATE TABLE items_notes (text_color TEXT, id INTEGER PRIMARY KEY, fixed_size TEXT, background_color TEXT,
                                  text TEXT, style INTEGER);
        CREATE TABLE items_groups (id INTEGER PRIMARY KEY, background_color TEXT, lock_mode INTEGER);
    """)
    for image_id, fmt, data, w, h in images:
        con.execute("insert into images (id, source_type, format, data, width, height) values (?, 1, ?, ?, ?, ?)", (image_id, fmt, data, w, h))
    for item_id, parent, transform, z, opacity in items:
        con.execute("insert into items (id, parent, name, transform, z, opacity, locked) values (?, ?, '', ?, ?, ?, 0)", (item_id, parent, transform, z, opacity))
    for item_id, image_id, image_transform, bounds in item_images:
        con.execute("insert into items_images (id, image, image_transform, image_bounds, flags) values (?, ?, ?, ?, 1)", (item_id, image_id, image_transform, bounds))
    for item_id, text, background in notes:
        con.execute("insert into items_notes (id, text, background_color, style) values (?, ?, ?, 1)", (item_id, text, background))
    for group_id in groups:
        con.execute("insert into items_groups (id, background_color, lock_mode) values (?, '#ff4f3f3f', 0)", (group_id,))
    for drawing_id in drawings:
        con.execute("insert into items_drawings (id, strokes) values (?, x'00')", (drawing_id,))
    con.commit()
    con.close()
    data = database.read_bytes()
    database.unlink()
    name = version.encode("utf-16-be")
    header = struct.pack(">I", len(name)) + name
    header = header.ljust(14, b"\x00") + struct.pack(">Q", len(data)) + b"\xff\xd8 a preview"
    # The header goes over the database's first bytes, which move to the end
    path.write_bytes(header + data[len(header):] + data[:len(header)])
    return path


def centered(w, h, scale=1.0):
    """An image's pixels in its item: centered (as boards save them)."""
    return qt_transform(scale, 0, 0, scale, -w * scale / 2, -h * scale / 2)


def outline(w, h, left=0.0, top=0.0, right=None, bottom=None, scale=1.0):
    right = w if right is None else right
    bottom = h if bottom is None else bottom
    to_item = lambda x, y: ((x - w / 2) * scale, (y - h / 2) * scale)  # noqa: E731
    return qt_path([to_item(left, top), to_item(right, top), to_item(right, bottom), to_item(left, bottom), to_item(left, top)])


def rotation(degrees, scale=1.0, dx=0.0, dy=0.0, flip=False):
    c, s = math.cos(math.radians(degrees)) * scale, math.sin(math.radians(degrees)) * scale
    f = -1.0 if flip else 1.0
    return qt_transform(f * c, f * s, -s, c, dx, dy)  # (a flip, then the rotation: as on the board)


@pytest.fixture
def board_file(tmp_path):
    return make_board(
        tmp_path / "Hands.pur",
        images=[(1, "png", png(40, 20), 40, 20), (2, "JPG", png(30, 30, "blue"), 30, 30), (3, "", png(10, 10), 10, 10)],
        items=[
            (10, -1, qt_transform(2, 0, 0, 2, 100, 50), 5, 1.0),  # plain, scale 2
            (11, -1, rotation(90, 1.5, -300, 80), 9, 0.5),  # rotated, see-through, cropped
            (12, -1, rotation(30, 1.0, 400, -200, flip=True), 1, 1.0),  # flipped
            (20, -1, qt_transform(3, 0, 0, 3, 1000, 1000), 2, 1.0),  # a group
            (13, 20, qt_transform(1, 0, 0, 1, 10, -5), 1, 1.0),  # in it
            (30, 10, qt_transform(0.5, 0, 0, 0.5, -20, -30), 1, 1.0),  # a note attached to image 10
            (40, -1, qt_transform(), 3, 1.0),  # a drawing
        ],
        item_images=[
            (10, 1, centered(40, 20), outline(40, 20)),
            (11, 2, centered(30, 30, 2.0), outline(30, 30, left=15, top=5, right=30, bottom=25, scale=2.0)),
            (12, 1, centered(40, 20), outline(40, 20)),
            (13, 3, centered(10, 10), outline(10, 10)),
        ],
        notes=[(30, '<span style="font-size:12px;">Grip &amp; wrist</span>', "#e2111111")],
        groups=[20],
        drawings=[40],
    )


def expected_corner(board, image_index, corner=(0.0, 0.0)):
    """Where a board image's pixel lands on the page (the importer scales the whole board by one factor)."""
    image = board.images[image_index]
    factor = pur_import._board_scale(board)
    x, y = pur_import.apply(image.matrix, *corner)
    return QPointF(x * factor, y * factor)


def test_a_board_becomes_a_page_with_everything_in_its_place(qapp, board_file):
    from ui.artbook_board import BoardView, ImageItem, NoteItem

    board = pur_import.read_board(board_file)
    assert len(board.images) == 4 and len(board.notes) == 1
    assert board.skipped["drawing"] == 1 and board.groups == {20: "#4f3f3f"}  # (opaque: saved without its alpha)
    assert [i.ext for i in board.images] == [".png", ".jpg", ".png", ".png"]  # (no format saved: found from the data)

    page, skipped = pur_import.import_board(board_file)
    assert page["name"] == "Hands" and skipped["drawing"] == 1
    assert store.load_page(page["id"])["items"] == page["items"]  # written to disk
    view = BoardView()
    view.load_page(page)
    images = sorted((i for i in view.items_list() if isinstance(i, ImageItem)), key=lambda i: i.zValue())
    assert len(images) == 4

    by_size = {}
    for item in images:
        by_size.setdefault(item.natural, []).append(item)
    plain, flipped = sorted(by_size[(40, 20)], key=lambda i: i.state().flip_h)
    rotated, grouped = by_size[(30, 30)][0], by_size[(10, 10)][0]
    for item, index in ((plain, 0), (rotated, 1), (flipped, 2), (grouped, 3)):
        w, h = item.natural
        for corner in ((0, 0), (w, 0), (0, h), (w, h)):
            on_page = item.mapToScene(QPointF(corner[0] - w / 2, corner[1] - h / 2))
            expected = expected_corner(board, index, corner)
            assert on_page.x() == pytest.approx(expected.x(), abs=1e-6) and on_page.y() == pytest.approx(expected.y(), abs=1e-6)
    assert rotated.crop == (15.0, 5.0, 30.0, 25.0) and rotated.state().rotation == pytest.approx(90)
    assert rotated.opacity() == pytest.approx(0.5)
    assert flipped.state().flip_h and plain.crop is None

    (note,) = [i for i in view.items_list() if isinstance(i, NoteItem)]
    assert note.toPlainText() == "Grip & wrist" and note.fill == "#e2111111"
    assert note.zValue() > max(i.zValue() for i in images)  # labels above the images
    inset = pur_import.THEIR_NOTE_MARGIN - pur_import.OUR_NOTE_MARGIN
    factor = pur_import._board_scale(board)
    x, y = pur_import.apply(board.notes[0].matrix, inset, inset)
    assert note.pos().x() == pytest.approx(x * factor) and note.pos().y() == pytest.approx(y * factor)
    assert note.textWidth() < 200  # as wide as its words
    assert grouped.zValue() > min(i.zValue() for i in images)  # (inside its group's place in the stack)
    assert grouped.group is not None and plain.group is None  # the group came over, with its colour
    assert page["groups"] == {grouped.group: {"fill": "#4f3f3f"}}


def test_board_scaled_to_real_size_and_kept_viewable(tmp_path):
    huge = make_board(
        tmp_path / "Huge.pur",
        images=[(1, "png", png(20, 20), 20, 20)],
        items=[(1, -1, qt_transform(300, 0, 0, 300, 0, 0), 1, 1.0), (2, -1, qt_transform(300, 0, 0, 300, 500000, 0), 2, 1.0)],
        item_images=[(1, 1, centered(20, 20), outline(20, 20)), (2, 1, centered(20, 20), outline(20, 20))],
    )
    page, _ = pur_import.import_board(huge)
    first, second = page["items"]
    assert first["scale"] == pytest.approx(1.0)  # real size...
    spread = abs(second["x"] - first["x"])
    assert spread == pytest.approx(500000 / 300)  # ...and the layout the same
    assert len(list(store.images_dir(page["id"]).iterdir())) == 1  # (one picture, stored once)


def test_old_and_foreign_files_are_refused_and_leave_nothing(tmp_path):
    old = make_board(tmp_path / "Old.pur", version="1.10")
    with pytest.raises(pur_import.PurError, match="Open it in PureRef 2"):
        pur_import.import_board(old)
    junk = tmp_path / "junk.pur"
    junk.write_bytes(b"not a board at all" * 10)
    with pytest.raises(pur_import.PurError, match="isn't a PureRef board"):
        pur_import.import_board(junk)
    empty = make_board(tmp_path / "Empty.pur")
    with pytest.raises(pur_import.PurError, match="nothing on it"):
        pur_import.import_board(empty)
    assert store.list_pages() == []


def test_qt_values_saved_as_text_are_read_back():
    m = pur_import._transform(qt_transform(0.5, 0.25, -0.25, 0.5, 1234.5, -99.0).encode("utf-8"))
    assert m == (0.5, 0.25, -0.25, 0.5, 1234.5, -99.0)
    assert pur_import._path_points(qt_path([(1, 2), (3, 4)]).encode("utf-8")) == [(1, 2), (3, 4)]
    assert pur_import._transform(b"garbage") == pur_import.IDENTITY
    assert pur_import._color(b"#ffaabbcc") == "#aabbcc" and pur_import._color(b"") is None


def test_importing_from_the_pages_sidebar_and_by_dropping(qapp, tmp_path, board_file, monkeypatch):
    from PySide6.QtCore import QMimeData, QUrl
    from PySide6.QtWidgets import QFileDialog

    from ui.animations import set_motion_mode
    from ui.artbook import ArtbookPanel

    set_motion_mode("off")
    panel = ArtbookPanel()
    panel.resize(1000, 700)
    panel.show()
    qapp.processEvents()
    folder_id = store.create_folder("Imported")
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([str(board_file)], "")))
    panel.pages.import_boards(folder_id)

    def wait():
        deadline = time.monotonic() + 15
        while panel.importing() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        qapp.processEvents()

    wait()
    page_id = panel.current_page_id()
    assert panel.page["name"] == "Hands"  # opened
    index = store.page_tree()
    assert page_id in index["folders"][0]["pages"]  # in the folder it was imported into
    assert "Imported 1 board." in panel.board.toast.text() and "drawing" in panel.board.toast.text()

    # Dropping board files on the board imports them too (a broken one is reported, the rest go on)
    broken = tmp_path / "Broken.pur"
    broken.write_bytes(b"\x00" * 64)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(broken)), QUrl.fromLocalFile(str(board_file))])
    panel.board.boards_dropped.emit([u.toLocalFile() for u in mime.urls()])
    wait()
    assert sorted(p["name"] for p in store.list_pages()) == ["Hands", "Hands", "Page 1"]  # (the broken one left nothing)
    assert "isn't a PureRef board" in panel.board.toast.text()
    panel.close()
    panel.deleteLater()
    set_motion_mode("on")


def test_imported_images_get_their_names_and_files_back(tmp_path):
    hashed = "8681c73ecbe4ef557f01587603dcd402"
    assert pur_import._image_name("male-planes-torso", "https://example.com/x.png") == "male-planes-torso"
    assert pur_import._image_name(hashed, "https://example.com/refs/skull%20side.JPG?size=2") == "skull side.JPG"
    assert pur_import._image_name(hashed, "https://encrypted-tbn0.gstatic.com/images?q=tbn") == ""  # (nothing to go on)
    there = tmp_path / "ref.png"
    there.write_bytes(png(4, 4))
    assert pur_import._local_file(str(there)) == str(there)  # still on this computer: linked again
    assert pur_import._local_file(str(tmp_path / "gone.png")) is None and pur_import._local_file("https://x.com/a.png") is None
