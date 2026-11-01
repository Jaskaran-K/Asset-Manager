"""Notes in the crash log when a view's scroll bar is freed while the view is still open.

Several crashes (the 0.5.x file viewer and Settings) were Qt laying out a scroll area whose
scroll bar had already been freed: something frees it, and the view crashes the next time it
lays itself out, with nothing in the log to say which view or what freed it. This watches
every scroll area's bars (from when it's first shown) and, when one goes while its view
stays, logs the view and the Python stack at that moment (core.crash_log), so the next crash
log says where to look. A bar going because its view is closing is normal and isn't logged.
"""

from __future__ import annotations

import traceback
from typing import Optional

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, Slot
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QWidget

from core import crash_log


def describe(widget: QWidget) -> str:
    """The widget and the ones it's in, up to its window: "QListWidget 'notesList' in PdfViewer ..."."""
    parts = []
    current: Optional[QWidget] = widget
    while current is not None and len(parts) < 12:
        name = current.objectName()
        parts.append(type(current).__name__ + (f" '{name}'" if name else ""))
        if current.isWindow():
            title = current.windowTitle()
            if title:
                parts[-1] += f' (window "{title}")'
            break
        current = current.parentWidget()
    return " in ".join(parts)


class _BarWatch(QObject):
    """Watches one scroll area's two bars (a child of the area: it goes with it)."""

    def __init__(self, area: QAbstractScrollArea):
        super().__init__(area)
        self.area = area
        self.reported = False
        for bar in (area.horizontalScrollBar(), area.verticalScrollBar()):
            bar.destroyed.connect(self._on_bar_destroyed)

    @Slot()
    def _on_bar_destroyed(self) -> None:
        area = getattr(self, "area", None)  # (None as Python shuts down: its objects go before Qt's)
        if area is None or self.reported or not shiboken6.isValid(area):
            return  # (the view is being freed)
        if None in area.children():
            return  # (the view is closing: Qt is freeing its children, the bars among them)
        self.reported = True
        crash_log.log_error(
            "A scroll bar was freed while its view is still open (Qt crashes the next time that view lays itself out):",
            f"View: {describe(area)}\nPython stack when it was freed:\n{''.join(traceback.format_stack()[:-1])}",
        )


class _Watcher(QObject):
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Polish and isinstance(obj, QAbstractScrollArea):
            if not obj.findChildren(_BarWatch, options=Qt.FindChildOption.FindDirectChildrenOnly):
                _BarWatch(obj)
        return False


_watcher: Optional[_Watcher] = None


def install(app: Optional[QApplication] = None) -> None:
    """Watch every scroll area's bars from now on (call once, after the app exists)."""
    global _watcher
    app = app or QApplication.instance()
    if app is None or _watcher is not None:
        return
    _watcher = _Watcher(app)
    app.installEventFilter(_watcher)
