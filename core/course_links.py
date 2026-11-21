"""Lessons that are web addresses (core.courses: a lesson's "url").

- YouTube plays in the course window through YouTube's own embedded player (ui.course_web shows embed_page()); its
  player's API reports where it is, so progress, notes and the next lesson work as for a file.
- A link straight to a video file (an .mp4 on a server, a direct Dropbox link) plays in the app's own player.
- Anything else (Vimeo: Qt's web view can't decode the H.264 its player sends; course sites you log into) opens in
  your browser; you tick it done yourself.
- Titles (and Vimeo's lengths) come from the sites' public oEmbed answers (no login, nothing about you sent along).
- yt-dlp, if you've installed it yourself (it isn't part of the app), downloads a lesson for offline: video and sound
  joined into one HD .mp4 by ffmpeg, saved with the course; the lesson then plays that file. Downloading from YouTube
  is against YouTube's terms: it's your call, made by installing yt-dlp.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from core.scanner import VIDEO_EXTENSIONS

TIMEOUT = 10.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AssetManager"  # (a generic client: nothing about the user)
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)%")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DOWNLOAD_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"  # (up to 1080p, video and sound joined)


def is_web(text: str) -> bool:
    return isinstance(text, str) and text.strip().lower().startswith(("http://", "https://"))


def classify(url: str) -> dict[str, str]:
    """{"kind": "youtube" (with "id"), "vimeo" (with "id"), "file" (a video file's address) or "page", "site": its
    host}."""
    parts = urllib.parse.urlparse(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = parts.path or ""
    query = urllib.parse.parse_qs(parts.query)
    video_id = ""
    if host in ("youtube.com", "youtube-nocookie.com", "music.youtube.com"):
        if path == "/watch":
            video_id = (query.get("v") or [""])[0]
        else:
            found = re.match(r"^/(?:embed|shorts|live|v)/([^/?#]+)", path)
            video_id = found.group(1) if found else ""
    elif host == "youtu.be":
        video_id = path.strip("/").split("/")[0]
    if video_id and _YOUTUBE_ID.match(video_id):
        return {"kind": "youtube", "id": video_id, "site": "YouTube"}
    if host in ("vimeo.com", "player.vimeo.com"):
        found = re.search(r"/(?:video/)?(\d+)", path)
        if found:
            return {"kind": "vimeo", "id": found.group(1), "site": "Vimeo"}
    if Path(urllib.parse.unquote(path)).suffix.lower() in VIDEO_EXTENSIONS | {".m3u8"}:
        return {"kind": "file", "site": host}
    return {"kind": "page", "site": host}


def start_seconds(url: str) -> int:
    """A YouTube link's own start time (?t=90, ?t=1m30s), in seconds; 0 if none."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    value = (query.get("t") or query.get("start") or ["0"])[0]
    if value.isdigit():
        return int(value)
    found = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    if not found:
        return 0
    hours, minutes, seconds = (int(g or 0) for g in found.groups())
    return hours * 3600 + minutes * 60 + seconds


def name_from(url: str) -> str:
    """A lesson's name until its real title is known: the page's last part, else its site."""
    kind = classify(url)
    last = urllib.parse.unquote(urllib.parse.urlparse(url).path.rstrip("/").rpartition("/")[2])
    if kind["kind"] in ("youtube", "vimeo"):
        return f"{kind['site']} video"
    stem = Path(last).stem if last else ""
    return " ".join(re.sub(r"[_\-.+]+", " ", stem).split()) or kind["site"] or "Web lesson"


# ----------------------------------------------------------------------
# Titles (oEmbed)
# ----------------------------------------------------------------------
def _get_json(url: str) -> Optional[dict]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read(1_000_000).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


def details(url: str) -> dict[str, Any]:
    """{"title", "duration" (ms, when the site says), "thumbnail"} from the site's oEmbed (YouTube, Vimeo), else a
    page's <title>; {} if nothing could be read. Blocking: a worker thread."""
    kind = classify(url)
    quoted = urllib.parse.quote(url, safe="")
    endpoint = {"youtube": f"https://www.youtube.com/oembed?format=json&url={quoted}",
                "vimeo": f"https://vimeo.com/api/oembed.json?url={quoted}"}.get(kind["kind"])
    if endpoint:
        data = _get_json(endpoint) or {}
        out: dict[str, Any] = {}
        if isinstance(data.get("title"), str):
            out["title"] = data["title"].strip()
        if isinstance(data.get("duration"), (int, float)) and data["duration"] > 0:
            out["duration"] = int(data["duration"] * 1000)
        if isinstance(data.get("thumbnail_url"), str):
            out["thumbnail"] = data["thumbnail_url"]
        return out
    if kind["kind"] == "page":
        from core import web_images

        try:
            page = web_images.download(url, limit=2_000_000, timeout=TIMEOUT)[0].decode("utf-8", "replace")
            found = web_images.page_details(url, page)
        except Exception:  # (any page that can't be read: the address stays its name)
            return {}
        return {"title": found["title"]} if found.get("title") else {}
    return {}


# ----------------------------------------------------------------------
# yt-dlp (optional: only if you've installed it)
# ----------------------------------------------------------------------
def find_ytdlp() -> Optional[str]:
    """yt-dlp's program, if it's installed (on the PATH, or where winget / scoop / pip put it); else None."""
    found = shutil.which("yt-dlp")
    if found:
        return found
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    for candidate in (local / "Microsoft" / "WinGet" / "Links" / "yt-dlp.exe", home / "scoop" / "shims" / "yt-dlp.exe",
                      Path(os.environ.get("APPDATA") or home) / "Python" / "Scripts" / "yt-dlp.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def safe_file_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    return " ".join(name.split()).strip(" .")[:120] or "Lesson"


def download_command(exe: str, url: str, target: Path, ffmpeg: Optional[str]) -> list[str]:
    command = [exe, "--no-playlist", "--newline", "--no-part", "--no-mtime", "-f", DOWNLOAD_FORMAT,
               "--merge-output-format", "mp4", "--progress-template", "download:%(progress._percent_str)s",
               "-o", str(target.with_suffix("")) + ".%(ext)s"]
    if ffmpeg:
        command += ["--ffmpeg-location", str(Path(ffmpeg).parent)]
    return command + ["--", url]


class DownloadError(Exception):
    """yt-dlp couldn't download it (the message says why, for the user)."""


def download(url: str, folder: Path, name: str, progress: Optional[Callable[[int, int], None]] = None,
             should_stop: Optional[Callable[[], bool]] = None, exe: Optional[str] = None) -> Path:
    """Download a lesson with yt-dlp into folder as "<name>.mp4"; its path. progress(done, total) in thousandths of
    each file it downloads (the video, then the sound); raises DownloadError, or archives.Cancelled when stopped."""
    from core import archives
    from core.video_tools import find_ffmpeg

    exe = exe or find_ytdlp()
    if exe is None:
        raise DownloadError("yt-dlp isn't installed (winget install yt-dlp).")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{safe_file_name(name)}.mp4"
    process = subprocess.Popen(download_command(exe, url, target, find_ffmpeg()), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    tail: list[str] = []
    try:
        for raw in iter(process.stdout.readline, b""):
            if should_stop is not None and should_stop():
                process.kill()
                raise archives.Cancelled()
            line = raw.decode("utf-8", "replace").strip()
            tail = (tail + [line])[-6:]
            found = _PERCENT.search(line)
            if found and progress is not None:
                progress(int(float(found.group(1)) * 10), 1000)
        process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if process.returncode != 0 or not target.is_file():
        why = next((line for line in reversed(tail) if "ERROR" in line), "") or (tail[-1] if tail else "")
        raise DownloadError(why.replace("ERROR:", "").strip() or "yt-dlp couldn't download it (try yt-dlp -U to update it).")
    return target


# ----------------------------------------------------------------------
# YouTube's embedded player
# ----------------------------------------------------------------------
def embed_page(video_id: str, start: int = 0, autoplay: bool = True) -> str:
    """A page holding YouTube's embedded player (its IFrame API), filling the view. window.am reports where it is
    ({state, t, d, error}: seconds); am_play() / am_pause() / am_seek(seconds) drive it."""
    if not _YOUTUBE_ID.match(video_id):
        raise ValueError("not a YouTube video id")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; height: 100%; background: #000; overflow: hidden; }}
#player {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
</style></head><body><div id="player"></div><script>
window.am = {{state: -2, t: {int(start)}, d: 0, error: null}};
var player = null;
var tag = document.createElement('script');
tag.src = 'https://www.youtube.com/iframe_api';
document.body.appendChild(tag);
function onYouTubeIframeAPIReady() {{
  player = new YT.Player('player', {{
    width: '100%', height: '100%', host: 'https://www.youtube-nocookie.com', videoId: '{video_id}',
    playerVars: {{autoplay: {1 if autoplay else 0}, start: {int(start)}, rel: 0, playsinline: 1, modestbranding: 1,
                 fs: 1, origin: location.origin}},
    events: {{
      onReady: function (e) {{ window.am.state = -1; window.am.d = e.target.getDuration(); }},
      onStateChange: function (e) {{ window.am.state = e.data; }},
      onError: function (e) {{ window.am.error = e.data; }}
    }}
  }});
}}
setInterval(function () {{
  if (player && player.getCurrentTime) {{ window.am.t = player.getCurrentTime(); window.am.d = player.getDuration() || window.am.d; }}
}}, 400);
function am_play() {{ if (player && player.playVideo) player.playVideo(); }}
function am_pause() {{ if (player && player.pauseVideo) player.pauseVideo(); }}
function am_seek(s) {{ if (player && player.seekTo) player.seekTo(s, true); }}
</script></body></html>"""


YOUTUBE_ERRORS = {
    2: "YouTube says that address isn't a video.",
    5: "YouTube's player can't play this video here.",
    100: "That video is gone (deleted, or private).",
    101: "Its owner doesn't allow it to play outside YouTube.",
    150: "Its owner doesn't allow it to play outside YouTube.",
    153: "YouTube's player wouldn't start here.",
}
