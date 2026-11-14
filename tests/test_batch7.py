"""Batch 7: the browser extension (browser-extension/, core.browser_link, ui.browser_inbox).

The helper is tested as the browser runs it too: a process of its own, started through cmd with the extension's
origin as its argument, with framed JSON on its standard input."""

from __future__ import annotations

import base64
import http.server
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from core import browser_link, profiles, settings
from core.tag_repository import get_repository

ROOT = Path(__file__).resolve().parent.parent
PAGE = "https://www.artstation.com/artwork/abc"


def pump(qapp, condition, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def picture(fmt: str = "PNG", mode: str = "RGB", size=(64, 32), color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color + ((128,) if mode == "RGBA" else ())).save(buffer, fmt)
    return buffer.getvalue()


def data_url(data: bytes, kind: str = "png") -> str:
    return f"data:image/{kind};base64," + base64.b64encode(data).decode()


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    settings.set_scan_folders([root])
    settings.set_file_extensions([".png", ".jpg", ".jpeg"])
    return root


@pytest.fixture
def web(tmp_path):
    """A local site: a picture, one that needs a login (403), and the referrer each request came with."""
    png = picture(size=(300, 200))
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(self.headers.get("Referer", ""))
            path = self.path.split("?")[0]
            if path == "/private.png":
                self.send_response(403)
                self.end_headers()
                return
            if path not in ("/art/creature-sketch.png", "/originals/a.png"):
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", png, seen
    server.shutdown()
    server.server_close()


# ----------------------------------------------------------------------
# The helper's pieces
# ----------------------------------------------------------------------
def test_messages_are_framed_as_the_browser_frames_them():
    stream = io.BytesIO()
    browser_link.write_message(stream, {"op": "hello", "text": "ü"})
    raw = stream.getvalue()
    assert struct.unpack("=I", raw[:4])[0] == len(raw) - 4
    stream.seek(0)
    assert browser_link.read_message(stream) == {"op": "hello", "text": "ü"}
    assert browser_link.read_message(stream) is None  # (the browser closed it)
    with pytest.raises(ValueError):
        browser_link.read_message(io.BytesIO(struct.pack("=I", browser_link.MAX_MESSAGE + 1)))


def test_names_sites_and_bigger_versions():
    stem = browser_link.file_stem
    assert stem("https://cdn.example.com/art/creature-sketch_02.jpg?x=1", "A page") == "creature-sketch_02"
    assert stem("https://i.pinimg.com/736x/3f/a2/9b/3fa29b0c1d2e3f4a5b6c7d8e9f0a1b2c.jpg", "Orc Warrior | Pinterest") == "Orc Warrior Pinterest"
    assert stem("https://example.com/image.png", "Rocks: a study") == "Rocks a study"  # (a plain word: the title)
    assert stem("data:image/png;base64,AAAA", "") == "web picture"
    assert browser_link.clean_name("con") == "con_" and browser_link.clean_name('a<b>c?.  ') == "a b c"
    assert browser_link.site_name("https://www.artstation.com/artwork/x") == "artstation.com"
    assert browser_link.site_name("https://cdna.artstation.com:443/p/x.jpg") == "cdna.artstation.com"
    assert browser_link.site_name("") == "Other sites"
    art = "https://cdna.artstation.com/p/assets/images/images/012/345/678/large/someone-orc.jpg?1530000000"
    assert browser_link.bigger_versions(art) == [art.replace("/large/", "/4k/"), art]
    pin = "https://i.pinimg.com/236x/3f/a2/9b/pin.jpg"
    assert browser_link.bigger_versions(pin) == ["https://i.pinimg.com/originals/3f/a2/9b/pin.jpg"]
    assert browser_link.bigger_versions("https://example.com/a.jpg") == []


def test_types_the_library_shows_are_kept_others_converted():
    shown = [".png", ".jpg"]
    data = picture("PNG")
    assert browser_link.fit_type(data, ".png", shown) == (data, ".png")
    converted, ext = browser_link.fit_type(picture("WEBP"), ".webp", shown)
    assert ext == ".jpg" and Image.open(io.BytesIO(converted)).format == "JPEG"
    converted, ext = browser_link.fit_type(picture("WEBP", "RGBA"), ".webp", shown)
    assert ext == ".png" and Image.open(io.BytesIO(converted)).mode == "RGBA"  # (transparency kept)
    assert browser_link.fit_type(picture("WEBP"), ".webp", [".webp"])[1] == ".webp"


def test_save_to_library(library):
    data = picture()
    reply = browser_link.handle({"op": "save", "target": "library", "sources": [data_url(data)], "page": PAGE,
                                 "title": "Creature Concept, Someone"})
    saved = library / "Web Images" / "artstation.com" / "Creature Concept, Someone.png"
    assert reply["ok"] and Path(reply["path"]) == saved and saved.read_bytes() == data
    assert "Web Images\\artstation.com\\Creature Concept, Someone.png" in reply["message"]
    assert get_repository().web_page_of(saved) == PAGE  # (Open Source Page)
    # The same picture again: not a second copy
    again = browser_link.handle({"op": "save", "target": "library", "sources": [data_url(data)], "page": PAGE, "title": "Other"})
    assert again["ok"] and "Already in your library" in again["message"] and len(list(saved.parent.iterdir())) == 1
    # Another picture with the same name: numbered
    other = browser_link.handle({"op": "save", "target": "library", "sources": [data_url(picture(color=(1, 2, 3)))],
                                 "page": PAGE, "title": "Creature Concept, Someone"})
    assert Path(other["path"]).name == "Creature Concept, Someone (2).png"
    assert not list(saved.parent.glob("*.partial"))


def test_nothing_but_web_pictures_is_taken(library):
    reply = browser_link.handle({"op": "save", "target": "library", "page": PAGE,
                                 "sources": ["file:///C:/Windows/win.ini", "javascript:alert(1)", r"C:\Windows\win.ini"]})
    assert not reply["ok"] and "no picture" in reply["message"]
    reply = browser_link.handle({"op": "save", "target": "library", "sources": ["data:image/png;base64,aGVsbG8="], "page": PAGE})
    assert not reply["ok"] and "isn't an image" in reply["message"]
    assert not browser_link.handle({"op": "delete", "target": "library"})["ok"]
    assert not browser_link.handle({"op": "save", "target": "somewhere", "sources": [data_url(picture())]})["ok"]
    assert not (library / "Web Images").exists()


def test_each_profile_has_its_own_web_folder(tmp_path, library):
    assert settings.get_web_folder() == library / "Web Images"
    chosen = tmp_path / "Refs"
    settings.set_web_folder(str(chosen))
    other_library = tmp_path / "Other"
    other = profiles.create_profile("Other")
    profiles.switch_profile(other.id)
    settings.set_scan_folders([other_library])
    assert settings.get_web_folder() == other_library / "Web Images"  # (not the first profile's choice)
    browser_link.handle({"op": "save", "target": "library", "sources": [data_url(picture())], "page": PAGE, "title": "x pic"})
    assert (other_library / "Web Images" / "artstation.com" / "x pic.png").is_file()
    profiles.switch_profile(profiles.DEFAULT_ID)
    assert settings.get_web_folder() == chosen


def test_no_library_folder_says_so():
    reply = browser_link.handle({"op": "save", "target": "library", "sources": [data_url(picture())], "page": PAGE})
    assert not reply["ok"] and "Settings > Folders" in reply["message"]


def test_downloads_send_the_page_and_a_refusal_goes_to_the_browser(library, web):
    base, png, seen = web
    reply = browser_link.handle({"op": "save", "target": "library", "page": PAGE, "title": "t",
                                 "sources": [f"{base}/missing.png", f"{base}/art/creature-sketch.png"]})
    assert reply["ok"] and Path(reply["path"]).name == "creature-sketch.png" and PAGE in seen  # (the referrer)
    reply = browser_link.handle({"op": "save", "target": "library", "page": PAGE, "sources": [f"{base}/private.png"]})
    assert not reply["ok"] and reply["in_browser"] == f"{base}/private.png"  # (the extension downloads it itself)


def test_a_picture_the_browser_downloaded_is_moved_into_place(tmp_path, library):
    downloads = tmp_path / "Downloads" / browser_link.DOWNLOAD_FOLDER
    downloads.mkdir(parents=True)
    file = downloads / "1731600000000"
    data = picture(color=(9, 9, 9))
    file.write_bytes(data)
    reply = browser_link.handle({"op": "take", "target": "library", "path": str(file), "url": "https://site.com/p/locked-art.png",
                                 "page": "https://www.site.com/gallery", "title": "Gallery"})
    assert reply["ok"] and Path(reply["path"]) == library / "Web Images" / "site.com" / "locked-art.png"
    assert not file.exists() and not downloads.exists()  # (moved, and the browser's folder tidied)
    # Only from the extension's own download folder
    elsewhere = tmp_path / "secret.png"
    elsewhere.write_bytes(data)
    assert not browser_link.handle({"op": "take", "target": "library", "path": str(elsewhere)})["ok"]
    assert elsewhere.exists()


def test_artbook_pictures_wait_in_the_inbox_while_the_app_is_closed():
    reply = browser_link.handle({"op": "save", "target": "artbook", "sources": [data_url(picture())], "page": PAGE,
                                 "title": "Board ref"})
    assert reply["ok"] and "next opens" in reply["message"]
    reply = browser_link.handle({"op": "save", "target": "artbook", "sources": [data_url(picture("WEBP"), "webp")],
                                 "page": PAGE, "title": "Second"})
    taken = browser_link.take_inbox()
    assert [(ext, name) for _data, ext, name in taken] == [(".png", "Board ref"), (".webp", "Second")]
    assert browser_link.take_inbox() == [] and not list(browser_link.inbox().iterdir())


# ----------------------------------------------------------------------
# The helper as the browser runs it
# ----------------------------------------------------------------------
def _run_helper(tmp_path, messages):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".3d_asset_manager_config.json").write_text(json.dumps({"scan_folders": [str(tmp_path / "Library")]}))
    env = dict(os.environ, USERPROFILE=str(home), LOCALAPPDATA=str(tmp_path / "local"))
    line = subprocess.list2cmdline([sys.executable, str(ROOT / "main.py"),
                                    f"chrome-extension://{browser_link.EXTENSION_ID}/", "--parent-window=0"])
    payload = b""
    for message in messages:
        body = json.dumps(message).encode()
        payload += struct.pack("=I", len(body)) + body
    started = time.perf_counter()
    done = subprocess.run(f'cmd /d /c "{line}"', input=payload, capture_output=True, env=env, timeout=60)
    took = time.perf_counter() - started
    replies, out = [], done.stdout
    while len(out) >= 4:
        (size,) = struct.unpack("=I", out[:4])
        replies.append(json.loads(out[4:4 + size]))
        out = out[4 + size:]
    return replies, took


