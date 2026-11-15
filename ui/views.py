"""App-wide polish for scroll areas and item views, installed once by install_view_effects().

- RowHighlightDelegate: the rounded hover / selection highlight of tree rows. The stylesheet
  can't draw it: Qt also paints item backgrounds into the indentation left of the row,
  which showed up as a stray square beside hovered and selected rows. It also gives
  rename-in-place editors the row's full height (the row's padding would clip their text).
- Smooth wheel scrolling: each wheel notch glides to its target instead of jumping there.
  Touchpads already send smooth pixel deltas, so they're left alone.
- Trees animate expanding and collapsing: sub-rows slide out instead of popping in.
- HoverFade: a row's (or a grid card's) hover fades in and out rather than snapping.

The motion follows the Animations setting (ui.animations.motion_enabled). A widget can opt
out with the property "smoothScroll" = False.
"""

from __future__ import annotations

from typing import Optional

import shiboken6

from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QEvent, QModelIndex, QObject, QPersistentModelIndex, QRect, QRectF, Qt, QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette
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
HEADING_ROLE = Qt.ItemDataRole.UserRole + 900  # True: a heading row (no hover, the theme's faint text)


class RowHighlightDelegate(QStyledItemDelegate):
    """Rounded hover / selection pill behind a row (spanning all its columns): the selection is a soft
    accent tint with the theme's selected text, like a checked button (not a solid accent block)."""

    INSET = 4
    RADIUS = 6.0

    def color(self, token: str) -> QColor:
        """A theme colour for the pill (the Theme Studio's preview shows the theme being made)."""
        return theme_color(token)

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
        heading = bool(index.data(HEADING_ROLE))
        level = 0.0
        if bool(state & QStyle.StateFlag.State_Enabled) and not heading:
            level = hover_level(option.widget, index, bool(state & QStyle.StateFlag.State_MouseOver))
        if selected or level > 0.0:
            first = index.column() == 0
            last = index.column() >= index.model().columnCount(index.parent()) - 1
            cell = QRectF(option.rect).adjusted(self.INSET if first else 0, 1, -self.INSET if last else 0, -1)
            # Middle cells continue the pill: round only the row's outer ends
            pill = cell.adjusted(0 if first else -2 * self.RADIUS, 0, 0 if last else 2 * self.RADIUS, 0)
            path = QPainterPath()
            path.addRoundedRect(pill, self.RADIUS, self.RADIUS)
            color = self.color("accent_soft" if selected else "hover")
            if not selected:
                color.setAlphaF(color.alphaF() * level)  # (fading in or out: HoverFade)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipRect(cell)
            painter.fillPath(path, color)
            painter.restore()

        opt = QStyleOptionViewItem(option)
        opt.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_HasFocus)
        if selected or heading:
            for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText):
                opt.palette.setColor(role, self.color("card_selected_text" if selected else "text_faint"))
        super().paint(painter, opt, index)


