"""Showing a file in Explorer and opening it with its usual app."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


def reveal(path: Path) -> bool:
    """Open the file's folder with the file selected (False if it isn't there)."""
    path = Path(path)
    if not path.exists():
        return False
    if sys.platform == "win32":
        target = os.path.normpath(str(path.resolve()))
        if _select_in_explorer(target):
            return True
        # A list, not a shell string: odd characters in the name (%, &, commas) can't break the command
        subprocess.Popen(["explorer", "/select,", target])  # pylint: disable=consider-using-with
        return True
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


def _select_in_explorer(path: str) -> bool:
    """Ask Windows itself to show the file selected in its folder (as Explorer's own "Open file
    location" does). Any name works, unlike explorer.exe's /select, which trips over commas."""
    try:
        import ctypes
        from ctypes import wintypes

        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
        shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        initialized = ole32.CoInitialize(None) >= 0  # (Qt's UI thread has COM already: this just counts up)
        try:
            item = shell32.ILCreateFromPathW(path)
            if not item:
                return False
            try:
                return shell32.SHOpenFolderAndSelectItems(item, 0, None, 0) == 0
            finally:
                shell32.ILFree(item)
        finally:
            if initialized:
                ole32.CoUninitialize()
    except (AttributeError, OSError):
        return False


def open_file(path: Path) -> bool:
    """Open the file with the app Windows uses for it (False if it isn't there)."""
    path = Path(path)
    return path.exists() and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
