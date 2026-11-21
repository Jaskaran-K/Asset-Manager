"""Every web browser: the "@browsers" program in a profile's list stands for all of them.

They're the well-known browsers' programs, plus every browser Windows has registered on this PC
(HKLM / HKCU \\SOFTWARE\\Clients\\StartMenuInternet: the list Windows' "Default apps" picks a
browser from), so a browser not named here still counts once it's installed.
"""
from __future__ import annotations

import functools
import os
import shlex
import sys

TOKEN = "@browsers"

KNOWN = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe", "vivaldi.exe",
    "arc.exe", "iexplore.exe", "librewolf.exe", "waterfox.exe", "floorp.exe", "zen.exe", "thorium.exe",
    "chromium.exe", "whale.exe", "yandex.exe", "palemoon.exe", "seamonkey.exe", "torbrowser.exe",
    "duckduckgo.exe", "sidekick.exe", "maxthon.exe",
})
_CLIENTS = r"SOFTWARE\Clients\StartMenuInternet"


def _exe_from_command(command: str) -> str:
    """'"C:\\...\\chrome.exe" --flag' -> "chrome.exe"."""
    command = command.strip()
    if not command:
        return ""
    if command.startswith('"'):
        path = command[1:].split('"', 1)[0]
    else:
        try:
            path = shlex.split(command, posix=False)[0]
        except ValueError:
            path = command.split(" ", 1)[0]
    return os.path.basename(path).lower()


def registered() -> set[str]:
    """The browsers Windows has registered (their exe names)."""
    if sys.platform != "win32":
        return set()
    import winreg

    found: set[str] = set()
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                clients = winreg.OpenKey(root, _CLIENTS, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with clients:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(clients, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(clients, rf"{name}\shell\open\command", 0, winreg.KEY_READ | view) as key:
                            exe = _exe_from_command(str(winreg.QueryValueEx(key, "")[0]))
                    except OSError:
                        continue
                    if exe.endswith(".exe"):
                        found.add(exe)
    return found


@functools.lru_cache(maxsize=1)
def all_browsers() -> frozenset[str]:
    """Every browser's exe name (read once per run)."""
    try:
        return KNOWN | frozenset(registered())
    except Exception:  # pylint: disable=broad-except  (the known list is enough)
        return KNOWN
