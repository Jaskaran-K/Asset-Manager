"""The browser extension's link to the app (browser-extension/, for Chrome and Opera GX).

Right-click a picture on a web page > Asset Manager > Save to Library / Add to Artbook Page. An extension
can't touch files, so it hands the picture to this helper through the browser's "native messaging": the
browser starts AssetManager.exe for each picture (main.py spots the chrome-extension:// argument on its first
lines, before the window's modules load: 0.1 s, not 0.8), writes one message to its standard input and reads
the reply. Only the extension named in the registration may start it (EXTENSION_ID, fixed by the key in its
manifest): the browser checks that. No network port is opened. Opera GX reads Chrome's registration, so one
registry entry (REGISTRY_KEY, the current user's only: no admin rights) serves both.

Save to Library: the helper downloads the picture (with its page as the referrer, which gets past most
hot-link blocks: core.web_images), trying the extension's addresses best first (the picture's link, its
srcset's biggest, then the picture), with a site's known bigger versions ahead of them (bigger_versions). It
saves it in the profile's web folder (settings.get_web_folder: each profile has its own) in a folder per site
(artstation.com), under the picture's own name, or the page's title when that's a code. The page it came from
goes in the tag database (Open Source Page). The same picture already there isn't saved twice. A type the
profile doesn't show (WebP, AVIF...) is saved as PNG (with transparency) or JPG.

If the site refuses (401 / 403 / 429: a login), the reply says so and the extension downloads it with the
browser's own login, into DOWNLOAD_FOLDER of the browser's downloads folder, and hands over the file ("take").

Add to Artbook Page: the picture goes to INBOX, and the app is told; it puts it on the Artbook page that's
open (ui.browser_inbox). With the app closed it waits there for the next time the Artbook opens.

The app listens on a named pipe with a random key (LINK_FILE, written as it starts; core.model_reader's way)
only to hear that something arrived: the saving is all done here, so it works the same with the app closed.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Optional

from . import web_images

HOST_NAME = "com.assetmanager.browser"
EXTENSION_ID = "fdanbmlnahoojlfbplpkmhdjljckbhba"  # (browser-extension/manifest.json's key)
REGISTRY_KEY = rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"  # (Opera GX reads Chrome's)
DATA_ROOT = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AssetManager"
DOWNLOAD_FOLDER = "Asset Manager"  # (the extension's DOWNLOAD_FOLDER)
MAX_MESSAGE = 80 * 1024 * 1024  # the browser sends at most 64 MiB (a picture inside the page, as data)
MAX_SOURCES = 12
MAX_NAME = 80
APP_TIMEOUT = 3.0  # seconds for the app to answer "something arrived"
_BAD_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)$", re.IGNORECASE)
_CODE = re.compile(r"^[0-9a-z]{16,}$", re.IGNORECASE)  # a file name that's a code, not words
_PLAIN_NAMES = {
    "image", "images", "img", "photo", "picture", "download", "original", "originals", "large", "full", "media",
    "file", "index", "default", "thumb", "thumbnail", "preview", "raw", "max", "main", "4k", "unknown",
}


def folder() -> Path:
    """The helper's registration (host.json), the extension's copy and the last-used note."""
    return DATA_ROOT / "browser"


def inbox() -> Path:
    """Pictures for the Artbook, waiting for the app (each with a .json of its name and page)."""
    return DATA_ROOT / "browser-inbox"


def link_file() -> Path:
    """Where the running app says how to reach it: {"address", "key", "pid"}."""
    return DATA_ROOT / "browser-link.json"


def extension_folder() -> Path:
    """The extension the browser loads ("Load unpacked"): a copy kept up to date by the app (refresh)."""
    return folder() / "extension"


# ----------------------------------------------------------------------
# Native messaging: 4 bytes of length (this computer's byte order), then the message as UTF-8 JSON
# ----------------------------------------------------------------------
def read_message(stream: BinaryIO) -> Optional[dict[str, Any]]:
    """The next message, or None once the browser has closed the stream."""
    head = stream.read(4)
    if len(head) < 4:
        return None
    (size,) = struct.unpack("=I", head)
    if size > MAX_MESSAGE:
        raise ValueError("message too big")
    body = b""
    while len(body) < size:
        chunk = stream.read(size - len(body))
        if not chunk:
            return None
        body += chunk
    message = json.loads(body.decode("utf-8"))
    return message if isinstance(message, dict) else {}


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    body = json.dumps(message).encode("utf-8")
    stream.write(struct.pack("=I", len(body)) + body)
    stream.flush()


def _std_stream(fd: int, mode: str) -> BinaryIO:
    stream = sys.stdin if fd == 0 else sys.stdout
    if stream is not None and hasattr(stream, "buffer"):
        return stream.buffer
    # (the windowed exe may have no Python streams; the browser's pipes are still its standard handles)
    try:
        return os.fdopen(fd, mode, buffering=0)
    except OSError:
        import ctypes
        import msvcrt

        handle = ctypes.windll.kernel32.GetStdHandle(-10 if fd == 0 else -11)
        return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY if fd == 0 else os.O_WRONLY), mode, buffering=0)


def run_host() -> int:
    """The helper, as the browser starts it: answer messages until the browser closes the stream."""
    stdin, stdout = _std_stream(0, "rb"), _std_stream(1, "wb")
    sys.stdout = sys.stderr  # (a stray print would break the stream to the browser)
    while True:
        try:
            request = read_message(stdin)
        except (ValueError, OSError):
            return 1
        if request is None:
            return 0
        try:
            reply = handle(request)
        except Exception as exc:  # pylint: disable=broad-except
            _log(f"Browser helper: {type(exc).__name__}: {exc}")
            reply = {"ok": False, "message": f"Asset Manager couldn't do that: {exc}"}
        write_message(stdout, reply)


def _log(text: str) -> None:
    try:
        from . import crash_log

        crash_log.log_error(text)
    except Exception:  # pylint: disable=broad-except
        pass


def handle(request: dict[str, Any]) -> dict[str, Any]:
    op = request.get("op")
    _note_used()
    if op == "hello":
        from .version import APP_VERSION

        return {"ok": True, "version": APP_VERSION, "app": ask_app({"op": "hello"}) is not None}
    if op not in ("save", "take"):
        return {"ok": False, "message": "Asset Manager didn't understand that: update the app and the extension."}
    target = request.get("target")
    if target not in ("library", "artbook"):
        return {"ok": False, "message": "Asset Manager didn't understand where to put it."}
    page = request.get("page") if isinstance(request.get("page"), str) and web_images.is_web_url(request["page"]) else ""
    title = request.get("title") if isinstance(request.get("title"), str) else ""
    if op == "take":
        return _take(request, target, page, title[:300])
    raw = request.get("sources")
    sources = [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []
    sources = [s for s in sources if web_images.is_web_url(s) or s.startswith("data:image/")][:MAX_SOURCES]
    if not sources:
        return {"ok": False, "message": "There's no picture there to save."}
    tried: list[str] = []
    for source in sources:
        tried += [s for s in bigger_versions(source) if s not in tried and s not in sources] + [source]
    refused = ""
    problem = ""
    for source in tried:
        try:
            data, ext = web_images.fetch_image(source, page)
        except web_images.Refused as exc:
            refused = refused or source
            problem = problem or str(exc)
            continue
        except Exception as exc:  # pylint: disable=broad-except
            problem = problem or str(exc) or type(exc).__name__
            continue
        return deliver(target, data, ext, source, page, title[:300])
    if refused:
        return {"ok": False, "in_browser": refused, "message": problem}
    return {"ok": False, "message": f"Couldn't get the picture: {problem}"}


def bigger_versions(url: str) -> list[str]:
    """A site's bigger copies of a picture, best first, tried ahead of it (and skipped if they're not there)."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower()
    if host.endswith("artstation.com") and "/images/" in parts.path:
        # ArtStation: /4k/ (the upload, up to 3840 wide), then /large/ (1920)
        sizes = r"/(?:4k|large|medium|small|smaller_square|micro_square|small_square)/"
        return [urllib.parse.urlunsplit(parts._replace(path=re.sub(sizes, f"/{size}/", parts.path, count=1)))
                for size in ("4k", "large") if re.search(sizes, parts.path)]
    if host == "i.pinimg.com":
        # Pinterest: /originals/ is the upload (/236x/, /474x/, /736x/... are copies)
        path = re.sub(r"^/(?:\d+x\d*|\d+x\d+_RS)/", "/originals/", parts.path, count=1)
        return [urllib.parse.urlunsplit(parts._replace(path=path))] if path != parts.path else []
    return []


