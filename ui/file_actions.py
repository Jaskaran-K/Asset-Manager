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
        # A list, not a shell string: odd characters in the name can't break the command
        subprocess.Popen(["explorer", f"/select,{os.path.normpath(str(path))}"])  # pylint: disable=consider-using-with
        return True
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


def open_file(path: Path) -> bool:
    """Open the file with the app Windows uses for it (False if it isn't there)."""
    path = Path(path)
    return path.exists() and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
