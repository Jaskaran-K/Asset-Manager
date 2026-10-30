"""Duotone icons ("Gradient" style): every icon in the app is drawn here, in theme colors.

Each icon is drawn on a 24 x 24 grid in two layers:
- the body: the main shape, filled with a vertical gradient of the secondary color
  (95% opaque at the top, 45% at the bottom), like lit glass;
- the details: small strokes / fills in the primary color on top (a door, a check mark).

A tone picks the two colors. "accent" (the default) uses the theme's accent_text / accent;
others are for meaning (success, danger, warning) or for sitting on a colored button.
Colors are looked up when the icon is painted, so icons follow theme changes, and they're
vector-drawn, so they're crisp at any size and screen scale.

    set_icon(button_or_action, "settings")          # icon on a button / action
    duotone_icon("check", "success")                # a QIcon
    IconLabel(...).set_icon("warning", "warning")   # icon + text, for status lines

Adding an icon: write a _name(p, primary, secondary) function below and register it in
ICONS. See CLAUDE.md: no emoji or symbol characters as icons.
"""

from __future__ import annotations

import math
from typing import Callable, Optional, Union

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QIconEngine,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QTransform,
)
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QSizePolicy, QWidget

from ui.themes import parse_color, theme_tokens, current_theme_id

Tone = Union[str, tuple[str, str]]  # a TONES name, or (primary, secondary) color tokens / "#hex"

TONES: dict[str, tuple[str, str]] = {
    "accent": ("accent_text", "accent"),
    "text": ("text", "text_muted"),
    "success": ("#bbf7d0", "#16a34a"),
    "danger": ("warn_text", "danger"),
    "warning": ("note_text", "#f59e0b"),
    "light": ("#ffffff", "#fff7ed"),  # on a colored button (e.g. the amber "In Library")
}
DISABLED = ("text_faint", "text_faint")


def _color(token: str) -> QColor:
    tokens = theme_tokens(current_theme_id())
    color = parse_color(tokens.get(token, token))
    return color if color.isValid() else QColor("#9ca3af")


def tone_colors(tone: Tone, disabled: bool = False) -> tuple[QColor, QColor]:
    primary, secondary = DISABLED if disabled else (TONES[tone] if isinstance(tone, str) else tone)
    return _color(primary), _color(secondary)


# ----------------------------------------------------------------------
# Drawing helpers (24 x 24 grid)
# ----------------------------------------------------------------------
def _pen(color: QColor, width: float = 1.8) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _body(p: QPainter, path: QPainterPath, secondary: QColor, round_corners: float = 0.0) -> None:
    """Fill the body with the glass gradient (round_corners: also stroke it, softening corners)."""
    box = path.boundingRect()
    gradient = QLinearGradient(0, box.top(), 0, box.bottom())
    top, bottom = QColor(secondary), QColor(secondary)
    top.setAlphaF(secondary.alphaF() * 0.95)
    bottom.setAlphaF(secondary.alphaF() * 0.45)
    gradient.setColorAt(0.0, top)
    gradient.setColorAt(1.0, bottom)
    p.fillPath(path, gradient)
    if round_corners:
        pen = QPen(gradient, round_corners)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.strokePath(path, pen)


def _polygon(*points: tuple[float, float]) -> QPainterPath:
    path = QPainterPath()
    path.addPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
    path.closeSubpath()
    return path


def _line(p: QPainter, color: QColor, width: float, *points: tuple[float, float]) -> None:
    p.setPen(_pen(color, width))
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    p.drawPath(path)


def _outline(p: QPainter, path: QPainterPath, color: QColor, width: float = 1.3) -> None:
    p.setPen(_pen(color, width))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)


def _rounded(x: float, y: float, w: float, h: float, r: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), r, r)
    return path


def _circle(cx: float, cy: float, r: float) -> QPainterPath:
    path = QPainterPath()
    path.addEllipse(QPointF(cx, cy), r, r)
    return path


