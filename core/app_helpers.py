"""Importing a model into a Maya or Blender that's already open.

A program started from outside always opens a new window, so a window that's open has to be asked
from inside: a small helper script, installed once from Settings > Apps, starts with the app. It
writes a file saying it's there, and checks a folder for requests, each naming a model to import.
There's no network port, and a request can only ever import a model of the types listed: it runs no
commands. Maya has one helper for all its versions (its shared scripts folder); Blender one for each
version (its scripts/startup folder, which it runs without any preference being changed).

Only OBJ, FBX and glTF go this way. Scenes (.ma, .mb, .blend) still open in a new window, since
opening one in a window that's in use would replace what's there.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QStandardPaths

from . import video_tools

HELPER_FILE = "asset_manager_helper.py"
REQUEST_SUFFIX = ".request"
ALIVE_PREFIX = "alive-"
MAX_REQUEST_SECONDS = 120  # older ones are dropped: a window opened later doesn't import what's long forgotten

IMPORT_TYPES: dict[str, tuple[str, ...]] = {
    "maya": (".obj", ".fbx"),
    "blender": (".obj", ".fbx", ".glb", ".gltf"),
}
_PROGRAMS = {"maya": ("maya.exe",), "blender": ("blender.exe",)}  # (the running window, not Blender's launcher)
_BLOCK_START = "# Asset Manager's helper (Settings > Apps adds and removes this)"
_BLOCK_END = "# End of Asset Manager's helper"
_USER_SETUP_BLOCK = f"""{_BLOCK_START}
try:
    import asset_manager_helper
    asset_manager_helper.start()
except Exception as exc:
    print("Asset Manager's helper didn't start:", exc)
{_BLOCK_END}
"""

_COMMON = '''"""Asset Manager's helper: imports a model into this window when the Asset Manager asks.

Added by its Settings > Apps, which also removes it. It only reads the folder below, and only ever
imports model files. Safe to delete.
"""

import json
import os
import time

FOLDER = @FOLDER@
TYPES = @TYPES@
MAX_AGE = @MAX_AGE@


def announce():
    """Say this window is here (the Asset Manager checks that its process is still running)."""
    os.makedirs(FOLDER, exist_ok=True)
    with open(os.path.join(FOLDER, "@ALIVE@%d" % os.getpid()), "w"):
        pass


def has_requests():
    try:
        return any(name.endswith("@SUFFIX@") for name in os.listdir(FOLDER))
    except OSError:
        return False


def check():
    """Import what's been asked for. Renaming a request first means only one open window gets it."""
    try:
        names = sorted(os.listdir(FOLDER))
    except OSError:
        return
    for name in names:
        if not name.endswith("@SUFFIX@"):
            continue
        request = os.path.join(FOLDER, name)
        claimed = "%s.%d" % (request, os.getpid())
        try:
            os.replace(request, claimed)
        except OSError:
            continue
        try:
            age = time.time() - os.path.getmtime(claimed)
            with open(claimed, encoding="utf-8") as f:
                path = json.load(f)["file"]
        except (OSError, ValueError, KeyError, TypeError):
            path = None
        finally:
            try:
                os.remove(claimed)
            except OSError:
                pass
        if not isinstance(path, str) or age > MAX_AGE or not os.path.isfile(path):
            continue
        extension = os.path.splitext(path)[1].lower()
        if extension in TYPES:
            try:
                import_model(path, extension)
            except Exception as exc:
                print("Asset Manager's helper couldn't import %s: %s" % (path, exc))
'''

_MAYA = '''

import threading

_waiting = False


def import_model(path, extension):
    from maya import cmds

    plugin, kind = {".obj": ("objExport", "OBJ"), ".fbx": ("fbxmaya", "FBX")}[extension]
    cmds.loadPlugin(plugin, quiet=True)
    cmds.file(path, i=True, type=kind, ignoreVersion=True)


def _run_waiting():
    global _waiting
    _waiting = False
    check()


def _watch():
    """Only looks at the folder here: Maya's own thread is asked to act when there's something to do."""
    global _waiting
    import maya.utils

    while True:
        time.sleep(1)
        if not _waiting and has_requests():
            _waiting = True
            maya.utils.executeDeferred(_run_waiting)


def start():
    from maya import cmds

    if cmds.about(batch=True):
        return  # (a batch job or a render: not a window to import into)
    announce()
    threading.Thread(target=_watch, name="asset-manager-helper", daemon=True).start()
'''

_BLENDER = '''

import bpy


def import_model(path, extension):
    operator = {
        ".obj": bpy.ops.wm.obj_import, ".fbx": bpy.ops.import_scene.fbx,
        ".glb": bpy.ops.import_scene.gltf, ".gltf": bpy.ops.import_scene.gltf,
    }[extension]
    windows = bpy.context.window_manager.windows
    if windows:  # (a timer has no window of its own, and the importers want one)
        with bpy.context.temp_override(window=windows[0]):
            operator("EXEC_DEFAULT", True, filepath=path)  # (True: Ctrl+Z undoes it)
    else:
        operator("EXEC_DEFAULT", True, filepath=path)


def _tick():
    check()
    return 1.0


