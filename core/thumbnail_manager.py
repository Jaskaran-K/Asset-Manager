"""Asynchronous thumbnail loader and caching manager.

Solves:
1. High-resolution texture freezing: loads thumbnails in background QThreadPool workers.
2. 256 MB allocation limit: uses Pillow for streaming downsampled decoding without full-res buffers.
3. TIFF tag warnings: avoids libtiff parser bugs on null-terminated metadata tags.
4. Slow repeat launches: pictures are cached on disk (disk_cache_path), without their card,
   so a big texture is decoded once, not every session or theme.
5. Fast scrolling: the newest requests are made first, and jobs for cards that left the
   screen are skipped, so the cards you land on don't wait behind the ones passed.
6. Memory: only the most recently used thumbnails stay in memory (MAX_MEMORY_BYTES).
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

from PIL import Image
from PySide6.QtCore import (
    QObject,
    QPointF,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QImageReader,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtPdf import QPdfDocument

from . import alphas, app_icons, apps, archives, file_pictures
from .hdr_image import is_hdr, read_hdr_image
from .psd_reader import read_psd_composite
from .scanner import APP_FILE_EXTENSIONS, AssetItem
from .video_tools import extract_poster_frame

# Increase Qt's default image allocation limit from 256 MB to 2048 MB
QImageReader.setAllocationLimit(2048)

# Finished thumbnails are cached as PNGs in core.video_tools.THUMB_DIR (see disk_cache_path)
MAX_DISK_CACHE_BYTES = 512 * 1024 * 1024
_CACHE_VERSION = 2  # bump when thumbnail drawing changes, so old cached pictures aren't reused
_CACHE_PREFIX = f"v{_CACHE_VERSION}-"  # file names start with it; prune_disk_cache removes other versions'
# Thumbnails kept in memory (the most recently used); about 1,800 cards at 144 px, 570 at 256 px
MAX_MEMORY_BYTES = 150 * 1024 * 1024
STALE_SECONDS = 2.0  # a job whose card wasn't asked for in this long has scrolled away: skipped
_COMPANION_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")  # model.png next to model.fbx
# 3D models without a companion picture get one rendered (core.model_thumbnail), one at a time.
# Bigger files only when they're opened in the viewer (it has loaded them already): reading a
# 300 MB sculpt takes seconds and about a GB of memory, too much just for scrolling past it.
MAX_RENDERED_MODEL_BYTES = 150 * 1024 * 1024

# Format-specific color palettes for badges
PALETTE_MAP = {
    "FBX": (QColor(59, 130, 246), QColor(29, 78, 216), QColor(30, 58, 138)),
    "OBJ": (QColor(245, 158, 11), QColor(217, 119, 6), QColor(146, 64, 14)),
    "glTF": (QColor(16, 185, 129), QColor(5, 150, 105), QColor(6, 95, 70)),
    "STL": (QColor(236, 72, 153), QColor(190, 24, 93), QColor(131, 24, 67)),
    "PNG": (QColor(52, 211, 153), QColor(16, 185, 129), QColor(6, 95, 70)),
    "JPEG": (QColor(251, 191, 36), QColor(217, 119, 6), QColor(146, 64, 14)),
    "TIFF": (QColor(96, 165, 250), QColor(37, 99, 235), QColor(30, 58, 138)),
    "PDF": (QColor(239, 68, 68), QColor(185, 28, 28), QColor(127, 29, 29)),
    "MP4": (QColor(249, 115, 22), QColor(234, 88, 12), QColor(154, 52, 18)),
    "MOV": (QColor(244, 63, 94), QColor(225, 29, 72), QColor(136, 19, 55)),
    "ZIP": (QColor(167, 139, 250), QColor(124, 58, 237), QColor(76, 29, 149)),
    "RAR": (QColor(232, 121, 249), QColor(192, 38, 211), QColor(112, 26, 117)),
    "7Z": (QColor(125, 211, 252), QColor(14, 165, 233), QColor(7, 89, 133)),
}


# Thumbnail card colors, set by the active UI theme (see ui.themes.apply_theme)
_CARD_STYLE = {
    "card": QColor(26, 28, 36),
    "border": QColor(46, 50, 64),
    "badge": QColor(18, 20, 26),
    "badge_text": QColor(255, 255, 255),
}


def set_thumbnail_card_style(card: QColor, border: QColor, badge: QColor, badge_text: QColor) -> None:
    """Set the card colors used for generated thumbnails (call clear_cache afterwards)."""
    _CARD_STYLE.update(card=QColor(card), border=QColor(border), badge=QColor(badge), badge_text=QColor(badge_text))


def open_image(file_path: Path, max_size: Optional[int] = None) -> Image.Image:
    """Open an image with Pillow, falling back to the built-in PSD reader.

    Pillow can't decode 16/32-bit PSDs (common for ZBrush alphas), so those go
    through core.psd_reader, and EXR / HDR through core.hdr_image. max_size lets
    those readers subsample for thumbnails.
    """
    if is_hdr(file_path):
        return read_hdr_image(file_path, max_size=max_size)
    try:
        return Image.open(file_path)
    except Exception:
        if file_path.suffix.lower() not in (".psd", ".psb"):
            raise
    return read_psd_composite(file_path, max_size=max_size)


def _safe_convert_pil_to_rgba(im: Image.Image) -> Image.Image:
    """Safely convert any PIL image mode (including 16-bit TIFFs, CMYK) to RGBA.

    For high-bit-depth modes (I;16, I, F) the pixel values are normalized
    into the 0-255 range *before* converting to L, which preserves smooth
    gradients instead of clipping everything above 255 to white.
    """
    mode = im.mode
    if mode == "RGBA":
        return im
    if mode == "RGB":
        return im.convert("RGBA")

    # 16-bit unsigned integer (0-65535) – common for 16-bit TIFFs
    if mode in ("I;16", "I;16L", "I;16B"):
        im_l = im.point(lambda p: p / 256.0).convert("L")
        return im_l.convert("RGBA")

    # 32-bit signed integer – range varies per image
    if mode == "I":
        lo, hi = im.getextrema()
        if hi > 255:
            scale = 255.0 / hi if hi > 0 else 1.0
            im_l = im.point(lambda p: p * scale).convert("L")
        else:
            im_l = im.convert("L")
        return im_l.convert("RGBA")

    # 32-bit float – range varies (often 0.0-1.0 or 0.0-65535.0)
    if mode == "F":
        lo, hi = im.getextrema()
        if hi <= 1.0:
            im_l = im.point(lambda p: p * 255.0).convert("L")
        elif hi > 255:
            scale = 255.0 / hi if hi > 0 else 1.0
            im_l = im.point(lambda p: p * scale).convert("L")
        else:
            im_l = im.convert("L")
        return im_l.convert("RGBA")

    try:
        return im.convert("RGBA")
    except Exception:
        return im.convert("RGB").convert("RGBA")


def card_style() -> dict[str, QColor]:
    """The current card colors, to hand to a background job (the theme can change while it runs)."""
    return {name: QColor(color) for name, color in _CARD_STYLE.items()}


def _draw_archive_box(painter: QPainter, center_x: float, center_y: float, size: float, colors: tuple) -> None:
    """A packed box with a zip down its front: the placeholder picture for archives."""
    lid_col, body_col, zip_col = colors
    w, h = size * 0.40, size * 0.30
    left, top = center_x - w / 2, center_y - h / 2
    painter.setPen(QPen(body_col.lighter(125), 1))
    painter.setBrush(QBrush(body_col))
    painter.drawRoundedRect(QRectF(left, top + h * 0.22, w, h * 0.78), 3, 3)
    painter.setPen(QPen(lid_col.lighter(125), 1))
    painter.setBrush(QBrush(lid_col))
    painter.drawRoundedRect(QRectF(left - w * 0.05, top, w * 1.10, h * 0.30), 3, 3)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(zip_col.lighter(160)))
    tooth = max(1.5, size * 0.022)
    y = top + h * 0.36
    while y < top + h - tooth:
        painter.drawRect(QRectF(center_x - tooth * (1.2 if int((y - top) / tooth) % 4 else 0.2), y, tooth, tooth * 0.7))
        y += tooth * 1.4


def _draw_file_sheet(painter: QPainter, center_x: float, center_y: float, size: float, colors: tuple) -> None:
    """A sheet with a folded corner: the picture for an app file whose app isn't on this computer."""
    light, body, dark = colors
    w, h = size * 0.30, size * 0.38
    left, top = center_x - w / 2, center_y - h / 2
    fold = w * 0.34
    painter.setPen(QPen(body.lighter(125), 1))
    painter.setBrush(QBrush(body))
    painter.drawPolygon(QPolygonF([
        QPointF(left, top), QPointF(left + w - fold, top), QPointF(left + w, top + fold),
        QPointF(left + w, top + h), QPointF(left, top + h),
    ]))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(dark))
    painter.drawPolygon(QPolygonF([
        QPointF(left + w - fold, top), QPointF(left + w, top + fold), QPointF(left + w - fold, top + fold),
    ]))
    painter.setBrush(QBrush(light))
    for line in (0.52, 0.66, 0.80):  # lines of "text"
        painter.drawRect(QRectF(left + w * 0.18, top + h * line, w * 0.64, max(1.0, size * 0.014)))


