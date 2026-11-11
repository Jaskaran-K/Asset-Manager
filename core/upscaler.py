"""Upscaling pictures with Real-ESRGAN (the Artbook's Upscale): downloaded the first time it's asked for, not
part of the app.

The download is the project's own Windows build (realesrgan-ncnn-vulkan), checked against the hash below; only
the program and the two models used are unpacked into the app's data folder. It runs as a program of its own on
the graphics card (Vulkan: NVIDIA, AMD or Intel), one picture at a time. Real-ESRGAN is BSD-licensed.

Its models always make a picture 4 times bigger; a smaller size (2x, or the most a picture may become,
MAX_SIDE) is made from that. Transparency isn't something the models know: it's resized on its own and put
back. The result is made up of believable detail, not recovered detail, so the original is always kept.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from PIL import Image

from .archives import Cancelled
from .render_engine import Download

VERSION = "0.2.5.0"
DOWNLOAD = Download(
    "realesrgan-ncnn-vulkan-20220424-windows.zip",
    "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip",
    45474481, "abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d",
)
PROGRAM = "realesrgan-ncnn-vulkan.exe"
# The kinds of picture there's a model for: (model, what it's called, what it's good for)
MODELS = {
    "photo": ("realesrgan-x4plus", "Photo", "Photos, renders, sculpts and paintings"),
    "illustration": ("realesrgan-x4plus-anime", "Illustration", "Line art, manga and anime: clean lines, flat tones"),
}
DEFAULT_MODEL = "photo"
FACTORS = (2, 4)
DEFAULT_FACTOR = 4
MODEL_FACTOR = 4  # (what the models make; other sizes are resized from it)
MAX_SIDE = 6000  # the longest a result may be (a 6000 px picture is about 140 MB on the board)
MIN_GAIN = 1.25  # less than this much bigger isn't worth it
MAX_MODEL_INPUT = MAX_SIDE // 2  # bigger pictures are made this big first (the model's 4x would be huge)
_KEEP = {PROGRAM, "vcomp140.dll"} | {f"models/{m}.{ext}" for m, _name, _tip in MODELS.values() for ext in ("bin", "param")}
_MARKER = "installed.json"
_CHUNK = 1024 * 1024
_TIMEOUT = 60
_RUN_TIMEOUT = 600  # seconds for one picture (a big one on a slow card takes a minute or two)
_PERCENT = re.compile(r"^\s*(\d+(?:\.\d+)?)%\s*$")
_NO_GPU = ("vkcreateinstance failed", "invalid gpu device", "no vulkan", "vkenumeratephysicaldevices")

Progress = Callable[[int, int], None]


class UpscaleError(Exception):
    """It couldn't be installed or run (the message says why, for the user)."""


def _root() -> Path:
    from . import video_tools  # (read when used, so tests can redirect it)

    return video_tools.CACHE_ROOT / "upscaler"


def engine_folder() -> Path:
    return _root() / f"realesrgan-{VERSION}"


def jobs_folder() -> Path:
    """Pictures being upscaled: each has a folder of its own, deleted when it's done."""
    return _root() / "jobs"


def supported() -> bool:
    return sys.platform == "win32"


def is_installed() -> bool:
    return supported() and (engine_folder() / _MARKER).is_file()


def installed_bytes() -> int:
    return sum(p.stat().st_size for p in engine_folder().rglob("*") if p.is_file()) if is_installed() else 0


def remove() -> None:
    shutil.rmtree(_root(), ignore_errors=True)


