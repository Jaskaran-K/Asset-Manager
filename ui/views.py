"""App-wide polish for scroll areas and item views, installed once by install_view_effects().

- RowHighlightDelegate: the rounded hover / selection highlight of tree rows. The stylesheet
  can't draw it: Qt also paints item backgrounds into the indentation left of the row,
  which showed up as a stray square beside hovered and selected rows. It also gives
  rename-in-place editors the row's full height (the row's padding would clip their text).
- Smooth wheel scrolling: each wheel notch glides to its target instead of jumping there.
  Touchpads already send smooth pixel deltas, so they're left alone.
- Trees animate expanding and collapsing: sub-rows slide out instead of popping in.

The motion follows the Animations setting (ui.animations.motion_enabled). A widget can opt
out with the property "smoothScroll" = False.
"""

from __future__ import annotations

from typing import Optional

import shiboken6

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QObject, QPropertyAnimation, QRect, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QLineEdit,
    QPlainTextEdit,
    QScrollBar,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTextEdit,
    QTreeView,
    QWidget,
)

from ui import animations
from ui.themes import theme_color

SCROLL_MS = 260
MIN_NOTCH_PX = 100  # about three rows of text per wheel notch, like Windows' default


# ----------------------------------------------------------------------
# Row highlight
# ----------------------------------------------------------------------
class RowHighlightDelegate(QStyledItemDelegate):
    """Rounded hover / selection pill behind a row (spanning all its columns).

    An icon with a Selected-mode pixmap shows that on the pill (e.g. a light version of an
    accent-colored icon, which would vanish on the accent pill)."""

    INSET = 4
    RADIUS = 5.0
    _selected_row = False

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:
        super().initStyleOption(option, index)
        icon = option.icon
        if self._selected_row and not icon.isNull() and icon.availableSizes(QIcon.Mode.Selected):
            ratio = option.widget.devicePixelRatioF() if option.widget is not None else 1.0
            option.icon = QIcon(icon.pixmap(option.decorationSize, ratio, QIcon.Mode.Selected))

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index) -> QWidget:
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            editor.setObjectName("inlineEditor")  # slim: see the stylesheet
        return editor

    def updateEditorGeometry(self, editor: QWidget, option: QStyleOptionViewItem, index) -> None:
        # From where the text starts to the end of the row, the row's full height
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        text = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        row = option.rect
        left = max(row.left(), text.left() - 4)
        editor.setGeometry(QRect(left, row.top() + 2, row.right() - self.INSET - left, row.height() - 4))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        state = option.state
        selected = bool(state & QStyle.StateFlag.State_Selected)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver) and bool(state & QStyle.StateFlag.State_Enabled)
        if selected or hovered:
            first = index.column() == 0
            last = index.column() >= index.model().columnCount(index.parent()) - 1
            cell = QRectF(option.rect).adjusted(self.INSET if first else 0, 1, -self.INSET if last else 0, -1)
            # Middle cells continue the pill: round only the row's outer ends
            pill = cell.adjusted(0 if first else -2 * self.RADIUS, 0, 0 if last else 2 * self.RADIUS, 0)
            path = QPainterPath()
            path.addRoundedRect(pill, self.RADIUS, self.RADIUS)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipRect(cell)
            painter.fillPath(path, theme_color("accent" if selected else "hover"))
            painter.restore()

        opt = QStyleOptionViewItem(option)
        opt.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_HasFocus)
        if selected:
            for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText):
                opt.palette.setColor(role, Qt.GlobalColor.white)
        self._selected_row = selected  # initStyleOption (called by paint) can't see it: it's cleared above
        try:
            super().paint(painter, opt, index)
        finally:
            self._selected_row = False


