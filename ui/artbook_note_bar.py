"""The format bar that floats above the selected note on the Artbook board.

Text colour, highlight and the note's fill colour (each: the theme default, white, black, or a
custom one picked live in ui.color_popup), bold / italic / underline, font size and font, and a menu with alignment and
clearing the formatting. With text selected while typing, changes apply to that text; otherwise
to the whole note. The board (ui.artbook_board) does the formatting and records it for undo.

Nothing here takes the keyboard focus: clicking the bar mustn't end typing in the note.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFontComboBox,
    QFrame,
    QHBoxLayout,
    QMenu,
    QToolButton,
    QWidget,
)

from ui import shortcuts
from ui.color_popup import ColorPopup, color_text
from ui.duotone import set_icon

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

FONT_SIZES = (8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 36, 48, 64, 72, 96)
SWATCHES = (("White", "#ffffff"), ("Black", "#111111"))


def swatch_icon(color: Optional[str]) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor(128, 128, 128, 160))
    if color:
        painter.setBrush(QColor(color))
    painter.drawEllipse(QRectF(4, 4, 24, 24))
    if not color:  # "Default": a diagonal line through an empty circle
        painter.drawLine(10, 22, 22, 10)
    painter.end()
    return QIcon(pixmap)


class ColorButton(QToolButton):
    """An icon with a bar of the current colour under it; its menu picks a colour: the theme's,
    white, black, or Custom... (a floating picker that changes the colour live, kept as one undo
    step: on_live(True) when it opens, on_live(False) when it closes). The picker opens beside
    avoid(), a rectangle on screen it mustn't cover."""

    def __init__(
        self, icon: str, tooltip: str, on_pick: Callable[[Optional[str]], None],
        on_live: Optional[Callable[[bool], None]] = None, avoid: Optional[Callable[[], QRect]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.color: Optional[QColor] = None
        self.title = tooltip.split(" (")[0]
        self.on_pick = on_pick
        self.on_live = on_live
        self.avoid = avoid
        self.popup: Optional[ColorPopup] = None
        set_icon(self, icon, size=16)
        self.setToolTip(tooltip)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setFixedSize(30, 28)
        menu = QMenu(self)
        default = menu.addAction(swatch_icon(None), "Theme Default")
        default.triggered.connect(self._on_default)
        for name, hex_color in SWATCHES:
            action = menu.addAction(swatch_icon(hex_color), name)
            action.setData(hex_color)
            action.triggered.connect(self._on_swatch)
        menu.addSeparator()
        menu.addAction("Custom...").triggered.connect(self.open_custom)
        self.setMenu(menu)
        self.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")

    def set_color(self, color: Optional[QColor]) -> None:
        self.color = color
        self.update()

    def _on_default(self) -> None:
        self.on_pick(None)

    def _on_swatch(self) -> None:
        self.on_pick(self.sender().data())

    def open_custom(self) -> None:
        """The custom colour picker, beside avoid() (one at a time)."""
        self.close_picker()
        self.popup = ColorPopup(f"Select {self.title.lower()}", self.color or QColor("#ffffff"), self)
        self.popup.changed.connect(self._on_live_color)
        self.popup.default_picked.connect(self._on_live_default)
        self.popup.finished.connect(self._on_popup_closed)
        if self.on_live is not None:
            self.on_live(True)
        self.popup.show_beside(self.avoid() if self.avoid is not None else QRect(self.mapToGlobal(QPoint(0, 0)), self.size()))

    def close_picker(self) -> None:
        if self.popup is not None:
            self.popup.close()

    def _on_live_color(self, color: QColor) -> None:
        self.on_pick(color_text(color))

    def _on_live_default(self) -> None:
        self.on_pick(None)
        if self.popup is not None and self.color is not None:
            self.popup.set_color(self.color)  # show the theme's colour it now has

    def _on_popup_closed(self) -> None:
        self.popup = None
        if self.on_live is not None:
            self.on_live(False)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.color if self.color is not None and self.color.isValid() else QColor(128, 128, 128, 90))
        painter.drawRoundedRect(QRectF(7, self.height() - 6, self.width() - 14, 3), 1.5, 1.5)


class NoteBar(QFrame):
    """See the module docstring. sync() shows the target note's formatting."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setObjectName("noteBar")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)

        live, avoid = board.live_change, self.avoid_rect
        self.btn_color = ColorButton("text_color", "Text colour", board.set_note_text_color, live, avoid)
        self.btn_highlight = ColorButton("highlight", "Highlight colour (behind the text)", board.set_note_highlight, live, avoid)
        self.btn_fill = ColorButton("fill", "Note colour", board.set_note_fill, live, avoid)
        for button in (self.btn_color, self.btn_highlight, self.btn_fill):
            layout.addWidget(button)
        layout.addWidget(self._divider())

        self.btn_bold = self._style_button("B", "bold", f"Bold ({shortcuts.get('artbook.note_bold').keys} while typing)", board.toggle_note_bold)
        self.btn_italic = self._style_button("I", "italic", f"Italic ({shortcuts.get('artbook.note_italic').keys} while typing)", board.toggle_note_italic)
        self.btn_underline = self._style_button("U", "underline", f"Underline ({shortcuts.get('artbook.note_underline').keys} while typing)", board.toggle_note_underline)
        for button in (self.btn_bold, self.btn_italic, self.btn_underline):
            layout.addWidget(button)
        layout.addWidget(self._divider())

        self.size_box = QComboBox()
        self.size_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.size_box.setToolTip("Text size")
        for size in FONT_SIZES:
            self.size_box.addItem(str(size), size)
        self.size_box.activated.connect(self._on_size_picked)
        layout.addWidget(self.size_box)
        self.font_box = QFontComboBox()
        self.font_box.setEditable(False)
        self.font_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.font_box.setToolTip("Font")
        self.font_box.setMaximumWidth(150)
        self.font_box.activated.connect(self._on_font_picked)
        layout.addWidget(self.font_box)
        layout.addWidget(self._divider())

        self.btn_more = QToolButton()
        set_icon(self.btn_more, "chevron_down", size=16)
        self.btn_more.setToolTip("More: alignment, clear formatting")
        self.btn_more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_more.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        more = QMenu(self.btn_more)
        self.align_actions = {}
        for alignment, (text, icon) in {
            Qt.AlignmentFlag.AlignLeft: ("Align Left", "align_left"),
            Qt.AlignmentFlag.AlignHCenter: ("Align Center", "align_center"),
            Qt.AlignmentFlag.AlignRight: ("Align Right", "align_right"),
        }.items():
            action = more.addAction(text)
            set_icon(action, icon)
            action.setCheckable(True)
            action.setData(alignment)
            action.triggered.connect(self._on_align_picked)
            self.align_actions[alignment] = action
        more.addSeparator()
        more.addAction("Clear Formatting").triggered.connect(board.clear_note_format)
        self.btn_more.setMenu(more)
        layout.addWidget(self.btn_more)
        self.adjustSize()
        self.hide()

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("noteBarDivider")
        line.setFixedSize(1, 18)
        return line

    def _style_button(self, letter: str, style: str, tooltip: str, handler: Callable[[], None]) -> QToolButton:
        button = QToolButton()
        button.setText(letter)
        font = QFont(button.font())
        font.setBold(style == "bold")
        font.setItalic(style == "italic")
        font.setUnderline(style == "underline")
        font.setPointSizeF(font.pointSizeF() + 1)
        button.setFont(font)
        button.setCheckable(True)
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setFixedSize(28, 28)
        button.clicked.connect(handler)
        return button

    # ------------------ handlers ------------------
    def _on_size_picked(self, index: int) -> None:
        self.board.set_note_font_size(self.size_box.itemData(index))

    def _on_font_picked(self, _index: int) -> None:
        self.board.set_note_font_family(self.font_box.currentFont().family())

    def _on_align_picked(self) -> None:
        self.board.set_note_alignment(self.sender().data())

    # ------------------ the colour pickers ------------------
    def avoid_rect(self) -> QRect:
        """The bar and its note on screen: what an open colour picker mustn't cover."""
        area = QRect(self.mapToGlobal(QPoint(0, 0)), self.size())
        note = self.board.note_bar_target()
        if note is not None:
            box = self.board.mapFromScene(note.sceneBoundingRect()).boundingRect()
            area = area.united(QRect(self.board.mapToGlobal(box.topLeft()), box.size()))
        return area

    def close_pickers(self) -> None:
        for button in (self.btn_color, self.btn_highlight, self.btn_fill):
            button.close_picker()

    # ------------------ showing ------------------
    def sync(self, formats: dict[str, Any]) -> None:
        """Show a note's formatting (from BoardView.note_formats)."""
        self.btn_color.set_color(formats["color"])
        self.btn_highlight.set_color(formats["highlight"])
        self.btn_fill.set_color(formats["fill"])
        for button, on in ((self.btn_bold, formats["bold"]), (self.btn_italic, formats["italic"]), (self.btn_underline, formats["underline"])):
            button.blockSignals(True)
            button.setChecked(on)
            button.blockSignals(False)
        size = round(formats["size"])
        index = self.size_box.findData(size)
        if index < 0:
            self.size_box.addItem(str(size), size)
            index = self.size_box.count() - 1
        self.size_box.setCurrentIndex(index)
        self.font_box.blockSignals(True)
        self.font_box.setCurrentFont(QFont(formats["family"]))
        self.font_box.blockSignals(False)
        for alignment, action in self.align_actions.items():
            action.setChecked(alignment == formats["align"])

    def sizeHint(self) -> QSize:
        return self.layout().sizeHint()
