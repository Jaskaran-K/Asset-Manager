"""UI themes: color tokens and the application-wide stylesheet built from them.

Each theme is a dict of tokens substituted into APP_QSS_TEMPLATE. apply_theme()
installs the stylesheet, a matching QPalette (for widgets the stylesheet doesn't
cover, like color pickers and message boxes) and the thumbnail card colors.

The 3D and texture viewports keep their dark canvas in every theme, as most DCC
tools do, so models and textures read the same regardless of UI theme.
"""

from __future__ import annotations

from string import Template

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from core.thumbnail_manager import set_thumbnail_card_style


THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "label": "Dark Mode",
        "window_bg": "#17181e",
        "surface": "#21232c",
        "surface_alt": "#1e2029",
        "sidebar_bg": "#191a21",
        "grid_bg": "#121318",
        "border": "#2b2e3b",
        "text": "#e0e0e0",
        "text_strong": "#ffffff",
        "text_muted": "#9ca3af",
        "text_faint": "#6b7280",
        "button": "#343845",
        "button_border": "#4a5063",
        "button_hover": "#434859",
        "button_border_hover": "#5c647b",
        "button_pressed": "#2a2e3a",
        "button_disabled": "#262833",
        "input_bg": "#15161b",
        "input_border": "#363a48",
        "hover": "#252834",
        "accent": "#2563eb",
        "accent_border": "#3b82f6",
        "accent_hover": "#1d4ed8",
        "accent_soft": "#1e3a8a",
        "accent_text": "#60a5fa",
        "card": "#1c1e26",
        "card_border": "#292c39",
        "card_hover": "#242733",
        "card_hover_border": "#4b5563",
        "card_selected": "#1e3a8a",
        "card_selected_text": "#ffffff",
        "scroll_track": "#15161b",
        "scroll_handle": "#374151",
        "scroll_handle_hover": "#4b5563",
        "danger": "#ef4444",
        "warn_text": "#fca5a5", "warn_bg": "#3b1d22", "warn_border": "#7f1d1d",
        "note_text": "#fcd34d", "note_bg": "#33290f", "note_border": "#78590f",
        "info_text": "#93c5fd", "info_bg": "#172554", "info_border": "#1e40af",
        "thumb_card": "#1a1c24", "thumb_border": "#2e3240",
        "thumb_badge": "#12141a", "thumb_badge_text": "#ffffff",
    },
    "light": {
        "label": "Light Mode",
        "window_bg": "#f3f4f6",
        "surface": "#ffffff",
        "surface_alt": "#f9fafb",
        "sidebar_bg": "#f9fafb",
        "grid_bg": "#eef0f3",
        "border": "#e5e7eb",
        "text": "#1f2937",
        "text_strong": "#111827",
        "text_muted": "#6b7280",
        "text_faint": "#9ca3af",
        "button": "#ffffff",
        "button_border": "#d1d5db",
        "button_hover": "#f3f4f6",
        "button_border_hover": "#9ca3af",
        "button_pressed": "#e5e7eb",
        "button_disabled": "#f3f4f6",
        "input_bg": "#ffffff",
        "input_border": "#d1d5db",
        "hover": "#e5e7eb",
        "accent": "#2563eb",
        "accent_border": "#3b82f6",
        "accent_hover": "#1d4ed8",
        "accent_soft": "#dbeafe",
        "accent_text": "#2563eb",
        "card": "#ffffff",
        "card_border": "#e5e7eb",
        "card_hover": "#f9fafb",
        "card_hover_border": "#9ca3af",
        "card_selected": "#dbeafe",
        "card_selected_text": "#1e3a8a",
        "scroll_track": "#eef0f3",
        "scroll_handle": "#c4c8cf",
        "scroll_handle_hover": "#9ca3af",
        "danger": "#dc2626",
        "warn_text": "#991b1b", "warn_bg": "#fef2f2", "warn_border": "#fecaca",
        "note_text": "#92400e", "note_bg": "#fffbeb", "note_border": "#fde68a",
        "info_text": "#1e40af", "info_bg": "#eff6ff", "info_border": "#bfdbfe",
        "thumb_card": "#f3f4f6", "thumb_border": "#e5e7eb",
        "thumb_badge": "#ffffff", "thumb_badge_text": "#1f2937",
    },
    "dark_purple": {
        "label": "Dark Purple",
        "window_bg": "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #241338, stop:0.55 #160d26, stop:1 #0c0716)",
        "surface": "rgba(36, 22, 60, 0.92)",
        "surface_alt": "rgba(30, 18, 50, 0.92)",
        "sidebar_bg": "rgba(20, 11, 36, 0.55)",
        "grid_bg": "rgba(8, 4, 16, 0.30)",
        "border": "#35244f",
        "text": "#ede9fe",
        "text_strong": "#ffffff",
        "text_muted": "#b4a5d6",
        "text_faint": "#7d6f9c",
        "button": "#2e1f4a",
        "button_border": "#4c3a72",
        "button_hover": "#3b2a5e",
        "button_border_hover": "#6d55a0",
        "button_pressed": "#24173b",
        "button_disabled": "#21162f",
        "input_bg": "#150c24",
        "input_border": "#3d2b5e",
        "hover": "rgba(139, 92, 246, 0.16)",
        "accent": "#8b5cf6",
        "accent_border": "#a78bfa",
        "accent_hover": "#7c3aed",
        "accent_soft": "#4c1d95",
        "accent_text": "#c4b5fd",
        "card": "rgba(42, 27, 70, 0.75)",
        "card_border": "#3b2a5c",
        "card_hover": "rgba(58, 38, 96, 0.85)",
        "card_hover_border": "#7c5cc4",
        "card_selected": "#4c1d95",
        "card_selected_text": "#ffffff",
        "scroll_track": "transparent",
        "scroll_handle": "#4c3a72",
        "scroll_handle_hover": "#6d55a0",
        "danger": "#f87171",
        "warn_text": "#fca5a5", "warn_bg": "#3b1626", "warn_border": "#7f1d3d",
        "note_text": "#fcd34d", "note_bg": "#33240f", "note_border": "#78590f",
        "info_text": "#ddd6fe", "info_bg": "#2e1065", "info_border": "#6d28d9",
        "thumb_card": "#1e1332", "thumb_border": "#3b2a5c",
        "thumb_badge": "#140b24", "thumb_badge_text": "#ffffff",
    },
}

