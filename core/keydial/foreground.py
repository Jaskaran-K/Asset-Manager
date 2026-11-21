"""Which program is in front (for per-app profiles), and which programs are running.

Plain Windows calls through ctypes (no pywin32 / psutil). Elsewhere these return None / nothing.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD))
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    _kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    _kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))

_QUERY_LIMITED = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION (allowed even for apps run as administrator)
_SNAP_PROCESSES = 0x2
_INVALID_HANDLE = ctypes.c_void_p(-1).value if sys.platform == "win32" else -1


def foreground_pid() -> Optional[int]:
    """The process id of the focused window's program."""
    if sys.platform != "win32":
        return None
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


def exe_of(pid: int) -> Optional[str]:
    """Lower-case exe name of a process, e.g. "maya.exe"."""
    if sys.platform != "win32":
        return None
    handle = _kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return os.path.basename(buffer.value).lower() or None
    finally:
        _kernel32.CloseHandle(handle)


def get_foreground_exe() -> Optional[str]:
    """Lower-case exe name of the focused window's program, e.g. "krita.exe"."""
    try:
        pid = foreground_pid()
        return exe_of(pid) if pid else None
    except OSError:
        return None


def own_window_in_front() -> bool:
    """True while one of this program's windows has the focus."""
    try:
        return foreground_pid() == os.getpid()
    except OSError:
        return False


def running_exes() -> set[str]:
    """Lower-case exe names of every running program."""
    if sys.platform != "win32":
        return set()
    snap = _kernel32.CreateToolhelp32Snapshot(_SNAP_PROCESSES, 0)
    if not snap or snap == _INVALID_HANDLE:
        return set()
    names = set()
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return names


def window_exes() -> set[str]:
    """Lower-case exe names of the programs with a visible, titled window (for "Add a Program...")."""
    if sys.platform != "win32":
        return set()
    pids: set[int] = set()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _param):
        if _user32.IsWindowVisible(hwnd) and _user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.add(pid.value)
        return True

    _user32.EnumWindows(callback_type(visit), 0)
    return {exe for exe in (exe_of(pid) for pid in pids) if exe}
