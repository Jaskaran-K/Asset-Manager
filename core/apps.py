"""The apps files open in: finding them on this computer, and starting them with a file.

Apps are often installed outside Program Files and in several versions, so they're looked for in
what Windows itself records (file types, App Paths, installed programs) and in the shortcuts of the
Start Menu and the Desktop, as well as the usual folders. detect() does the looking, off the UI
thread; the results are kept in the settings (found_apps) and given to configure().

Each app is started with a list of arguments, never through a shell, so no file name can change
what's run. Blender and Maya can't open an OBJ or FBX by being given it: they get a short command
that imports it.
"""

from __future__ import annotations

import glob
import os
import re
import struct
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Iterator, Optional

from . import app_helpers
from .scanner import determine_asset_group
from .settings import get_app_choices, get_app_types, get_custom_apps, get_found_apps

_IMAGES = frozenset({".psd", ".psb", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".tga", ".bmp", ".exr", ".hdr", ".webp"})


class AppError(Exception):
    """An app couldn't be started (the message says why, for the user)."""


@dataclass(frozen=True)
class App:
    id: str
    name: str
    exe_names: tuple[str, ...]  # (lower case; the first is preferred if a folder has several)
    folders: tuple[str, ...]  # where it usually is, under Program Files
    opens: frozenset[str]  # the file types Open In offers it for
    owns: frozenset[str] = frozenset()  # the file types whose cards show its icon
    path: str = ""  # an app added by hand (Settings > Apps): its program


APPS: tuple[App, ...] = (  # (the order of the Open In menu: the likeliest first for each type)
    App("blender", "Blender", ("blender-launcher.exe", "blender.exe"), ("Blender Foundation/Blender*",),
        frozenset({".blend", ".obj", ".fbx", ".glb", ".gltf"}), frozenset({".blend"})),
    App("maya", "Maya", ("maya.exe",), ("Autodesk/Maya*/bin",),
        frozenset({".ma", ".mb", ".obj", ".fbx"}), frozenset({".ma", ".mb"})),
    App("photoshop", "Photoshop", ("photoshop.exe",), ("Adobe/Adobe Photoshop*",), _IMAGES),
    App("zbrush", "ZBrush", ("zbrush.exe",), ("Maxon ZBrush*", "Pixologic/ZBrush*"),
        # (Not images: given a PSD or JPEG, ZBrush starts without loading it. An OBJ loads, in the
        # window already open if there is one. Tried on 2024.0.1.)
        frozenset({".zbp", ".ztl", ".zpr", ".zmt", ".obj"}), frozenset({".zbp", ".ztl", ".zpr", ".zmt"})),
    App("painter", "Substance 3D Painter", ("adobe substance 3d painter.exe",), ("Adobe/Adobe Substance 3D Painter",),
        frozenset({".spp", ".sbsar", ".spsm"}), frozenset({".spp", ".sbsar", ".spsm"})),
    App("toolbag", "Marmoset Toolbag", ("toolbag.exe",), ("Marmoset/Toolbag*",),
        frozenset({".tbscene"}), frozenset({".tbscene"})),
)
_BY_EXE = {name: app for app in APPS for name in app.exe_names}

_BLENDER_IMPORTERS = {
    ".obj": "wm.obj_import", ".fbx": "import_scene.fbx", ".glb": "import_scene.gltf", ".gltf": "import_scene.gltf",
}
# The file's path comes after "--", where Blender leaves it alone: no name can break the command
_BLENDER_COMMAND = "import bpy, sys; bpy.ops.{}(filepath=sys.argv[sys.argv.index('--') + 1])"
_MAYA_IMPORTERS = {".obj": ("objExport", "OBJ"), ".fbx": ("fbxmaya", "FBX")}

# What was found (settings found_apps), and what was picked (settings app_choices)
_found: dict[str, list[str]] = {}
_chosen: dict[str, str] = {}
_all_apps: tuple[App, ...] = APPS  # with the file types set by hand, and the apps added by hand
_icon_programs: dict[str, Optional[str]] = {}  # file type -> icon_program (asked for on every grid paint)


