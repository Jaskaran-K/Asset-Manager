"""ffmpeg helpers for video assets: H.264 preview proxies and grid poster frames.

ffmpeg is an external executable (never a Python dependency). It is looked up
from the path set in Settings > Video, falling back to the system PATH.

Cache layout (%LOCALAPPDATA%/AssetManager):
- proxies/<key>.mp4   720p H.264 proxies for files QMediaPlayer can't play smoothly
- posters/<key>.jpg   poster frames used as grid thumbnails
<key> hashes the source path plus its modified time, so editing or replacing a
video invalidates its cached files automatically.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QProcess, Signal

from .settings import get_ffmpeg_path, get_media_backend

CACHE_ROOT: Path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AssetManager"
PROXY_DIR: Path = CACHE_ROOT / "proxies"
POSTER_DIR: Path = CACHE_ROOT / "posters"
THUMB_DIR: Path = CACHE_ROOT / "thumbnails"  # grid thumbnails (core.thumbnail_manager)

PROXY_HEIGHT = 720
POSTER_WIDTH = 320
POSTER_TIMEOUT_SECONDS = 30

# Keeps ffmpeg from flashing a console window when launched from the windowed exe
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def apply_media_backend() -> None:
    """Set QT_MEDIA_BACKEND from settings. Must run before QApplication is created.

    An existing QT_MEDIA_BACKEND environment variable wins, so the backend can
    also be forced per launch: set QT_MEDIA_BACKEND=windows (or ffmpeg).
    """
    backend = get_media_backend()
    if backend != "auto" and not os.environ.get("QT_MEDIA_BACKEND"):
        os.environ["QT_MEDIA_BACKEND"] = backend


def find_ffmpeg(configured: Optional[str] = None) -> Optional[str]:
    """Return the ffmpeg executable path, or None if it can't be found.

    configured overrides the saved setting (used by the settings page to
    validate a path before it is saved). A configured path that doesn't exist
    returns None rather than silently falling back to PATH.
    """
    configured = get_ffmpeg_path() if configured is None else configured.strip()
    if configured:
        path = Path(configured)
        if path.is_dir():
            path = path / "ffmpeg.exe"
        return _resolve_shim(str(path)) if path.is_file() else None
    found = shutil.which("ffmpeg")
    return _resolve_shim(found) if found else None


def _resolve_shim(exe: str) -> str:
    """Follow a Scoop shim to the real ffmpeg.exe.

    A shim runs ffmpeg as a child process, so killing the shim (cancelling a
    proxy) would leave ffmpeg running and still writing the file.
    """
    shim = Path(exe).with_suffix(".shim")
    try:
        match = re.search(r'path\s*=\s*"?([^"\r\n]+)"?', shim.read_text(encoding="utf-8"))
    except OSError:
        return exe
    if match and Path(match.group(1)).is_file():
        return match.group(1)
    return exe


def _cache_key(source: Path) -> str:
    """Hash of the normalized source path and its modified time."""
    resolved = os.path.normcase(str(Path(source).resolve()))
    mtime = Path(source).stat().st_mtime_ns
    return hashlib.sha1(f"{resolved}|{mtime}".encode("utf-8")).hexdigest()[:24]


def proxy_path_for(source: Path) -> Path:
    return PROXY_DIR / f"{_cache_key(source)}.mp4"


def cached_proxy(source: Path) -> Optional[Path]:
    """The finished proxy for source if one is cached, else None."""
    try:
        path = proxy_path_for(source)
    except OSError:
        return None
    return path if path.is_file() and path.stat().st_size > 0 else None


def proxy_command(ffmpeg: str, source: Path, output: Path) -> list[str]:
    """ffmpeg arguments for a 720p H.264/AAC proxy (no shell, so spaces and
    non-ASCII characters in paths need no quoting)."""
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        "-i", str(source),
        # First video + optional first audio; skips timecode/data tracks that mp4 can't hold
        "-map", "0:v:0", "-map", "0:a:0?",
        # Never upscale; keep height even for yuv420p
        "-vf", rf"scale=-2:'trunc(min({PROXY_HEIGHT}\,ih)/2)*2'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        # 10-bit / 4:2:2 sources (ProRes, DNxHR) would otherwise give a profile few decoders support
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        str(output),
    ]


class ProxyJob(QObject):
    """Generates a proxy in a background ffmpeg QProcess (never blocks the UI).

    Writes to a .partial file and renames it on success, so an interrupted
    job never leaves a half-written proxy in the cache.
    """

    progress = Signal(float)  # 0.0 - 1.0, or -1.0 while the duration is unknown
    finished = Signal(str)  # proxy path
    failed = Signal(str)  # error message

    def __init__(self, source: Path, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.source = Path(source)
        self.output = proxy_path_for(self.source)
        self._partial = self.output.with_suffix(".partial.mp4")
        self._duration_us = 0
        self._stderr_tail = ""
        self._cancelled = False
        self._process = QProcess(self)
        self._process.readyReadStandardOutput.connect(self._on_stdout)
        self._process.readyReadStandardError.connect(self._on_stderr)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_process_error)

    def start(self, ffmpeg: str) -> None:
        PROXY_DIR.mkdir(parents=True, exist_ok=True)
        self._remove_partial()  # left over from a job that couldn't be cleaned up
        args = proxy_command(ffmpeg, self.source, self._partial)
        self._process.start(args[0], args[1:])

    def is_running(self) -> bool:
        return self._process.state() != QProcess.ProcessState.NotRunning

    def cancel(self) -> None:
        """Stop ffmpeg and discard the partial file (no signals are emitted)."""
        self._cancelled = True
        if self.is_running():
            if sys.platform == "win32":
                # Whole tree, in case ffmpeg is a wrapper that spawned the real one
                try:
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(self._process.processId())],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=3, creationflags=_NO_WINDOW, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._process.kill()
            self._process.waitForFinished(3000)
        self._remove_partial()

    def _remove_partial(self) -> None:
        try:
            self._partial.unlink(missing_ok=True)
        except OSError:
            pass

    def _on_stdout(self) -> None:
        text = bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace")
        for line in text.splitlines():
            key, _, value = line.partition("=")
            # out_time_ms is (despite its name) microseconds; out_time_us exists on newer builds only
            if key in ("out_time_us", "out_time_ms") and value.strip().isdigit() and self._duration_us:
                self.progress.emit(min(1.0, int(value) / self._duration_us))

    def _on_stderr(self) -> None:
        text = bytes(self._process.readAllStandardError()).decode("utf-8", "replace")
        if not self._duration_us:
            match = _DURATION_PATTERN.search(self._stderr_tail + text)
            if match:
                h, m, s = match.groups()
                self._duration_us = int((int(h) * 3600 + int(m) * 60 + float(s)) * 1_000_000)
        self._stderr_tail = (self._stderr_tail + text)[-4000:]
        if not self._duration_us:
            self.progress.emit(-1.0)

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        if self._cancelled:
            return
        if exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0 and self._partial.is_file():
            try:
                os.replace(self._partial, self.output)
            except OSError as exc:
                self._remove_partial()
                self.failed.emit(f"Couldn't save the preview: {exc}")
                return
            self.finished.emit(str(self.output))
            return
        self._remove_partial()
        lines = [ln for ln in self._stderr_tail.strip().splitlines() if ln.strip()]
        detail = "\n".join(lines[-3:]) if lines else f"ffmpeg exited with code {exit_code}"
        self.failed.emit(detail)

    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart and not self._cancelled:
            self.failed.emit("ffmpeg couldn't be started. Check the ffmpeg path in Settings > Video.")


def extract_poster_frame(source: Path) -> Optional[Path]:
    """Cached JPEG poster frame for a video, extracting it with ffmpeg if needed.

    Blocking: call from a background thread (the thumbnail worker pool).
    Returns None if ffmpeg is missing or the frame can't be decoded.
    """
    try:
        poster = POSTER_DIR / f"{_cache_key(source)}.jpg"
    except OSError:
        return None
    if poster.is_file() and poster.stat().st_size > 0:
        return poster
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return None
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
    partial = poster.with_suffix(".partial.jpg")
    # 1 s in skips black lead-in frames; clips shorter than that fall back to the first frame
    for seek in ("1", "0"):
        args = [
            ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-ss", seek, "-i", str(source),
            "-frames:v", "1", "-vf", rf"scale='min({POSTER_WIDTH}\,iw)':-2",
            "-f", "image2", "-c:v", "mjpeg", str(partial),
        ]
        try:
            subprocess.run(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=POSTER_TIMEOUT_SECONDS, creationflags=_NO_WINDOW, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            break
        if partial.is_file() and partial.stat().st_size > 0:
            os.replace(partial, poster)
            return poster
    try:
        partial.unlink(missing_ok=True)
    except OSError:
        pass
    return None


_VIDEO_SIZE_PATTERN = re.compile(r"Stream #.*Video:.*?(\d{2,5})x(\d{2,5})")


def probe_video(source: Path) -> Optional[tuple[int, int, int]]:
    """(duration in ms, width, height) read by ffmpeg, or None if ffmpeg is missing or can't read it.

    Blocking (a fraction of a second per file): call from a background thread.
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return None
    try:
        # With no output file ffmpeg prints the file's details and exits with an error code
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-i", str(source)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=POSTER_TIMEOUT_SECONDS, creationflags=_NO_WINDOW, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    info = result.stderr.decode("utf-8", errors="replace")
    duration, size = _DURATION_PATTERN.search(info), _VIDEO_SIZE_PATTERN.search(info)
    if duration is None:
        return None
    hours, minutes, seconds = duration.groups()
    ms = int((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)
    width, height = (int(size.group(1)), int(size.group(2))) if size else (0, 0)
    return ms, width, height


def cache_size_bytes() -> int:
    """Total size of cached proxies, poster frames and grid thumbnails."""
    total = 0
    for folder in (PROXY_DIR, POSTER_DIR, THUMB_DIR):
        if folder.is_dir():
            total += sum(f.stat().st_size for f in folder.iterdir() if f.is_file())
    return total


def clear_video_cache() -> None:
    """Delete all cached proxies, poster frames and grid thumbnails (files in use are skipped)."""
    for folder in (PROXY_DIR, POSTER_DIR, THUMB_DIR):
        if not folder.is_dir():
            continue
        for file in folder.iterdir():
            try:
                file.unlink()
            except OSError:
                pass
