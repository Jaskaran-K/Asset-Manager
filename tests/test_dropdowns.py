"""Dropdowns (every QComboBox, through the theme's stylesheet): they open under the button rather than over it,
show at most 10 rows and scroll after that, and slide down into place (ui.animations)."""

from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QComboBox, QVBoxLayout, QWidget

import pytest

from ui import animations
from ui.themes import apply_theme


@pytest.fixture(autouse=True)
def no_motion():
    animations.set_motion_mode("off")  # (where it ends up: the slide would be part way)
    yield
    animations.set_motion_mode("on")


def open_combo(qapp, items: int, current: int = 0, top: int = 40):
    window = QWidget()
    window.resize(300, 700)
    window.move(100, 0)
    layout = QVBoxLayout(window)
    layout.setContentsMargins(20, top, 20, 20)
    combo = QComboBox(window)
    combo.addItems([f"Item {i}" for i in range(items)])
    combo.setCurrentIndex(current)
    layout.addWidget(combo)
    layout.addStretch(1)
    window.show()
    qapp.processEvents()
    combo.showPopup()
    qapp.processEvents()
    popup = combo.view().window()
    return window, combo, popup


def close(window, combo):
    combo.hidePopup()
    window.close()
    window.deleteLater()


def test_a_long_list_opens_under_the_button_and_scrolls(qapp):
    apply_theme("dark", qapp)
    animations.install_popup_animations(qapp)
    window, combo, popup = open_combo(qapp, 25, current=12)
    try:
        button_bottom = combo.mapToGlobal(QPoint(0, combo.height())).y()
        assert popup.geometry().top() >= button_bottom  # (under it, not over it around the chosen item)
        assert popup.width() >= combo.width()
        view = combo.view()
        assert combo.maxVisibleItems() == 10
        shown = view.viewport().height() // view.sizeHintForRow(0)
        assert 9 <= shown <= 10 and view.verticalScrollBar().isVisible()
    finally:
        close(window, combo)


def test_a_short_list_has_no_scroll_bar_and_opens_under_the_button_whatever_is_chosen(qapp):
    apply_theme("dark", qapp)
    window, combo, popup = open_combo(qapp, 5, current=4)
    try:
        assert popup.geometry().top() >= combo.mapToGlobal(QPoint(0, combo.height())).y()
        assert not combo.view().verticalScrollBar().isVisible()
    finally:
        close(window, combo)


def test_separators_show_as_a_line_and_font_boxes_keep_their_look(qapp):
    from PySide6.QtWidgets import QFontComboBox

    from ui.themes import theme_color

    apply_theme("dark", qapp)
    animations.install_popup_animations(qapp)
    window = QWidget()
    combo = QComboBox(window)
    combo.addItems(["Studio", "Soft", "kloppenheim_02_4k"])
    combo.insertSeparator(2)
    fonts = QFontComboBox(window)
    window.show()
    qapp.processEvents()
    try:
        assert isinstance(combo.view().itemDelegate(), animations.ComboRowDelegate)
        assert not isinstance(fonts.view().itemDelegate(), animations.ComboRowDelegate)
        combo.showPopup()
        qapp.processEvents()
        view = combo.view()
        rect = view.visualRect(combo.model().index(2, 0))
        assert rect.height() == animations.SEPARATOR_HEIGHT
        image = view.viewport().grab().toImage()
        line = image.pixelColor(rect.center().x(), rect.center().y())
        assert line == theme_color("border")
        combo.hidePopup()
    finally:
        window.close()
        window.deleteLater()


def test_with_no_room_below_it_opens_above(qapp):
    apply_theme("dark", qapp)
    screen = qapp.primaryScreen().availableGeometry()
    window, combo, popup = open_combo(qapp, 25, top=screen.height() - 120)
    try:
        assert popup.geometry().bottom() <= combo.mapToGlobal(QPoint(0, 0)).y()
    finally:
        close(window, combo)
