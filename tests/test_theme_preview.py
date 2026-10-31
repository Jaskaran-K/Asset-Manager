"""Settings > Appearance previews themes and textures live. The app is restyled once the click is
over (restyling from inside a button's click handling crashed Qt), and quick clicks apply once."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest

from ui import themes


@pytest.fixture
def dialog(qapp):
    from ui.settings_dialog import SettingsDialog

    themes.apply_theme("dark", qapp, "none")
    widget = SettingsDialog()
    widget.show_page("Appearance")
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    themes.apply_theme("dark", qapp, "none")


def click(button):
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)


def test_a_theme_applies_after_the_click_not_during_it(qapp, dialog):
    page = dialog.page_appearance
    applied = []
    real = themes.apply_theme

    def recording(*args, **kwargs):
        applied.append(args[0])
        real(*args, **kwargs)

    themes.apply_theme = recording
    try:
        click(page.theme_buttons["light"])
        assert applied == []  # not from inside the click...
        qapp.processEvents()
        assert applied == ["light"] and themes.current_theme_id() == "light"  # ...straight after it
    finally:
        themes.apply_theme = real


def test_quick_clicks_restyle_once_with_the_last_choice(qapp, dialog):
    page = dialog.page_appearance
    applied = []
    real = themes.apply_theme
    themes.apply_theme = lambda *args, **kwargs: (applied.append((args[0], kwargs.get("texture_id"))), real(*args, **kwargs))
    try:
        click(page.theme_buttons["light"])
        click(page.texture_buttons["grid"])
        click(page.theme_buttons["dark_purple"])
        qapp.processEvents()
        assert applied == [("dark_purple", "grid")]
    finally:
        themes.apply_theme = real


def test_switching_back_and_forth_many_times(qapp, dialog):
    page = dialog.page_appearance
    names = ["light", "dark_purple", "dark"]
    textures = list(page.texture_buttons)
    for n in range(9):
        click(page.theme_buttons[names[n % 3]])
        if n % 2:
            qapp.processEvents()
        click(page.texture_buttons[textures[n % len(textures)]])
        qapp.processEvents()
    assert themes.current_theme_id() == names[8 % 3]


def test_cancel_puts_the_saved_theme_back(qapp, dialog):
    click(dialog.page_appearance.theme_buttons["light"])
    qapp.processEvents()
    assert themes.current_theme_id() == "light"
    dialog.reject()
    qapp.processEvents()
    assert themes.current_theme_id() == "dark"  # (the saved one: nothing was saved)
