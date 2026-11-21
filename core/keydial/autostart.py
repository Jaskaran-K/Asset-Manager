"""Starting KeyDial with Windows, and running it as administrator.

- Normally: a value in HKCU\\...\\Run (no administrator needed).
- Run as administrator: a Task Scheduler task that starts it at sign-in with the highest rights,
  so Windows doesn't ask at every start. Making or removing that task needs KeyDial to be running
  as administrator already (Windows asks once: relaunch_elevated).

Windows blocks input from normal programs into programs run as administrator, so KeyDial's
shortcuts only reach those if KeyDial runs as administrator too.

The registry and schtasks calls go through _registry / _schtasks, which the tests replace.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "KeyDial"
TASK_NAME = "KeyDial"
TRAY_ARG = "--tray"


def command() -> list[str]:
    """How to start KeyDial: its exe, or (from the source) pythonw with keydial_main.py."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    script = Path(__file__).resolve().parents[2] / "keydial_main.py"
    return [str(windowless if windowless.exists() else python), str(script)]


def _quoted(parts: list[str]) -> str:
    return subprocess.list2cmdline(parts)


def is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


# -- the backends (replaced in tests) ----------------------------------------------------------
class _Registry:
    def set(self, value: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, value)

    def get(self) -> Optional[str]:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return winreg.QueryValueEx(key, RUN_VALUE)[0]
        except OSError:
            return None

    def delete(self) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except OSError:
            pass


def _run_schtasks(args: list[str]) -> tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=flags, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


_registry = _Registry()
_schtasks = _run_schtasks


# -- the task ----------------------------------------------------------------------------------
def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def task_xml(parts: list[str]) -> str:
    """The task: at sign-in, highest rights, never stopped after a time limit, and started on
    battery too (schtasks' own defaults stop a task after 3 days and skip it on battery)."""
    program, arguments = parts[0], _quoted(parts[1:] + [TRAY_ARG])
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "") if os.environ.get("USERNAME") else ""
    user_line = f"<UserId>{_escape(user)}</UserId>" if user else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>KeyDial: the Huion KD100's keys and dial (runs as administrator)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled>{user_line}</LogonTrigger></Triggers>
  <Principals><Principal id="Author">{user_line}<LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
  </Settings>
  <Actions Context="Author"><Exec><Command>{_escape(program)}</Command><Arguments>{_escape(arguments)}</Arguments></Exec></Actions>
</Task>
"""


def _make_task() -> Optional[str]:
    with tempfile.TemporaryDirectory() as folder:
        xml = Path(folder) / "KeyDial.xml"
        xml.write_text(task_xml(command()), encoding="utf-16")
        code, out = _schtasks(["/Create", "/TN", TASK_NAME, "/XML", str(xml), "/F"])
    return None if code == 0 else out or f"schtasks failed ({code})"


def has_task() -> bool:
    return _schtasks(["/Query", "/TN", TASK_NAME])[0] == 0


def _remove_task() -> Optional[str]:
    if not has_task():
        return None
    code, out = _schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    return None if code == 0 else out or f"schtasks failed ({code})"


# -- what the settings call --------------------------------------------------------------------
def apply(start_with_windows: bool, run_as_admin: bool) -> Optional[str]:
    """Make Windows start KeyDial as the settings say. Returns a problem to show, or None.

    The administrator task can only be made or removed while KeyDial runs as administrator; until
    then the normal start entry stays, so KeyDial still starts (just without administrator rights).
    """
    elevated = is_elevated()
    if start_with_windows and run_as_admin:
        if not elevated:
            _registry.set(_quoted(command() + [TRAY_ARG]))
            return "Starting as administrator is set up the next time KeyDial runs as administrator."
        problem = _make_task()
        if problem:
            _registry.set(_quoted(command() + [TRAY_ARG]))
            return f"Couldn't make the start-up task: {problem}"
        _registry.delete()
        return None
    if start_with_windows:
        _registry.set(_quoted(command() + [TRAY_ARG]))
    else:
        _registry.delete()
    if elevated:
        return _remove_task()
    return None


def remove_all() -> None:
    """The uninstaller's clean-up (KeyDial.exe --remove-autostart): no start entry left behind."""
    _registry.delete()
    if is_elevated():
        _remove_task()


def relaunch_elevated(extra: tuple[str, ...] = ()) -> bool:
    """Start KeyDial again as administrator (Windows asks). False if that was declined or failed."""
    if sys.platform != "win32":
        return False
    import ctypes

    parts = command()
    params = _quoted(parts[1:] + list(extra))
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", parts[0], params or None, None, 1)
    return int(result) > 32
