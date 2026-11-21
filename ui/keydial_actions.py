"""KeyDial's editors for what a key or a dial turn does.

ShortcutField   click it, then press a shortcut on the keyboard (or pick a media / F13+ key)
ActionEditor    the kind (Shortcut, Hold Keys, Type Text, Scroll, Open, Macro, Dial Mode, Nothing)
                and its settings; compact=True picks the kind from a list instead of buttons
MacroEditor     a macro's steps: record them from the keyboard or add them one at a time
DialModesEditor the dial's modes: a name, what turning right and left does

They all edit actions as profiles.json saves them (core.keydial.actions), and say so through
`changed`. Keys are read by their Windows key code (core.keydial.keys), so what's recorded is
what KeyDial sends.
"""

from __future__ import annotations

import copy
from typing import Any, Optional, Sequence

from PySide6.QtCore import QElapsedTimer, QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetricsF, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QSizePolicy, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from core.keydial.actions import describe, kind_of, scroll_parts, scroll_spec
from core.keydial.keys import MODIFIERS, SPECIAL_KEYS, combo_spec, key_text, modifier_for_vk, name_for_vk
from ui.controls import GhostButton, ghost_button
from ui.themes import theme_color

KIND_NAMES = {
    "keys": ("Shortcut", "Press a shortcut, like Ctrl+Z"),
    "hold": ("Hold Keys", "Hold keys down while the KD100 key is held, like Space to pan or Alt to orbit"),
    "text": ("Type Text", "Type some text"),
    "scroll": ("Scroll", "Turn the mouse wheel, with keys held if you like (Ctrl + wheel zooms in many apps)"),
    "launch": ("Open", "Open a program, a file, a folder or a web page"),
    "macro": ("Macro", "Several steps in a row"),
    "dial_mode": ("Dial Mode", "Switch the dial to its next mode, or to one by name"),
    "profile": ("Profile", "Switch profile: the next or previous one, or one by name (manual mode), or back to auto mode"),
    "delay": ("Wait", "Wait a moment (between a macro's steps)"),
    "none": ("Nothing", "Do nothing"),
}
KEY_KINDS = ("keys", "hold", "text", "scroll", "launch", "macro", "dial_mode", "profile", "none")
TURN_KINDS = ("keys", "scroll", "text", "macro", "profile", "none")
STEP_KINDS = ("keys", "text", "launch", "delay")


class ComboReader:
    """Turns key presses into a combo: modifiers held plus one key ("ctrl+shift+z"), or modifiers
    alone when they're let go with nothing else pressed ("ctrl", for Hold Keys)."""

    def __init__(self) -> None:
        self.held: list[str] = []
        self._peak: list[str] = []

    def reset(self) -> None:
        self.held, self._peak = [], []

    def press(self, event: QKeyEvent) -> Optional[str]:
        """The finished combo, if this press finishes one."""
        vk = event.nativeVirtualKey()
        modifier = modifier_for_vk(vk)
        if modifier:
            if modifier not in self.held:
                self.held.append(modifier)
                self._peak = list(self.held)
            return None
        name = name_for_vk(vk)
        if not name:
            return None
        mods = [m for m in MODIFIERS if m in self._mods_from(event)]
        self._peak = []
        return combo_spec(tuple(mods) + (name,))

    def release(self, event: QKeyEvent) -> Optional[str]:
        modifier = modifier_for_vk(event.nativeVirtualKey())
        if not modifier:
            return None
        if modifier in self.held:
            self.held.remove(modifier)
        if not self.held and self._peak:
            combo, self._peak = combo_spec(tuple(m for m in MODIFIERS if m in self._peak)), []
            return combo
        return None

    def _mods_from(self, event: QKeyEvent) -> set[str]:
        mods = set(self.held)
        flags = event.modifiers()
        for flag, name in ((Qt.KeyboardModifier.ControlModifier, "ctrl"), (Qt.KeyboardModifier.ShiftModifier, "shift"),
                           (Qt.KeyboardModifier.AltModifier, "alt"), (Qt.KeyboardModifier.MetaModifier, "win")):
            if flags & flag:
                mods.add(name)
        return mods


