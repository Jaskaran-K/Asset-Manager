"""Artbook pages on disk.

Each page is a folder in PAGES_DIR:

    <page id>/page.json      name, view, and every item (position, scale, rotation, flips...)
    <page id>/images/<sha256>.<ext>   image data, named by content so each is stored once
    <page id>/thumb.png      small picture of the board, for the Pages list

Saving rewrites page.json only (written to a temporary file, then swapped in, so a crash
mid-save never leaves a broken page); images are written once, when they're added.

Images from files are *linked*: the item keeps the file's path, plus a downscaled fallback
copy in images/ in case the file moves or disappears. Pasted / dropped image data has no
file, so it's stored in full.

PAGES_DIR/index.json arranges the pages: folders (which can hold folders), the order of
everything, and which folder each page is in. Pages stay self-contained folders, and the
index repairs itself when read: a page folder it doesn't list shows up at the top level,
and an entry whose page is gone is dropped. Page names live in page.json only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

import numpy as np
from PIL import Image
from PySide6.QtCore import QFile
from PySide6.QtGui import QImage

from core.thumbnail_manager import _safe_convert_pil_to_rgba, open_image

PAGES_DIR: Path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AssetManager" / "artbook"
FORMAT_VERSION = 1
INDEX_VERSION = 1
MAX_DECODE_SIDE = 4096  # images are shown at most this big (more detail when zoomed in comes later)
FALLBACK_SIDE = 1024  # size of the copy kept for linked files
MAX_NAME = 120

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".tga",
    ".psd", ".psb", ".exr", ".hdr", ".dds", ".ico", ".jfif",
}


class PageError(Exception):
    pass


# ----------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------
def pages_dir() -> Path:
    return PAGES_DIR  # read at call time, so tests can point it elsewhere


def page_dir(page_id: str) -> Path:
    return pages_dir() / page_id


def images_dir(page_id: str) -> Path:
    return page_dir(page_id) / "images"


def new_page(name: str) -> dict[str, Any]:
    """A new, empty page (saved straight away). create_page also files it in the index."""
    now = time.time()
    page = {
        "version": FORMAT_VERSION, "id": uuid.uuid4().hex, "name": name,
        "created": now, "modified": now, "view": None, "items": [],
    }
    save_page(page)
    return page


# Saves come from the UI thread (switching page, renaming, quitting) and from autosave jobs:
# they're written one at a time, and a snapshot taken earlier never replaces a newer one.
_save_lock = threading.Lock()
_saved_at: dict[str, float] = {}  # page id -> "modified" of the version on disk
_trashed: set[str] = set()  # deleted: a late save or image mustn't bring the folder back


def save_page(page: dict[str, Any]) -> None:
    with _save_lock:
        page_id = page["id"]
        if page_id in _trashed:
            return
        modified = float(page.get("modified") or 0)
        if modified < _saved_at.get(page_id, 0.0):
            return  # an older snapshot finishing late
        folder = page_dir(page_id)
        folder.mkdir(parents=True, exist_ok=True)
        _write_json(folder / "page.json", page)
        _saved_at[page_id] = modified


def _write_json(target: Path, data: Any) -> None:
    temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")  # unique: two saves never share a temp file
    temp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(temp, target)


def load_page(page_id: str) -> dict[str, Any]:
    path = page_dir(page_id) / "page.json"
    try:
        page = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PageError(f"Couldn't open the page: {exc}") from exc
    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        raise PageError("The page file is damaged.")
    page.setdefault("view", None)
    return page


_summaries: dict[str, tuple[int, dict[str, Any]]] = {}  # page id -> (page.json mtime, summary)


def page_summary(page_id: str) -> Optional[dict[str, Any]]:
    """A page's id, name and timestamps (items left out), or None if it can't be read."""
    path = page_dir(page_id) / "page.json"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    cached = _summaries.get(page_id)
    if cached and cached[0] == mtime:
        return dict(cached[1])
    try:
        page = load_page(page_id)
    except PageError:
        return None
    summary = {k: page.get(k) for k in ("name", "created", "modified")}
    summary["id"] = page_id  # the folder's name is the id the index uses
    _summaries[page_id] = (mtime, summary)
    return dict(summary)


def list_pages() -> list[dict[str, Any]]:
    """Every page's summary (see page_summary), oldest first."""
    pages = []
    folder = pages_dir()
    if folder.is_dir():
        for child in folder.iterdir():
            if child.name not in _trashed and (child / "page.json").is_file():
                summary = page_summary(child.name)
                if summary is not None:
                    pages.append(summary)
    return sorted(pages, key=lambda p: p.get("created") or 0)


