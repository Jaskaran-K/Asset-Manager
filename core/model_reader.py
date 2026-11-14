"""Reading 3D models in a helper program of its own (AssetManager.exe --model-reader), so a file that crashes
its reader, or needs more memory than there is, can't take the app with it, and so the app's window doesn't
stutter while a big model is read (Python's reading would hold up the app's own work).

The app starts helpers when they're needed: one for the library (thumbnails and file details) and one for
the viewer, so opening a model never waits behind thumbnails. Each reads one model at a time, stays while
there's work, and goes after IDLE_SECONDS. They talk over a named pipe that only they and the app know (a
random key), and they share a Windows job object that caps their memory together at half the computer's,
and ends them if the app goes.

A model that crashed a helper, ran it out of memory or took too long is remembered by its path, size and date
(FAILURES_FILE), so the library doesn't read it again until it changes; the viewer always tries again.

If no helper can be started, models are read in the app as before (tests choose that with READER_ENV).
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Any, Callable, Optional

IDLE_SECONDS = 60.0
START_SECONDS = 20.0  # for a helper to start listening (a cold disk, an antivirus scan)
BASE_TIMEOUT = 120.0  # seconds for one model, plus TIMEOUT_PER_GB for each GB of it
TIMEOUT_PER_GB = 120.0
KEY_ENV = "ASSETMANAGER_READER_KEY"
READER_ENV = "ASSETMANAGER_MODEL_READER"  # "inprocess": read in the app (tests), else a helper
FAILURES_FILE = "model-reader-failures.json"
MAX_FAILURES = 2000


class ReadFailed(Exception):
    """The helper crashed, ran out of memory or took too long on this model (the message says which)."""


class ReaderUnavailable(Exception):
    """No helper could be started."""


# ----------------------------------------------------------------------
# The helper's side
# ----------------------------------------------------------------------
def _picture_reply(mesh, size: int) -> dict[str, Any]:
    from PySide6.QtGui import QImage

    from .thumbnail_manager import render_model_picture

    if mesh.stand_in:
        return {"picture": None, "faces": None}
    picture = render_model_picture(mesh.vertices, mesh.faces, mesh.normals, size)
    if picture is None or picture.isNull():
        return {"picture": None, "faces": mesh.face_count}
    picture = picture.convertToFormat(QImage.Format.Format_RGBA8888)
    data = bytes(picture.constBits())[: picture.sizeInBytes()]
    return {"picture": (picture.width(), picture.height(), picture.bytesPerLine(), data), "faces": mesh.face_count}


MESH_ARRAYS = ("vertices", "faces", "normals")
SPILL_BYTES = 64 * 1024 * 1024  # a mesh bigger than this comes back in files, not through the pipe


def _spill_mesh(mesh, folder: Path):
    """Put a big mesh's arrays in files (the pipe would copy them, and the app would hold two copies while
    it took them in); the mesh sent has their paths instead."""
    if sum(getattr(mesh, name).nbytes for name in MESH_ARRAYS) <= SPILL_BYTES:
        return mesh
    import numpy as np

    folder.mkdir(parents=True, exist_ok=True)
    for name in MESH_ARRAYS:
        target = folder / f"{name}.npy"
        np.save(target, getattr(mesh, name))
        setattr(mesh, name, str(target))
    return mesh


def _take_mesh(mesh):
    """The app's side of _spill_mesh: the arrays read back, and their files deleted."""
    import numpy as np

    for name in MESH_ARRAYS:
        value = getattr(mesh, name)
        if isinstance(value, str):
            path = Path(value)
            try:
                setattr(mesh, name, np.load(path))
            finally:
                path.unlink(missing_ok=True)
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
    return mesh


def jobs_folder() -> Path:
    from . import video_tools

    return video_tools.CACHE_ROOT / "model-reader"