def render_placeholder_image(
    asset: AssetItem, size: int = 110, style: Optional[dict[str, QColor]] = None, note: str = "", label: str = "",
    icon: Optional[QImage] = None,
) -> QImage:
    """Render a fast procedural placeholder card (thread-safe QImage). note: a line such as
    "No preview" under the picture, for files that can't be shown. label: the badge's text
    (default: the file type). icon: its app's icon, for an app file (core.app_icons)."""
    style = style or _CARD_STYLE
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)

    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    top_col, left_col, right_col = PALETTE_MAP.get(
        asset.category_name,
        (QColor(139, 92, 246), QColor(109, 40, 217), QColor(76, 29, 149)),
    )

    # Card background
    card_rect = QRectF(3, 3, size - 6, size - 6)
    painter.setBrush(QBrush(style["card"]))
    painter.setPen(QPen(style["border"], 1.2))
    painter.drawRoundedRect(card_rect, 8, 8)

    center_x = size / 2.0
    center_y = size * (0.34 if note else 0.40)
    cube_w = size * 0.22
    cube_h = size * 0.12
    cube_depth = size * 0.18

    if icon is not None:
        side = max(16, int(size * (0.44 if note else 0.56)))
        scaled = icon.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        painter.drawImage(QPointF(center_x - scaled.width() / 2, size * (0.32 if note else 0.40) - scaled.height() / 2), scaled)
    elif asset.opens_in_app:
        _draw_file_sheet(painter, center_x, size * (0.34 if note else 0.40), size, (top_col, left_col, right_col))
    elif asset.is_archive:
        _draw_archive_box(painter, center_x, center_y + size * 0.06, size, (top_col, left_col, right_col))
    else:
        _draw_cube(painter, center_x, center_y, cube_w, cube_h, cube_depth, (top_col, left_col, right_col))

    if note:
        note_color = QColor(style["badge_text"])
        note_color.setAlpha(170)
        painter.setPen(QPen(note_color))
        painter.setFont(QFont("Segoe UI", max(5, int(size * 0.065))))
        painter.drawText(QRectF(4, size * 0.60, size - 8, size * 0.13), Qt.AlignmentFlag.AlignCenter, note)

    # Badge pill
    badge_rect = QRectF(size * 0.15, size * 0.74, size * 0.70, size * 0.17)
    painter.setBrush(QBrush(style["badge"]))
    painter.setPen(QPen(top_col, 1.2))
    painter.drawRoundedRect(badge_rect, 4, 4)

    painter.setPen(QPen(style["badge_text"]))
    font = QFont("Segoe UI", max(5, int(size * 0.08)), QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, label or asset.category_name)

    painter.end()
    return img