def _font(pixels: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont()
    font.setPixelSize(max(8, int(pixels)))
    font.setWeight(weight)
    return font


class ShortcutField(QFrame):
    """Shows a combo as key caps; click it and press a shortcut to change it. changed(spec): "ctrl+z" or ""."""

    changed = Signal(str)

    def __init__(self, hint: str = "Click here, then press the shortcut on your keyboard", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("kdShortcut")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(74)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip("Click, then press the shortcut. Keys your keyboard hasn't got (media keys, F13 and up) are under More Keys")
        self._combo = ""
        self._hint = hint
        self._reader = ComboReader()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 8, 0)
        row.addStretch(1)
        self.more_button = ghost_button("chevron_down", "Keys most keyboards haven't got: media keys, F13 to F24, the number pad",
                                        "More Keys", parent=self)
        self.more_button.clicked.connect(self._show_more)
        row.addWidget(self.more_button, 0, Qt.AlignmentFlag.AlignVCenter)

    def combo(self) -> str:
        return self._combo

    def set_combo(self, spec: str, announce: bool = False) -> None:
        self._combo = spec or ""
        self.update()
        if announce:
            self.changed.emit(self._combo)

    def sizeHint(self) -> QSize:
        return QSize(320, 74)

    # -- the keyboard ----------------------------------------------------------------------------
    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ShortcutOverride:  # (every key comes here, none is a shortcut)
            event.accept()
            return True
        if event.type() == QEvent.Type.KeyPress:  # (Tab too: it's a key a shortcut can use)
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return
        combo = self._reader.press(event)
        self.update()
        if combo:
            self._reader.reset()
            self.set_combo(combo, announce=True)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return
        combo = self._reader.release(event)
        self.update()
        if combo:
            self.set_combo(combo, announce=True)

    def focusInEvent(self, event) -> None:
        self._reader.reset()
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._reader.reset()
        self.update()
        super().focusOutEvent(event)

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def _show_more(self) -> None:
        menu = QMenu(self)
        groups = (("Media", SPECIAL_KEYS[:7]), ("F13 to F24", SPECIAL_KEYS[7:19]), ("Number Pad", SPECIAL_KEYS[19:34]),
                  ("Other", SPECIAL_KEYS[34:]))
        for title, names in groups:
            sub = menu.addMenu(title)
            for name in names:
                action = sub.addAction(key_text(name))
                action.setData(name)
        chosen = menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))
        if chosen is not None and chosen.data():
            self.set_combo(chosen.data(), announce=True)

    # -- painting --------------------------------------------------------------------------------
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        focused = self.hasFocus()
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(theme_color("accent" if focused else "input_border"), 1.2))
        p.setBrush(theme_color("input_bg"))
        p.drawRoundedRect(box, 9, 9)
        parts = list(self._reader.held) if focused and self._reader.held else (self._combo.split("+") if self._combo else [])
        x, y = 14.0, 10.0
        p.setFont(_font(15, QFont.Weight.Bold))
        metrics = QFontMetricsF(p.font())
        for n, part in enumerate(parts):
            if n:
                p.setPen(theme_color("text_muted"))
                p.drawText(QRectF(x, y, 16, 34), int(Qt.AlignmentFlag.AlignCenter), "+")
                x += 18
            text = key_text(part)
            w = max(metrics.horizontalAdvance(text) + 22, 38)
            cap = QRectF(x, y, w, 34)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme_color("border"))
            p.drawRoundedRect(cap.translated(0, 2), 6, 6)
            p.setPen(QPen(theme_color("border"), 1))
            p.setBrush(theme_color("raised"))
            p.drawRoundedRect(cap, 6, 6)
            p.setPen(theme_color("text_strong"))
            p.drawText(cap, int(Qt.AlignmentFlag.AlignCenter), text)
            x += w + 4
        p.setFont(_font(12))
        p.setPen(theme_color("accent_text" if focused else "text_muted"))
        only_modifiers = bool(self._combo) and all(part in MODIFIERS for part in self._combo.split("+"))
        if focused:
            hint = "Press the shortcut now..."
        elif only_modifiers:
            hint = "Held for as long as the KD100 key is: press another KD100 key with it"
        else:
            hint = self._hint if self._combo else "Nothing set. " + self._hint
        p.drawText(QRectF(14, 48, self.width() - 140, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), hint)
        p.end()