def test_the_helper_answers_the_browser_without_loading_the_window(tmp_path):
    replies, took = _run_helper(tmp_path, [
        {"op": "hello"},
        {"op": "save", "target": "library", "sources": [data_url(picture())], "page": PAGE, "title": "From the helper"},
    ])
    assert replies[0]["ok"] and replies[0]["app"] is False
    assert replies[1]["ok"] and (tmp_path / "Library" / "Web Images" / "artstation.com" / "From the helper.png").is_file()
    assert took < 5.0  # (about 0.2 s: Qt and numpy aren't loaded)


# ----------------------------------------------------------------------
# The app's end
# ----------------------------------------------------------------------
def test_the_running_app_hears_of_pictures(qapp):
    from ui.browser_inbox import BrowserLinkWorker

    from PySide6.QtCore import QObject, Slot

    heard = []

    class Receiver(QObject):
        @Slot(dict)
        def take(self, message):
            heard.append(message)

    receiver = Receiver()
    worker = BrowserLinkWorker()
    worker.arrived.connect(receiver.take)
    assert worker.start()
    try:
        reply = browser_link.ask_app({"op": "artbook"})
        assert reply is not None and reply["ok"]
        assert pump(qapp, lambda: heard == [{"op": "artbook"}])
        # Someone without the key gets nothing
        link = json.loads(browser_link.link_file().read_text())
        from multiprocessing.connection import Client
        from multiprocessing import AuthenticationError

        with pytest.raises((AuthenticationError, EOFError, OSError)):
            with Client(link["address"], family="AF_PIPE", authkey=b"wrong") as c:
                c.send({"op": "artbook"})
                c.recv()
        assert browser_link.ask_app({"op": "hello"}) is not None  # (and it still listens)
    finally:
        worker.stop()
    assert not browser_link.link_file().exists()
    assert browser_link.ask_app({"op": "hello"}) is None