def configure(
    found: dict[str, list[str]], chosen: dict[str, str], custom: Iterable[dict] = (), types: Optional[dict[str, list[str]]] = None,
) -> None:
    """Which programs to use (call at startup, and when any of it changes). custom: apps added by hand
    ({"id", "name", "program", "types"}); types: the file types set by hand for the others."""
    global _found, _chosen, _all_apps
    _found, _chosen = dict(found), dict(chosen)
    types = types or {}
    _all_apps = tuple(replace(app, opens=frozenset(types[app.id])) if types.get(app.id) else app for app in APPS) + tuple(
        App(c["id"], c["name"], (), (), frozenset(c["types"]), path=c["program"]) for c in custom
    )
    _icon_programs.clear()


def load_settings() -> None:
    """configure, from what's saved."""
    configure(get_found_apps(), get_app_choices(), get_custom_apps(), get_app_types())


def all_apps() -> tuple[App, ...]:
    return _all_apps


def custom_types() -> set[str]:
    """The file types of the apps added by hand."""
    return {extension for app in _all_apps if app.path for extension in app.opens}


def program(app_id: str) -> Optional[str]:
    """The program to start for an app: the one picked, else the newest found (None: neither is there)."""
    added = next((app.path for app in _all_apps if app.id == app_id and app.path), None)
    candidates = [added] if added else ([_chosen[app_id]] if app_id in _chosen else []) + _found.get(app_id, [])
    return next((path for path in candidates if os.path.isfile(path)), None)


def apps_for(extension: str) -> list[App]:
    """The apps on this computer that fit a file type, for Open In."""
    extension = extension.lower()
    return [app for app in _all_apps if extension in app.opens and program(app.id) is not None]


def icon_program(extension: str) -> Optional[str]:
    """The program whose icon stands for a file type on its card: the app that owns the type, or else
    the one Windows opens it with. PureRef boards never (the rule: nothing is taken from PureRef.exe)."""
    extension = extension.lower()
    if extension not in _icon_programs:
        _icon_programs[extension] = _find_icon_program(extension)
    return _icon_programs[extension]


def _find_icon_program(extension: str) -> Optional[str]:
    if extension == ".pur":
        return None
    for app in _all_apps:
        if extension in app.owns and (path := program(app.id)) is not None:
            return path
    for app in _all_apps:  # (a type the library has no viewer for, opened by an app added by hand)
        if app.path and extension in app.opens and not is_viewable_type(extension) and (path := program(app.id)) is not None:
            return path
    return _associated_program(extension)


def is_viewable_type(extension: str) -> bool:
    return determine_asset_group(extension) not in ("App Files", "Other")


def command(app: App, exe: str, file: Path | str) -> list[str]:
    """The arguments that start an app with a file."""
    file = str(file)
    extension = os.path.splitext(file)[1].lower()
    if app.id == "blender" and extension in _BLENDER_IMPORTERS:
        return [exe, "--python-expr", _BLENDER_COMMAND.format(_BLENDER_IMPORTERS[extension]), "--", file]
    if app.id == "maya":
        if extension in _MAYA_IMPORTERS:
            plugin, kind = _MAYA_IMPORTERS[extension]
            quoted = file.replace("\\", "/").replace('"', "")  # (MEL: forward slashes; Windows names have no ")
            return [exe, "-command", f'loadPlugin -quiet "{plugin}"; file -import -type "{kind}" "{quoted}";']
        return [exe, "-file", file]
    return [exe, file]


