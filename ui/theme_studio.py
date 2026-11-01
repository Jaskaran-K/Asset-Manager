"""The Theme Studio: a window for making themes and backgrounds (AssetManager.exe --theme-studio).

On the left, every colour token of a theme (grouped by where it shows, each with a swatch, its
value and a way back to the base theme's) and the background: none, a built-in texture, a
pattern made from settings, or a tile made from an image of your own (ui.pattern_tiles). On the
right, a preview built from the app's own widgets and its real stylesheet (ui.themes
.stylesheet_from), redrawn as you go. A theme is saved as a .amtheme file (ui.theme_pack): plain
data, which can be handed over to go into the app (Copy for the App gives its THEMES entry).

The studio runs on its own: the app doesn't load any of this unless it's started with
--theme-studio, and the studio doesn't change the app's settings.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

import shiboken6
from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QIcon, QImage, QLinearGradient, QPainter, QPen, QPixmap, QRegion
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyleOption,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui import pattern_tiles, shortcuts, theme_pack
from ui.artbook_board import GROUP_DEFAULT_ALPHA
from ui.color_popup import ColorPopup
from ui.duotone import DISABLED, ICONS, TONES, DuotoneGlyph, set_icon
from ui.glass import GlassPanel
from ui.themes import THEMES, palette_from, parse_color, stylesheet_from
from ui.views import RowHighlightDelegate

PREVIEW_DELAY_MS = 120  # after the last change, the preview is redrawn
SWATCH = QSize(34, 22)
TILE_PREVIEW_PX = 220
# Where each token shows, grouped as the editor lists them: (group, [(token, name, where)])
TOKEN_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("Surfaces", [
        ("window_bg", "Window", "Behind everything: windows and dialogs (a colour or a gradient)"),
        ("surface", "Panels", "Toolbars, headers and panels"),
        ("surface_alt", "Panels (second)", "Viewer toolbars: the image, PDF and video viewers"),
        ("sidebar_bg", "Sidebar", "The sidebar and the inspector on the right"),
        ("grid_bg", "Asset grid", "Behind the thumbnails"),
        ("border", "Borders", "Lines between panels, frames"),
        ("hover", "Hover", "A row or small button under the pointer"),
    ]),
    ("Text", [
        ("text", "Text", "Most text"),
        ("text_strong", "Headings", "Titles and headings"),
        ("text_muted", "Muted text", "Secondary text: details, labels"),
        ("text_faint", "Faint text", "Hints and placeholders"),
    ]),
    ("Buttons", [
        ("button", "Button", "Buttons"),
        ("button_border", "Button border", ""),
        ("button_hover", "Button, hovered", ""),
        ("button_border_hover", "Button border, hovered", ""),
        ("button_pressed", "Button, pressed", ""),
        ("button_disabled", "Button, unavailable", ""),
    ]),
    ("Text boxes", [
        ("input_bg", "Text box", "Search box, text boxes, lists in dialogs"),
        ("input_border", "Text box border", ""),
    ]),
    ("Accent", [
        ("accent", "Accent", "Main buttons, selection, sliders, ticks"),
        ("accent_border", "Accent border", "Around main buttons and selected thumbnails"),
        ("accent_hover", "Accent, hovered", "A main button under the pointer"),
        ("accent_soft", "Accent, soft", "Buttons that are on (tabs, toggles)"),
        ("accent_text", "Accent text", "Links, inspector headings and the details of icons"),
    ]),
    ("Thumbnails", [
        ("card", "Card", "A thumbnail's card in the grid"),
        ("card_border", "Card border", ""),
        ("card_hover", "Card, hovered", ""),
        ("card_hover_border", "Card border, hovered", ""),
        ("card_selected", "Card, selected", ""),
        ("card_selected_text", "Selected text", "Text on selected cards and on buttons that are on"),
        ("thumb_card", "Placeholder card", "Behind a file's icon until its picture is ready"),
        ("thumb_border", "Placeholder border", ""),
        ("thumb_badge", "File-type badge", "The little EXT label on placeholders"),
        ("thumb_badge_text", "File-type badge text", ""),
    ]),
    ("Scroll bars", [
        ("scroll_track", "Track", ""),
        ("scroll_handle", "Handle", ""),
        ("scroll_handle_hover", "Handle, hovered", ""),
    ]),
    ("Messages", [
        ("danger", "Danger", "Delete buttons and errors"),
        ("warn_text", "Warning text", ""), ("warn_bg", "Warning", "Warning boxes"), ("warn_border", "Warning border", ""),
        ("note_text", "Note text", ""), ("note_bg", "Note", "Note boxes"), ("note_border", "Note border", ""),
        ("info_text", "Info text", ""), ("info_bg", "Info", "Info boxes"), ("info_border", "Info border", ""),
    ]),
    ("Glass and background", [
        ("glass_tint", "Glass tint", "The frosted sidebar's colour (see-through)"),
        ("glass_edge", "Glass edge", "The frosted sidebar's edge highlight"),
        ("texture_ink", "Texture ink", "The colour background textures are drawn in"),
        ("board_area", "Artbook active area", "Behind the pictures on an Artbook page (see-through)"),
    ]),
]
# Text and what it's read against, for the contrast badges: text token -> background token
CONTRAST_PAIRS = {
    "text": "surface", "text_strong": "window_bg", "text_muted": "surface", "text_faint": "surface",
    "accent_text": "surface", "card_selected_text": "card_selected", "thumb_badge_text": "thumb_badge",
    "warn_text": "warn_bg", "note_text": "note_bg", "info_text": "info_bg",
}
GRADIENT_DIRECTIONS = {
    "Diagonal": (0, 0, 1, 1), "Down": (0, 0, 0, 1), "Across": (0, 0, 1, 0), "Up": (0, 1, 0, 0),
}
_PREVIEW_SAMPLES = [
    ("Hero_Character.fbx", ".fbx", "3D Models", "FBX"), ("Crate.obj", ".obj", "3D Models", "OBJ"),
    ("Helmet.glb", ".glb", "3D Models", "glTF"), ("Brick_Albedo.png", ".png", "Textures", "PNG"),
    ("Rock_Normal.png", ".png", "Textures", "PNG"), ("Style_Guide.pdf", ".pdf", "Documents", "PDF"),
    ("Turntable.mp4", ".mp4", "Videos", "MP4"), ("Moodboard.jpg", ".jpg", "Textures", "JPG"),
    ("Sword.fbx", ".fbx", "3D Models", "FBX"), ("Notes.pdf", ".pdf", "Documents", "PDF"),
    ("Walk_Cycle.mp4", ".mp4", "Videos", "MP4"), ("Bark_Rough.png", ".png", "Textures", "PNG"),
]


def contrast_ratio(a: QColor, b: QColor) -> float:
    """WCAG contrast between two colours (1 to 21; 4.5 is easy to read for body text)."""

    def luminance(color: QColor) -> float:
        def channel(value: float) -> float:
            return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

        return 0.2126 * channel(color.redF()) + 0.7152 * channel(color.greenF()) + 0.0722 * channel(color.blueF())

    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def color_text(color: QColor) -> str:
    """How a picked colour is written: #rrggbb, or #aarrggbb when it's see-through."""
    return color.name(QColor.NameFormat.HexRgb if color.alpha() == 255 else QColor.NameFormat.HexArgb)