def test_pictures_go_on_the_open_page(qapp, library):
    from tests.test_main_window import open_window

    window = open_window(qapp)
    try:
        assert window.browser_worker.running
        reply = browser_link.handle({"op": "save", "target": "artbook", "sources": [data_url(picture(size=(90, 60)))],
                                     "page": PAGE, "title": "Ref one"})
        assert reply == {"ok": True, "message": "Sent to the Artbook."}  # (no page yet to name)
        board = window.artbook_panel.board
        assert pump(qapp, lambda: len(board.items_list()) == 1)
        assert board.items_list()[0].name == "Ref one"
        name = window.artbook_panel.page.get("name") or "Untitled"
        assert window.status_label.text().startswith(f'Added 1 picture from the browser to "{name}".')
        assert browser_link.take_inbox() == []
        # Now there's a page: the reply names it
        reply = browser_link.handle({"op": "save", "target": "artbook", "sources": [data_url(picture(color=(5, 5, 5)))],
                                     "page": PAGE, "title": "Ref two"})
        assert reply["message"] == f'Added to the Artbook page "{name}".'
        assert pump(qapp, lambda: len(board.items_list()) == 2)
    finally:
        window.close()


def test_waiting_pictures_go_on_the_page_when_the_app_starts(qapp, library):
    browser_link.handle({"op": "save", "target": "artbook", "sources": [data_url(picture())], "page": PAGE, "title": "Waiting"})
    from tests.test_main_window import open_window

    window = open_window(qapp)
    try:
        assert pump(qapp, lambda: len(window.artbook_panel.board.items_list()) == 1)
        assert window.artbook_panel.board.items_list()[0].name == "Waiting"
    finally:
        window.close()