def _child_environment() -> dict[str, str]:
    """The environment for another program: without what this app sets for itself. Maya, Painter and
    Toolbag are Qt apps too, and would load this app's Qt plugins through these."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("QT_", "QML", "PYTHON", "_PYI", "PYI_"))}
    if getattr(sys, "frozen", False):
        own = [os.path.normcase(str(Path(sys.executable).parent))]
        if hasattr(sys, "_MEIPASS"):
            own.append(os.path.normcase(sys._MEIPASS))  # pylint: disable=protected-access
        env["PATH"] = os.pathsep.join(
            p for p in env.get("PATH", "").split(os.pathsep) if not os.path.normcase(p).startswith(tuple(own))
        )
    return env


def launch(app: App, file: Path | str) -> bool:
    """Open a file in an app. A model goes into a window that's open, if the app has its helper
    (core.app_helpers): True. Otherwise the app is started with the file, on its own, so it stays open
    when this app closes: False. Raises AppError."""
    exe = program(app.id)
    if exe is None:
        raise AppError(f"{app.name} wasn't found. Settings > Apps can point to it.")
    if app_helpers.goes_to_open_window(app.id, file):
        try:
            app_helpers.send(app.id, file)
            return True
        except OSError:
            pass  # (a new window, then)
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    frozen_dll_folder = _forget_dll_folder()
    try:
        subprocess.Popen(  # pylint: disable=consider-using-with
            command(app, exe, file), cwd=os.path.dirname(exe), env=_child_environment(), close_fds=True, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise AppError(f"{app.name} couldn't be started: {exc}") from exc
    finally:
        _restore_dll_folder(frozen_dll_folder)
    return False


def _forget_dll_folder() -> Optional[str]:
    """The one-file build tells Windows to look for DLLs in its own folder, and programs it starts
    would inherit that (and load its Qt): turned off while one starts."""
    folder = getattr(sys, "_MEIPASS", None)
    if sys.platform == "win32" and folder:
        import ctypes

        ctypes.windll.kernel32.SetDllDirectoryW(None)
    return folder


def _restore_dll_folder(folder: Optional[str]) -> None:
    if sys.platform == "win32" and folder:
        import ctypes

        ctypes.windll.kernel32.SetDllDirectoryW(folder)


# ----------------------------------------------------------------------
# Finding them
# ----------------------------------------------------------------------
def detect() -> dict[str, list[str]]:
    """Every program found for each app, newest first. Takes a second or two: call off the UI thread."""
    candidates = [*_registered_programs(), *_usual_programs(), *_shortcut_targets()]
    found: dict[str, dict[str, str]] = {}  # app id -> its folder -> the program in it
    for path in candidates:
        path = os.path.normpath(path)
        app = _BY_EXE.get(os.path.basename(path).lower())
        if app is None or not os.path.isfile(path):
            continue
        folders = found.setdefault(app.id, {})
        folder = os.path.normcase(os.path.dirname(path))
        known = folders.get(folder)
        if known is None or _preference(app, path) < _preference(app, known):
            folders[folder] = path
    return {app_id: sorted(folders.values(), key=_newest_first) for app_id, folders in found.items()}


def _preference(app: App, path: str) -> int:
    return app.exe_names.index(os.path.basename(path).lower())


def _newest_first(path: str) -> tuple:
    """By the version in its folder's name ("Maya2027", "Blender 4.0"), then by the program's date."""
    version: tuple[int, ...] = ()
    for part in reversed(Path(path).parent.parts[1:]):
        numbers = re.findall(r"\d+(?:\.\d+)*", part)
        if numbers:
            version = tuple(int(n) for n in numbers[-1].split("."))
            break
    try:
        modified = os.path.getmtime(path)
    except OSError:
        modified = 0.0
    return tuple(-n for n in version) + (0,) * (4 - len(version)), -modified, path.lower()


def _usual_programs() -> Iterator[str]:
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if not root:
            continue
        for app in APPS:
            for pattern in app.folders:
                for folder in glob.glob(os.path.join(glob.escape(root), pattern)):
                    for name in app.exe_names:
                        yield os.path.join(folder, name)


def _shortcut_folders() -> list[Path]:
    home = Path.home()
    folders = [home / "Desktop", Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"]
    for root in (os.environ.get("ProgramData"), os.environ.get("APPDATA")):
        if root:
            folders.append(Path(root) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return folders


def _shortcut_targets() -> Iterator[str]:
    for folder in _shortcut_folders():
        for current, _dirs, names in os.walk(folder):
            for name in names:
                if name.lower().endswith(".lnk"):
                    target = read_shortcut(Path(current) / name)
                    if target:
                        yield target


def read_shortcut(path: Path) -> Optional[str]:
    """The local file a Windows shortcut points at (Microsoft's Shell Link format), or None: not a
    shortcut, damaged, or one without a plain path (installers' "advertised" shortcuts)."""
    try:
        raw = Path(path).read_bytes()
        if len(raw) < 76 or struct.unpack_from("<I", raw, 0)[0] != 0x4C:
            return None
        flags = struct.unpack_from("<I", raw, 20)[0]
        at = 76
        if flags & 0x01:  # (first comes a list of item IDs)
            at += 2 + struct.unpack_from("<H", raw, at)[0]
        if not flags & 0x02:  # (no link info: no path)
            return None
        header_size, info_flags = struct.unpack_from("<II", raw, at + 4)
        if not info_flags & 0x01:  # (not on a local drive)
            return None
        if header_size >= 0x24:  # (the path in UTF-16)
            start = at + struct.unpack_from("<I", raw, at + 28)[0]
            end = start
            while raw[end:end + 2] != b"\x00\x00":
                end += 2
                if end >= len(raw):
                    return None
            return raw[start:end].decode("utf-16-le")
        start = at + struct.unpack_from("<I", raw, at + 16)[0]
        return raw[start:raw.index(b"\x00", start)].decode("mbcs" if sys.platform == "win32" else "latin-1")
    except (OSError, struct.error, ValueError, IndexError):
        return None


def _command_program(command_line: str) -> Optional[str]:
    """The program in a registered command: "C:\\a b\\app.exe" "%1", or the same unquoted."""
    command_line = command_line.strip()
    if command_line.startswith('"'):
        end = command_line.find('"', 1)
        return command_line[1:end] if end > 0 else None
    match = re.match(r"(.+?\.exe)\b", command_line, re.IGNORECASE)
    return match.group(1) if match else None


def _registry_value(root, key: str, name: str = "") -> Optional[str]:
    """A text value from the Windows registry (None: not there)."""
    try:
        import winreg

        with winreg.OpenKey(root, key) as handle:
            value = winreg.QueryValueEx(handle, name)[0]
        return value if isinstance(value, str) else None
    except (ImportError, OSError):
        return None


def _associated_program(extension: str) -> Optional[str]:
    """The program Windows opens a file type with, if it's there."""
    try:
        import winreg
    except ImportError:
        return None
    file_type = _registry_value(winreg.HKEY_CLASSES_ROOT, extension)
    if not file_type:
        return None
    line = _registry_value(winreg.HKEY_CLASSES_ROOT, rf"{file_type}\shell\open\command")
    path = _command_program(line) if line else None
    return path if path and os.path.isfile(path) else None


def _subkeys(root, key: str) -> Iterable[str]:
    import winreg

    try:
        with winreg.OpenKey(root, key) as handle:
            return [winreg.EnumKey(handle, i) for i in range(winreg.QueryInfoKey(handle)[0])]
    except OSError:
        return []


def _registered_programs() -> Iterator[str]:
    """What Windows records: the programs for file types, App Paths, and the installed programs list."""
    try:
        import winreg
    except ImportError:
        return
    for extension in sorted({e for app in APPS for e in app.owns} | {".psd"}):
        path = _associated_program(extension)
        if path:
            yield path
    roots = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    for root in roots:
        for name in _BY_EXE:
            value = _registry_value(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}")
            if value:
                yield value.strip('"')
    for root, key in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ):
        for entry in _subkeys(root, key):
            icon = _registry_value(root, rf"{key}\{entry}", "DisplayIcon")
            if icon:
                yield icon.split(",")[0].strip('"')
            location = _registry_value(root, rf"{key}\{entry}", "InstallLocation")
            if location:
                for name in _BY_EXE:
                    yield os.path.join(location, name)
                    yield os.path.join(location, "bin", name)
