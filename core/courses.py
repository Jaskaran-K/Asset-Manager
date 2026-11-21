"""Courses: lesson videos and the files that go with them, in an order of your own.

A course is its own list, saved as one JSON file per course in COURSES_DIR (like an Artbook page): chapters of lessons,
each lesson a video (or a part of one: start / end, for streams cut into lessons), its files, whether it's done and
where you were, and your notes. Files are linked by their paths, never copied or moved; relink() finds moved ones by
name. A course can start empty (new_course) or as a draft of a folder (draft_from_folder): lessons ordered by the
numbers in their names (01_, "Lesson 3", "Part 2"), else in name order, else by date; each other file goes with the
lesson whose number its name or folder starts with, documents without one with the whole course, the rest in the
"Course files" tray (loose) to drag onto a lesson.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from PySide6.QtCore import QFile

from core.scanner import ARCHIVE_EXTENSIONS, VIDEO_EXTENSIONS

COURSES_DIR: Path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AssetManager" / "courses"
FORMAT_VERSION = 1
DONE_FRACTION = 0.9  # a lesson watched this far counts as done
MAX_NAME = 160
MAX_NOTE = 4000
COURSE_DOCUMENTS = {".pdf", ".txt", ".rtf", ".md", ".html", ".htm", ".doc", ".docx"}  # (unnumbered: the whole course's)
SKIPPED_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}
FOLDER_ENTRY_MIN = 4  # an unnumbered folder of at least this many files joins the tray as one entry (a brush folder)
LESSON_WORDS = r"(?:lesson|lecture|part|chapter|episode|ep|video|module|section|class|day)"
_LEADING = re.compile(rf"^\s*(?:{LESSON_WORDS}[\s_.-]*)?0*(\d{{1,3}})(?!\d)(?!\s*-\s*\d)", re.IGNORECASE)
_WORDED = re.compile(rf"{LESSON_WORDS}[\s_.-]*0*(\d{{1,3}})(?!\d)", re.IGNORECASE)
_RANGE = re.compile(r"(?<!\d)(\d{1,3})\s*-\s*(\d{1,3})(?!\d)")
# The usual order of a 3D course's steps, for lessons whose names have no numbers ("ModelingAndSculpting" before
# "BakingAndTexturing"), and for files with no number to go with a lesson ("Zbrush" / "Bakes" / "Marmoset" folders)
STAGES = (
    (0, r"intro|welcome|promo|trailer|overview|preview"),
    (1, r"references?|refs|concepts?|planning|plan|set ?up|prep\w*|gather\w*"),
    (2, r"block\w*|model\w*|sculpt\w*|base ?mesh\w*|high ?poly|zbrush|maya|blender"),
    (3, r"retopo\w*|topolog\w*|low ?poly"),
    (4, r"uvs?|unwrap\w*"),
    (5, r"bak(?:e|es|ed|ing)\w*|xnormal"),
    (6, r"textur\w*|paint\w*|materials?|shad(?:ing|er|ers)|look ?dev|substance"),
    (7, r"rig|rigg\w*|skinning|pos(?:e|es|ing)"),
    (8, r"light\w*"),
    (9, r"render\w*|marmoset|toolbag|present\w*|composit\w*|post"),
    (10, r"outro|wrap ?up|wrapping|conclu\w*|final|bonus|extras?"),
)
_STAGE_WORDS = [(rank, re.compile(rf"\b(?:{words})\b")) for rank, words in STAGES]
STAGE_TYPES = {".ztl": 2, ".zpr": 2, ".ma": 2, ".mb": 2, ".blend": 2, ".spp": 6, ".sbsar": 6, ".tbscene": 9}


def to_recycle_bin(path: Path) -> bool:
    """(Tests replace this.)"""
    return QFile.moveToTrash(str(path))


class CourseError(Exception):
    """A course can't be read, saved or made (the message says why, for the user)."""


# ----------------------------------------------------------------------
# Names and numbers
# ----------------------------------------------------------------------
def lesson_number(name: str) -> Optional[int]:
    """The lesson a file or folder name is numbered as ("05_Head", "Lesson 3", "Part 2 - Wings"), or None. A range
    ("02-19", a zip of lessons 2 to 19) isn't a lesson's number."""
    stem = Path(name).stem if Path(name).suffix.lower() in VIDEO_EXTENSIONS | ARCHIVE_EXTENSIONS else name
    found = _LEADING.match(stem) or _WORDED.search(stem)
    return int(found.group(1)) if found else None