def _star_path() -> QPainterPath:
    points = []
    for i in range(10):
        angle = math.radians(-90 + i * 36)
        radius = 9.8 if i % 2 == 0 else 4.3
        points.append((12 + radius * math.cos(angle), 12.8 + radius * math.sin(angle)))
    return _polygon(*points)


# ----------------------------------------------------------------------
# Icons
# ----------------------------------------------------------------------
def _home(p, primary, secondary):
    _body(p, _polygon((5, 10), (5, 20.5), (19, 20.5), (19, 10), (12, 4)), secondary)
    _line(p, primary, 1.6, (2.5, 11.5), (12, 3), (21.5, 11.5))
    p.fillPath(_rounded(9.75, 14, 4.5, 6.5, 1.2), primary)


def _menu(p, primary, secondary):
    for y in (5, 10.5, 16):
        _body(p, _rounded(3, y, 18, 3.2, 1.6), secondary)
        p.fillPath(_rounded(3, y, 5, 3.2, 1.6), primary)


def _tag(p, primary, secondary):
    _body(p, _polygon((3.5, 4), (12, 4), (20.5, 12.5), (12.5, 20.5), (3.5, 11.5)), secondary, round_corners=2.5)
    p.fillPath(_circle(8.2, 8.7, 1.9), primary)


def _settings(p, primary, secondary):
    gear = _circle(12, 12, 7)
    for i in range(8):
        tooth = QPainterPath()
        tooth.addRoundedRect(QRectF(-2, -10.5, 4, 5), 1, 1)
        gear = gear.united(QTransform().translate(12, 12).rotate(45 * i).map(tooth))
    _body(p, gear, secondary)
    _outline(p, _circle(12, 12, 3), primary, 2.0)


def _star(p, primary, secondary):
    star = _star_path()
    _body(p, star, secondary, round_corners=1.5)
    _outline(p, star, primary, 1.3)


def _pin(p, primary, secondary):
    head = _rounded(8, 2.5, 8, 3.6, 1.5).united(_polygon((9.3, 5.5), (14.7, 5.5), (17.5, 13), (6.5, 13)))
    _body(p, head, secondary, round_corners=1.2)
    _line(p, primary, 1.8, (6.5, 13), (17.5, 13))
    _line(p, primary, 2.0, (12, 13), (12, 21.5))


def _close(p, primary, secondary):
    _body(p, _circle(12, 12, 9.5), secondary)
    _line(p, primary, 2.1, (8.5, 8.5), (15.5, 15.5))
    _line(p, primary, 2.1, (15.5, 8.5), (8.5, 15.5))


def _check(p, primary, secondary):
    _body(p, _circle(12, 12, 9.5), secondary)
    _line(p, primary, 2.3, (7.5, 12.5), (10.8, 15.7), (16.5, 9))


def _warning(p, primary, secondary):
    _body(p, _polygon((12, 3.5), (21.5, 20), (2.5, 20)), secondary, round_corners=2.5)
    _line(p, primary, 2.3, (12, 9.5), (12, 14))
    p.fillPath(_circle(12, 17, 1.3), primary)


def _arrow_up(p, primary, secondary):
    _body(p, _polygon((12, 3.5), (19.5, 12), (4.5, 12)), secondary, round_corners=2.0)
    _line(p, primary, 3.0, (12, 12), (12, 20.5))


def _arrow_down(p, primary, secondary):
    p.save()
    p.translate(12, 12)
    p.rotate(180)
    p.translate(-12, -12)
    _arrow_up(p, primary, secondary)
    p.restore()


def _grid(p, primary, secondary):
    _body(p, _rounded(3, 3, 18, 18, 3.5), secondary)
    for v in (9, 15):
        _line(p, primary, 1.4, (v, 5), (v, 19))
        _line(p, primary, 1.4, (5, v), (19, v))