def _take(request: dict[str, Any], target: str, page: str, title: str) -> dict[str, Any]:
    """A picture the browser downloaded (the site refused the app): moved from the browser's downloads."""
    raw = request.get("path")
    path = Path(raw) if isinstance(raw, str) and raw else None
    if path is None or path.parent.name != DOWNLOAD_FOLDER or not path.is_file():
        return {"ok": False, "message": "The browser's download wasn't where Asset Manager looked for it."}
    try:
        if path.stat().st_size > web_images.MAX_DOWNLOAD_BYTES:
            return {"ok": False, "message": "That image is too big (over 64 MB)."}
        data = path.read_bytes()
        ext = web_images.image_ext(data)
        if ext is None:
            return {"ok": False, "message": "What the browser downloaded isn't a picture."}
        url = request.get("url") if isinstance(request.get("url"), str) else ""
        return deliver(target, data, ext, url, page, title)
    finally:
        try:
            path.unlink(missing_ok=True)
            path.parent.rmdir()  # (only if nothing else is in it)
        except OSError:
            pass


# ----------------------------------------------------------------------
# Saving
# ----------------------------------------------------------------------
def deliver(target: str, data: bytes, ext: str, url: str, page: str, title: str) -> dict[str, Any]:
    if target == "artbook":
        return _to_artbook(data, ext, url, page, title)
    return _to_library(data, ext, url, page, title)


