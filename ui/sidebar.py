"""Slide-out sidebar with a frosted-glass background.

SidebarHost is a content area with a sidebar panel on its left: the library's categories
beside the asset grid, and the Artbook's Pages list beside the board. The sidebar is either docked beside the content
(pinned) or floats over it and closes again after you pick something (unpinned). Opening
and closing slide it in from the left; its right edge can be dragged to resize it.

The panel is frosted glass (ui.glass) over what's behind it: the window background when
docked, the content (grid / board) when floating over it.
"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRect, Qt, QTimer, Property, Signal
from PySide6.QtGui import QAction, QColor, QImage, QLinearGradient, QPainter, QRegion
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.settings import SIDEBAR_WIDTH_RANGE, get_sidebar_state, set_sidebar_state
from ui import animations, shortcuts
from ui.duotone import set_icon
from ui.glass import GlassPanel
from ui.themes import theme_color

SLIDE_MS = 280
REFRESH_MS = 60  # floating over the grid: follow its changes (scrolling, thumbnails) this often
SHADOW_PX = 16


class _EdgeShadow(QWidget):
    """Soft shadow along the right edge of the floating panel."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, self.width(), 0)
        gradient.setColorAt(0.0, QColor(0, 0, 0, 70))
        gradient.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), gradient)