def swatch_pixmap(value: str, size: QSize = SWATCH) -> QPixmap:
    """A token's colour (a gradient's stops side by side) over a checkerboard, for its swatch."""
    pixmap = QPixmap(size)
    pixmap.fill(QColor("#ffffff"))
    painter = QPainter(pixmap)
    for x in range(0, size.width(), 6):
        for y in range(0, size.height(), 6):
            if (x // 6 + y // 6) % 2:
                painter.fillRect(x, y, 6, 6, QColor("#c8c8c8"))
    stops = theme_pack.gradient_stops(value)
    if stops:
        width = size.width() / len(stops)
        for index, (_at, color) in enumerate(stops):
            painter.fillRect(round(index * width), 0, round(width) + 1, size.height(), theme_pack.first_color(color))
    elif theme_pack.valid_value("window_bg", value):
        painter.fillRect(pixmap.rect(), theme_pack.first_color(value))
    painter.setPen(QPen(QColor(0, 0, 0, 90), 1))
    painter.drawRect(pixmap.rect().adjusted(0, 0, -1, -1))
    painter.end()
    return pixmap


_picker: Optional[ColorPopup] = None  # (one colour picker open at a time)


def open_picker(anchor: QWidget, title: str, color: QColor, on_change: Callable[[QColor], None],
                on_default: Optional[Callable[[], None]] = None) -> ColorPopup:
    """The Artbook's colour picker (square, hue, opacity, dropper) with number sliders, beside
    anchor. on_change gets every change as it's made: the theme follows live."""
    global _picker
    if _picker is not None and shiboken6.isValid(_picker):
        _picker.close()
    picker = ColorPopup(title, color, anchor.window(), channels=True)
    picker.changed.connect(on_change)
    if on_default is not None:
        picker.default_picked.connect(on_default)
    top_left = anchor.mapToGlobal(anchor.rect().topLeft())
    picker.show_beside(QRect(top_left, anchor.size()))
    _picker = picker
    return picker


def _direction_of(value: str) -> str:
    """Which GRADIENT_DIRECTIONS a gradient runs (Diagonal if it's another way)."""
    match = re.search(r"x1:\s*([-\d.]+),\s*y1:\s*([-\d.]+),\s*x2:\s*([-\d.]+),\s*y2:\s*([-\d.]+)", value)
    if match:
        coords = tuple(round(float(v)) for v in match.groups())
        for name, direction in GRADIENT_DIRECTIONS.items():
            if direction == coords:
                return name
    return "Diagonal"


class ColorField(QWidget):
    """One token: its swatch (click for the colour picker, which changes it live), its value (type
    it) and a way back to the base theme's. Surfaces can be a gradient: two or three colours
    (each with its own swatch) running one way."""

    changed = Signal(str, str)  # token, its new value (only valid ones)
    STOP_NAMES = ("From", "Middle", "To")

    def __init__(self, key: str, name: str, where: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.key = key
        self.base_value = ""
        self.stops = [QColor("#000000"), QColor("#000000"), QColor("#000000")]  # (from, middle, to)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 1, 0, 1)
        column.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(6)
        column.addLayout(row)
        self.label = QLabel(name)
        self.label.setToolTip(f"{where}\n(token: {key})" if where else f"token: {key}")
        self.label.setMinimumWidth(150)
        row.addWidget(self.label, 1)
        self.chk_gradient: Optional[QCheckBox] = None
        if key in theme_pack.GRADIENT_TOKENS:
            self.chk_gradient = QCheckBox("Gradient", self)
            self.chk_gradient.setToolTip("Make it a gradient of two or three colours")
            self.chk_gradient.toggled.connect(self._on_gradient_toggled)
            row.addWidget(self.chk_gradient)
        self.btn_swatch = QPushButton()
        self.btn_swatch.setIconSize(SWATCH)
        self.btn_swatch.setFixedSize(SWATCH + QSize(10, 8))
        self.btn_swatch.setToolTip("Pick this colour: it changes live as you pick")
        self.btn_swatch.clicked.connect(self._pick)
        row.addWidget(self.btn_swatch)
        self.edit = QLineEdit()
        self.edit.setFixedWidth(150)
        self.edit.setToolTip("The colour: #rrggbb, #aarrggbb (see-through), rgba(r, g, b, a) or transparent")
        self.edit.textEdited.connect(self._on_typed)
        row.addWidget(self.edit)
        self.contrast = QLabel("")
        self.contrast.setFixedWidth(64)
        self.contrast.setVisible(key in CONTRAST_PAIRS)
        row.addWidget(self.contrast)
        self.btn_reset = QPushButton()
        set_icon(self.btn_reset, "refresh")
        self.btn_reset.setToolTip("Back to the base theme's colour")
        self.btn_reset.clicked.connect(self.reset)
        row.addWidget(self.btn_reset)

        self.gradient_row = QWidget()
        stops = QHBoxLayout(self.gradient_row)
        stops.setContentsMargins(24, 0, 0, 4)
        stops.setSpacing(6)
        self.stop_buttons: list[QPushButton] = []
        self.chk_middle = QCheckBox(self.STOP_NAMES[1], self.gradient_row)
        self.chk_middle.setToolTip("A third colour halfway")
        self.chk_middle.toggled.connect(self._on_gradient_edited)
        for index, stop_name in enumerate(self.STOP_NAMES):
            stops.addWidget(self.chk_middle if index == 1 else QLabel(stop_name))
            button = QPushButton()
            button.setIconSize(SWATCH)
            button.setFixedSize(SWATCH + QSize(10, 8))
            button.setToolTip(f"Pick the gradient's {stop_name.lower()} colour: it changes live as you pick")
            button.clicked.connect(self._make_stop_picker(index))
            self.stop_buttons.append(button)
            stops.addWidget(button)
        self.cmb_direction = QComboBox()
        self.cmb_direction.addItems(list(GRADIENT_DIRECTIONS))
        self.cmb_direction.setToolTip("Which way the colours run")
        self.cmb_direction.currentIndexChanged.connect(self._on_gradient_edited)
        stops.addWidget(self.cmb_direction)
        stops.addStretch(1)
        self.gradient_row.setVisible(False)
        column.addWidget(self.gradient_row)

    # ------------------ the value ------------------
    def value(self) -> str:
        return self.edit.text().strip()

    def is_gradient(self) -> bool:
        return self.chk_gradient is not None and self.chk_gradient.isChecked()

    def set_value(self, value: str, base_value: Optional[str] = None) -> None:
        if base_value is not None:
            self.base_value = base_value
        self.edit.setText(value)
        stops = theme_pack.gradient_stops(value)
        if self.chk_gradient is not None:
            for widget in (self.chk_gradient, self.chk_middle, self.cmb_direction):
                widget.blockSignals(True)
            self.chk_gradient.setChecked(bool(stops))
            if stops:
                colors = [parse_color(color) for _at, color in stops]
                self.stops = [colors[0], colors[len(colors) // 2], colors[-1]]
                self.chk_middle.setChecked(len(colors) > 2)
                self.cmb_direction.setCurrentText(_direction_of(value))
            for widget in (self.chk_gradient, self.chk_middle, self.cmb_direction):
                widget.blockSignals(False)
            self.gradient_row.setVisible(bool(stops))
        self._show(value, valid=True)

    def gradient_value(self) -> str:
        x1, y1, x2, y2 = GRADIENT_DIRECTIONS[self.cmb_direction.currentText()]
        colors = self.stops if self.chk_middle.isChecked() else [self.stops[0], self.stops[2]]
        places = [index / (len(colors) - 1) for index in range(len(colors))]
        stops = ", ".join(f"stop:{at:g} {color_text(color)}" for at, color in zip(places, colors))
        return f"qlineargradient(x1:{x1}, y1:{y1}, x2:{x2}, y2:{y2}, {stops})"

    def _show(self, value: str, valid: bool) -> None:
        self.edit.setStyleSheet("" if valid else "QLineEdit { border: 1px solid #ef4444; }")
        if valid:
            self.btn_swatch.setIcon(QIcon(swatch_pixmap(value)))
            if self.chk_gradient is not None:
                for index, button in enumerate(self.stop_buttons):
                    button.setIcon(QIcon(swatch_pixmap(color_text(self.stops[index]))))
                self.stop_buttons[1].setEnabled(self.chk_middle.isChecked())
        self.btn_reset.setEnabled(value != self.base_value)

    def _on_typed(self, text: str) -> None:
        if theme_pack.valid_value(self.key, text):
            position = self.edit.cursorPosition()
            self.set_value(text.strip())  # (a typed gradient shows its colours below)
            self.edit.setCursorPosition(position)
            self.changed.emit(self.key, text.strip())
        else:
            self._show(text.strip(), valid=False)

    def _apply(self, value: str) -> None:
        self.set_value(value)
        self.changed.emit(self.key, value)

    def reset(self) -> None:
        if self.base_value:
            self._apply(self.base_value)

    # ------------------ picking ------------------
    def _pick(self) -> None:
        if self.is_gradient():
            self._make_stop_picker(0)()
            return
        open_picker(self.btn_swatch, self.label.text(), parse_color(self.value() or self.base_value), self._on_picked, self.reset)

    def _on_picked(self, color: QColor) -> None:
        self._apply(color_text(color))

    def _make_stop_picker(self, index: int) -> Callable[[], None]:
        def picked(color: QColor) -> None:
            self.stops[index] = QColor(color)
            self._on_gradient_edited()

        def pick() -> None:
            title = f"{self.label.text()}: {self.STOP_NAMES[index]}"
            open_picker(self.stop_buttons[index], title, self.stops[index], picked)

        return pick

    def _on_gradient_toggled(self, on: bool) -> None:
        color = parse_color(self.value() or self.base_value)
        if on:
            self.stops = [color.lighter(118), color, color.darker(135)]
            self.chk_middle.setChecked(False)
            self._apply(self.gradient_value())
        else:
            self._apply(color_text(color))  # (its middle colour)

    def _on_gradient_edited(self, *_args) -> None:
        if self.is_gradient():
            self._apply(self.gradient_value())

    def show_contrast(self, ratio: Optional[float]) -> None:
        if ratio is None:
            self.contrast.setText("")
            return
        self.contrast.setText(f"{ratio:.1f}:1")
        color = "#22c55e" if ratio >= 4.5 else "#f59e0b" if ratio >= 3.0 else "#ef4444"
        self.contrast.setStyleSheet(f"color: {color}; font-weight: bold;")
        self.contrast.setToolTip(
            f"Contrast with {CONTRAST_PAIRS[self.key]}: {ratio:.1f} to 1. "
            "4.5 or more is easy to read; under 3 is hard to read for many people."
        )


class _Slider(QWidget):
    """A slider with its value beside it; value() in the setting's own units (factor: per step)."""

    changed = Signal()

    def __init__(self, low: float, high: float, factor: float = 1.0, unit: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.factor, self.unit = factor, unit
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(round(low / factor), round(high / factor))
        self.slider.valueChanged.connect(self._on_moved)
        row.addWidget(self.slider, 1)
        self.label = QLabel()
        self.label.setFixedWidth(52)
        row.addWidget(self.label)

    def value(self) -> float:
        return self.slider.value() * self.factor

    def set_value(self, value: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(round(float(value) / self.factor))
        self.slider.blockSignals(False)
        self._label()

    def _label(self) -> None:
        value = self.value()
        self.label.setText(f"{value:g}{self.unit}")

    def _on_moved(self, _value: int) -> None:
        self._label()
        self.changed.emit()


class _TintRow(QWidget):
    """What a pattern or a tinted image is drawn in: the theme's texture ink, or a colour of its own."""

    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.color = QColor("#ffffff")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.chk_own = QCheckBox("Own colour", self)
        self.chk_own.setToolTip("Off: the theme's texture ink (Colours > Glass and background). On: the colour beside it")
        self.chk_own.toggled.connect(self._on_toggled)
        row.addWidget(self.chk_own)
        self.btn_swatch = QPushButton()
        self.btn_swatch.setIconSize(SWATCH)
        self.btn_swatch.setToolTip("Pick the tint colour (changes the tile as you drag)")
        self.btn_swatch.clicked.connect(self._pick)
        row.addWidget(self.btn_swatch)
        row.addStretch(1)
        self._show()

    def value(self) -> Optional[str]:
        return self.color.name(QColor.NameFormat.HexArgb) if self.chk_own.isChecked() else None

    def set_value(self, value: Optional[str], ink: QColor) -> None:
        """Show this tint (None: the theme's ink, which the swatch then starts from)."""
        self.color = QColor(value) if value else QColor(ink)
        self.chk_own.blockSignals(True)
        self.chk_own.setChecked(bool(value))
        self.chk_own.blockSignals(False)
        self._show()

    def follow_ink(self, ink: QColor) -> None:
        if not self.chk_own.isChecked():
            self.color = QColor(ink)
            self._show()

    def _show(self) -> None:
        self.btn_swatch.setIcon(QIcon(swatch_pixmap(self.color.name(QColor.NameFormat.HexArgb))))
        self.btn_swatch.setEnabled(self.chk_own.isChecked())

    def _on_toggled(self, _on: bool) -> None:
        self._show()
        self.changed.emit()

    def _pick(self) -> None:
        open_picker(self.btn_swatch, "Tint", self.color, self._on_picked, self._on_default)

    def _on_picked(self, color: QColor) -> None:
        self.color = QColor(color)
        self._show()
        self.changed.emit()

    def _on_default(self) -> None:
        self.chk_own.setChecked(False)  # (back to the theme's ink)


class BackgroundPanel(QWidget):
    """The theme's background: none, a built-in texture, a pattern or an image tile."""

    changed = Signal()
    KINDS = {"none": "None", "builtin": "Built-in texture", "pattern": "Pattern", "image": "Image tile"}
    # Which settings each pattern uses
    SHAPE_SETTINGS = {
        "dots": {"cell", "size", "opacity", "stagger"}, "grid": {"cell", "thickness", "opacity"},
        "lines": {"cell", "thickness", "opacity", "direction"}, "crosshatch": {"cell", "thickness", "opacity"},
        "checker": {"cell", "opacity"}, "hexagon": {"cell", "thickness", "opacity"},
        "waves": {"cell", "thickness", "amplitude", "opacity"}, "noise": {"cell", "contrast", "seed", "opacity"},
    }

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ink = QColor("#ffffff")
        self.window_color = QColor("#17181e")
        self._image_png = ""  # (base64, as the theme file keeps it)
        self._loading = False
        layout = QVBoxLayout(self)
        self.cmb_kind = QComboBox()
        for kind, label in self.KINDS.items():
            self.cmb_kind.addItem(label, kind)
        self.cmb_kind.setToolTip("What's behind windows, dialogs and the asset grid")
        self.cmb_kind.currentIndexChanged.connect(self._on_kind)
        layout.addWidget(self.cmb_kind)
        self.stack = QStackedWidget()
        self.stack.addWidget(QLabel("No background texture."))
        self.stack.addWidget(self._build_builtin())
        self.stack.addWidget(self._build_pattern())
        self.stack.addWidget(self._build_image())
        layout.addWidget(self.stack)
        self.tile_view = QLabel()
        self.tile_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tile_view.setMinimumHeight(TILE_PREVIEW_PX)
        self.tile_view.setToolTip("The tile repeated 3 x 3, twice its size (dashed lines: where copies meet)")
        layout.addWidget(self.tile_view)
        self.chk_edges = QCheckBox("Show where the tiles meet", self)
        self.chk_edges.setChecked(True)
        self.chk_edges.toggled.connect(self.refresh_tile)
        layout.addWidget(self.chk_edges)
        self.lbl_seam = QLabel("")
        self.lbl_seam.setObjectName("mutedLabel")
        layout.addWidget(self.lbl_seam)
        layout.addStretch(1)

    def _build_builtin(self) -> QWidget:
        from ui.backgrounds import TEXTURES

        page = QWidget()
        form = QFormLayout(page)
        self.cmb_texture = QComboBox()
        for texture, label in TEXTURES.items():
            if texture != "none":
                self.cmb_texture.addItem(label, texture)
        self.cmb_texture.setToolTip("One of the app's own textures")
        self.cmb_texture.currentIndexChanged.connect(self._emit)
        form.addRow("Texture", self.cmb_texture)
        return page

    def _build_pattern(self) -> QWidget:
        page = QWidget()
        self.pattern_form = QFormLayout(page)
        self.cmb_shape = QComboBox()
        for shape, label in pattern_tiles.SHAPES.items():
            self.cmb_shape.addItem(label, shape)
        self.cmb_shape.setToolTip("What the pattern is made of")
        self.cmb_shape.currentIndexChanged.connect(self._on_shape)
        self.pattern_form.addRow("Pattern", self.cmb_shape)
        ranges = pattern_tiles.PATTERN_RANGES
        self.pattern_controls: dict[str, QWidget] = {
            "cell": _Slider(*ranges["cell"], 1, " px"), "thickness": _Slider(*ranges["thickness"], 0.25, " px"),
            "size": _Slider(*ranges["size"], 0.01), "opacity": _Slider(*ranges["opacity"], 1, "%"),
            "amplitude": _Slider(*ranges["amplitude"], 0.05), "contrast": _Slider(*ranges["contrast"], 0.25),
        }
        tips = {
            "cell": "How far apart the shapes are", "thickness": "How thick the lines are",
            "size": "How big the dots are (part of the spacing)", "opacity": "How strongly it shows",
            "amplitude": "How tall the waves are", "contrast": "Higher: fewer, brighter specks",
        }
        names = {"cell": "Spacing", "thickness": "Thickness", "size": "Dot size", "opacity": "Strength",
                 "amplitude": "Wave height", "contrast": "Sparseness"}
        for key, control in self.pattern_controls.items():
            control.setToolTip(tips[key])
            control.changed.connect(self._emit)
            self.pattern_form.addRow(names[key], control)
        self.cmb_direction = QComboBox()
        for direction, label in pattern_tiles.DIRECTION_LABELS.items():
            self.cmb_direction.addItem(label, direction)
        self.cmb_direction.setToolTip("Which way the lines run (directions that repeat without a seam)")
        self.cmb_direction.currentIndexChanged.connect(self._emit)
        self.pattern_form.addRow("Direction", self.cmb_direction)
        self.pattern_controls["direction"] = self.cmb_direction
        self.chk_stagger = QCheckBox("Every other row shifted", page)
        self.chk_stagger.setToolTip("Dots in a honeycomb rather than in rows and columns")
        self.chk_stagger.toggled.connect(self._emit)
        self.pattern_form.addRow("", self.chk_stagger)
        self.pattern_controls["stagger"] = self.chk_stagger
        self.spin_seed = QSpinBox()
        self.spin_seed.setRange(*(int(v) for v in pattern_tiles.PATTERN_RANGES["seed"]))
        self.spin_seed.setToolTip("Another number: another scatter of specks")
        self.spin_seed.valueChanged.connect(self._emit)
        self.pattern_form.addRow("Variation", self.spin_seed)
        self.pattern_controls["seed"] = self.spin_seed
        self.pattern_tint = _TintRow(page)
        self.pattern_tint.changed.connect(self._emit)
        self.pattern_form.addRow("Tint", self.pattern_tint)
        return page

    def _build_image(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        row = QHBoxLayout()
        self.btn_image = QPushButton("Choose Image...")
        set_icon(self.btn_image, "import")
        self.btn_image.setToolTip("Use a picture of your own as the tile (kept in the theme file)")
        self.btn_image.clicked.connect(self.choose_image)
        row.addWidget(self.btn_image)
        self.lbl_image = QLabel("No image yet")
        self.lbl_image.setObjectName("mutedLabel")
        row.addWidget(self.lbl_image, 1)
        form.addRow(row)
        self.cmb_mode = QComboBox()
        for mode, label in pattern_tiles.IMAGE_MODES.items():
            self.cmb_mode.addItem(label, mode)
        self.cmb_mode.setToolTip("Tint: its light parts show in the tint colour (the theme's texture ink, or your own). Own colours: as it is")
        self.cmb_mode.currentIndexChanged.connect(self._on_mode)
        form.addRow("Colour", self.cmb_mode)
        self.image_tint = _TintRow(page)
        self.image_tint.changed.connect(self._emit)
        form.addRow("Tint", self.image_tint)
        self.image_form = form
        ranges = pattern_tiles.IMAGE_RANGES
        self.sld_image_opacity = _Slider(*ranges["opacity"], 1, "%")
        self.sld_image_opacity.setToolTip("How strongly it shows")
        self.sld_image_opacity.changed.connect(self._emit)
        form.addRow("Strength", self.sld_image_opacity)
        self.sld_image_scale = _Slider(*ranges["scale"], 5, "%")
        self.sld_image_scale.setToolTip("Its size as a tile")
        self.sld_image_scale.changed.connect(self._emit)
        form.addRow("Size", self.sld_image_scale)
        self.chk_invert = QCheckBox("Invert", page)
        self.chk_invert.setToolTip("Swap light and dark")
        self.chk_invert.toggled.connect(self._emit)
        form.addRow("", self.chk_invert)
        self.chk_seamless = QCheckBox("Make seamless", page)
        self.chk_seamless.setToolTip("Blend its edges so copies meet without a seam")
        self.chk_seamless.toggled.connect(self._emit)
        form.addRow("", self.chk_seamless)
        return page

    # ------------------ reading and setting ------------------
    def kind(self) -> str:
        return self.cmb_kind.currentData()

    def background(self) -> dict[str, Any]:
        kind = self.kind()
        if kind == "builtin":
            return {"kind": "builtin", "texture": self.cmb_texture.currentData()}
        if kind == "pattern":
            data: dict[str, Any] = {"kind": "pattern", **pattern_tiles.PATTERN_DEFAULTS, "shape": self.cmb_shape.currentData()}
            for key in ("cell", "thickness", "size", "opacity", "amplitude", "contrast"):
                data[key] = self.pattern_controls[key].value()
            data["cell"] = int(round(data["cell"]))
            data["direction"] = self.cmb_direction.currentData()
            data["stagger"] = self.chk_stagger.isChecked()
            data["seed"] = self.spin_seed.value()
            if self.pattern_tint.value():
                data["tint"] = self.pattern_tint.value()
            return data
        if kind == "image" and self._image_png:
            data = {
                "kind": "image", "png": self._image_png, "mode": self.cmb_mode.currentData(),
                "opacity": self.sld_image_opacity.value(), "scale": self.sld_image_scale.value(),
                "invert": self.chk_invert.isChecked(), "seamless": self.chk_seamless.isChecked(),
            }
            if self.image_tint.value() and data["mode"] == "tint":
                data["tint"] = self.image_tint.value()
            return data
        return {"kind": "none"}

    def set_background(self, background: dict[str, Any]) -> None:
        self._loading = True
        kind = background.get("kind", "none")
        pattern = {**pattern_tiles.PATTERN_DEFAULTS, **(background if kind == "pattern" else {})}
        image = {**pattern_tiles.IMAGE_DEFAULTS, **(background if kind == "image" else {})}
        self.cmb_texture.setCurrentIndex(max(0, self.cmb_texture.findData(background.get("texture", "grain"))))
        self.cmb_shape.setCurrentIndex(max(0, self.cmb_shape.findData(pattern["shape"])))
        for key in ("cell", "thickness", "size", "opacity", "amplitude", "contrast"):
            self.pattern_controls[key].set_value(pattern[key])
        self.cmb_direction.setCurrentIndex(max(0, self.cmb_direction.findData(pattern["direction"])))
        self.chk_stagger.setChecked(bool(pattern["stagger"]))
        self.spin_seed.setValue(int(pattern["seed"]))
        self._image_png = background.get("png", "") if kind == "image" else ""
        self.lbl_image.setText("From the theme file" if self._image_png else "No image yet")
        self.cmb_mode.setCurrentIndex(max(0, self.cmb_mode.findData(image["mode"])))
        self.sld_image_opacity.set_value(image["opacity"])
        self.sld_image_scale.set_value(image["scale"])
        self.chk_invert.setChecked(bool(image["invert"]))
        self.chk_seamless.setChecked(bool(image["seamless"]))
        self.pattern_tint.set_value(background.get("tint") if kind == "pattern" else None, self.ink)
        self.image_tint.set_value(background.get("tint") if kind == "image" else None, self.ink)
        self._on_mode()
        self.cmb_kind.setCurrentIndex(max(0, self.cmb_kind.findData(kind)))
        self.stack.setCurrentIndex(self.cmb_kind.currentIndex())
        self._on_shape()
        self._loading = False
        self.refresh_tile()

    def set_image(self, image, name: str) -> None:
        """Use this picture (a QImage) as the tile."""
        image = pattern_tiles.prepare_import(image)
        self._image_png = theme_pack.png_text(image)
        self.lbl_image.setText(f"{name} ({image.width()} x {image.height()})")
        self.cmb_kind.setCurrentIndex(self.cmb_kind.findData("image"))
        self._emit()

    def choose_image(self) -> None:
        from PySide6.QtGui import QImage

        path, _ = QFileDialog.getOpenFileName(self, "Choose an Image for the Tile", "", "Images (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff)")
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            QMessageBox.warning(self, "Theme Studio", "That image couldn't be read.")
            return
        self.set_image(image, Path(path).name)

    # ------------------ changes ------------------
    def _on_kind(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self._emit()

    def _on_mode(self, *_args) -> None:
        self.image_form.setRowVisible(self.image_tint, self.cmb_mode.currentData() == "tint")  # (own colours: no tint)
        self._emit()

    def _on_shape(self) -> None:
        used = self.SHAPE_SETTINGS.get(self.cmb_shape.currentData(), set())
        for key, control in self.pattern_controls.items():
            self.pattern_form.setRowVisible(control, key in used)
        self._emit()

    def _emit(self, *_args) -> None:
        if self._loading:
            return
        self.refresh_tile()
        self.changed.emit()

    def set_colors(self, ink: QColor, window: QColor) -> None:
        self.ink, self.window_color = ink, window
        self.pattern_tint.follow_ink(ink)
        self.image_tint.follow_ink(ink)
        self.refresh_tile()

    def refresh_tile(self, *_args) -> None:
        tile = theme_pack.render_background(self.background(), self.ink, 1)
        side = TILE_PREVIEW_PX
        canvas = QPixmap(side, side)
        canvas.fill(self.window_color)
        self.lbl_seam.setText("")
        if tile is not None and not tile.isNull():
            zoom = 2
            width, height = tile.width() * zoom, tile.height() * zoom
            painter = QPainter(canvas)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            left, top = (side - 3 * width) // 2, (side - 3 * height) // 2
            for row in range(3):
                for col in range(3):
                    painter.drawImage(left + col * width, top + row * height, tile.scaled(width, height))
            if self.chk_edges.isChecked():
                pen = QPen(QColor(255, 80, 80, 160), 1, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                for index in range(4):
                    painter.drawLine(left + index * width, 0, left + index * width, side)
                    painter.drawLine(0, top + index * height, side, top + index * height)
            painter.end()
            if self.kind() == "image":
                self.lbl_seam.setText(pattern_tiles.seam_words(pattern_tiles.seam_score(tile)))
            else:
                self.lbl_seam.setText(f"Tile: {tile.width()} x {tile.height()} px")
        self.tile_view.setPixmap(canvas)


class _PreviewRows(RowHighlightDelegate):
    """The app's row highlight, in the colours of the theme being made."""

    tokens: dict[str, str] = {}

    def color(self, token: str) -> QColor:
        return parse_color(self.tokens[token]) if token in self.tokens else super().color(token)


class PreviewPane(QFrame):
    """Samples of the app's widgets, styled with the theme being made."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("studioPreview")
        self.icon_tokens: dict[str, str] = {}  # (changed in place by apply: its icons follow it)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 12)
        outer.setSpacing(6)
        caption = QLabel("The main window (the gaps around it are the Window colour)")
        caption.setObjectName("hintLabel")
        outer.addWidget(caption)
        main = QFrame()
        main.setObjectName("studioMain")
        layout = QVBoxLayout(main)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        outer.addWidget(main, 1)
        header = QWidget()
        header.setObjectName("headerBar")
        top = QHBoxLayout(header)
        title = QLabel("Asset Manager")
        title.setObjectName("pageTitle")
        top.addWidget(title)
        search = QLineEdit()
        search.setPlaceholderText("Search assets...")
        top.addWidget(search, 1)
        btn_settings = QPushButton("Settings")
        set_icon(btn_settings, "settings", tokens=self.icon_tokens)
        btn_settings.setToolTip("(A sample button)")
        top.addWidget(btn_settings)
        btn_import = QPushButton("Add Folder")
        btn_import.setObjectName("primaryBtn")
        set_icon(btn_import, "new_folder", "light", tokens=self.icon_tokens)
        btn_import.setToolTip("(A sample main button)")
        top.addWidget(btn_import)
        layout.addWidget(header)

        body = QSplitter()
        side = QWidget()
        side.setObjectName("studioSidebar")
        side_layout = QVBoxLayout(side)
        side_title = QLabel("LIBRARY")
        side_title.setObjectName("sidebarTitle")
        side_layout.addWidget(side_title)
        tree = QTreeWidget()
        tree.setObjectName("sidebarTree")
        tree.setHeaderHidden(True)
        self.rows = _PreviewRows(tree)
        tree.setItemDelegate(self.rows)
        for group, children in (("All Assets", []), ("3D Models", ["Characters", "Props"]), ("Textures", ["Rock", "Wood"]), ("Documents", [])):
            item = QTreeWidgetItem(tree, [group])
            for child in children:
                QTreeWidgetItem(item, [child])
            item.setExpanded(True)
        tree.setCurrentItem(tree.topLevelItem(1).child(0))
        side_layout.addWidget(tree)
        body.addWidget(side)

        self.grid = QListWidget()
        self.grid.setObjectName("assetGrid")
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setIconSize(QSize(96, 96))
        self.grid.setGridSize(QSize(124, 140))
        self.grid.setSpacing(6)
        body.addWidget(self.grid)

        inspector = QWidget()
        inspector.setObjectName("infoPanel")
        info = QVBoxLayout(inspector)
        heading = QLabel("DETAILS")
        heading.setObjectName("inspectorHeader")
        heading.setToolTip("An inspector heading: Accent text")
        info.addWidget(heading)
        for key, value in (("Name", "Hero_Character.fbx"), ("Size", "12.4 MB"), ("Modified", "2 days ago")):
            k = QLabel(key)
            k.setObjectName("inspectorKey")
            v = QLabel(value)
            v.setObjectName("inspectorValue")
            info.addWidget(k)
            info.addWidget(v)
        muted = QLabel("Muted text: a detail")
        muted.setObjectName("mutedLabel")
        info.addWidget(muted)
        hint = QLabel("Faint hint text")
        hint.setObjectName("hintLabel")
        info.addWidget(hint)
        link = QLabel('<a href="#">A link: Accent text</a>')
        link.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        info.addWidget(link)
        info.addStretch(1)
        body.addWidget(inspector)
        body.setSizes([170, 460, 170])
        layout.addWidget(body, 1)

        caption = QLabel("A dialog, such as Settings or a viewer: it sits on the Window colour")
        caption.setObjectName("hintLabel")
        outer.addWidget(caption)
        controls = QFrame()
        controls.setObjectName("studioDialog")
        dialog = QVBoxLayout(controls)
        dialog.setContentsMargins(0, 0, 0, 9)
        dialog.addWidget(self._viewer_toolbar())
        dialog_title = QLabel("Settings")
        dialog_title.setObjectName("pageTitle")
        dialog.addWidget(dialog_title)
        section = QLabel("Appearance")
        section.setObjectName("sectionTitle")
        for label in (dialog_title, section):
            label.setContentsMargins(9, 0, 9, 0)
        dialog.addWidget(section)
        grid = QGridLayout()
        grid.setContentsMargins(9, 0, 9, 0)
        dialog.addLayout(grid)
        btn_normal = QPushButton("Button")
        btn_normal.setToolTip("(A sample button: hover and press it)")
        btn_off = QPushButton("Unavailable")
        btn_off.setEnabled(False)
        btn_off.setToolTip("(A sample unavailable button)")
        btn_on = QPushButton("Toggle (on)")
        btn_on.setCheckable(True)
        btn_on.setChecked(True)
        btn_on.setToolTip("(A sample toggle that's on)")
        grid.addWidget(btn_normal, 0, 0)
        grid.addWidget(btn_off, 0, 1)
        grid.addWidget(btn_on, 0, 2)
        tabs = QHBoxLayout()
        for index, name in enumerate(("Contents", "Pages", "Notes")):
            tab = QPushButton(name)
            tab.setObjectName("pdfTab")
            tab.setCheckable(True)
            tab.setChecked(index == 0)
            tab.setToolTip("(A sample tab)")
            tabs.addWidget(tab)
        grid.addLayout(tabs, 0, 3)
        check = QCheckBox("Check box", controls)
        check.setChecked(True)
        grid.addWidget(check, 1, 0)
        grid.addWidget(QRadioButton("Option", controls), 1, 1)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setValue(60)
        grid.addWidget(slider, 1, 2)
        combo = QComboBox()
        combo.addItems(["Drop-down", "Second", "Third"])
        grid.addWidget(combo, 1, 3)
        for column, (name, text) in enumerate((("warningLabel", "A warning"), ("noteLabel", "A note"), ("comingSoon", "Some information"))):
            label = QLabel(text)
            label.setObjectName(name)
            grid.addWidget(label, 2, column)
        spin = QSpinBox()
        spin.setValue(42)
        grid.addWidget(spin, 2, 3)
        outer.addWidget(controls)

    def _viewer_toolbar(self) -> QWidget:
        """A viewer's toolbar (Panels (second)), as the PDF viewer's."""
        bar = QWidget()
        bar.setObjectName("viewerToolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 4)
        title = QLabel("Style_Guide.pdf")
        title.setObjectName("viewerTitle")
        row.addWidget(title)
        badge = QLabel("PDF")
        badge.setObjectName("formatBadge")
        row.addWidget(badge)
        row.addStretch(1)
        for name, tip in (("zoom_out", "Zoom out"), ("zoom_in", "Zoom in"), ("fit_width", "Fit the width"), ("hand", "Move"), ("sticky_note", "A note")):
            button = QPushButton()
            set_icon(button, name, tokens=self.icon_tokens)
            button.setToolTip(f"(A sample viewer button: {tip})")
            row.addWidget(button)
        page = QLabel("Page 3 of 12")
        page.setObjectName("toolbarLabel")
        row.addWidget(page)
        zoom = QLabel("100%")
        zoom.setObjectName("speedValue")
        zoom.setToolTip("A highlighted value: Accent text")
        row.addWidget(zoom)
        return bar

    @staticmethod
    def shows(key: str) -> bool:
        return key not in GlassPreview.ONLY_HERE

    def apply(self, tokens: dict[str, str], tile: Optional[Path]) -> None:
        """Restyle with these tokens (and this background tile, or none)."""
        from core.scanner import AssetItem
        from core.thumbnail_manager import render_placeholder_image, set_thumbnail_card_style

        sheet = stylesheet_from(tokens)
        window, border = tokens["window_bg"], tokens["border"]
        sheet += (
            f"\n#studioPreview, #studioDialog {{ background: {window}; }}\n"
            f"#studioMain, #studioDialog {{ border: 1px solid {border}; border-radius: 6px; }}\n"
            f"#studioSidebar {{ background: {tokens['sidebar_bg']}; }}\n"
        )
        if tile is not None:
            sheet += f'#studioPreview, #studioDialog, #studioPreview #assetGrid {{ background-image: url("{tile.as_posix()}"); background-repeat: repeat-xy; }}\n'
        self.rows.tokens = tokens
        self.icon_tokens.clear()
        self.icon_tokens.update(tokens)
        self.setStyleSheet(sheet)
        palette = palette_from(tokens)
        for widget in [self, *self.findChildren(QWidget)]:
            widget.setPalette(palette)  # (after the stylesheet, which resets them; each one, as they don't all inherit it)
        set_thumbnail_card_style(
            QColor(tokens["thumb_card"]), QColor(tokens["thumb_border"]), QColor(tokens["thumb_badge"]), QColor(tokens["thumb_badge_text"]),
        )
        self.grid.clear()
        for name, ext, group, category in _PREVIEW_SAMPLES:
            asset = AssetItem(Path(name), name, ext, 0, 0.0, group, category)
            item = QListWidgetItem(QIcon(QPixmap.fromImage(render_placeholder_image(asset, 96))), name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            self.grid.addItem(item)
        self.grid.setCurrentRow(1)


def _restyle(page: QWidget, sheet: str, tokens: dict[str, str]) -> None:
    page.setStyleSheet(sheet)
    palette = palette_from(tokens)
    for widget in [page, *page.findChildren(QWidget)]:
        widget.setPalette(palette)  # (after the stylesheet, which resets them)
        widget.update()  # (icons drawn from the tokens)


class _BoardSample(QWidget):
    """An Artbook page: the board (Asset grid colour and texture), its active area and a few pictures."""

    AREA_INSET = (110, 24, 24, 24)  # (it runs under the sidebar, so the frosted blur shows)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("artbookBoard")
        self.tokens: dict[str, str] = {}

    def area(self) -> QRectF:
        left, top, right, bottom = self.AREA_INSET
        return QRectF(self.rect()).adjusted(left, top, -right, -bottom)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        option = QStyleOption()
        option.initFrom(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option, painter, self)  # (the stylesheet's background)
        if not self.tokens:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self.area()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(parse_color(self.tokens["board_area"]))
        painter.drawRoundedRect(area, 14, 14)
        # Pictures (their own colours, as photos are), two of them in a group
        unit = max(1.0, min(area.width() / 3.6, area.height() / 1.6))
        pictures = [
            (QRectF(area.left() + unit * 0.25, area.top() + unit * 0.2, unit * 1.3, unit * 0.9), ("#f59e0b", "#7c2d12")),
            (QRectF(area.left() + unit * 1.75, area.top() + unit * 0.2, unit * 0.8, unit * 1.1), ("#38bdf8", "#1e3a8a")),
            (QRectF(area.left() + unit * 0.45, area.top() + unit * 1.3, unit * 2.6, unit * 0.25), ("#a3e635", "#14532d")),
        ]
        group = pictures[0][0].united(pictures[1][0]).adjusted(-unit * 0.1, -unit * 0.1, unit * 0.1, unit * 0.1)
        accent = parse_color(self.tokens["accent"])
        fill, edge = QColor(accent), QColor(accent)
        fill.setAlphaF(GROUP_DEFAULT_ALPHA)
        edge.setAlphaF(0.35)
        painter.setPen(QPen(edge, 1.5))
        painter.setBrush(fill)
        radius = min(group.width(), group.height()) * 0.03
        painter.drawRoundedRect(group, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)
        for box, (top, bottom) in pictures:
            shade = QLinearGradient(box.topLeft(), box.bottomRight())
            shade.setColorAt(0.0, QColor(top))
            shade.setColorAt(1.0, QColor(bottom))
            painter.setBrush(shade)
            painter.drawRoundedRect(box, 4, 4)


class GlassPreview(QFrame):
    """The frosted sidebar over an Artbook page: Glass tint, Glass edge, Texture ink, the active area."""

    ONLY_HERE = {"glass_tint", "glass_edge", "board_area"}
    SHOWS = ONLY_HERE | {"texture_ink", "grid_bg", "window_bg", "accent", "text", "text_faint", "hover"}
    SIDEBAR_WIDTH = 190

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("studioGlassPage")
        self._grabbing = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 12)
        caption = QLabel(
            "An Artbook page with the frosted sidebar over it. The sidebar is the Glass tint over a blur of what's behind "
            "it, with the Glass edge down its right. The lighter panel is the Artbook active area; the board is the Asset "
            "grid colour, with the texture in Texture ink."
        )
        caption.setObjectName("hintLabel")
        caption.setWordWrap(True)
        outer.addWidget(caption)
        self.stage = QWidget()
        self.stage.installEventFilter(self)
        outer.addWidget(self.stage, 1)
        self.board = _BoardSample(self.stage)
        self.glass = GlassPanel(self.stage)
        side = QVBoxLayout(self.glass)
        side.setContentsMargins(0, 6, 0, 0)
        title = QLabel("LIBRARY")
        title.setObjectName("sidebarTitle")
        title.setContentsMargins(14, 2, 8, 2)
        side.addWidget(title)
        tree = QTreeWidget()
        tree.setObjectName("sidebarTree")
        tree.setHeaderHidden(True)
        self.rows = _PreviewRows(tree)
        tree.setItemDelegate(self.rows)
        for name in ("Moodboards", "Characters", "Environments", "Props", "Lighting"):
            QTreeWidgetItem(tree, [name])
        tree.setCurrentItem(tree.topLevelItem(1))
        side.addWidget(tree)
        hint = QLabel("How frosted it is: Settings > Appearance > Glass Frost")
        hint.setObjectName("hintLabel")
        outer.addWidget(hint)

    @classmethod
    def shows(cls, key: str) -> bool:
        return key in cls.SHOWS

    def eventFilter(self, obj, event) -> bool:
        if obj is self.stage and event.type() == event.Type.Resize and not self._grabbing:
            self.board.setGeometry(self.stage.rect())
            self.glass.setGeometry(0, 0, self.SIDEBAR_WIDTH, self.stage.height())
            self.refresh_glass()
        return False

    def refresh_glass(self) -> None:
        """Give the glass what's behind it (the board under it), as ui.sidebar does."""
        size = self.glass.size()
        if size.isEmpty() or self._grabbing:
            return
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        self._grabbing = True  # (rendering sends the stage resize events again)
        try:
            painter = QPainter(image)
            self.board.render(painter, QPoint(0, 0), QRegion(QRect(QPoint(0, 0), size)))
            painter.end()
        finally:
            self._grabbing = False
        self.glass.set_backdrop(image)

    def apply(self, tokens: dict[str, str], tile: Optional[Path]) -> None:
        sheet = stylesheet_from(tokens, tile)  # (with the texture, behind the board as in the app)
        sheet += f"\n#studioGlassPage {{ background: {tokens['window_bg']}; }}\n"
        self.board.tokens = tokens
        self.glass.tokens = tokens
        self.rows.tokens = tokens
        _restyle(self, sheet, tokens)
        self.refresh_glass()


class IconsPreview(QScrollArea):
    """Every icon of the app, in the theme being made: each tone, at both sizes, on panels and buttons."""

    SAMPLES = ("check", "close", "warning", "star", "folder", "tag", "eye", "settings", "copy", "undo")
    PER_ROW = 14
    SHOWS = {token for pair in (*TONES.values(), DISABLED) for token in pair if not token.startswith("#")} | {
        "surface", "sidebar_bg", "border", "button", "button_border", "button_hover", "button_disabled", "accent_border",
        "accent_soft", "accent_hover", "card_selected_text", "text_strong",
    }

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.tokens: dict[str, str] = {}  # (changed in place by apply: every icon here follows it)
        page = QFrame()
        page.setObjectName("studioIcons")
        column = QVBoxLayout(page)
        column.setContentsMargins(16, 12, 16, 16)
        column.setSpacing(8)
        self._section(column, "Every icon (Accent: the details in Accent text, on a body in Accent)")
        column.addLayout(self._grid(list(ICONS), "accent", 24, self.PER_ROW))
        self._section(column, "At 16 px, as on most buttons")
        column.addLayout(self._grid(list(ICONS), "accent", 16, self.PER_ROW * 2))
        self._section(column, "Other tones")
        tones = [("Text (Text, Muted text)", "text"), ("Done (a fixed green)", "success"), ("Danger (Warning text, Danger)", "danger"),
                 ("Warning (Note text, amber)", "warning"), ("Unavailable (Faint text)", DISABLED)]
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        for row, (label, tone) in enumerate(tones):
            name = QLabel(label)
            name.setObjectName("mutedLabel")
            grid.addWidget(name, row, 0)
            for at, icon in enumerate(self.SAMPLES, start=1):
                grid.addWidget(self._glyph(icon, tone, 22), row, at)
        grid.setColumnStretch(len(self.SAMPLES) + 1, 1)
        column.addLayout(grid)
        self._section(column, "On the sidebar")
        side = QFrame()
        side.setObjectName("studioIconsSide")
        side_row = QHBoxLayout(side)
        for icon in self.SAMPLES:
            side_row.addWidget(self._glyph(icon, "accent", 22))
        side_row.addStretch(1)
        column.addWidget(side)
        self._section(column, "On buttons")
        buttons = QHBoxLayout()
        for text, icon, tone, kind in (
            ("Button", "settings", "accent", ""), ("Main button", "new_folder", "light", "primaryBtn"),
            ("Toggle (on)", "eye", "accent", "on"), ("Unavailable", "copy", "accent", "off"),
        ):
            button = QPushButton(text)
            set_icon(button, icon, tone, tokens=self.tokens)
            button.setToolTip(f"(A sample: {text.lower()}, with an icon)")
            if kind == "primaryBtn":
                button.setObjectName(kind)
            button.setCheckable(kind == "on")
            button.setChecked(kind == "on")
            button.setEnabled(kind != "off")
            buttons.addWidget(button)
        buttons.addStretch(1)
        column.addLayout(buttons)
        column.addStretch(1)
        self.setWidget(page)

    @staticmethod
    def _section(column: QVBoxLayout, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        column.addWidget(label)

    def _glyph(self, name: str, tone, size: int) -> DuotoneGlyph:
        glyph = DuotoneGlyph(name, tone, size, tokens=self.tokens)
        glyph.setToolTip(name.replace("_", " "))
        return glyph

    def _grid(self, names: list[str], tone, size: int, per_row: int) -> QGridLayout:
        grid = QGridLayout()
        grid.setSpacing(10)
        for index, name in enumerate(names):
            grid.addWidget(self._glyph(name, tone, size), index // per_row, index % per_row)
        grid.setColumnStretch(per_row, 1)
        return grid

    @classmethod
    def shows(cls, key: str) -> bool:
        return key in cls.SHOWS

    def apply(self, tokens: dict[str, str], _tile: Optional[Path]) -> None:
        self.tokens.clear()
        self.tokens.update(tokens)
        sheet = stylesheet_from(tokens) + (
            f"\n#studioIcons {{ background: {tokens['surface']}; }}\n"
            f"#studioIconsSide {{ background: {tokens['sidebar_bg']}; border: 1px solid {tokens['border']}; border-radius: 6px; }}\n"
        )
        _restyle(self, sheet, tokens)


class ThemeStudio(QMainWindow):
    """The Theme Studio window (see the module docstring)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Theme Studio")
        self.resize(1500, 900)
        self.path: Optional[Path] = None
        self.dirty = False
        self.pack = theme_pack.from_theme("dark")
        self._tiles = Path(tempfile.mkdtemp(prefix="asset-manager-theme-studio-"))  # (removed on close)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self.refresh_preview)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.addWidget(self._build_toolbar())
        splitter = QSplitter()
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_colors(), "Colours")
        self.background = BackgroundPanel()
        self.background.changed.connect(self._on_background_changed)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.background)
        self.tabs.addTab(scroll, "Background")
        splitter.addWidget(self.tabs)
        self.preview = PreviewPane()
        self.glass_preview = GlassPreview()
        self.icons_preview = IconsPreview()
        self.previews = QTabWidget()
        self.previews.addTab(self.preview, "App")
        self.previews.addTab(self.glass_preview, "Glass and Artbook")
        self.previews.addTab(self.icons_preview, "Icons")
        for index, tip in enumerate((
            "The main window and a dialog", "The frosted sidebar over an Artbook page", "Every icon, in each tone",
        )):
            self.previews.setTabToolTip(index, tip)
        splitter.addWidget(self.previews)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([640, 860])
        outer.addWidget(splitter, 1)
        self.setCentralWidget(central)
        for shortcut_id, slot in (
            ("studio.new", self.new_theme), ("studio.open", self.open_theme),
            ("studio.save", self.save_theme), ("studio.save_as", self.save_theme_as),
        ):
            action = QAction(self)
            action.setShortcut(shortcuts.sequence(shortcut_id))
            action.triggered.connect(slot)
            self.addAction(action)
        self.load_pack(self.pack, None)

    # ------------------ building ------------------
    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Name"))
        self.edit_name = QLineEdit()
        self.edit_name.setMaxLength(theme_pack.MAX_NAME)
        self.edit_name.setToolTip("The theme's name, as Settings would list it")
        self.edit_name.textEdited.connect(self._on_name)
        row.addWidget(self.edit_name, 1)
        row.addWidget(QLabel("Start from"))
        self.cmb_base = QComboBox()
        for theme_id, theme in THEMES.items():
            self.cmb_base.addItem(theme["label"], theme_id)
        self.cmb_base.setToolTip("The built-in theme New starts from (and Reset goes back to)")
        row.addWidget(self.cmb_base)
        self.btn_new = QPushButton("New")
        self.btn_new.setToolTip("Start a new theme from the chosen built-in one (Ctrl+N)")
        self.btn_new.clicked.connect(self.new_theme)
        self.btn_open = QPushButton("Open...")
        set_icon(self.btn_open, "folder")
        self.btn_open.setToolTip("Open a theme file (Ctrl+O)")
        self.btn_open.clicked.connect(self.open_theme)
        self.btn_save = QPushButton("Save")
        set_icon(self.btn_save, "save")
        self.btn_save.setToolTip("Save the theme file (Ctrl+S)")
        self.btn_save.clicked.connect(self.save_theme)
        self.btn_save_as = QPushButton("Save As...")
        self.btn_save_as.setToolTip("Save the theme as a new file (Ctrl+Shift+S)")
        self.btn_save_as.clicked.connect(self.save_theme_as)
        self.btn_copy = QPushButton("Copy for the App")
        set_icon(self.btn_copy, "copy")
        self.btn_copy.setToolTip("Copy the theme's colours as an entry for the app's themes (ui/themes.py)")
        self.btn_copy.clicked.connect(self.copy_for_app)
        for button in (self.btn_new, self.btn_open, self.btn_save, self.btn_save_as, self.btn_copy):
            row.addWidget(button)
        return bar

    def _build_colors(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        helpers = QHBoxLayout()
        self.btn_shades = QPushButton("Accent Shades from the Accent")
        set_icon(self.btn_shades, "palette")
        self.btn_shades.setToolTip("Work out the accent's border, hover, soft and text colours from the accent colour")
        self.btn_shades.clicked.connect(self.accent_shades)
        helpers.addWidget(self.btn_shades)
        self.btn_reset_all = QPushButton("Reset All")
        set_icon(self.btn_reset_all, "refresh")
        self.btn_reset_all.setToolTip("Every colour back to the base theme's")
        self.btn_reset_all.clicked.connect(self.reset_all)
        helpers.addWidget(self.btn_reset_all)
        helpers.addStretch(1)
        column.addLayout(helpers)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        fields = QVBoxLayout(holder)
        self.fields: dict[str, ColorField] = {}
        grouped = {key for _group, rows in TOKEN_GROUPS for key, _n, _w in rows}
        others = [(key, key, "") for key in theme_pack.token_keys() if key not in grouped]
        for group, rows in TOKEN_GROUPS + ([("Other", others)] if others else []):
            title = QLabel(group)
            title.setObjectName("sectionTitle")
            fields.addWidget(title)
            for key, name, where in rows:
                field = ColorField(key, name, where)
                field.changed.connect(self._on_token)
                self.fields[key] = field
                fields.addWidget(field)
        fields.addStretch(1)
        scroll.setWidget(holder)
        column.addWidget(scroll, 1)
        return page

    # ------------------ the theme ------------------
    def tokens(self) -> dict[str, str]:
        return self.pack.full_tokens()

    def load_pack(self, pack: theme_pack.ThemePack, path: Optional[Path]) -> None:
        self.pack, self.path = pack, path
        base = THEMES.get(pack.base, THEMES["dark"])
        tokens = pack.full_tokens()
        for key, field in self.fields.items():
            field.set_value(tokens[key], base[key])
        self.edit_name.setText(pack.name)
        self.cmb_base.setCurrentIndex(max(0, self.cmb_base.findData(pack.base)))
        self.background.set_background(pack.background)
        self._set_dirty(False)
        self._after_colors()
        self.refresh_preview()

    def _on_token(self, key: str, value: str) -> None:
        self.pack.tokens[key] = value
        self._set_dirty(True)
        self._after_colors()
        self.show_preview_of(key)
        self._preview_timer.start()

    def show_preview_of(self, key: str) -> None:
        """Turn to a preview that shows this colour, unless the one open does."""
        if self.previews.currentWidget().shows(key):
            return
        for index in range(self.previews.count()):
            if self.previews.widget(index).shows(key):
                self.previews.setCurrentIndex(index)
                return

    def _on_name(self, text: str) -> None:
        self.pack.name = text.strip() or "Untitled"
        self._set_dirty(True)

    def _on_background_changed(self) -> None:
        self.pack.background = self.background.background()
        self._set_dirty(True)
        self._preview_timer.start()

    def _after_colors(self) -> None:
        tokens = self.tokens()
        for key, field in self.fields.items():
            if key in CONTRAST_PAIRS:
                field.show_contrast(contrast_ratio(theme_pack.first_color(tokens[key]), theme_pack.first_color(tokens[CONTRAST_PAIRS[key]])))
        self.background.set_colors(QColor(tokens["texture_ink"]), theme_pack.first_color(tokens["window_bg"]))

    def set_token(self, key: str, value: str) -> None:
        """Change one colour (as if it were typed)."""
        self.fields[key].set_value(value)
        self._on_token(key, value)

    def accent_shades(self) -> None:
        accent = theme_pack.first_color(self.tokens()["accent"])
        window = theme_pack.first_color(self.tokens()["window_bg"])
        dark_ui = window.lightnessF() < 0.5
        soft = QColor.fromHslF(accent.hslHueF(), min(1.0, accent.hslSaturationF()), 0.30 if dark_ui else 0.90)
        shades = {
            "accent_border": accent.lighter(125), "accent_hover": accent.darker(115), "accent_soft": soft,
            "accent_text": accent.lighter(150) if dark_ui else accent.darker(140),
        }
        for key, color in shades.items():
            self.set_token(key, color_text(color))

    def reset_all(self) -> None:
        for field in self.fields.values():
            field.reset()

    def _set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        name = self.path.name if self.path else "unsaved theme"
        self.setWindowTitle(f"Theme Studio - {self.pack.name} ({name}){' *' if dirty else ''}")

    def refresh_preview(self) -> None:
        tokens = self.tokens()
        tile = theme_pack.tile_file(self.pack.background, QColor(tokens["texture_ink"]), self._tiles)
        for index in range(self.previews.count()):
            self.previews.widget(index).apply(tokens, tile)

    # ------------------ files ------------------
    def _keep_changes(self) -> bool:
        """Ask to save unsaved changes first; False if the person cancels."""
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Theme Studio", f'Save the changes to "{self.pack.name}" first?',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_theme()
        return answer == QMessageBox.StandardButton.Discard

    def new_theme(self) -> None:
        if self._keep_changes():
            self.load_pack(theme_pack.from_theme(self.cmb_base.currentData()), None)

    def open_theme(self) -> None:
        if not self._keep_changes():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open a Theme", "", f"Themes (*{theme_pack.SUFFIX})")
        if path:
            self.open_path(Path(path))

    def open_path(self, path: Path) -> bool:
        try:
            pack, notes = theme_pack.load(path)
        except theme_pack.ThemePackError as error:
            QMessageBox.warning(self, "Theme Studio", f"{path.name} couldn't be opened. {error}")
            return False
        self.load_pack(pack, path)
        if notes:
            QMessageBox.information(self, "Theme Studio", "\n\n".join(notes))
        return True

    def save_theme(self) -> bool:
        if self.path is None:
            return self.save_theme_as()
        return self.save_to(self.path)

    def save_theme_as(self) -> bool:
        suggested = theme_pack.theme_id_for(self.pack.name) + theme_pack.SUFFIX
        path, _ = QFileDialog.getSaveFileName(self, "Save the Theme", suggested, f"Themes (*{theme_pack.SUFFIX})")
        if not path:
            return False
        path = Path(path)
        if path.suffix.lower() != theme_pack.SUFFIX:
            path = path.with_name(path.name + theme_pack.SUFFIX)
        return self.save_to(path)

    def save_to(self, path: Path) -> bool:
        try:
            theme_pack.save(self.pack, path)
        except OSError as error:
            QMessageBox.warning(self, "Theme Studio", f"The theme couldn't be saved ({error.strerror or error}).")
            return False
        self.path = path
        self._set_dirty(False)
        return True

    def copy_for_app(self) -> None:
        QApplication.clipboard().setText(theme_pack.python_snippet(self.pack, theme_pack.theme_id_for(self.pack.name)))
        self.statusBar().showMessage("Copied: the theme's entry for THEMES in ui/themes.py", 4000)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._keep_changes():
            event.ignore()
            return
        shutil.rmtree(self._tiles, ignore_errors=True)
        super().closeEvent(event)


def run(argv: list[str]) -> int:
    """Start the studio on its own (main.py --theme-studio [theme file])."""
    from core.settings import get_theme
    from ui import main_thread_gc, scroll_bar_watch
    from ui.animations import install_popup_animations
    from ui.themes import apply_theme
    from ui.views import install_view_effects

    app = QApplication.instance() or QApplication(argv)
    app.setStyle("Fusion")
    apply_theme(get_theme(), app)  # (the studio's own look: the app's theme; never changed here)
    install_popup_animations(app)
    install_view_effects(app)
    main_thread_gc.install(app)
    scroll_bar_watch.install(app)
    window = ThemeStudio()
    files = [arg for arg in argv[1:] if arg.lower().endswith(theme_pack.SUFFIX)]
    if files:
        window.open_path(Path(files[0]))
    window.show()
    return app.exec()