# ----------------------------------------------------------------------
# Smooth scrolling
# ----------------------------------------------------------------------
class _Glide(QObject):
    """Animates one scroll bar toward a target; further wheel notches extend the target."""

    def __init__(self, bar: QScrollBar):
        super().__init__(bar)
        self.target = bar.value()
        self.anim = QPropertyAnimation(bar, b"value", self)
        self.anim.setDuration(SCROLL_MS)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        bar.sliderPressed.connect(self.anim.stop)  # dragging the handle takes over

    def by(self, bar: QScrollBar, pixels: int) -> bool:
        """Glide bar (the live one the wheel event found: a reference kept from earlier can outlive
        its scroll bar) by pixels; False if already at that end (the wheel then goes to the parent)."""
        if self.anim.state() != QAbstractAnimation.State.Running:
            self.target = bar.value()
        target = max(bar.minimum(), min(bar.maximum(), self.target + pixels))
        if target == self.target:
            return self.anim.state() == QAbstractAnimation.State.Running
        self.target = target
        self.anim.stop()
        self.anim.setStartValue(bar.value())
        self.anim.setEndValue(target)
        self.anim.start()
        return True


def _glide_for(bar: QScrollBar) -> _Glide:
    for glide in bar.findChildren(_Glide, options=Qt.FindChildOption.FindDirectChildrenOnly):
        if shiboken6.isValid(glide.anim) and glide.anim.targetObject() is bar:
            return glide
        glide.deleteLater()  # (left over, its scroll bar gone: a new one takes over)
    return _Glide(bar)


def _notch_pixels(bar: QScrollBar) -> int:
    lines = max(1, QApplication.wheelScrollLines())
    return min(max(bar.singleStep() * lines, MIN_NOTCH_PX), max(bar.pageStep() // 2, 20))


def scroll_smoothly(area: QAbstractScrollArea, event) -> bool:
    """Handle a mouse-wheel event on area with a glide; False to leave it to Qt."""
    if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
        return False  # Ctrl+wheel zooms in some views
    if not event.pixelDelta().isNull():
        return False  # touchpad: already smooth
    delta = event.angleDelta()
    vertical = abs(delta.y()) >= abs(delta.x())
    amount = delta.y() if vertical else delta.x()
    bar = area.verticalScrollBar() if vertical else area.horizontalScrollBar()
    if vertical and bar.maximum() <= bar.minimum():
        bar = area.horizontalScrollBar()  # nothing to scroll vertically: the wheel scrolls sideways
    if amount == 0 or bar.maximum() <= bar.minimum() or bar.isSliderDown():
        return False
    # Shift+wheel pages, as in Qt's own scroll bars
    step = bar.pageStep() if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else _notch_pixels(bar)
    return _glide_for(bar).by(bar, round(-amount / 120 * step))


# ----------------------------------------------------------------------
# Installation
# ----------------------------------------------------------------------
def _polish_view(view: QAbstractItemView) -> None:
    # Pixel scrolling, so glides (and touchpads) move smoothly rather than a row at a time
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    if isinstance(view, QTreeView):
        # Hover events, so the delegate sees the hovered row (a stylesheet :hover rule used to do this)
        view.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)
        view.setAnimated(animations.motion_enabled())
        if type(view.itemDelegate()) is QStyledItemDelegate:  # not views with their own delegate
            view.setItemDelegate(RowHighlightDelegate(view))


def _smooth_scroll_area(widget: QObject) -> Optional[QAbstractScrollArea]:
    """The scroll area whose viewport widget is, if it should scroll smoothly."""
    area = widget.parent() if isinstance(widget, QWidget) else None
    if not isinstance(area, QAbstractScrollArea) or area.viewport() is not widget:
        return None
    if isinstance(area, (QPlainTextEdit, QTextEdit)) or area.property("smoothScroll") is False:
        return None  # text views scroll by lines, not pixels
    return area


class _ViewEffects(QObject):
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Polish and isinstance(obj, QAbstractItemView):
            _polish_view(obj)
        elif kind == QEvent.Type.Wheel and animations.motion_enabled():
            area = _smooth_scroll_area(obj)
            if area is not None and scroll_smoothly(area, event):
                event.accept()
                return True
        return False


def _on_motion_changed(enabled: bool) -> None:
    for widget in QApplication.allWidgets():
        if isinstance(widget, QTreeView):
            widget.setAnimated(enabled)


_view_effects: Optional[_ViewEffects] = None


def install_view_effects(app: Optional[QApplication] = None) -> None:
    """Row highlights, smooth scrolling and animated trees for every view (call once)."""
    global _view_effects
    app = app or QApplication.instance()
    if app is None or _view_effects is not None:
        return
    _view_effects = _ViewEffects(app)
    app.installEventFilter(_view_effects)
    animations.on_motion_changed(_on_motion_changed)
