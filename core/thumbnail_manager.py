"""Asynchronous thumbnail loader and caching manager.

Solves:
1. High-resolution texture freezing: loads thumbnails in background QThreadPool workers.
2. 256 MB allocation limit: uses Pillow for streaming downsampled decoding without full-res buffers.
3. TIFF tag warnings: avoids libtiff parser bugs on null-terminated metadata tags.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

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

from .psd_reader import read_psd_composite
from .scanner import AssetItem

# Increase Qt's default image allocation limit from 256 MB to 2048 MB
QImageReader.setAllocationLimit(2048)

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
    through core.psd_reader. max_size lets that reader subsample for thumbnails.
    """
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


def render_placeholder_image(asset: AssetItem, size: int = 110) -> QImage:
    """Render a fast procedural placeholder card (thread-safe QImage)."""
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
    painter.setBrush(QBrush(_CARD_STYLE["card"]))
    painter.setPen(QPen(_CARD_STYLE["border"], 1.2))
    painter.drawRoundedRect(card_rect, 8, 8)

    center_x = size / 2.0
    center_y = size * 0.40
    cube_w = size * 0.22
    cube_h = size * 0.12
    cube_depth = size * 0.18

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

    # Badge pill
    badge_rect = QRectF(size * 0.15, size * 0.74, size * 0.70, size * 0.17)
    painter.setBrush(QBrush(_CARD_STYLE["badge"]))
    painter.setPen(QPen(top_col, 1.2))
    painter.drawRoundedRect(badge_rect, 4, 4)

    painter.setPen(QPen(_CARD_STYLE["badge_text"]))
    font = QFont("Segoe UI", max(5, int(size * 0.08)), QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, asset.category_name)

    painter.end()
    return img


def load_texture_thumbnail_image(file_path: Path, category_name: str, size: int = 110) -> Optional[QImage]:
    """Load and downsample texture using Pillow with zero Qt 256MB memory overhead."""
    try:
        with open_image(file_path, max_size=size) as im:
            # Convert high-bit-depth modes to RGBA *before* thumbnailing,
            # because Pillow's BILINEAR resampling crashes on I;16 mode.
            rgba_im = _safe_convert_pil_to_rgba(im)

            # Downscale preserving aspect ratio
            max_inner_w = size - 16
            max_inner_h = size - 32
            rgba_im.thumbnail((max_inner_w, max_inner_h), Image.Resampling.BILINEAR)
            rgba_im.load()
            inner_w, inner_h = rgba_im.size
            raw_bytes = rgba_im.tobytes()

        # Create QImage from raw bytes
        inner_qimg = QImage(
            raw_bytes,
            inner_w,
            inner_h,
            QImage.Format.Format_RGBA8888,
        ).copy()

        # Compose inside a styled card
        card_img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        card_img.fill(Qt.GlobalColor.transparent)

        p = QPainter(card_img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        card_rect = QRectF(2, 2, size - 4, size - 4)
        p.setBrush(QBrush(_CARD_STYLE["card"]))
        p.setPen(QPen(_CARD_STYLE["border"], 1))
        p.drawRoundedRect(card_rect, 6, 6)

        # Center texture preview
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
        p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, category_name)

        p.end()
        return card_img

    except Exception:
        return None


def load_pdf_thumbnail_image(file_path: Path, size: int = 110) -> Optional[QImage]:
    """Render page 0 of a PDF into a thumbnail QImage."""
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
class _WorkerSignals(QObject):
    ready = Signal(str, QImage)


class _ThumbnailRunnable(QRunnable):
    """Background task to decode image/texture and generate thumbnail."""

    def __init__(self, asset: AssetItem, size: int):
        super().__init__()
        self.asset = asset
        self.size = size
        self.signals = _WorkerSignals()

    def run(self) -> None:
        qimg: Optional[QImage] = None

        if self.asset.is_texture:
            qimg = load_texture_thumbnail_image(self.asset.path, self.asset.category_name, self.size)
        elif self.asset.is_document and self.asset.extension == ".pdf":
            qimg = load_pdf_thumbnail_image(self.asset.path, self.size)
        elif self.asset.is_3d_model:
            # Check for companion preview images (model.png, etc.)
            for preview_ext in [".png", ".jpg", ".jpeg", ".webp"]:
                companion = self.asset.path.with_suffix(preview_ext)
                if companion.exists():
                    qimg = load_texture_thumbnail_image(companion, self.asset.category_name, self.size)
                    if qimg is not None:
                        break

        if qimg is not None and not qimg.isNull():
            self.signals.ready.emit(str(self.asset.path), qimg)


# ----------------------------------------------------------------------
# Thumbnail Manager & Cache
# ----------------------------------------------------------------------
class ThumbnailManager(QObject):
    """Manages asynchronous thumbnail generation and in-memory pixmap caching."""

    thumbnail_loaded = Signal(str, QPixmap)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._cache: dict[str, QPixmap] = {}
        self._pending: set[str] = set()
        self._thread_pool = QThreadPool()
        # Limit concurrent image worker threads to prevent disk IO saturation
        self._thread_pool.setMaxThreadCount(4)

    def get_thumbnail(self, asset: AssetItem, size: int = 110) -> QPixmap:
        """Return cached thumbnail if present; otherwise returns placeholder and queues background load."""
        cache_key = str(asset.path)

        if cache_key in self._cache:
            return self._cache[cache_key]

        # Generate fast placeholder immediately
        placeholder_qimg = render_placeholder_image(asset, size)
        placeholder_pixmap = QPixmap.fromImage(placeholder_qimg)

        # Queue background worker if this asset needs async texture/preview loading
        needs_async = (
            asset.is_texture
            or asset.is_document
            or any(asset.path.with_suffix(ext).exists() for ext in [".png", ".jpg", ".jpeg", ".webp"])
        )

        if needs_async and cache_key not in self._pending:
            self._pending.add(cache_key)
            runnable = _ThumbnailRunnable(asset, size)
            runnable.signals.ready.connect(self._on_worker_ready)
            self._thread_pool.start(runnable)

        return placeholder_pixmap

    def _on_worker_ready(self, path_str: str, qimg: QImage) -> None:
        """Slot executed on main GUI thread when a background thumbnail completes."""
        if path_str in self._pending:
            self._pending.remove(path_str)

        pixmap = QPixmap.fromImage(qimg)
        self._cache[path_str] = pixmap
        self.thumbnail_loaded.emit(path_str, pixmap)

    def clear_cache(self) -> None:
        """Clear the in-memory thumbnail cache."""
        self._cache.clear()
        self._pending.clear()