def _play(p, primary, secondary):
    triangle = _polygon((7.5, 4.5), (19.5, 12), (7.5, 19.5))
    _body(p, triangle, secondary, round_corners=2.5)
    _line(p, primary, 1.6, (7.5, 8), (7.5, 16))


def _pause(p, primary, secondary):
    for x in (5.5, 13.5):
        _body(p, _rounded(x, 4.5, 5, 15, 1.8), secondary)
        _line(p, primary, 1.6, (x + 1.6, 7), (x + 1.6, 10))


def _picture(p, primary, x, y, w, h):
    """A tiny landscape picture (mountain and sun) in the primary color."""
    _line(p, primary, 1.6, (x, y + h), (x + w * 0.38, y + h * 0.35), (x + w * 0.62, y + h * 0.7),
          (x + w * 0.78, y + h * 0.5), (x + w, y + h))
    p.fillPath(_circle(x + w * 0.8, y + h * 0.05, 1.3), primary)


def _view_assets(p, primary, secondary):
    _body(p, _rounded(2.5, 3.5, 19, 17, 3), secondary)
    for x in (6, 13.5):
        for y in (7, 13.5):
            p.fillPath(_rounded(x, y, 4.5, 4, 1), primary)


def _view_artbook(p, primary, secondary):
    _body(p, _rounded(2.5, 3.5, 19, 17, 3), secondary)
    _picture(p, primary, 5.5, 8.5, 13, 8.5)


def _view_split(p, primary, secondary):
    _body(p, _rounded(2, 4, 9.2, 16, 2.5), secondary)
    _body(p, _rounded(12.8, 4, 9.2, 16, 2.5), secondary)
    for y in (7.5, 12.5):
        p.fillPath(_rounded(4.3, y, 4.6, 3.4, 0.8), primary)
    _picture(p, primary, 14.2, 9.5, 6.4, 6.5)


def _view_separate(p, primary, secondary):
    back = _rounded(2, 3, 13, 11, 2.5)
    _body(p, back, secondary)
    front = _rounded(8.5, 9, 13.5, 12, 2.5)
    _body(p, front, secondary)
    _outline(p, front, primary, 1.4)
    _line(p, primary, 1.6, (8.5, 12.3), (22, 12.3))  # the front window's title bar


def _folder(p, primary, secondary):
    _body(p, _polygon((3, 5.5), (9.5, 5.5), (11.5, 7.5), (21, 7.5), (21, 19), (3, 19)), secondary, round_corners=2.0)
    _line(p, primary, 1.7, (3.5, 11), (20.5, 11))


def _refresh(p, primary, secondary):
    _body(p, _circle(12, 12, 9.5), secondary)
    arc = QPainterPath()
    arc.arcMoveTo(QRectF(6.5, 6.5, 11, 11), 60)
    arc.arcTo(QRectF(6.5, 6.5, 11, 11), 60, 270)
    _outline(p, arc, primary, 2.0)
    p.fillPath(_polygon((14.2, 4.6), (18.6, 7.3), (13.9, 9.4)), primary)  # the arrowhead


def _eye(p, primary, secondary):
    almond = QPainterPath(QPointF(2, 12))
    almond.quadTo(QPointF(12, 1.5), QPointF(22, 12))
    almond.quadTo(QPointF(12, 22.5), QPointF(2, 12))
    _body(p, almond, secondary)
    p.fillPath(_circle(12, 12, 3.4), primary)


def _tools(p, primary, secondary):
    head = _circle(8, 8, 5.6)
    handle = QTransform().translate(8, 8).rotate(-45).map(_rounded(-2, 0, 4, 15, 2))
    notch = QTransform().translate(8, 8).rotate(-45).map(_rounded(-1.6, -7, 3.2, 5, 0.8))
    _body(p, head.united(handle).subtracted(notch), secondary)
    p.fillPath(_circle(16.8, 16.8, 1.4), primary)


