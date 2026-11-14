"""The release build's self-test (main.py --self-test LOG): exercises the bundled libraries
without showing a window, so a build missing a library or a DLL fails before it ships."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QApplication

from core.settings import get_theme
from ui.icons import app_icon
from ui.settings_dialog import SettingsDialog
from ui.themes import apply_theme


def run_self_test(log_path: Optional[str]) -> int:
    """Exercise the bundled libraries without showing a window (used by the release build).

    Returns a process exit code; writes a short report to log_path if given, since the
    windowed executable has no console.
    """
    import tempfile
    import traceback

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Qt painting/fonts (used by thumbnails) need an application object first
    app = QApplication.instance() or QApplication(sys.argv[:1])
    lines: list[str] = []
    ok = True

    def step(name: str, fn) -> None:
        nonlocal ok
        try:
            detail = fn()
            lines.append(f"PASS  {name}" + (f": {detail}" if detail else ""))
        except Exception:  # pylint: disable=broad-except
            ok = False
            lines.append(f"FAIL  {name}\n{traceback.format_exc()}")

    def check_imports():
        import fbxloader, fbxloader.utils, OpenGL.GL, scipy.spatial, trimesh  # noqa: E401,F401
        import core.fbx_fast  # noqa: F401  (binary FBX: uses fbxloader's transforms)
        return f"trimesh {trimesh.__version__}"

    def check_models():
        import trimesh
        from core.model_loader import load_3d_model
        with tempfile.TemporaryDirectory() as tmp:
            results = []
            for ext in (".obj", ".glb", ".stl"):
                path = Path(tmp) / f"box{ext}"
                trimesh.creation.box().export(path)
                mesh = load_3d_model(path)
                if mesh.face_count != 12:
                    raise RuntimeError(f"{ext}: expected 12 faces, got {mesh.face_count}")
                results.append(ext)
            from core.thumbnail_manager import render_model_thumbnail
            if render_model_thumbnail(mesh.vertices, mesh.faces, mesh.normals, "STL", 110) is None:
                raise RuntimeError("rendering a model's thumbnail failed")
            return ", ".join(results) + ", rendered thumbnail"

    def check_viewer_lighting_and_uvs():
        # HDRI lighting (a panorama read and turned into what the shader needs), and a model's UVs
        import numpy as np
        import trimesh
        from core import environment
        from core.model_uvs import read_uvs
        pixels = np.full((256, 512, 3), 0.4, np.float32)
        pixels[40:44, 100:104] = 5000.0
        env = environment.build(pixels, "sky.hdr")
        if not env.lights or env.pixels.dtype != np.float16:
            raise RuntimeError("an HDRI's light wasn't found")
        with tempfile.TemporaryDirectory() as tmp:
            obj = Path(tmp) / "quad.obj"
            obj.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nf 1/1 2/2 3/3 4/4\n", encoding="utf-8")
            layout = read_uvs(obj)
            if layout is None or layout.edge_count != 4 or layout.tiles != [1001]:
                raise RuntimeError("reading an OBJ's UVs failed")
            glb = Path(tmp) / "box.glb"
            trimesh.creation.box().export(glb)
            read_uvs(glb)  # (no UVs in it: only that it reads)
        return f"{len(env.lights)} light from a panorama, OBJ UVs read"

    def check_render():
        # The Render button: the app's Python must be the one LuxCore's download is built for, and a
        # render's process must start up and say what became of it (here: LuxCore isn't there)
        import json
        from unittest import mock
        import numpy as np
        from core import render_engine, render_scene, render_worker
        if not render_engine.supported():
            raise RuntimeError(f"LuxCore's download is for Python {render_engine.PYTHON}: update core.render_engine")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            render_scene.RenderJob("none.obj", 64, 36, 1.0, False, [0, 0, 3], [0, 0, 0], [0, 1, 0]).save(folder)
            with mock.patch.object(render_engine, "is_installed", lambda: False):
                code = render_worker.run(folder)
            state = json.loads((folder / render_worker.STATUS_FILE).read_text(encoding="utf-8"))
            if code != 1 or state["phase"] != "failed":
                raise RuntimeError(f"a render with no engine ended as {state}")
            ply = folder / "mesh.ply"
            render_worker.write_ply(ply, np.zeros((3, 3), np.float32), np.array([[0, 1, 2]], np.uint32), np.zeros((3, 3), np.float32))
        return f"LuxCore {render_engine.ENGINE_VERSION} fits this Python; the worker reports back"

    def check_upscale():
        # The Artbook's Upscale: its modules are in the build, and a picture goes through it (the model
        # stood in by a resize: Real-ESRGAN is downloaded by the user, not shipped)
        from unittest import mock
        from PIL import Image
        from core import upscaler
        from ui import artbook_upscale  # noqa: F401 (the window: it must be in the build)

        def model(source, result, _model, _progress, _stop):
            with Image.open(source) as im:
                im.resize((im.width * 4, im.height * 4)).save(result)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(upscaler, "_root", lambda: Path(tmp)), \
                mock.patch.object(upscaler, "is_installed", lambda: True), mock.patch.object(upscaler, "_run_model", model):
            made = upscaler.upscale(Image.new("RGBA", (40, 20)), "photo", 2)
        if made.size != (80, 40):
            raise RuntimeError(f"an upscaled picture came out {made.size}")
        return f"Real-ESRGAN {upscaler.VERSION} downloads on first use; a picture goes through"

    def check_picture_looks():
        # Colours and look-alikes (core.picture_look): a picture and a resized copy of it are read, from
        # files, and found alike; its main colour is named
        import numpy as np
        from PIL import Image
        from core.picture_look import closeness, parse_color, read_look

        rng = np.random.default_rng(4)
        picture = Image.fromarray((rng.random((8, 8, 3)) * 255).astype(np.uint8)).resize((256, 256), Image.Resampling.BICUBIC)
        red = Image.new("RGB", (64, 64), (214, 40, 30))
        red.paste((0, 0, 0), (0, 0, 64, 8))
        with tempfile.TemporaryDirectory() as tmp:
            picture.save(Path(tmp) / "a.png")
            picture.resize((128, 128)).save(Path(tmp) / "b.jpg", quality=70)
            red.save(Path(tmp) / "red.png")
            a, b, c = (read_look(str(Path(tmp) / name)) for name in ("a.png", "b.jpg", "red.png"))
        if closeness(a, b) is None:
            raise RuntimeError("a resized copy wasn't found alike")
        if not parse_color("red").matches(c):
            raise RuntimeError(f"a red picture's colours came out {c.colors}")
        return "colours and look-alikes are read"

    def check_texture_sets():
        # Texture sets, their material request (the helpers' scripts compile), and a conversion
        import numpy as np
        from PIL import Image
        from core import app_helpers, batch_convert
        from core.scanner import AssetItem
        from core.texture_sets import find_sets
        from ui import batch_dialogs, material_view  # noqa: F401 (the windows: they must be in the build)

        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for kind, color in (("BaseColor", (200, 80, 40)), ("Normal", (128, 128, 255)), ("ORM", (255, 128, 0))):
                path = Path(tmp) / f"Crate_{kind}.png"
                Image.new("RGB", (64, 64), color).save(path)
                files.append(AssetItem(path, path.name, ".png", path.stat().st_size, 0.0, "Textures", "PNG"))
            sets = find_sets(files)
            if len(sets) != 1 or sets[0].roles != ["color", "normal", "packed"]:
                raise RuntimeError(f"the texture set came out {[(s.name, s.roles) for s in sets]}")
            request = app_helpers.material_request(sets[0], {str(a.path): a.path for a in files})
            for app_id in ("blender", "maya"):
                compile(app_helpers.helper_source(app_id), app_id, "exec")
            options = batch_convert.ConvertOptions("tga", 32)
            row = batch_convert.plan(files[:1], options).jobs[0]
            batch_convert.convert(row, options)
            with Image.open(row.target) as made:
                if made.size != (32, 32):
                    raise RuntimeError(f"a converted picture came out {made.size}")
        return f"a set of {len(request['maps'])} maps found, sent as a material, converted"

    def check_ai_tools():
        # The AI tools (core.ai_helper): the helper program (this app again, with --ai-helper) must start and
        # answer through its pipe, from the build as it ships. The stand-in engine answers (the models are
        # downloaded by the user, not shipped); with AI Search downloaded, the real model answers too.
        from unittest import mock

        import numpy as np
        from PIL import Image

        from core import ai_helper, ai_search, ai_tools
        from ui import ai_download, artbook_upscale, batch_dialogs  # noqa: F401 (the windows: they must be in the build)

        with mock.patch.dict(os.environ, {ai_helper.ENGINE_ENV: "fake"}):
            try:
                pixels = np.zeros((2, ai_helper.PICTURE_SIDE, ai_helper.PICTURE_SIDE, 3), np.uint8)
                pixels[1, ..., 0] = 255
                vectors = ai_helper.picture_vectors(pixels)
                red = ai_helper.text_vector("red")
                picture = Image.new("RGB", (60, 40), (20, 20, 20))
                picture.paste((230, 230, 230), (20, 10, 40, 30))
                cut = ai_search.cut_out(picture)
                pid = ai_helper._reader().request({"op": "ping"}, 30)["pong"]  # pylint: disable=protected-access
            finally:
                ai_helper.close()
        if vectors.shape != (2, ai_helper.VECTOR_SIZE) or not (vectors[1].astype(np.float32) @ red > vectors[0].astype(np.float32) @ red):
            raise RuntimeError("the AI helper's vectors came back wrong")
        if pid == os.getpid() or cut.getpixel((2, 2))[3] != 0 or cut.getpixel((30, 20))[3] != 255:
            raise RuntimeError("the AI helper's cut-out came back wrong")
        if ai_tools.is_installed("search"):
            try:
                ai_helper.text_vector("a skull")
                device = ai_helper.last_device.get("search")
            finally:
                ai_helper.close()
            return f"the helper answers; AI Search ran on the {device}"
        return f"the helper answers; ONNX Runtime {ai_tools.RUNTIME_VERSION} and the models download on first use"

    def check_browser_link():
        # The browser extension's helper (core.browser_link): the browser starts this app through cmd with the
        # extension's origin and talks over its standard input and output, which the windowed build must have.
        # Run that way, in a temp home and data folder, it must answer and save a picture; the extension's files
        # must be in the build.
        import base64
        import io
        import json
        import struct
        import subprocess
        import time

        from PIL import Image

        from core import browser_link

        bundled = browser_link.bundled_extension()
        if not (bundled / "manifest.json").is_file() or not (bundled / "background.js").is_file():
            raise RuntimeError(f"the browser extension's files aren't in the build ({bundled})")
        command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve().parent / "main.py")]
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 30, 30)).save(buffer, "PNG")
        messages = [
            {"op": "hello"},
            {"op": "save", "target": "library", "page": "https://example.com/art", "title": "Self-test",
             "sources": ["data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()]},
        ]
        payload = b"".join(struct.pack("=I", len(b)) + b for b in (json.dumps(m).encode() for m in messages))
        with tempfile.TemporaryDirectory() as tmp:
            home, library = Path(tmp) / "home", Path(tmp) / "Library"
            home.mkdir()
            (home / ".3d_asset_manager_config.json").write_text(json.dumps({"scan_folders": [str(library)]}), encoding="utf-8")
            env = dict(os.environ, USERPROFILE=str(home), LOCALAPPDATA=str(Path(tmp) / "local"))
            line = subprocess.list2cmdline(command + [f"chrome-extension://{browser_link.EXTENSION_ID}/", "--parent-window=0"])
            started = time.perf_counter()
            done = subprocess.run(f'cmd /d /c "{line}"', input=payload, capture_output=True, env=env, timeout=120,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            took = time.perf_counter() - started
            replies, out = [], done.stdout
            while len(out) >= 4:
                (size,) = struct.unpack("=I", out[:4])
                replies.append(json.loads(out[4:4 + size]))
                out = out[4 + size:]
            saved = library / "Web Images" / "example.com" / "Self-test.png"
            if len(replies) != 2 or not replies[0].get("ok") or not replies[1].get("ok") or not saved.is_file():
                raise RuntimeError(f"the browser helper answered {replies!r} (exit {done.returncode}, {done.stderr[-300:]!r})")
        return f"the helper answers the browser and saves a picture ({took:.2f} s to start and answer)"

    def check_model_reader():
        # 3D models are read in a helper program (this app again, with --model-reader): it must start, read a
        # model and send it back, from the build as it ships
        from unittest import mock

        from core import model_reader

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {model_reader.READER_ENV: ""}), \
                mock.patch.object(model_reader, "_unavailable", False):  # (the real helper, whatever reads models here)
            cube = Path(tmp) / "cube.obj"
            cube.write_text(
                "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 0 0 1\nv 1 0 1\nv 1 1 1\nv 0 1 1\n"
                "f 1 2 3 4\nf 5 6 7 8\nf 1 2 6 5\nf 2 3 7 6\nf 3 4 8 7\nf 4 1 5 8\n", encoding="utf-8",
            )
            try:
                faces = model_reader.read_faces(cube)
                mesh = model_reader.read_mesh(cube)
                pid = model_reader.reader("viewer").request({"op": "ping", "path": ""}, 30)["pong"]
            finally:
                model_reader.close_all()
            if model_reader.in_process():
                raise RuntimeError("the model reader didn't start: models would be read in the app")
            if faces != 12 or mesh.face_count != 12 or pid == os.getpid():
                raise RuntimeError(f"the model reader read {faces} / {mesh.face_count} triangles of a cube")
        return f"a helper read a model (memory cap {model_reader.memory_limit() / 1024 ** 3:.0f} GB)"

    def check_images():
        from PIL import Image
        from core.thumbnail_manager import load_texture_thumbnail_image
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tex.png"
            Image.new("RGB", (64, 64), (200, 50, 50)).save(path)
            if load_texture_thumbnail_image(path, "PNG", 110) is None:
                raise RuntimeError("thumbnail generation failed")
            # EXR (the OpenEXR package) and Radiance HDR (read by core.hdr_image)
            import numpy as np
            import OpenEXR
            exr = Path(tmp) / "sky.exr"
            header = {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage}
            with OpenEXR.File(header, {"RGB": np.full((16, 32, 3), 2.0, np.float32)}) as f:
                f.write(str(exr))
            hdr = Path(tmp) / "sky.hdr"
            hdr.write_bytes(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 4 +X 4\n" + bytes((128, 64, 32, 129)) * 16)
            for path in (exr, hdr):
                if load_texture_thumbnail_image(path, path.suffix[1:].upper(), 110) is None:
                    raise RuntimeError(f"{path.suffix} thumbnail failed")
            return "PNG, EXR, HDR"

    def check_ui():
        apply_theme(get_theme(), app)
        # The Settings dialog's Tags page opens the library database: use a throwaway one,
        # so a self-test never upgrades or changes the user's real library
        import core.tag_repository as tag_repository
        with tempfile.TemporaryDirectory() as tmp:
            saved = tag_repository._repository
            tag_repository._repository = tag_repository.TagRepository(Path(tmp) / "self-test.db")
            try:
                dialog = SettingsDialog()
                dialog.close()
                dialog.deleteLater()
            finally:
                tag_repository._repository.close_thread_connection()
                tag_repository._repository = saved
        from PySide6.QtPdf import QPdfDocument  # noqa: F401
        if app_icon().isNull():
            raise RuntimeError("application icon missing from the build")
        return "settings dialog built"

    def check_tags():
        from core.tag_repository import TagRepository
        from core.scanner import AssetScanner
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.png").write_bytes(b"x")
            repo = TagRepository(Path(tmp) / "test.db")
            ids = repo.sync_assets(AssetScanner().scan(Path(tmp)))
            tag = repo.create_custom_tag("SelfTest")
            repo.add_tags(list(ids.values()), [tag.id])
            png = repo.find_tag("PNG")
            if repo.find_assets_by_tags([tag.id, png.id]) != set(ids.values()):
                raise RuntimeError("tag search returned the wrong files")
            version = repo.schema_version()
            repo.close_thread_connection()  # the temp folder can't be deleted while the DB is open
            return f"schema v{version}"

    def check_archives():
        # A zip with a Deflate64 image (method 9: Python's zipfile can't read those; the inflate64
        # package does), written by hand: listed, read back and checked
        import struct
        import zlib
        import inflate64
        from PIL import Image
        from core import archives
        import io
        picture = io.BytesIO()
        Image.new("RGB", (32, 32), (40, 160, 90)).save(picture, "PNG")
        data = picture.getvalue()
        deflater = inflate64.Deflater()
        packed = deflater.deflate(data) + deflater.flush()
        name, crc = b"preview.png", zlib.crc32(data)
        local = struct.pack("<4s5H3L2H", b"PK\x03\x04", 21, 0, 9, 0, 0x21, crc, len(packed), len(data), len(name), 0)
        central = struct.pack(
            "<4s6H3L5H2L", b"PK\x01\x02", 21, 21, 0, 9, 0, 0x21, crc, len(packed), len(data), len(name), 0, 0, 0, 0, 0, 0,
        )
        body = local + name + packed
        end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(central) + len(name), len(body), 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pack.zip"
            path.write_bytes(body + central + name + end)
            members = archives.read_listing(path).members
            if len(members) != 1 or archives.read_bytes(members[0]) != data:
                raise RuntimeError("reading a Deflate64 zip failed")
        # (RAR / 7z use the user's own 7-Zip: not part of the app, so only reported)
        return f"Deflate64 zip read; 7-Zip for RAR / 7z: {archives.find_seven_zip() or 'not installed'}"

    def check_app_files():
        # Blender saves its files with Zstandard (a module loaded only when one turns up), and the
        # icons on cards come through Windows' own API
        import struct
        from compression import zstd
        from core import app_icons, file_pictures
        block = b"TEST" + struct.pack("<i", 8 + 4) + bytes(16) + struct.pack("<ii", 1, 1) + bytes(4)
        packed = zstd.compress(b"BLENDER-v400" + block)
        if file_pictures.blend_picture(packed) is None:
            raise RuntimeError("reading a compressed .blend's picture failed")
        icon = app_icons.read_icon(sys.executable) if sys.platform == "win32" else None
        return f"compressed .blend read; this program's icon: {f'{icon.width()} px' if icon is not None else 'none'}"

    def check_video():
        from PySide6.QtMultimedia import QMediaFormat
        from ui.video_player import VideoPlayer
        formats = QMediaFormat().supportedFileFormats(QMediaFormat.ConversionMode.Decode)
        if not formats:
            raise RuntimeError("no multimedia backend: Qt can't decode any video format")
        player = VideoPlayer()
        player.stop()
        player.close()
        return f"{len(formats)} decodable container formats"

    step("imports", check_imports)
    step("3D model loading", check_models)
    step("HDRI lighting and UVs", check_viewer_lighting_and_uvs)
    step("rendering", check_render)
    step("upscaling", check_upscale)
    step("model reader", check_model_reader)
    step("AI tools", check_ai_tools)
    step("browser extension", check_browser_link)
    step("colours and look-alikes", check_picture_looks)
    step("texture sets and batch convert", check_texture_sets)
    step("image thumbnails", check_images)
    step("zip archives", check_archives)
    step("app files", check_app_files)
    step("user interface", check_ui)
    step("video playback", check_video)
    step("tag database", check_tags)

    lines.append("RESULT: " + ("PASS" if ok else "FAIL"))
    report = "\n".join(lines)
    if log_path:
        Path(log_path).write_text(report, encoding="utf-8")
    elif sys.stdout is not None:
        print(report)
    return 0 if ok else 1
