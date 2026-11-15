"""The keyboard focus ring: a 2 px accent ring just outside whatever has the keyboard's focus, shown only while
you move around with the keyboard (Tab, the arrow keys) and gone at the next click, like a browser's
:focus-visible. In a list or the grid it rings the current row or card, and follows the arrow keys.

One ring per window (a FocusRing, a child drawn above everything in it, letting the mouse through); it glides
from control to control unless Animations are off. Text fields and dropdowns already show their focus (an accent
border), and big canvases (the Artbook board, a 3D view) would be all ring, so those get none; a widget can opt
out with the property "focusRing" = False.

install_focus_ring(app) once at startup (main.py).
"""

from __future__ import annotations

from typing import Optional

import shiboken6

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QAbstractScrollArea, QAbstractSpinBox, QApplication, QCheckBox, QComboBox,
    QGraphicsView, QLineEdit, QListView, QMenu, QPlainTextEdit, QRadioButton, QTabBar, QTextEdit, QTreeView, QWidget,
)

from ui import animations
from ui.themes import theme_color

GAP = 3  # pixels between a control and its ring
WIDTH = 2
GLIDE_MS = 120
RADII = {"seg": 7.0, "ghost": 8.0, "ghostText": 8.0, "ghostSplit": 8.0, "ghostDrop": 8.0}  # (as the stylesheet rounds them)
SKIP_CLASSES = ("QOpenGLWidget", "QVideoWidget", "QQuickWidget")  # canvases (by name: not every module is loaded)


class FocusRing(QWidget):
    """The ring itself: a child of the window, above everything in it, the mouse passing through."""

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.setObjectName("focusRing")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.radius = 6.0
        self.clip = QRect()  # in the window's coordinates: the visible part of the control's scroll areas
        self.glide = QVariantAnimation(self)
        self.glide.setDuration(GLIDE_MS)
        self.glide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.glide.valueChanged.connect(self._on_glide)
        self.hide()

    def place(self, target: QRect, radius: float, clip: QRect) -> None:
        """Ring target (window coordinates), as round as radius, showing only inside clip."""
        outer = target.adjusted(-GAP - WIDTH, -GAP - WIDTH, GAP + WIDTH, GAP + WIDTH)
        gliding_there = self.glide.state() == QVariantAnimation.State.Running and self.glide.endValue() == outer
        if self.isVisible() and (self.geometry() == outer or gliding_there) and (radius, clip) == (self.radius, self.clip):
            return  # (already there: nothing moved)
        self.radius, self.clip = radius, clip
        self.glide.stop()
        if self.isVisible() and animations.motion_enabled() and self.geometry() != outer:
            self.glide.setStartValue(self.geometry())
            self.glide.setEndValue(outer)
            self.glide.start()
        else:
            self.setGeometry(outer)
        self.raise_()
        self.show()
        self.update()

    def _on_glide(self, value) -> None:
        if isinstance(value, QRect):
            self.setGeometry(value)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.clip.isValid():
            painter.setClipRect(self.clip.translated(-self.pos()))
        painter.setPen(QPen(theme_color("accent_border"), WIDTH))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        half = WIDTH / 2
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(half, half, -half, -half),
                                self.radius + GAP, self.radius + GAP)


def _skipped(widget: QWidget) -> bool:
    """Controls that show their focus already, or would be all ring."""
    if widget.isWindow() or widget.property("focusRing") is False:
        return True
    if isinstance(widget, (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit, QGraphicsView, QComboBox, QMenu)):
        return True
    names = {cls.__name__ for cls in type(widget).__mro__}
    if names & set(SKIP_CLASSES):
        return True
    return (widget.window().windowFlags() & Qt.WindowType.Popup) == Qt.WindowType.Popup  # (menus, dropdown lists)