class _ResizeGrip(QWidget):
    """Drag handle on the panel's right edge."""

    def __init__(self, host: "SidebarHost"):
        super().__init__(host)
        self.host = host
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setToolTip("Drag to resize the sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._drag_from: Optional[tuple[int, int]] = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = (round(event.globalPosition().x()), self.host.sidebar_width())

    def mouseMoveEvent(self, event) -> None:
        if self._drag_from is not None:
            start_x, start_width = self._drag_from
            self.host.set_sidebar_width(start_width + round(event.globalPosition().x()) - start_x, save=False)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_from is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = None
            self.host.set_sidebar_width(self.host.sidebar_width())

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        if self.underMouse() or self._drag_from is not None:
            painter = QPainter(self)
            painter.fillRect(QRect(self.width() // 2 - 1, 0, 2, self.height()), theme_color("accent"))


class SidebarHost(QWidget):
    """Content area with a slide-out sidebar (see the module docstring)."""

    open_changed = Signal(bool)

    def __init__(
        self,
        body: QWidget,
        content: QWidget,
        parent: Optional[QWidget] = None,
        *,
        title: str = "CATEGORIES",
        settings_key: str = "sidebar",
        header_widgets: Sequence[QWidget] = (),
        close_shortcut: str = "main.close_sidebar",
        content_name: str = "the grid",
        picking: str = "pick a category",
        toggle_hint: str = "the menu button or Ctrl+B",
    ):
        super().__init__(parent)
        self.setObjectName("sidebarHost")
        self._settings_key = settings_key
        self._content_name, self._picking, self._toggle_hint = content_name, picking, toggle_hint
        state = get_sidebar_state(settings_key)
        self._width: int = state["width"]
        self._pinned: bool = state["pinned"]
        self._open: bool = state["open"] and self._pinned  # a floating one would cover the grid at startup
        self._progress = 1.0 if self._open else 0.0  # 0 = hidden, 1 = fully out
        self._slide_from = self._slide_to = self._progress
        self._grabbing = False
        self._watching_clicks = False
        self._watched: list[QWidget] = []

        self.content = content
        content.setParent(self)

        self.panel = GlassPanel(self)
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(0, 6, 0, 0)
        panel_layout.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(14, 2, 8, 2)
        title_label = QLabel(title)
        title_label.setObjectName("sidebarTitle")
        header.addWidget(title_label)
        header.addStretch(1)
        for widget in header_widgets:
            header.addWidget(widget)
        self.btn_pin = QPushButton()
        set_icon(self.btn_pin, "pin", size=16)
        self.btn_pin.setObjectName("sidebarPin")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pin.clicked.connect(self._on_pin_clicked)
        header.addWidget(self.btn_pin)
        panel_layout.addLayout(header)
        panel_layout.addWidget(body, 1)
        self.body = body

        # Esc closes the floating sidebar while it has the keyboard focus
        close_action = QAction(self.panel)
        close_action.setShortcut(shortcuts.sequence(close_shortcut))
        close_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        close_action.triggered.connect(self._on_escape)
        self.panel.addAction(close_action)

        self.shadow = _EdgeShadow(self)
        self.grip = _ResizeGrip(self)

        self._anim = QPropertyAnimation(self, b"slide", self)
        self._anim.setDuration(SLIDE_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_slide_finished)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(REFRESH_MS)
        self._refresh_timer.timeout.connect(self.refresh_backdrop)

        self._update_pin_button()
        self.panel.setVisible(self._open)

    # ------------------ state ------------------
    def is_open(self) -> bool:
        return self._open

    def is_pinned(self) -> bool:
        return self._pinned

    def is_floating(self) -> bool:
        return not self._pinned

    def sidebar_width(self) -> int:
        return self._width

    def toggle(self) -> None:
        self.set_open(not self._open)

    def set_open(self, open_: bool, animate: bool = True) -> None:
        if open_ == self._open:
            return
        self._open = open_
        set_sidebar_state(self._settings_key, open=open_)
        if open_:
            self.panel.show()
            self.refresh_backdrop()
            if self.is_floating():
                self.body.setFocus()
        self._watch_clicks(open_ and self.is_floating())
        target = 1.0 if open_ else 0.0
        self._anim.stop()
        if animate and animations.motion_enabled() and self.isVisible():
            self._slide_from, self._slide_to = self._progress, target
            self._anim.setStartValue(self._progress)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._set_progress(target)
            self._on_slide_finished()
        self.open_changed.emit(open_)

    def close_if_floating(self) -> None:
        """After picking something in the floating sidebar: get it out of the way."""
        if self.is_floating():
            self.set_open(False)

    def set_pinned(self, pinned: bool) -> None:
        if pinned == self._pinned:
            return
        self._pinned = pinned
        set_sidebar_state(self._settings_key, pinned=pinned)
        self._update_pin_button()
        self._watch_clicks(self._open and self.is_floating())
        self._layout()
        self.refresh_backdrop()

    def set_sidebar_width(self, width: int, save: bool = True) -> None:
        lo, hi = SIDEBAR_WIDTH_RANGE
        self._width = max(lo, min(hi, int(width)))
        self._layout()
        if save:
            set_sidebar_state(self._settings_key, width=self._width)
            self.refresh_backdrop()

    def _on_pin_clicked(self, checked: bool) -> None:
        self.set_pinned(checked)

    def _on_escape(self) -> None:
        if self.is_floating() and self._open:
            self.set_open(False)
            self.content.setFocus()

    def _update_pin_button(self) -> None:
        self.btn_pin.setChecked(self._pinned)
        self.btn_pin.setToolTip(
            f"Docked beside {self._content_name}. Click to float it over {self._content_name} instead: it then "
            f"slides away after you {self._picking} ({self._toggle_hint} brings it back)"
            if self._pinned else
            f"Floating over {self._content_name}. Click to dock it beside {self._content_name} instead"
        )

    # ------------------ sliding ------------------
    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = value
        self._layout()

    slide = Property(float, _get_progress, _set_progress)

    def _on_slide_finished(self) -> None:
        self._slide_from = self._slide_to = self._progress
        if not self._open:
            self.panel.hide()
        self._layout()

    def _layout(self) -> None:
        width, height = self.width(), self.height()
        w, p = self._width, self._progress
        x = round(-w * (1.0 - p))
        self.panel.setGeometry(x, 0, w, height)
        self.panel.offset_x = x
        if self._pinned:
            # While sliding, the content moves without changing width, then resizes once at the
            # end (re-flowing thousands of grid cards on every frame would stutter)
            offset = round(w * p)
            lay_out_for = min(self._slide_from, self._slide_to) if self._slide_from != self._slide_to else p
            self.content.setGeometry(offset, 0, width - round(w * lay_out_for), height)
        else:
            self.content.setGeometry(0, 0, width, height)
        floating_out = self.is_floating() and p > 0.0
        self.shadow.setVisible(floating_out)
        self.shadow.setGeometry(x + w, 0, SHADOW_PX, height)
        self.grip.setVisible(p >= 1.0)
        self.grip.setGeometry(x + w - 4, 0, 8, height)
        for widget in (self.panel, self.shadow, self.grip):
            widget.raise_()
        self.panel.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout()
        self._refresh_timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.panel.backdrop is None:
            self.refresh_backdrop()

    def changeEvent(self, event) -> None:
        # Theme / background texture changed: the backdrop shows the old one
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            self._refresh_timer.start()
        super().changeEvent(event)

    # ------------------ frosted backdrop ------------------
    def watch(self, widget: QWidget) -> None:
        """Repaints of widget (inside the content) update the glass while it floats over them."""
        self._watched.append(widget)
        widget.installEventFilter(self)

    def refresh_backdrop(self) -> None:
        if not self._open or self.height() <= 0:
            return
        size = QRect(0, 0, self._width, self.height())
        image = QImage(size.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(self.palette().window().color())
        painter = QPainter(image)
        window = self.window()
        # The window's own background (theme gradient and texture), without any child widgets
        window.render(
            painter, QPoint(0, 0), QRegion(QRect(self.mapTo(window, QPoint(0, 0)), size.size())),
            QWidget.RenderFlag.DrawWindowBackground,
        )
        if self.is_floating():
            # Floating over the content: show the grid through the glass
            self._grabbing = True
            try:
                # Children only: the content itself is see-through over the window background
                # (QWidget's render: a QGraphicsView content, the Artbook board, has its own render())
                QWidget.render(self.content, painter, QPoint(0, 0), QRegion(size), QWidget.RenderFlag.DrawChildren)
            finally:
                self._grabbing = False
        painter.end()
        self.panel.set_backdrop(image)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Paint and obj in self._watched:
            if (
                not self._grabbing and self._open and self.is_floating()
                and not self._refresh_timer.isActive() and self._slide_from == self._slide_to
            ):
                painted = QRect(obj.mapTo(self, event.rect().topLeft()), event.rect().size())
                if painted.intersects(self.panel.geometry()):
                    self._refresh_timer.start()
        elif kind == QEvent.Type.MouseButtonPress and self._watching_clicks:
            # Floating: a click on the content beside the sidebar closes it (and does nothing else)
            if isinstance(obj, QWidget) and (obj is self.content or self.content.isAncestorOf(obj)):
                self.set_open(False)
                return True
        return False

    def _watch_clicks(self, watch: bool) -> None:
        if watch == self._watching_clicks:
            return
        self._watching_clicks = watch
        app = QApplication.instance()
        if watch:
            app.installEventFilter(self)
        else:
            app.removeEventFilter(self)