def delete_page(page_id: str) -> None:
    """Delete a page for good (trash_page sends it to the Recycle Bin instead)."""
    with _save_lock:
        _trashed.add(page_id)
        shutil.rmtree(page_dir(page_id), ignore_errors=True)
    _forget(page_id)


def trash_page(page_id: str) -> None:
    """Send a page to the Recycle Bin and take it out of the index."""
    folder = page_dir(page_id)
    with _save_lock:
        _trashed.add(page_id)  # first: an autosave in flight must not recreate it
        if folder.exists() and not QFile.moveToTrash(str(folder)):
            _trashed.discard(page_id)
            raise PageError("Couldn't move the page to the Recycle Bin.")
    _forget(page_id)


def _forget(page_id: str) -> None:
    _summaries.pop(page_id, None)
    index = _load_index()
    if _remove_page(index, page_id):
        _save_index(index)


def unique_page_name(base: str = "Page") -> str:
    """'Page N' with the lowest N that no page uses yet."""
    taken = {(p.get("name") or "").casefold() for p in list_pages()}
    number = 1
    while f"{base} {number}".casefold() in taken:
        number += 1
    return f"{base} {number}"


def clean_name(name: str) -> str:
    name = " ".join(str(name).split())
    if not name:
        raise PageError("The name can't be empty.")
    return name[:MAX_NAME]


def create_page(name: str, folder_id: Optional[str] = None) -> dict[str, Any]:
    """A new, empty page at the end of a folder (None: the top level)."""
    index = page_tree()
    _find_folder(index, folder_id)  # before writing anything
    page = new_page(clean_name(name))
    _insert_page(index, page["id"], folder_id)
    _save_index(index)
    return page


def rename_page(page_id: str, name: str) -> dict[str, Any]:
    """Rename a page on disk. The open page is renamed by its panel, which holds the newest copy."""
    page = load_page(page_id)
    page["name"] = clean_name(name)
    page["modified"] = max(time.time(), float(page.get("modified") or 0))
    save_page(page)
    return page


def duplicate_page(page_id: str) -> dict[str, Any]:
    """A copy of a page (with its images), placed right after it."""
    source = load_page(page_id)
    copy = dict(source)
    now = time.time()
    name = f"{source.get('name') or 'Untitled'} copy"
    copy.update(id=uuid.uuid4().hex, name=name[:MAX_NAME], created=now, modified=now)
    target = page_dir(copy["id"])
    try:
        shutil.copytree(page_dir(page_id), target, ignore=shutil.ignore_patterns("*.tmp"))
    except OSError as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise PageError(f"Couldn't copy the page: {exc}") from exc
    save_page(copy)
    index = page_tree()
    folder, position = _page_place(index, page_id)
    _insert_page(index, copy["id"], folder["id"], position + 1)
    _save_index(index)
    return copy


# ----------------------------------------------------------------------
# The index: folders, and the order of everything
# ----------------------------------------------------------------------
# A folder is {"id", "name", "open", "folders": [folder...], "pages": [page id...]}. The index
# itself is the top level, a folder with id None. A folder lists its folders before its pages.
def index_path() -> Path:
    return pages_dir() / "index.json"


def _empty_index() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "id": None, "folders": [], "pages": []}