def _fit(p, primary, secondary):
    _body(p, _rounded(7, 7, 10, 10, 2.2), secondary)
    for (x, y), (dx, dy) in (((3.5, 3.5), (1, 1)), ((20.5, 3.5), (-1, 1)), ((20.5, 20.5), (-1, -1)), ((3.5, 20.5), (1, -1))):
        _line(p, primary, 1.9, (x, y + 4.5 * dy), (x, y), (x + 4.5 * dx, y))  # the frame's corners


def _page(p, primary, secondary):
    _body(p, _polygon((5, 3), (14.5, 3), (19, 7.5), (19, 21), (5, 21)), secondary, round_corners=1.6)
    p.fillPath(_polygon((14.5, 3), (19, 7.5), (14.5, 7.5)), primary)  # the folded corner
    _line(p, primary, 1.7, (8.5, 12.5), (15.5, 12.5))
    _line(p, primary, 1.7, (8.5, 16.5), (13, 16.5))


def _new_page(p, primary, secondary):
    _body(p, _polygon((3.5, 2.5), (12.5, 2.5), (16.5, 6.5), (16.5, 19.5), (3.5, 19.5)), secondary, round_corners=1.6)
    p.fillPath(_polygon((12.5, 2.5), (16.5, 6.5), (12.5, 6.5)), primary)
    p.fillPath(_circle(17.5, 17.5, 5), primary)
    _line(p, secondary, 1.8, (17.5, 15), (17.5, 20))  # the plus, cut out of the badge
    _line(p, secondary, 1.8, (15, 17.5), (20, 17.5))


def _new_folder(p, primary, secondary):
    _body(p, _polygon((2.5, 4.5), (8.5, 4.5), (10.5, 6.5), (19, 6.5), (19, 17), (2.5, 17)), secondary, round_corners=2.0)
    _line(p, primary, 1.7, (3, 9.5), (13, 9.5))
    p.fillPath(_circle(17.5, 17.5, 5), primary)
    _line(p, secondary, 1.8, (17.5, 15), (17.5, 20))
    _line(p, secondary, 1.8, (15, 17.5), (20, 17.5))


def _edit(p, primary, secondary):
    turn = QTransform().translate(13, 11).rotate(45)  # a pencil leaning to the right
    _body(p, turn.map(_rounded(-3.2, -10, 6.4, 14.5, 1.6)), secondary)
    p.fillPath(turn.map(_polygon((-3.2, 5.3), (3.2, 5.3), (0, 10.5))), primary)  # the tip
    ends = [turn.map(QPointF(x, -5.5)) for x in (-3.2, 3.2)]
    _line(p, primary, 1.8, *((e.x(), e.y()) for e in ends))  # the band near the top


def _window(p, primary, secondary):
    _body(p, _rounded(3, 4, 18, 16, 2.6), secondary)
    _line(p, primary, 1.8, (3.8, 8.8), (20.2, 8.8))  # the title bar
    p.fillPath(_circle(6.6, 6.4, 1.1), primary)
    _line(p, primary, 1.7, (8, 14.5), (16, 14.5))


def _text_color(p, primary, secondary):
    _body(p, _polygon((5, 18), (10, 4), (14, 4), (19, 18), (15.5, 18), (12, 8), (8.5, 18)), secondary, round_corners=1.2)
    _line(p, primary, 1.9, (8.6, 13.5), (15.4, 13.5))  # the crossbar


def _highlight(p, primary, secondary):
    turn = QTransform().translate(12.5, 10.5).rotate(40)
    _body(p, turn.map(_rounded(-3.5, -8, 7, 12, 1.6)), secondary)
    p.fillPath(turn.map(_polygon((-3.5, 4), (3.5, 4), (1.6, 8.5), (-1.6, 8.5))), primary)  # the chisel tip


def _fill(p, primary, secondary):
    bucket = QTransform().translate(10.5, 11).rotate(-45).map(_rounded(-5, -5, 10, 10, 1.6))
    _body(p, bucket, secondary)
    _line(p, primary, 1.7, (4, 11), (10.5, 4.5), (17, 11))
    drop = _circle(19, 16.5, 2.2).united(_polygon((17.1, 15.4), (19, 11.5), (20.9, 15.4)))
    p.fillPath(drop, primary)


