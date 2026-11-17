"""Finding words on Artbook pages: what each saved item says (a note's text, a table's cells, a card's title, an
image's name, a comment...), and the pages whose content has a word, with a few words around each match. The
Artbook's find bar (ui.artbook_find) searches every page with this, and the Pages sidebar finds pages by what's on
them. No Qt here: it runs off the UI thread (pages are read from disk, each kept until its file changes).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from . import artbook_store as store

SNIPPET_ROOM = 34  # characters shown each side of a match
MAX_HITS_PER_PAGE = 40

_cache: dict[str, tuple[Any, list[dict[str, Any]]]] = {}  # page id -> (its file's stamp, its texts)
_cache_lock = threading.Lock()


def item_kind(item: dict[str, Any]) -> str:
    """What a saved item is, for a result's label: note / todo / heading / title / table / link / file / page_card /
    image / column / line."""
    kind = item.get("kind", "image")
    if kind == "note":
        style = item.get("style")
        if style in ("heading", "title"):
            return style
        html = item.get("html") if isinstance(item.get("html"), str) else ""
        return "todo" if '<li class="checked"' in html or '<li class="unchecked"' in html else "note"
    return kind if isinstance(kind, str) else "image"


def item_texts(item: dict[str, Any]) -> list[str]:
    """The words a saved item shows (each separately, so a match's snippet stays in one of them)."""
    kind = item.get("kind", "image")

    def text(key: str) -> str:
        value = item.get(key)
        return value if isinstance(value, str) else ""

    if kind == "note":
        return [text("text"), text("about")]
    if kind == "table":
        out = [text("title")]
        out += [c.get("name", "") for c in item.get("columns", []) if isinstance(c, dict) and isinstance(c.get("name"), str)]
        for row in item.get("rows", []) if isinstance(item.get("rows"), list) else []:
            out += [cell for cell in row if isinstance(cell, str)] if isinstance(row, list) else []
        return out
    if kind == "link":
        return [text("title"), text("about"), text("site")]
    if kind == "file":
        return [text("name") or Path(text("path")).name]
    if kind == "page_card":
        return [text("name")]
    if kind == "image":
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        path = source.get("path") if isinstance(source.get("path"), str) else ""
        return [text("name") or (Path(path).name if path else ""), text("caption")]
    if kind == "column":
        return [text("title")]
    if kind == "line":
        return [text("label")]
    return []


def occurrences(text: str, query: str) -> list[int]:
    """Where query is in text (ignoring case), each start."""
    if not query:
        return []
    low, q = text.lower(), query.lower()
    out, start = [], low.find(q)
    while start >= 0:
        out.append(start)
        start = low.find(q, start + len(q))
    return out


def snippet(text: str, start: int, length: int, room: int = SNIPPET_ROOM) -> tuple[str, str, str]:
    """(before, the match, after): a few words either side, on one line, with "..." where it's cut."""
    flat = " ".join(text.split()) if "\n" in text or "  " in text else text
    if flat != text:  # (lines joined: find it again in the flat text)
        hits = occurrences(flat, text[start:start + length])
        start = hits[0] if hits else 0
        text = flat
    before, after = text[max(0, start - room):start], text[start + length:start + length + room]
    if start - room > 0:
        cut = before.find(" ")
        before = "..." + (before[cut + 1:] if 0 <= cut < room // 2 else before)
    if start + length + room < len(text):
        cut = after.rfind(" ")
        after = (after[:cut] if cut > room // 2 else after) + "..."
    return before, text[start:start + length], after


def _texts(page: dict[str, Any]) -> list[dict[str, Any]]:
    """What a page says, as entries {"item": id or None, "thread": id or None, "kind", "texts"}."""
    out = []
    for item in page.get("items", []) if isinstance(page.get("items"), list) else []:
        if isinstance(item, dict) and item.get("kind") != "stroke":
            texts = [t for t in item_texts(item) if t]
            if texts:
                out.append({"item": item.get("id"), "thread": None, "kind": item_kind(item), "texts": texts})
    for thread in page.get("comments", []) if isinstance(page.get("comments"), list) else []:
        if isinstance(thread, dict):
            texts = [n["text"] for n in thread.get("notes", []) if isinstance(n, dict) and isinstance(n.get("text"), str)]
            if texts:
                out.append({"item": thread.get("item"), "thread": thread.get("id"), "kind": "comment", "texts": texts})
    return out


def search_page(page: dict[str, Any], query: str, limit: int = MAX_HITS_PER_PAGE) -> list[dict[str, Any]]:
    """Each item (or comment thread) on a page that has query: {"item", "thread", "kind", "count" (how many times),
    "before", "match", "after" (its first match, with words around it)}, in the page's order."""
    return _search_texts(_texts(page), query.strip(), limit)


def _search_texts(entries: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    hits = []
    if not query:
        return hits
    for entry in entries:
        count, first = 0, None
        for text in entry["texts"]:
            found = occurrences(text, query)
            if found and first is None:
                first = snippet(text, found[0], len(query))
            count += len(found)
        if count:
            before, match, after = first
            hits.append({"item": entry["item"], "thread": entry["thread"], "kind": entry["kind"], "count": count,
                         "before": before, "match": match, "after": after})
            if len(hits) >= limit:
                break
    return hits


def _page_texts(page_id: str) -> Optional[tuple[str, list[dict[str, Any]]]]:
    """(name, what it says) of a page on disk (kept until its file changes); None if it can't be read."""
    folder = store.page_dir(page_id)
    stamp = (store._stamp(folder / store.PAGE_FILE), store._stamp(folder / store.BACKUP_FILE))
    with _cache_lock:
        cached = _cache.get(page_id)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        page = store.load_page(page_id)
    except (store.PageError, OSError, ValueError):
        return None
    texts = (str(page.get("name") or ""), _texts(page))
    with _cache_lock:
        _cache[page_id] = (stamp, texts)
    return texts


def search_pages(query: str, skip: Optional[str] = None, limit: int = MAX_HITS_PER_PAGE) -> list[dict[str, Any]]:
    """Every page (but skip) with query in its name or on it, in the Pages order: {"page", "name", "count",
    "name_match" (its name has it), "hits" (search_page's)}."""
    query = query.strip()
    if not query:
        return []
    out = []
    for page_id in store.ordered_page_ids():
        if page_id == skip:
            continue
        found = _page_texts(page_id)
        if found is None:
            continue
        name, entries = found
        hits = _search_texts(entries, query, limit)
        named = bool(occurrences(name, query))
        if hits or named:
            out.append({"page": page_id, "name": name, "count": sum(h["count"] for h in hits), "name_match": named, "hits": hits})
    return out