class _KindPicker(QWidget):
    """The kind of action, as buttons in two rows (or a list, compact)."""

    chosen = Signal(str)

    def __init__(self, kinds: Sequence[str], compact: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.kinds = tuple(kinds)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.combo: Optional[QComboBox] = None
        self.buttons: dict[str, GhostButton] = {}
        if compact:
            self.combo = QComboBox(self)
            for kind in self.kinds:
                self.combo.addItem(KIND_NAMES[kind][0], kind)
            self.combo.setToolTip("What it does")
            self.combo.activated.connect(self._on_combo)
            layout.addWidget(self.combo, 0, 0)
            return
        track = QWidget(self)
        track.setObjectName("segment")
        track.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        grid = QGridLayout(track)
        grid.setContentsMargins(3, 3, 3, 3)
        grid.setSpacing(2)
        per_row = 5
        for n, kind in enumerate(self.kinds):
            name, tip = KIND_NAMES[kind]
            button = GhostButton(track)
            button.setObjectName("seg")
            button.setText(name)
            button.setToolTip(tip)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(self._on_button)
            button.setProperty("kind", kind)
            grid.addWidget(button, n // per_row, n % per_row)
            self.buttons[kind] = button
        layout.addWidget(track, 0, 0)

    def set_kind(self, kind: str) -> None:
        if self.combo is not None:
            index = self.combo.findData(kind)
            self.combo.setCurrentIndex(max(0, index))
        elif kind in self.buttons:
            self.buttons[kind].setChecked(True)

    def _on_combo(self, index: int) -> None:
        self.chosen.emit(self.combo.itemData(index))

    def _on_button(self) -> None:
        self.chosen.emit(self.sender().property("kind"))


class ActionEditor(QWidget):
    """changed(spec): the action as profiles.json saves it (None: does nothing, or not finished yet)."""

    changed = Signal(object)

    def __init__(self, kinds: Sequence[str] = KEY_KINDS, compact: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._loading = False
        self._kind = "none"
        self._mode_names: list[str] = []
        self._profile_names: list[str] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.picker = _KindPicker(kinds, compact, self)
        self.picker.chosen.connect(self._on_kind)
        layout.addWidget(self.picker)
        self.pages = QStackedWidget(self)
        self.pages.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.pages)
        self._page: dict[str, QWidget] = {}
        for kind in kinds:
            self._page[kind] = getattr(self, f"_make_{kind}")()
            self.pages.addWidget(self._page[kind])

    # -- pages -----------------------------------------------------------------------------------
    def _column(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget(self.pages)
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        return page, column

    def _hint(self, text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setObjectName("pageHint")
        label.setWordWrap(True)
        return label

    def _make_keys(self) -> QWidget:
        page, column = self._column()
        self.keys_field = ShortcutField(parent=page)
        self.keys_field.changed.connect(self._emit)
        column.addWidget(self.keys_field)
        return page

    def _make_hold(self) -> QWidget:
        page, column = self._column()
        self.hold_field = ShortcutField("Click here, then press the keys to hold (Space, Alt...)", page)
        self.hold_field.changed.connect(self._emit)
        column.addWidget(self.hold_field)
        column.addWidget(self._hint("Held down for as long as you hold the KD100 key: Space to pan, Alt to orbit, Ctrl to mask.", page))
        return page

    def _make_text(self) -> QWidget:
        page, column = self._column()
        self.text_edit = QLineEdit(page)
        self.text_edit.setPlaceholderText("The text to type")
        self.text_edit.textEdited.connect(self._emit)
        column.addWidget(self.text_edit)
        return page

    def _make_scroll(self) -> QWidget:
        page, column = self._column()
        row = QHBoxLayout()
        self.scroll_direction = QComboBox(page)
        for value, text in (("up", "Up"), ("down", "Down"), ("left", "Left"), ("right", "Right")):
            self.scroll_direction.addItem(text, value)
        self.scroll_direction.setToolTip("Which way the wheel turns")
        self.scroll_direction.activated.connect(self._emit)
        self.scroll_steps = QSpinBox(page)
        self.scroll_steps.setRange(1, 20)
        self.scroll_steps.setSuffix(" steps")
        self.scroll_steps.setToolTip("How many wheel steps")
        self.scroll_steps.valueChanged.connect(self._emit)
        row.addWidget(self.scroll_direction, 1)
        row.addWidget(self.scroll_steps)
        column.addLayout(row)
        held = QHBoxLayout()
        held.addWidget(QLabel("While holding", page))
        self.scroll_mods: dict[str, QCheckBox] = {}
        for name in ("ctrl", "shift", "alt"):
            box = QCheckBox(key_text(name), page)
            box.setToolTip(f"Hold {key_text(name)} while the wheel turns")
            box.toggled.connect(self._emit)
            held.addWidget(box)
            self.scroll_mods[name] = box
        held.addStretch(1)
        column.addLayout(held)
        return page

    def _make_launch(self) -> QWidget:
        page, column = self._column()
        row = QHBoxLayout()
        self.launch_edit = QLineEdit(page)
        self.launch_edit.setPlaceholderText("A program, file, folder or web address")
        self.launch_edit.textEdited.connect(self._emit)
        row.addWidget(self.launch_edit, 1)
        browse = ghost_button("folder", "Pick a program or a file", "Browse...", parent=page)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        column.addLayout(row)
        return page

    def _make_macro(self) -> QWidget:
        page, column = self._column()
        self.macro = MacroEditor(page)
        self.macro.changed.connect(self._emit)
        column.addWidget(self.macro)
        return page

    def _make_dial_mode(self) -> QWidget:
        page, column = self._column()
        self.mode_combo = QComboBox(page)
        self.mode_combo.setToolTip("Which dial mode")
        self.mode_combo.activated.connect(self._emit)
        column.addWidget(self.mode_combo)
        return page

    def _make_profile(self) -> QWidget:
        page, column = self._column()
        self.profile_combo = QComboBox(page)
        self.profile_combo.setToolTip("Which profile (any of these turns manual mode on, but Auto Mode)")
        self.profile_combo.activated.connect(self._emit)
        column.addWidget(self.profile_combo)
        column.addWidget(self._hint("Manual mode keeps that profile in every app until you switch again; "
                                    "Auto Mode goes back to following the app in front.", page))
        return page

    def _make_delay(self) -> QWidget:
        page, column = self._column()
        self.delay_spin = QSpinBox(page)
        self.delay_spin.setRange(0, 10000)
        self.delay_spin.setSingleStep(10)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setToolTip("How long to wait (1000 ms is a second)")
        self.delay_spin.valueChanged.connect(self._emit)
        column.addWidget(self.delay_spin)
        return page

    def _make_none(self) -> QWidget:
        page, column = self._column()
        column.addWidget(self._hint("Does nothing.", page))
        return page

    # -- loading and reading ---------------------------------------------------------------------
    def set_mode_names(self, names: Sequence[str]) -> None:
        self._mode_names = list(names)

    def set_profile_names(self, names: Sequence[str]) -> None:
        self._profile_names = list(names)

    def set_spec(self, spec: Any) -> None:
        self._loading = True
        try:
            kind = kind_of(spec)
            if kind not in self._page:
                kind = "none" if "none" in self._page else next(iter(self._page))
                spec = None
            self._kind = kind
            self.picker.set_kind(kind)
            self._show_page(kind)
            self._fill(kind, spec)
        finally:
            self._loading = False

    def _fill(self, kind: str, spec: Any) -> None:
        if kind == "keys":
            self.keys_field.set_combo(spec if isinstance(spec, str) else (spec or {}).get("keys", ""))
        elif kind == "hold":
            self.hold_field.set_combo(spec["hold"] if isinstance(spec, dict) else "")
        elif kind == "text":
            self.text_edit.setText(spec["text"] if isinstance(spec, dict) else "")
        elif kind == "scroll":
            direction, steps, held = scroll_parts(spec) if isinstance(spec, dict) else ("up", 1, "")
            self.scroll_direction.setCurrentIndex(max(0, self.scroll_direction.findData(direction)))
            self.scroll_steps.setValue(max(1, steps))
            for name, box in self.scroll_mods.items():
                box.setChecked(name in held.split("+"))
        elif kind == "launch":
            self.launch_edit.setText(spec["launch"] if isinstance(spec, dict) else "")
        elif kind == "macro":
            self.macro.set_steps(spec["macro"] if isinstance(spec, dict) else [])
        elif kind == "dial_mode":
            self.mode_combo.clear()
            self.mode_combo.addItem("Next mode", "next")
            self.mode_combo.addItem("Previous mode", "prev")
            for name in self._mode_names:
                self.mode_combo.addItem(f"Mode: {name}", name)
            target = spec["dial_mode"] if isinstance(spec, dict) else "next"
            index = self.mode_combo.findData(target)
            if index < 0:
                self.mode_combo.addItem(f"Mode: {target}", target)
                index = self.mode_combo.count() - 1
            self.mode_combo.setCurrentIndex(index)
        elif kind == "profile":
            self.profile_combo.clear()
            self.profile_combo.addItem("Next profile", "next")
            self.profile_combo.addItem("Previous profile", "prev")
            self.profile_combo.addItem("Auto Mode (follow the app in front)", "auto")
            for name in self._profile_names:
                self.profile_combo.addItem(f"Profile: {name}", name)
            target = spec["profile"] if isinstance(spec, dict) else "next"
            index = self.profile_combo.findData(target)
            if index < 0:
                self.profile_combo.addItem(f"Profile: {target}", target)
                index = self.profile_combo.count() - 1
            self.profile_combo.setCurrentIndex(index)
        elif kind == "delay":
            self.delay_spin.setValue(int(spec["delay"]) if isinstance(spec, dict) else 50)

    def kind(self) -> str:
        return self._kind

    def _show_page(self, kind: str) -> None:
        """Only the page showing counts for the size (a stack is otherwise as tall as its tallest page)."""
        for name, page in self._page.items():
            policy = QSizePolicy.Policy.Preferred if name == kind else QSizePolicy.Policy.Ignored
            page.setSizePolicy(policy, policy)
        self.pages.setCurrentWidget(self._page[kind])
        self.pages.adjustSize()
        self.pages.updateGeometry()
        self.updateGeometry()

    def spec(self) -> Any:
        """The action as it stands (None if it does nothing or isn't finished: no shortcut pressed yet)."""
        kind = self._kind
        if kind == "keys":
            return self.keys_field.combo() or None
        if kind == "hold":
            return {"hold": self.hold_field.combo()} if self.hold_field.combo() else None
        if kind == "text":
            return {"text": self.text_edit.text()} if self.text_edit.text() else None
        if kind == "scroll":
            held = "+".join(name for name, box in self.scroll_mods.items() if box.isChecked())
            return scroll_spec(self.scroll_direction.currentData(), self.scroll_steps.value(), held)
        if kind == "launch":
            target = self.launch_edit.text().strip()
            return {"launch": target} if target else None
        if kind == "macro":
            steps = self.macro.steps()
            return {"macro": steps} if steps else None
        if kind == "dial_mode":
            return {"dial_mode": self.mode_combo.currentData() or "next"}
        if kind == "profile":
            return {"profile": self.profile_combo.currentData() or "next"}
        if kind == "delay":
            return {"delay": self.delay_spin.value()}
        return None

    def _on_kind(self, kind: str) -> None:
        if kind == self._kind:
            return
        self._kind = kind
        self._show_page(kind)
        self._loading = True
        try:
            self._fill(kind, None)
        finally:
            self._loading = False
        if kind in ("keys", "hold"):
            (self.keys_field if kind == "keys" else self.hold_field).setFocus(Qt.FocusReason.OtherFocusReason)
        self._emit()

    def _emit(self, *_args) -> None:
        if not self._loading:
            self.changed.emit(self.spec())

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open", "", "Programs (*.exe *.bat *.cmd *.lnk);;All files (*)")
        if path:
            self.launch_edit.setText(path.replace("/", "\\"))
            self._emit()


class MacroEditor(QWidget):
    """A macro's steps, in order. Record: keys pressed on the keyboard become steps, with the
    pauses between them as waits. changed(steps)."""

    changed = Signal(object)
    MIN_WAIT_MS = 120  # (shorter pauses while recording aren't kept: they're just typing speed)
    MAX_WAIT_MS = 3000

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._reader = ComboReader()
        self._clock = QElapsedTimer()
        self._loading = False
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.list = QListWidget(self)
        self.list.setObjectName("kdSteps")
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setMinimumHeight(150)
        self.list.setToolTip("The steps, in order. Drag to reorder; pick one to change it")
        self.list.model().rowsMoved.connect(self._emit)
        self.list.currentRowChanged.connect(self._show_step)
        column.addWidget(self.list)
        buttons = QHBoxLayout()
        self.record_button = ghost_button("play", "Record steps: press keys on your keyboard (the pauses between them "
                                          "become waits). Click again to stop", "Record", checkable=True, parent=self)
        self.record_button.toggled.connect(self._record)
        self.add_button = ghost_button("add", "Add a step: a shortcut, a wait, some text or something to open", "Add Step", parent=self)
        self.add_button.clicked.connect(self._add_menu)
        self.remove_button = ghost_button("close", "Remove the picked step", "Remove", parent=self)
        self.remove_button.clicked.connect(self._remove)
        for button in (self.record_button, self.add_button, self.remove_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        column.addLayout(buttons)
        self.step_editor = ActionEditor(STEP_KINDS, compact=True, parent=self)
        self.step_editor.changed.connect(self._step_changed)
        self.step_editor.setVisible(False)
        column.addWidget(self.step_editor)

    def set_steps(self, steps: Sequence[Any]) -> None:
        self._loading = True
        try:
            self.list.clear()
            for step in steps:
                self._append(step)
            self.step_editor.setVisible(False)
        finally:
            self._loading = False

    def steps(self) -> list:
        return [copy.deepcopy(self.list.item(n).data(Qt.ItemDataRole.UserRole)) for n in range(self.list.count())]

    def _append(self, step: Any) -> QListWidgetItem:
        item = QListWidgetItem(f"{self.list.count() + 1}.  {describe(step)}")
        item.setData(Qt.ItemDataRole.UserRole, step)
        self.list.addItem(item)
        return item

    def _renumber(self) -> None:
        for n in range(self.list.count()):
            item = self.list.item(n)
            item.setText(f"{n + 1}.  {describe(item.data(Qt.ItemDataRole.UserRole))}")

    def _emit(self, *_args) -> None:
        self._renumber()
        if not self._loading:
            self.changed.emit(self.steps())

    def _show_step(self, row: int) -> None:
        item = self.list.item(row) if row >= 0 else None
        self.step_editor.setVisible(item is not None and not self.is_recording())
        if item is not None:
            self.step_editor.set_spec(item.data(Qt.ItemDataRole.UserRole))

    def _step_changed(self, spec: Any) -> None:
        item = self.list.currentItem()
        if item is None or spec is None:
            return
        item.setData(Qt.ItemDataRole.UserRole, spec)
        self._emit()

    def _add_menu(self) -> None:
        menu = QMenu(self)
        for kind in STEP_KINDS:
            action = menu.addAction(KIND_NAMES[kind][0])
            action.setData(kind)
        chosen = menu.exec(self.add_button.mapToGlobal(self.add_button.rect().bottomLeft()))
        if chosen is None:
            return
        self.add_step(chosen.data())

    def add_step(self, kind: str) -> None:
        start = {"keys": "ctrl+z", "delay": {"delay": 50}, "text": {"text": "text"}, "launch": {"launch": "notepad.exe"}}[kind]
        item = self._append(start)
        self.list.setCurrentItem(item)
        self._emit()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)
            self._emit()

    # -- recording -------------------------------------------------------------------------------
    def _record(self, on: bool) -> None:
        self._reader.reset()
        self._recording = on
        if on:
            self.record_button.setText("Stop")
            self.step_editor.setVisible(False)
            self._clock.invalidate()
            self.setFocus(Qt.FocusReason.OtherFocusReason)
            self.grabKeyboard()
        else:
            self.record_button.setText("Record")
            self.releaseKeyboard()

    def is_recording(self) -> bool:
        return getattr(self, "_recording", False)  # (event() runs while the widget is still being built)

    def event(self, event) -> bool:
        if self.is_recording() and event.type() == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        if self.is_recording() and event.type() == QEvent.Type.KeyPress:
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self.is_recording():
            super().keyPressEvent(event)
            return
        if event.isAutoRepeat():
            return
        self._recorded(self._reader.press(event))

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if not self.is_recording():
            super().keyReleaseEvent(event)
            return
        if not event.isAutoRepeat():
            self._recorded(self._reader.release(event))

    def _recorded(self, combo: Optional[str]) -> None:
        if not combo:
            return
        if self._clock.isValid():
            waited = min(self._clock.elapsed(), self.MAX_WAIT_MS)
            if waited >= self.MIN_WAIT_MS:
                self._append({"delay": int(round(waited / 10.0) * 10)})
        self._clock.start()
        self._append(combo)
        self._emit()

    def hideEvent(self, event) -> None:
        if self.is_recording():
            self.record_button.setChecked(False)
        super().hideEvent(event)


class DialModesEditor(QWidget):
    """The dial's modes, in order: pick one to name it and set what turning right and left does.
    changed(modes): [{"name", "cw", "ccw"}, ...]."""

    changed = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._loading = False
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.list = QListWidget(self)
        self.list.setObjectName("kdModes")
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setMinimumHeight(120)
        self.list.setMaximumHeight(190)
        self.list.setToolTip("The dial's modes, in the order pressing the dial steps through them. Drag to reorder")
        self.list.model().rowsMoved.connect(self._emit)
        self.list.currentRowChanged.connect(self._show_mode)
        column.addWidget(self.list)
        buttons = QHBoxLayout()
        self.add_button = ghost_button("add", "Add a dial mode", "Add Mode", parent=self)
        self.add_button.clicked.connect(self.add_mode)
        self.remove_button = ghost_button("close", "Remove the picked mode", "Remove", parent=self)
        self.remove_button.clicked.connect(self._remove)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        column.addLayout(buttons)
        self.detail = QFrame(self)
        self.detail.setObjectName("settingCard")
        form = QGridLayout(self.detail)
        form.setContentsMargins(14, 12, 14, 12)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        self.name_edit = QLineEdit(self.detail)
        self.name_edit.setPlaceholderText("Mode name")
        self.name_edit.textEdited.connect(self._detail_changed)
        self.right_editor = ActionEditor(TURN_KINDS, compact=True, parent=self.detail)
        self.left_editor = ActionEditor(TURN_KINDS, compact=True, parent=self.detail)
        self.right_editor.changed.connect(self._detail_changed)
        self.left_editor.changed.connect(self._detail_changed)
        form.addWidget(QLabel("Name", self.detail), 0, 0)
        form.addWidget(self.name_edit, 0, 1)
        form.addWidget(QLabel("Turn right", self.detail), 1, 0, Qt.AlignmentFlag.AlignTop)
        form.addWidget(self.right_editor, 1, 1)
        form.addWidget(QLabel("Turn left", self.detail), 2, 0, Qt.AlignmentFlag.AlignTop)
        form.addWidget(self.left_editor, 2, 1)
        form.setColumnStretch(1, 1)
        self.detail.setVisible(False)
        column.addWidget(self.detail)

    def set_profile_names(self, names: Sequence[str]) -> None:
        self.right_editor.set_profile_names(names)
        self.left_editor.set_profile_names(names)

    def set_modes(self, modes: Sequence[dict]) -> None:
        self._loading = True
        try:
            row = max(0, self.list.currentRow())
            self.list.clear()
            for mode in modes:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, copy.deepcopy(mode))
                self.list.addItem(item)
            self._relabel()
            if self.list.count():
                self.list.setCurrentRow(min(row, self.list.count() - 1))
            self._show_mode(self.list.currentRow())
        finally:
            self._loading = False

    def modes(self) -> list[dict]:
        return [copy.deepcopy(self.list.item(n).data(Qt.ItemDataRole.UserRole)) for n in range(self.list.count())]

    def _relabel(self) -> None:
        for n in range(self.list.count()):
            item = self.list.item(n)
            mode = item.data(Qt.ItemDataRole.UserRole)
            item.setText(f"{mode.get('name', '')}      right: {describe(mode.get('cw'))}  ·  left: {describe(mode.get('ccw'))}")

    def _show_mode(self, row: int) -> None:
        item = self.list.item(row) if row >= 0 else None
        self.detail.setVisible(item is not None)
        if item is None:
            return
        mode = item.data(Qt.ItemDataRole.UserRole)
        loading, self._loading = self._loading, True
        try:
            self.name_edit.setText(mode.get("name", ""))
            self.right_editor.set_spec(mode.get("cw"))
            self.left_editor.set_spec(mode.get("ccw"))
        finally:
            self._loading = loading

    def _detail_changed(self, *_args) -> None:
        item = self.list.currentItem()
        if item is None or self._loading:
            return
        item.setData(Qt.ItemDataRole.UserRole, {"name": self.name_edit.text().strip() or "Mode",
                                               "cw": self.right_editor.spec(), "ccw": self.left_editor.spec()})
        self._emit()

    def add_mode(self) -> None:
        names = {m.get("name") for m in self.modes()}
        n = 1
        while f"Mode {n}" in names:
            n += 1
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, {"name": f"Mode {n}", "cw": {"scroll": 1}, "ccw": {"scroll": -1}})
        self.list.addItem(item)
        self.list.setCurrentItem(item)
        self._emit()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)
            self._emit()

    def _emit(self, *_args) -> None:
        self._relabel()
        if not self._loading:
            self.changed.emit(self.modes())
