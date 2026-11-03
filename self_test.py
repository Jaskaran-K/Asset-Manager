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
    step("image thumbnails", check_images)
    step("zip archives", check_archives)
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