def is_range(name: str) -> bool:
    """A name for a run of lessons ("02-19", "videos_01-17"): not one lesson."""
    return bool(_RANGE.search(name))


def words_of(name: str) -> str:
    """A name as lower-case words: "BakingAndTexturing" -> "baking and texturing"."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", name)
    return " ".join(re.sub(r"[^A-Za-z0-9]+", " ", text).lower().split())


def stages_of(name: str) -> list[int]:
    """The course steps a name speaks of (STAGES), in the order it names them."""
    words = words_of(name)
    found = []
    for rank, pattern in _STAGE_WORDS:
        match = pattern.search(words)
        if match:
            found.append((match.start(), rank))
    return [rank for _at, rank in sorted(found)]


def natural_key(text: str) -> list:
    """Sorts "Part 2" before "Part 10"."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def clean_title(stem: str) -> str:
    """A lesson's name from its file's: "05_HeadBlockout" -> "Head Blockout", "Lesson 3 - Wings" -> "Wings"."""
    text = _LEADING.sub("", stem, count=1) if _LEADING.match(stem) else _WORDED.sub("", stem, count=1)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)", " ", text)  # (CamelCase and words run into numbers)
    text = re.sub(r"[_.]+", " ", text)
    text = re.sub(r"^[\s\-–:]+|[\s\-–:]+$", "", text)
    text = " ".join(text.split())
    return text[:MAX_NAME] or " ".join(re.sub(r"[_.]+", " ", stem).split())[:MAX_NAME] or "Lesson"


def format_length(ms: Optional[int]) -> str:
    """12:04, or 1:02:40 past an hour; "" when unknown."""
    if not ms or ms <= 0:
        return ""
    seconds = int(ms) // 1000
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


# ----------------------------------------------------------------------
# A course
# ----------------------------------------------------------------------
def new_id() -> str:
    return uuid.uuid4().hex


def new_lesson(name: str, video: Optional[str] = None, number: str = "", start: Optional[int] = None,
               end: Optional[int] = None, url: Optional[str] = None) -> dict[str, Any]:
    """A lesson: its video (a file), or its web address (url: core.course_links; once downloaded for offline, video
    is that copy and url stays)."""
    return {"id": new_id(), "name": name[:MAX_NAME] or "Lesson", "number": number, "video": video, "start": start,
            "end": end, "url": url, "files": [], "done": False, "watched": 0.0, "position": None, "duration": None,
            "notes": []}


def new_chapter(name: str, lessons: Optional[list] = None) -> dict[str, Any]:
    return {"id": new_id(), "name": name[:MAX_NAME] or "Lessons", "lessons": list(lessons or [])}


def new_course(name: str) -> dict[str, Any]:
    """An empty course (drag videos and files in)."""
    return {"version": FORMAT_VERSION, "id": new_id(), "name": (name.strip() or "New course")[:MAX_NAME],
            "created": time.time(), "opened": time.time(), "folder": None, "chapters": [new_chapter("Lessons")],
            "files": [], "loose": [], "artbook_page": None, "page_layout": {}, "last_lesson": None}


