"""UI animations, and the Animations setting (off = reduce motion)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QEnterEvent
from PySide6.QtWidgets import QApplication, QMenu

from ui import animations
from ui.animations import AnimatedButton, PlayPauseButton, set_motion_mode


@pytest.fixture(autouse=True)
def restore_motion():
    yield
    set_motion_mode("on")


def hover(button):
    QApplication.sendEvent(button, QEnterEvent(QPointF(2, 2), QPointF(2, 2), QPointF(2, 2)))


def test_motion_on_animates(qapp):
    set_motion_mode("on")
    button = AnimatedButton("x")
    button.show()
    hover(button)
    assert 0.0 <= button.hoverLevel < 1.0  # fading in, not there yet
    play = PlayPauseButton()
    play.show()
    play.set_playing(True)
    assert play.morph < 1.0


def test_motion_off_jumps_to_the_end(qapp):
    set_motion_mode("off")
    button = AnimatedButton("x")
    button.show()
    hover(button)
    assert button.hoverLevel == 1.0
    play = PlayPauseButton()
    play.show()
    play.set_playing(True)
    assert play.morph == 1.0
    animations.install_popup_animations(qapp)
    menu = QMenu()
    menu.addAction("Item")
    menu.popup(button.mapToGlobal(button.rect().center()))
    qapp.processEvents()
    assert menu.windowOpacity() == 1.0
    menu.close()


def test_auto_follows_windows(monkeypatch):
    monkeypatch.setattr(animations, "windows_animations_enabled", lambda: False)
    set_motion_mode("auto")
    assert not animations.motion_enabled()
    monkeypatch.setattr(animations, "windows_animations_enabled", lambda: True)
    set_motion_mode("auto")
    assert animations.motion_enabled()
