"""Crash, hang and error logging to %LOCALAPPDATA%\\AssetManager\\crash.log (standard library only).

Each running session writes to its own session-<pid>.log:
- faulthandler dumps every thread's stack on a hard crash (access violation etc.),
- a watchdog dumps the stacks if the UI thread stops responding for HANG_SECONDS,
- unhandled Python exceptions (including ones in Qt slots and threads) are logged.

On a clean exit the session file is folded into crash.log if anything was logged,
then deleted. A session file left behind means that session didn't exit normally;
the next launch moves it into crash.log and, if it holds a crash or hang dump,
tells the user (see collect_previous_sessions()).
"""

from __future__ import annotations

import atexit
import faulthandler
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import IO, Optional

LOG_DIR: Path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AssetManager"
CRASH_LOG_NAME = "crash.log"
MAX_LOG_BYTES = 1_000_000  # crash.log is rotated to crash.log.1 beyond this
HANG_SECONDS = 20
_CRASH_MARKERS = ("Fatal Python error", "Windows fatal exception", "Timeout (")

_session_file: Optional[IO[str]] = None
_session_path: Optional[Path] = None
_header = ""


def crash_log_path() -> Path:
    return LOG_DIR / CRASH_LOG_NAME


def _append_to_crash_log(text: str) -> None:
    log = crash_log_path()
    try:
        if log.exists() and log.stat().st_size > MAX_LOG_BYTES:
            log.replace(log.with_name(CRASH_LOG_NAME + ".1"))
        with open(log, "a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


def collect_previous_sessions() -> bool:
    """Move logs of sessions that didn't exit normally into crash.log.

    Returns True if one of them crashed or hung (worth telling the user about).
    Session files of other instances that are still running are left alone: Windows
    won't let us rename a file another process has open.
    """
    crashed = False
    for path in sorted(LOG_DIR.glob("session-*.log")):
        if path == _session_path:
            continue
        claimed = path.with_suffix(".collecting")
        try:
            path.replace(claimed)
        except OSError:
            continue  # still open: that instance is running
        try:
            text = claimed.read_text(encoding="utf-8", errors="replace")
            claimed.unlink()
        except OSError:
            continue
        header, _, body = text.partition("\n")
        note = "this session did not close normally"
        if any(marker in body for marker in _CRASH_MARKERS):
            crashed = True
            note = "this session crashed or stopped responding"
        _append_to_crash_log(f"{header}\n--- {note} ---\n{body}")
    return crashed


def _write(text: str) -> None:
    if _session_file is not None:
        try:
            _session_file.write(text if text.endswith("\n") else text + "\n")
            _session_file.flush()
        except (OSError, ValueError):
            pass


def log_error(title: str, details: str = "") -> None:
    """Record an error in this session's log (shown in crash.log after the session ends)."""
    _write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {title}\n{details}".rstrip())


def _excepthook(exc_type, exc, tb) -> None:
    log_error("Unhandled error:", "".join(traceback.format_exception(exc_type, exc, tb)))
    if sys.stderr is not None:  # None in the windowed executable
        sys.__excepthook__(exc_type, exc, tb)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    name = args.thread.name if args.thread is not None else "?"
    log_error(
        f"Unhandled error in thread {name}:",
        "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
    )


def start_session(app_name: str, version: str) -> bool:
    """Start logging this session. Returns True if the previous session crashed or hung."""
    global _session_file, _session_path, _header
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        crashed = collect_previous_sessions()
        _session_path = LOG_DIR / f"session-{os.getpid()}.log"
        _session_file = open(_session_path, "w", encoding="utf-8", buffering=1)
    except OSError:
        return False
    _header = f"=== {app_name} {version}, session started {datetime.now():%Y-%m-%d %H:%M:%S} (pid {os.getpid()}) ==="
    _write(_header)
    faulthandler.enable(_session_file, all_threads=True)
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    atexit.register(finish_session)
    return crashed


def heartbeat() -> None:
    """Call regularly from the UI thread; if it isn't called for HANG_SECONDS, all stacks are dumped."""
    if _session_file is not None:
        faulthandler.dump_traceback_later(HANG_SECONDS, repeat=False, file=_session_file)


def finish_session() -> None:
    """Clean exit: keep the session's log only if something was logged."""
    global _session_file, _session_path
    if _session_file is None or _session_path is None:
        return
    faulthandler.cancel_dump_traceback_later()
    faulthandler.disable()
    try:
        _session_file.close()
        text = _session_path.read_text(encoding="utf-8", errors="replace")
        _session_path.unlink()
    except OSError:
        text = ""
    _session_file = _session_path = None
    header, _, body = text.partition("\n")
    if body.strip():
        _append_to_crash_log(f"{header}\n{body}")
