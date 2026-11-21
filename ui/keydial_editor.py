"""KeyDial's editor: profiles on the left, the KD100 in the middle (each key showing what it does),
and what the picked key, the dial or the profile does on the right.

Every change is saved straight away (core.keydial.store) and used at once. Closing the window
leaves KeyDial running in the tray. While this window is in front, pressing a key on the KD100
picks it here instead of sending its shortcut (core.keydial.engine: own_window).
"""

from __future__ import annotations

import copy
import os
import sys
from typing import Any, Optional

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QScrollArea, QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate,
    QVBoxLayout, QWidget,
)

from core.keydial import browsers, document, presets, store
from core.keydial.actions import describe
from core.keydial.profiles import DEFAULT_PROFILE
from core.keydial.protocol import DIAL_BUTTON
from ui.controls import ghost_button, section_heading, segmented
from ui.duotone import IconLabel, duotone_icon
from ui.keydial_actions import KEY_KINDS, ActionEditor, DialModesEditor
from ui.keydial_device import KD100View
from ui.themes import theme_color

# Starter layouts -> the app Asset Manager knows them as (core.apps), for "found on this PC"
PRESET_APP_IDS = {"Maya": "maya", "ZBrush": "zbrush", "Substance 3D Painter": "painter", "Photoshop": "photoshop",
                  "Blender": "blender", "Marmoset Toolbag": "toolbag"}


def _font(pixels: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont()
    font.setPixelSize(int(pixels))
    font.setWeight(weight)
    return font


def found_apps() -> set[str]:
    """The art apps Asset Manager found on this PC (its Settings > Apps), by id."""
    try:
        from core.settings import get_found_apps

        return {app_id for app_id, paths in get_found_apps().items() if paths}
    except Exception:  # pylint: disable=broad-except
        return set()


class _ProfileDelegate(QStyledItemDelegate):
    """A profile: its name, the programs it's for, and "in front" on the one in use."""

    def __init__(self, editor: "KeyDialWindow"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self, option, index) -> QSize:
        return QSize(200, 50)

    def paint(self, p: QPainter, option, index) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        row = QRectF(option.rect).adjusted(4, 2, -4, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        name = index.data(Qt.ItemDataRole.DisplayRole)
        sub = index.data(Qt.ItemDataRole.UserRole + 1) or ""
        if selected:
            fill = theme_color("accent_soft")
            fill.setAlphaF(0.45)
            p.setPen(QPen(theme_color("accent_border"), 1))
            p.setBrush(fill)
            p.drawRoundedRect(row, 8, 8)
        elif hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme_color("hover"))
            p.drawRoundedRect(row, 8, 8)
        right = row.right() - 10
        if name == self.editor.service.front_profile:
            p.setFont(_font(11, QFont.Weight.DemiBold))
            label = "manual" if self.editor.service.mode() == "manual" else "in front"
            width = QFontMetricsF(p.font()).horizontalAdvance(label) + 14
            chip = QRectF(right - width, row.center().y() - 9, width, 18)
            fill = theme_color("accent")
            fill.setAlphaF(0.25)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(chip, 9, 9)
            p.setPen(theme_color("accent_text"))
            p.drawText(chip, int(Qt.AlignmentFlag.AlignCenter), label)
            right = chip.left() - 6
        p.setFont(_font(13, QFont.Weight.DemiBold if selected else QFont.Weight.Normal))
        p.setPen(theme_color("text_strong" if selected else "text"))
        p.drawText(QRectF(row.left() + 12, row.top() + 5, right - row.left() - 12, 20),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   QFontMetricsF(p.font()).elidedText(name, Qt.TextElideMode.ElideRight, right - row.left() - 12))
        p.setFont(_font(11))
        p.setPen(theme_color("text_muted"))
        p.drawText(QRectF(row.left() + 12, row.top() + 24, row.width() - 24, 18),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   QFontMetricsF(p.font()).elidedText(sub, Qt.TextElideMode.ElideRight, row.width() - 24))
        p.restore()


class StatusChip(QWidget):
    """A dot and the device's state: "KD100 connected" (green dot), "Waiting for the KD100"..."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._text = ""
        self._good = False
        self.setToolTip("The KD100's connection")

    def set_state(self, text: str, good: bool) -> None:
        self._text, self._good = text, good
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(int(QFontMetricsF(_font(12, QFont.Weight.DemiBold)).horizontalAdvance(self._text)) + 40, 26)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(0, 2, 0, -2)
        fill = theme_color("accent_soft" if self._good else "raised")
        fill.setAlphaF(0.6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(box, box.height() / 2, box.height() / 2)
        p.setBrush(QColor(62, 207, 110) if self._good else theme_color("text_muted"))
        p.drawEllipse(QRectF(box.left() + 10, box.center().y() - 4, 8, 8))
        p.setFont(_font(12, QFont.Weight.DemiBold))
        p.setPen(theme_color("accent_text" if self._good else "text"))
        p.drawText(box.adjusted(26, 0, -8, 0), int(Qt.AlignmentFlag.AlignVCenter), self._text)
        p.end()


class AddProgramDialog(QDialog):
    """A profile for another program: pick one with a window open now, or its .exe."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Add Profile for a Program")
        self.setMinimumWidth(420)
        column = QVBoxLayout(self)
        column.addWidget(QLabel("Program (its .exe name)", self))
        row = QHBoxLayout()
        self.program = QComboBox(self)
        self.program.setEditable(True)
        self.program.setToolTip("Programs with a window open now; or type an .exe name")
        from core.keydial.foreground import window_exes

        own = os.path.basename(sys.executable).lower()
        for exe in sorted(window_exes() - {own, "explorer.exe", "applicationframehost.exe", "textinputhost.exe"}):
            self.program.addItem(exe)
        self.program.setCurrentText("")
        row.addWidget(self.program, 1)
        browse = ghost_button("folder", "Pick the program's .exe", "Browse...", parent=self)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        column.addLayout(row)
        column.addWidget(QLabel("Profile name", self))
        self.name = QLineEdit(self)
        self.name.setPlaceholderText("Named after the program")
        column.addWidget(self.name)
        self.program.currentTextChanged.connect(self._suggest_name)
        hint = QLabel("Keys you don't set in it do what Default does.", self)
        hint.setObjectName("pageHint")
        column.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)
        self._named = False
        self.name.textEdited.connect(self._typed_name)

    def _typed_name(self, _text: str) -> None:
        self._named = True

    def _suggest_name(self, text: str) -> None:
        if not self._named:
            stem = os.path.splitext(os.path.basename(text.strip()))[0]
            self.name.setText(stem.replace("_", " ").replace("-", " ").title() if stem else "")

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Pick the Program", "", "Programs (*.exe)")
        if path:
            self.program.setCurrentText(os.path.basename(path).lower())

    def exe(self) -> str:
        return os.path.basename(self.program.currentText().strip()).lower()

    def profile_name(self) -> str:
        return self.name.text().strip() or os.path.splitext(self.exe())[0].title()