def _draw_cube(painter: QPainter, center_x: float, center_y: float, cube_w: float, cube_h: float, cube_depth: float,
               colors: tuple) -> None:
    """The placeholder picture: an isometric cube in the file type's colours."""
    top_col, left_col, right_col = colors
    # Isometric Cube / Swatch
    painter.setBrush(QBrush(top_col))
    painter.setPen(QPen(top_col.lighter(130), 1))
    top_poly = QPolygonF([
        QPointF(center_x, center_y - cube_h),
        QPointF(center_x + cube_w, center_y),
        QPointF(center_x, center_y + cube_h),
        QPointF(center_x - cube_w, center_y),
    ])
    painter.drawPolygon(top_poly)

    painter.setBrush(QBrush(left_col))
    painter.setPen(QPen(left_col.lighter(120), 1))
    left_poly = QPolygonF([
        QPointF(center_x - cube_w, center_y),
        QPointF(center_x, center_y + cube_h),
        QPointF(center_x, center_y + cube_h + cube_depth),
        QPointF(center_x - cube_w, center_y + cube_depth),
    ])
    painter.drawPolygon(left_poly)

    painter.setBrush(QBrush(right_col))
    painter.setPen(QPen(right_col.lighter(120), 1))
    right_poly = QPolygonF([
        QPointF(center_x, center_y + cube_h),
        QPointF(center_x + cube_w, center_y),
        QPointF(center_x + cube_w, center_y + cube_depth),
        QPointF(center_x, center_y + cube_h + cube_depth),
    ])
    painter.drawPolygon(right_poly)


def load_texture_picture(file_path: Path, size: int = 110) -> QImage:
    """The image scaled to fit a card of this size (raises if it can't be read).

    Pillow decodes it (no Qt 256 MB limit), with high-bit-depth modes converted first."""
    with open_image(file_path, max_size=size) as im:
        return _fit_picture(im, size)


def _fit_picture(im: Image.Image, size: int) -> QImage:
    """An opened image scaled to fit a card of this size."""
    # Convert high-bit-depth modes to RGBA *before* thumbnailing,
    # because Pillow's BILINEAR resampling crashes on I;16 mode.
    rgba_im = _safe_convert_pil_to_rgba(im)

    # Downscale preserving aspect ratio
    rgba_im.thumbnail((size - 16, size - 32), Image.Resampling.BILINEAR)
    rgba_im.load()
    inner_w, inner_h = rgba_im.size
    raw_bytes = rgba_im.tobytes()
    return QImage(raw_bytes, inner_w, inner_h, QImage.Format.Format_RGBA8888).copy()


def load_member_image(member: "archives.ArchiveMember", size: int = 110) -> QImage:
    """An image inside an archive scaled to fit a card (raises if it can't be read): decoded from
    memory, or (EXR, HDR, 16-bit PSD: their readers want a file) from a temporary copy."""
    if not is_hdr(member.base_name):
        data = archives.read_bytes(member)
        try:
            with Image.open(io.BytesIO(data)) as im:
                return _fit_picture(im, size)
        except Image.DecompressionBombError:
            raise
        except Exception:  # pylint: disable=broad-except
            if not member.base_name.lower().endswith((".psd", ".psb")):
                raise
    with archives.local_file(member) as path:
        return load_texture_picture(path, size)


def load_relief_picture(source, name: str, size: int = 110) -> Optional[QImage]:
    """An alpha's picture as a lit relief, from its full-precision heights, to fit a card; None if the
    image isn't an alpha (core.alphas). source: a file, or its bytes (not PSD / EXR)."""
    height = alphas.read_height(source, name, max(16, size - 16))
    if height is None:
        return None
    with alphas.relief_image(height) as im:
        return _fit_picture(im, size)


def load_member_picture(member: "archives.ArchiveMember", size: int, relief: bool) -> tuple[QImage, bool]:
    """An image inside an archive to fit a card: (picture, whether it's a relief). With relief, an
    alpha is drawn as one; anything else as itself."""
    if relief:
        name = member.base_name
        if name.lower().endswith((".psd", ".psb", ".exr")):
            with archives.local_file(member) as path:
                picture = load_relief_picture(path, name, size)
        else:
            picture = load_relief_picture(io.BytesIO(archives.read_bytes(member)), name, size)
        if picture is not None:
            return picture, True
    return load_member_image(member, size), False


