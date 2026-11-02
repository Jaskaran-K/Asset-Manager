"""Title bars in the theme's colours (Windows).

Windows draws each window's title bar itself, dark or light as set in Windows, whatever the app's
theme (a dark bar over Light Mode). Each window is told the theme's colours as it's shown, and
again when the theme changes: dark or light to match, and on Windows 11 the theme's top-bar
colour and title text colour too, so the title bar and the toolbar under it read as one. Windows
10 has no caption colour: there it's only dark or light.
"""

from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_CAPTION_COLOR = 35  # (Windows 11)
DWMWA_TEXT_COLOR = 36  # (Windows 11)


def title_bar_colors() -> tuple[QColor, QColor]:
    """(the bar, its text): the theme's top-bar (surface) and strong text colours, made solid."""
    from ui.themes import theme_color

    bar, text = theme_color("surface"), theme_color("text_strong")
    if not bar.isValid():
        bar = theme_color("window_bg")
    return QColor(bar.red(), bar.green(), bar.blue()), QColor(text.red(), text.green(), text.blue())


def style_title_bar(window: QWidget) -> None:
    """Give a shown window's title bar the theme's colours (nothing for windows without one)."""
    if not _native() or not _has_title_bar(window):
        return
    bar, text = title_bar_colors()
    hwnd = int(window.winId())
    _set_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if bar.lightnessF() < 0.5 else 0)
    _set_attribute(hwnd, DWMWA_CAPTION_COLOR, _colorref(bar))
    _set_attribute(hwnd, DWMWA_TEXT_COLOR, _colorref(text))


def restyle_all() -> None:
    """After a theme change: every open window's title bar."""
    for widget in QApplication.topLevelWidgets():
        if widget.isVisible():
            style_title_bar(widget)


def _has_title_bar(window: QWidget) -> bool:
    if not window.isWindow() or window.windowFlags() & Qt.WindowType.FramelessWindowHint:
        return False
    return window.windowType() in (Qt.WindowType.Window, Qt.WindowType.Dialog)


def _native() -> bool:
    return sys.platform == "win32" and QGuiApplication.platformName() == "windows"


def _colorref(color: QColor) -> int:
    return color.red() | (color.green() << 8) | (color.blue() << 16)  # (0x00BBGGRR)


def _set_attribute(hwnd: int, attribute: int, value: int) -> None:
    import ctypes
    from ctypes import wintypes

    data = wintypes.DWORD(value)
    # An HRESULT error (Windows 10 has no caption colour) leaves that part as Windows draws it
    ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), wintypes.DWORD(attribute), ctypes.byref(data), ctypes.sizeof(data))


class _TitleBars(QObject):
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            style_title_bar(obj)
        return False


_title_bars: Optional[_TitleBars] = None


def install(app: Optional[QApplication] = None) -> None:
    """Style every window's title bar as it's shown (call once)."""
    global _title_bars
    app = app or QApplication.instance()
    if app is None or _title_bars is not None:
        return
    _title_bars = _TitleBars(app)
    app.installEventFilter(_title_bars)