def _aligned(p, primary, secondary, alignment):
    _body(p, _rounded(3, 4, 18, 16, 2.4), secondary)
    for y, length in ((8, 12), (12, 8), (16, 12)):
        start = {"left": 6, "center": 12 - length / 2, "right": 18 - length}[alignment]
        _line(p, primary, 1.7, (start, y), (start + length, y))


def _align_left(p, primary, secondary):
    _aligned(p, primary, secondary, "left")


def _align_center(p, primary, secondary):
    _aligned(p, primary, secondary, "center")


def _align_right(p, primary, secondary):
    _aligned(p, primary, secondary, "right")


def _chevron_down(p, primary, secondary):
    _body(p, _circle(12, 12, 9.5), secondary)
    _line(p, primary, 2.1, (7.5, 10.2), (12, 14.6), (16.5, 10.2))



def _eyedropper(p, primary, secondary):
    turn = QTransform().translate(12, 12).rotate(45)
    _body(p, turn.map(_rounded(-2.6, -9.5, 5.2, 13, 1.8)), secondary)  # the glass tube
    p.fillPath(turn.map(_rounded(-4.2, -10.5, 8.4, 4.2, 1.6)), primary)  # the bulb's collar
    _line(p, primary, 2.0, turn.map(QPointF(0, 3.5)).toTuple(), turn.map(QPointF(0, 9.5)).toTuple())  # the tip


def _copy(p, primary, secondary):
    _line(p, primary, 1.8, (4.5, 15.5), (4.5, 4.5), (15.5, 4.5))  # the sheet behind
    _body(p, _rounded(8, 8, 12.5, 13, 2.4), secondary)
    _line(p, primary, 1.7, (11.5, 12.5), (17, 12.5))
    _line(p, primary, 1.7, (11.5, 16.5), (15, 16.5))


def _import(p, primary, secondary):
    _body(p, _rounded(3, 12, 18, 9, 2.4), secondary)  # the tray
    _line(p, primary, 2.1, (12, 3), (12, 14))  # an arrow down into it
    _line(p, primary, 2.1, (8, 10.5), (12, 14.5), (16, 10.5))