def load_app_file_picture(asset: AssetItem, size: int = 110, relief: bool = False) -> Optional[QImage]:
    """A picture for a file only its app can open, to fit a card: the render a .blend holds, the picture
    a PureRef board holds, or else a picture beside the file that's clearly of it (a brush's alpha:
    drawn as a relief, with relief). None: there's none, and the card shows the app's icon."""
    extension = asset.extension.lower()
    if extension in (".blend", ".pur"):
        head = file_pictures.file_head(asset)
        image = file_pictures.blend_picture(head) if extension == ".blend" else file_pictures.board_picture(head)
        if image is None:
            return None
        # (These are small, so they're also made bigger to fill the card)
        scale = min((size - 16) / image.width, (size - 32) / image.height)
        if scale > 1:
            image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.BICUBIC)
        return _fit_picture(image, size)
    if asset.in_archive:
        member = file_pictures.member_pack_picture(asset.archive)
        return load_member_picture(member, size, relief)[0] if member is not None else None
    path = file_pictures.pack_picture(asset.path)
    if path is None:
        return None
    picture = load_relief_picture(path, path.name, size) if relief else None
    return picture if picture is not None else load_texture_picture(path, size)


def load_archive_picture(asset: AssetItem, size: int = 110) -> tuple[Optional[QImage], str]:
    """An archive's card picture: the best image inside (core.archives.card_picture_candidates),
    or None and a note: what's inside ("MP4, SRT"), or why it can't be read."""
    result = archives.listing(asset.path, asset.size_bytes, asset.modified_timestamp)
    if result.error:
        return None, result.error if len(result.error) <= 32 else "Can't be read"
    for member in archives.card_picture_candidates(result)[:3]:
        try:
            return load_member_image(member, size), ""
        except Exception:  # pylint: disable=broad-except
            continue  # (the next best, if any)
    summary = archives.summarize(result)
    return None, ", ".join(kind for kind, _count in summary.by_type[:3]) or "Empty"


def archive_label(asset: AssetItem) -> str:
    """An archive card's badge: its type and how many files it holds ("ZIP · 124"). Blocks (call
    off the UI thread)."""
    return archives.summarize(archives.listing(asset.path, asset.size_bytes, asset.modified_timestamp)).badge(asset.category_name)


def load_texture_thumbnail_image(file_path: Path, category_name: str, size: int = 110) -> Optional[QImage]:
    """An image's thumbnail card (None if it can't be read)."""
    try:
        return compose_card(load_texture_picture(file_path, size), category_name, size)
    except Exception:  # pylint: disable=broad-except
        return None


def compose_card(
    inner_qimg: QImage, category_name: str, size: int, style: Optional[dict[str, QColor]] = None, label: str = ""
) -> QImage:
    """A picture centred in a styled card, with the file type's badge (fits (size - 16) x (size - 32)).
    label: the badge's text (default: the file type)."""
    style = style or _CARD_STYLE
    inner_w, inner_h = inner_qimg.width(), inner_qimg.height()
    card_img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    card_img.fill(Qt.GlobalColor.transparent)

    p = QPainter(card_img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    card_rect = QRectF(2, 2, size - 4, size - 4)
    p.setBrush(QBrush(style["card"]))
    p.setPen(QPen(style["border"], 1))
    p.drawRoundedRect(card_rect, 6, 6)

    # Center the picture
    dx = (size - inner_w) / 2.0
    dy = (size - 18 - inner_h) / 2.0 + 4
    p.drawImage(int(dx), int(dy), inner_qimg)

    # Format pill badge
    badge_rect = QRectF(size * 0.18, size - 20, size * 0.64, 15)
    badge_col = PALETTE_MAP.get(category_name, (QColor(16, 185, 129), None, None))[0]
    p.setBrush(QBrush(badge_col))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(badge_rect, 3, 3)

    p.setPen(QPen(Qt.GlobalColor.white))
    p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
    p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, label or category_name)

    p.end()
    return card_img


def render_model_picture(vertices, faces, normals, size: int) -> Optional[QImage]:
    """A 3D model's rendered picture, to fit a card of this size (thread-safe: CPU only)."""
    from .model_thumbnail import render_model

    return render_model(vertices, faces, normals, max(16, size - 32))


def render_model_thumbnail(vertices, faces, normals, category_name: str, size: int) -> Optional[QImage]:
    """A 3D model's rendered picture in its card (thread-safe: CPU only)."""
    picture = render_model_picture(vertices, faces, normals, size)
    return compose_card(picture, category_name, size) if picture is not None else None


def load_model_picture(file_path: Path, size: int = 110) -> Optional[QImage]:
    """Read a 3D model and render its picture (None if it can't be read)."""
    from .model_loader import load_3d_model

    try:
        mesh = load_3d_model(file_path)
    except Exception:  # pylint: disable=broad-except
        return None
    if mesh.stand_in:
        return None  # (no picture beats a picture of the stand-in cube)
    return render_model_picture(mesh.vertices, mesh.faces, mesh.normals, size)


def load_model_thumbnail_image(file_path: Path, category_name: str, size: int = 110) -> Optional[QImage]:
    """Read a 3D model and render its picture in its card (None if it can't be read)."""
    picture = load_model_picture(file_path, size)
    return compose_card(picture, category_name, size) if picture is not None else None


def load_video_picture(file_path: Path, size: int = 110) -> Optional[QImage]:
    """A video's poster frame (extracted and cached by ffmpeg), to fit a card (None without one)."""
    poster = extract_poster_frame(file_path)
    return load_texture_picture(poster, size) if poster is not None else None


