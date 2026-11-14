"""The app's side of the browser extension (core.browser_link): hearing that a picture arrived, and setting the
extension up (Settings > Browser).

The helper the browser starts does the saving; the app only hears about it, on the link's own thread, and the
main window acts on the UI thread (BrowserLinkWorker.arrived, connected to its methods): pictures for the
Artbook go on the page that's open (take_inbox), and a picture saved to the library is mentioned in the status
line (folder watching shows it).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Signal

from core import artbook_store, browser_link
from core.settings import get_artbook_page

ICON_SIZES = (16, 32, 48, 128)
# The browsers' own pages for their extensions (opened by starting the browser with the address)
BROWSERS = (
    ("Chrome", "chrome.exe", "chrome://extensions/"),
    ("Opera GX", "opera.exe", "opera://extensions"),
)


class BrowserLinkWorker(QObject):
    """Listens for the browser helper (core.browser_link.AppLink); arrived is emitted on the link's thread."""

    arrived = Signal(dict)  # {"op": "artbook"} or {"op": "library", "path": ...}

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.link = browser_link.AppLink(self._on_message)
        self.running = False

    def start(self) -> bool:
        try:
            self.link.start()
        except OSError:
            return False  # (the extension still works: pictures wait for the next start)
        self.running = True
        return True

    def stop(self) -> None:
        if self.running:
            self.running = False
            self.link.stop()

    def _on_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """(The link's thread.) The reply names the Artbook page pictures go on."""
        if message.get("op") in ("artbook", "library"):
            self.arrived.emit(dict(message))
        return {"ok": True, "page": page_name(get_artbook_page())}


def page_name(page_id: str) -> str:
    try:
        pages = artbook_store.list_pages()
    except Exception:  # pylint: disable=broad-except
        return ""
    names = {p["id"]: p.get("name") or "Untitled" for p in pages}
    return names.get(page_id) or (next(iter(names.values())) if names else "")


def extension_icons() -> dict[int, bytes]:
    """The app's icon as the extension's PNGs (no image files are shipped for it: ui.icons draws the app's)."""
    from ui.icons import app_icon

    icon = app_icon()
    icons = {}
    for size in ICON_SIZES:
        pixmap = icon.pixmap(size, size)
        if pixmap.isNull():
            continue
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.toImage().save(buffer, "PNG")
        icons[size] = bytes(data.data())
    return icons


def set_up() -> Path:
    """Register the helper for Chrome and Opera GX and copy the extension: the folder to load."""
    return browser_link.set_up(extension_icons())


def refresh_if_needed() -> bool:
    """Set up again if it was set up by another copy of the app or an older version. Whether it was."""
    if browser_link.needs_refresh():
        set_up()
        return True
    return False


def browser_program(exe: str) -> Optional[str]:
    """Where a browser is installed (Windows' App Paths), if it is."""
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                value = str(winreg.QueryValueEx(key, "")[0]).strip('"')
        except OSError:
            continue
        if value and os.path.isfile(value):
            return value
    return None


def open_extensions_page(exe: str, address: str) -> bool:
    """Start the browser on its extensions page. False if it isn't installed."""
    program = browser_program(exe)
    if program is None:
        return False
    subprocess.Popen([program, address], close_fds=True)  # noqa: S603 (the browser's own program, a fixed address)
    return True
