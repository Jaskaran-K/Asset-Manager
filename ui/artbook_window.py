"""Separate View's Artbook window, and its options for working over other apps.

The Window button (WindowOptionsButton) in its toolbar, and the Window submenu of the board's
right-click menu (both a WindowMenu), offer:

- Always on Top: the references stay over Blender, Photoshop and so on.
- Opacity: see through the whole window (20-100%).
- Click-through: the window ignores the mouse, so clicks reach the app underneath, and it
  stays on top meanwhile. You can't click the window to turn it off again, so there are two
  ways out: a global hotkey (GlobalHotkey: works whichever app is in front) and a tray icon
  that shows while it's on. Turning it on first explains this (with "Don't show again").

Always on Top and Opacity are remembered; Click-through always starts off.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Callable, Optional

from PySide6.QtCore import QAbstractNativeEventFilter, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSystemTrayIcon,
    QWidget,
    QWidgetAction,
)

from core.settings import ARTBOOK_OPACITY_RANGE, get_artbook_window_state, set_artbook_window_state
from ui import shortcuts
from ui.duotone import set_icon
from ui.icons import app_icon

CLICK_THROUGH_SHORTCUT = "view.click_through"


def click_through_keys() -> str:
    return shortcuts.get(CLICK_THROUGH_SHORTCUT).keys


def show_click_through_tip(parent: QWidget, hotkey_works: bool) -> bool:
    """The heads-up before click-through goes on; returns whether "Don't show again" was ticked.

    (Tests replace this.)"""
    tray = "click the Asset Manager icon in the tray"
    how = f"press {click_through_keys()} (from any app) or {tray}" if hotkey_works else tray
    box = QMessageBox(parent)
    box.setWindowTitle("Click-through")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText("Clicks will go through the Artbook window to the app underneath.")
    box.setInformativeText(f"The window stays on top while it's on. To turn it off, {how}.")
    # Parented to the box, so Qt owns it: a parentless one is freed by Python as soon as this
    # line ends, and the box is left pointing at a deleted check box (a crash when clicked)
    dont_show = QCheckBox("Don't show again", box)
    box.setCheckBox(dont_show)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()
    ticked = dont_show.isChecked()
    box.deleteLater()  # a child of the window: don't keep one per showing
    return ticked


# ----------------------------------------------------------------------
# Global hotkey (Windows)
# ----------------------------------------------------------------------
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000


def hotkey_parts(sequence: QKeySequence) -> tuple[int, int]:
    """(RegisterHotKey modifiers, virtual key) for a sequence like Ctrl+Alt+T.

    Letters and digits only: their Qt key codes are their Windows virtual-key codes."""
    combo = sequence[0]
    modifiers = combo.keyboardModifiers()
    mods = MOD_NOREPEAT
    for qt_modifier, win_modifier in (
        (Qt.KeyboardModifier.ControlModifier, MOD_CONTROL),
        (Qt.KeyboardModifier.AltModifier, MOD_ALT),
        (Qt.KeyboardModifier.ShiftModifier, MOD_SHIFT),
        (Qt.KeyboardModifier.MetaModifier, MOD_WIN),
    ):
        if modifiers & qt_modifier:
            mods |= win_modifier
    key = combo.key().value
    if not (0x30 <= key <= 0x39 or 0x41 <= key <= 0x5A):
        raise ValueError(f"{sequence.toString()}: a global hotkey must end in a letter or digit")
    return mods, key


class GlobalHotkey(QAbstractNativeEventFilter):
    """A key combination that reaches the app whichever app is in front (Windows only).

    Registered only while it's needed (Separate View), since it takes the keys from every
    other app. callback runs on the UI thread, after the native event has been handled.
    """

    HOTKEY_ID = 0xA11

    def __init__(self, shortcut_id: str, callback: Callable[[], None]):
        super().__init__()
        self.shortcut_id = shortcut_id
        self.callback = callback
        self.registered = False

    def register(self) -> bool:
        """True if the hotkey works now (False: not Windows, or another app already has it)."""
        if self.registered:
            return True
        if sys.platform != "win32":
            return False
        mods, key = hotkey_parts(shortcuts.sequence(self.shortcut_id))
        if not ctypes.windll.user32.RegisterHotKey(None, self.HOTKEY_ID, mods, key):
            return False
        QApplication.instance().installNativeEventFilter(self)
        self.registered = True
        return True

    def unregister(self) -> None:
        if not self.registered:
            return
        self.registered = False
        ctypes.windll.user32.UnregisterHotKey(None, self.HOTKEY_ID)
        app = QApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(self)

    def nativeEventFilter(self, event_type, message):
        # message is a shiboken VoidPtr: it has no size, so it's falsy; only None means no message
        if self.registered and message is not None:
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                QTimer.singleShot(0, self.callback)  # not in the middle of native event handling
                return True, 0
        return False, 0


# ----------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------
class ArtbookWindow(QMainWindow):
    """Separate View's window for the Artbook panel."""

    closed_by_user = Signal()
    options_changed = Signal()  # on top / click-through changed (the Window button's menu follows)

    def __init__(self, parent_title: str):
        super().__init__()
        self.setWindowTitle(f"Artbook - {parent_title}")
        self.setWindowIcon(app_icon())
        self.resize(900, 700)
        self.quitting = False
        state = get_artbook_window_state()
        self.on_top: bool = state["on_top"]
        self.click_through = False
        self.hotkey_works = False  # set by ViewModes once it has tried to register the hotkey
        self._tray: Optional[QSystemTrayIcon] = None
        self.setWindowOpacity(state["opacity"] / 100)
        self._apply_flags()

    def closeEvent(self, event) -> None:
        self.set_click_through(False)
        if not self.quitting:
            self.closed_by_user.emit()
        super().closeEvent(event)

    # ------------------ options ------------------
    def opacity_percent(self) -> int:
        return round(self.windowOpacity() * 100)

    def set_opacity(self, percent: int, save: bool = True) -> None:
        lo, hi = ARTBOOK_OPACITY_RANGE
        percent = max(lo, min(hi, int(percent)))
        self.setWindowOpacity(percent / 100)
        if save:
            set_artbook_window_state(opacity=percent)

    def set_on_top(self, on_top: bool) -> None:
        self.on_top = on_top
        set_artbook_window_state(on_top=on_top)
        self._apply_flags()
        self.options_changed.emit()

    def set_click_through(self, on: bool) -> None:
        if on == self.click_through:
            return
        if on and get_artbook_window_state()["click_through_tip"]:
            # Before the window stops taking clicks (the notice must be clickable)
            if show_click_through_tip(self, self.hotkey_works):
                set_artbook_window_state(click_through_tip=False)
        self.click_through = on
        self._apply_flags()
        self._update_tray()
        self.options_changed.emit()

    def toggle_click_through(self) -> None:
        self.set_click_through(not self.click_through)

    def _apply_flags(self) -> None:
        # Click-through keeps the window on top: otherwise clicking the app below would bury it
        wanted = {
            Qt.WindowType.WindowStaysOnTopHint: self.on_top or self.click_through,
            Qt.WindowType.WindowTransparentForInput: self.click_through,
        }
        visible = self.isVisible()
        changed = False
        for flag, on in wanted.items():
            if bool(self.windowFlags() & flag) != on:
                self.setWindowFlag(flag, on)  # hides the window: shown again below
                changed = True
        if changed and visible:
            self.show()

    # ------------------ the way out of click-through ------------------
    def _update_tray(self) -> None:
        if not self.click_through:
            if self._tray is not None:
                self._tray.hide()
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self._tray is None:
            self._tray = QSystemTrayIcon(app_icon(), self)
            self._tray.activated.connect(self._on_tray_activated)
        how = f"Click here or press {click_through_keys()}" if self.hotkey_works else "Click here"
        self._tray.setToolTip(f"Artbook click-through is on. {how} to turn it off")
        self._tray.show()  # (no balloon: the heads-up already said how)

    def _on_tray_activated(self, _reason) -> None:
        self.set_click_through(False)
        self.activateWindow()