def _load_index() -> dict[str, Any]:
    try:
        index = json.loads(index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_index()
    return index if isinstance(index, dict) else _empty_index()


def _save_index(index: dict[str, Any]) -> None:
    index_path().parent.mkdir(parents=True, exist_ok=True)
    _write_json(index_path(), index)


def page_tree() -> dict[str, Any]:
    """The index, repaired against the page folders on disk (and saved if that changed it)."""
    raw = _load_index()
    on_disk = [p["id"] for p in list_pages()]  # oldest first
    known = set(on_disk)
    seen_pages: set[str] = set()
    seen_folders: set[str] = set()

    def clean(folder: dict[str, Any], top: bool, depth: int) -> dict[str, Any]:
        if top:
            result: dict[str, Any] = _empty_index()
        else:
            name = folder.get("name")
            result = {
                "id": folder["id"],
                "name": clean_name(name) if isinstance(name, str) and name.strip() else "Folder",
                "open": folder.get("open") is not False,
                "folders": [],
                "pages": [],
            }
        subfolders = folder.get("folders")
        for sub in subfolders if isinstance(subfolders, list) and depth < 64 else []:
            sub_id = sub.get("id") if isinstance(sub, dict) else None
            if isinstance(sub_id, str) and sub_id and sub_id not in seen_folders:
                seen_folders.add(sub_id)
                result["folders"].append(clean(sub, False, depth + 1))
        pages = folder.get("pages")
        for page_id in pages if isinstance(pages, list) else []:
            if isinstance(page_id, str) and page_id in known and page_id not in seen_pages:
                seen_pages.add(page_id)
                result["pages"].append(page_id)
        return result

    index = clean(raw, True, 0)
    index["pages"] += [page_id for page_id in on_disk if page_id not in seen_pages]
    if index != raw and (index["folders"] or index["pages"] or index_path().exists()):
        _save_index(index)
    return index


def walk_folders(folder: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every folder inside folder, depth first, in order."""
    for sub in folder["folders"]:
        yield sub
        yield from walk_folders(sub)


def _all_folders(index: dict[str, Any]) -> list[dict[str, Any]]:
    return [index, *walk_folders(index)]


def _find_folder(index: dict[str, Any], folder_id: Optional[str]) -> dict[str, Any]:
    for folder in _all_folders(index):
        if folder["id"] == folder_id:
            return folder
    raise PageError("That folder doesn't exist any more.")


def _folder_parent(index: dict[str, Any], folder_id: str) -> dict[str, Any]:
    for folder in _all_folders(index):
        if any(sub["id"] == folder_id for sub in folder["folders"]):
            return folder
    raise PageError("That folder doesn't exist any more.")


def _page_place(index: dict[str, Any], page_id: str) -> tuple[dict[str, Any], int]:
    for folder in _all_folders(index):
        if page_id in folder["pages"]:
            return folder, folder["pages"].index(page_id)
    raise PageError("That page doesn't exist any more.")


def _remove_page(index: dict[str, Any], page_id: str) -> bool:
    for folder in _all_folders(index):
        if page_id in folder["pages"]:
            folder["pages"].remove(page_id)
            return True
    return False


def _insert_page(index: dict[str, Any], page_id: str, folder_id: Optional[str], position: Optional[int] = None) -> None:
    folder = _find_folder(index, folder_id)
    _remove_page(index, page_id)
    pages = folder["pages"]
    pages.insert(len(pages) if position is None else max(0, min(position, len(pages))), page_id)


def page_folder(page_id: str) -> Optional[str]:
    """Id of the folder a page is in (None: the top level, or not listed)."""
    try:
        return _page_place(page_tree(), page_id)[0]["id"]
    except PageError:
        return None


def move_page(page_id: str, folder_id: Optional[str], position: Optional[int] = None) -> None:
    """Put a page in a folder (None: the top level), at position among its pages (None: last).

    position counts the pages there now, the moved one included (as a drop indicator shows it).
    """
    index = page_tree()
    old_folder, old_position = _page_place(index, page_id)
    target = _find_folder(index, folder_id)
    if position is not None and target is old_folder and position > old_position:
        position -= 1  # it's taken out before it goes back in
    _insert_page(index, page_id, folder_id, position)
    _save_index(index)


def create_folder(name: str, parent_id: Optional[str] = None) -> str:
    index = page_tree()
    parent = _find_folder(index, parent_id)
    folder_id = uuid.uuid4().hex
    parent["folders"].append({"id": folder_id, "name": clean_name(name), "open": True, "folders": [], "pages": []})
    _save_index(index)
    return folder_id


def rename_folder(folder_id: str, name: str) -> None:
    index = page_tree()
    _find_folder(index, folder_id)["name"] = clean_name(name)
    _save_index(index)


def set_folder_open(folder_id: str, open_: bool) -> None:
    """Remember whether a folder is expanded in the Pages list."""
    index = page_tree()
    try:
        folder = _find_folder(index, folder_id)
    except PageError:
        return
    if folder["id"] is not None and folder["open"] != open_:
        folder["open"] = open_
        _save_index(index)


def delete_folder(folder_id: str) -> None:
    """Remove a folder. What was in it moves up into its parent, where the folder was."""
    index = page_tree()
    parent = _folder_parent(index, folder_id)
    position = next(i for i, sub in enumerate(parent["folders"]) if sub["id"] == folder_id)
    folder = parent["folders"].pop(position)
    parent["folders"][position:position] = folder["folders"]
    parent["pages"] += folder["pages"]
    _save_index(index)


def move_folder(folder_id: str, parent_id: Optional[str], position: Optional[int] = None) -> None:
    """Put a folder inside another (None: the top level), at position among its folders."""
    index = page_tree()
    folder = _find_folder(index, folder_id)
    target = _find_folder(index, parent_id)
    if target is folder or any(sub is target for sub in walk_folders(folder)):
        raise PageError("A folder can't go inside itself.")
    old_parent = _folder_parent(index, folder_id)
    old_position = old_parent["folders"].index(folder)
    if position is not None and target is old_parent and position > old_position:
        position -= 1
    old_parent["folders"].remove(folder)
    folders = target["folders"]
    folders.insert(len(folders) if position is None else max(0, min(position, len(folders))), folder)
    _save_index(index)


def ordered_page_ids(index: Optional[dict[str, Any]] = None) -> list[str]:
    """Every page, in the order the Pages list shows them."""
    index = index if index is not None else page_tree()
    ids: list[str] = []

    def collect(folder: dict[str, Any]) -> None:
        for sub in folder["folders"]:
            collect(sub)
        ids.extend(folder["pages"])

    collect(index)
    return ids


# ----------------------------------------------------------------------
# Thumbnails
# ----------------------------------------------------------------------
def thumbnail_path(page_id: str) -> Path:
    return page_dir(page_id) / "thumb.png"


def save_thumbnail(page_id: str, data: Optional[bytes]) -> None:
    """Store a page's thumbnail (None: it has none, e.g. an empty board)."""
    with _save_lock:
        if page_id in _trashed or not page_dir(page_id).is_dir():
            return
        target = thumbnail_path(page_id)
        if data is None:
            target.unlink(missing_ok=True)
            return
        temp = target.with_name(f"thumb.{uuid.uuid4().hex}.tmp")
        temp.write_bytes(data)
        os.replace(temp, target)


# ----------------------------------------------------------------------
# Images
# ----------------------------------------------------------------------
def store_blob(page_id: str, data: bytes, ext: str) -> str:
    """Save image data in the page (once per content); returns its file name."""
    name = f"{hashlib.sha256(data).hexdigest()}{ext}"
    if page_id in _trashed:
        raise PageError("The page was deleted.")
    folder = images_dir(page_id)
    target = folder / name
    if not target.is_file():
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / f"{name}.{uuid.uuid4().hex}.tmp"
        temp.write_bytes(data)
        os.replace(temp, target)
    return name


def blob_path(page_id: str, name: str) -> Path:
    return images_dir(page_id) / name


def _to_qimage(im: Image.Image, max_side: int) -> QImage:
    rgba = _safe_convert_pil_to_rgba(im)
    if max(rgba.size) > max_side:
        rgba.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    rgba.load()
    w, h = rgba.size
    return QImage(rgba.tobytes(), w, h, 4 * w, QImage.Format.Format_RGBA8888).copy()


def image_size(path: Path) -> Optional[tuple[int, int]]:
    """Full size of an image file, read from its header only (fast)."""
    try:
        with open_image(path, max_size=64) as im:
            return im.size
    except Exception:  # pylint: disable=broad-except
        return None


def decode_file(path: Path, max_side: int = MAX_DECODE_SIDE) -> tuple[QImage, tuple[int, int]]:
    """(image to show, full size) for any image file the library can preview. Safe off the UI thread."""
    try:
        with open_image(path, max_size=max_side) as im:
            size = im.size
            return _to_qimage(im, max_side), size
    except Exception as exc:  # pylint: disable=broad-except
        raise PageError(f"Couldn't read {path.name}: {exc}") from exc


def decode_bytes(data: bytes, max_side: int = MAX_DECODE_SIDE) -> tuple[QImage, tuple[int, int]]:
    try:
        with Image.open(io.BytesIO(data)) as im:
            size = im.size
            return _to_qimage(im, max_side), size
    except Exception as exc:  # pylint: disable=broad-except
        raise PageError(f"Couldn't read the image: {exc}") from exc


def encode_fallback(image: QImage, side: int = FALLBACK_SIDE) -> tuple[bytes, str]:
    """A small copy of image to keep in the page: (data, extension). JPEG unless it has transparency."""
    from PySide6.QtCore import QBuffer, QIODevice, Qt

    if max(image.width(), image.height()) > side:
        image = image.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    transparent = image.hasAlphaChannel() and _has_transparency(image)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG" if transparent else "JPG", -1 if transparent else 88)
    return bytes(buffer.data().data()), ".png" if transparent else ".jpg"


def _has_transparency(image: QImage) -> bool:
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    rows = np.frombuffer(alpha.constBits(), np.uint8, alpha.sizeInBytes()).reshape(alpha.height(), alpha.bytesPerLine())
    return bool((rows[:, : alpha.width()] != 255).any())


def encode_png(image: QImage) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data().data())