DEFAULT_THEME = "dark"

APP_QSS_TEMPLATE = Template("""
QMainWindow, QDialog {
    background: $window_bg;
}

QWidget {
    color: $text;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
}

QToolTip {
    background-color: $surface;
    color: $text;
    border: 1px solid $border;
    padding: 4px;
}

/* Buttons */
QPushButton {
    background-color: $button;
    border: 1px solid $button_border;
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: 500;
    color: $text;
}
QPushButton:hover { background-color: $button_hover; border-color: $button_border_hover; }
QPushButton:pressed { background-color: $button_pressed; }
QPushButton:checked { background-color: $accent_soft; border-color: $accent; color: $card_selected_text; }
QPushButton:disabled { color: $text_faint; background-color: $button_disabled; border-color: $border; }

#primaryBtn { background-color: $accent; border: 1px solid $accent_border; color: #ffffff; font-weight: bold; }
#primaryBtn:hover { background-color: $accent_hover; }
#defaultActiveBtn { background-color: #d97706; border: 1px solid #f59e0b; color: #ffffff; font-weight: bold; }
#defaultActiveBtn:hover { background-color: #b45309; }
#viewBtn { background-color: #059669; border: 1px solid #10b981; color: #ffffff; font-weight: bold; }
#viewBtn:hover { background-color: #047857; }
#primaryBtn:disabled, #viewBtn:disabled {
    color: $text_faint; background-color: $button_disabled; border-color: $border;
}

/* Inputs */
QLineEdit, QComboBox {
    background-color: $input_bg;
    border: 1px solid $input_border;
    border-radius: 6px;
    padding: 6px 10px;
    color: $text;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid $accent_border; }
QComboBox QAbstractItemView {
    background-color: $input_bg;
    color: $text;
    border: 1px solid $border;
    selection-background-color: $accent;
    selection-color: #ffffff;
}
QPlainTextEdit {
    background-color: $input_bg;
    border: 1px solid $input_border;
    border-radius: 6px;
    padding: 6px;
    color: $text;
    font-family: Consolas, "Courier New", monospace;
}
QPlainTextEdit[invalid="true"] { border: 1px solid $danger; }

QMenu { background-color: $surface; border: 1px solid $border; padding: 4px; }
QMenu::item { padding: 6px 18px; border-radius: 4px; }
QMenu::item:selected { background-color: $accent; color: #ffffff; }
QMenu::separator { height: 1px; background: $border; margin: 4px 6px; }

QSplitter::handle { background-color: $border; }

/* Main window */
#controlBar { background: $surface; border-bottom: 1px solid $border; }

QTreeWidget {
    background: $sidebar_bg;
    border: none;
    border-right: 1px solid $border;
    padding: 8px 4px;
    show-decoration-selected: 0;
    outline: 0;
}
QTreeWidget::item { padding: 6px 8px; border-radius: 5px; margin: 1px 4px; }
QTreeWidget::item:hover { background: $hover; }
QTreeWidget::item:selected { background: $accent; color: #ffffff; }

QListWidget {
    background-color: $input_bg;
    border: 1px solid $input_border;
    border-radius: 6px;
    padding: 4px;
    outline: 0;
}
QListWidget::item { border-radius: 4px; padding: 5px 8px; }
QListWidget::item:selected { background-color: $accent; color: #ffffff; }

#assetGrid {
    background: $grid_bg;
    border: none;
    border-radius: 0px;
    padding: 12px;
}
#assetGrid::item {
    background: $card;
    border: 1px solid $card_border;
    border-radius: 8px;
    margin: 6px;
    padding: 8px;
    color: $text;
}
#assetGrid::item:hover { background: $card_hover; border-color: $card_hover_border; }
#assetGrid::item:selected { background: $card_selected; border: 1px solid $accent_border; color: $card_selected_text; }

#emptyState { color: $text_faint; font-size: 14px; padding: 40px; }
#welcomePanel { background: $grid_bg; }
#welcomeTitle { color: $text_strong; font-size: 22px; font-weight: bold; }
#welcomeText { color: $text_muted; font-size: 14px; }
#mutedLabel { color: $text_muted; }

QStatusBar { background: $surface; border-top: 1px solid $border; color: $text_muted; font-size: 12px; }
QStatusBar::item { border: none; }
QStatusBar QLabel { padding: 0px 8px; }

QScrollArea { background: transparent; border: none; }
#scrollContent { background: transparent; }

/* Scrollbars */
QScrollBar:vertical { background: $scroll_track; width: 10px; margin: 0; }
QScrollBar:horizontal { background: $scroll_track; height: 10px; margin: 0; }
QScrollBar::handle:vertical { background: $scroll_handle; min-height: 24px; border-radius: 5px; margin: 2px; }
QScrollBar::handle:horizontal { background: $scroll_handle; min-width: 24px; border-radius: 5px; margin: 2px; }
QScrollBar::handle:hover { background: $scroll_handle_hover; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* Settings dialog */
#settingsNav { background: $sidebar_bg; border: none; border-right: 1px solid $border; padding: 8px 4px; }
#categoryTree { background: $input_bg; border: 1px solid $input_border; border-radius: 6px; padding: 4px; }
#pageTitle { font-size: 18px; font-weight: bold; color: $text_strong; }
#sectionTitle { font-size: 14px; font-weight: bold; color: $text_strong; }
#pageHint { color: $text_muted; }
#warningLabel { color: $warn_text; background-color: $warn_bg; border: 1px solid $warn_border; border-radius: 6px; padding: 8px 10px; }
#noteLabel { color: $note_text; background-color: $note_bg; border: 1px solid $note_border; border-radius: 6px; padding: 8px 10px; }
#comingSoon { color: $info_text; background-color: $info_bg; border: 1px solid $info_border; border-radius: 6px; padding: 8px 10px; }
#settingsFooter { background: $surface; border-top: 1px solid $border; }
#footerWarning { color: $warn_text; }
#themeCard { padding: 10px 14px; text-align: left; }

/* Asset viewer */
#headerBar { background: $surface; border-bottom: 1px solid $border; }
#infoPanel { background: $sidebar_bg; border-left: 1px solid $border; }
#bottomBar { background: $surface; border-top: 1px solid $border; }
#viewerToolbar { background: $surface_alt; border-bottom: 1px solid $border; }
#viewerTitle { color: $text_strong; }
#formatBadge { background-color: $accent; color: #ffffff; border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 11px; }
#toolbarLabel { color: $text_muted; font-size: 12px; }
#hintLabel { color: $text_faint; font-size: 11px; }
#inspectorHeader { color: $accent_text; letter-spacing: 1px; }
#inspectorKey { color: $text_faint; }
#inspectorValue { color: $text; font-size: 12px; }
#inspectorLine { color: $border; }
/* Video area stays dark in every theme, like the 3D viewport */
#videoSurface, #videoMessage { background-color: #0b0c10; }
#videoMessageTitle { color: #f3f4f6; font-size: 15px; font-weight: bold; }
#videoMessageDetail { color: #9ca3af; font-size: 12px; }
#seekPreview { background-color: #0b0c10; border: 1px solid $accent; border-radius: 6px; }
#seekPreviewFrame { background-color: #000000; }
#seekPreviewTime { color: #f3f4f6; font-size: 11px; font-weight: bold; }
/* Video controls: hover fill is painted by ui.animations, so :hover stays flat here */
#mediaBtn, #mediaBtn:hover, #mediaBtn:pressed {
    background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 0px 6px;
}
#mediaBtn:checked { background-color: $accent_soft; border-color: $accent; color: $card_selected_text; }
#mediaBtn:disabled { background: transparent; border-color: transparent; color: $text_faint; }
#playBtn, #playBtn:hover, #playBtn:pressed {
    background-color: $accent; border: 1px solid $accent_border; border-radius: 19px; padding: 0px;
}
#playBtn:disabled { background-color: $button_disabled; border-color: $border; }
#speedPopup { background-color: $input_bg; border: 1px solid $border; border-radius: 10px; }  /* solid: it floats over video */
#speedValue { color: $accent_text; font-weight: bold; }
#speedPreset, #speedPreset:hover, #speedPreset:pressed {
    background: transparent; border: none; border-radius: 6px; padding: 5px 10px; text-align: left;
}
#speedPreset:checked { background-color: $accent; color: #ffffff; font-weight: bold; }
""")

