"""The building blocks of the app's bars (the November 2026 look: quiet until you need them).

- ghost_button: a borderless tool button (a GhostButton: its hover fades in, a press springs); hovering shows its shape. An icon alone (its tooltip says what it
  does) or an icon with a word, for the few main actions. Checkable ones fill with the accent when on.
- bar_separator: the thin line between groups in a bar.
- DropButton: a borderless dropdown showing the current choice and a small chevron. ComboDrop is one that
  shows a QComboBox's items (the combo is kept, hidden, as the choice: its signals and tests stay as they were).
- fit_popup: a dropdown's list as wide as its longest entry (for the narrow, borderless ones).
- segmented: a row of choices in a soft track, the chosen one filled with the accent (the Render window's settings).
- soft_shadow: the floating bars' shadow for a panel placed by its owner (the Artbook's note, drawing and group
  bars, its Images list): fainter on a light theme, following theme changes.
- FloatingBar: a rounded bar floating over a view (a child of it, kept at its bottom centre), with a soft
  shadow, fading in and out (unless Animations are off). The selection bar (ui.selection_bar) is one, and a
  viewer's tools; those fade away while the mouse is still over the picture (fade_when_idle).
  stack_on() puts a smaller bar just above another, shown with it while it's wanted (a 3D viewer's
  Roughness / Exposure, there only while they apply).
- FloatingCard: the same at the view's top right, its contents scrolling when taller than the view (a viewer's
  details).
- section_heading / SettingCard: Settings pages are cards (a setting's name and a line on it on the left, its
  control on the right) under small caps headings.
- DialogFrame / DialogFooter: the dialog standard. A title and a line on what it does at the top, sections with
  small caps headings, and a footer bar: extras on the left, then Cancel and the one filled button at the right.

Their colours come from the theme's stylesheet (ui.themes: #ghost, #ghostText, #ghostSplit, #floatingBar,
#settingCard, #dialogFooter),
so they follow every theme, the Theme Studio's included.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import (
    QEasingCurve, QEvent, QObject, QPoint, QPointF, QPropertyAnimation, QRectF, QSize, Qt, QTimer, QVariantAnimation,
)
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
    QMenu, QPushButton, QScrollArea,
    QSizePolicy, QStyle, QStyleOptionToolButton, QStylePainter, QToolButton, QVBoxLayout, QWidget,
)

from ui.animations import HOVER_MS, PRESS_MS, RELEASE_MS, motion_enabled
from ui.duotone import set_icon

ICON_SIZE = 18
FADE_MS = 140
IDLE_MS = 2500  # a viewer's tools fade after the mouse has been still this long
PRESS_SCALE = 0.88  # how far a ghost button dips while pressed, before it springs back


class GhostButton(QToolButton):
    """The borderless buttons (ghost_button, DropButton, a segmented choice, the view switch): the hover fades
    in and out, and a press springs (the button dips, then bounces back, like the video player's buttons).

    The stylesheet still draws it, its :hover look included (the bar it's in may round it differently): it's
    drawn as it is without the hover, then with it on top, as faint as the fade has got. Both follow the
    Animations setting (with them off the hover simply shows, and nothing moves)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._hover = 0.0
        self._scale = 1.0
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.valueChanged.connect(self._set_hover)
        self._scale_anim = QVariantAnimation(self)
        self._scale_anim.valueChanged.connect(self._set_scale)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.pressed.connect(self._on_pressed)
        self.released.connect(self._on_released)

    # The animated values
    def hover_level(self) -> float:
        return self._hover

    def press_scale(self) -> float:
        return self._scale

    def _set_hover(self, value) -> None:
        self._hover = float(value)
        self.update()

    def _set_scale(self, value) -> None:
        self._scale = float(value)
        self.update()

    def _fade_to(self, level: float) -> None:
        self._hover_anim.stop()
        if not motion_enabled() or not self.isVisible():
            self._set_hover(level)
            return
        self._hover_anim.setDuration(HOVER_MS)
        self._hover_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(float(level))
        self._hover_anim.start()

    def _scale_to(self, scale: float, ms: int, curve: QEasingCurve.Type) -> None:
        self._scale_anim.stop()
        if not motion_enabled() or not self.isVisible():
            self._set_scale(1.0)
            return
        self._scale_anim.setDuration(ms)
        self._scale_anim.setEasingCurve(curve)
        self._scale_anim.setStartValue(self._scale)
        self._scale_anim.setEndValue(float(scale))
        self._scale_anim.start()

    def spring(self) -> None:
        """The bounce back (from a dip made here when there wasn't one: a menu opening at once)."""
        if not motion_enabled():
            return
        if self._scale >= 1.0:
            self._set_scale(PRESS_SCALE)
        self._scale_to(1.0, RELEASE_MS, QEasingCurve.Type.OutBack)

    # Events
    def _opens_menu_now(self, pos: QPoint) -> bool:
        """A press here opens the menu straight away (the menu then has the mouse until it closes)."""
        if self.menu() is None:
            return False
        mode = self.popupMode()
        if mode == QToolButton.ToolButtonPopupMode.InstantPopup:
            return True
        if mode == QToolButton.ToolButtonPopupMode.MenuButtonPopup:
            option = QStyleOptionToolButton()
            self.initStyleOption(option)
            arrow = self.style().subControlRect(QStyle.ComplexControl.CC_ToolButton, option,
                                                QStyle.SubControl.SC_ToolButtonMenu, self)
            return arrow.contains(pos)
        return False

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._opens_menu_now(event.position().toPoint()):
            self.spring()  # (before the menu opens: it keeps the release)
        super().mousePressEvent(event)

    def _on_pressed(self) -> None:
        if self.menu() is not None and self.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup:
            return  # (sprung already, in mousePressEvent)
        self._scale_to(PRESS_SCALE, PRESS_MS, QEasingCurve.Type.OutQuad)

    def _on_released(self) -> None:
        if self._scale < 1.0:
            self.spring()

    def enterEvent(self, event) -> None:
        if self.isEnabled():
            self._fade_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._fade_to(0.0)
        super().leaveEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self._hover_anim.stop()
            self._set_hover(0.0)
        super().changeEvent(event)

    def hideEvent(self, event) -> None:
        self._hover_anim.stop()
        self._scale_anim.stop()
        self._hover, self._scale = 0.0, 1.0
        super().hideEvent(event)

    # Painting
    def paintEvent(self, event) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        plain = QStyleOptionToolButton(option)
        plain.state &= ~QStyle.StateFlag.State_MouseOver
        if self._scale != 1.0:
            centre = QRectF(self.rect()).center()
            painter.translate(centre)
            painter.scale(self._scale, self._scale)
            painter.translate(-centre)
        level = self._hover if self.isEnabled() else 0.0
        if level < 1.0 or not hovered:
            painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, plain)
        if level > 0.0:
            option.state |= QStyle.StateFlag.State_MouseOver  # (fading out: the mouse has gone)
            painter.setOpacity(level)
            painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)
            painter.setOpacity(1.0)
        self.paint_over(painter)

    def paint_over(self, painter: QPainter) -> None:
        """Anything a kind of button draws on top (DropButton's chevron), dipping with it."""


