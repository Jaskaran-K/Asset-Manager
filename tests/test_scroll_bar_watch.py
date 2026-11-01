"""The note in the crash log when a view's scroll bar is freed while the view stays open
(ui.scroll_bar_watch)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QDialog, QListWidget, QScrollBar, QVBoxLayout

from core import crash_log
from ui import scroll_bar_watch


@pytest.fixture
def logged(qapp, monkeypatch):
    scroll_bar_watch.install(qapp)
    notes = []
    monkeypatch.setattr(crash_log, "log_error", lambda title, details="": notes.append((title, details)))
    return notes


def _dialog(qapp, title):
    dialog = QDialog()
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    view = QListWidget()
    view.setObjectName("notesList")
    layout.addWidget(view)
    dialog.show()
    qapp.processEvents()
    return dialog, view


def _free(qapp, dialog):
    dialog.close()
    dialog.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_closing_a_view_logs_nothing(qapp, logged):
    dialog, _view = _dialog(qapp, "Viewer")
    _free(qapp, dialog)
    assert logged == []


def test_a_bar_freed_under_an_open_view_is_logged_with_where_it_was_freed(qapp, logged):
    dialog, view = _dialog(qapp, "Viewer")
    view.setVerticalScrollBar(QScrollBar())  # (frees the old bar, the view staying open)
    ((title, details),) = logged
    assert "scroll bar was freed" in title
    assert "QListWidget 'notesList' in QDialog (window \"Viewer\")" in details
    assert "setVerticalScrollBar" in details  # (the Python line that freed it)
    view.setHorizontalScrollBar(QScrollBar())
    assert len(logged) == 1  # (once per view)
    _free(qapp, dialog)
    assert len(logged) == 1