def add_play_badge(card_img: QImage, size: int) -> QImage:
    """Draw the play badge of a video's card on it."""
    p = QPainter(card_img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    radius = max(8.0, size * 0.12)
    center = QPointF(size / 2.0, (size - 18) / 2.0 + 4)
    p.setBrush(QBrush(QColor(0, 0, 0, 150)))
    p.setPen(QPen(QColor(255, 255, 255, 200), 1.2))
    p.drawEllipse(center, radius, radius)
    tri = radius * 0.45
    p.setBrush(QBrush(QColor(255, 255, 255, 230)))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(QPolygonF([
        QPointF(center.x() - tri * 0.7, center.y() - tri),
        QPointF(center.x() + tri, center.y()),
        QPointF(center.x() - tri * 0.7, center.y() + tri),
    ]))
    p.end()
    return card_img


def load_video_thumbnail_image(file_path: Path, category_name: str, size: int = 110) -> Optional[QImage]:
    """Poster-frame thumbnail for a video with a play badge (None without a poster)."""
    try:
        picture = load_video_picture(file_path, size)
    except Exception:  # pylint: disable=broad-except
        return None
    return add_play_badge(compose_card(picture, category_name, size), size) if picture is not None else None


def load_pdf_thumbnail_image(file_path: Path, size: int = 110) -> Optional[QImage]:
    """Render page 0 of a PDF into a thumbnail QImage (a white card of its own: no theme colors)."""
    doc = QPdfDocument()
    try:
        doc.load(str(file_path))
        if doc.pageCount() > 0:
            rendered = doc.render(0, QSize(size - 16, size - 16))
            if not rendered.isNull():
                card_img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
                card_img.fill(Qt.GlobalColor.transparent)

                p = QPainter(card_img)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)

                card_rect = QRectF(2, 2, size - 4, size - 4)
                p.setBrush(QBrush(QColor(255, 255, 255)))
                p.setPen(QPen(QColor(180, 180, 180), 1))
                p.drawRoundedRect(card_rect, 6, 6)

                dx = (size - rendered.width()) / 2.0
                dy = (size - rendered.height()) / 2.0
                p.drawImage(int(dx), int(dy), rendered)

                # Red PDF tag badge
                badge_rect = QRectF(size - 38, 4, 32, 16)
                p.setBrush(QBrush(QColor(220, 38, 38)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(badge_rect, 3, 3)
                p.setPen(QPen(Qt.GlobalColor.white))
                p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
                p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, "PDF")

                p.end()
                return card_img
    except Exception:
        pass
    finally:
        doc.close()
    return None


# ----------------------------------------------------------------------
# Background Thread Pool Worker
# ----------------------------------------------------------------------
class _Request:
    """One thumbnail being made. While its card is on screen the grid keeps asking for it, which
    keeps last_asked fresh; a job whose card scrolled away is skipped when its turn comes."""

    __slots__ = ("key", "last_asked", "cancelled", "keep")

    def __init__(self, key: str, keep: bool = False):
        self.key = key
        self.last_asked = time.monotonic()  # (a float: written on the UI thread, read by the job)
        self.cancelled = False  # the cache was reset or the file changed: the result isn't wanted
        self.keep = keep  # made even if nobody is looking (the viewer's model picture)


class _WorkerSignals(QObject):
    ready = Signal(object, QImage)  # the _Request, its card
    skipped = Signal(object)  # the _Request: its card left the screen before the job's turn


def disk_cache_path(asset: AssetItem, size: int, relief: bool = False) -> Optional[Path]:
    """Where a thumbnail's picture is cached on disk, or None for types that aren't cached.

    The picture is stored without its card, so a theme change doesn't make every file be read
    again; the name covers everything the picture depends on (file, size, modified time,
    thumbnail size), so a changed file simply gets a new entry. For 3D models it's their
    rendered picture (a companion image isn't cached: it can change).
    """
    if not (
        asset.is_texture or asset.is_video or asset.is_3d_model or (asset.is_document and asset.extension == ".pdf")
        or asset.is_archive or asset.is_app_file
    ):
        return None
    from . import video_tools  # CACHE_ROOT can be redirected (tests)

    key = f"{os.path.normcase(str(asset.path))}|{asset.size_bytes}|{asset.modified_timestamp!r}|{size}|{_CACHE_VERSION}"
    if asset.archive is not None:
        key += f"|{asset.archive.crc}|{asset.archive.archive_size}"  # (its archive replaced by another)
    if relief:
        key += "|relief"  # an alpha drawn as a lit relief (core.alphas)
    return video_tools.THUMB_DIR / f"{_CACHE_PREFIX}{hashlib.sha1(key.encode('utf-8')).hexdigest()}.png"


def prune_disk_cache(max_bytes: int = MAX_DISK_CACHE_BYTES) -> int:
    """Delete cached pictures from older versions of the app, then the least recently used ones
    beyond max_bytes. Returns how many were removed."""
    from . import video_tools

    try:
        entries = [(e.stat().st_mtime, e.stat().st_size, e) for e in os.scandir(video_tools.THUMB_DIR) if e.is_file()]
    except OSError:
        return 0
    removed = 0
    current = []
    for entry in entries:
        if entry[2].name.startswith(_CACHE_PREFIX):
            current.append(entry)
            continue
        try:
            os.unlink(entry[2].path)  # (an older version's: never used again)
            removed += 1
        except OSError:
            pass
    total = sum(size for _mtime, size, _entry in current)
    if total <= max_bytes:
        return removed
    for _mtime, size, entry in sorted(current, key=lambda e: e[0]):  # least recently used first
        if total <= max_bytes * 0.8:  # leave headroom so pruning doesn't run on every launch
            break
        try:
            os.unlink(entry.path)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


class _ThumbnailRunnable(QRunnable):
    """Background task to decode image/texture and generate thumbnail."""

    def __init__(
        self, request: _Request, asset: AssetItem, size: int, cache_file: Optional[Path],
        style: dict[str, QColor], render_model: bool = False, relief: bool = False, alpha: Optional[bool] = None,
    ):
        """relief: an image that's an alpha is drawn as a lit relief; alpha: whether it is one, if
        known (the library's file details), else it's found out here."""
        super().__init__()
        self.request = request
        self.asset = asset
        self.size = size
        self.cache_file = cache_file
        self.style = style  # the card colors when it was asked for (taken on the UI thread)
        self.render_model = render_model  # a 3D model without a companion picture: render one
        self.label = ""  # the badge's text, if not the file type ("ZIP · 124")
        self.relief = relief and (asset.is_texture or asset.is_app_file)
        self.alpha = alpha
        self._relief_file: Optional[Path] = None  # where a relief picture is cached
        self._undecided = False  # whether it's an alpha is only known once it's read (inside an archive)
        self._made_relief = False
        self.signals = _WorkerSignals()

    def run(self) -> None:
        request = self.request
        if request.cancelled:
            return
        if not request.keep and time.monotonic() - request.last_asked > STALE_SECONDS:
            self.signals.skipped.emit(request)
            return
        if self.asset.is_archive and not self.asset.in_archive:
            self.label = archive_label(self.asset)
        if self.relief and self.asset.is_texture:
            self._choose_relief_cache()
        picture, note = self._from_disk(), ""
        if picture is None:
            picture, note = self._render()
            if picture is not None and not picture.isNull():
                if self._undecided:
                    self.cache_file = self._relief_file if self._made_relief else disk_cache_path(self.asset, self.size)
                self._to_disk(picture)
        self.signals.ready.emit(request, self._card(picture, note))

    def _choose_relief_cache(self) -> None:
        """An alpha's relief picture is cached apart from its plain one (so turning reliefs off and on
        needs no reading, and the plain pictures of everything else stay as they are)."""
        alpha = self.alpha
        if alpha is None and not self.asset.in_archive:
            alpha = alphas.detect(self.asset.path, self.asset.name)  # (from its header: milliseconds)
        if alpha is False:
            self.relief = False
            return
        self._relief_file = disk_cache_path(self.asset, self.size, relief=True)
        if alpha is True or self._relief_file is not None and self._relief_file.is_file():
            self.cache_file = self._relief_file
        else:
            self.cache_file, self._undecided = None, True  # (read first: a plain picture made before may be an alpha's)

    def _card(self, picture: Optional[QImage], note: str) -> QImage:
        if picture is None or picture.isNull():
            if self.asset.opens_in_app:  # (its app's icon says what it is: no "No preview" needed)
                icon = app_icons.icon_for(self.asset.extension)
                return render_placeholder_image(self.asset, self.size, self.style, note, self.label, icon)
            return render_placeholder_image(self.asset, self.size, self.style, note or "No preview", self.label)
        if self.asset.is_document:
            return picture  # (a PDF page comes as a whole card)
        card = compose_card(picture, self.asset.category_name, self.size, self.style, self.label)
        return add_play_badge(card, self.size) if self.asset.is_video else card

    def _from_disk(self) -> Optional[QImage]:
        if self.cache_file is None or not self.cache_file.is_file():
            return None
        image = QImage(str(self.cache_file))
        if image.isNull():
            return None
        try:
            os.utime(self.cache_file)  # mark as recently used for prune_disk_cache
        except OSError:
            pass
        return image

    def _to_disk(self, image: QImage) -> None:
        if self.cache_file is None:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            partial = self.cache_file.with_suffix(f".{threading.get_ident()}.partial")
            if image.save(str(partial), "PNG"):
                os.replace(partial, self.cache_file)
        except OSError:
            pass  # caching is only a speed-up

    def _render(self) -> tuple[Optional[QImage], str]:
        """(the picture, or None and why it can't be shown)."""
        asset, size = self.asset, self.size
        try:
            if asset.opens_in_app:
                return load_app_file_picture(asset, size, self.relief), ""
            if asset.in_archive:
                return self._render_member()
            if asset.is_archive:
                return load_archive_picture(asset, size)
            if asset.is_3d_model and not self.render_model:
                # Companion preview images (model.png, etc.)
                for preview_ext in _COMPANION_EXTENSIONS:
                    companion = asset.path.with_suffix(preview_ext)
                    if companion.exists():
                        return load_texture_picture(companion, size), ""
                return None, ""
            return self._render_file(asset.path), ""
        except Image.DecompressionBombError:
            return None, "Too large to preview"
        except archives.ArchiveError as exc:
            return None, "Password-protected" if "password" in str(exc) else "Damaged in archive"
        except Exception:  # pylint: disable=broad-except
            pass
        return None, ""

    def _render_file(self, path: Path) -> Optional[QImage]:
        """The picture of the file at path (the asset's own, or an unpacked copy of it)."""
        asset, size = self.asset, self.size
        if asset.is_texture and self.relief:
            picture = load_relief_picture(path, asset.name, size)
            if picture is not None:
                self._made_relief = True
                return picture
        if asset.is_texture:
            return load_texture_picture(path, size)
        if asset.is_document and asset.extension == ".pdf":
            return load_pdf_thumbnail_image(path, size)
        if asset.is_video:
            return load_video_picture(path, size)
        if asset.is_3d_model and self.render_model:
            return load_model_picture(path, size)
        return None

    def _render_member(self) -> tuple[Optional[QImage], str]:
        """A file inside an archive: from its unpacked copy if it was opened before, else read from
        the archive, up to archives.MAX_PREVIEW_BYTES (bigger ones and videos: once opened)."""
        asset, member = self.asset, self.asset.archive
        copy = archives.cached_copy(member)
        if copy is not None:
            return self._render_file(copy), ""
        if member.encrypted:
            return None, "Password-protected"
        if asset.is_video:
            return None, "Play to preview"
        if asset.is_archive:
            return None, "Archive inside"
        if member.size > archives.MAX_PREVIEW_BYTES:
            return None, "Open to preview"
        if asset.is_texture:
            picture, self._made_relief = load_member_picture(member, self.size, self.relief)
            return picture, ""
        if asset.is_3d_model and not self.render_model:
            return None, ""
        with archives.local_file(member) as path:
            return self._render_file(path), ""


class _ModelPictureRunnable(_ThumbnailRunnable):
    """Renders a model the viewer has already loaded (its arrays), and caches the picture."""

    def __init__(self, request: _Request, asset: AssetItem, size: int, cache_file: Optional[Path], style, arrays: tuple):
        super().__init__(request, asset, size, cache_file, style)
        self.arrays = arrays

    def run(self) -> None:
        vertices, faces, normals = self.arrays
        self.arrays = None
        if self.request.cancelled:
            return
        picture = render_model_picture(vertices, faces, normals, self.size)
        if picture is not None and not picture.isNull():
            self._to_disk(picture)
            self.signals.ready.emit(self.request, self._card(picture, ""))


# ----------------------------------------------------------------------
# Thumbnail Manager & Cache
# ----------------------------------------------------------------------
class ThumbnailManager(QObject):
    """Makes thumbnails in the background (the newest requests first) and keeps the most
    recently used ones in memory (up to MAX_MEMORY_BYTES; the rest reload from the disk cache)."""

    thumbnail_loaded = Signal(str, QPixmap)
    thumbnail_skipped = Signal(str)  # not made: the card had left the screen (ask again to get it)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()  # least recently used first
        self._cache_bytes = 0
        self.max_memory_bytes = MAX_MEMORY_BYTES
        self._pending: dict[str, _Request] = {}
        self._order = 0  # pool priority: each request outranks the ones before it
        # Placeholders depend only on the file type and size: draw each once
        self._placeholders: dict[tuple[str, int], QPixmap] = {}
        self._has_companion: dict[str, bool] = {}  # 3D model -> has a model.png etc. next to it
        self._rendered_on_disk: dict[str, bool] = {}  # big 3D model -> its picture (from the viewer) is cached
        self._sizes: dict[str, int] = {}  # the size each file's thumbnail was asked for
        self._thread_pool = QThreadPool()
        # Limit concurrent image worker threads to prevent disk IO saturation
        self._thread_pool.setMaxThreadCount(4)
        # Rendering 3D models: one at a time (each can take a few hundred MB while it's read)
        self._model_pool = QThreadPool()
        self._model_pool.setMaxThreadCount(1)
        self.render_models = True  # (core.settings model_view "thumbnails"; set by the main window)
        self.alpha_relief = True  # alphas drawn as a lit relief (model_view "alpha_relief"; set by the main window)
        # Whether a file is an alpha, when the library knows it already (else the job finds out)
        self.alpha_of: Callable[[AssetItem], Optional[bool]] = lambda asset: None

    def cached(self, asset: AssetItem) -> Optional[QPixmap]:
        return self._cache.get(str(asset.path))

    def memory_used(self) -> int:
        """Bytes of thumbnails kept in memory."""
        return self._cache_bytes

    def placeholder(self, asset: AssetItem, size: int) -> QPixmap:
        icon = app_icons.known_icon(asset.extension) if asset.opens_in_app else None  # (read by the card's job)
        key = (asset.category_name, size, icon is not None)
        pixmap = self._placeholders.get(key)
        if pixmap is None:
            pixmap = self._placeholders[key] = QPixmap.fromImage(render_placeholder_image(asset, size, icon=icon))
        return pixmap

    def _has_companion_picture(self, asset: AssetItem) -> bool:
        key = str(asset.path)
        if asset.in_archive:
            return False
        if key not in self._has_companion:
            self._has_companion[key] = any(asset.path.with_suffix(ext).exists() for ext in _COMPANION_EXTENSIONS)
        return self._has_companion[key]

    def _renders(self, asset: AssetItem, size: int) -> bool:
        """Whether this 3D model's picture is rendered: small enough to read for it, or already
        rendered and cached (opened in the viewer)."""
        if not self.render_models or not asset.is_3d_model or asset.opens_in_app or self._has_companion_picture(asset):
            return False
        if asset.size_bytes <= MAX_RENDERED_MODEL_BYTES:
            return True
        key = f"{asset.path}|{size}"
        if key not in self._rendered_on_disk:
            cache_file = disk_cache_path(asset, size)
            self._rendered_on_disk[key] = cache_file is not None and cache_file.is_file()
        return self._rendered_on_disk[key]

    def _needs_async(self, asset: AssetItem, size: int = 110) -> bool:
        if asset.is_texture or asset.is_document or asset.is_video or asset.is_archive or asset.opens_in_app:
            return True
        return self._has_companion_picture(asset) or self._renders(asset, size)

    def get_thumbnail(self, asset: AssetItem, size: int = 110) -> QPixmap:
        """Return cached thumbnail if present; otherwise returns placeholder and queues background load."""
        cache_key = str(asset.path)

        pixmap = self._cache.get(cache_key)
        if pixmap is not None:
            self._cache.move_to_end(cache_key)  # (most recently used)
            return pixmap

        self._sizes[cache_key] = size
        request = self._pending.get(cache_key)
        if request is not None:
            request.last_asked = time.monotonic()  # still on screen
        elif self._needs_async(asset, size):
            request = self._pending[cache_key] = _Request(cache_key)
            rendered = self._renders(asset, size)
            relief = self.alpha_relief and (asset.is_texture or asset.is_app_file)
            # (A pack picture can be an alpha: an app file's picture is kept apart for reliefs on and off)
            cache_file = (
                disk_cache_path(asset, size, relief=relief and asset.is_app_file)
                if not asset.is_3d_model or rendered or asset.opens_in_app else None
            )
            self._start(_ThumbnailRunnable(
                request, asset, size, cache_file, card_style(), render_model=rendered,
                relief=relief, alpha=self.alpha_of(asset) if relief and asset.is_texture else None,
            ), rendered)

        return self.placeholder(asset, size)

    def _start(self, runnable: _ThumbnailRunnable, model: bool) -> None:
        runnable.signals.ready.connect(self._on_worker_ready)
        runnable.signals.skipped.connect(self._on_worker_skipped)
        self._order += 1  # newest first: after a fast scroll, the cards now on screen come before the ones passed
        (self._model_pool if model else self._thread_pool).start(runnable, self._order)

    def model_viewed(self, asset: AssetItem, mesh) -> None:
        """A model was opened in the viewer: give it a rendered picture now if it has none (its
        arrays are used as they are: nothing is read again). Call before the viewer lets them go."""
        key = str(asset.path)
        size = self._sizes.get(key)
        if (
            size is None or not self.render_models or self._has_companion_picture(asset) or mesh.vertices is None
            or mesh.stand_in
        ):
            return
        cache_file = disk_cache_path(asset, size)
        if key in self._cache and self._renders(asset, size) or cache_file is None or cache_file.is_file():
            return
        old = self._pending.get(key)
        if old is not None:
            old.cancelled = True
        request = self._pending[key] = _Request(key, keep=True)
        self._start(_ModelPictureRunnable(request, asset, size, cache_file, card_style(),
                                          (mesh.vertices, mesh.faces, mesh.normals)), model=True)
        self._rendered_on_disk[f"{asset.path}|{size}"] = True

    def _on_worker_ready(self, request: _Request, qimg: QImage) -> None:
        """Slot executed on main GUI thread when a background thumbnail completes."""
        if self._pending.get(request.key) is not request:
            return  # from before the cache was cleared or the file changed
        del self._pending[request.key]
        pixmap = QPixmap.fromImage(qimg)
        self._store(request.key, pixmap)
        self.thumbnail_loaded.emit(request.key, pixmap)

    def _on_worker_skipped(self, request: _Request) -> None:
        if self._pending.get(request.key) is request:
            del self._pending[request.key]
            self.thumbnail_skipped.emit(request.key)  # (if the card is on screen after all, it asks again)

    def _store(self, key: str, pixmap: QPixmap) -> None:
        self._forget(key)
        self._cache[key] = pixmap
        self._cache_bytes += _pixmap_bytes(pixmap)
        while self._cache_bytes > self.max_memory_bytes and len(self._cache) > 1:
            _oldest, dropped = self._cache.popitem(last=False)
            self._cache_bytes -= _pixmap_bytes(dropped)

    def _forget(self, key: str) -> None:
        dropped = self._cache.pop(key, None)
        if dropped is not None:
            self._cache_bytes -= _pixmap_bytes(dropped)

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """Drop queued thumbnail jobs and wait for the running ones (before the app quits)."""
        self._cancel_pending()
        return self._thread_pool.waitForDone(timeout_ms) and self._model_pool.waitForDone(timeout_ms)

    def _cancel_pending(self) -> None:
        self._thread_pool.clear()
        self._model_pool.clear()
        for request in self._pending.values():
            request.cancelled = True  # (the running ones: their results are dropped)
        self._pending.clear()

    def invalidate(self, path_str: str) -> None:
        """Forget one file's thumbnail (it was edited)."""
        self._forget(path_str)
        request = self._pending.pop(path_str, None)
        if request is not None:
            request.cancelled = True

    def forget_app_cards(self) -> None:
        """The apps in use changed: read their icons again, and redo the cards that show them."""
        app_icons.forget()
        self._placeholders.clear()
        extensions = APP_FILE_EXTENSIONS | {".blend"} | apps.custom_types()
        for key in [k for k in (*self._cache, *self._pending) if os.path.splitext(k)[1].lower() in extensions]:
            self.invalidate(key)

    def clear_cache(self) -> None:
        """Clear the in-memory thumbnail cache (e.g. after a theme or size change)."""
        self._cancel_pending()
        self._cache.clear()
        self._cache_bytes = 0
        self._placeholders.clear()
        self._has_companion.clear()
        self._rendered_on_disk.clear()


def _pixmap_bytes(pixmap: QPixmap) -> int:
    return pixmap.width() * pixmap.height() * max(1, pixmap.depth() // 8)