def _to_library(data: bytes, ext: str, url: str, page: str, title: str) -> dict[str, Any]:
    from . import settings

    web_folder = settings.get_web_folder()
    if web_folder is None:
        return {"ok": False, "message": "Asset Manager has no library folder yet: add one in Settings > Folders."}
    site = site_name(page or url)
    target_dir = web_folder / site
    data, ext = fit_type(data, ext, settings.get_file_extensions())
    shown = f"{web_folder.name}\\{site}"
    same = same_file(target_dir, data)
    if same is not None:
        return {"ok": True, "path": str(same), "message": f"Already in your library: {shown}\\{same.name}"}
    target_dir.mkdir(parents=True, exist_ok=True)
    path = unique_path(target_dir, file_stem(url, title), ext)
    _write(path, data)
    if page:
        try:
            from .tag_repository import TagRepository, DB_PATH

            repo = TagRepository(DB_PATH)
            repo.set_web_page(path, page)
            repo.close_thread_connection()
        except Exception as exc:  # pylint: disable=broad-except
            _log(f"Browser helper: the source page wasn't recorded: {exc}")
    ask_app({"op": "library", "path": str(path)})
    return {"ok": True, "path": str(path), "message": f"Saved to your library: {shown}\\{path.name}"}


def _to_artbook(data: bytes, ext: str, url: str, page: str, title: str) -> dict[str, Any]:
    folder_path = inbox()
    folder_path.mkdir(parents=True, exist_ok=True)
    stem = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
    _write(folder_path / f"{stem}{ext}", data)
    (folder_path / f"{stem}.json").write_text(
        json.dumps({"name": file_stem(url, title), "page": page}), encoding="utf-8"
    )
    reply = ask_app({"op": "artbook"})
    if reply is None:
        return {"ok": True, "message": "Saved for the Artbook: it goes on your page when Asset Manager next opens."}
    name = reply.get("page") if isinstance(reply.get("page"), str) else ""
    return {"ok": True, "message": f'Added to the Artbook page "{name}".' if name else "Sent to the Artbook."}


def take_inbox() -> list[tuple[bytes, str, str]]:
    """The pictures waiting for the Artbook, oldest first, as (data, extension, name); taken out of the inbox.
    (The app: ui.browser_inbox.)"""
    folder_path = inbox()
    if not folder_path.is_dir():
        return []
    taken = []
    for meta in sorted(folder_path.glob("*.json")):
        pictures = [p for p in folder_path.glob(f"{meta.stem}.*") if p.suffix != ".json"]
        try:
            name = json.loads(meta.read_text(encoding="utf-8")).get("name", "")
        except (OSError, ValueError, AttributeError):
            name = ""
        for picture in pictures:
            try:
                taken.append((picture.read_bytes(), picture.suffix.lower(), str(name or "")))
                picture.unlink()
            except OSError:
                continue
        meta.unlink(missing_ok=True)
    return taken


def site_name(url: str) -> str:
    """artstation.com for https://www.artstation.com/...: the folder a site's pictures go in."""
    host = (urllib.parse.urlsplit(url).hostname or "") if web_images.is_web_url(url) else ""
    host = host.lower().removeprefix("www.")
    return clean_name(host) or "Other sites"


