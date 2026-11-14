"""Tag UI: chips, the tag-aware search bar, and the bulk tagging dialog.

Data access goes through core.tag_repository; slow queries run off the UI
thread with run_in_background().
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QPoint,
    QRect,
    QRunnable,
    QSize,
    QStringListModel,
    Qt,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCompleter,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from core.search_query import TAG_NAME, TAG_TOKEN, quote_tag_name
from core.tag_repository import MAX_TAG_NAME_LENGTH, Tag, TagChange, TagError, TagRepository
from ui import shortcuts
from ui.duotone import Tone, set_icon
from ui.icons import MAIN_CATEGORY_COLORS, SUB_CATEGORY_COLORS, color_swatch_icon

# Colors handed out to new custom tags, in order
TAG_COLORS = list(dict.fromkeys(MAIN_CATEGORY_COLORS + SUB_CATEGORY_COLORS))

# Tag filter tokens (grammar in core.search_query):  tag:name  tag:"two words"  -tag:name  tag:a|b
_NAME = TAG_NAME
_TAG_TOKEN = TAG_TOKEN
_UNFINISHED_TOKEN = re.compile(r'(?i)(?:^|\s)-?tag:(?:"[^"]*|\S*)$')
# The colours shown for color:name in the search bar's palette menu
SEARCH_SWATCHES = {
    "red": "#d63a2f", "orange": "#f08a24", "yellow": "#f2d230", "green": "#3fa34d", "cyan": "#2fb9c4",
    "blue": "#2f62d6", "purple": "#8a45c9", "pink": "#e866b0", "brown": "#7a4a26", "skin": "#e0ac90",
    "black": "#111111", "white": "#f5f5f5", "grey": "#8c8c8c",
}
_SIMILAR_TOKEN = re.compile(r"(?i)(?<!\S)similar:(\d+)(?!\S)")
_LIKE_TOKEN = re.compile(r"(?i)(?<!\S)like:(\d+)(?!\S)")  # More Like This (AI Search)
_AI_TOKEN = re.compile(r"(?i)(?<!\S)ai:on(?!\S)")  # AI Search on (the AI button), in saved searches


@dataclass(frozen=True)
class TagToken:
    """A parsed tag:... token: names of tags (several = any of them), and whether it's excluded."""

    names: tuple[str, ...]
    exclude: bool = False


@dataclass(frozen=True)
class TagFilter:
    """An active tag filter (a chip): has any of `tags`, or with exclude=True, has none of them."""

    tags: tuple[Tag, ...]
    exclude: bool = False


@dataclass(frozen=True)
class TagQuery:
    """Active tag filters, in the shape TagRepository.find_assets() takes."""

    all_of: tuple[int, ...] = ()
    any_of: tuple[tuple[int, ...], ...] = ()
    none_of: tuple[int, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.all_of or self.any_of or self.none_of)


def next_tag_color(existing: Sequence[Tag]) -> str:
    used = {t.color for t in existing}
    return next((c for c in TAG_COLORS if c not in used), TAG_COLORS[len(existing) % len(TAG_COLORS)])


_quote = quote_tag_name


def tag_token(name: str) -> str:
    """Search-bar token for a tag name."""
    return f"tag:{_quote(name)}"


def filter_token(tag_filter: TagFilter) -> str:
    """Search-bar text for a filter chip (the inverse of parse_search_text)."""
    names = "|".join(_quote(t.name) for t in tag_filter.tags)
    return f"{'-' if tag_filter.exclude else ''}tag:{names}"


def parse_search_text(text: str) -> tuple[list[TagToken], str]:
    """Split search text into tag tokens and the remaining free text.

    An unfinished trailing "tag:" is dropped from the free text so the file
    name filter doesn't flicker to "no results" while it's being typed.
    """
    tokens = [
        TagToken(tuple(name.strip('"') for name in re.findall(_NAME, group)), bool(minus))
        for minus, group in _TAG_TOKEN.findall(text)
    ]
    rest = _UNFINISHED_TOKEN.sub(" ", _TAG_TOKEN.sub(" ", text))
    return tokens, " ".join(rest.split())


# ----------------------------------------------------------------------
# Background database work
# ----------------------------------------------------------------------
class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _CallbackRelay(QObject):
    """Lives on the UI thread. Connecting worker signals to its methods (rather than to a
    plain lambda, which PySide would call directly on the worker thread) makes Qt queue
    the call, so callbacks always run on the UI thread.

    Lifetime: parented to the application, so Qt (not Python refcounting) owns it, and it
    holds its task so the task and its signals object outlive the worker thread. Once the
    result is delivered it removes itself with deleteLater(), which runs on the UI thread
    after the current slot returns. Freeing either object any earlier (inside its own slot,
    or on the worker thread) crashes the process when several searches overlap.
    """

    def __init__(self, task: "_DbTask | _Job", on_done: Callable[[Any], None], on_failed: Optional[Callable[[str], None]]):
        super().__init__(QCoreApplication.instance())
        self._task = task
        self._on_done, self._on_failed = on_done, on_failed
        task.signals.done.connect(self.done)
        task.signals.failed.connect(self.failed)

    @Slot(object)
    def done(self, result: Any) -> None:
        try:
            self._on_done(result)
        finally:
            self.deleteLater()

    @Slot(str)
    def failed(self, message: str) -> None:
        try:
            if self._on_failed is not None:
                self._on_failed(message)
        finally:
            self.deleteLater()


