"""A course's Artbook page: "Course: <its name>", where each note you make while watching lands as a card with the
frame it was made on, grouped together, in a row per lesson (the lesson's name as a heading at the start of the row).

The page is made with the first note (or by Open the Artbook page). A note goes straight into the page's file, so the
page you have open in the Artbook stays put; if the course's page is the one open, the note goes on the board instead
(one undo step there). Where the next card goes is kept in the course ("page_layout"), so moving cards about on the
page never puts a new one on top of them... unless you move them where the next one goes.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from PySide6.QtCore import QPointF
from PySide6.QtGui import QImage

from core import artbook_store as store
from core import courses
from ui.video_player import format_time

CARD_WIDTH = 420.0  # a frame's width on the page, and its note's
GAP = 40.0
ROW_TOP = 420.0  # the first row, under the page's header
ROW_HEIGHT = 560.0
HEADING_HEIGHT = 80.0


def page_name(course: dict[str, Any]) -> str:
    return f"Course: {course['name']}"


def page_exists(page_id: Optional[str]) -> bool:
    return bool(page_id) and store.page_summary(page_id) is not None


def ensure_page(course: dict[str, Any], panel) -> str:
    """The course's page (made, with a header, if it has none or it was deleted); the course is saved."""
    if page_exists(course.get("artbook_page")):
        return course["artbook_page"]
    from ui.artbook_board import page_header_dict

    name = page_name(course)
    if any((p.get("name") or "").casefold() == name.casefold() for p in store.list_pages()):
        name = store.unique_page_name(name)
    page = store.create_page(name)
    header = page_header_dict(page)
    header["about"] = "Notes from the course, each with the frame it was made on"
    page["items"] = [header]
    store.save_page_now(page)
    course["artbook_page"] = page["id"]
    course["page_layout"] = {}
    courses.save_course(course)
    if panel is not None:
        panel.reload_pages()
    return page["id"]


def _note_dict(text: str, x: float, y: float, style: str = "card", width: float = CARD_WIDTH, group: Optional[str] = None) -> dict:
    return {"id": uuid.uuid4().hex, "kind": "note", "text": text, "html": "", "fill": None, "width": width,
            "group": group, "style": style, "x": x, "y": y, "scale": 1.0, "rotation": 0.0, "z": 0.0, "opacity": 1.0}


def note_items(course: dict[str, Any], lesson: dict[str, Any], note: dict[str, Any], page_id: str,
               frame: Optional[QImage]) -> list[dict[str, Any]]:
    """The cards for one note (its frame, stored in the page, and the note under it; the lesson's heading first if
    its row is new), placed where its lesson's row goes on; the course's layout moves on (save the course after)."""
    layout = course.setdefault("page_layout", {})
    rows = layout.setdefault("rows", {})
    items = []
    row = rows.get(lesson["id"])
    if row is None:
        row = {"y": float(layout.get("next_y", ROW_TOP)), "x": 0.0}
        rows[lesson["id"]] = row
        layout["next_y"] = row["y"] + ROW_HEIGHT
        title = f"{lesson['number']} · {lesson['name']}" if lesson.get("number") else lesson["name"]
        items.append(_note_dict(title, 0.0, row["y"], "heading", 600.0))
    x, top = row["x"], row["y"] + HEADING_HEIGHT
    stamp = format_time(note["at"]) if note.get("at") is not None else ""  # (a browser lesson's notes have no time)
    group = uuid.uuid4().hex
    text_top = top
    if frame is not None and not frame.isNull():
        blob = store.store_blob(page_id, store.encode_png(frame), ".png")
        scale = CARD_WIDTH / max(1, frame.width())
        height = frame.height() * scale
        items.append({
            "id": uuid.uuid4().hex, "kind": "image", "source": {"path": None, "blob": blob},
            "size": [frame.width(), frame.height()], "x": x + CARD_WIDTH / 2, "y": top + height / 2, "scale": scale,
            "rotation": 0.0, "flip_h": False, "flip_v": False, "z": 0.0, "crop": None, "opacity": 1.0, "gray": False,
            "group": group, "caption": "", "pixelated": False, "name": f"{lesson['name']} {stamp}".strip(),
            "values": 0, "guides": "",
        })
        text_top = top + height + 12.0
    items.append(_note_dict(f"{stamp} · {note['text']}" if stamp else note["text"], x, text_top, group=group))
    row["x"] = x + CARD_WIDTH + GAP
    return items


def add_note(course: dict[str, Any], lesson: dict[str, Any], note: dict[str, Any], frame: Optional[QImage], panel) -> str:
    """Put a note (and its frame) on the course's page; its id. The course is saved."""
    page_id = ensure_page(course, panel)
    items = note_items(course, lesson, note, page_id, frame)
    if panel is not None and panel.current_page_id() == page_id:
        bounds = [0.0, 0.0, 1.0, 1.0]
        panel.board.insert_package({"version": 1, "page": page_id, "items": items, "comments": [], "groups": {},
                                    "bounds": bounds}, offset=QPointF(0, 0), text="course note")
        panel.save_now()
    else:
        page = store.load_page(page_id)
        top = max((float(d.get("z") or 0) for d in page.get("items", []) if isinstance(d, dict)), default=0.0)
        for n, item in enumerate(items, 1):
            item["z"] = top + n
        page["items"] = list(page.get("items", [])) + items
        store.save_page_now(page)
        if panel is not None:
            panel.page_saved.emit(page_id)
    courses.save_course(course)
    return page_id
