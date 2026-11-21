"""KeyDial, the Huion KD100's tray app: its core (keys, profiles, store, start-up) and its windows.
No device and no real input: a fake reader and a recording output stand in."""

from __future__ import annotations

import json
import threading
import time

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from core.keydial import autostart, document, presets, store
from core.keydial.actions import ConfigError, describe, kind_of, scroll_parts, scroll_spec
from core.keydial.engine import Engine
from core.keydial.keys import combo_text, key_text, modifier_for_vk, name_for_vk
from core.keydial.profiles import load_config
from core.keydial.protocol import ButtonEvent, DialEvent

VK_CTRL, VK_SHIFT, VK_ALT, VK_Z, VK_A, VK_F5 = 0x11, 0x10, 0x12, 0x5A, 0x41, 0x74


class RecordingOutput:
    def __init__(self):
        self.log = []

    def press_combo(self, c): self.log.append(("press", c))
    def release_combo(self, c): self.log.append(("release", c))
    def tap_combo(self, c): self.log.append(("tap", c))
    def type_text(self, t): self.log.append(("text", t))
    def scroll(self, dx, dy, modifiers=()): self.log.append(("scroll", dx, dy, modifiers))
    def launch(self, target, args=()): self.log.append(("launch", target))


class FakeReader:
    def __init__(self, on_event, on_status=None):
        self.on_event, self.on_status = on_event, on_status
        self.started = self.stopped = False

    def start(self): self.started = True
    def stop(self): self.stopped = True


def wait_until(condition, timeout=3.0):
    app = QApplication.instance()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def key_event(kind, vk, modifiers=Qt.KeyboardModifier.NoModifier, key=Qt.Key.Key_unknown):
    return QKeyEvent(kind, key, modifiers, 0, vk, 0, "")


@pytest.fixture
def service(qapp):
    from ui.keydial_app import KeyDialService

    made = KeyDialService(reader_factory=FakeReader, output=RecordingOutput(), start_device=False)
    yield made
    made.shutdown()


# -- core ------------------------------------------------------------------------------------------
def test_keys_from_the_keyboard_and_as_text():
    assert name_for_vk(VK_Z) == "z" and name_for_vk(VK_F5) == "f5" and name_for_vk(0xDB) == "["
    assert name_for_vk(0x60) == "num0" and name_for_vk(0xAF) == "volume_up"
    assert modifier_for_vk(0xA2) == "ctrl" and modifier_for_vk(VK_Z) is None
    assert combo_text("ctrl+shift+z") == "Ctrl+Shift+Z" and combo_text("alt+period") == "Alt+."
    assert key_text("page_down") == "Page Down" and key_text("num7") == "Num 7"
    assert combo_text("not a key+x") == ""


def test_actions_are_described_and_scrolls_round_trip():
    assert describe("ctrl+z") == "Ctrl+Z" and describe({"hold": "space"}) == "hold Space"
    assert describe(None) == "Nothing" and describe({"macro": ["a", "b"]}) == "Macro: 2 steps"
    assert describe({"scroll": 1, "with": "ctrl"}) == "Scroll up (Ctrl)" and describe({"scroll": -3}) == "Scroll down 3"
    assert describe({"dial_mode": "next"}) == "Next dial mode"
    assert [kind_of(s) for s in ("a", {"hold": "a"}, {"hscroll": 1}, {"delay": 5}, None)] == [
        "keys", "hold", "scroll", "delay", "none"]
    for direction in ("up", "down", "left", "right"):
        spec = scroll_spec(direction, 3, "alt")
        assert scroll_parts(spec) == (direction, 3, "alt")
        load_config({"profiles": [{"name": "Default", "buttons": {"K1": spec}}]})


def test_every_starter_profile_loads_and_names_each_key():
    config = load_config(presets.starter_document())
    names = [p.name for p in config.profiles]
    assert names == ["Default", "Maya", "ZBrush", "Substance 3D Painter", "Photoshop", "Blender", "Marmoset Toolbag",
                     "Multimedia", "Browsers"]
    for profile in config.profiles:
        assert profile.dial, profile.name
        assert profile.buttons.get("DIAL") is not None
        assert len(profile.labels) == 18, profile.name
    assert config.profile_for("spotify.exe").name == "Multimedia"
    for browser in ("chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe", "vivaldi.exe"):
        assert config.profile_for(browser).name == "Browsers", browser
    default = config.named("Default")  # (a number pad: the tall key is Enter, the wide one 0)
    assert default.labels["K16"] == "Enter" and default.labels["K17"] == "0" and default.buttons["K6"].combo == ("num8",)
    assert config.named("Marmoset Toolbag").buttons["K13"] is None  # (not a number: does nothing)
    maya = config.named("Maya")
    assert maya.labels["K16"] == "Camera" and maya.labels["K17"] == "Ctrl"  # (the tall key, the wide key)