class _DbTask(QRunnable):
    def __init__(self, repo: TagRepository, fn: Callable[[TagRepository], Any]):
        super().__init__()
        # The relay owns this task; the pool must not delete it on the worker thread
        self.setAutoDelete(False)
        self.repo, self.fn = repo, fn
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            self.signals.done.emit(self.fn(self.repo))
        except Exception as exc:  # pylint: disable=broad-except
            self.signals.failed.emit(str(exc))
        finally:
            self.repo.close_thread_connection()  # never leave a connection on a pool thread


class _Job(QRunnable):
    """A plain background job (no database): see run_job."""

    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.setAutoDelete(False)  # owned by its relay, as for _DbTask
        self.fn = fn
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            self.signals.done.emit(self.fn())
        except Exception as exc:  # pylint: disable=broad-except
            self.signals.failed.emit(str(exc))


_db_pool: Optional[QThreadPool] = None


_slow_pool: Optional[QThreadPool] = None
_lanes: dict[str, QThreadPool] = {}  # run_in_background(slow="name")


_job_pool: Optional[QThreadPool] = None


def run_job(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None],
    on_failed: Optional[Callable[[str], None]] = None,
) -> None:
    """Run fn() on a worker thread (image decoding, file writes); callbacks run on the UI thread."""
    global _job_pool
    if _job_pool is None:
        _job_pool = QThreadPool()
        _job_pool.setMaxThreadCount(max(2, min(4, QThreadPool.globalInstance().maxThreadCount() - 1)))
    job = _Job(fn)
    _CallbackRelay(job, on_done, on_failed)
    _job_pool.start(job)


def wait_for_background(timeout_ms: int = 5000) -> bool:
    """Drop queued background jobs and wait for running ones (call before the app quits:
    Qt objects being torn down under a running job crash the process). True if all finished."""
    finished = True
    for pool in (_db_pool, _slow_pool, _job_pool, *_lanes.values()):
        if pool is not None:
            pool.clear()
            finished = pool.waitForDone(timeout_ms) and finished
    return finished


def wait_for_database_jobs(timeout_ms: int = 5000) -> bool:
    """Drop queued database jobs and wait for running ones, so none has the database open (before
    it's replaced by a backup). Other background work carries on. True if all finished."""
    finished = True
    for pool in (_db_pool, _slow_pool, *_lanes.values()):
        if pool is not None:
            pool.clear()
            finished = pool.waitForDone(timeout_ms) and finished
    return finished


def run_in_background(
    repo: TagRepository,
    fn: Callable[[TagRepository], Any],
    on_done: Callable[[Any], None],
    on_failed: Optional[Callable[[str], None]] = None,
    slow: bool | str = False,
) -> None:
    """Run fn(repo) on a worker thread (with its own connection); callbacks run on the UI thread.

    slow=True uses a separate single-thread pool, so long background jobs (reading file
    details) never hold up quick ones like tag searches; a name (slow="looks") a single-thread
    pool of that name, so two long jobs don't wait for each other either.
    """
    global _db_pool, _slow_pool
    if _db_pool is None:
        _db_pool = QThreadPool()
        _db_pool.setMaxThreadCount(2)
        _slow_pool = QThreadPool()
        _slow_pool.setMaxThreadCount(1)
    task = _DbTask(repo, fn)
    _CallbackRelay(task, on_done, on_failed)  # owned by the application until it reports back
    if isinstance(slow, str):
        if slow not in _lanes:
            _lanes[slow] = QThreadPool()
            _lanes[slow].setMaxThreadCount(1)
        _lanes[slow].start(task)
    else:
        (_slow_pool if slow else _db_pool).start(task)


# ----------------------------------------------------------------------
# Chips
# ----------------------------------------------------------------------
def _text_color_for(background: QColor) -> str:
    luminance = 0.299 * background.red() + 0.587 * background.green() + 0.114 * background.blue()
    return "#111827" if luminance > 160 else "#ffffff"


def _chip_tone(bg: str, fg: str) -> Tone:
    """Icon colors for a chip's remove button: a body in its text color, the x cut out in its background."""
    return (bg, fg) if bg.startswith("#") and fg.startswith("#") else ("input_bg", "text")


def chip_colors(tag: Tag) -> tuple[str, str, str]:
    """(background, text, border) for a chip: the tag's color for custom tags,
    a neutral outline for default (file type) tags that reads on light and dark themes."""
    if tag.is_default or not tag.color:
        return "rgba(148, 163, 184, 0.16)", "palette(text)", "rgba(148, 163, 184, 0.6)"
    bg = QColor(tag.color)
    return bg.name(), _text_color_for(bg), bg.darker(130).name()