# ----------------------------------------------------------------------
# Hover fade
# ----------------------------------------------------------------------
class HoverFade(QObject):
    """Fades a view's hover in and out: the row (or card) under the mouse brightens over HOVER_MS while the one
    it left fades away. Delegates ask hover_level() instead of reading State_MouseOver (RowHighlightDelegate,
    ui.asset_grid's cards). A tree's row is one hover, whichever column the mouse is in."""

    def __init__(self, view: QAbstractItemView):
        super().__init__(view)
        self.setObjectName("hoverFade")
        self.current = QPersistentModelIndex()
        self.previous = QPersistentModelIndex()
        self.previous_from = 0.0  # how lit the row was when the mouse left it
        self.t = 1.0  # how far the fade has got
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(animations.HOVER_MS)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.valueChanged.connect(self._on_step)
        view.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)
        view.viewport().installEventFilter(self)

    def _view(self) -> Optional[QAbstractItemView]:
        view = self.parent()
        return view if isinstance(view, QAbstractItemView) and shiboken6.isValid(view) else None

    @staticmethod
    def _row(index) -> QModelIndex:
        return index.sibling(index.row(), 0) if index.isValid() else QModelIndex()

    def is_current(self, index) -> bool:
        row = self._row(index)
        return row.isValid() and self.current.isValid() and self.current == row

    def level(self, index) -> float:
        row = self._row(index)
        if not row.isValid():
            return 0.0
        if self.current.isValid() and self.current == row:
            return self.t
        if self.previous.isValid() and self.previous == row:
            return self.previous_from * (1.0 - self.t)
        return 0.0

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.HoverMove, QEvent.Type.HoverEnter, QEvent.Type.MouseMove):
            view = self._view()
            if view is not None:
                self.hover(view.indexAt(event.position().toPoint()))
        elif kind in (QEvent.Type.HoverLeave, QEvent.Type.Leave):
            self.hover(QModelIndex())
        return False

    def hover(self, index) -> None:
        """The mouse is over index now (an invalid one: over no row)."""
        row = self._row(index)
        if row.isValid() == self.current.isValid() and (not row.isValid() or self.current == row):
            return
        self._repaint(self.previous)  # (its fade is cut short)
        self.previous = QPersistentModelIndex(self.current)
        self.previous_from = self.t if self.current.isValid() else 0.0
        self.current = QPersistentModelIndex(row)
        self.anim.stop()
        if animations.motion_enabled():
            self.t = 0.0
            self.anim.start()
        else:
            self.t = 1.0
        self._repaint(self.previous)
        self._repaint(self.current)

    def _on_step(self, value) -> None:
        self.t = float(value)
        self._repaint(self.previous)
        self._repaint(self.current)

    def _repaint(self, row: QPersistentModelIndex) -> None:
        view = self._view()
        if view is None or not row.isValid() or view.model() is None:
            return
        index = view.model().index(row.row(), 0, row.parent())
        rect = view.visualRect(index)
        if isinstance(view, QTreeView):
            rect = QRect(0, rect.top(), view.viewport().width(), rect.height())
        view.viewport().update(rect.adjusted(-4, -4, 4, 4))


def hover_level(view: Optional[QWidget], index, hovered: bool) -> float:
    """How lit index's hover is (0 to 1): its view's HoverFade, or hovered as it is without one."""
    fade = view.findChild(HoverFade, "hoverFade", Qt.FindChildOption.FindDirectChildrenOnly) if view is not None else None
    if fade is None:
        return 1.0 if hovered else 0.0
    if hovered and not fade.is_current(index):
        # The rows moved under a still mouse (a scroll): it's over this one now
        keep = QPersistentModelIndex(index)
        QTimer.singleShot(0, fade, lambda: fade.hover(keep) if keep.isValid() else None)
    return fade.level(index)


# ----------------------------------------------------------------------
# Smooth scrolling
# ----------------------------------------------------------------------
class _Glide(QObject):
    """Animates one scroll bar (its parent) toward a target; further wheel notches extend the target.

    The animation has no target object: a QPropertyAnimation on the bar keeps a reference to the
    bar's wrapper, which with the bar's to this glide and this glide's to its animation made a
    reference cycle. Collecting it freed the real scroll bar under its open view, and Qt crashed
    the next time the view laid itself out (the 0.5.x PDF viewer and Settings crashes)."""

    def __init__(self, bar: QScrollBar):
        super().__init__(bar)
        self.target = bar.value()
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(SCROLL_MS)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.valueChanged.connect(self._on_step)
        bar.sliderPressed.connect(self.anim.stop)  # dragging the handle takes over

    def _on_step(self, value) -> None:
        bar = self.parent()
        if isinstance(bar, QScrollBar):
            bar.setValue(round(value))

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
        if shiboken6.isValid(glide.anim):
            return glide
        glide.deleteLater()  # (its animation gone: a new one takes over)
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
    if view.findChild(HoverFade, "hoverFade", Qt.FindChildOption.FindDirectChildrenOnly) is None:
        HoverFade(view)  # (hover events too, so the delegate sees the hovered row)
    if isinstance(view, QTreeView):
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