class KeyDialWindow(QMainWindow):
    def __init__(self, service, keydial_app=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.keydial_app = keydial_app
        self.allow_close = False
        self.setWindowTitle("KeyDial")
        self.setWindowIcon(duotone_icon("keydial"))
        self.setMinimumSize(1040, 700)
        self.doc, error = service.document()
        self.profile = DEFAULT_PROFILE
        self.selected: Optional[str] = None  # a button, or None: the profile itself
        self._loading = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self.save_now)
        self._build()
        self._load_error(error)
        service.status_changed.connect(self._on_status)
        service.paused_changed.connect(self._on_status)
        service.button_event.connect(self._on_button)
        service.front_changed.connect(self._on_front)
        service.state_changed.connect(self._on_engine_state)
        service.mode_changed.connect(self._refresh_mode)
        service.profiles_changed.connect(self._refresh_mode)
        self._refresh_profiles()
        self.select_profile(service.front_profile if document.find(self.doc, service.front_profile) else DEFAULT_PROFILE)
        self._on_status()
        self._refresh_mode()
        geometry = service.settings.get("editor_geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        else:
            self.resize(1320, 840)

    # -- building --------------------------------------------------------------------------------
    def _build(self) -> None:
        central = QWidget(self)
        central.setObjectName("kdRoot")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 10, 16, 12)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = IconLabel(text_name="dialogTitle", parent=central)
        title.set_icon("keydial")
        title.setText("KeyDial")
        title.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        header.addWidget(title)
        header.addSpacing(18)
        self.mode_track, self.mode_group = segmented(
            (("auto", "Auto", "Auto mode: the profile follows the app in front"),
             ("manual", "Manual", "Manual mode: the profile you pick here, in every app")),
            self.service.mode(), central)
        self.mode_group.buttonClicked.connect(self._on_mode_button)
        header.addWidget(self.mode_track)
        self.manual_combo = QComboBox(central)
        self.manual_combo.setToolTip("Manual mode's profile, used in every app")
        self.manual_combo.setMinimumWidth(190)
        self.manual_combo.activated.connect(self._on_manual_combo)
        header.addWidget(self.manual_combo)
        header.addStretch(1)
        self.status_chip = StatusChip(central)
        header.addWidget(self.status_chip)
        self.pause_button = ghost_button("pause", "Pause: the KD100's keys do nothing until you resume", "Pause",
                                         checkable=True, parent=central)
        self.pause_button.toggled.connect(self._on_pause)
        header.addWidget(self.pause_button)
        self.settings_button = ghost_button("settings", "Start with Windows, run as administrator, the pop-up", "Settings",
                                            parent=central)
        self.settings_button.clicked.connect(self._settings_menu)
        header.addWidget(self.settings_button)
        root.addLayout(header)

        self.banner = QFrame(central)
        self.banner.setObjectName("kdBanner")
        self.banner.setStyleSheet(
            f"#kdBanner {{ background: {theme_color('warn_bg').name()}; border: 1px solid {theme_color('warn_border').name()};"
            f" border-radius: 9px; }} #kdBanner QLabel {{ color: {theme_color('warn_text').name()}; background: transparent; }}")
        banner_row = QHBoxLayout(self.banner)
        banner_row.setContentsMargins(14, 8, 8, 8)
        self.banner_text = QLabel(self.banner)
        self.banner_text.setWordWrap(True)
        banner_row.addWidget(self.banner_text, 1)
        self.banner_button = ghost_button("check", "Hide this note", "Got It", parent=self.banner)
        self.banner_button.clicked.connect(self._hide_banner)
        banner_row.addWidget(self.banner_button)
        self.banner.setVisible(False)
        root.addWidget(self.banner)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        left = QFrame(central)
        left.setObjectName("settingCard")
        left.setFixedWidth(280)
        left_col = QVBoxLayout(left)
        left_col.setContentsMargins(10, 12, 10, 10)
        heading = QLabel("Profiles", left)
        heading.setObjectName("cardTitle")
        left_col.addWidget(heading)
        self.profiles_hint = QLabel("Switches by the app in front", left)
        self.profiles_hint.setObjectName("pageHint")
        left_col.addWidget(self.profiles_hint)
        self.profile_list = QListWidget(left)
        self.profile_list.setObjectName("kdProfiles")
        self.profile_list.setItemDelegate(_ProfileDelegate(self))
        self.profile_list.setFrameShape(QFrame.Shape.NoFrame)
        self.profile_list.setStyleSheet("QListWidget#kdProfiles { background: transparent; border: none; }")
        self.profile_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.profile_list.customContextMenuRequested.connect(self._profile_menu)
        self.profile_list.currentItemChanged.connect(self._on_profile_row)
        self.profile_list.setMouseTracking(True)
        left_col.addWidget(self.profile_list, 1)
        self.add_button = ghost_button("add", "Add a profile: a starter layout for an art app, or one for any program",
                                       "Add Profile for...", parent=left)
        self.add_button.clicked.connect(self._add_menu)
        left_col.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignLeft)
        body.addWidget(left)

        middle = QVBoxLayout()
        middle.setSpacing(6)
        self.device = KD100View(central)
        self.device.picked.connect(self.select)
        middle.addWidget(self.device, 1)
        self.modes_label = QLabel(central)
        self.modes_label.setObjectName("pageHint")
        self.modes_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.modes_label.setTextFormat(Qt.TextFormat.RichText)
        middle.addWidget(self.modes_label)
        tip = QLabel("Tip: press a key on your KD100 to pick it here. Click empty space for the profile's settings", central)
        tip.setObjectName("pageHint")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        middle.addWidget(tip)
        body.addLayout(middle, 1)

        right = QFrame(central)
        right.setObjectName("settingCard")
        right.setFixedWidth(470)
        right_col = QVBoxLayout(right)
        right_col.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(right)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.panels = QStackedWidget()
        scroll.setWidget(self.panels)
        right_col.addWidget(scroll)
        body.addWidget(right)
        self.profile_panel = self._build_profile_panel()
        self.key_panel = self._build_key_panel()
        self.dial_panel = self._build_dial_panel()
        for panel in (self.profile_panel, self.key_panel, self.dial_panel):
            self.panels.addWidget(panel)

        self.saved_label = QLabel(central)
        self.saved_label.setObjectName("pageHint")
        root.addWidget(self.saved_label)
        self.device.installEventFilter(self)

    def _panel(self) -> tuple[QWidget, QVBoxLayout]:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(20, 16, 20, 16)
        column.setSpacing(8)
        return panel, column

    def _title_block(self, column: QVBoxLayout, panel: QWidget) -> tuple[QLabel, QLabel, QLabel]:
        over = section_heading("", panel)
        title = QLabel(panel)
        title.setObjectName("pageTitle")
        sub = QLabel(panel)
        sub.setObjectName("pageHint")
        column.addWidget(over)
        column.addWidget(title)
        column.addWidget(sub)
        return over, title, sub

    def _build_key_panel(self) -> QWidget:
        panel, column = self._panel()
        self.key_over, self.key_title, self.key_sub = self._title_block(column, panel)
        column.addWidget(section_heading("Name (shown on the key here)", panel))
        self.name_edit = QLineEdit(panel)
        self.name_edit.setPlaceholderText("A short name, like Undo")
        self.name_edit.setToolTip("Shown on the key in this window (and nowhere else)")
        self.name_edit.textEdited.connect(self._on_key_edit)
        column.addWidget(self.name_edit)
        column.addWidget(section_heading("When pressed", panel))
        self.key_editor = ActionEditor(KEY_KINDS, parent=panel)
        self.key_editor.changed.connect(self._on_key_edit)
        column.addWidget(self.key_editor)
        self.same_box = QCheckBox("Same as the Default profile", panel)
        self.same_box.setToolTip("Do what this key does in Default (it follows Default when you change that)")
        self.same_box.toggled.connect(self._on_same)
        column.addSpacing(6)
        column.addWidget(self.same_box)
        column.addStretch(1)
        return panel

    def _build_dial_panel(self) -> QWidget:
        panel, column = self._panel()
        self.dial_over, self.dial_title, self.dial_sub = self._title_block(column, panel)
        self.dial_same = QCheckBox("Same as the Default profile", panel)
        self.dial_same.setToolTip("Use Default's dial modes (they follow Default when you change it)")
        self.dial_same.toggled.connect(self._on_dial_same)
        column.addWidget(self.dial_same)
        column.addWidget(section_heading("Modes (press the dial for the next one)", panel))
        self.modes_editor = DialModesEditor(panel)
        self.modes_editor.changed.connect(self._on_modes)
        column.addWidget(self.modes_editor)
        column.addWidget(section_heading("Press the dial", panel))
        self.press_editor = ActionEditor(KEY_KINDS, compact=True, parent=panel)
        self.press_editor.changed.connect(self._on_press_edit)
        column.addWidget(self.press_editor)
        column.addStretch(1)
        return panel

    def _build_profile_panel(self) -> QWidget:
        panel, column = self._panel()
        self.profile_over, self.profile_title, self.profile_sub = self._title_block(column, panel)
        column.addWidget(section_heading("Name", panel))
        self.profile_name = QLineEdit(panel)
        self.profile_name.setToolTip("The profile's name")
        self.profile_name.editingFinished.connect(self._on_rename)
        column.addWidget(self.profile_name)
        column.addWidget(section_heading("Used when this program is in front", panel))
        self.apps_edit = QLineEdit(panel)
        self.apps_edit.setPlaceholderText("maya.exe (several: commas between)")
        self.apps_edit.setToolTip("The programs' .exe names, with commas between")
        self.apps_edit.editingFinished.connect(self._on_apps)
        column.addWidget(self.apps_edit)
        self.browsers_box = QCheckBox("Also in every web browser", panel)
        self.browsers_box.setToolTip("Chrome, Edge, Firefox, Opera, Brave, Vivaldi... and any other browser Windows "
                                     "has installed")
        self.browsers_box.toggled.connect(self._on_browsers)
        column.addWidget(self.browsers_box)
        self.pick_program = ghost_button("add", "Add a program with a window open now, or pick its .exe", "Add a Program...",
                                         parent=panel)
        self.pick_program.clicked.connect(self._pick_program)
        column.addWidget(self.pick_program, 0, Qt.AlignmentFlag.AlignLeft)
        self.inherit_heading = section_heading("Keys not set here", panel)
        column.addWidget(self.inherit_heading)
        self.inherit_combo = QComboBox(panel)
        self.inherit_combo.addItem("Do what Default does", True)
        self.inherit_combo.addItem("Do nothing", False)
        self.inherit_combo.setToolTip("What the keys this profile doesn't set do")
        self.inherit_combo.activated.connect(self._on_inherit)
        column.addWidget(self.inherit_combo)
        column.addSpacing(10)
        row = QHBoxLayout()
        self.reset_button = ghost_button("refresh", "Put back this app's starter layout (its programs stay)",
                                         "Reset to Starter Layout", parent=panel)
        self.reset_button.clicked.connect(self._reset_profile)
        self.delete_button = ghost_button("close", "Delete this profile", "Delete Profile", parent=panel, tone="danger")
        self.delete_button.clicked.connect(self._delete_profile)
        row.addWidget(self.reset_button)
        row.addWidget(self.delete_button)
        row.addStretch(1)
        column.addLayout(row)
        about = QLabel("Pick a key or the dial on the KD100 picture to change what it does.", panel)
        about.setObjectName("pageHint")
        about.setWordWrap(True)
        column.addSpacing(10)
        column.addWidget(about)
        column.addStretch(1)
        return panel

    # -- showing ---------------------------------------------------------------------------------
    def show_and_raise(self) -> None:
        self._reload_if_changed()
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        self._check_huion()

    def _reload_if_changed(self) -> None:
        doc, error = self.service.document()
        if doc != self.doc and not self._save_timer.isActive():
            self.doc = doc
            if document.find(doc, self.profile) is None:
                self.profile = DEFAULT_PROFILE
            self._refresh_profiles()
            self.select(self.selected)
        self._load_error(error)

    def _load_error(self, error: Optional[str]) -> None:
        if error:
            self._show_banner(f"profiles.json couldn't be read ({error}). The starter profiles are shown; your first "
                              "change saves them, and the old file is kept beside it as profiles.json.broken.", "error")

    def _check_huion(self) -> None:
        if self.service.settings.get("huion_tip_seen") or self.banner.isVisible():
            return
        if self.service.huion_driver_running():
            self._show_banner("The Huion driver is running (for your tablet, perhaps). If a KD100 key does two things, set "
                              "each of the KD100's keys and its dial to None in the Huion driver: KeyDial does them now.",
                              "huion")

    def _show_banner(self, text: str, kind: str) -> None:
        self.banner_text.setText(text)
        self.banner.setProperty("kind", kind)
        self.banner.setVisible(True)

    def _hide_banner(self) -> None:
        if self.banner.property("kind") == "huion":
            self.service.set_setting("huion_tip_seen", True)
        self.banner.setVisible(False)

    def _refresh_profiles(self) -> None:
        self._loading = True
        try:
            self.profile_list.clear()
            for profile in self.doc.get("profiles", []):
                item = QListWidgetItem(profile["name"])
                apps = profile.get("apps") or []
                shown = ["all web browsers" if app == browsers.TOKEN else app for app in apps]
                sub = "Every other app" if profile["name"] == DEFAULT_PROFILE else (", ".join(shown) if shown else "No program yet")
                item.setData(Qt.ItemDataRole.UserRole + 1, sub)
                self.profile_list.addItem(item)
                if profile["name"] == self.profile:
                    self.profile_list.setCurrentItem(item)
        finally:
            self._loading = False

    def _on_profile_row(self, current: Optional[QListWidgetItem], _previous) -> None:
        if current is not None and not self._loading:
            self.select_profile(current.text())

    def select_profile(self, name: str) -> None:
        if document.find(self.doc, name) is None:
            name = DEFAULT_PROFILE
        self.profile = name
        for row in range(self.profile_list.count()):
            if self.profile_list.item(row).text() == name and self.profile_list.currentRow() != row:
                self._loading = True
                self.profile_list.setCurrentRow(row)
                self._loading = False
        self._refresh_device()
        self.select(self.selected)

    def _refresh_device(self) -> None:
        resolved = document.resolve(self.doc, self.profile)
        labels = {}
        inherits = resolved["parent"] is not None
        for button, spec in resolved["buttons"].items():
            name = resolved["labels"].get(button, "")
            does = describe(spec)
            sub = does if name and name.lower() != does.lower() else ""  # (not "Mute" twice)
            labels[button] = (name or does, sub, inherits and button not in resolved["own"])
        for n in range(18):  # (keys the profile leaves unset do nothing)
            labels.setdefault(f"K{n + 1}", ("", "Nothing", False))
        self.device.set_labels(labels)
        modes = resolved["dial"]
        if modes:
            index = self.service.mode_index(self.profile, len(modes))
            current = modes[index]
            self.device.set_dial_text(f"Dial: {current.get('name', '')}",
                                      f"right: {describe(current.get('cw'))} · left: {describe(current.get('ccw'))}")
            accent = theme_color("accent_text").name()
            parts = [f"<b style='color:{accent}'>{m.get('name', '')}</b>" if n == index else m.get("name", "")
                     for n, m in enumerate(modes)]
            self.modes_label.setText("Dial modes: " + " · ".join(parts))
        else:
            self.device.set_dial_text("Dial", "no modes yet")
            self.modes_label.setText("The dial has no modes in this profile: pick the dial to add some")

    @Slot(str)
    def select(self, button: Optional[str]) -> None:
        """Show what a key or the dial does (None: the profile's own settings)."""
        self.selected = button
        self.device.set_selected(button)
        self._loading = True
        try:
            if button == DIAL_BUTTON:
                self._show_dial()
            elif button:
                self._show_key(button)
            else:
                self._show_profile()
        finally:
            self._loading = False

    def _profile_word(self) -> str:
        return f"{self.profile} profile"

    def _show_key(self, button: str) -> None:
        resolved = document.resolve(self.doc, self.profile)
        spec = resolved["buttons"].get(button)
        label = resolved["labels"].get(button, "")
        self.key_over.setText(f"KEY {button[1:]}")
        self.key_title.setText(label or describe(spec))
        self.key_sub.setText(self._profile_word())
        self.name_edit.setText(label)
        self.key_editor.set_mode_names([m.get("name", "") for m in resolved["dial"]])
        self.key_editor.set_profile_names(document.profile_names(self.doc))
        self.key_editor.set_spec(spec)
        inherits = resolved["parent"] is not None
        self.same_box.setVisible(inherits)
        self.same_box.setText(f"Same as the {resolved['parent']} profile" if inherits else "")
        self.same_box.setChecked(inherits and button not in resolved["own"])
        self.panels.setCurrentWidget(self.key_panel)

    def _show_dial(self) -> None:
        resolved = document.resolve(self.doc, self.profile)
        self.dial_over.setText("DIAL")
        self.dial_title.setText("Dial Modes")
        self.dial_sub.setText(f"{self._profile_word()} · drag to reorder")
        inherits = resolved["parent"] is not None
        self.dial_same.setVisible(inherits)
        self.dial_same.setChecked(inherits and not resolved["own_dial"])
        self.modes_editor.set_modes(resolved["dial"])
        self.press_editor.set_mode_names([m.get("name", "") for m in resolved["dial"]])
        self.press_editor.set_profile_names(document.profile_names(self.doc))
        self.modes_editor.set_profile_names(document.profile_names(self.doc))
        self.press_editor.set_spec(resolved["buttons"].get(DIAL_BUTTON))
        self.panels.setCurrentWidget(self.dial_panel)

    def _show_profile(self) -> None:
        profile = document.find(self.doc, self.profile)
        is_default = self.profile == DEFAULT_PROFILE
        self.profile_over.setText("PROFILE")
        self.profile_title.setText(self.profile)
        self.profile_sub.setText("Used whenever the app in front has no profile of its own" if is_default
                                 else "Switched to when its program is in front")
        self.profile_name.setText(self.profile)
        self.profile_name.setEnabled(not is_default)
        apps = profile.get("apps") or []
        self.apps_edit.setText(", ".join(app for app in apps if app != browsers.TOKEN))
        self.apps_edit.setCursorPosition(0)
        self.browsers_box.setChecked(browsers.TOKEN in apps)
        for widget in (self.apps_edit, self.pick_program, self.inherit_heading, self.inherit_combo, self.delete_button,
                       self.browsers_box):
            widget.setVisible(not is_default)
        self.inherit_combo.setCurrentIndex(0 if profile.get("inherits") else 1)
        self.reset_button.setVisible(presets.preset_for(self.profile) is not None)
        self.panels.setCurrentWidget(self.profile_panel)

    # -- editing ---------------------------------------------------------------------------------
    def _changed(self, refresh_list: bool = False) -> None:
        if refresh_list:
            self._refresh_profiles()
        self._refresh_device()
        self._save_timer.start()

    @Slot()
    def save_now(self) -> None:
        self._save_timer.stop()
        problem = self.service.save(copy.deepcopy(self.doc))
        if problem:
            self.saved_label.setText(f"Not saved: {problem}")
        else:
            self.saved_label.setText("Saved. Changes work straight away.")

    def _on_key_edit(self, *_args) -> None:
        if self._loading or not self.selected or self.selected == DIAL_BUTTON:
            return
        label = self.name_edit.text().strip()
        spec = self.key_editor.spec()
        document.set_button(self.doc, self.profile, self.selected, spec, label)
        self.key_title.setText(label or describe(spec))
        if self.same_box.isChecked():
            self._loading = True
            self.same_box.setChecked(False)
            self._loading = False
        self._changed()

    def _on_same(self, same: bool) -> None:
        if self._loading or not self.selected:
            return
        if same:
            document.clear_button(self.doc, self.profile, self.selected)
        else:
            resolved = document.resolve(self.doc, self.profile)
            document.set_button(self.doc, self.profile, self.selected, resolved["buttons"].get(self.selected),
                                resolved["labels"].get(self.selected, ""))
        self._changed()
        self.select(self.selected)

    def _on_modes(self, modes: list) -> None:
        if self._loading:
            return
        document.set_dial(self.doc, self.profile, modes)
        if self.dial_same.isChecked():
            self._loading = True
            self.dial_same.setChecked(False)
            self._loading = False
        self.press_editor.set_mode_names([m.get("name", "") for m in modes])
        self._changed()

    def _on_dial_same(self, same: bool) -> None:
        if self._loading:
            return
        if same:
            document.clear_dial(self.doc, self.profile)
        else:
            document.set_dial(self.doc, self.profile, document.resolve(self.doc, self.profile)["dial"])
        self._changed()
        self.select(DIAL_BUTTON)

    def _on_press_edit(self, spec: Any) -> None:
        if self._loading:
            return
        document.set_button(self.doc, self.profile, DIAL_BUTTON, spec)
        self._changed()

    def _on_rename(self) -> None:
        if self._loading:
            return
        new = document.rename_profile(self.doc, self.profile, self.profile_name.text())
        if new != self.profile:
            old, self.profile = self.profile, new
            self.save_now()
            self.service.profile_renamed(old, new)
            self._changed(refresh_list=True)
            self.select(None)

    def _on_apps(self) -> None:
        if self._loading or self.profile == DEFAULT_PROFILE:
            return
        apps = self.apps_edit.text().split(",") + ([browsers.TOKEN] if self.browsers_box.isChecked() else [])
        document.set_apps(self.doc, self.profile, apps)
        self._changed(refresh_list=True)

    def _on_browsers(self, on: bool) -> None:
        if self._loading or self.profile == DEFAULT_PROFILE:
            return
        self._on_apps()

    def _pick_program(self) -> None:
        dialog = AddProgramDialog(self)
        if dialog.exec() and dialog.exe():
            apps = list((document.find(self.doc, self.profile) or {}).get("apps") or [])
            document.set_apps(self.doc, self.profile, apps + [dialog.exe()])
            self._changed(refresh_list=True)
            self.select(None)

    def _on_inherit(self, index: int) -> None:
        document.set_inherits(self.doc, self.profile, bool(self.inherit_combo.itemData(index)))
        self._changed()

    def _reset_profile(self) -> None:
        preset = presets.preset_for(self.profile)
        if preset is None:
            return
        answer = QMessageBox.question(self, "Reset to Starter Layout",
                                      f"Put back the starter layout for {self.profile}? Your changes to its keys and dial go.")
        if answer == QMessageBox.StandardButton.Yes:
            document.replace_profile(self.doc, self.profile, preset)
            self._changed(refresh_list=True)
            self.select(None)

    def _delete_profile(self) -> None:
        if self.profile == DEFAULT_PROFILE:
            return
        answer = QMessageBox.question(self, "Delete Profile", f"Delete the {self.profile} profile?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        document.remove_profile(self.doc, self.profile)  # (manual mode on it: Default from now on)
        self.profile = DEFAULT_PROFILE
        self._changed(refresh_list=True)
        self.select(None)

    def _add_menu(self) -> None:
        menu = QMenu(self)
        found = found_apps()
        have = set(document.profile_names(self.doc))
        menu.addSection("Starter layouts")
        for preset in presets.APP_PRESETS:
            name = preset["name"]
            app_id = PRESET_APP_IDS.get(name)
            note = " (found on this PC)" if app_id in found else ""
            action = menu.addAction(f"{name}{note}" + (" - already added" if name in have else ""))
            action.setData(name)
            action.setEnabled(name not in have)
        menu.addSeparator()
        other = menu.addAction("Other Program...")
        other.setData("")
        chosen = menu.exec(self.add_button.mapToGlobal(self.add_button.rect().topLeft()))
        if chosen is None:
            return
        if chosen.data():
            self.add_profile(presets.preset_for(chosen.data()))
            return
        dialog = AddProgramDialog(self)
        if dialog.exec() and dialog.exe():
            self.add_profile(presets.blank_profile(dialog.profile_name(), [dialog.exe()]))

    def add_profile(self, profile: dict) -> str:
        name = document.add_profile(self.doc, profile)
        self.profile = name
        self._changed(refresh_list=True)
        self.select_profile(name)
        self.select(None)
        return name

    def _profile_menu(self, pos) -> None:
        item = self.profile_list.itemAt(pos)
        if item is None:
            return
        self.select_profile(item.text())
        self.select(None)
        menu = QMenu(self)
        duplicate = menu.addAction("Duplicate")
        delete = menu.addAction("Delete")
        delete.setEnabled(item.text() != DEFAULT_PROFILE)
        chosen = menu.exec(self.profile_list.viewport().mapToGlobal(pos))
        if chosen is duplicate:
            copy_of = copy.deepcopy(document.find(self.doc, item.text()))
            copy_of["name"] = f"{item.text()} copy"
            copy_of.pop("apps", None)
            if item.text() == DEFAULT_PROFILE:
                copy_of["inherits"] = DEFAULT_PROFILE
            self.add_profile(copy_of)
        elif chosen is delete:
            self._delete_profile()

    # -- the header ------------------------------------------------------------------------------
    def _settings_menu(self) -> None:
        menu = QMenu(self)
        settings = self.service.settings
        start = menu.addAction("Start with Windows")
        start.setCheckable(True)
        start.setChecked(bool(settings.get("start_with_windows")))
        admin = menu.addAction("Run as Administrator")
        admin.setCheckable(True)
        admin.setChecked(bool(settings.get("run_as_admin")))
        admin.setToolTip("Needed only if an app you use runs as administrator")
        popup = menu.addAction("Show Pop-up")
        popup.setCheckable(True)
        popup.setChecked(bool(settings.get("show_popup", True)))
        top = menu.addAction("Pop-up at the Top of the Screen")
        top.setCheckable(True)
        top.setChecked(settings.get("popup_place") == "top")
        menu.addSeparator()
        folder = menu.addAction("Open the Profiles Folder")
        menu.setToolTipsVisible(True)
        chosen = menu.exec(self.settings_button.mapToGlobal(self.settings_button.rect().bottomLeft()))
        app = self.keydial_app
        if chosen is start:
            if app is not None:
                app.start_action.setChecked(start.isChecked())
            else:
                self.service.set_setting("start_with_windows", start.isChecked())
        elif chosen is admin:
            if app is not None:
                app.set_run_as_admin(admin.isChecked())
            else:
                self.service.set_setting("run_as_admin", admin.isChecked())
        elif chosen is popup:
            if app is not None:
                app.popup_action.setChecked(popup.isChecked())
            else:
                self.service.set_setting("show_popup", popup.isChecked())
        elif chosen is top:
            self.service.set_setting("popup_place", "top" if top.isChecked() else "bottom")
        elif chosen is folder and hasattr(os, "startfile"):
            store.DATA_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(store.DATA_DIR))

    @Slot()
    def _refresh_mode(self) -> None:
        manual = self.service.mode() == "manual"
        for button in self.mode_group.buttons():
            button.setChecked(button.property("value") == self.service.mode())
        self.manual_combo.clear()
        for name in document.profile_names(self.doc):
            self.manual_combo.addItem(name)
        self.manual_combo.setCurrentText(self.service.manual_profile())
        self.manual_combo.setEnabled(manual)
        self.profiles_hint.setText(f"Manual: {self.service.manual_profile()} in every app" if manual
                                   else "Switches by the app in front")
        self.profile_list.viewport().update()

    def _on_mode_button(self, button) -> None:
        self.service.set_mode(button.property("value"))

    def _on_manual_combo(self, index: int) -> None:
        self.service.set_manual_profile(self.manual_combo.itemText(index))

    @Slot(bool)
    def _on_pause(self, paused: bool) -> None:
        if paused != self.service.engine.paused:
            self.service.set_paused(paused)

    @Slot()
    def _on_status(self, *_args) -> None:
        self.status_chip.set_state(self.service.status_text(), self.service.connected() and not self.service.engine.paused)
        paused = self.service.engine.paused
        if self.pause_button.isChecked() != paused:
            self.pause_button.blockSignals(True)
            self.pause_button.setChecked(paused)
            self.pause_button.blockSignals(False)
        self.pause_button.setText("Resume" if paused else "Pause")

    @Slot(str, bool)
    def _on_button(self, button: str, down: bool) -> None:
        self.device.set_pressed(button, down)
        if down and self.isActiveWindow():
            self.select(button)

    @Slot(str)
    def _on_front(self, _profile: str) -> None:
        self.profile_list.viewport().update()

    @Slot(str, object, int, int, str)
    def _on_engine_state(self, profile: str, *_args) -> None:
        if profile == self.profile:
            self._refresh_device()

    # -- the window ------------------------------------------------------------------------------
    def eventFilter(self, watched, event) -> bool:
        if watched is self.device and event.type() == event.Type.MouseButtonPress:
            if self.device.button_at(event.position()) is None:
                self.select(None)
        return super().eventFilter(watched, event)

    def changeEvent(self, event) -> None:
        if event.type() == event.Type.ActivationChange and self.isActiveWindow():
            self._reload_if_changed()
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        if self._save_timer.isActive():
            self.save_now()
        try:
            self.service.set_setting("editor_geometry", bytes(self.saveGeometry().toBase64().data()).decode("ascii"))
        except OSError:
            pass
        if self.allow_close or self.keydial_app is None:
            event.accept()
            return
        event.ignore()
        self.hide()
        self.keydial_app.tell_still_running()