def clean_name(text: str) -> str:
    """text as a file or folder name: no characters Windows refuses, no reserved names, not too long."""
    text = _BAD_CHARACTERS.sub(" ", text)
    text = " ".join(text.split())[:MAX_NAME].strip(" .")
    if _RESERVED.match(text):
        text += "_"
    return text


def file_stem(url: str, title: str) -> str:
    """The picture's own name from its address, or the page's title when that's a code or a plain word."""
    stem = ""
    if web_images.is_web_url(url):
        last = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
        stem = clean_name(os.path.splitext(last)[0])
    plain = len(stem) < 3 or stem.casefold() in _PLAIN_NAMES or bool(_CODE.match(stem) and re.search(r"\d", stem))
    if plain:
        stem = clean_name(title) or stem or "web picture"
    return stem


def unique_path(target_dir: Path, stem: str, ext: str) -> Path:
    path = target_dir / f"{stem}{ext}"
    count = 2
    while path.exists():
        path = target_dir / f"{stem} ({count}){ext}"
        count += 1
    return path


def same_file(target_dir: Path, data: bytes) -> Optional[Path]:
    """A file in target_dir with exactly these bytes, if there is one."""
    if not target_dir.is_dir():
        return None
    digest = None
    with os.scandir(target_dir) as entries:
        for entry in entries:
            try:
                if not entry.is_file() or entry.stat().st_size != len(data):
                    continue
                digest = digest or hashlib.sha256(data).digest()
                if hashlib.sha256(Path(entry.path).read_bytes()).digest() == digest:
                    return Path(entry.path)
            except OSError:
                continue
    return None


def fit_type(data: bytes, ext: str, shown: list[str]) -> tuple[bytes, str]:
    """data as a type the profile's library shows: kept if it does, else PNG (with transparency) or JPG."""
    if ext in shown or (ext == ".jpg" and ".jpeg" in shown):
        return data, ext
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.seek(0)
        alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
        out = io.BytesIO()
        if alpha or (".jpg" not in shown and ".jpeg" not in shown):
            if ".png" not in shown:
                return data, ext  # (the profile shows neither: keep what it is)
            image.convert("RGBA" if alpha else "RGB").save(out, "PNG")
            return out.getvalue(), ".png"
        image.convert("RGB").save(out, "JPEG", quality=95)
        return out.getvalue(), ".jpg"


def _write(path: Path, data: bytes) -> None:
    """Written beside and swapped in, so the library never sees half a picture."""
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(data)
    os.replace(partial, path)


def _note_used() -> None:
    try:
        folder().mkdir(parents=True, exist_ok=True)
        (folder() / "last-used.json").write_text(json.dumps({"time": time.time()}), encoding="utf-8")
    except OSError:
        pass


