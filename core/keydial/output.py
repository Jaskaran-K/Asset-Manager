"""Send keystrokes, mouse wheel and program launches to Windows (via pynput/SendInput).

Note: Windows won't let a normal process send input to a program running as
administrator. If a shortcut does nothing in one particular app, that app is
probably elevated; run the toolkit as admin too.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
from pynput.mouse import Controller as MouseController

from .keys import MODIFIERS, PYNPUT_KEYS, VK_KEYS

_VK_SHIFT_BIT = 0x100

if sys.platform == "win32":
    import ctypes

    _VkKeyScanW = ctypes.windll.user32.VkKeyScanW
    _VkKeyScanW.argtypes = (ctypes.c_wchar,)
    _VkKeyScanW.restype = ctypes.c_short
else:  # pragma: no cover - toolkit targets Windows
    _VkKeyScanW = None


# A tap's timing (seconds): between one key going down and the next, and how long the key is held.
# About 20 ms a combo in all: fast enough for quick taps, slow enough for every app to see it.
KEY_GAP = 0.004
KEY_HOLD = 0.012


class SystemOutput:
    def __init__(self, sleep=time.sleep) -> None:
        self._kb = KeyboardController()
        self._mouse = MouseController()
        self._sleep = sleep

    # -- key resolution ----------------------------------------------------
    def _resolve(self, combo: tuple[str, ...]) -> list:
        """Names -> pynput keys, modifiers first. Characters are sent by
        virtual-key code (not as Unicode) so app shortcuts recognise them;
        characters that need Shift (e.g. "{") get Shift added automatically."""
        mods: list[str] = [t for t in combo if t in MODIFIERS]
        keys = []
        for t in combo:
            if t in MODIFIERS:
                continue
            if t in PYNPUT_KEYS:
                keys.append(getattr(Key, PYNPUT_KEYS[t]))
            elif t in VK_KEYS:
                keys.append(KeyCode.from_vk(VK_KEYS[t]))
            else:  # single character
                res = _VkKeyScanW(t) if _VkKeyScanW else -1
                if res == -1:
                    keys.append(KeyCode.from_char(t))  # no key for it; Unicode fallback
                else:
                    keys.append(KeyCode.from_vk(res & 0xFF))
                    if res & _VK_SHIFT_BIT and "shift" not in mods:
                        mods.append("shift")
        return [getattr(Key, PYNPUT_KEYS[m]) for m in mods] + keys

    # -- output API (used by Engine) --------------------------------------
    def press_combo(self, combo: tuple[str, ...]) -> None:
        for k in self._resolve(combo):
            self._kb.press(k)

    def release_combo(self, combo: tuple[str, ...]) -> None:
        for k in reversed(self._resolve(combo)):
            self._kb.release(k)

    def tap_combo(self, combo: tuple[str, ...]) -> None:
        """Press and let go, as a hand would: each key goes down a moment after the one before and the
        key is held briefly. Sent all in the same instant, apps that read the keyboard's state as they
        handle a key (Maya, ZBrush, Photoshop, games...) could find Ctrl already up, or miss the key."""
        keys = self._resolve(combo)
        pressed = []
        try:
            for n, k in enumerate(keys):
                if n:
                    self._sleep(KEY_GAP)
                self._kb.press(k)
                pressed.append(k)
            self._sleep(KEY_HOLD)
        finally:
            for n, k in enumerate(reversed(pressed)):
                if n:
                    self._sleep(KEY_GAP)
                self._kb.release(k)

    def type_text(self, text: str) -> None:
        self._kb.type(text)

    def scroll(self, dx: int, dy: int, modifiers: tuple[str, ...] = ()) -> None:
        mods = self._resolve(modifiers) if modifiers else []
        try:
            for k in mods:
                self._kb.press(k)
            if mods:
                self._sleep(KEY_GAP)
            self._mouse.scroll(dx, dy)
            if mods:
                self._sleep(KEY_GAP)
        finally:
            for k in reversed(mods):
                self._kb.release(k)

    def launch(self, target: str, args: tuple[str, ...] = ()) -> None:
        target = os.path.expandvars(os.path.expanduser(target))
        if args:
            subprocess.Popen([target, *args], close_fds=True)
        elif sys.platform == "win32":
            os.startfile(target)  # works for exes, documents, folders and URLs
        else:  # pragma: no cover
            subprocess.Popen([target], close_fds=True)
