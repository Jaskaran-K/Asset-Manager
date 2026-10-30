"""Sharing Artbook pages as .artbook files, and opening them.

An .artbook file is a zip archive holding one page:

    manifest.json        {"format": "artbook-page", "version": 1}
    page.json            the page (as in the page's folder), every image item pointing at a file in images/
    images/<sha256>.<ext>  the pictures
    thumb.png            the page's small picture (optional)

Images linked to files on this computer go in as their full original files (whoever opens it won't
have them), others as the copy kept in the page. Opening one makes a new page; nothing in the archive
is trusted: only the names above are read, sizes are checked before unpacking, and each picture is
stored under its own content's hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Any, Optional

from core import artbook_store as store

SUFFIX = ".artbook"
FORMAT = "artbook-page"
VERSION = 1
MAX_TOTAL_BYTES = 8 * 1024 ** 3  # unpacked: more than any real page
MAX_JSON_BYTES = 64 * 1024 ** 2
MAX_RATIO = 200  # unpacked / packed, for anything sizeable (a zip bomb packs far tighter)
_IMAGE_NAME = re.compile(r"[0-9a-f]{64}\.[a-z0-9]{1,6}")


class ShareError(store.PageError):
    pass


# ----------------------------------------------------------------------
# Exporting
# ----------------------------------------------------------------------
def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_page(page_id: str, target: Path) -> int:
    """Write a page to target (an .artbook file); returns how many of its images were missing
    (they stay on the page, as missing). Safe off the UI thread."""
    page = store.load_page(page_id)
    pictures: dict[str, Path] = {}  # name in the archive -> file to pack
    missing = 0
    items = []
    for item in page.get("items", []):
        if not isinstance(item, dict):
            continue
        item = dict(item)
        if item.get("kind") == "image":
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            path, blob = source.get("path"), source.get("blob")
            name = None
            if path and Path(path).is_file():  # the full original: they won't have the file
                ext = Path(path).suffix.lower()
                if ext in store.IMAGE_EXTENSIONS:
                    name = _file_hash(Path(path)) + ext
                    pictures[name] = Path(path)
            if name is None and blob and store.blob_path(page_id, blob).is_file() and _IMAGE_NAME.fullmatch(blob):
                name = blob
                pictures[name] = store.blob_path(page_id, blob)
            if name is None:
                missing += 1
            item["source"] = {"path": None, "blob": name}
        items.append(item)
    shared = {k: v for k, v in page.items() if k not in ("id", "items")}
    shared["items"] = items

    target = Path(target)
    temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps({"format": FORMAT, "version": VERSION}))
            archive.writestr("page.json", json.dumps(shared, indent=1))
            for name, path in pictures.items():
                archive.write(path, f"images/{name}", compress_type=zipfile.ZIP_STORED)  # (already compressed)
            thumb = store.thumbnail_path(page_id)
            if thumb.is_file():
                archive.write(thumb, "thumb.png", compress_type=zipfile.ZIP_STORED)
        os.replace(temp, target)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise ShareError(f"Couldn't save {target.name}: {exc}") from exc
    return missing


# ----------------------------------------------------------------------
# Opening
# ----------------------------------------------------------------------
def _check_archive(archive: zipfile.ZipFile, name: str) -> dict[str, zipfile.ZipInfo]:
    """The entries to read, by name, once their names and sizes are known to be sane."""
    entries: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        entry = info.filename
        if entry in ("manifest.json", "page.json", "thumb.png") or (
            entry.startswith("images/") and _IMAGE_NAME.fullmatch(entry[len("images/"):])
        ):
            if info.file_size > MAX_RATIO * max(info.compress_size, 1) and info.file_size > 1024 * 1024:
                raise ShareError(f"{name} looks damaged (an entry unpacks far too big).")
            total += info.file_size
            entries[entry] = info
        # anything else in the archive is ignored
    if total > MAX_TOTAL_BYTES:
        raise ShareError(f"{name} is too big to open.")
    for required in ("manifest.json", "page.json"):
        if required not in entries:
            raise ShareError(f"{name} isn't an Artbook page.")
    for small in ("manifest.json", "page.json"):
        if entries[small].file_size > MAX_JSON_BYTES:
            raise ShareError(f"{name} looks damaged.")
    return entries


def _read_json(archive: zipfile.ZipFile, entry: str, name: str) -> Any:
    try:
        return json.loads(archive.read(entry).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ShareError(f"{name} is damaged: {exc}") from exc


def _groups(value: Any) -> dict[str, dict[str, Any]]:
    """The page's groups' settings, as far as they're sane: {group id: {"fill": "#rrggbb" / "#aarrggbb",
    "locked": True}} (either may be left out)."""
    if not isinstance(value, dict):
        return {}
    groups = {}
    for gid, meta in list(value.items())[:10000]:
        if not (isinstance(gid, str) and 0 < len(gid) <= 64 and isinstance(meta, dict)):
            continue
        clean: dict[str, Any] = {}
        fill = meta.get("fill")
        if isinstance(fill, str) and re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", fill):
            clean["fill"] = fill
        if meta.get("locked") is True:
            clean["locked"] = True
        if clean:
            groups[gid] = clean
    return groups


def import_page(source: Path) -> dict[str, Any]:
    """Make a new page from an .artbook file (not yet filed in the index: page_tree() does that).
    Written whole (pictures first, page.json last); a failure leaves nothing. Safe off the UI thread."""
    source = Path(source)
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ShareError(f"{source.name} isn't an Artbook page.") from exc
    page_id = uuid.uuid4().hex
    try:
        with archive:
            entries = _check_archive(archive, source.name)
            manifest = _read_json(archive, "manifest.json", source.name)
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                raise ShareError(f"{source.name} isn't an Artbook page.")
            if not isinstance(manifest.get("version"), int) or manifest["version"] > VERSION:
                raise ShareError(f"{source.name} was made by a newer version of the app. Update to open it.")
            shared = _read_json(archive, "page.json", source.name)
            if not isinstance(shared, dict) or not isinstance(shared.get("items"), list):
                raise ShareError(f"{source.name} is damaged.")

            stored: dict[str, Optional[str]] = {}  # name in the archive -> name in the new page

            def picture(name: Any) -> Optional[str]:
                if not isinstance(name, str) or f"images/{name}" not in entries:
                    return None
                if name not in stored:
                    data = archive.read(entries[f"images/{name}"])
                    stored[name] = store.store_blob(page_id, data, "." + name.rsplit(".", 1)[-1])
                return stored[name]

            items = []
            for item in shared["items"]:
                if not isinstance(item, dict) or item.get("kind", "image") not in ("image", "note", "stroke"):
                    continue
                item = dict(item)
                item["id"] = uuid.uuid4().hex
                if item.get("kind", "image") == "image":
                    source_info = item.get("source") if isinstance(item.get("source"), dict) else {}
                    item["source"] = {"path": None, "blob": picture(source_info.get("blob"))}
                items.append(item)
            now = time.time()
            name = shared.get("name")
            page = {
                "version": store.FORMAT_VERSION, "id": page_id,
                "name": store.clean_name(name if isinstance(name, str) and name.strip() else source.stem),
                "created": now, "modified": now,
                "view": shared.get("view") if isinstance(shared.get("view"), dict) else None,
                "items": items,
                "groups": _groups(shared.get("groups")),
            }
            store.save_page(page)
            if "thumb.png" in entries and entries["thumb.png"].file_size < MAX_JSON_BYTES:
                store.save_thumbnail(page_id, archive.read(entries["thumb.png"]))
            return page
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        shutil.rmtree(store.page_dir(page_id), ignore_errors=True)
        raise ShareError(f"{source.name} is damaged: {exc}") from exc
    except BaseException:
        shutil.rmtree(store.page_dir(page_id), ignore_errors=True)
        raise