class TagChip(QFrame):
    """Rounded tag label, optionally with a remove (x) button."""

    remove_clicked = Signal(object)  # Tag

    def __init__(self, tag: Tag, removable: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.tag = tag
        bg, fg, border = chip_colors(tag)
        self.setObjectName("tagChip")
        self.setStyleSheet(
            f"#tagChip {{ background-color: {bg}; border: 1px solid {border}; border-radius: 9px; }}"
            f"#tagChip QLabel, #tagChip QToolButton {{ color: {fg}; background: transparent; border: none; padding: 0; }}"
        )
        self.setToolTip("File type tag (automatic)" if tag.is_default else f"Tag: {tag.name}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 1, 8 if not removable else 4, 1)
        layout.setSpacing(4)
        layout.addWidget(QLabel(tag.name))
        if removable:
            btn = QToolButton()
            set_icon(btn, "close", _chip_tone(bg, fg), size=11)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"Remove the {tag.name} filter")
            btn.clicked.connect(lambda: self.remove_clicked.emit(self.tag))
            layout.addWidget(btn)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


class FilterChip(QFrame):
    """Search bar chip for one tag filter: a tag, several tags (any of them), or excluded tags.

    Clicking the chip switches it between "has" and "doesn't have"; the x removes it.
    """

    remove_clicked = Signal(object)  # TagFilter
    toggle_clicked = Signal(object)  # TagFilter

    def __init__(self, tag_filter: TagFilter, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.tag_filter = tag_filter
        bg, fg, border = chip_colors(tag_filter.tags[0])
        names = " or ".join(t.name for t in tag_filter.tags)
        if tag_filter.exclude:
            # Outline only, with the tag color as the border: reads as "not this"
            fg, style = "palette(text)", f"background-color: transparent; border: 1px dashed {bg if bg.startswith('#') else border};"
            text = f"not {names}"
        else:
            style = f"background-color: {bg}; border: 1px solid {border};"
            text = names
        self.setObjectName("tagChip")
        self.setStyleSheet(
            f"#tagChip {{ {style} border-radius: 9px; }}"
            f"#tagChip QLabel, #tagChip QToolButton {{ color: {fg}; background: transparent; border: none; padding: 0; }}"
        )
        if tag_filter.exclude:
            meaning = f"Files without {names}"
        elif len(tag_filter.tags) > 1:
            meaning = f"Files with any of: {', '.join(t.name for t in tag_filter.tags)}"
        else:
            meaning = f"Files tagged {names}"
        self.setToolTip(f"{meaning}\nClick to switch between has / doesn't have")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 1, 4, 1)
        layout.setSpacing(4)
        layout.addWidget(QLabel(text))
        btn = QToolButton()
        set_icon(btn, "close", _chip_tone(bg, fg), size=11)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("Remove this filter")
        btn.clicked.connect(lambda: self.remove_clicked.emit(self.tag_filter))
        layout.addWidget(btn)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.toggle_clicked.emit(self.tag_filter)
        super().mouseReleaseEvent(event)


class SimilarChip(QFrame):
    """Search bar chip for Find Similar: "Similar to skull.png", with an x to remove it."""

    remove_clicked = Signal()

    def __init__(self, label: str, parent: Optional[QWidget] = None, tooltip: str = "",
                 remove_tip: str = "Stop showing only look-alikes"):
        super().__init__(parent)
        bg, fg, border = "rgba(148, 163, 184, 0.16)", "palette(text)", "rgba(148, 163, 184, 0.6)"
        self.setObjectName("tagChip")
        self.setStyleSheet(
            f"#tagChip {{ background-color: {bg}; border: 1px solid {border}; border-radius: 9px; }}"
            f"#tagChip QLabel, #tagChip QToolButton {{ color: {fg}; background: transparent; border: none; padding: 0; }}"
        )
        self.setToolTip(tooltip or f"{label} and its look-alikes (resized or re-saved copies), closest first")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 1, 4, 1)
        layout.setSpacing(4)
        layout.addWidget(QLabel(label))
        btn = QToolButton()
        set_icon(btn, "close", _chip_tone(bg, fg), size=11)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(remove_tip)
        btn.clicked.connect(self.remove_clicked)
        layout.addWidget(btn)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


class FlowLayout(QLayout):
    """Lays widgets out left to right, wrapping onto new rows (Qt's classic flow layout example)."""

    def __init__(self, parent: Optional[QWidget] = None, spacing: int = 6):
        super().__init__(parent)
        self._items: list[QWidgetItem] = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect: QRect, apply: bool) -> int:
        x, y, row_height = rect.x(), rect.y(), 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and row_height > 0:
                x, y, row_height = rect.x(), y + row_height + spacing, 0
            if apply:
                item.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x += hint.width() + spacing
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y()