def register():
    if bpy.app.background:
        return  # (a batch job or a render: not a window to import into)
    announce()
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=1.0, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
'''


class HelperError(Exception):
    """A helper couldn't be added or removed (the message says why, for the user)."""


def mailbox(app_id: str) -> Path:
    """Where the helper of an app looks for requests."""
    return video_tools.CACHE_ROOT / "app-requests" / app_id


def helper_source(app_id: str) -> str:
    """The helper script for an app."""
    source = _COMMON + (_MAYA if app_id == "maya" else _BLENDER)
    for mark, value in (
        ("@FOLDER@", repr(str(mailbox(app_id)))), ("@TYPES@", repr(IMPORT_TYPES[app_id])),
        ("@MAX_AGE@", str(MAX_REQUEST_SECONDS)), ("@ALIVE@", ALIVE_PREFIX), ("@SUFFIX@", REQUEST_SUFFIX),
    ):
        source = source.replace(mark, value)
    return source


# ----------------------------------------------------------------------
# Asking an open window
# ----------------------------------------------------------------------
def _process_program(pid: int) -> Optional[str]:
    """The program a running process was started from (None: there's no such process)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # (PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        name = ctypes.create_unicode_buffer(size.value)
        return name.value if kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)) else None
    finally:
        kernel32.CloseHandle(handle)


def window_is_open(app_id: str) -> bool:
    """Whether a window of the app with its helper is running. A process id can be reused, so its program
    is checked too; a window that's gone leaves a file, removed here."""
    if app_id not in IMPORT_TYPES:
        return False
    folder = mailbox(app_id)
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    open_now = False
    for name in names:
        if not name.startswith(ALIVE_PREFIX):
            continue
        program = _process_program(int(name[len(ALIVE_PREFIX):])) if name[len(ALIVE_PREFIX):].isdigit() else None
        if program is not None and os.path.basename(program).lower() in _PROGRAMS[app_id]:
            open_now = True
        else:
            try:
                os.remove(folder / name)
            except OSError:
                pass
    return open_now


def goes_to_open_window(app_id: str, file: Path | str) -> bool:
    """Whether a file would be imported into an open window, rather than start a new one."""
    return os.path.splitext(str(file))[1].lower() in IMPORT_TYPES.get(app_id, ()) and window_is_open(app_id)


def send(app_id: str, file: Path | str) -> None:
    """Ask the open window to import a file (raises OSError). It's picked up within a second or so, or
    as soon as the app is free. Written whole, then given its name, so it's never read half written."""
    folder = mailbox(app_id)
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for entry in os.scandir(folder):
        if entry.name.endswith(REQUEST_SUFFIX) and now - entry.stat().st_mtime > MAX_REQUEST_SECONDS:
            try:
                os.remove(entry.path)
            except OSError:
                pass
    name = uuid.uuid4().hex
    partial = folder / f"{name}.partial"
    partial.write_text(json.dumps({"file": str(file)}), encoding="utf-8")
    os.replace(partial, folder / f"{name}{REQUEST_SUFFIX}")


# ----------------------------------------------------------------------
# Adding and removing the helpers
# ----------------------------------------------------------------------
def _maya_scripts() -> Path:
    """Maya's scripts folder for all its versions."""
    root = os.environ.get("MAYA_APP_DIR")
    if root:
        return Path(root) / "scripts"
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)) / "maya" / "scripts"


def _blender_startup(program: Optional[str]) -> Path:
    """The folder of scripts that the Blender at this path runs when it starts."""
    root = os.environ.get("BLENDER_USER_SCRIPTS")
    if root:
        return Path(root) / "startup"
    match = re.search(r"(\d+\.\d+)", os.path.basename(os.path.dirname(program or "")))
    if match is None or not os.environ.get("APPDATA"):
        raise HelperError("Couldn't tell which version of Blender this is, from the name of its folder.")
    return Path(os.environ["APPDATA"]) / "Blender Foundation" / "Blender" / match.group(1) / "scripts" / "startup"


def _helper_path(app_id: str, program: Optional[str]) -> Path:
    return (_maya_scripts() if app_id == "maya" else _blender_startup(program)) / HELPER_FILE


def is_installed(app_id: str, program: Optional[str]) -> bool:
    try:
        return _helper_path(app_id, program).is_file()
    except HelperError:
        return False


def install(app_id: str, program: Optional[str]) -> None:
    """Add the helper (raises HelperError). The app runs it the next time it starts."""
    try:
        path = _helper_path(app_id, program)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(helper_source(app_id), encoding="utf-8")
        if app_id == "maya":
            _add_to_user_setup(path.parent / "userSetup.py")
    except (OSError, UnicodeError) as exc:
        raise HelperError(f"The helper couldn't be added: {exc}") from exc


def remove(app_id: str, program: Optional[str]) -> None:
    """Remove the helper (raises HelperError). A window running it keeps it until it's closed."""
    try:
        path = _helper_path(app_id, program)
        if app_id == "maya":
            _remove_from_user_setup(path.parent / "userSetup.py")
        path.unlink(missing_ok=True)
    except (OSError, UnicodeError) as exc:
        raise HelperError(f"The helper couldn't be removed: {exc}") from exc


def _add_to_user_setup(path: Path) -> None:
    """Add our lines to Maya's startup script. Whatever's there is left as it is."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if _BLOCK_START in text:
        return
    if text and not text.endswith("\n"):
        text += "\n"
    _write_whole(path, text + _USER_SETUP_BLOCK)


def _remove_from_user_setup(path: Path) -> None:
    """Take out only our lines (a script that held nothing else goes too)."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    start, end = text.find(_BLOCK_START), text.find(_BLOCK_END)
    if start < 0 or end < start:
        return
    end += len(_BLOCK_END)
    if text[end:end + 1] == "\n":
        end += 1
    rest = text[:start] + text[end:]
    if rest.strip():
        _write_whole(path, rest)
    else:
        path.unlink()


def _write_whole(path: Path, text: str) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)
