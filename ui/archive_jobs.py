"""Unpacking files from archives in the background, with progress, for the viewer and the grid's
Extract actions. The work itself is core.archives; this only runs it off the UI thread."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QCoreApplication, QObject, Signal, Slot

from ui.tag_widgets import run_job

ProgressCallback = Callable[[int, int], None]
PROGRESS_INTERVAL = 0.1  # seconds between progress reports


class _ProgressRelay(QObject):
    """Takes progress reports on the worker thread and hands them to the UI thread (changed is
    connected to a method, so Qt queues it there). Parented to the application like the job relays
    in ui.tag_widgets, so it outlives the job; let go with finish()."""

    changed = Signal(object, object)  # bytes done, bytes in all (Python ints: past 2 GB)

    def __init__(self, on_progress: Optional[ProgressCallback]):
        super().__init__(QCoreApplication.instance())
        self._on_progress = on_progress
        self._last = 0.0
        self.changed.connect(self._deliver)

    def report(self, done: int, total: int) -> None:
        """Worker thread: at most every PROGRESS_INTERVAL, and always the last one."""
        now = time.monotonic()
        if done < total and now - self._last < PROGRESS_INTERVAL:
            return
        self._last = now
        self.changed.emit(done, total)

    @Slot(object, object)
    def _deliver(self, done: int, total: int) -> None:
        if self._on_progress is not None:
            self._on_progress(done, total)

    def finish(self) -> None:
        self._on_progress = None
        self.deleteLater()


class UnpackJob:
    """One background unpack. cancel() makes the work stop at its next piece (should_stop)."""

    def __init__(self) -> None:
        self._stop = threading.Event()

    def cancel(self) -> None:
        self._stop.set()

    @property
    def cancelled(self) -> bool:
        return self._stop.is_set()


def unpack_in_background(
    work: Callable[[ProgressCallback, Callable[[], bool]], Any],
    on_done: Callable[[Any], None],
    on_failed: Callable[[str], None],
    on_progress: Optional[ProgressCallback] = None,
    job: Optional[UnpackJob] = None,
) -> UnpackJob:
    """Run work(progress, should_stop) on a worker thread. on_progress(done, total), on_done(result)
    and on_failed(message) run on the UI thread; nothing is called after cancel() (on_failed gets
    "cancelled" only if work itself didn't stop first)."""
    job = job or UnpackJob()
    relay = _ProgressRelay(on_progress)

    def done(result: Any) -> None:
        relay.finish()
        if not job.cancelled:
            on_done(result)

    def failed(message: str) -> None:
        relay.finish()
        if not job.cancelled:
            on_failed(message)

    run_job(lambda: work(relay.report, job._stop.is_set), done, failed)  # pylint: disable=protected-access
    return job