def course_path(course_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", course_id or ""):
        raise CourseError("That isn't a course.")
    return COURSES_DIR / f"{course_id}.json"


def save_course(course: dict[str, Any]) -> None:
    """Write it whole (to a temporary file first, so a crash never leaves half a course)."""
    path = course_path(course["id"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(course, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise CourseError(f"Couldn't save the course: {exc}") from exc


def _clean(course: Any) -> dict[str, Any]:
    """A course as read, made safe to use: what's missing or the wrong type gets its default."""
    if not isinstance(course, dict) or not isinstance(course.get("id"), str):
        raise CourseError("The course's file is damaged.")
    clean = new_course(str(course.get("name") or "Course"))
    clean.update({k: course[k] for k in ("id", "created", "opened", "folder", "artbook_page", "last_lesson")
                  if k in course})
    clean["files"] = [p for p in course.get("files", []) if isinstance(p, str)]
    clean["loose"] = [p for p in course.get("loose", []) if isinstance(p, str)]
    clean["page_layout"] = course["page_layout"] if isinstance(course.get("page_layout"), dict) else {}
    chapters = []
    for chapter in course.get("chapters", []) if isinstance(course.get("chapters"), list) else []:
        if not isinstance(chapter, dict):
            continue
        lessons = []
        for lesson in chapter.get("lessons", []) if isinstance(chapter.get("lessons"), list) else []:
            if not isinstance(lesson, dict):
                continue
            made = new_lesson(str(lesson.get("name") or "Lesson"))
            made.update({k: lesson[k] for k in made if k in lesson})
            made["files"] = [p for p in made["files"] if isinstance(p, str)] if isinstance(made["files"], list) else []
            made["notes"] = [n for n in made["notes"] if isinstance(n, dict) and isinstance(n.get("text"), str)
                             and (n.get("at") is None or isinstance(n.get("at"), (int, float)))] if isinstance(made["notes"], list) else []
            if not (isinstance(made["url"], str) and made["url"].lower().startswith(("http://", "https://"))):
                made["url"] = None
            lessons.append(made)
        chapters.append({"id": str(chapter.get("id") or new_id()), "name": str(chapter.get("name") or "Lessons"),
                         "lessons": lessons})
    clean["chapters"] = chapters or [new_chapter("Lessons")]
    return clean


def load_course(course_id: str) -> dict[str, Any]:
    try:
        return _clean(json.loads(course_path(course_id).read_text(encoding="utf-8")))
    except OSError as exc:
        raise CourseError(f"Couldn't read the course: {exc}") from exc
    except ValueError as exc:
        raise CourseError("The course's file is damaged.") from exc


def list_courses() -> list[dict[str, Any]]:
    """Every course, the one opened last first (damaged ones left out)."""
    out = []
    if COURSES_DIR.is_dir():
        for path in COURSES_DIR.glob("*.json"):
            try:
                out.append(load_course(path.stem))
            except CourseError:
                continue
    return sorted(out, key=lambda c: -float(c.get("opened") or 0))


def trash_course(course_id: str) -> None:
    """The course (its list, notes and progress) to the Recycle Bin. Its videos and files stay where they are."""
    path = course_path(course_id)
    if path.exists() and not to_recycle_bin(path):
        raise CourseError("Couldn't move the course to the Recycle Bin.")


# ----------------------------------------------------------------------
# Lessons
# ----------------------------------------------------------------------
def lessons(course: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for chapter in course["chapters"]:
        yield from chapter["lessons"]


def find_lesson(course: dict[str, Any], lesson_id: Optional[str]) -> Optional[tuple[dict, dict]]:
    """(its chapter, the lesson), or None."""
    for chapter in course["chapters"]:
        for lesson in chapter["lessons"]:
            if lesson["id"] == lesson_id:
                return chapter, lesson
    return None


def lesson_after(course: dict[str, Any], lesson_id: str) -> Optional[dict[str, Any]]:
    flat = list(lessons(course))
    ids = [lesson["id"] for lesson in flat]
    if lesson_id not in ids:
        return None
    at = ids.index(lesson_id)
    return flat[at + 1] if at + 1 < len(flat) else None


def lesson_length(lesson: dict[str, Any]) -> Optional[int]:
    """How long the lesson is (ms): its part of the video, or the whole video; None if not known yet."""
    start = lesson.get("start") or 0
    end = lesson.get("end") or lesson.get("duration")
    return max(0, int(end) - int(start)) if end else None


def progress(course: dict[str, Any]) -> tuple[int, int, int]:
    """(lessons done, lessons, ms left in the lessons not done yet, as far as their lengths are known)."""
    done = total = left = 0
    for lesson in lessons(course):
        total += 1
        if lesson["done"]:
            done += 1
        else:
            length = lesson_length(lesson) or 0
            left += int(length * (1 - min(1.0, float(lesson.get("watched") or 0))))
    return done, total, left


def next_up(course: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Where to carry on: the lesson you were on, unless it's done; then the first one not done after it."""
    flat = list(lessons(course))
    if not flat:
        return None
    last = find_lesson(course, course.get("last_lesson"))
    if last is not None:
        if not last[1]["done"]:
            return last[1]
        after = flat[flat.index(last[1]) + 1:]
        return next((lesson for lesson in after if not lesson["done"]), None) or next((l for l in flat if not l["done"]), None)
    return next((lesson for lesson in flat if not lesson["done"]), None)


def record_watch(lesson: dict[str, Any], position: int) -> bool:
    """You're at position (ms, in the video) in this lesson: where you were, and how far you've watched. True when
    this makes it done (watched DONE_FRACTION of it)."""
    lesson["position"] = int(position)
    length = lesson_length(lesson)
    if not length:
        return False
    fraction = min(1.0, max(0.0, (position - (lesson.get("start") or 0)) / length))
    lesson["watched"] = max(float(lesson.get("watched") or 0), round(fraction, 4))
    if not lesson["done"] and lesson["watched"] >= DONE_FRACTION:
        lesson["done"] = True
        return True
    return False


def add_note(lesson: dict[str, Any], at: Optional[int], text: str) -> dict[str, Any]:
    """A note at a moment of the lesson (at: ms), or with no moment (a lesson watched in the browser)."""
    note = {"id": new_id(), "at": None if at is None else max(0, int(at)), "text": text.strip()[:MAX_NOTE],
            "created": time.time()}
    lesson["notes"].append(note)
    lesson["notes"].sort(key=note_order)
    return note


def note_order(note: dict[str, Any]) -> tuple:
    return (note.get("at") is None, note.get("at") or 0, note.get("created") or 0)


def new_web_lesson(url: str, name: str) -> dict[str, Any]:
    return new_lesson(name, None, "", url=url.strip())


def remove_note(lesson: dict[str, Any], note_id: str) -> None:
    lesson["notes"] = [n for n in lesson["notes"] if n.get("id") != note_id]


# ----------------------------------------------------------------------
# A long video cut into lessons
# ----------------------------------------------------------------------
def parts_of(course: dict[str, Any], video: str) -> list[dict[str, Any]]:
    """The lessons that are parts of this video, in order."""
    return sorted((lesson for lesson in lessons(course) if lesson.get("video") == video), key=lambda l: l.get("start") or 0)


def cut_lesson(course: dict[str, Any], lesson_id: str, at: int) -> Optional[dict[str, Any]]:
    """Cut a lesson's video at `at` (ms): the lesson ends there, and a new lesson right after it plays on from there
    (its notes after the cut go with it). None if `at` isn't inside the lesson."""
    found = find_lesson(course, lesson_id)
    if found is None:
        return None
    chapter, lesson = found
    start = lesson.get("start") or 0
    end = lesson.get("end") or lesson.get("duration")
    if at <= start + 1000 or (end and at >= end - 1000):
        return None
    count = len(parts_of(course, lesson["video"]))
    part = new_lesson(f"Part {count + 1}", lesson["video"], "", int(at), lesson.get("end"))
    part["duration"] = lesson.get("duration")
    lesson["end"] = int(at)
    part["notes"] = [n for n in lesson["notes"] if (n["at"] or 0) >= at]
    lesson["notes"] = [n for n in lesson["notes"] if (n["at"] or 0) < at]
    if lesson.get("start") is None:
        lesson["start"] = 0
    chapter["lessons"].insert(chapter["lessons"].index(lesson) + 1, part)
    return part


def join_with_next(course: dict[str, Any], lesson_id: str) -> bool:
    """Remove the cut at the end of this lesson: it and the part after it are one lesson again."""
    found = find_lesson(course, lesson_id)
    if found is None:
        return False
    _chapter, lesson = found
    parts = parts_of(course, lesson["video"])
    if lesson not in parts or parts.index(lesson) + 1 >= len(parts):
        return False
    after = parts[parts.index(lesson) + 1]
    lesson["end"] = after.get("end")
    lesson["notes"] += after["notes"]
    lesson["files"] += [p for p in after["files"] if p not in lesson["files"]]
    lesson["done"] = lesson["done"] and after["done"]
    for chapter in course["chapters"]:
        if after in chapter["lessons"]:
            chapter["lessons"].remove(after)
    return True


def move_cut(course: dict[str, Any], lesson_id: str, at: int) -> bool:
    """Drag the cut at the end of this lesson to `at` (inside both parts it divides)."""
    found = find_lesson(course, lesson_id)
    if found is None:
        return False
    lesson = found[1]
    parts = parts_of(course, lesson["video"])
    if lesson not in parts or parts.index(lesson) + 1 >= len(parts):
        return False
    after = parts[parts.index(lesson) + 1]
    end = after.get("end") or after.get("duration")
    if at <= (lesson.get("start") or 0) + 1000 or (end and at >= end - 1000):
        return False
    lesson["end"] = after["start"] = int(at)
    notes = sorted(lesson["notes"] + after["notes"], key=note_order)  # (each with the part it's now in)
    lesson["notes"] = [n for n in notes if (n["at"] or 0) < at]
    after["notes"] = [n for n in notes if (n["at"] or 0) >= at]
    return True


# ----------------------------------------------------------------------
# A draft from a folder
# ----------------------------------------------------------------------
def _walk(folder: Path, should_stop: Optional[Callable[[], bool]]) -> Iterator[Path]:
    for root, dirs, files in os.walk(folder):
        if should_stop is not None and should_stop():
            raise CourseError("Stopped.")
        dirs[:] = sorted((d for d in dirs if not d.startswith(".")), key=natural_key)
        for name in sorted(files, key=natural_key):
            if name.lower() not in SKIPPED_FILES and not name.startswith("~$"):
                yield Path(root, name)


def draft_from_folder(folder: Path, should_stop: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """A course drafted from a folder's videos and files (see the module docstring). Raises CourseError."""
    folder = Path(folder)
    if not folder.is_dir():
        raise CourseError(f"{folder} isn't a folder.")
    videos, others = [], []
    for path in _walk(folder, should_stop):
        ext = path.suffix.lower()
        if ext in VIDEO_EXTENSIONS:
            videos.append(path)
        elif ext not in ARCHIVE_EXTENSIONS:
            others.append(path)
    if not videos:
        raise CourseError("There are no videos in that folder.")
    course = new_course(clean_title(folder.name) if lesson_number(folder.name) is None else folder.name)
    course["folder"] = str(folder)

    # Chapters: one per folder of videos, unless they're just ranges of one numbered run (zips of lessons 1-17...)
    groups: dict[str, list[Path]] = {}
    for video in videos:
        groups.setdefault(str(video.parent.relative_to(folder)), []).append(video)
    numbers = [lesson_number(v.name) for v in videos]
    numbered = all(n is not None for n in numbers) and len(set(numbers)) == len(numbers)
    names = [Path(g).name for g in groups if g != "."]
    one_run = len(groups) == 1 or (numbered and all(is_range(n) or lesson_number(n) is None for n in names))

    def ordered(paths: list[Path]) -> list[Path]:
        if all(lesson_number(p.name) is not None for p in paths):
            return sorted(paths, key=lambda p: (lesson_number(p.name), natural_key(p.name)))
        if any(stages_of(p.stem) for p in paths):  # (the steps they name: modeling before baking; the rest after)
            return sorted(paths, key=lambda p: ((stages_of(p.stem) or [99])[0], natural_key(p.name)))
        if any(re.search(r"\d", p.stem) for p in paths):
            return sorted(paths, key=lambda p: natural_key(p.name))
        if len({int(p.stat().st_mtime) for p in paths}) > 1:
            return sorted(paths, key=lambda p: p.stat().st_mtime)  # (names that say nothing: by date)
        return sorted(paths, key=lambda p: natural_key(p.name))

    def lesson_of(video: Path) -> dict[str, Any]:
        number = lesson_number(video.name)
        return new_lesson(clean_title(video.stem), str(video), f"{number:02d}" if number is not None else "")

    if one_run:
        course["chapters"] = [new_chapter("Lessons", [lesson_of(v) for v in ordered(videos)])]
    else:
        course["chapters"] = [
            new_chapter(clean_title(Path(group).name) if group != "." else "Lessons", [lesson_of(v) for v in ordered(paths)])
            for group, paths in sorted(groups.items(), key=lambda g: natural_key(g[0]))
        ]

    # Files: with the lesson their name (or a folder they're in) is numbered as
    by_number = {}
    for lesson in lessons(course):
        if lesson["number"]:
            by_number.setdefault(int(lesson["number"]), lesson)
    by_stage: dict[int, list[dict]] = {}  # (a step: the lessons that name it; numbered lessons go by their numbers)
    for lesson in lessons(course):
        for rank in [] if lesson["number"] else stages_of(lesson["name"]):
            by_stage.setdefault(rank, []).append(lesson)
    staged: dict[tuple[str, Path], list[Path]] = {}  # (the lesson, the folder: its files there, put in together)
    trayed_folders: dict[Path, list[Path]] = {}
    for path in others:
        relative = path.relative_to(folder)
        number = lesson_number(path.stem)
        if number is None:
            for part in reversed(relative.parts[:-1]):
                if not is_range(part) and lesson_number(part) is not None:
                    number = lesson_number(part)
                    break
        stage_lesson = None
        if number is None and by_stage:  # (no number: the step its name or folders speak of, if one lesson names it)
            hints = [path.stem, *reversed(relative.parts[:-1])]
            ranks = [rank for hint in hints for rank in stages_of(hint)]
            if path.suffix.lower() in STAGE_TYPES:
                ranks.append(STAGE_TYPES[path.suffix.lower()])
            stage_lesson = next((by_stage[r][0] for r in ranks if len(by_stage.get(r, [])) == 1), None)
        if number is not None and number in by_number:
            by_number[number]["files"].append(str(path))
        elif stage_lesson is not None and not (path.suffix.lower() in COURSE_DOCUMENTS and len(relative.parts) == 1):
            staged.setdefault((stage_lesson["id"], path.parent), []).append(path)
        elif path.suffix.lower() in COURSE_DOCUMENTS and len(relative.parts) <= 2:
            course["files"].append(str(path))
        elif len(relative.parts) > 1:
            trayed_folders.setdefault(folder / relative.parts[0], []).append(path)
        else:
            course["loose"].append(str(path))
    lessons_by_id = {lesson["id"]: lesson for lesson in lessons(course)}
    for (lesson_id, parent), paths in staged.items():  # (a folder of fifteen bakes: one entry)
        whole = parent != folder and len(paths) >= FOLDER_ENTRY_MIN and len(paths) == sum(1 for p in parent.iterdir() if p.is_file())
        lessons_by_id[lesson_id]["files"] += [str(parent)] if whole else [str(p) for p in paths]
    for top, paths in trayed_folders.items():  # (a folder of brushes or alphas: one entry, not forty)
        if len(paths) >= FOLDER_ENTRY_MIN and not any(v.is_relative_to(top) for v in videos):
            course["loose"].append(str(top))
        else:
            course["loose"] += [str(p) for p in paths]
    return course


def add_videos(course: dict[str, Any], paths: list[Path], chapter_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Videos added by hand (Add Videos..., or dropped): a lesson each, at the end of the chapter (the last one)."""
    chapter = next((c for c in course["chapters"] if c["id"] == chapter_id), course["chapters"][-1])
    made = []
    for path in sorted((Path(p) for p in paths), key=lambda p: natural_key(p.name)):
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            number = lesson_number(path.name)
            lesson = new_lesson(clean_title(path.stem), str(path), f"{number:02d}" if number is not None else "")
            chapter["lessons"].append(lesson)
            made.append(lesson)
    return made


# ----------------------------------------------------------------------
# Files that moved
# ----------------------------------------------------------------------
def linked_paths(course: dict[str, Any]) -> list[str]:
    out = list(course["files"]) + list(course["loose"])
    for lesson in lessons(course):
        if lesson.get("video"):
            out.append(lesson["video"])
        out += lesson["files"]
    return out


def missing_paths(course: dict[str, Any]) -> list[str]:
    return [p for p in dict.fromkeys(linked_paths(course)) if not os.path.exists(p)]


def relink(course: dict[str, Any], folder: Path, should_stop: Optional[Callable[[], bool]] = None) -> int:
    """Files that moved, found again by name in folder (and its folders): how many were found. A name found twice
    isn't guessed at."""
    missing = missing_paths(course)
    if not missing:
        return 0
    wanted = {Path(p).name.lower() for p in missing}
    found: dict[str, list[str]] = {}
    for root, dirs, files in os.walk(folder):
        if should_stop is not None and should_stop():
            break
        for name in files + dirs:
            if name.lower() in wanted:
                found.setdefault(name.lower(), []).append(str(Path(root, name)))
    moved = {p: found[Path(p).name.lower()][0] for p in missing if len(found.get(Path(p).name.lower(), [])) == 1}

    def fix(path: str) -> str:
        return moved.get(path, path)

    course["files"] = [fix(p) for p in course["files"]]
    course["loose"] = [fix(p) for p in course["loose"]]
    for lesson in lessons(course):
        if lesson.get("video"):
            lesson["video"] = fix(lesson["video"])
        lesson["files"] = [fix(p) for p in lesson["files"]]
    return len(moved)