class WindowMenu(QMenu):
    """Always on Top, Opacity and Click-through for the separate Artbook window."""

    def __init__(self, window: ArtbookWindow, parent: Optional[QWidget] = None):
        super().__init__("Window", parent)
        self.window_ = window
        set_icon(self.menuAction(), "window")
        self.setToolTipsVisible(True)
        self.action_on_top = self.addAction("Always on Top")
        self.action_on_top.setCheckable(True)
        self.action_on_top.setToolTip("Keep this window over other apps")
        self.action_on_top.toggled.connect(self._on_top_toggled)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(36, 4, 14, 4)  # lined up with the items' text (after the tick column)
        row_layout.setSpacing(8)
        row_layout.addWidget(QLabel("Opacity"))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(*ARTBOOK_OPACITY_RANGE)
        self.slider.setMinimumWidth(140)
        self.slider.setToolTip("How see-through this window is")
        row_layout.addWidget(self.slider, 1)
        self.lbl_opacity = QLabel()
        self.lbl_opacity.setMinimumWidth(36)
        row_layout.addWidget(self.lbl_opacity)
        opacity_action = QWidgetAction(self)
        opacity_action.setDefaultWidget(row)
        self.addAction(opacity_action)
        self.slider.valueChanged.connect(self._on_opacity_changed)
        self.slider.sliderReleased.connect(self._on_opacity_released)

        self.addSeparator()
        self.action_click_through = self.addAction(f"Click-through ({click_through_keys()})")
        self.action_click_through.setCheckable(True)
        self.action_click_through.toggled.connect(self._on_click_through_toggled)

        window.options_changed.connect(self.sync)
        self.sync()

    def sync(self) -> None:
        """Show the window's current options."""
        window = self.window_
        for action, value in ((self.action_on_top, window.on_top), (self.action_click_through, window.click_through)):
            action.blockSignals(True)
            action.setChecked(value)
            action.blockSignals(False)
        self.slider.blockSignals(True)
        self.slider.setValue(window.opacity_percent())
        self.slider.blockSignals(False)
        self.lbl_opacity.setText(f"{window.opacity_percent()}%")
        how = f"press {click_through_keys()} (from any app) or click the tray icon" if window.hotkey_works else "click the tray icon"
        self.action_click_through.setToolTip(
            f"Clicks go through this window to the app underneath, and it stays on top. To turn it off, {how}"
        )

    def _on_top_toggled(self, checked: bool) -> None:
        self.window_.set_on_top(checked)

    def _on_opacity_changed(self, value: int) -> None:
        self.lbl_opacity.setText(f"{value}%")
        self.window_.set_opacity(value, save=not self.slider.isSliderDown())  # dragging: saved on release

    def _on_opacity_released(self) -> None:
        self.window_.set_opacity(self.slider.value())

    def _on_click_through_toggled(self, checked: bool) -> None:
        self.window_.set_click_through(checked)


class WindowOptionsButton(QPushButton):
    """The separate Artbook window's Window button (its menu: WindowMenu)."""

    def __init__(self, window: ArtbookWindow, parent: Optional[QWidget] = None):
        super().__init__("Window", parent)
        set_icon(self, "window")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Always on top, opacity and click-through for this window")
        self.window_menu = WindowMenu(window, self)
        self.setMenu(self.window_menu)

    def sync(self) -> None:
        self.window_menu.sync()