def last_used() -> Optional[float]:
    """When the extension last reached the helper (seconds since 1970), if it ever has."""
    try:
        value = json.loads((folder() / "last-used.json").read_text(encoding="utf-8")).get("time")
    except (OSError, ValueError, AttributeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


# ----------------------------------------------------------------------
# Talking to the running app
# ----------------------------------------------------------------------
def ask_app(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Tell the running app (its reply), or None if it isn't running or doesn't answer in APP_TIMEOUT."""
    try:
        link = json.loads(link_file().read_text(encoding="utf-8"))
        address, key = str(link["address"]), bytes.fromhex(str(link["key"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    result: list[Any] = []

    def talk() -> None:
        from multiprocessing.connection import Client

        try:
            with Client(address, family="AF_PIPE", authkey=key) as connection:
                connection.send(message)
                if connection.poll(APP_TIMEOUT):
                    result.append(connection.recv())
        except Exception:  # pylint: disable=broad-except
            pass  # (not running: its pipe is gone)

    thread = threading.Thread(target=talk, daemon=True)
    thread.start()
    thread.join(APP_TIMEOUT + 1.0)
    reply = result[0] if result else None
    return reply if isinstance(reply, dict) else None


class AppLink:
    """The app's end: a named pipe with a random key, written to link_file() for the helper. handler(message)
    runs on the listening thread and returns the reply (ui.browser_inbox hands it to the UI thread)."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.key = os.urandom(32)
        self.address = rf"\\.\pipe\AssetManager-browser-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._listener = None
        self._stopped = False

    def start(self) -> None:
        from multiprocessing.connection import Listener

        self._listener = Listener(self.address, family="AF_PIPE", authkey=self.key)
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        partial = link_file().with_suffix(".partial")
        partial.write_text(json.dumps({"address": self.address, "key": self.key.hex(), "pid": os.getpid()}), encoding="utf-8")
        os.replace(partial, link_file())
        threading.Thread(target=self._serve, name="browser-link", daemon=True).start()

    def _serve(self) -> None:
        while not self._stopped:
            try:
                connection = self._listener.accept()
            except Exception:  # pylint: disable=broad-except
                if self._stopped:
                    return
                continue  # (a wrong key, or a caller that went away)
            try:
                with connection:
                    if connection.poll(APP_TIMEOUT):
                        message = connection.recv()
                        reply = self.handler(message if isinstance(message, dict) else {})
                        connection.send(reply if isinstance(reply, dict) else {})
            except Exception:  # pylint: disable=broad-except
                continue

    def stop(self) -> None:
        self._stopped = True
        try:
            link = json.loads(link_file().read_text(encoding="utf-8"))
            if link.get("address") == self.address:
                link_file().unlink(missing_ok=True)
        except (OSError, ValueError, AttributeError):
            pass
        if self._listener is not None:
            try:
                from multiprocessing.connection import Client

                with Client(self.address, family="AF_PIPE", authkey=self.key):
                    pass  # (wakes accept() so the thread ends)
            except Exception:  # pylint: disable=broad-except
                pass
            try:
                self._listener.close()
            except Exception:  # pylint: disable=broad-except
                pass


# ----------------------------------------------------------------------
# Setting it up (Settings > Browser)
# ----------------------------------------------------------------------
def bundled_extension() -> Path:
    """The extension's files as shipped: beside the exe (PyInstaller's datas) or in the source folder."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "browser-extension"


def host_command() -> Path:
    """What the browser runs: the exe itself, or from source a .bat that runs main.py with this Python."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    script = Path(__file__).resolve().parent.parent / "main.py"
    batch = folder() / "asset-manager-helper.bat"
    folder().mkdir(parents=True, exist_ok=True)
    batch.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    return batch


def manifest() -> dict[str, Any]:
    return {
        "name": HOST_NAME,
        "description": "Asset Manager: saves pictures sent from the browser",
        "path": str(host_command()),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }


def set_up(icons: dict[int, bytes]) -> Path:
    """Register the helper for Chrome and Opera GX, and copy the extension (with these PNG icons, by size) to
    extension_folder(), the folder the browser loads. Returns that folder."""
    import winreg

    copy_extension(icons)
    host_file = folder() / "host.json"
    host_file.write_text(json.dumps(manifest(), indent=2), encoding="utf-8")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(host_file))
    return extension_folder()


def copy_extension(icons: dict[int, bytes]) -> None:
    from .version import APP_VERSION

    source, target = bundled_extension(), extension_folder()
    target.mkdir(parents=True, exist_ok=True)
    for name in ("background.js",):
        (target / name).write_bytes((source / name).read_bytes())
    data = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    data["version"] = ".".join(str(int(p)) for p in re.findall(r"\d+", APP_VERSION)[:4]) or "1.0"
    if icons:
        data["icons"] = {str(size): f"icon{size}.png" for size in sorted(icons)}
        for size, png in icons.items():
            (target / f"icon{size}.png").write_bytes(png)
    (target / "manifest.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_set_up() -> bool:
    """The helper is registered (by this app) and the extension's copy is there."""
    registered = registered_manifest()
    return registered is not None and registered.get("name") == HOST_NAME and (extension_folder() / "manifest.json").is_file()


def registered_manifest() -> Optional[dict[str, Any]]:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            path = winreg.QueryValueEx(key, "")[0]
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return None
    return data if isinstance(data, dict) else None


def needs_refresh() -> bool:
    """Set up, but by another copy of the app or an older version (the helper's path or the extension's files
    differ): set_up again, so the browser starts this one."""
    registered = registered_manifest()
    if registered is None:
        return False
    from .version import APP_VERSION

    try:
        copied = json.loads((extension_folder() / "manifest.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        copied = None
    wanted = ".".join(str(int(p)) for p in re.findall(r"\d+", APP_VERSION)[:4]) or "1.0"
    return registered.get("path") != str(host_command()) or copied != wanted


def remove() -> None:
    """Undo set_up: the registration and the extension's copy (the browser keeps showing the extension, broken,
    until it's removed there)."""
    import shutil
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY)
    except OSError:
        pass
    shutil.rmtree(folder(), ignore_errors=True)
