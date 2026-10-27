"""Tag UI: chips, the tag-aware search bar, and the bulk tagging dialog.

Data access goes through core.tag_repository; slow queries run off the UI
thread with run_in_background().
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
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
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from core.tag_repository import MAX_TAG_NAME_LENGTH, Tag, TagChange, TagError, TagRepository
from ui import shortcuts
from ui.duotone import Tone, set_icon
from ui.icons import MAIN_CATEGORY_COLORS, SUB_CATEGORY_COLORS, color_swatch_icon

# Colors handed out to new custom tags, in order
TAG_COLORS = list(dict.fromkeys(MAIN_CATEGORY_COLORS + SUB_CATEGORY_COLORS))

# Tag filter tokens:  tag:name   tag:"two words"   -tag:name (must not have)   tag:a|b|"c d" (any of)
_NAME = r'(?:"[^"]+"|[^\s|"]+)'
_TAG_TOKEN = re.compile(rf'(?<!\S)(-?)tag:({_NAME}(?:\|{_NAME})*)\|?(?!\S)', re.IGNORECASE)
_UNFINISHED_TOKEN = re.compile(r'(?i)(?:^|\s)-?tag:(?:"[^"]*|\S*)$')


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


def _quote(name: str) -> str:
    return f'"{name}"' if re.search(r'[\s|]', name) else name


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

    def __init__(self, task: "_DbTask", on_done: Callable[[Any], None], on_failed: Optional[Callable[[str], None]]):
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


_db_pool: Optional[QThreadPool] = None


_slow_pool: Optional[QThreadPool] = None


def wait_for_background(timeout_ms: int = 5000) -> bool:
    """Drop queued background jobs and wait for running ones (call before the app quits:
    Qt objects being torn down under a running job crash the process). True if all finished."""
    finished = True
    for pool in (_db_pool, _slow_pool):
        if pool is not None:
            pool.clear()
            finished = pool.waitForDone(timeout_ms) and finished
    return finished


def run_in_background(
    repo: TagRepository,
    fn: Callable[[TagRepository], Any],
    on_done: Callable[[Any], None],
    on_failed: Optional[Callable[[str], None]] = None,
    slow: bool = False,
) -> None:
    """Run fn(repo) on a worker thread (with its own connection); callbacks run on the UI thread.

    slow=True uses a separate single-thread pool, so long background jobs (reading file
    details) never hold up quick ones like tag searches.
    """
    global _db_pool, _slow_pool
    if _db_pool is None:
        _db_pool = QThreadPool()
        _db_pool.setMaxThreadCount(2)
        _slow_pool = QThreadPool()
        _slow_pool.setMaxThreadCount(1)
    task = _DbTask(repo, fn)
    _CallbackRelay(task, on_done, on_failed)  # owned by the application until it reports back
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
    """

    changed = Signal()  # filters or text changed
    unknown_tag = Signal(str)
    save_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("tagSearchBar")
        self._filters: list[TagFilter] = []  # active chips
        self._all_tags: list[Tag] = []

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
            "is:fav, rating:>=3, res:>=2048, size:>10mb, dur:<30s, poly:<10k, ext:png\n"
            "Click a tag chip to switch has / doesn't have. Esc clears."
        )
        self.edit.setMinimumWidth(160)
        self.edit.textChanged.connect(self._on_text_changed)
        self.edit.returnPressed.connect(self._commit_tokens)
        self.edit.installEventFilter(self)
        layout.addWidget(self.edit, 1)

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

    def text(self) -> str:
        """Free-text part of the search (tag: tokens excluded)."""
        return parse_search_text(self.edit.text())[1]

    def query_text(self) -> str:
        """The whole search as text (chips as tokens), e.g. for saving it."""
        return " ".join([filter_token(f) for f in self._filters] + ([self.text()] if self.text() else []))

    def set_query_text(self, text: str) -> None:
        """Replace the search with a saved one."""
        self._filters = []
        self.edit.blockSignals(True)
        self.edit.setText(text)
        self.edit.blockSignals(False)
        self._commit_tokens()
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
        had_anything = bool(self._filters or self.edit.text())
        self._filters = []
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
        for tag_filter in self._filters:
            chip = FilterChip(tag_filter)
            chip.remove_clicked.connect(self.remove_filter)
            chip.toggle_clicked.connect(self.toggle_filter)
            self._chip_row.addWidget(chip)
        self._update_buttons()

    def _update_buttons(self) -> None:
        active = bool(self._filters or self.edit.text().strip())
        self.btn_clear.setVisible(active)
        self.btn_save.setVisible(active)

    def _on_text_changed(self, text: str) -> None:
        self._update_buttons()
        # "tag:..." + Space completes a token as soon as it's typed
        if text.endswith(" ") and _TAG_TOKEN.search(text):
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

    def eventFilter(self, obj, event) -> bool:
        if obj is self.edit and event.type() == event.Type.KeyPress:
            if shortcuts.matches("search.remove_last_chip", event) and not self.edit.text() and self._filters:
                self.remove_filter(self._filters[-1])
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
    """

    def __init__(self, repo: TagRepository, asset_ids: Sequence[int], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.repo = repo
        self.asset_ids = list(asset_ids)
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