def handle(request: dict[str, Any]) -> dict[str, Any]:
    """Do one request (in the helper)."""
    from .model_loader import load_3d_model

    op, path = request.get("op"), Path(request.get("path", ""))
    if op == "ping":
        return {"pong": os.getpid()}
    if op == "mesh":
        return {"mesh": _spill_mesh(load_3d_model(path), Path(request["spill"]))}
    if op == "picture":
        return _picture_reply(load_3d_model(path), int(request.get("size", 110)))
    if op == "faces":
        mesh = load_3d_model(path)
        return {"faces": None if mesh.stand_in else mesh.face_count}
    if op == "uvs":
        from .model_uvs import read_uvs

        return {"uvs": read_uvs(path, int(request.get("set", 0)))}
    if op == "crash" and os.environ.get(READER_ENV) == "testing":  # (tests: a reader dying mid-read)
        os._exit(3)
    if op == "sleep" and os.environ.get(READER_ENV) == "testing":  # (tests: a read that takes long)
        time.sleep(float(request.get("seconds", 1)))
        return {"slept": True}
    return {"error": f"unknown request {op!r}"}


def serve(address: str, handler: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
          idle_seconds: float = IDLE_SECONDS) -> int:
    """The helper: answer requests on address (with handler: handle by default) until the app goes, or
    nothing comes for idle_seconds. (core.ai_helper serves its own requests this way too.)"""
    handler = handler or handle
    key = bytes.fromhex(os.environ.get(KEY_ENV, ""))
    if not key:
        return 2
    listener = Listener(address, family="AF_PIPE", authkey=key)
    try:
        connection = listener.accept()
    finally:
        listener.close()
    with connection:
        while True:
            try:
                if not connection.poll(idle_seconds):
                    return 0
                request = connection.recv()
            except (EOFError, OSError):
                return 0  # (the app went)
            try:
                reply = handler(request)
            except MemoryError:
                reply = {"error": "memory"}
            except Exception as exc:  # pylint: disable=broad-except
                reply = {"error": f"{type(exc).__name__}: {exc}", "kind": type(exc).__name__}
            try:
                connection.send(reply)
            except (OSError, ValueError):
                return 0


