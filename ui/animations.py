"""Small QPropertyAnimation-based effects shared across the UI.

- AnimatedButton: push button whose hover highlight fades in and out, and whose
  icon/text springs back after a press (optionally nudging sideways).
- PlayPauseButton: AnimatedButton whose glyph morphs between play and pause.
- install_popup_animations(): fades and slides in every QMenu and QComboBox
  dropdown in the app (plus any popup with the "animatePopup" property).
- set_motion_mode(): Settings > Appearance > Animations; "auto" follows Windows'
  "Animation effects" switch. With motion off everything jumps to its end state.

Stylesheet colors still come from ui.themes; the effects are painted on top.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    Property,
)
from PySide6.QtGui import QColor, QPainter, QPalette, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QMenu,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QWidget,
)

HOVER_MS = 160
PRESS_MS = 90
RELEASE_MS = 360
MORPH_MS = 220
POPUP_MS = 150
POPUP_SLIDE_PX = 8


_motion_enabled = True


def windows_animations_enabled() -> bool:
    """Windows' Settings > Accessibility > Visual effects > "Animation effects"."""
    if sys.platform != "win32":
        return True
    try:
        enabled = ctypes.c_int(1)
        SPI_GETCLIENTAREAANIMATION = 0x1042
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0):
            return bool(enabled.value)
    except (AttributeError, OSError):
        pass
    return True


def set_motion_mode(mode: str) -> None:
    """"on", "off", or "auto" (follow Windows). With motion off, effects jump to their end state."""
    global _motion_enabled
    _motion_enabled = {"on": True, "off": False}.get(mode, None)
    if _motion_enabled is None:
        _motion_enabled = windows_animations_enabled()


def motion_enabled() -> bool:
    return _motion_enabled


def _animation(target: QObject, prop: bytes, duration: int, curve: QEasingCurve.Type) -> QPropertyAnimation:
    anim = QPropertyAnimation(target, prop, target)
    anim.setDuration(duration)
    anim.setEasingCurve(curve)
    return anim


def _run(anim: QPropertyAnimation, end, duration: Optional[int] = None, curve: Optional[QEasingCurve.Type] = None) -> None:
    """Animate from wherever the property is now (so interrupted animations don't jump)."""
    anim.stop()
    if not _motion_enabled:
        anim.targetObject().setProperty(anim.propertyName().data().decode(), end)
        return
    if duration is not None:
        anim.setDuration(duration)
    if curve is not None:
        anim.setEasingCurve(curve)
    anim.setStartValue(anim.targetObject().property(anim.propertyName().data().decode()))
    anim.setEndValue(end)
    anim.start()


# ----------------------------------------------------------------------
# Buttons
# ----------------------------------------------------------------------
class AnimatedButton(QPushButton):
    """QPushButton with a fading hover highlight and a springy press.

    nudge: -1 / +1 shifts the content that way while pressed (for back / forward
    buttons), 0 just shrinks it. hover_color defaults to the theme accent.
    """

    def __init__(
        self,
        text: str = "",
        parent: Optional[QWidget] = None,
        *,
        nudge: int = 0,
        hover_color: Optional[QColor] = None,
        hover_alpha: int = 46,
    ):
        super().__init__(text, parent)
        self._hover = 0.0
        self._scale = 1.0
        self._offset = 0.0
        self._nudge = nudge
        self._hover_color = hover_color
        self._hover_alpha = hover_alpha
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._hover_anim = _animation(self, b"hoverLevel", HOVER_MS, QEasingCurve.Type.OutCubic)
        self._scale_anim = _animation(self, b"contentScale", PRESS_MS, QEasingCurve.Type.OutQuad)
        self._offset_anim = _animation(self, b"contentOffset", PRESS_MS, QEasingCurve.Type.OutQuad)

    # Animated properties
    def _get_hover(self) -> float:
        return self._hover

    def _set_hover(self, value: float) -> None:
        self._hover = value
        self.update()

    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, value: float) -> None:
        self._scale = value
        self.update()

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    hoverLevel = Property(float, _get_hover, _set_hover)
    contentScale = Property(float, _get_scale, _set_scale)
    contentOffset = Property(float, _get_offset, _set_offset)

    # Events
    def enterEvent(self, event) -> None:
        if self.isEnabled():
            _run(self._hover_anim, 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        _run(self._hover_anim, 0.0)
        super().leaveEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self._hover_anim.stop()
            self._set_hover(0.0)
        super().changeEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and _motion_enabled:
            _run(self._scale_anim, 0.8, PRESS_MS, QEasingCurve.Type.OutQuad)
            _run(self._offset_anim, 3.0 * self._nudge, PRESS_MS, QEasingCurve.Type.OutQuad)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.spring()
        super().mouseReleaseEvent(event)

    def spring(self) -> None:
        """Play the release bounce (also used when a keyboard shortcut triggers the button)."""
        if not _motion_enabled:
            return
        if self._scale == 1.0 and self._offset == 0.0:
            self._set_scale(0.8)
            self._set_offset(3.0 * self._nudge)
        _run(self._scale_anim, 1.0, RELEASE_MS, QEasingCurve.Type.OutBack)
        _run(self._offset_anim, 0.0, RELEASE_MS, QEasingCurve.Type.OutBack)

    # Painting
    def paintEvent(self, _event) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        painter.drawControl(QStyle.ControlElement.CE_PushButtonBevel, option)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._hover > 0.0 and self.isEnabled():
            color = QColor(self._hover_color or self.palette().color(QPalette.ColorRole.Highlight))
            color.setAlpha(int(self._hover_alpha * self._hover))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            radius = min(8.0, self.height() / 2.0)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), radius, radius)

        center = QPointF(self.rect().center()) + QPointF(0.5, 0.5)
        painter.translate(center + QPointF(self._offset, 0.0))
        painter.scale(self._scale, self._scale)
        painter.translate(-center)
        self.paint_content(painter, option)

    def paint_content(self, painter: QStylePainter, option: QStyleOptionButton) -> None:
        """Draw the icon/text (subclasses can draw their own glyph instead)."""
        painter.drawControl(QStyle.ControlElement.CE_PushButtonLabel, option)