# ----------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------
def _download(target: Path, progress: Optional[Progress], should_stop: Optional[Callable[[], bool]]) -> None:
    digest = hashlib.sha256()
    request = urllib.request.Request(DOWNLOAD.url, headers={"User-Agent": "AssetManager"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as reply, open(target, "wb") as out:  # noqa: S310 (a fixed https address)
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
                    progress(got, DOWNLOAD.size)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise UpscaleError(f"The download failed: {getattr(exc, 'reason', exc)}") from exc
    if digest.hexdigest() != DOWNLOAD.sha256:  # (nothing that isn't exactly this file gets run)
        raise UpscaleError("Real-ESRGAN didn't download correctly. Try again.")


def _unpack(archive: Path, folder: Path) -> None:
    """Only the program and the models used (the archive also has demo pictures and a video)."""
    found = set()
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            name = PurePosixPath(info.filename)
            if info.is_dir() or name.is_absolute() or ".." in name.parts or name.as_posix() not in _KEEP:
                continue
            target = folder.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out, _CHUNK)
            found.add(name.as_posix())
    if found != _KEEP:
        raise UpscaleError(f"The download is missing {', '.join(sorted(_KEEP - found))}.")


def install(progress: Optional[Progress] = None, should_stop: Optional[Callable[[], bool]] = None) -> None:
    """Download and unpack it. Nothing is left in place unless it all worked. Raises UpscaleError, Cancelled."""
    if not supported():
        raise UpscaleError("Upscaling is only available on Windows.")
    if is_installed():
        return
    working = _root() / "unpacking"
    try:
        shutil.rmtree(working, ignore_errors=True)
        contents = working / "contents"
        contents.mkdir(parents=True)
        archive = working / DOWNLOAD.name
        _download(archive, progress, should_stop)
        if should_stop is not None and should_stop():
            raise Cancelled()
        _unpack(archive, contents)
        (contents / _MARKER).write_text(json.dumps({"version": VERSION}), encoding="utf-8")
        target = engine_folder()
        shutil.rmtree(target, ignore_errors=True)
        contents.rename(target)
    except OSError as exc:
        raise UpscaleError(f"Real-ESRGAN couldn't be installed: {exc.strerror or exc}") from exc
    finally:
        shutil.rmtree(working, ignore_errors=True)


# ----------------------------------------------------------------------
# Upscaling
# ----------------------------------------------------------------------
def target_size(size: tuple[int, int], factor: int) -> Optional[tuple[int, int]]:
    """How big a picture of this size becomes (factor times, but at most MAX_SIDE), or None when
    that's not worth it (it's big already)."""
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    longest = max(w, h)
    goal = min(longest * factor, MAX_SIDE)
    if goal < longest * MIN_GAIN:
        return None
    ratio = goal / longest
    return max(1, round(w * ratio)), max(1, round(h * ratio))


def _command(source: Path, result: Path, model: str) -> list[str]:
    folder = engine_folder()
    return [
        str(folder / PROGRAM), "-i", str(source), "-o", str(result),
        "-n", MODELS[model][0], "-s", str(MODEL_FACTOR), "-m", str(folder / "models"),
    ]


def _explain(output: str) -> str:
    text = output.lower()
    if any(sign in text for sign in _NO_GPU):
        return "It needs a graphics card that supports Vulkan, and none was found."
    lines = [line.strip() for line in output.splitlines() if line.strip() and not _PERCENT.match(line) and not line.startswith("[")]
    return lines[-1] if lines else "it stopped without making a picture."


def _run_model(source: Path, result: Path, model: str, progress: Optional[Callable[[float], None]],
               should_stop: Optional[Callable[[], bool]]) -> None:
    """Run Real-ESRGAN on one picture file (progress: 0..1 as its tiles finish)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            _command(source, result, model), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            cwd=engine_folder(), creationflags=flags,
        )
    except OSError as exc:
        raise UpscaleError(f"Real-ESRGAN couldn't be started: {exc.strerror or exc}") from exc
    output: list[str] = []
    finished = threading.Event()

    def stopper() -> None:  # (stopped from here: reading its output waits for its next line)
        while not finished.wait(0.1):
            if should_stop is not None and should_stop():
                process.kill()
                return

    threading.Thread(target=stopper, daemon=True).start()
    timer = threading.Timer(_RUN_TIMEOUT, process.kill)
    timer.daemon = True
    timer.start()
    try:
        for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").strip()
            match = _PERCENT.match(line)
            if match:
                if progress is not None:
                    progress(min(1.0, float(match.group(1)) / 100))
            elif line:
                output.append(line)
        process.wait()
    finally:
        finished.set()
        timer.cancel()
        process.stderr.close()
    if should_stop is not None and should_stop():
        raise Cancelled()
    if not result.is_file():  # (it says it finished either way: the picture is what counts)
        raise UpscaleError(f"Real-ESRGAN couldn't upscale it: {_explain(chr(10).join(output))}")


def upscale(picture: Image.Image, model: str, factor: int, progress: Optional[Callable[[float], None]] = None,
            should_stop: Optional[Callable[[], bool]] = None) -> Image.Image:
    """The picture factor times bigger (at most MAX_SIDE), by the model ("photo" or "illustration").
    Raises UpscaleError (also when it's big already: target_size), Cancelled."""
    if not is_installed():
        raise UpscaleError("Real-ESRGAN isn't installed.")
    goal = target_size(picture.size, factor)
    if goal is None:
        raise UpscaleError("It's big enough already.")
    alpha = picture.getchannel("A") if "A" in picture.getbands() else None
    if alpha is not None and alpha.getextrema() == (255, 255):
        alpha = None  # (fully opaque: nothing to keep)
    folder = jobs_folder() / uuid.uuid4().hex[:10]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        source, result = folder / "in.png", folder / "out.png"
        rgb = picture.convert("RGB")
        if max(rgb.size) > MAX_MODEL_INPUT:
            rgb.thumbnail((MAX_MODEL_INPUT, MAX_MODEL_INPUT), Image.Resampling.LANCZOS)
        rgb.save(source, "PNG")
        _run_model(source, result, model, progress, should_stop)
        with Image.open(result) as made:
            made.load()
            upscaled = made.convert("RGB")
    except OSError as exc:
        raise UpscaleError(f"The picture couldn't be read or written: {exc.strerror or exc}") from exc
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if upscaled.size != goal:
        upscaled = upscaled.resize(goal, Image.Resampling.LANCZOS)
    if alpha is not None:
        upscaled.putalpha(alpha.resize(goal, Image.Resampling.LANCZOS))
    return upscaled


def clean_up() -> None:
    """What a crash or a closed app may have left."""
    shutil.rmtree(jobs_folder(), ignore_errors=True)


def disk_name(original: Optional[str]) -> str:
    """The extension to store a result as: JPEG for a JPEG (a quarter of the space), else PNG."""
    return ".jpg" if (original or "").lower().endswith((".jpg", ".jpeg", ".jfif")) else ".png"
