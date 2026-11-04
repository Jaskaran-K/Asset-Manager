"""A scan's worker is freed on the UI thread, never on its own.

Freed on its own thread, Qt holds its signal lock while PySide asks for Python's lock (to look for
Python overrides as the object goes). The UI thread, holding Python's lock, wanting that signal lock
for any connect (creating a menu is enough) then waits forever: the app froze if a scan ended just
then. (Seen in a test about 1 run in 40, with native stacks from py-spy.)
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QEvent

from test_main_window import pump
from ui.library_state import LibraryState


def test_a_scans_worker_is_freed_on_the_ui_thread(qapp, tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    state = LibraryState()
    freed_on = []
    state.start_scan([tmp_path], [".png"])
    worker, thread = state.scan_worker, state.scan_thread
    worker.destroyed.connect(lambda: freed_on.append(threading.get_ident()))

    def done() -> bool:
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return bool(freed_on)

    assert pump(qapp, done)
    assert freed_on == [threading.get_ident()]
    assert thread.wait(5000)
    state.shutdown()