def test_document_inherits_renames_and_deletes():
    doc = presets.starter_document()
    maya = document.resolve(doc, "Maya")
    assert maya["parent"] == "Default" and "K1" in maya["own"]
    child = document.add_profile(doc, presets.blank_profile("Krita", ["krita.exe"]))
    krita = document.resolve(doc, "Krita")
    assert "K4" not in krita["own"] and krita["buttons"]["K4"] == "backspace"  # (from Default)
    document.set_button(doc, "Krita", "K4", "f11", "Full")
    assert document.resolve(doc, "Krita")["buttons"]["K4"] == "f11"
    document.clear_button(doc, "Krita", "K4")
    assert document.resolve(doc, "Krita")["buttons"]["K4"] == "backspace"
    assert child == "Krita" and document.add_profile(doc, presets.blank_profile("Krita", [])) == "Krita 2"
    assert document.rename_profile(doc, "Default", "X") == "Default"
    doc["profiles"].append({"name": "Child", "inherits": "Krita"})
    assert document.rename_profile(doc, "Krita", "Krita 5") == "Krita 5"
    assert document.find(doc, "Child")["inherits"] == "Krita 5"
    document.remove_profile(doc, "Krita 5")
    assert document.find(doc, "Child")["inherits"] == "Default"
    with pytest.raises(ValueError):
        document.remove_profile(doc, "Default")
    load_config(doc)


def test_store_first_run_saving_and_a_broken_file():
    path = store.ensure_profiles()
    assert path.parent == store.DATA_DIR and json.loads(path.read_text("utf-8")) == presets.starter_document()
    doc, error = store.load_document()
    assert error is None
    with pytest.raises(ConfigError):
        store.save_document({"profiles": [{"name": "Not Default"}]})
    assert json.loads(path.read_text("utf-8")) == presets.starter_document()  # (nothing saved)
    path.write_text("{ broken", encoding="utf-8")
    doc, error = store.load_document()
    assert error and doc == presets.starter_document()
    assert store.load_settings()["show_popup"] is True
    store.save_setting("mode", "manual")
    assert store.load_settings()["mode"] == "manual"
    store.settings_path().write_text(json.dumps({"pinned": "Multimedia"}), encoding="utf-8")  # (the first KeyDial's)
    assert store.load_settings()["mode"] == "manual" and store.load_settings()["manual_profile"] == "Multimedia"
    with pytest.raises(KeyError):
        store.save_setting("nonsense", 1)


def test_an_older_profiles_file_is_upgraded_keeping_changed_keys():
    old_default = {"name": "Default", "buttons": dict(presets.DEFAULT_V1_BUTTONS), "labels": {"K1": "Undo"}}
    old_default["buttons"]["K1"] = {"macro": ["a", "b"]}  # (changed by the user: stays)
    old_default["labels"]["K1"] = "Mine"
    old_multimedia = {"name": "Multimedia", "apps": ["vlc.exe", "opera.exe"], "inherits": "Default",
                      "buttons": dict(presets.MULTIMEDIA_V1_BUTTONS)}
    old_toolbag = {"name": "Marmoset Toolbag", "apps": ["toolbag.exe"], "inherits": "Default", "buttons": {"K1": "ctrl+z"}}
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.profiles_path().write_text(json.dumps({"profiles": [old_default, old_multimedia, old_toolbag]}), encoding="utf-8")
    doc, error = store.load_document()
    assert error is None and doc["starter"] == presets.STARTER_VERSION
    default = document.find(doc, "Default")
    assert default["buttons"]["K1"] == {"macro": ["a", "b"]} and default["labels"]["K1"] == "Mine"
    assert default["buttons"]["K5"] == "num7" and default["labels"]["K16"] == "Enter"
    multimedia = document.find(doc, "Multimedia")
    assert multimedia["apps"] == ["vlc.exe", "opera.exe"] and multimedia["buttons"]["K17"] == "shift+comma"
    assert document.find(doc, "Marmoset Toolbag")["buttons"]["K13"] == "none"
    assert document.find(doc, "Browsers")["apps"] == ["@browsers"]
    assert json.loads(store.profiles_path().read_text("utf-8")) == doc  # (saved)
    assert (store.DATA_DIR / "profiles.starter-1.json").exists()  # (the old file, kept)
    assert store.upgrade_document(doc) is False


