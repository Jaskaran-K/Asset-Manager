"""App icons for the cards of files that have no picture: read from the app installed on this
computer (core.apps), when it's asked for. None ship with this app."""

from __future__ import annotations

import sys
import threading
from typing import Optional

from PySide6.QtGui import QImage

from . import apps

ICON_SIDE = 256

_lock = threading.Lock()
_icons: dict[str, Optional[QImage]] = {}  # program -> its icon (None: it has none)


def read_icon(program: str) -> Optional[QImage]:
    """A program's own icon, up to ICON_SIDE px (None if it has none, or off Windows). Safe off the UI thread."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    extract = user32.PrivateExtractIconsW
    extract.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.HICON),
                        ctypes.POINTER(wintypes.UINT), wintypes.UINT, wintypes.UINT]
    extract.restype = wintypes.UINT
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    handle, resource = wintypes.HICON(), wintypes.UINT()
    count = extract(program, 0, ICON_SIDE, ICON_SIDE, ctypes.byref(handle), ctypes.byref(resource), 1, 0)
    if count == 0 or count == 0xFFFFFFFF or not handle:
        return None
    try:
        image = QImage.fromHICON(handle.value)
    finally:
        user32.DestroyIcon(handle)
    return None if image.isNull() else image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def icon_for(extension: str) -> Optional[QImage]:
    """The icon that stands for a file type (core.apps.icon_program), or None. The first time for each
    program it's read from its file: call off the UI thread (known_icon doesn't read)."""
    program = apps.icon_program(extension)
    if program is None:
        return None
    with _lock:
        if program in _icons:
            return _icons[program]
    image = read_icon(program)
    with _lock:
        _icons[program] = image
    return image


def known_icon(extension: str) -> Optional[QImage]:
    """The icon if it's been read already (never touches the disk beyond checking the program is there)."""
    program = apps.icon_program(extension)
    with _lock:
        return _icons.get(program) if program is not None else None


def forget() -> None:
    """Read them again (the programs in use changed)."""
    with _lock:
        _icons.clear()