def ghost_button(
    icon: Optional[str], tooltip: str, text: str = "", size: int = ICON_SIZE, checkable: bool = False,
    parent: Optional[QWidget] = None, tone: str = "accent",
) -> GhostButton:
    """A borderless button: an icon alone, or an icon and a word (text)."""
    button = GhostButton(parent)
    button.setObjectName("ghostText" if text else "ghost")
    if icon:
        set_icon(button, icon, tone, size=size)
    if text:
        button.setText(text)
        button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon if icon else Qt.ToolButtonStyle.ToolButtonTextOnly
        )
    button.setToolTip(tooltip)
    button.setCheckable(checkable)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def bar_separator(height: int = 20, parent: Optional[QWidget] = None) -> QFrame:
    line = QFrame(parent)
    line.setObjectName("barSep")
    line.setFixedSize(1, height)
    return line


class DropButton(GhostButton):
    """A borderless dropdown: an icon (a duotone name or a QIcon, or none), the current choice's name, and a
    chevron; its menu opens on a click."""

    def __init__(self, icon, text: str, tooltip: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ghostDrop")
        self.set_choice_icon(icon)
        self.setText(text)
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setMenu(QMenu(self))
        self.menu().setToolTipsVisible(True)

    def set_choice_icon(self, icon) -> None:
        if isinstance(icon, str):
            set_icon(self, icon, size=ICON_SIZE)
        elif isinstance(icon, QIcon):
            self.setIcon(icon)
            self.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        has_icon = not self.icon().isNull()
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if has_icon else Qt.ToolButtonStyle.ToolButtonTextOnly)

    def paint_over(self, painter: QPainter) -> None:
        from ui.themes import theme_color

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(theme_color("text_muted" if self.isEnabled() else "text_faint"), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        x, y = self.width() - 13.5, self.height() / 2 - 0.5
        painter.drawPolyline([QPointF(x - 3.5, y - 1.75), QPointF(x, y + 1.75), QPointF(x + 3.5, y - 1.75)])


class ComboDrop(DropButton):
    """A DropButton for combo's items (its separators and tooltips too); picking one sets the combo, which stays
    hidden as the choice. extra: (text, callback) entries after a line at the end of the menu."""

    def __init__(self, combo: QComboBox, icon, tooltip: str, extra: Sequence[tuple[str, Callable[[], None]]] = (),
                 parent: Optional[QWidget] = None):
        super().__init__(icon, combo.currentText(), tooltip, parent)
        self.combo = combo
        self.extra_actions = []  # (kept: only the combo's entries are made again each time the menu opens)
        for text, callback in extra:
            action = QAction(text, self)
            action.triggered.connect(lambda _checked=False, c=callback: c())
            self.extra_actions.append(action)
        combo.hide()
        combo.currentIndexChanged.connect(self.sync)
        self.menu().aboutToShow.connect(self._fill)
        self._fill()

    def sync(self, *_args) -> None:
        """Show the combo's choice (call it after the combo was changed with its signals blocked)."""
        self.setText(self.combo.currentText())

    def _fill(self) -> None:
        self.sync()
        menu, combo = self.menu(), self.combo
        menu.clear()
        current = combo.currentIndex()
        for index in range(combo.count()):
            if combo.itemData(index, Qt.ItemDataRole.AccessibleDescriptionRole) == "separator":
                menu.addSeparator()
                continue
            action = menu.addAction(combo.itemText(index))
            action.setCheckable(True)
            action.setChecked(index == current)
            tip = combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
            if tip:
                action.setToolTip(str(tip))
            action.triggered.connect(lambda _checked=False, i=index: self.pick(i))
        if self.extra_actions:
            menu.addSeparator()
            menu.addActions(self.extra_actions)

    def pick(self, index: int) -> None:
        """As choosing the combo's item index in the menu."""
        self.combo.setCurrentIndex(index)
        self.combo.activated.emit(index)
        self.sync()


class _SoftShadow(QGraphicsDropShadowEffect):
    """Darker under a dark theme, fainter on a light one (checked as it draws: the theme can change)."""

    def draw(self, painter: QPainter) -> None:
        from ui.themes import theme_color

        alpha = 40 if theme_color("window_bg").lightness() > 160 else 110
        if self.color().alpha() != alpha:
            self.setColor(QColor(0, 0, 0, alpha))
        super().draw(painter)


def reading_order(*parts: Optional[QWidget]) -> list[QWidget]:
    """The controls Tab can reach in parts (left out when None or hidden), part by part; inside a part, row by row
    from the top, left to right in a row (a bar's buttons, a panel's header then its list). For a window whose
    focus chain was made in another order (the main window's: its bars move between views)."""
    order: list[QWidget] = []
    for part in parts:
        if part is None or not part.isVisible():
            continue
        found = []
        for widget in part.findChildren(QWidget):
            if not (widget.focusPolicy() & Qt.FocusPolicy.TabFocus) or not widget.isVisible() or not widget.isEnabled():
                continue
            if widget.focusProxy() is not None or widget.window() is not part.window():
                continue
            at = widget.mapTo(part, QPoint(0, 0))
            found.append(((at.y() + widget.height() // 2) // 32, at.x(), at.y(), widget))
        order += [entry[-1] for entry in sorted(found, key=lambda entry: entry[:3])]
    return order


def soft_shadow(widget: QWidget) -> None:
    """Give a floating panel the floating bars' soft shadow."""
    effect = _SoftShadow(widget)
    effect.setBlurRadius(26)
    effect.setOffset(0, 4)
    effect.setColor(QColor(0, 0, 0, 110))
    widget.setGraphicsEffect(effect)


def fit_popup(combo: QComboBox) -> None:
    """A dropdown's list as wide as its longest entry (a borderless dropdown can be narrower than its list: the
    list would take its width and show only "..."). Call it after filling the combo."""
    widest = max((combo.fontMetrics().horizontalAdvance(combo.itemText(i)) for i in range(combo.count())), default=0)
    combo.view().setMinimumWidth(widest + 56)  # (the list's padding, an item's padding and a scroll bar)


def segmented(choices: Sequence[tuple[str, str, str]], current: str, parent: Optional[QWidget] = None
              ) -> tuple[QWidget, QButtonGroup]:
    """(value, text, tooltip) choices side by side in a soft track (the chosen one in the accent); each button's "value" property is its value."""
    track = QWidget(parent)
    track.setObjectName("segment")
    track.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    row = QHBoxLayout(track)
    row.setContentsMargins(3, 3, 3, 3)
    row.setSpacing(2)
    group = QButtonGroup(track)
    group.setExclusive(True)
    for value, text, tip in choices:
        button = GhostButton(track)
        button.setObjectName("seg")
        button.setText(text)
        button.setToolTip(tip)
        button.setCheckable(True)
        button.setChecked(value == current)
        button.setProperty("value", value)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        group.addButton(button)
        row.addWidget(button)
    return track, group


class FloatingBar(QWidget):
    """A rounded bar over host, at its bottom centre (gap: pixels above host's bottom edge). Add controls to
    .row; reveal() / conceal() show and hide it with a short fade."""

    SHADOW = 14  # room around the bar for its shadow

    def __init__(self, host: QWidget, gap: int = 14, object_name: str = "floatingBar", vertical: bool = False):
        super().__init__(host)
        self.host = host
        self.gap = gap
        self._watched: Optional[QWidget] = None  # (fade_when_idle)
        self._idle: Optional[QTimer] = None
        self._followed: list[QWidget] = []  # (follow)
        self._below: Optional[FloatingBar] = None  # (stack_on)
        self._above: list[FloatingBar] = []
        self._stack_spacing = 6
        self.wanted = True
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(self.SHADOW, self.SHADOW - 4, self.SHADOW, self.SHADOW + 4)
        self.frame = QFrame(self)
        self.frame.setObjectName(object_name)
        outer.addWidget(self.frame)
        self.row = QVBoxLayout(self.frame) if vertical else QHBoxLayout(self.frame)
        self.row.setContentsMargins(8, 5, 8, 5)
        self.row.setSpacing(2)
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._on_faded)
        self._leaving = False
        host.installEventFilter(self)
        self.hide()

    # Showing and hiding ----------------------------------------------------
    def reveal(self) -> None:
        self._leaving = False
        if self._idle is not None:
            self._idle.start()  # (shown again: it has the whole while before fading)
        self.place()
        if not self.isHidden() and self._fade.state() != QPropertyAnimation.State.Running:
            self.raise_()
            return
        if self.isHidden():
            self._opacity.setOpacity(0.0 if motion_enabled() else 1.0)  # (fades in from nothing)
        self.show()
        self.raise_()
        self._fade_to(1.0)
        for bar in self._above:
            if bar.wanted:
                bar.reveal()

    def conceal(self) -> None:
        for bar in self._above:
            bar.conceal()
        if self.isHidden() or self._leaving:
            return
        self._leaving = True
        self._fade_to(0.0)

    def showing(self) -> bool:
        """On screen, and not fading away."""
        return not self.isHidden() and not self._leaving

    # A bar on top of another -------------------------------------------------
    def stack_on(self, below: "FloatingBar", spacing: int = 6) -> None:
        """Sit just above below (centred), shown and hidden with it while wanted (set_wanted)."""
        self._below = below
        self._stack_spacing = spacing
        below._above.append(self)

    def set_wanted(self, wanted: bool) -> None:
        self.wanted = wanted
        if wanted and self._below is not None and self._below.showing():
            self.reveal()
        elif not wanted:
            self.conceal()

    def _fade_to(self, opacity: float) -> None:
        self._fade.stop()
        if not motion_enabled():
            self._opacity.setOpacity(opacity)
            self._on_faded()
            return
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(opacity)
        self._fade.start()

    def _on_faded(self) -> None:
        if self._leaving:
            self._leaving = False
            self.hide()
            self._opacity.setOpacity(1.0)

    # Place -----------------------------------------------------------------
    def natural_size(self) -> QSize:
        """The size the bar needs now (worked out at once, not at the next layout pass)."""
        for layout in (self.row, self.layout()):
            layout.invalidate()
            layout.activate()
        return self.layout().sizeHint()

    def place(self) -> None:
        self.resize(self.natural_size())
        x = max(0, (self.host.width() - self.width()) // 2)
        if self._below is not None:  # (its frame's bottom just above the other's frame)
            below = self._below
            frame_bottom = self.height() - self.layout().contentsMargins().bottom()  # (its frame's, from the size just set)
            y = below.y() + below.layout().contentsMargins().top() - self._stack_spacing - frame_bottom
        else:
            y = self.host.height() - self.height() - self.gap + self.SHADOW
        self.move(QPoint(x, max(0, y)))
        for bar in self._above:
            if not bar.isHidden():
                bar.place()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.host and event.type() == QEvent.Type.Resize and not self.isHidden():
            self.place()  # (shown, even before its window is on screen)
        elif obj in self._followed and event.type() in (QEvent.Type.Move, QEvent.Type.Resize) and not self.isHidden():
            self.place()
        if obj is self._watched and event.type() in _ACTIVITY:
            self.reveal()
            self._idle.start()
        return False

    def follow(self, widget: QWidget) -> None:
        """Place the bar again whenever widget moves or resizes (what its position is worked out from)."""
        self._followed.append(widget)
        widget.installEventFilter(self)

    # Fading while the mouse is still -------------------------------------
    def fade_when_idle(self, watched: QWidget, ms: int = IDLE_MS) -> None:
        """Hide after the mouse has been still over watched for ms (not while it's on the bar or one of its
        menus is open); any movement there brings the bar back."""
        self._watched = watched
        watched.setMouseTracking(True)
        watched.installEventFilter(self)
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(ms)
        self._idle.timeout.connect(self._on_idle)
        self._idle.start()

    def _on_idle(self) -> None:
        if any(bar.underMouse() for bar in [self, *self._above]) or QApplication.activePopupWidget() is not None:
            self._idle.start()
            return
        self.conceal()

    def paintEvent(self, event) -> None:
        """A soft shadow under the bar (the bar itself is the stylesheet's #floatingBar / #floatingCard)."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.frame.geometry())
        # (fainter on a light theme, where a dark halo would show as a ring)
        light = self.frame.palette().window().color().lightness() > 160
        layers = ((10, 3), (7, 5), (4, 7), (2, 9)) if light else ((12, 7), (8, 11), (5, 16), (2, 24))
        for spread, alpha in layers:
            path = QPainterPath()
            path.addRoundedRect(box.adjusted(-spread, -spread + 3, spread, spread + 3), 14 + spread, 14 + spread)
            painter.fillPath(path, QColor(0, 0, 0, alpha))


_ACTIVITY = (QEvent.Type.MouseMove, QEvent.Type.Enter, QEvent.Type.Wheel, QEvent.Type.MouseButtonPress)


class FloatingCard(FloatingBar):
    """A floating panel at host's top right, width wide; set_content() puts a widget in it, which scrolls when
    taller than host (a viewer's details). top(): how far down host it starts (below a view's own tool bars)."""

    def __init__(self, host: QWidget, width: int = 300, gap: int = 14, top: Optional[Callable[[], int]] = None):
        super().__init__(host, gap, "floatingCard", vertical=True)
        self.card_width = width
        self.top = top or (lambda: 0)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(self.frame)
        self.scroll.setObjectName("floatingCardScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.row.addWidget(self.scroll)

    def set_content(self, widget: QWidget) -> None:
        old = self.scroll.takeWidget()
        widget.setAutoFillBackground(False)
        self.scroll.setWidget(widget)
        if old is not None:
            old.deleteLater()
        if not self.isHidden():
            self.place()

    def place(self) -> None:
        margins = self.layout().contentsMargins()
        content = self.scroll.widget()
        wanted = (content.sizeHint().height() if content is not None else 120) + 2
        top = self.top()
        room = max(120, self.host.height() - top - 2 * self.gap)
        self.resize(self.card_width + margins.left() + margins.right(), min(wanted, room) + margins.top() + margins.bottom())
        x = self.host.width() - self.card_width - self.gap - margins.left()
        self.move(QPoint(max(0, x), max(0, top + self.gap - margins.top())))


# ------------------ Settings cards and the dialog standard ------------------
def section_heading(text: str, parent: Optional[QWidget] = None) -> QLabel:
    """A small caps heading over a group of setting cards, or over a section of a dialog."""
    label = QLabel(text.upper(), parent)
    label.setObjectName("sectionHeading")
    return label


class SettingCard(QFrame):
    """One setting in a rounded card: its name and a line on what it does on the left, its control on the right
    (add_control), and anything wide (a path field, a list, swatches) under them, across the card (add_below)."""

    def __init__(self, title: str, about: str = "", control: Optional[QWidget] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("settingCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("cardTitle")
        text.addWidget(self.title_label)
        self.about_label = QLabel(about, self)
        self.about_label.setObjectName("pageHint")
        self.about_label.setWordWrap(True)
        self.about_label.setVisible(bool(about))
        text.addWidget(self.about_label)
        row.addLayout(text, 1)
        self.controls = QHBoxLayout()
        self.controls.setSpacing(6)
        row.addLayout(self.controls)
        outer.addLayout(row)
        self.below = QVBoxLayout()
        self.below.setSpacing(6)
        outer.addLayout(self.below)
        if control is not None:
            self.add_control(control)

    def add_control(self, widget: QWidget) -> QWidget:
        self.controls.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        return widget

    def add_below(self, item) -> None:
        """A widget or a layout under the name, across the card."""
        if isinstance(item, QWidget):
            self.below.addWidget(item)
        else:
            self.below.addLayout(item)

    def set_about(self, text: str) -> None:
        self.about_label.setText(text)
        self.about_label.setVisible(bool(text))


class DialogFrame:
    """Lays a dialog out the standard way: its title and a line on what it does at the top, the body (.body, a
    QVBoxLayout to fill), and a footer bar (add_footer) with any extra actions on the left, then Cancel (or
    Close) and the one filled main button at the far right."""

    def __init__(self, dialog: QWidget, title: str, about: str = ""):
        self.dialog = dialog
        self.root = QVBoxLayout(dialog)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(0)
        self.header = QWidget(dialog)
        self.header.setObjectName("dialogHeader")
        head = QVBoxLayout(self.header)
        head.setContentsMargins(22, 18, 22, 4)
        head.setSpacing(4)
        self.title = QLabel(title, self.header)
        self.title.setObjectName("dialogTitle")
        head.addWidget(self.title)
        self.about = QLabel(about, self.header)
        self.about.setObjectName("pageHint")
        self.about.setWordWrap(True)
        self.about.setVisible(bool(about))
        head.addWidget(self.about)
        self.root.addWidget(self.header)
        body = QWidget(dialog)
        body.setObjectName("dialogBody")
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(22, 10, 22, 18)
        self.body.setSpacing(8)
        self.root.addWidget(body, 1)
        self.footer: Optional[DialogFooter] = None

    def add_footer(self, primary: str = "", primary_tip: str = "", cancel: str = "Cancel",
                   cancel_tip: str = "Close without changing anything",
                   on_primary: Optional[Callable[[], None]] = None) -> "DialogFooter":
        if not any(self.body.stretch(i) for i in range(self.body.count())):
            self.body.addStretch(1)  # (a short dialog's spare room goes under its content, not between its sections)
        self.footer = DialogFooter(self.dialog, primary, primary_tip, cancel, cancel_tip, on_primary)
        self.root.addWidget(self.footer)
        return self.footer


class DialogFooter(QFrame):
    """The bar at the bottom of a dialog (DialogFrame.add_footer): extras on the left (add_extra), then Cancel and the
    filled main button (.btn_primary: dialog.accept unless on_primary is given) on the right."""

    def __init__(self, dialog: QWidget, primary: str = "", primary_tip: str = "", cancel: str = "Cancel",
                 cancel_tip: str = "Close without changing anything", on_primary: Optional[Callable[[], None]] = None):
        super().__init__(dialog)
        self.setObjectName("dialogFooter")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(16, 10, 16, 10)
        self.row.setSpacing(8)
        self.extras = QHBoxLayout()
        self.extras.setSpacing(6)
        self.row.addLayout(self.extras)
        self.row.addStretch(1)
        self.btn_cancel: Optional[QPushButton] = None
        if cancel:
            self.btn_cancel = QPushButton(cancel, self)
            self.btn_cancel.setToolTip(cancel_tip)
            self.btn_cancel.clicked.connect(dialog.reject)
            self.row.addWidget(self.btn_cancel)
        self.btn_primary: Optional[QPushButton] = None
        if primary:
            self.btn_primary = QPushButton(primary, self)
            self.btn_primary.setObjectName("primaryBtn")
            self.btn_primary.setToolTip(primary_tip)
            self.btn_primary.setDefault(True)
            self.btn_primary.clicked.connect(on_primary or dialog.accept)
            self.row.addWidget(self.btn_primary)

    def add_extra(self, widget: QWidget) -> QWidget:
        """An action of its own, at the footer's left (Open a GitHub Issue, Select All...)."""
        widget.setParent(self)
        self.extras.addWidget(widget)
        return widget

    def add_status(self, label: QLabel) -> QLabel:
        """A line of status (progress, a warning) between the extras and the buttons."""
        label.setParent(self)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.row.insertWidget(1, label, 1)  # (just after the extras, on the left)
        return label
