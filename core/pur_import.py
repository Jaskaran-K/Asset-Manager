"""Importing PureRef boards (.pur files, saved by PureRef 2): each becomes an Artbook page.

This only reads the user's own board files, to bring their layouts over; nothing is ever written
in this format. What a version-2 file holds, as found in the files themselves:

- A short header: the format version as a Qt string ("2.1"), and at byte 14 a big-endian
  64-bit number, the length of an embedded SQLite database. Then a JPEG preview of the board.
- The database: the header and preview were written over its first bytes, which were moved to
  the end of the file. Putting them back (the tail first, then the rest) gives a normal database.
- Its tables: `images` (each picture's original file data, format and size), `items` (every
  item's parent, transform, stacking z and opacity), `items_images` (which picture an item shows,
  how its pixels sit in the item, and the visible part, i.e. the crop), `items_notes` (rich text and
  colours), `items_groups` and `items_drawings`.
- Transforms, crop outlines and sizes are Qt values (QDataStream, big-endian) stored as text:
  each byte above 127 was saved as two UTF-8 bytes, so they're turned back into bytes first.

Every item is placed relative to its parent (a group, or an image for a note attached to it), so
the importer follows each chain to the board. Groups and drawings aren't on the Artbook yet:
grouped items keep their places, ungrouped; drawings are left out (and counted). The whole board
is scaled so its images are near their real size (the layout is unchanged), since boards can be
far bigger than the Artbook zooms out to.
"""

from __future__ import annotations

import html as html_lib
import io
import math
import re
import shutil
import sqlite3
import struct
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Optional

from PIL import Image

from core import artbook_store as store

SUFFIX = ".pur"
QT_TRANSFORM = 80  # Qt's type ids, as saved before each value
QT_SIZEF = 22
QT_USER_TYPE = 1024  # followed by the type's name
TARGET_SCALE = 1.0  # images end up near their real size...
MAX_BOARD_SIDE = 40000.0  # ...unless the board would then be too big to see all at once
THEIR_NOTE_MARGIN = 4.0  # around a note's text (Qt's default)
OUR_NOTE_MARGIN = 12.0  # (ui.artbook_board.NOTE_PADDING): imported text lands where it was

Affine = tuple[float, float, float, float, float, float]  # Qt's m11, m12, m21, m22, dx, dy
IDENTITY: Affine = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class PurError(store.PageError):
    pass


@dataclass
class ImportedImage:
    data: bytes
    ext: str
    size: tuple[int, int]
    matrix: Affine  # the image's pixels (top-left origin) to the board
    crop: Optional[tuple[float, float, float, float]]  # in its pixels
    order: tuple[float, ...]
    opacity: float


@dataclass
class ImportedNote:
    html: str
    fill: Optional[str]
    matrix: Affine  # the note's top-left corner to the board
    order: tuple[float, ...]
    opacity: float


@dataclass
class Board:
    images: list[ImportedImage] = field(default_factory=list)
    notes: list[ImportedNote] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)  # drawings, groups (ungrouped), missing images