class PlayPauseButton(AnimatedButton):
    """Round play/pause button whose triangle morphs into two pause bars."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("", parent, hover_color=QColor("#ffffff"), hover_alpha=40)
        self._morph = 0.0  # 0 = play triangle, 1 = pause bars
        self._playing = False
        self._morph_anim = _animation(self, b"morph", MORPH_MS, QEasingCurve.Type.InOutCubic)

    def _get_morph(self) -> float:
        return self._morph

    def _set_morph(self, value: float) -> None:
        self._morph = value
        self.update()

    morph = Property(float, _get_morph, _set_morph)

    def set_playing(self, playing: bool, animate: bool = True) -> None:
        if playing == self._playing:
            return
        self._playing = playing
        if animate and self.isVisible():
            _run(self._morph_anim, 1.0 if playing else 0.0)
        else:
            self._morph_anim.stop()
            self._set_morph(1.0 if playing else 0.0)

    def is_playing(self) -> bool:
        return self._playing

    def paint_content(self, painter: QStylePainter, option: QStyleOptionButton) -> None:
        size = min(self.width(), self.height()) * 0.38
        cx, cy = self.width() / 2.0, self.height() / 2.0
        top, bottom = cy - size / 2.0, cy + size / 2.0
        quarter = size / 4.0

        # Play: the triangle split into two quads; pause: two bars. Each corner slides to its partner.
        left, right = cx - size * 0.36, cx + size * 0.5  # optical centring of the triangle
        mid = (left + right) / 2.0
        play_shapes = (
            ((left, top), (mid, top + quarter), (mid, bottom - quarter), (left, bottom)),
            ((mid, top + quarter), (right, cy), (right, cy), (mid, bottom - quarter)),
        )
        bar = size * 0.3
        pause_left, pause_right = cx - size * 0.4, cx + size * 0.1
        pause_shapes = (
            ((pause_left, top), (pause_left + bar, top), (pause_left + bar, bottom), (pause_left, bottom)),
            ((pause_right, top), (pause_right + bar, top), (pause_right + bar, bottom), (pause_right, bottom)),
        )

        t = self._morph
        color = QColor("#ffffff") if self.isEnabled() else self.palette().color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for play, pause in zip(play_shapes, pause_shapes):
            points = [
                QPointF(px + (qx - px) * t, py + (qy - py) * t) for (px, py), (qx, qy) in zip(play, pause)
            ]
            painter.drawPolygon(QPolygonF(points))


# ----------------------------------------------------------------------
# Popups (menus and dropdowns)
# ----------------------------------------------------------------------
def animate_popup(popup: QWidget) -> None:
    """Fade a just-shown popup window in while it slides into place."""
    if not popup.isWindow() or not _motion_enabled:
        return
    final_pos = popup.pos()
    group = QParallelAnimationGroup(popup)
    fade = QPropertyAnimation(popup, b"windowOpacity", group)
    fade.setDuration(POPUP_MS)
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    fade.setEasingCurve(QEasingCurve.Type.OutCubic)
    slide = QPropertyAnimation(popup, b"pos", group)
    slide.setDuration(POPUP_MS + 40)
    # Slide away from whatever opened it: down for popups opening below, up for those above
    opened_above = popup.property("popupOpensUp") is True
    slide.setStartValue(final_pos + QPoint(0, POPUP_SLIDE_PX if opened_above else -POPUP_SLIDE_PX))
    slide.setEndValue(final_pos)
    slide.setEasingCurve(QEasingCurve.Type.OutCubic)
    group.addAnimation(fade)
    group.addAnimation(slide)
    popup.setWindowOpacity(0.0)
    group.start(QParallelAnimationGroup.DeletionPolicy.DeleteWhenStopped)


def _is_combo_dropdown(widget: QWidget) -> bool:
    # QComboBox's list lives in a private top-level container whose parent is the combo box
    return widget.isWindow() and isinstance(widget.parentWidget(), QComboBox)


class _PopupAnimator(QObject):
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            if isinstance(obj, QMenu) or _is_combo_dropdown(obj) or obj.property("animatePopup") is True:
                if _is_combo_dropdown(obj):
                    combo = obj.parentWidget()
                    obj.setProperty("popupOpensUp", obj.y() < combo.mapToGlobal(QPoint(0, 0)).y())
                animate_popup(obj)
        return False


_popup_animator: Optional[_PopupAnimator] = None


def install_popup_animations(app: Optional[QApplication] = None) -> None:
    """Animate every menu and dropdown in the app (call once after creating QApplication)."""
    global _popup_animator
    app = app or QApplication.instance()
    if app is None or _popup_animator is not None:
        return
    # Qt's own combo/menu effects would fight with ours
    for effect in (Qt.UIEffect.UI_AnimateCombo, Qt.UIEffect.UI_AnimateMenu, Qt.UIEffect.UI_FadeMenu):
        QApplication.setEffectEnabled(effect, False)
    _popup_animator = _PopupAnimator(app)
    app.installEventFilter(_popup_animator)