def tag_chip_cloud(tags: Sequence[Tag], parent: Optional[QWidget] = None) -> QWidget:
    """Read-only wrapping row of chips (empty-state text if there are none)."""
    box = QWidget(parent)
    flow = FlowLayout(box)
    for tag in tags:
        flow.addWidget(TagChip(tag))
    if not tags:
        empty = QLabel("No tags")
        empty.setObjectName("inspectorValue")
        flow.addWidget(empty)
    return box


# ----------------------------------------------------------------------
# Search bar
# ----------------------------------------------------------------------
class TagSearchBar(QFrame):
    """Search box with tag filter chips.

    - Typing filters by file name (plus filters like rating:>=3, see core.search_query).
    - Pick a suggested tag (autocomplete over tag names) or type a token and press
      Enter/Space to turn it into a chip:
        tag:name      files with the tag        (several chips must all match)
        -tag:name     files without it
        tag:a|b       files with any of them
      Clicking a chip switches it between "has" and "doesn't have".
    - Backspace in an empty box removes the last chip; the clear button (or Esc)
      removes everything. The star saves the search as a sidebar collection.
    - Find Similar shows as a chip too (similar:<id> in the saved text); similar_label names
      the picture. The palette button adds a color: filter (a named colour, or one picked).
    - The AI button (ai:on in the saved text) makes the words a description for AI Search (core.ai_search):
      files whose pictures match, best first, not file names. More Like This is a chip (like:<id>).
    """

    changed = Signal()  # filters or text changed
    unknown_tag = Signal(str)
    save_requested = Signal()
    ai_wanted = Signal()  # the AI button was turned on (the window downloads AI Search first if it must)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("tagSearchBar")
        self._filters: list[TagFilter] = []  # active chips
        self._all_tags: list[Tag] = []
        self._similar: Optional[int] = None  # Find Similar: the picture's database id
        self.similar_label: Callable[[int], str] = lambda asset_id: "Similar picture"
        self._like: Optional[int] = None  # More Like This: the file's database id
        self.like_label: Callable[[int], str] = lambda asset_id: "More like a file"
        self._ai = False  # AI Search: the words describe what's in the pictures
        self._color_popup = None  # (the colour picker, while it's open)
        self._picked_token = ""  # the color: token the picker put in the box

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        self._chip_row = QHBoxLayout()
        self._chip_row.setSpacing(4)
        layout.addLayout(self._chip_row)

        self.edit = QLineEdit()
        self.edit.setObjectName("tagSearchEdit")
        self.edit.setPlaceholderText("Search assets or tag:name...")
        self.edit.setToolTip(
            "Type to search file names.\n"
            "tag:name  has the tag (pick a suggestion, or type it and press Enter)\n"
            "-tag:name  doesn't have it      tag:a|b  has any of them\n"
            "is:fav, is:alpha, rating:>=3, res:>=2048, size:>10mb, dur:<30s, poly:<10k, ext:png\n"
            "color:red, color:#c04020 (pictures with that colour on 15% of them or more)\n"
            "A minus excludes (-ext:png, -is:fav); several words match in any order.\n"
            "Click a tag chip to switch has / doesn't have. Esc clears."
        )
        self.edit.setMinimumWidth(160)
        self._placeholder = self.edit.placeholderText()
        self.edit.textChanged.connect(self._on_text_changed)
        self.edit.returnPressed.connect(self._commit_tokens)
        self.edit.installEventFilter(self)
        layout.addWidget(self.edit, 1)

        self.btn_ai = QToolButton()
        set_icon(self.btn_ai, "ai", size=16)
        self.btn_ai.setCheckable(True)
        self.btn_ai.setToolTip(
            "AI Search: describe what you're looking for (\"creature skull side view\") and get the files whose\n"
            "pictures match, best first, not file names. Tags and filters still apply. Click again to turn it off."
        )
        self.btn_ai.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ai.setAutoRaise(True)
        self.btn_ai.clicked.connect(self._on_ai_clicked)
        layout.addWidget(self.btn_ai)

        self.btn_color = QToolButton()
        set_icon(self.btn_color, "palette", size=16)
        self.btn_color.setToolTip("Search by colour: pictures with a colour on 15% of them or more")
        self.btn_color.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_color.setAutoRaise(True)
        self.btn_color.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_color.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        self.btn_color.setMenu(self._color_menu())
        layout.addWidget(self.btn_color)

        self.btn_save = QToolButton()
        set_icon(self.btn_save, "star", size=16)
        self.btn_save.setToolTip("Save this search as a collection in the sidebar")
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save.setAutoRaise(True)
        self.btn_save.clicked.connect(self.save_requested)
        self.btn_save.hide()
        layout.addWidget(self.btn_save)

        self.btn_clear = QToolButton()
        set_icon(self.btn_clear, "close", "text", size=14)
        self.btn_clear.setToolTip("Clear search and tag filters (Esc)")
        self.btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear.setAutoRaise(True)
        self.btn_clear.clicked.connect(self.clear)
        self.btn_clear.hide()
        layout.addWidget(self.btn_clear)

        self._completer_model = QStringListModel(self)
        self.completer = QCompleter(self._completer_model, self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.setWidget(self.edit)
        self.completer.activated.connect(self._on_completion)

    # Public ------------------------------------------------------------
    def set_available_tags(self, tags: Sequence[Tag]) -> None:
        """Tags offered by autocomplete; chips of deleted tags are dropped, renamed ones updated."""
        self._all_tags = list(tags)
        self._completer_model.setStringList([t.name for t in self._all_tags])
        by_id = {t.id: t for t in self._all_tags}
        updated = []
        for f in self._filters:
            tags_now = tuple(by_id[t.id] for t in f.tags if t.id in by_id)
            if tags_now:
                updated.append(TagFilter(tags_now, f.exclude))

        def shown(filters: list[TagFilter]) -> list:  # file counts changing doesn't matter
            return [(f.exclude, [(t.id, t.name, t.color) for t in f.tags]) for f in filters]

        changed = shown(updated) != shown(self._filters)
        self._filters = updated
        if changed:
            self._rebuild_chips()
            self.changed.emit()

    def filters(self) -> list[TagFilter]:
        return list(self._filters)

    def tag_query(self) -> TagQuery:
        all_of, any_of, none_of = [], [], []
        for f in self._filters:
            ids = tuple(t.id for t in f.tags)
            if f.exclude:
                none_of.extend(ids)
            elif len(ids) == 1:
                all_of.extend(ids)
            else:
                any_of.append(ids)
        return TagQuery(tuple(all_of), tuple(any_of), tuple(none_of))

    def has_tag_filters(self) -> bool:
        return bool(self._filters)

    def similar_id(self) -> Optional[int]:
        """Find Similar's picture (database id), if it's on."""
        return self._similar

    def set_similar(self, asset_id: Optional[int]) -> None:
        """Show only a picture and its look-alikes (None: stop)."""
        if asset_id == self._similar:
            return
        self._similar = asset_id
        self._rebuild_chips()
        self.changed.emit()

    def like_id(self) -> Optional[int]:
        """More Like This: the file (database id), if it's on."""
        return self._like

    def set_like(self, asset_id: Optional[int]) -> None:
        """Show only a file and the files most like it (None: stop)."""
        if asset_id == self._like:
            return
        self._like = asset_id
        self._rebuild_chips()
        self.changed.emit()

    def ai_on(self) -> bool:
        """The words are a description for AI Search."""
        return self._ai

    def set_ai(self, on: bool) -> None:
        if on == self._ai:
            return
        self._ai = on
        self._show_ai()
        self._update_buttons()
        self.changed.emit()

    def _show_ai(self) -> None:
        self.btn_ai.setChecked(self._ai)
        self.edit.setPlaceholderText("Describe what you're looking for..." if self._ai else self._placeholder)

    def _on_ai_clicked(self, checked: bool) -> None:
        self.set_ai(checked)
        if checked:
            self.ai_wanted.emit()

    def add_color(self, word: str) -> None:
        """Add color:word to the search (replacing a colour the picker added)."""
        self._replace_picked(f"color:{word}")
        self._picked_token = ""

    def _replace_picked(self, token: str) -> None:
        words = [w for w in self.edit.text().split(" ") if w and w != self._picked_token and w != token]
        self.edit.setText(" ".join(words + [token]) + " ")
        self._picked_token = token

    def text(self) -> str:
        """Free-text part of the search (tag: tokens excluded)."""
        return parse_search_text(self.edit.text())[1]

    def query_text(self) -> str:
        """The whole search as text (chips as tokens), e.g. for saving it."""
        similar = [f"similar:{self._similar}"] if self._similar is not None else []
        like = [f"like:{self._like}"] if self._like is not None else []
        ai = ["ai:on"] if self._ai else []
        return " ".join(ai + similar + like + [filter_token(f) for f in self._filters] + ([self.text()] if self.text() else []))

    def set_query_text(self, text: str) -> None:
        """Replace the search with a saved one."""
        self._filters = []
        self._similar = None
        self._like = None
        self._ai = False
        self.edit.blockSignals(True)
        self.edit.setText(text)
        self.edit.blockSignals(False)
        self._commit_tokens()
        self._show_ai()
        self._rebuild_chips()
        self.changed.emit()

    def add_tag(self, tag: Tag, exclude: bool = False) -> None:
        self.add_filter(TagFilter((tag,), exclude))

    def add_filter(self, tag_filter: TagFilter) -> None:
        if tag_filter in self._filters:
            return
        self._filters.append(tag_filter)
        self._rebuild_chips()
        self.changed.emit()

    def remove_filter(self, tag_filter: TagFilter) -> None:
        self._filters = [f for f in self._filters if f != tag_filter]
        self._rebuild_chips()
        self.changed.emit()

    def remove_tag(self, tag: Tag) -> None:
        self._filters = [f for f in self._filters if all(t.id != tag.id for t in f.tags)]
        self._rebuild_chips()
        self.changed.emit()

    def toggle_filter(self, tag_filter: TagFilter) -> None:
        """Switch a chip between "has" and "doesn't have"."""
        self._filters = [TagFilter(f.tags, not f.exclude) if f == tag_filter else f for f in self._filters]
        self._rebuild_chips()
        self.changed.emit()

    def clear(self) -> None:
        had_anything = bool(self._filters or self.edit.text() or self._similar is not None or self._like is not None)
        self._filters = []
        self._similar = None
        self._like = None
        self._rebuild_chips()
        self.edit.blockSignals(True)
        self.edit.clear()
        self.edit.blockSignals(False)
        self._update_buttons()
        if had_anything:
            self.changed.emit()

    # Internals ---------------------------------------------------------
    def _find(self, name: str) -> Optional[Tag]:
        folded = " ".join(name.split()).casefold()
        return next((t for t in self._all_tags if t.name.casefold() == folded), None)

    def _rebuild_chips(self) -> None:
        while self._chip_row.count():
            widget = self._chip_row.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        if self._similar is not None:
            chip = SimilarChip(self.similar_label(self._similar))
            chip.remove_clicked.connect(self._remove_similar)
            self._chip_row.addWidget(chip)
        if self._like is not None:
            label = self.like_label(self._like)
            chip = SimilarChip(label, tooltip=f"{label}: files with the same kind of content (AI Search), closest first",
                               remove_tip="Stop showing only files like it")
            chip.remove_clicked.connect(self._remove_like)
            self._chip_row.addWidget(chip)
        for tag_filter in self._filters:
            chip = FilterChip(tag_filter)
            chip.remove_clicked.connect(self.remove_filter)
            chip.toggle_clicked.connect(self.toggle_filter)
            self._chip_row.addWidget(chip)
        self._update_buttons()

    def _remove_similar(self) -> None:
        self.set_similar(None)

    def _remove_like(self) -> None:
        self.set_like(None)

    def _update_buttons(self) -> None:
        active = bool(self._filters or self.edit.text().strip() or self._similar is not None or self._like is not None)
        self.btn_clear.setVisible(active)
        self.btn_save.setVisible(active)

    def _on_text_changed(self, text: str) -> None:
        self._update_buttons()
        # "tag:..." + Space completes a token as soon as it's typed
        if text.endswith(" ") and (_TAG_TOKEN.search(text) or _SIMILAR_TOKEN.search(text) or _LIKE_TOKEN.search(text)
                                   or _AI_TOKEN.search(text)):
            self._commit_tokens(report_unknown=False)
            return
        word = text.rsplit(" ", 1)[-1]
        if word.lower().startswith(("tag:", "-tag:")):
            # Just "tag:" lists every tag; more letters narrow it down (after | for "any of")
            typed = word.split(":", 1)[1].rsplit("|", 1)[-1]
            self.completer.setCompletionPrefix(typed.strip('"'))
            self.completer.complete()
        else:
            self.completer.popup().hide()
        self.changed.emit()

    def _commit_tokens(self, report_unknown: bool = True) -> None:
        like, ai = _LIKE_TOKEN.findall(self.edit.text()), _AI_TOKEN.search(self.edit.text())
        if like or ai:
            if like:
                self._like = int(like[-1])
            if ai:
                self._ai = True
                self._show_ai()
            self.edit.blockSignals(True)
            self.edit.setText(" ".join(_AI_TOKEN.sub(" ", _LIKE_TOKEN.sub(" ", self.edit.text())).split()))
            self.edit.blockSignals(False)
            self._rebuild_chips()
            self.changed.emit()
        similar = _SIMILAR_TOKEN.findall(self.edit.text())
        if similar:
            self._similar = int(similar[-1])
            self.edit.blockSignals(True)
            self.edit.setText(" ".join(_SIMILAR_TOKEN.sub(" ", self.edit.text()).split()))
            self.edit.blockSignals(False)
            self._rebuild_chips()
            self.changed.emit()
        tokens, rest = parse_search_text(self.edit.text())
        for token in tokens:
            tags = []
            for name in token.names:
                tag = self._find(name)
                if tag is None:
                    if report_unknown:
                        self.unknown_tag.emit(name)
                elif tag not in tags:
                    tags.append(tag)
            if tags:
                tag_filter = TagFilter(tuple(tags), token.exclude)
                if tag_filter not in self._filters:
                    self._filters.append(tag_filter)
        if tokens:
            self.edit.blockSignals(True)
            self.edit.setText(rest + (" " if rest else ""))
            self.edit.blockSignals(False)
            self._rebuild_chips()
            self.changed.emit()

    def _on_completion(self, name: str) -> None:
        if self._find(name) is None:
            return
        # Finish the half-typed token with the picked name, then turn it into a chip
        words = self.edit.text().split(" ")
        token = words[-1] if words and words[-1].lower().startswith(("tag:", "-tag:")) else "tag:"
        head = token[: max(token.rfind("|"), token.find(":")) + 1]
        words[-1:] = [head + _quote(name)]
        self.edit.blockSignals(True)
        self.edit.setText(" ".join(w for w in words if w))
        self.edit.blockSignals(False)
        self._commit_tokens()
        self._update_buttons()

    # Search by colour ----------------------------------------------------
    def _color_menu(self) -> QMenu:
        from core.picture_look import COLOR_NAMES

        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        for name in COLOR_NAMES:
            action = menu.addAction(color_swatch_icon(SEARCH_SWATCHES[name], 22, 14), name.capitalize())
            action.setToolTip(f"Pictures with {name} on 15% of them or more (color:{name}); normal maps are left out")
            action.triggered.connect(partial(self.add_color, name))
        menu.addSeparator()
        pick = menu.addAction("Pick a Colour...")
        pick.setToolTip("Pick any colour, or take one from the screen: pictures with colours close to it (color:#hex)")
        pick.triggered.connect(self.pick_color)
        return menu

    def pick_color(self) -> None:
        """The colour picker: the search follows the colour as it's picked."""
        from ui.color_popup import ColorPopup

        if self._color_popup is not None:
            self._color_popup.close()
        self._picked_token = ""
        self._color_popup = ColorPopup("Search by colour", QColor("#c04020"), self)
        self._color_popup.changed.connect(self._on_color_picked)
        self._color_popup.finished.connect(self._on_color_popup_closed)
        self._on_color_picked(QColor("#c04020"))
        self._color_popup.show_beside(QRect(self.mapToGlobal(QPoint(0, 0)), self.size()))

    def _on_color_picked(self, color: QColor) -> None:
        self._replace_picked(f"color:{color.name(QColor.NameFormat.HexRgb)}")

    def _on_color_popup_closed(self) -> None:
        self._color_popup = None
        self._picked_token = ""

    def eventFilter(self, obj, event) -> bool:
        if obj is self.edit and event.type() == event.Type.KeyPress:
            if shortcuts.matches("search.remove_last_chip", event) and not self.edit.text() and self._filters:
                self.remove_filter(self._filters[-1])
                return True
            if shortcuts.matches("search.remove_last_chip", event) and not self.edit.text() and self._like is not None:
                self.set_like(None)
                return True
            if shortcuts.matches("search.remove_last_chip", event) and not self.edit.text() and self._similar is not None:
                self.set_similar(None)
                return True
            if shortcuts.matches("search.clear", event) and not self.completer.popup().isVisible():
                self.clear()
                return True
        return super().eventFilter(obj, event)


# ----------------------------------------------------------------------
# Tagging dialog
# ----------------------------------------------------------------------
_TAG_ROLE = Qt.ItemDataRole.UserRole
_INITIAL_STATE_ROLE = Qt.ItemDataRole.UserRole + 1


class TagAssetsDialog(QDialog):
    """Add / remove custom tags on one or more files.

    Checkboxes are tri-state: checked = every selected file has the tag,
    partial = some do, unchecked = none. Only rows the user changes are
    applied, so an untouched partial row leaves each file as it was.
    suggested: tag names from the most similar files already tagged (core.ai_search.suggest_tags), shown as
    buttons that tick them.
    """

    def __init__(self, repo: TagRepository, asset_ids: Sequence[int], parent: Optional[QWidget] = None,
                 suggested: Sequence[str] = ()):
        super().__init__(parent)
        self.repo = repo
        self.asset_ids = list(asset_ids)
        self.suggested = list(suggested)
        self.changed = False  # True once tags were applied or created
        self.last_change = TagChange()  # what Apply did, for undo
        count = len(self.asset_ids)
        self.setWindowTitle("Tag File" if count == 1 else f"Tag {count} Files")
        self.setMinimumSize(380, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        hint = QLabel(
            "Tick a tag to add it to "
            + ("this file" if count == 1 else f"all {count} files")
            + ", untick to remove it."
            + ("" if count == 1 else " A partly filled box means only some of the files have it.")
        )
        hint.setObjectName("pageHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.list_tags = QListWidget()
        self.list_tags.itemChanged.connect(self._update_apply_button)
        layout.addWidget(self.list_tags, 1)

        self.suggestion_buttons: dict[str, QPushButton] = {}
        if self.suggested:
            row = QHBoxLayout()
            label = QLabel("Suggested:")
            label.setObjectName("pageHint")
            label.setToolTip("Tags of the most similar files you've tagged (AI Search)")
            row.addWidget(label)
            for name in self.suggested:
                button = QPushButton(name)
                set_icon(button, "ai", size=14)
                button.setToolTip(f"Tick {name}: the files most like {'this one' if count == 1 else 'these'} have it")
                button.clicked.connect(lambda _checked=False, n=name: self.tick_suggestion(n))
                row.addWidget(button)
                self.suggestion_buttons[name] = button
            row.addStretch(1)
            layout.addLayout(row)

        new_row = QHBoxLayout()
        self.txt_new = QLineEdit()
        self.txt_new.setPlaceholderText("New tag name")
        self.txt_new.setMaxLength(MAX_TAG_NAME_LENGTH)
        self.txt_new.returnPressed.connect(self._on_create)
        btn_create = QPushButton("+ Create")
        btn_create.setToolTip("Create the tag and tick it for these files (or press Enter)")
        btn_create.clicked.connect(self._on_create)
        new_row.addWidget(self.txt_new, 1)
        new_row.addWidget(btn_create)
        layout.addLayout(new_row)

        self._default_box = QVBoxLayout()
        layout.addLayout(self._default_box)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setToolTip("Close without changing any tags")
        btn_cancel.clicked.connect(self.reject)
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setToolTip("Apply the ticked and unticked tags to the files")
        self.btn_apply.setObjectName("primaryBtn")
        self.btn_apply.setDefault(False)
        self.btn_apply.setAutoDefault(False)
        self.btn_apply.clicked.connect(self._on_apply)
        buttons.addWidget(btn_cancel)
        buttons.addWidget(self.btn_apply)
        layout.addLayout(buttons)

        self._populate()

    def _populate(self) -> None:
        counts = self.repo.get_tags_for_assets(self.asset_ids)
        total = len(self.asset_ids)
        tags = self.repo.list_tags()
        self.list_tags.blockSignals(True)
        self.list_tags.clear()
        for tag in (t for t in tags if not t.is_default):
            n = counts.get(tag.id, 0)
            state = (
                Qt.CheckState.Checked if n == total
                else Qt.CheckState.PartiallyChecked if n
                else Qt.CheckState.Unchecked
            )
            self._add_row(tag, state)
        self.list_tags.blockSignals(False)
        if not self.list_tags.count():
            placeholder = QListWidgetItem("No custom tags yet. Create one below.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_tags.addItem(placeholder)

        # File type tags are read-only: just show them
        while self._default_box.count():
            widget = self._default_box.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        defaults = [t for t in tags if t.is_default and counts.get(t.id)]
        if defaults:
            label = QLabel("File type tags (automatic, can't be changed):")
            label.setObjectName("pageHint")
            self._default_box.addWidget(label)
            self._default_box.addWidget(tag_chip_cloud(defaults))
        self._update_apply_button()

    def tick_suggestion(self, name: str) -> None:
        """Tick a suggested tag (it's applied with Apply, like any other)."""
        for i in range(self.list_tags.count()):
            item = self.list_tags.item(i)
            tag = item.data(_TAG_ROLE)
            if tag is not None and tag.name.casefold() == name.casefold():
                item.setCheckState(Qt.CheckState.Checked)
                self.list_tags.scrollToItem(item)
                break
        button = self.suggestion_buttons.get(name)
        if button is not None:
            button.setEnabled(False)

    def _add_row(self, tag: Tag, state: Qt.CheckState) -> QListWidgetItem:
        item = QListWidgetItem(f"{tag.name}   ({tag.file_count} file{'s' if tag.file_count != 1 else ''})")
        item.setIcon(color_swatch_icon(tag.color or "#9ca3af", 14, 14))
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
        if state == Qt.CheckState.PartiallyChecked:
            flags |= Qt.ItemFlag.ItemIsUserTristate  # lets the user click back to "leave as is"
        item.setFlags(flags)
        item.setCheckState(state)
        item.setData(_TAG_ROLE, tag)
        item.setData(_INITIAL_STATE_ROLE, state)
        self.list_tags.addItem(item)
        return item

    def _changes(self) -> tuple[list[int], list[int]]:
        add, remove = [], []
        for i in range(self.list_tags.count()):
            item = self.list_tags.item(i)
            tag = item.data(_TAG_ROLE)
            if tag is None or item.checkState() == item.data(_INITIAL_STATE_ROLE):
                continue
            if item.checkState() == Qt.CheckState.Checked:
                add.append(tag.id)
            elif item.checkState() == Qt.CheckState.Unchecked:
                remove.append(tag.id)
        return add, remove

    def _update_apply_button(self, *_args) -> None:
        add, remove = self._changes()
        self.btn_apply.setEnabled(bool(add or remove))

    def _on_create(self) -> None:
        name = self.txt_new.text()
        try:
            tag = self.repo.create_custom_tag(name, next_tag_color(self.repo.list_tags(kind="custom")))
        except TagError as exc:
            QMessageBox.warning(self, "Can't Create Tag", str(exc))
            return
        self.changed = True
        self.txt_new.clear()
        if self.list_tags.count() == 1 and self.list_tags.item(0).data(_TAG_ROLE) is None:
            self.list_tags.clear()
        self.list_tags.blockSignals(True)
        item = self._add_row(tag, Qt.CheckState.Unchecked)
        item.setCheckState(Qt.CheckState.Checked)  # a tag made here is meant for these files
        self.list_tags.blockSignals(False)
        self.list_tags.scrollToItem(item)
        self._update_apply_button()

    def _on_apply(self) -> None:
        add, remove = self._changes()
        try:
            self.last_change = self.repo.apply_tag_changes(self.asset_ids, add, remove)
        except TagError as exc:
            QMessageBox.warning(self, "Can't Apply Tags", str(exc))
            return
        self.changed = True
        self.accept()
