"""Garbage is only collected on the UI thread (ui.main_thread_gc).

A collection on a worker thread can free Qt objects that belong to the UI thread, which
crashed the v0.4.0 release build (an access violation in the event loop during an autosave).
"""

from __future__ import annotations

import gc
import time

from ui import main_thread_gc
from ui.tag_widgets import run_job


def collections() -> int:
    return sum(generation["collections"] for generation in gc.get_stats())


def test_workers_never_collect_and_the_ui_thread_does(qapp):
    assert not gc.isenabled()  # installed by the qapp fixture, as main() does
    before = collections()
    done = []

    def make_garbage():
        for _ in range(50_000):  # far past every collection threshold
            a, b = [], []
            a.append(b)
            b.append(a)  # a cycle: only the collector can free it
        return True

    timer = main_thread_gc._collector.timer
    timer.stop()  # only the worker runs while we watch (on a slow machine the timer could fire)
    try:
        run_job(make_garbage, done.append)
        deadline = time.monotonic() + 10
        while not done and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
    finally:
        timer.start()
    assert done == [True]
    assert collections() == before  # nothing was collected on the worker

    main_thread_gc._collector.check()  # what the timer does, on this thread
    assert collections() > before
