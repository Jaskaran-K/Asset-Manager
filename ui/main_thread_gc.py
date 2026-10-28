"""Python's garbage collection, only ever on the UI thread.

Python's cycle collector runs on whichever thread happens to be allocating when it's due,
often a background job (decoding images, saving an Artbook page). If it then frees Qt
objects that belong to the UI thread (pixmaps, widgets, graphics items left in reference
cycles), Qt's state is corrupted and the app crashes a moment later, at random. This is
what crashed the v0.4.0 release build.

install() turns automatic collection off and collects on a timer on the UI thread instead,
generation by generation, as Python would (pyqtgraph does the same, for the same reason).
Reference counting, which frees almost everything, is unaffected.
"""

from __future__ import annotations

import gc
from typing import Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

INTERVAL_MS = 1000


class _Collector(QObject):
    def __init__(self, app: QApplication):
        super().__init__(app)
        self.thresholds = gc.get_threshold()
        self.timer = QTimer(self)
        self.timer.setInterval(INTERVAL_MS)
        self.timer.timeout.connect(self.check)
        self.timer.start()

    def check(self) -> None:
        """Collect what Python would have by now (youngest generation first)."""
        young, middle, old = gc.get_count()
        if young > self.thresholds[0]:
            gc.collect(0)
            if middle > self.thresholds[1]:
                gc.collect(1)
                if old > self.thresholds[2]:
                    gc.collect(2)


_collector: Optional[_Collector] = None


def install(app: Optional[QApplication] = None) -> None:
    """Collect garbage only on the UI thread from now on (call once, after the app exists)."""
    global _collector
    app = app or QApplication.instance()
    if app is None or _collector is not None:
        return
    gc.disable()
    _collector = _Collector(app)