def test_starting_with_windows_and_as_administrator(monkeypatch):
    run_key, tasks = autostart._registry, autostart._schtasks
    monkeypatch.setattr(autostart, "is_elevated", lambda: False)
    assert autostart.apply(True, False) is None
    assert run_key.value.endswith(autostart.TRAY_ARG) and "keydial_main.py" in run_key.value
    note = autostart.apply(True, True)  # (not running as administrator yet: the normal entry stays)
    assert "next time" in note and run_key.value and not tasks.tasks
    monkeypatch.setattr(autostart, "is_elevated", lambda: True)
    assert autostart.apply(True, True) is None
    xml = tasks.tasks[autostart.TASK_NAME]
    for part in ("<RunLevel>HighestAvailable</RunLevel>", "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
                 "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>", "<LogonTrigger>", "--tray"):
        assert part in xml
    assert run_key.value is None
    assert autostart.apply(False, True) is None
    assert not tasks.tasks and run_key.value is None
    autostart.apply(True, False)
    autostart.remove_all()
    assert run_key.value is None


def test_engine_pause_pin_own_window_and_state():
    config = load_config(presets.starter_document())
    out, states = RecordingOutput(), []
    front = {"exe": "maya.exe", "own": False}
    engine = Engine(lambda: config, out, foreground=lambda: front["exe"], notify=lambda m: None,
                    own_window=lambda: front["own"], on_state=lambda *a: states.append(a))

    def tap(button):
        engine.process(ButtonEvent(button, True))
        engine.process(ButtonEvent(button, False))

    tap("K6")
    assert out.log == [("tap", ("w",))] and states[-1] == ("Maya", "Zoom", 0, 4, "profile")
    tap("DIAL")
    assert states[-1] == ("Maya", "Timeline", 1, 4, "dial")
    engine.process(DialEvent(+1))
    assert out.log[-1] == ("tap", ("alt", "period"))
    engine.paused = True
    tap("K6")
    assert len(out.log) == 2
    engine.paused = False
    engine.process(ButtonEvent("K4", True))  # (Hotbox: hold Space) ...
    engine.paused = True
    engine.process(ButtonEvent("K4", False))  # ... a pause never leaves a key held
    assert out.log[-1] == ("release", ("space",))
    engine.paused = False
    front["own"] = True  # (KeyDial's editor in front: the KD100 picks keys there instead)
    tap("K1")
    assert out.log[-1] == ("release", ("space",))
    front["own"] = False
    engine.pinned = "Multimedia"
    tap("K2")
    assert out.log[-1] == ("tap", ("play_pause",)) and states[-1][0] == "Multimedia"
    assert engine.current()[0] == "Multimedia"


def test_profile_action_switches_profiles_and_modes():
    doc = presets.starter_document()
    document.set_button(doc, "Default", "K1", {"profile": "next"})
    document.set_button(doc, "Maya", "K1", {"profile": "auto"})
    document.set_button(doc, "Maya", "K2", {"profile": "Browsers"})
    config = load_config(doc)
    out, pins, states = RecordingOutput(), [], []
    engine = Engine(lambda: config, out, foreground=lambda: "notepad.exe", notify=lambda m: None,
                    on_state=lambda *a: states.append(a), on_pin=pins.append)

    def tap(button):
        engine.process(ButtonEvent(button, True))
        engine.process(ButtonEvent(button, False))

    tap("K1")  # (Default in notepad: next profile, Maya, in every app)
    assert pins == ["Maya"] and engine.pinned == "Maya" and states[-1][0] == "Maya"
    tap("K2")
    assert engine.pinned == "Browsers"
    engine.pinned = "Maya"
    tap("K1")  # (back to auto: notepad's profile, Default)
    assert pins[-1] is None and engine.pinned is None and states[-1][0] == "Default"
    assert describe({"profile": "prev"}) == "Previous profile"


