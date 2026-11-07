"""LuxCore, the Render button's engine: downloaded the first time it's asked for, not part of the app.

The two files come straight from PyPI, each checked against the hash below, and are unpacked into the
app's data folder. They must be built for the app's Python, so a new Python means new files here
(tests/test_render.py fails until they're updated). LuxCore is Apache-licensed, and the second file is NVIDIA's
own (proprietary): downloading it from NVIDIA's PyPI package keeps it out of the app.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from .archives import Cancelled


@dataclass(frozen=True)
class Download:
    name: str
    url: str
    size: int
    sha256: str


PYTHON = (3, 14)  # the Python these are built for
ENGINE_VERSION = "2.11.2"
DOWNLOADS = (
    Download(
        "pyluxcore-2.11.2-cp314-cp314-win_amd64.whl",
        "https://files.pythonhosted.org/packages/aa/bd/16ecfb9b4ad74da7625df66057ea94ef4cce4cac2d564f21b4a1802b2d14/pyluxcore-2.11.2-cp314-cp314-win_amd64.whl",
        83233947, "d9ec7079b52cb05ee0bfdc93988131ffe5c9dba0cd390bc1bbf6417b3c9def93",
    ),
    Download(
        "nvidia_cuda_nvrtc_cu12-12.8.93-py3-none-win_amd64.whl",
        "https://files.pythonhosted.org/packages/45/51/52a3d84baa2136cc8df15500ad731d74d3a1114d4c123e043cb608d4a32b/nvidia_cuda_nvrtc_cu12-12.8.93-py3-none-win_amd64.whl",
        73586838, "7a4b6b2904850fe78e0bd179c4b655c404d4bb799ef03ddc60804247099ae909",
    ),
)
DOWNLOAD_BYTES = sum(d.size for d in DOWNLOADS)
_LEFT_OUT = ("bin/", "pyluxcoretest/", "pyluxcoretools/")  # (its other programs and tests: not needed to render)
_MARKER = "installed.json"
_CHUNK = 1024 * 1024
_TIMEOUT = 60

Progress = Callable[[int, int], None]


class EngineError(Exception):
    """It couldn't be installed (the message says why, for the user)."""


def _root() -> Path:
    from . import video_tools  # (read when used, so tests can redirect it)

    return video_tools.CACHE_ROOT / "render"


def engine_folder() -> Path:
    """(Short: its DLLs' names are long, and Windows won't load a DLL from a path of over 260 characters.)"""
    return _root() / f"lux-{ENGINE_VERSION}"


def jobs_folder() -> Path:
    """Renders under way (core.render_worker): each has a folder of its own, deleted when its window closes."""
    return _root() / "jobs"


def supported() -> bool:
    return sys.platform == "win32" and sys.version_info[:2] == PYTHON


def is_installed() -> bool:
    return supported() and (engine_folder() / _MARKER).is_file()


def add_to_path() -> None:
    """So that "import pyluxcore" finds it (the worker process only)."""
    sys.path.insert(0, str(engine_folder()))


def installed_bytes() -> int:
    return sum(p.stat().st_size for p in engine_folder().rglob("*") if p.is_file()) if is_installed() else 0


def remove() -> None:
    """Delete it, whatever version is there."""
    shutil.rmtree(_root(), ignore_errors=True)


def _download(item: Download, target: Path, done_before: int, progress: Optional[Progress], should_stop: Optional[Callable[[], bool]]) -> None:
    digest = hashlib.sha256()
    request = urllib.request.Request(item.url, headers={"User-Agent": "AssetManager"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as reply, open(target, "wb") as out:
            got = 0
            while True:
                if should_stop is not None and should_stop():
                    raise Cancelled()
                chunk = reply.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                if progress is not None:
                    progress(done_before + got, DOWNLOAD_BYTES)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise EngineError(f"The download failed: {getattr(exc, 'reason', exc)}") from exc
    if digest.hexdigest() != item.sha256:  # (nothing that isn't exactly this file gets run)
        raise EngineError(f"{item.name} didn't download correctly. Try again.")


def _unpack(wheel: Path, folder: Path) -> None:
    with zipfile.ZipFile(wheel) as z:
        for info in z.infolist():
            name = PurePosixPath(info.filename)
            if info.is_dir() or info.filename.startswith(_LEFT_OUT):
                continue
            if name.is_absolute() or ".." in name.parts:
                raise EngineError(f"{wheel.name} holds a file it shouldn't: {info.filename}")
            target = folder.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out, _CHUNK)


def install(progress: Optional[Progress] = None, should_stop: Optional[Callable[[], bool]] = None) -> None:
    """Download and unpack it. Nothing is left in place unless it all worked. Raises EngineError, Cancelled."""
    if not supported():
        raise EngineError("LuxCore isn't available for this version of the app.")
    if is_installed():
        return
    root = _root()
    working = root / "unpacking"
    try:
        shutil.rmtree(working, ignore_errors=True)
        downloads = working / "downloads"
        contents = working / "contents"
        downloads.mkdir(parents=True)
        contents.mkdir()
        done = 0
        for item in DOWNLOADS:
            wheel = downloads / item.name
            _download(item, wheel, done, progress, should_stop)
            done += item.size
        for item in DOWNLOADS:
            if should_stop is not None and should_stop():
                raise Cancelled()
            _unpack(downloads / item.name, contents)
        (contents / _MARKER).write_text(json.dumps({"version": ENGINE_VERSION}), encoding="utf-8")
        target = engine_folder()
        shutil.rmtree(target, ignore_errors=True)
        contents.rename(target)
    except OSError as exc:
        raise EngineError(f"LuxCore couldn't be installed: {exc.strerror or exc}") from exc
    finally:
        shutil.rmtree(working, ignore_errors=True)