# ----------------------------------------------------------------------
# A job object: the helpers' memory cap, and their end when the app ends
# ----------------------------------------------------------------------
def memory_limit() -> int:
    """Half the computer's memory (bytes), what the helpers may use together."""
    if sys.platform != "win32":
        return 0

    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in ("total", "free", "page_total", "page_free", "virtual_total", "virtual_free", "extended")
        ]

    status = Status()
    status.length = ctypes.sizeof(Status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return status.total // 2


_job_handle: Optional[int] = None
_job_lock = threading.Lock()


def _job() -> Optional[int]:
    """The job object every helper goes in (made once). None if it couldn't be made."""
    global _job_handle
    if sys.platform != "win32":
        return None
    with _job_lock:
        if _job_handle is not None:
            return _job_handle or None
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("Basic", Basic), ("IoInfo", ctypes.c_ulonglong * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            _job_handle = 0
            return None
        info = Extended()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: they end with the app
        limit = memory_limit()
        if limit:
            info.Basic.LimitFlags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = limit
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):  # (extended limits)
            _job_handle = 0
            return None
        _job_handle = handle
        return handle


def _put_in_job(process: subprocess.Popen) -> None:
    job = _job()
    if job is None:
        return
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject(job, int(process._handle))  # pylint: disable=protected-access


# ----------------------------------------------------------------------
# The app's side
# ----------------------------------------------------------------------
def helper_command(address: str) -> list[str]:
    """How to start a helper: the app itself with --model-reader (from source: this module)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--model-reader", address]
    return [sys.executable, "-m", "core.model_reader", address]


class ModelReader:
    """One helper (started when first needed, again after it goes). Calls take turns: one model at a time."""

    def __init__(self, name: str, background: bool, command: Optional[Callable[[str], list[str]]] = None,
                 extra_env: Optional[Callable[[], dict[str, str]]] = None):
        self.name = name
        self.command = command or helper_command  # (core.ai_helper starts its own helper)
        self.extra_env = extra_env  # (more for its environment: core.ai_helper says where the tools are)
        self.background = background  # (below normal priority: the library's reading never slows the app)
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._connection: Optional[Connection] = None

    def _alive(self) -> bool:
        return self._process is not None and self._process.poll() is None and self._connection is not None

    def _start(self) -> None:
        address = rf"\\.\pipe\AssetManager-{os.getpid()}-{self.name}-{uuid.uuid4().hex[:8]}"
        key = os.urandom(16)
        env = {**os.environ, KEY_ENV: key.hex(), **(self.extra_env() if self.extra_env is not None else {})}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if self.background:
            flags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        root = Path(__file__).resolve().parent.parent
        try:
            process = subprocess.Popen(
                self.command(address), env=env, cwd=str(root), creationflags=flags,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise ReaderUnavailable(f"the model reader couldn't be started: {exc}") from exc
        _put_in_job(process)
        deadline = time.monotonic() + START_SECONDS
        while True:
            try:
                self._connection = Client(address, family="AF_PIPE", authkey=key)
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if process.poll() is not None or time.monotonic() > deadline:
                    process.kill()
                    raise ReaderUnavailable("the model reader didn't start") from None
                time.sleep(0.03)
        self._process = process

    def _kill(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except OSError:
                pass
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            try:
                self._process.wait(5)
            except subprocess.TimeoutExpired:
                pass
        self._process = self._connection = None

    def close(self) -> None:
        with self._lock:
            self._kill()

    def request(self, request: dict[str, Any], timeout: float,
                should_stop: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
        """Send a request and wait for the answer. Raises ReadFailed (the helper is ended),
        ReaderUnavailable, obj_fast.LoadCancelled (should_stop said so: the helper is ended)."""
        from .obj_fast import LoadCancelled

        with self._lock:
            if not self._alive():
                self._kill()
                self._start()
            connection = self._connection
            try:
                connection.send(request)
                deadline = time.monotonic() + timeout
                while not connection.poll(0.1):
                    if should_stop is not None and should_stop():
                        self._kill()
                        raise LoadCancelled()
                    if time.monotonic() > deadline:
                        self._kill()
                        raise ReadFailed(f"reading it took over {timeout / 60:.0f} minutes")
                    if self._process is None or self._process.poll() is not None:
                        break
                reply = connection.recv()
            except (EOFError, OSError, ConnectionError):
                code = self._process.poll() if self._process is not None else None
                self._kill()
                if code is not None and code & 0xFFFFFFFF in (0xC0000017, 0xC000012D):  # (out of memory, commit limit)
                    raise ReadFailed("it needs more memory than the reader may use") from None
                raise ReadFailed("reading it crashed the model reader") from None
        if reply.get("error") == "memory":
            raise ReadFailed("it needs more memory than the reader may use")
        return reply


_readers: dict[str, ModelReader] = {}
_readers_lock = threading.Lock()
_unavailable = False  # (a helper couldn't be started: read in the app from then on)


def reader(name: str) -> ModelReader:
    with _readers_lock:
        if name not in _readers:
            _readers[name] = ModelReader(name, background=name != "viewer")
        return _readers[name]


def close_all() -> None:
    for each in list(_readers.values()):
        each.close()


def in_process() -> bool:
    return _unavailable or os.environ.get(READER_ENV) == "inprocess"


def _timeout(path: Path) -> float:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return BASE_TIMEOUT + TIMEOUT_PER_GB * size / 1024 ** 3


def _ask(name: str, request: dict[str, Any], should_stop=None) -> Optional[dict[str, Any]]:
    """The helper's answer, or None if there's no helper (read in the app instead)."""
    global _unavailable
    if in_process():
        return None
    try:
        reply = reader(name).request(request, _timeout(Path(request["path"])), should_stop)
    except ReaderUnavailable as exc:
        _unavailable = True
        from . import crash_log

        crash_log.log_error("Models are read in the app: the model reader couldn't start", str(exc))
        return None
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply


# ------------------ remembered failures ------------------
_failures: Optional[dict[str, str]] = None
_failures_lock = threading.Lock()


def _failures_path() -> Path:
    from . import video_tools

    return video_tools.CACHE_ROOT / FAILURES_FILE


def _file_key(path: Path) -> Optional[str]:
    try:
        info = path.stat()
    except OSError:
        return None
    return f"{os.path.normcase(str(path))}|{info.st_size}|{info.st_mtime_ns}"


def _load_failures() -> dict[str, str]:
    global _failures
    if _failures is None:
        try:
            data = json.loads(_failures_path().read_text(encoding="utf-8"))
            _failures = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _failures = {}
    return _failures


def known_failure(path: Path) -> Optional[str]:
    """Why the library's reading of this model failed before (it hasn't changed since), or None."""
    key = _file_key(path)
    with _failures_lock:
        return _load_failures().get(key) if key else None


def _remember_failure(path: Path, reason: str) -> None:
    key = _file_key(path)
    if key is None:
        return
    with _failures_lock:
        failures = _load_failures()
        failures[key] = reason
        while len(failures) > MAX_FAILURES:
            failures.pop(next(iter(failures)))
        try:
            target = _failures_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
            temp.write_text(json.dumps(failures), encoding="utf-8")
            os.replace(temp, target)
        except OSError:
            pass


def _library_ask(request: dict[str, Any], name: str = "library") -> Optional[dict[str, Any]]:
    """The library's reading: skips models that failed before; remembers new failures."""
    path = Path(request["path"])
    reason = known_failure(path)
    if reason is not None:
        raise ReadFailed(reason)
    try:
        return _ask(name, request)
    except ReadFailed as exc:
        _remember_failure(path, str(exc))
        raise