# -- the windows ----------------------------------------------------------------------------------
def test_device_picture_matches_the_kd100(qapp):
    from ui.keydial_device import KD100View

    view = KD100View()
    view.resize(400, 600)
    k13, k16, k17, k18 = (view.key_rect(n) for n in (12, 15, 16, 17))
    assert k16.height() > 2 * k13.height()  # (the tall key)
    assert k17.width() > 2 * k13.width()  # (the wide key)
    assert k18.left() > k17.right() and k16.left() > k18.right()
    assert view.button_at(k16.center()) == "K16" and view.button_at(view.dial_circle()[0]) == "DIAL"
    picked = []
    view.picked.connect(picked.append)
    centre = view.key_rect(5).center()
    QApplication.sendEvent(view, QMouseEvent(QEvent.Type.MouseButtonPress, centre, view.mapToGlobal(centre.toPoint()).toPointF(),
                                             Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert picked == ["K6"]
    view.set_labels({"K1": ("Undo", "Ctrl+Z", False)})
    view.set_pressed("K1", True)
    view.grab()  # (paints)
    view.deleteLater()


def test_shortcut_field_reads_shortcuts_and_held_keys(qapp):
    from ui.keydial_actions import ShortcutField

    field = ShortcutField()
    got = []
    field.changed.connect(got.append)
    ctrl_shift = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    for vk in (VK_CTRL, VK_SHIFT):
        field.event(key_event(QEvent.Type.KeyPress, vk))
    field.event(key_event(QEvent.Type.KeyPress, VK_Z, ctrl_shift, Qt.Key.Key_Z))
    assert got == ["ctrl+shift+z"] and field.combo() == "ctrl+shift+z"
    for vk in (VK_Z, VK_SHIFT, VK_CTRL):
        field.keyReleaseEvent(key_event(QEvent.Type.KeyRelease, vk))
    assert got == ["ctrl+shift+z"]  # (letting go doesn't change it)
    field.event(key_event(QEvent.Type.KeyPress, VK_ALT))
    field.keyReleaseEvent(key_event(QEvent.Type.KeyRelease, VK_ALT))
    assert got[-1] == "alt"  # (a modifier alone: for Hold Keys)
    field.event(key_event(QEvent.Type.KeyPress, 0x09))  # (Tab is a key too, not a move to the next field)
    assert got[-1] == "tab"
    field.grab()
    field.deleteLater()


def test_action_editor_round_trips_every_kind(qapp):
    from ui.keydial_actions import ActionEditor

    editor = ActionEditor()
    editor.set_mode_names(["Zoom", "Timeline"])
    for spec in ("ctrl+z", {"hold": "space"}, {"text": "hi"}, {"scroll": -2, "with": "ctrl"}, {"hscroll": 1},
                 {"launch": "C:/Tools/x.exe"}, {"macro": ["ctrl+a", {"delay": 50}, "ctrl+c"]}, {"dial_mode": "Timeline"},
                 {"dial_mode": "prev"}, None):
        editor.set_spec(spec)
        assert editor.spec() == spec, spec
    seen = []
    editor.changed.connect(seen.append)
    editor.picker.buttons["scroll"].click()
    assert editor.kind() == "scroll" and seen[-1] == {"scroll": 1}
    editor.picker.buttons["none"].click()
    assert seen[-1] is None
    editor.deleteLater()


def test_macro_records_keys_with_the_pauses_between(qapp, monkeypatch):
    from ui.keydial_actions import MacroEditor

    macro = MacroEditor()
    macro.set_steps(["ctrl+a"])
    got = []
    macro.changed.connect(got.append)
    macro.record_button.setChecked(True)
    macro.event(key_event(QEvent.Type.KeyPress, VK_CTRL))
    macro.event(key_event(QEvent.Type.KeyPress, 0x43, Qt.KeyboardModifier.ControlModifier, Qt.Key.Key_C))
    elapsed = {"ms": 400}
    monkeypatch.setattr(macro._clock, "elapsed", lambda: elapsed["ms"])
    macro.keyReleaseEvent(key_event(QEvent.Type.KeyRelease, VK_CTRL))
    macro.event(key_event(QEvent.Type.KeyPress, 0x56, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
                          Qt.Key.Key_V))
    macro.record_button.setChecked(False)
    assert macro.steps() == ["ctrl+a", "ctrl+c", {"delay": 400}, "ctrl+shift+v"]
    assert got[-1] == macro.steps()
    macro.list.setCurrentRow(2)
    assert macro.step_editor.isVisibleTo(macro) and macro.step_editor.spec() == {"delay": 400}
    macro._remove()
    assert macro.steps() == ["ctrl+a", "ctrl+c", "ctrl+shift+v"]
    macro.deleteLater()


def test_dial_modes_editor(qapp):
    from ui.keydial_actions import DialModesEditor

    modes = DialModesEditor()
    modes.set_modes([{"name": "Zoom", "cw": {"scroll": 1}, "ccw": {"scroll": -1}}])
    got = []
    modes.changed.connect(got.append)
    modes.add_mode()
    assert [m["name"] for m in got[-1]] == ["Zoom", "Mode 1"]
    modes.name_edit.setText("Size")
    modes.name_edit.textEdited.emit("Size")
    modes.right_editor.set_spec("]")
    modes.right_editor.changed.emit("]")
    assert got[-1][1] == {"name": "Size", "cw": "]", "ccw": {"scroll": -1}}
    modes.deleteLater()


def test_editor_changes_are_saved_and_used(qapp, service):
    from ui.keydial_editor import KeyDialWindow

    window = KeyDialWindow(service, None)
    window.select_profile("Maya")
    window.select("K6")
    assert window.key_title.text() == "Move" and window.key_editor.spec() == "w"
    window.key_editor.keys_field.set_combo("g", announce=True)
    window.save_now()
    saved = json.loads(store.profiles_path().read_text("utf-8"))
    assert document.find(saved, "Maya")["buttons"]["K6"] == "g"
    assert service.store.get().named("Maya").buttons["K6"].combo == ("g",)  # (used straight away)
    # Back to what Default does, and back again
    window.select("K15")
    window.same_box.setChecked(True)
    window.save_now()
    assert "K15" not in document.find(json.loads(store.profiles_path().read_text("utf-8")), "Maya")["buttons"]
    assert window.key_editor.spec() == "num3"  # (Default's number pad)
    # The dial
    window.select("DIAL")
    assert window.panels.currentWidget() is window.dial_panel
    assert [m["name"] for m in window.modes_editor.modes()] == ["Zoom", "Timeline", "Keyframes", "Undo / Redo"]
    window.modes_editor.add_mode()
    window.save_now()
    assert len(service.store.get().named("Maya").dial) == 5
    # Profiles: add one for another program, rename it, delete it
    name = window.add_profile(presets.blank_profile("Krita", ["krita.exe"]))
    assert name == "Krita" and window.profile_list.count() == 10
    window.browsers_box.setChecked(True)  # (Krita in every web browser too)
    window.save_now()
    assert service.store.get().profile_for("brave.exe").name == "Browsers"  # (Browsers comes first)
    assert "@browsers" in document.find(service.document()[0], "Krita")["apps"]
    window.browsers_box.setChecked(False)
    # Manual mode from the header
    window.manual_combo.setCurrentText("Maya")
    window._on_manual_combo(window.manual_combo.currentIndex())
    assert service.mode() == "manual" and service.engine.pinned == "Maya"
    window.mode_group.buttons()[0].click()
    assert service.mode() == "auto"
    window.profile_name.setText("Krita 5")
    window._on_rename()
    window.save_now()
    assert service.store.get().profile_for("krita.exe").name == "Krita 5"
    monkey_yes(window)
    window._delete_profile()
    window.save_now()
    assert service.store.get().profile_for("krita.exe").name == "Default"
    # A key pressed on the KD100 lights up in the picture
    service.device_worker.button.emit("K3", True)
    assert wait_until(lambda: "K3" in window.device._pressed)
    window.grab()
    window.allow_close = True
    window.close()
    window.deleteLater()


def monkey_yes(window):
    from PySide6.QtWidgets import QMessageBox

    import ui.keydial_editor as editor_module

    class _Yes:
        StandardButton = QMessageBox.StandardButton

        @staticmethod
        def question(*_a, **_k):
            return QMessageBox.StandardButton.Yes

    editor_module.QMessageBox = _Yes


@pytest.fixture(autouse=True)
def _restore_message_box():
    import ui.keydial_editor as editor_module
    from PySide6.QtWidgets import QMessageBox

    yield
    editor_module.QMessageBox = QMessageBox


def test_events_from_the_reader_thread_reach_the_ui(qapp, service):
    events, states = [], []
    service.button_event.connect(lambda b, d: events.append((b, d)))
    service.state_changed.connect(lambda *a: states.append(a))
    thread = threading.Thread(target=lambda: (service._from_reader(ButtonEvent("DIAL", True)),
                                              service._from_reader(ButtonEvent("DIAL", False)),
                                              service._status_from_reader("connected")))
    thread.start()
    thread.join()
    assert wait_until(lambda: len(events) == 2 and service.status == "connected" and states)
    assert events == [("DIAL", True), ("DIAL", False)]
    assert states[-1][4] == "dial"  # (the dial stepped to its next mode)


def test_popup_never_takes_focus(qapp):
    from ui.keydial_popup import ProfilePopup

    popup = ProfilePopup()
    assert popup.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert popup.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert popup.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    popup.show_state("Maya", "Timeline", 1, 4)
    assert popup.isVisible() and popup.text() == ("Maya", "Timeline", 1, 4)
    assert QApplication.activeWindow() is not popup
    popup.grab()
    popup._fade_out()
    assert wait_until(lambda: not popup.isVisible(), 2)
    popup.deleteLater()


def test_tray_pause_and_modes(qapp, service):
    from ui.keydial_app import KeyDialApp

    keydial = KeyDialApp(service)
    keydial.toggle_pause()
    assert service.engine.paused and keydial.pause_action.text() == "Resume"
    keydial.toggle_pause()
    assert not service.engine.paused
    assert keydial.auto_action.isChecked() and service.mode() == "auto"
    multimedia = next(a for a in keydial.profiles_menu.actions() if a.text() == "Multimedia")
    multimedia.trigger()  # (picking a manual profile: manual mode with it)
    assert service.engine.pinned == "Multimedia" and keydial.manual_action.isChecked()
    assert store.load_settings()["mode"] == "manual" and store.load_settings()["manual_profile"] == "Multimedia"
    assert service.front_profile == "Multimedia" and "Manual: Multimedia" in keydial.front_action.text()
    keydial.auto_action.trigger()
    assert service.engine.pinned is None and service.mode() == "auto"
    keydial.manual_action.trigger()  # (manual again: the profile picked before)
    assert service.engine.pinned == "Multimedia"
    service.device_worker.pin.emit(None)  # (a Profile action's "Auto Mode", from the engine's thread)
    assert wait_until(lambda: service.mode() == "auto" and keydial.auto_action.isChecked())
    keydial.show_editor()
    assert keydial.editor is not None and keydial.editor.isVisible()
    keydial.editor.close()  # (closing hides it: KeyDial stays in the tray)
    assert not keydial.editor.isVisible()
    keydial.editor.allow_close = True
    keydial.editor.close()
    keydial.editor.deleteLater()
    keydial.tray.hide()
    keydial.popup.deleteLater()
    keydial.menu.deleteLater()


def spec_excludes() -> list[str]:
    """The libraries KeyDial.spec leaves out of KeyDial's build."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "KeyDial.spec").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis":
            for keyword in node.keywords:
                if keyword.arg == "excludes":
                    return ast.literal_eval(keyword.value)
    raise AssertionError("no excludes in KeyDial.spec")


def test_self_test_passes_without_what_the_build_leaves_out(tmp_path):
    """KeyDial's self-test (its whole start-up: theme, tray, pop-up, editor) in a process of its own where
    every library KeyDial.spec leaves out can't be imported: KeyDial.exe 0.7.50 failed at start on numpy,
    which the theme's background textures need (and the self-test didn't apply the theme then)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    excludes = spec_excludes()
    assert "numpy" in excludes and "PIL" in excludes
    root = Path(__file__).resolve().parents[1]
    log = tmp_path / "self-test.log"
    blocker = (f"import sys, runpy\nfor name in {excludes!r}: sys.modules[name] = None\n"
               f"sys.argv = ['keydial_main.py', '--self-test', {str(log)!r}]\n"
               "runpy.run_path('keydial_main.py', run_name='__main__')\n")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", APPDATA=str(tmp_path / "appdata"))
    done = subprocess.run([sys.executable, "-c", blocker], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    report = log.read_text("utf-8") if log.exists() else ""
    assert done.returncode == 0, report + done.stdout + done.stderr
    assert "FAIL" not in report and "PASS  set-up" in report and "PASS  windows: tray, pop-up, editor" in report


# -- reading the device, and sending keys ---------------------------------------------------------------
PRESS_K1 = [0x08, 0xE0, 0x01, 0x01, 0x01, 0x00, 0x00, 0, 0, 0, 0, 0]
PRESS_K1_K2 = [0x08, 0xE0, 0x01, 0x01, 0x03, 0x00, 0x00, 0, 0, 0, 0, 0]
RELEASE = [0x08, 0xE0, 0x01, 0x01, 0x00, 0x00, 0x00, 0, 0, 0, 0, 0]


class ScriptedHandle:
    """A KD100 interface that sends the reports given ([] = nothing this time; an exception: unplugged)."""

    def __init__(self, reader, reports):
        self.reader, self.reports = reader, list(reports)

    def read(self, _size):
        if not self.reports:
            self.reader._stop.set()
            return []
        report = self.reports.pop(0)
        if isinstance(report, Exception):
            raise report
        return report


def read_events(handles_reports):
    from core.keydial.device import KD100Reader

    events = []
    reader = KD100Reader(events.append)
    handles = [ScriptedHandle(reader, reports) for reports in handles_reports]
    for handle in handles[1:]:  # (each runs out at the same time as the longest)
        handle.reports += [[]] * (max(len(h.reports) for h in handles) - len(handle.reports))
    reader._read_loop(handles)
    return [(e.button, e.pressed) for e in events]


def test_a_kd100_listed_twice_is_decoded_once():
    taps = [PRESS_K1, RELEASE, PRESS_K1, RELEASE, PRESS_K1_K2, PRESS_K1, RELEASE]
    # The second interface carries the same reports, a step behind: decoding both turned taps into extra
    # presses and releases, and broke the chord
    events = read_events([taps, [[]] + taps])
    assert events == [("K1", True), ("K1", False), ("K1", True), ("K1", False), ("K1", True), ("K2", True),
                      ("K2", False), ("K1", False)]


def test_the_other_interface_takes_over_when_the_first_is_unplugged():
    events = read_events([[PRESS_K1, RELEASE, OSError("unplugged")], [[], [], [], PRESS_K1, RELEASE]])
    assert events == [("K1", True), ("K1", False), ("K1", True), ("K1", False)]


def test_combos_are_sent_like_a_hand_presses_them(monkeypatch):
    from core.keydial import output

    sent, waits = [], []

    class Keyboard:
        def press(self, key): sent.append(("down", key))
        def release(self, key): sent.append(("up", key))

    out = output.SystemOutput(sleep=waits.append)
    out._kb = Keyboard()
    out.tap_combo(("ctrl", "shift", "z"))
    from pynput.keyboard import Key

    assert [s[0] for s in sent] == ["down", "down", "down", "up", "up", "up"]
    assert sent[0][1] == Key.ctrl_l and sent[1][1] == Key.shift_l  # (left ones: as a keyboard sends them)
    assert sum(waits) >= output.KEY_HOLD and len(waits) == 5 and sum(waits) < 0.05


def test_a_shortcut_of_only_ctrl_is_held_with_the_next_key():
    """A KD100 key set to the shortcut Ctrl, held while another set to A is pressed: Ctrl+A."""
    doc = presets.starter_document()
    document.set_button(doc, "Default", "K2", "ctrl", "Ctrl")
    document.set_button(doc, "Default", "K3", "a", "A")
    config = load_config(doc)
    out = RecordingOutput()
    engine = Engine(lambda: config, out, foreground=lambda: "notepad.exe", notify=lambda m: None)
    engine.process(ButtonEvent("K2", True))
    engine.process(ButtonEvent("K3", True))
    engine.process(ButtonEvent("K3", False))
    engine.process(ButtonEvent("K2", False))
    assert out.log == [("press", ("ctrl",)), ("tap", ("a",)), ("release", ("ctrl",))]
