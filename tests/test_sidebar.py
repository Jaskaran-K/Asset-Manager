"""The slide-out sidebar (ui.sidebar): docked / floating, sliding, resizing, frosted glass."""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QTreeWidget

from core import settings
from ui.animations import set_motion_mode
from ui.glass import frosted
from ui.sidebar import SidebarHost


@pytest.fixture(autouse=True)
def motion_off():
    set_motion_mode("off")  # positions are checked right away; test_slides_with_motion turns it on
    yield
    set_motion_mode("on")


def pump(qapp, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


def make_host(qapp, **state):
    if state:
        settings.set_sidebar_state(**state)
    window = QMainWindow()
    content = QLabel("grid")
    host = SidebarHost(QTreeWidget(), content)
    window.setCentralWidget(host)
    window.resize(900, 500)
    window.show()
    qapp.processEvents()
    return window, host


def click(widget):
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    QApplication.sendEvent(widget, QMouseEvent(
        QEvent.Type.MouseButtonPress, pos, widget.mapToGlobal(pos),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    ))


def test_docked_sidebar_sits_beside_the_content(qapp):
    window, host = make_host(qapp, open=True, pinned=True, width=250)
    assert host.panel.geometry().x() == 0 and host.panel.width() == 250
    assert host.content.x() == 250 and host.content.width() == host.width() - 250
    host.set_open(False)
    assert not host.panel.isVisible()
    assert host.content.x() == 0 and host.content.width() == host.width()
    assert settings.get_sidebar_state()["open"] is False
    window.close()


def test_floating_sidebar_starts_closed(qapp):
    window, host = make_host(qapp, open=True, pinned=False)
    assert not host.is_open() and not host.panel.isVisible()  # it would cover the grid
    window.close()


def test_floating_sidebar_covers_the_content_and_closes_after_a_pick(qapp):
    window, host = make_host(qapp, open=True, pinned=False)
    host.set_open(True)
    assert host.content.x() == 0 and host.content.width() == host.width()  # under the glass
    assert host.shadow.isVisible()
    host.close_if_floating()
    assert not host.is_open()
    host.set_pinned(True)
    host.set_open(True)
    host.close_if_floating()  # docked: stays
    assert host.is_open()
    window.close()


def test_clicking_beside_the_floating_sidebar_closes_it(qapp):
    window, host = make_host(qapp, open=True, pinned=False)
    host.set_open(True)
    click(host.content)
    assert not host.is_open()
    host.set_open(True)
    host.set_pinned(True)
    click(host.content)  # docked: clicks go to the grid as usual
    assert host.is_open()
    window.close()


def test_resizing_is_clamped_and_saved(qapp):
    window, host = make_host(qapp, open=True, pinned=True)
    lo, hi = settings.SIDEBAR_WIDTH_RANGE
    host.set_sidebar_width(10_000)
    assert host.sidebar_width() == hi and host.content.x() == hi
    host.set_sidebar_width(5)
    assert host.sidebar_width() == lo
    assert settings.get_sidebar_state()["width"] == lo
    window.close()


def test_slides_with_motion(qapp):
    set_motion_mode("on")
    window, host = make_host(qapp, open=True, pinned=True, width=240)
    host.set_open(False)
    pump(qapp, 0.08)
    assert -240 < host.panel.x() < 0  # on its way out
    assert host.content.width() == host.width()  # laid out once for the end state, not every frame
    pump(qapp, 0.6)
    assert not host.panel.isVisible() and host.content.x() == 0
    window.close()


def test_glass_has_a_backdrop(qapp):
    window, host = make_host(qapp, open=True, pinned=True)
    assert host.panel.backdrop is not None  # from the start
    host.set_pinned(False)
    assert host.panel.backdrop.width() == host.sidebar_width()
    window.close()


def test_frosted_blurs_but_keeps_the_size(qapp):
    image = QImage(200, 100, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#000000"))
    painter = QPainter(image)
    painter.fillRect(100, 0, 100, 100, QColor("#ffffff"))
    painter.end()
    blurred = frosted(image, radius=7.0)
    assert blurred.size() == image.size()
    edge = blurred.pixelColor(100, 50).lightness()
    assert 40 < edge < 215  # the hard edge became a soft ramp
    assert blurred.pixelColor(2, 50).lightness() < 40 and blurred.pixelColor(197, 50).lightness() > 215  # edges not darkened


def test_state_setting_is_validated(isolated_user_data):
    assert settings.get_sidebar_state() == {"open": True, "pinned": True, "width": settings.DEFAULT_SIDEBAR_WIDTH}
    settings._update_config("sidebar", {"open": "yes", "pinned": 3, "width": 99999})
    state = settings.get_sidebar_state()
    assert state["open"] is True and state["pinned"] is True and state["width"] == settings.SIDEBAR_WIDTH_RANGE[1]
