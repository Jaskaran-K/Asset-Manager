"""A small undo / redo history for library edits (tags, favorites, ratings)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

MAX_STEPS = 50


@dataclass
class UndoStep:
    text: str  # e.g. "Tag 3 files"; shown as "Undo Tag 3 files"
    undo: Callable[[], None]
    redo: Callable[[], None]


class UndoHistory:
    def __init__(self, on_change: Optional[Callable[[], None]] = None):
        self._done: list[UndoStep] = []
        self._undone: list[UndoStep] = []
        self._on_change = on_change

    def push(self, step: UndoStep) -> None:
        """Record something that was just done (clears the redo list, like every editor)."""
        self._done.append(step)
        del self._done[:-MAX_STEPS]
        self._undone.clear()
        self._changed()

    def undo(self) -> Optional[UndoStep]:
        if not self._done:
            return None
        step = self._done.pop()
        step.undo()
        self._undone.append(step)
        self._changed()
        return step

    def redo(self) -> Optional[UndoStep]:
        if not self._undone:
            return None
        step = self._undone.pop()
        step.redo()
        self._done.append(step)
        self._changed()
        return step

    def next_undo(self) -> Optional[UndoStep]:
        return self._done[-1] if self._done else None

    def next_redo(self) -> Optional[UndoStep]:
        return self._undone[-1] if self._undone else None

    def clear(self) -> None:
        self._done.clear()
        self._undone.clear()
        self._changed()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()