def test_set_up_and_remove(qapp):
    import winreg

    from ui import browser_inbox

    assert not browser_link.is_set_up()
    try:
        folder = browser_inbox.set_up()
        assert browser_link.is_set_up() and not browser_link.needs_refresh()
        manifest = json.loads((folder / "manifest.json").read_text())
        from core.version import APP_VERSION

        assert manifest["version"] == APP_VERSION and manifest["key"]
        assert set(manifest["icons"]) == {"16", "32", "48", "128"} and (folder / "icon128.png").is_file()
        assert (folder / "background.js").read_text(encoding="utf-8").count(browser_link.HOST_NAME) == 1
        host = browser_link.registered_manifest()
        assert host["allowed_origins"] == [f"chrome-extension://{browser_link.EXTENSION_ID}/"]
        assert host["name"] == browser_link.HOST_NAME and host["type"] == "stdio"
        assert Path(host["path"]).name == "asset-manager-helper.bat"  # (from source: a .bat runs main.py)
        # Another copy of the app (or an older one) set it up: this one takes over
        (folder / "manifest.json").write_text(json.dumps({**manifest, "version": "0.0.1"}))
        assert browser_link.needs_refresh() and browser_inbox.refresh_if_needed() and not browser_link.needs_refresh()
    finally:
        browser_link.remove()
    assert not browser_link.is_set_up() and browser_link.registered_manifest() is None
    with pytest.raises(OSError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, browser_link.REGISTRY_KEY)


