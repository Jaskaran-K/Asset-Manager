"""Your Artbook page templates: a page saved as a starting point (right-click it in Pages > Save as Template), made
into a new page from the New Page window (ui.artbook_templates, which also has the built-in ones).

Each is <id>.artbook (core.artbook_share's format: the page, its pictures and its small picture) and <id>.json
(its name, when it was made, the page it came from), in the pages folder's templates folder (core.artbook_store:
it moves with your pages). No Qt here: saving and making pages run off the UI thread.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Optional

from . import artbook_share
from . import artbook_store as store

MAX_TEMPLATES = 200
_ID = re.compile(r"[0-9a-f]{32}")


def folder() -> Path:
    return store.pages_dir() / store.TEMPLATES_FOLDER


def _paths(template_id: str) -> tuple[Path, Path]:
    if not _ID.fullmatch(template_id or ""):
        raise store.PageError("That template isn't there.")
    return folder() / f"{template_id}{artbook_share.SUFFIX}", folder() / f"{template_id}.json"


def prepare_page(page: dict[str, Any], keep_pictures: bool = True, untick: bool = True) -> dict[str, Any]:
    """A page as a template: no comments (they were about that page), its to-dos unticked (untick), and without its
    pictures unless keep_pictures (its cards and notes stay)."""
    page = dict(page)
    page.pop("comments", None)
    items = []
    for item in page.get("items", []) if isinstance(page.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        if not keep_pictures and item.get("kind", "image") == "image":
            continue
        if untick and item.get("kind") == "note" and isinstance(item.get("html"), str):
            item = dict(item, html=item["html"].replace('<li class="checked"', '<li class="unchecked"'))
        items.append(item)
    if not keep_pictures:
        kept = {item.get("id") for item in items}
        for item in items:
            if item.get("kind") == "column" and isinstance(item.get("members"), list):
                item["members"] = [m for m in item["members"] if m in kept]
    page["items"] = items
    return page


def save_template(page_id: str, name: str, keep_pictures: bool = True, untick: bool = True) -> dict[str, Any]:
    """Save a page as a template (named name); returns its listing (list_templates)."""
    name = store.clean_name(name)
    if len(list_templates()) >= MAX_TEMPLATES:
        raise store.PageError(f"There are {MAX_TEMPLATES} templates already: delete some first (New Page, right-click one).")
    folder().mkdir(parents=True, exist_ok=True)
    template_id = uuid.uuid4().hex
    package, meta = _paths(template_id)
    source = store.load_page(page_id).get("name") or ""
    artbook_share.export_page(page_id, package, lambda page: prepare_page(page, keep_pictures, untick))
    entry = {"name": name, "created": time.time(), "from": source}
    temp = meta.with_name(meta.name + ".tmp")
    temp.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(temp, meta)
    return {"id": template_id, **entry}


def list_templates() -> list[dict[str, Any]]:
    """Your templates, oldest first: {"id", "name", "created", "from"}."""
    out = []
    place = folder()
    if not place.is_dir():
        return out
    for meta in place.glob("*.json"):
        if not _ID.fullmatch(meta.stem) or not (place / f"{meta.stem}{artbook_share.SUFFIX}").is_file():
            continue
        try:
            entry = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            created = entry.get("created")
            out.append({"id": meta.stem, "name": entry["name"], "created": created if isinstance(created, (int, float)) else 0.0,
                        "from": entry.get("from") if isinstance(entry.get("from"), str) else ""})
    return sorted(out, key=lambda e: e["created"])


def thumbnail(template_id: str) -> Optional[bytes]:
    """A template's small picture (its page's, when it was saved), as PNG data."""
    package, _meta = _paths(template_id)
    try:
        with zipfile.ZipFile(package) as archive:
            info = archive.getinfo("thumb.png")
            if info.file_size > 8 * 1024 * 1024:
                return None
            return archive.read(info)
    except (OSError, KeyError, zipfile.BadZipFile):
        return None


def rename_template(template_id: str, name: str) -> None:
    _package, meta = _paths(template_id)
    entry = json.loads(meta.read_text(encoding="utf-8"))
    entry["name"] = store.clean_name(name)
    temp = meta.with_name(meta.name + ".tmp")
    temp.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(temp, meta)


def delete_template(template_id: str) -> None:
    for path in _paths(template_id):
        path.unlink(missing_ok=True)


def page_from_template(template_id: str, name: str, folder_id: Optional[str] = None) -> dict[str, Any]:
    """A new page (named name, in folder_id) made from a template; returns it."""
    package, _meta = _paths(template_id)
    if not package.is_file():
        raise store.PageError("That template isn't there any more.")
    page = artbook_share.import_page(package)
    page = store.rename_page(page["id"], name)
    store.page_tree()  # (files it in the index, at the top level)
    if folder_id is not None:
        try:
            store.move_page(page["id"], folder_id)
        except store.PageError:
            pass  # (the folder went: it stays at the top level)
    return page