def target_of(widget: QWidget) -> Optional[tuple[QRect, float]]:
    """What to ring (in widget's own coordinates) and how round: a view's current row or card, a checkbox's box
    and words, a tab bar's current tab, or the whole control."""
    if isinstance(widget, QAbstractItemView):
        viewport = widget.viewport()
        index = widget.currentIndex()
        if not index.isValid():
            return QRect(viewport.mapTo(widget, QPoint(0, 0)), viewport.size()).adjusted(2, 2, -2, -2), 8.0
        rect = widget.visualRect(index)
        if isinstance(widget, QTreeView):  # (the row, as its hover pill: ui.views.RowHighlightDelegate)
            rect = QRect(4, rect.top() + 1, viewport.width() - 8, rect.height() - 2)
            radius = 6.0
        elif isinstance(widget, QListView) and widget.viewMode() == QListView.ViewMode.IconMode:
            rect = rect.adjusted(3, 3, -3, -3)  # (a card)
            radius = 8.0
        else:
            rect = rect.adjusted(2, 1, -2, -1)
            radius = 6.0
        if not rect.intersects(viewport.rect()):
            return None
        return rect.translated(viewport.mapTo(widget, QPoint(0, 0))), radius
    if isinstance(widget, QTabBar) and widget.currentIndex() >= 0:
        return widget.tabRect(widget.currentIndex()), 6.0
    if isinstance(widget, (QCheckBox, QRadioButton)):
        return QRect(0, 0, min(widget.width(), widget.sizeHint().width()), widget.height()), 6.0
    if isinstance(widget, QAbstractButton):
        return widget.rect(), RADII.get(widget.objectName(), 6.0)
    return widget.rect(), 6.0


def _clip(widget: QWidget, window: QWidget) -> QRect:
    """The part of the window widget can be seen in: inside every scroll area it's in (window coordinates)."""
    clip = window.rect()
    parent = widget.parentWidget()
    while parent is not None and parent is not window:
        area = parent.parentWidget()
        if isinstance(area, QAbstractScrollArea) and area.viewport() is parent:
            clip = clip.intersected(QRect(parent.mapTo(window, QPoint(0, 0)), parent.size()).adjusted(-GAP - WIDTH, -GAP - WIDTH, GAP + WIDTH, GAP + WIDTH))
        parent = parent.parentWidget()
    if isinstance(widget, QAbstractItemView):  # (a row half scrolled out: just its visible part)
        viewport = widget.viewport()
        clip = clip.intersected(QRect(viewport.mapTo(window, QPoint(0, 0)), viewport.size()))
    return clip


class FocusRings(QObject):
    """Watches the keyboard and mouse for the whole app: keyboard use shows the ring, a click hides it."""

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.keyboard = False
        self.ring: Optional[FocusRing] = None
        self._soon = QTimer(self)
        self._soon.setSingleShot(True)
        self._soon.setInterval(0)
        self._soon.timeout.connect(self.refresh)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            modifiers = event.modifiers()
            command = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
            if not modifiers & command and not (not event.text() and modifiers & Qt.KeyboardModifier.ShiftModifier):
                self.keyboard = True  # (not a shortcut, nor Shift on its own)
            self._soon.start()
        elif kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            if self.keyboard:
                self.keyboard = False
                self.hide()
        elif kind == QEvent.Type.FocusIn:
            if event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason):
                self.keyboard = True
            self._soon.start()
        elif kind == QEvent.Type.FocusOut:
            self._soon.start()
        elif self.keyboard and kind in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Wheel, QEvent.Type.Hide,
                                        QEvent.Type.Show, QEvent.Type.LayoutRequest):
            if self.ring is not None and obj is not self.ring:
                self._soon.start()  # (the control may have moved: once things settle)
        return False

    def hide(self) -> None:
        if self.ring is not None and shiboken6.isValid(self.ring):
            self.ring.glide.stop()
            self.ring.hide()

    def refresh(self) -> None:
        """Put the ring around the focused control (or hide it)."""
        widget = QApplication.focusWidget()
        if not self.keyboard or widget is None or not widget.isVisible() or _skipped(widget):
            self.hide()
            return
        found = target_of(widget)
        if found is None:
            self.hide()
            return
        rect, radius = found
        window = widget.window()
        ring = window.findChild(FocusRing, "focusRing", Qt.FindChildOption.FindDirectChildrenOnly)
        if ring is None:
            ring = FocusRing(window)
        if self.ring is not ring:
            self.hide()
            self.ring = ring
        ring.place(QRect(widget.mapTo(window, rect.topLeft()), rect.size()), radius, _clip(widget, window))


_rings: Optional[FocusRings] = None


def install_focus_ring(app: Optional[QApplication] = None) -> FocusRings:
    """Watch the whole app for keyboard focus (call once)."""
    global _rings
    app = app or QApplication.instance()
    if _rings is None:
        _rings = FocusRings(app)
        app.installEventFilter(_rings)
    return _rings