def test_the_extension_matches_the_helper():
    """The key in the manifest gives the ID the helper allows, and both name the same helper and folder."""
    import hashlib

    manifest = json.loads((ROOT / "browser-extension" / "manifest.json").read_text())
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    assert "".join(chr(ord("a") + int(c, 16)) for c in digest) == browser_link.EXTENSION_ID
    assert manifest["manifest_version"] == 3
    assert set(manifest["permissions"]) == {"contextMenus", "nativeMessaging", "activeTab", "scripting", "downloads"}
    assert "host_permissions" not in manifest  # (no "read all your data on all websites")
    script = (ROOT / "browser-extension" / "background.js").read_text(encoding="utf-8")
    assert f'const HOST = "{browser_link.HOST_NAME}"' in script
    assert f'const DOWNLOAD_FOLDER = "{browser_link.DOWNLOAD_FOLDER}"' in script


def test_the_settings_page(qapp, tmp_path, library):
    from ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    try:
        page = dialog.page_browser
        assert page.web_folder() == "" and page.txt_folder.placeholderText() == str(library / "Web Images")
        assert "Each site" in page.lbl_folder.label.text()
        assert not page.steps.isVisibleTo(page) and page.btn_set_up.text() == "Set Up"
        page.txt_folder.setText(str(tmp_path / "Elsewhere"))
        assert "isn't in one of this profile's library folders" in page.lbl_folder.label.text()
        page.txt_folder.setText(str(library / "Refs"))
        page._on_set_up()
        assert browser_link.is_set_up() and page.steps.isVisibleTo(page) and page.btn_set_up.text() == "Set Up Again"
        assert "hasn't reached the app yet" in page.lbl_status.label.text()
        page._on_copy()
        assert qapp.clipboard().text() == str(browser_link.extension_folder())
        dialog._on_save()
        assert settings.get_web_folder() == library / "Refs"
    finally:
        browser_link.remove()
        dialog.close()


def test_open_source_page_in_the_menu(qapp, library, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QWidget

    from core.scanner import AssetItem, determine_asset_group
    from ui.asset_menu import ArtbookSender, AssetMenuActions, build_asset_menu

    reply = browser_link.handle({"op": "save", "target": "library", "sources": [data_url(picture())], "page": PAGE, "title": "Menu pic"})
    path = Path(reply["path"])
    item = AssetItem(path, path.name, ".png", path.stat().st_size, 1.0, determine_asset_group(".png"), "")
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    actions = AssetMenuActions(lambda: None, lambda: None, lambda: None, lambda: None, lambda _r: None,
                               source_page=get_repository().web_page_of(path))
    parent = QWidget()

    class NoPanel:
        def current_page_id(self):
            return None

    menu = build_asset_menu(parent, item, [item], [], actions, ArtbookSender(NoPanel(), lambda _t: None))
    found = [a for a in menu.actions() if a.text() == "Open Source Page"]
    assert found and PAGE in found[0].toolTip()
    found[0].trigger()
    assert opened == [PAGE]
    menu.deleteLater()
    parent.deleteLater()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node isn't installed")
def test_the_extension_with_stand_in_browser_apis():
    """background.js: its menu, the click (biggest srcset first), a refusal downloaded by the browser and handed
    over, and the note when the helper isn't set up (tests/browser_extension_harness.js)."""
    done = subprocess.run(["node", str(ROOT / "tests" / "browser_extension_harness.js"), str(ROOT / "browser-extension" / "background.js")],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "ALL OK" in done.stdout, done.stdout + done.stderr
