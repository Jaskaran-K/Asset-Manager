"""Settings > Guide: the Artbook guide and the Read Me inside the app, linked to each other."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QUrl


@pytest.fixture
def dialog(qapp):
    from ui.settings_dialog import SettingsDialog

    widget = SettingsDialog()
    widget.resize(900, 600)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_both_guides_are_bundled_with_the_app():
    from ui.settings_dialog import DOCS_ROOT, GUIDES

    spec = (Path(__file__).resolve().parent.parent / "AssetManager.spec").read_text(encoding="utf-8")
    for file in GUIDES.values():
        assert (DOCS_ROOT / file).is_file()
        assert f"'{file}'" in spec  # (in the build, so the installed app has them)


def test_the_guides_show_in_settings(qapp, dialog):
    dialog.show_page("Artbook Guide")
    guide = dialog.guide_pages["Artbook Guide"]
    assert dialog.stack.currentWidget() is guide
    assert "Open the Artbook" in guide.text.toPlainText()
    assert "Asset-Manager" in dialog.guide_pages["Read Me"].text.toPlainText()


def test_links_between_the_guides_open_here(qapp, dialog, monkeypatch):
    from ui import settings_dialog

    opened = []
    monkeypatch.setattr(settings_dialog.QDesktopServices, "openUrl", lambda url: opened.append(url))
    guide, readme = dialog.guide_pages["Artbook Guide"], dialog.guide_pages["Read Me"]
    dialog.show_page("Artbook Guide")
    guide.text.anchorClicked.emit(QUrl("../README.md#the-artbook"))
    qapp.processEvents()
    assert dialog.stack.currentWidget() is readme
    assert readme.text.verticalScrollBar().value() > 0  # (at The Artbook, not the top)
    readme.text.anchorClicked.emit(QUrl("docs/artbook-guide.md"))
    assert dialog.stack.currentWidget() is guide
    guide.text.anchorClicked.emit(QUrl("https://example.com/"))
    assert opened == [QUrl("https://example.com/")]  # (web links: the browser)