def _lock(p, primary, secondary):
    shackle = QPainterPath(QPointF(8, 11))
    shackle.lineTo(8, 8)
    shackle.arcTo(QRectF(8, 4, 8, 8), 180, -180)
    shackle.lineTo(16, 11)
    p.setPen(_pen(primary, 2.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(shackle)
    _body(p, _rounded(4.5, 10.5, 15, 10.5, 2.4), secondary)
    p.fillPath(_circle(12, 15, 1.6), primary)  # the keyhole
    _line(p, primary, 1.6, (12, 15.5), (12, 18))


def _eraser(p, primary, secondary):
    turn = QTransform().translate(12, 11).rotate(-40)
    _body(p, turn.map(_rounded(-8, -4.5, 16, 9, 2.4)), secondary)
    p.fillPath(turn.map(_rounded(-8, -4.5, 6, 9, 2.0)), primary)  # the rubber tip
    _line(p, primary, 1.8, (6, 20.5), (19, 20.5))  # what's left on the page


IconPainter = Callable[[QPainter, QColor, QColor], None]
ICONS: dict[str, IconPainter] = {
    "home": _home,
    "menu": _menu,
    "tag": _tag,
    "settings": _settings,
    "star": _star,
    "pin": _pin,
    "close": _close,
    "check": _check,
    "warning": _warning,
    "arrow_up": _arrow_up,
    "arrow_down": _arrow_down,
    "grid": _grid,
    "play": _play,
    "pause": _pause,
    "view_assets": _view_assets,
    "view_artbook": _view_artbook,
    "view_split": _view_split,
    "view_separate": _view_separate,
    "folder": _folder,
    "refresh": _refresh,
    "eye": _eye,
    "tools": _tools,
    "fit": _fit,
    "page": _page,
    "new_page": _new_page,
    "new_folder": _new_folder,
    "edit": _edit,
    "window": _window,
    "text_color": _text_color,
    "highlight": _highlight,
    "fill": _fill,
    "align_left": _align_left,
    "align_center": _align_center,
    "align_right": _align_right,
    "chevron_down": _chevron_down, "eyedropper": _eyedropper, "copy": _copy, "import": _import, "lock": _lock, "eraser": _eraser,
}


def draw_icon(p: QPainter, rect: QRectF, name: str, tone: Tone = "accent", disabled: bool = False) -> None:
    primary, secondary = tone_colors(tone, disabled)
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(rect.topLeft())
    p.scale(rect.width() / 24.0, rect.height() / 24.0)
    ICONS[name](p, primary, secondary)
    p.restore()


# ----------------------------------------------------------------------
# QIcon / widgets
# ----------------------------------------------------------------------
class _DuotoneEngine(QIconEngine):
    """Paints the icon on demand, in the colors of the theme active at that moment."""

    def __init__(self, name: str, tone: Tone):
        super().__init__()
        self.name, self.tone = name, tone

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, _state: QIcon.State) -> None:
        draw_icon(painter, QRectF(rect), self.name, self.tone, mode == QIcon.Mode.Disabled)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:
        pixmap = QPixmap(round(size.width() * scale), round(size.height() * scale))
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, pixmap.width(), pixmap.height()), mode, state)
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        return pixmap

    def clone(self) -> "_DuotoneEngine":
        return _DuotoneEngine(self.name, self.tone)


_cache: dict[tuple[str, Tone], tuple[QIcon, _DuotoneEngine]] = {}


def duotone_icon(name: str, tone: Tone = "accent") -> QIcon:
    if name not in ICONS:
        raise KeyError(f"no duotone icon named {name!r}; add it to ui.duotone.ICONS")
    key = (name, tone)
    if key not in _cache:
        engine = _DuotoneEngine(name, tone)
        _cache[key] = (QIcon(engine), engine)  # keep the engine alive alongside its icon
    return _cache[key][0]


def set_icon(target: Union[QAbstractButton, QAction], name: Optional[str], tone: Tone = "accent", size: int = 0) -> None:
    """Give a button / action a duotone icon (None removes it); size sets a button's icon size."""
    target.setIcon(duotone_icon(name, tone) if name else QIcon())
    if size and isinstance(target, QAbstractButton):
        target.setIconSize(QSize(size, size))


class DuotoneGlyph(QWidget):
    """Just an icon, as a widget (painted live, so it follows theme changes)."""

    def __init__(self, name: Optional[str] = None, tone: Tone = "accent", size: int = 16, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.name, self.tone = name, tone
        self.setFixedSize(size, size)

    def set_icon(self, name: Optional[str], tone: Tone = "accent") -> None:
        self.name, self.tone = name, tone
        self.update()

    def paintEvent(self, _event) -> None:
        if self.name:
            painter = QPainter(self)
            draw_icon(painter, QRectF(self.rect()), self.name, self.tone, not self.isEnabled())


class IconLabel(QWidget):
    """An icon beside a (word-wrapped) label: for status and warning lines.

    box_name styles the whole row (e.g. "warningLabel"); text_name styles just the text.
    """

    def __init__(self, box_name: str = "", text_name: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        if box_name:
            self.setObjectName(box_name)
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.glyph = DuotoneGlyph(size=16)
        self.label = QLabel()
        self.label.setWordWrap(True)
        if text_name:
            self.label.setObjectName(text_name)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.label, 1)

    def set_icon(self, name: Optional[str], tone: Tone = "accent") -> None:
        self.glyph.set_icon(name, tone)
        self.glyph.setVisible(bool(name))

    def setText(self, text: str) -> None:
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()