# ----------------------------------------------------------------------
# Reading the file
# ----------------------------------------------------------------------
def file_version(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError as exc:
        raise PurError(f"Couldn't read {path.name}: {exc}") from exc
    if len(head) < 22:
        raise PurError(f"{path.name} isn't a PureRef board.")
    length = struct.unpack_from(">I", head, 0)[0]
    try:
        version = head[4:4 + length].decode("utf-16-be") if 0 < length <= 40 else ""
    except UnicodeDecodeError:
        version = ""
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        raise PurError(f"{path.name} isn't a PureRef board.")
    return version


def open_database(path: Path) -> sqlite3.Connection:
    """The board's database, put back together in memory (the file isn't changed)."""
    version = file_version(path)
    if int(version.split(".")[0]) < 2:
        raise PurError(
            f"{path.name} was saved by PureRef {version}, which uses an older format. "
            "Open it in PureRef 2 and save it, then import it again."
        )
    raw = path.read_bytes()
    length = struct.unpack_from(">Q", raw, 14)[0]
    if not 0 < length <= len(raw) or raw[length:length + 16] != b"SQLite format 3\x00":
        raise PurError(f"{path.name} is damaged, or from a PureRef version this can't read yet.")
    moved = raw[length:]
    database = moved + raw[len(moved):length]
    del raw
    con = sqlite3.connect(":memory:")
    try:
        con.deserialize(database)
        con.text_factory = bytes
        con.execute("select id from items limit 1")
    except sqlite3.Error as exc:
        con.close()
        raise PurError(f"{path.name} is damaged: {exc}") from exc
    return con


def _qt_bytes(value: Any) -> bytes:
    """A Qt value stored as text, as the bytes it was."""
    if value is None:
        return b""
    if isinstance(value, str):
        value = value.encode("utf-8")
    try:
        return bytes(value).decode("utf-8").encode("latin-1")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return bytes(value)  # (already plain bytes)


def _transform(value: Any) -> Affine:
    data = _qt_bytes(value)
    if len(data) < 77 or struct.unpack_from(">I", data, 0)[0] != QT_TRANSFORM:
        return IDENTITY
    m11, m12, _m13, m21, m22, _m23, dx, dy, _m33 = struct.unpack_from(">9d", data, 5)
    values = (m11, m12, m21, m22, dx, dy)
    return values if all(math.isfinite(v) for v in values) else IDENTITY


def _path_points(value: Any) -> list[tuple[float, float]]:
    """The corners of a saved outline (a QPainterPath)."""
    data = _qt_bytes(value)
    try:
        if struct.unpack_from(">I", data, 0)[0] != QT_USER_TYPE:
            return []
        name_length = struct.unpack_from(">I", data, 5)[0]
        if not data[9:9 + name_length].startswith(b"QPainterPath"):
            return []
        at = 9 + name_length
        count = struct.unpack_from(">i", data, at)[0]
        points = []
        for i in range(max(0, min(count, 10000))):
            _kind, x, y = struct.unpack_from(">idd", data, at + 4 + i * 20)
            points.append((x, y))
        return [p for p in points if all(math.isfinite(v) for v in p)]
    except struct.error:
        return []


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _color(value: Any) -> Optional[str]:
    """#aarrggbb or #rrggbb (as the Artbook stores colours), or None."""
    text = _text(value).strip()
    if re.fullmatch(r"#[0-9a-fA-F]{8}", text):
        return text.lower() if text[1:3].lower() != "ff" else "#" + text[3:].lower()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return text.lower()
    return None


# ----------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------
def combine(first: Affine, then: Affine) -> Affine:
    """first, then then (Qt's row-vector order: first * then)."""
    a1, b1, c1, d1, x1, y1 = first
    a2, b2, c2, d2, x2, y2 = then
    return (
        a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
        x1 * a2 + y1 * c2 + x2, x1 * b2 + y1 * d2 + y2,
    )


def apply(m: Affine, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, dx, dy = m
    return a * x + c * y + dx, b * x + d * y + dy


def invert(m: Affine) -> Affine:
    a, b, c, d, dx, dy = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return IDENTITY
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(dx * ia + dy * ic), -(dx * ib + dy * id_))


def decompose(m: Affine) -> tuple[float, float, bool]:
    """(scale, rotation in degrees, flipped left-right) such that the scale and rotation, then the
    flip give m's shape (the order Qt draws the board's items in: their flip is their transform()).
    A stretch becomes its average scale."""
    a, b, c, d, _dx, _dy = m
    flipped = a * d - b * c < 0
    if flipped:
        a, c = -a, -c  # (undo the flip: it negates the first column)
    scale = math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1.0
    rotation = math.degrees(math.atan2(b, a))
    return scale, round(rotation, 6) % 360.0, flipped


# ----------------------------------------------------------------------
# Reading a board
# ----------------------------------------------------------------------
def _ext(fmt: str, data: bytes) -> str:
    ext = "." + fmt.strip().lower().lstrip(".")
    ext = {".jpeg": ".jpg", ".tiff": ".tif"}.get(ext, ext)
    if ext in store.IMAGE_EXTENSIONS:
        return ext
    try:
        with Image.open(io.BytesIO(data)) as im:
            detected = "." + (im.format or "").lower()
    except Exception:  # pylint: disable=broad-except
        return ".png"
    return {".jpeg": ".jpg", ".tiff": ".tif"}.get(detected, detected) if detected != "." else ".png"


def read_board(path: Path) -> Board:
    """Everything on a board, placed on it (see the module docstring)."""
    con = open_database(path)
    try:
        return _read(con)
    except (sqlite3.Error, struct.error) as exc:
        raise PurError(f"{path.name} couldn't be read: {exc}") from exc
    finally:
        con.close()


def _read(con: sqlite3.Connection) -> Board:
    board = Board()
    items: dict[int, dict[str, Any]] = {}
    for item_id, parent, transform, z, opacity in con.execute("select id, parent, transform, z, opacity from items"):
        items[item_id] = {"parent": parent, "matrix": _transform(transform), "z": float(z or 0), "opacity": opacity}

    def placed(item_id: int) -> tuple[Affine, tuple[float, ...], float]:
        """(the item's corner to the board, its stacking order, its opacity) through every parent."""
        matrix, order, opacity, seen = IDENTITY, (), 1.0, set()
        current: Optional[int] = item_id
        while current is not None and current in items and current not in seen:
            seen.add(current)
            item = items[current]
            matrix = combine(matrix, item["matrix"])
            order = (item["z"],) + order  # children above their parent, then by z among siblings
            value = item["opacity"]
            opacity *= float(value) if isinstance(value, (int, float)) and 0 < value <= 1 else 1.0
            parent = item["parent"]
            current = parent if isinstance(parent, int) and parent >= 0 else None
        return matrix, order, opacity

    pictures = {}
    for image_id, fmt, data, width, height, source in con.execute("select id, format, data, width, height, source from images"):
        pictures[image_id] = (_text(fmt), data, width, height, _text(source))

    rows = con.execute("select id, image, image_transform, image_bounds from items_images").fetchall()
    for item_id, image_id, image_transform, bounds in rows:
        picture = pictures.get(image_id)
        if item_id not in items or picture is None or not picture[1]:
            board.skipped["missing image"] += 1
            continue
        fmt, data, width, height = picture[0], bytes(picture[1]), picture[2], picture[3]
        if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
            try:
                with Image.open(io.BytesIO(data)) as im:
                    width, height = im.size
            except Exception:  # pylint: disable=broad-except
                board.skipped["missing image"] += 1
                continue
        pixels = _transform(image_transform)  # the picture's pixels in the item
        matrix, order, opacity = placed(item_id)
        crop = None
        outline = _path_points(bounds)
        if outline:
            back = invert(pixels)
            corners = [apply(back, x, y) for x, y in outline]
            left = max(0.0, min(x for x, _ in corners))
            top = max(0.0, min(y for _, y in corners))
            right = min(float(width), max(x for x, _ in corners))
            bottom = min(float(height), max(y for _, y in corners))
            if right - left >= 1 and bottom - top >= 1 and (left > 0.5 or top > 0.5 or right < width - 0.5 or bottom < height - 0.5):
                crop = (round(left, 3), round(top, 3), round(right, 3), round(bottom, 3))
        board.images.append(ImportedImage(
            data, _ext(fmt, data), (width, height), combine(pixels, matrix), crop, order, opacity,
        ))

    for item_id, text, background in con.execute("select id, text, background_color from items_notes"):
        if item_id not in items:
            continue
        matrix, order, opacity = placed(item_id)
        board.notes.append(ImportedNote(_text(text), _color(background), matrix, order, opacity))

    board.skipped["drawing"] += con.execute("select count(*) from items_drawings").fetchone()[0]
    board.skipped["group"] += con.execute("select count(*) from items_groups").fetchone()[0]
    return board


# ----------------------------------------------------------------------
# Making the page
# ----------------------------------------------------------------------
def _plain(html: str) -> str:
    """A note's words (for search), without its formatting."""
    text = re.sub(r"(?is)<head.*?</head>|<!DOCTYPE[^>]*>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _board_scale(board: Board) -> float:
    """One factor for the whole board: images near their real size, the board not too big to view."""
    scales = [decompose(image.matrix)[0] for image in board.images]
    factor = TARGET_SCALE / median(scales) if scales else 1.0
    points = []
    for image in board.images:
        w, h = image.size
        points += [apply(image.matrix, x, y) for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    for note in board.notes:
        points.append(apply(note.matrix, 0, 0))
    if len(points) > 1:
        side = max(max(p[0] for p in points) - min(p[0] for p in points), max(p[1] for p in points) - min(p[1] for p in points))
        if side * factor > MAX_BOARD_SIDE:
            factor = MAX_BOARD_SIDE / side
    return factor if math.isfinite(factor) and factor > 0 else 1.0


def page_items(board: Board, blobs: list[str]) -> list[dict[str, Any]]:
    """The board as Artbook page items (blobs: each image's stored file name, in order)."""
    factor = _board_scale(board)
    zoom: Affine = (factor, 0.0, 0.0, factor, 0.0, 0.0)
    entries: list[tuple[tuple[float, ...], dict[str, Any]]] = []
    for image, blob in zip(board.images, blobs):
        w, h = image.size
        # The board's images turn around their middle: start from there
        middle = combine(combine((1.0, 0.0, 0.0, 1.0, w / 2.0, h / 2.0), image.matrix), zoom)
        scale, rotation, flipped = decompose(middle)
        entries.append(((0.0,) + image.order, {
            "id": uuid.uuid4().hex, "kind": "image", "source": {"path": None, "blob": blob}, "size": [w, h],
            "x": middle[4], "y": middle[5], "scale": scale, "rotation": rotation, "flip_h": flipped, "flip_v": False,
            "crop": list(image.crop) if image.crop else None, "opacity": round(image.opacity, 4), "gray": False,
        }))
    for note in board.notes:
        inset = THEIR_NOTE_MARGIN - OUR_NOTE_MARGIN
        corner = combine(combine((1.0, 0.0, 0.0, 1.0, inset, inset), note.matrix), zoom)
        scale, rotation, _flipped = decompose(corner)
        entries.append(((1.0,) + note.order, {  # (notes above the images: labels never hidden)
            "id": uuid.uuid4().hex, "kind": "note", "text": _plain(note.html)[:20000], "html": note.html,
            "fill": note.fill, "width": None,  # (None: as wide as its text)
            "x": corner[4], "y": corner[5], "scale": scale, "rotation": rotation, "opacity": round(note.opacity, 4),
        }))
    entries.sort(key=lambda entry: entry[0])
    items = []
    for z, (_order, item) in enumerate(entries, start=1):
        item["z"] = float(z)
        items.append(item)
    return items


def import_board(path: Path) -> tuple[dict[str, Any], Counter]:
    """Make a page from a board file: (the page, what was left out). The page is written whole
    (images first, page.json last), so a failed import leaves nothing behind. Safe off the UI thread."""
    board = read_board(Path(path))
    if not board.images and not board.notes:
        raise PurError(f"{Path(path).name} has nothing on it to import.")
    page_id = uuid.uuid4().hex
    try:
        blobs = [store.store_blob(page_id, image.data, image.ext) for image in board.images]
        now = time.time()
        page = {
            "version": store.FORMAT_VERSION, "id": page_id, "name": store.clean_name(Path(path).stem),
            "created": now, "modified": now, "view": None, "items": page_items(board, blobs),
        }
        store.save_page(page)
    except BaseException:
        shutil.rmtree(store.page_dir(page_id), ignore_errors=True)
        raise
    return page, board.skipped