_current_theme_id = DEFAULT_THEME


def theme_tokens(theme_id: str) -> dict[str, str]:
    return THEMES.get(theme_id, THEMES[DEFAULT_THEME])


def current_theme_id() -> str:
    return _current_theme_id


def build_stylesheet(theme_id: str) -> str:
    return APP_QSS_TEMPLATE.substitute(theme_tokens(theme_id))


def _solid(color: str, fallback: str) -> QColor:
    """QColor for a token, using fallback for gradients / rgba() values QColor can't parse."""
    c = QColor(color)
    return c if c.isValid() else QColor(fallback)


def apply_theme(theme_id: str, app: QApplication | None = None) -> None:
    """Install the theme's stylesheet, palette and thumbnail colors application-wide."""
    global _current_theme_id
    app = app or QApplication.instance()
    if theme_id not in THEMES:
        theme_id = DEFAULT_THEME
    _current_theme_id = theme_id
    t = THEMES[theme_id]

    base = _solid(t["input_bg"], "#15161b")
    window = _solid(t["window_bg"], t["thumb_card"])
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, QColor(t["text"]))
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, _solid(t["surface"], t["thumb_card"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(t["text"]))
    palette.setColor(QPalette.ColorRole.Button, _solid(t["button"], t["thumb_card"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(t["text"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, _solid(t["surface"], t["thumb_card"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(t["text"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(t["text_faint"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(t["accent"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link, QColor(t["accent_text"]))
    app.setPalette(palette)
    app.setStyleSheet(build_stylesheet(theme_id))

    set_thumbnail_card_style(
        QColor(t["thumb_card"]), QColor(t["thumb_border"]),
        QColor(t["thumb_badge"]), QColor(t["thumb_badge_text"]),
    )