# ------------------ what the app asks for ------------------
def read_picture(path: Path, size: int):
    """A model's thumbnail picture (QImage), or None if it can't be drawn. Raises ReadFailed."""
    reply = _library_ask({"op": "picture", "path": str(path), "size": size})
    if reply is None:
        from .thumbnail_manager import render_model_picture
        from .model_loader import load_3d_model

        mesh = load_3d_model(path)
        return None if mesh.stand_in else render_model_picture(mesh.vertices, mesh.faces, mesh.normals, size)
    if reply.get("picture") is None:
        return None
    from PySide6.QtGui import QImage

    width, height, line, data = reply["picture"]
    return QImage(data, width, height, line, QImage.Format.Format_RGBA8888).copy()


def read_faces(path: Path) -> Optional[int]:
    """How many triangles a model has (None if it can't be read). Raises ReadFailed."""
    reply = _library_ask({"op": "faces", "path": str(path)}, "details")  # (its own: a 1 GB model mustn't hold up thumbnails)
    if reply is None:
        from .model_loader import load_3d_model

        mesh = load_3d_model(path)
        return None if mesh.stand_in else mesh.face_count
    return reply.get("faces")


def read_mesh(path: Path, should_stop: Optional[Callable[[], bool]] = None):
    """The whole model, for the viewer (Mesh3D). Raises ReadFailed, obj_fast.LoadCancelled."""
    spill = jobs_folder() / uuid.uuid4().hex[:10]
    try:
        reply = _ask("viewer", {"op": "mesh", "path": str(path), "spill": str(spill)}, should_stop)
        if reply is None:
            from .model_loader import load_3d_model

            return load_3d_model(path, should_stop=should_stop)
        return _take_mesh(reply["mesh"])
    finally:
        if spill.exists():  # (left by a read that was stopped or failed)
            import shutil

            shutil.rmtree(spill, ignore_errors=True)


def read_uvs(path: Path, set_index: int = 0, should_stop: Optional[Callable[[], bool]] = None):
    """A model's UV layout (model_uvs.UvLayout), or None. Raises ReadFailed, obj_fast.LoadCancelled."""
    reply = _ask("viewer", {"op": "uvs", "path": str(path), "set": set_index}, should_stop)
    if reply is None:
        from .model_uvs import read_uvs as read_in_app

        return read_in_app(path, set_index, should_stop)
    return reply["uvs"]


if __name__ == "__main__":
    sys.exit(serve(sys.argv[1]))
