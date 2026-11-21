"""KeyDial: the Huion KD100's keys and dial, as a tray app of its own (keydial_main.py; shipped with
Asset Manager as KeyDial.exe).

KeyDialService runs the device: core.keydial's reader and engine work on their own threads, and
what they report reaches the UI through `device_worker`'s signals (connected to slots, never to
lambdas: CLAUDE.md's thread rules). The tray icon, the editor (ui.keydial_editor) and the pop-up
(ui.keydial_popup) sit on top.

Once KeyDial has started the KD100 it stays in its raw mode, sending nothing of its own, until
it's unplugged: so KeyDial keeps running in the tray when the editor is closed, and starts with
Windows (Settings: core.keydial.autostart).

    KeyDial.exe                 the tray, and the editor
    KeyDial.exe --tray          the tray only (how Windows starts it)
    KeyDial.exe --self-test LOG the release build's check (no window, no device)
    KeyDial.exe --remove-autostart   the uninstaller's clean-up
"""

from __future__ import annotations

import getpass
import hashlib
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from core.keydial import autostart, foreground, store
from core.keydial.actions import ConfigError
from core.keydial.engine import Engine
from core.keydial.profiles import ConfigStore, load_config
from core.keydial import presets
from ui.duotone import duotone_icon, set_icon

log = logging.getLogger(__name__)

HUION_DRIVER = "huiontablet.exe"
STATUS_TEXT = {
    "connected": "KD100 connected",
    "waiting for device": "Waiting for the KD100",
    "disconnected": "KD100 unplugged",
    "hidapi missing": "Can't read devices (hidapi missing)",
    "starting": "Starting...",
}


def instance_name() -> str:
    """One KeyDial per Windows user."""
    who = hashlib.sha1(getpass.getuser().encode("utf-8", "replace")).hexdigest()[:10]
    return f"KeyDial-{who}"


class DeviceWorker(QObject):
    """Signals emitted from the reader's and the engine's threads (delivered on the UI thread)."""

    status = Signal(str)
    button = Signal(str, bool)
    dial = Signal(int)
    state = Signal(str, object, int, int, str)
    pin = Signal(object)  # (a Profile action switched mode or profile)


class KeyDialService(QObject):
    status_changed = Signal(str)
    button_event = Signal(str, bool)
    dial_turned = Signal(int)
    state_changed = Signal(str, object, int, int, str)  # profile, dial mode, index, count, why
    front_changed = Signal(str)  # the profile for the app in front (not counting KeyDial itself)
    paused_changed = Signal(bool)
    profiles_changed = Signal()
    mode_changed = Signal()  # auto / manual, or manual mode's profile

    def __init__(self, reader_factory=None, output=None, start_device: bool = True, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.settings = store.load_settings()
        self.status = "starting"
        self.front_profile = "Default"
        self._front_exe: Optional[str] = None
        self._load_error: Optional[str] = None
        store.ensure_profiles()
        self.store = ConfigStore(store.profiles_path(), on_error=self._log,
                                 fallback=load_config(presets.starter_document()))
        self._load_error = self.store.error
        self.device_worker = DeviceWorker()
        self.device_worker.status.connect(self._on_status)
        self.device_worker.button.connect(self._on_button)
        self.device_worker.dial.connect(self._on_dial)
        self.device_worker.state.connect(self._on_state)
        self.device_worker.pin.connect(self._on_pin_from_key)
        if output is None:
            from core.keydial.output import SystemOutput

            output = SystemOutput()
        self.engine = Engine(self.store.get, output, notify=self._log, own_window=foreground.own_window_in_front,
                             on_state=self._state_from_engine, on_pin=self._pin_from_engine)
        self._apply_mode()
        self.engine.start()
        if reader_factory is None:
            from core.keydial.device import KD100Reader

            reader_factory = KD100Reader
        self.reader = reader_factory(self._from_reader, on_status=self._status_from_reader)
        if start_device:
            self.reader.start()
        self._front_timer = QTimer(self)
        self._front_timer.setInterval(700)
        self._front_timer.timeout.connect(self._check_front)
        self._front_timer.start()

    # -- from the other threads (plain calls: they only emit) ----------------------------------
    def _from_reader(self, event) -> None:
        self.engine.handle(event)
        from core.keydial.protocol import ButtonEvent

        if isinstance(event, ButtonEvent):
            self.device_worker.button.emit(event.button, event.pressed)
        else:
            self.device_worker.dial.emit(event.step)

    def _status_from_reader(self, status: str) -> None:
        self.device_worker.status.emit(status)

    def _state_from_engine(self, profile: str, mode: Optional[str], index: int, count: int, why: str) -> None:
        self.device_worker.state.emit(profile, mode, index, count, why)

    def _pin_from_engine(self, profile: Optional[str]) -> None:
        self.device_worker.pin.emit(profile)

    @staticmethod
    def _log(message: str) -> None:
        log.info("keydial: %s", message)

    # -- on the UI thread ------------------------------------------------------------------------
    @Slot(str)
    def _on_status(self, status: str) -> None:
        self.status = status
        self.status_changed.emit(status)

    @Slot(str, bool)
    def _on_button(self, button: str, down: bool) -> None:
        self.button_event.emit(button, down)

    @Slot(int)
    def _on_dial(self, step: int) -> None:
        self.dial_turned.emit(step)

    @Slot(str, object, int, int, str)
    def _on_state(self, profile: str, mode, index: int, count: int, why: str) -> None:
        self.state_changed.emit(profile, mode, index, count, why)

    @Slot(object)
    def _on_pin_from_key(self, profile) -> None:
        """A Profile action on a KD100 key: keep what it chose."""
        if profile:
            self.settings["manual_profile"] = profile
            store.save_setting("manual_profile", profile)
        self.set_setting("mode", "manual" if profile else "auto")
        self._check_front()
        self.mode_changed.emit()

    @Slot()
    def _check_front(self) -> None:
        """The profile in use: manual mode's, or the one for the last app in front (KeyDial's own window
        doesn't count, so the editor shows the app you came from)."""
        if not foreground.own_window_in_front():
            exe = foreground.get_foreground_exe()
            if exe:
                self._front_exe = exe
        config = self.store.get()
        profile = (config.named(self.engine.pinned) or config.profile_for(self._front_exe)).name
        if profile != self.front_profile:
            self.front_profile = profile
            self.front_changed.emit(profile)

    def status_text(self) -> str:
        if self.engine.paused:
            return "Paused"
        return STATUS_TEXT.get(self.status, self.status)

    def connected(self) -> bool:
        return self.status == "connected"

    # -- profiles --------------------------------------------------------------------------------
    def document(self) -> tuple[dict, Optional[str]]:
        doc, error = store.load_document()
        return doc, error

    def save(self, doc: dict) -> Optional[str]:
        """Save the editor's profiles and use them straight away. Returns a problem, or None."""
        try:
            if self._load_error:  # (the file couldn't be read: keep it beside, never just lost)
                broken = store.profiles_path().with_suffix(".json.broken")
                if store.profiles_path().exists() and not broken.exists():
                    shutil.copy2(store.profiles_path(), broken)
                self._load_error = None
            store.save_document(doc)
            self.store.set(doc)
        except (ConfigError, OSError) as exc:
            return str(exc)
        if self.mode() == "manual" and self.engine.pinned != self.manual_profile():
            self._apply_mode()
        self.profiles_changed.emit()
        return None

    def mode_index(self, profile: str, count: int) -> int:
        return self.engine._dial_mode.get(profile, 0) % count if count else 0

    # -- settings --------------------------------------------------------------------------------
    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        store.save_setting(key, value)

    def set_paused(self, paused: bool) -> None:
        self.engine.paused = paused
        if paused:
            self.engine.release_all()
        self.paused_changed.emit(paused)
        self.status_changed.emit(self.status)

    # -- auto / manual mode -------------------------------------------------------------------
    def mode(self) -> str:
        return "manual" if self.settings.get("mode") == "manual" else "auto"

    def manual_profile(self) -> str:
        """Manual mode's profile (Default if the one chosen has gone)."""
        name = self.settings.get("manual_profile")
        return name if name and self.store.get().named(name) is not None else "Default"

    def _apply_mode(self) -> None:
        self.engine.pinned = self.manual_profile() if self.mode() == "manual" else None
        self.engine._last_profile = None  # (the pop-up says which profile is in use on the next press)

    def set_mode(self, mode: str) -> None:
        """"auto": the profile follows the app in front. "manual": manual_profile() in every app."""
        if mode == "manual" and not self.settings.get("manual_profile"):
            self.settings["manual_profile"] = self.front_profile
            store.save_setting("manual_profile", self.front_profile)
        self.set_setting("mode", "manual" if mode == "manual" else "auto")
        self._mode_switched()

    def set_manual_profile(self, profile: str) -> None:
        """Use profile in every app (manual mode)."""
        self.settings["manual_profile"] = profile
        store.save_setting("manual_profile", profile)
        self.set_setting("mode", "manual")
        self._mode_switched()

    def profile_renamed(self, old: str, new: str) -> None:
        if self.settings.get("manual_profile") == old:
            self.settings["manual_profile"] = new
            store.save_setting("manual_profile", new)
            self._mode_switched()

    def _mode_switched(self) -> None:
        self._apply_mode()
        self._check_front()
        self.mode_changed.emit()
        profile, mode, index, count = self.engine.current()
        self.state_changed.emit(profile, mode, index, count, "profile")  # (the pop-up)

    def apply_autostart(self) -> Optional[str]:
        return autostart.apply(bool(self.settings.get("start_with_windows")), bool(self.settings.get("run_as_admin")))

    @staticmethod
    def huion_driver_running() -> bool:
        return HUION_DRIVER in foreground.running_exes()

    def shutdown(self) -> None:
        self._front_timer.stop()
        try:
            self.reader.stop()
        finally:
            self.engine.stop()  # (lets go of any keys a Hold Keys action still holds)


class KeyDialApp(QObject):
    """The tray icon and its menu, the editor window and the pop-up."""

    def __init__(self, service: KeyDialService, server: Optional[QLocalServer] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        from ui.keydial_popup import ProfilePopup

        self.service = service
        self.server = server
        self.editor = None
        self.popup = ProfilePopup()
        self.popup.place = service.settings.get("popup_place", "bottom")
        service.state_changed.connect(self._on_state)
        service.status_changed.connect(self._refresh_tray)
        service.paused_changed.connect(self._refresh_tray)
        service.profiles_changed.connect(self._refresh_profiles_menu)
        service.mode_changed.connect(self._refresh_profiles_menu)
        service.mode_changed.connect(self._refresh_tray)
        if server is not None:
            server.newConnection.connect(self._on_knock)
        self.tray = QSystemTrayIcon(duotone_icon("keydial"), self)
        self.tray.activated.connect(self._on_tray)
        self.menu = QMenu()
        self._build_menu()
        self.tray.setContextMenu(self.menu)
        self._refresh_tray()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    # -- the menu --------------------------------------------------------------------------------
    def _build_menu(self) -> None:
        menu = self.menu
        self.status_action = menu.addAction("KeyDial")
        self.status_action.setEnabled(False)
        self.front_action = menu.addAction("")
        self.front_action.setEnabled(False)
        menu.addSeparator()
        self.open_action = menu.addAction("Open KeyDial")
        set_icon(self.open_action, "keydial", "text")
        self.open_action.triggered.connect(self.show_editor)
        self.pause_action = menu.addAction("Pause")
        set_icon(self.pause_action, "pause", "text")
        self.pause_action.triggered.connect(self.toggle_pause)
        self.auto_action = menu.addAction("Auto Mode (the profile follows the app in front)")
        self.manual_action = menu.addAction("Manual Mode (one profile in every app)")
        modes = QActionGroup(menu)
        for action, mode in ((self.auto_action, "auto"), (self.manual_action, "manual")):
            action.setCheckable(True)
            action.setData(mode)
            modes.addAction(action)
        modes.triggered.connect(self._on_mode_action)
        self.profiles_menu = menu.addMenu("Manual Profile")
        self._refresh_profiles_menu()
        menu.addSeparator()
        self.start_action = self._check_action("Start with Windows", "start_with_windows", self._toggle_start)
        self.admin_action = self._check_action("Run as Administrator", "run_as_admin", self._toggle_admin)
        self.popup_action = self._check_action("Show Pop-up", "show_popup", self._toggle_popup)
        menu.addSeparator()
        self.quit_action = menu.addAction("Quit KeyDial")
        set_icon(self.quit_action, "close", "text")
        self.quit_action.triggered.connect(self.quit)
        menu.aboutToShow.connect(self._refresh_tray)

    def _check_action(self, text: str, key: str, slot) -> QAction:
        action = self.menu.addAction(text)
        action.setCheckable(True)
        action.setChecked(bool(self.service.settings.get(key)))
        action.toggled.connect(slot)
        return action

    @Slot()
    def _refresh_profiles_menu(self) -> None:
        """Manual Profile: picking one switches to manual mode with it."""
        self.profiles_menu.clear()
        group = QActionGroup(self.profiles_menu)
        manual = self.service.manual_profile()
        for profile in self.service.store.get().profiles:
            action = self.profiles_menu.addAction(profile.name)
            action.setCheckable(True)
            action.setChecked(profile.name == manual)
            action.setData(profile.name)
            group.addAction(action)
        group.triggered.connect(self._on_manual_profile)
        manual_mode = self.service.mode() == "manual"
        self.auto_action.setChecked(not manual_mode)
        self.manual_action.setChecked(manual_mode)

    @Slot(QAction)
    def _on_manual_profile(self, action: QAction) -> None:
        self.service.set_manual_profile(action.data())

    @Slot(QAction)
    def _on_mode_action(self, action: QAction) -> None:
        self.service.set_mode(action.data())

    @Slot()
    def _refresh_tray(self, *_args) -> None:
        service = self.service
        self.status_action.setText(f"KeyDial · {service.status_text()}")
        if service.mode() == "manual":
            self.front_action.setText(f"Manual: {service.manual_profile()} in every app")
        else:
            self.front_action.setText(f"Auto: {service.front_profile} (the app in front)")
        self.pause_action.setText("Resume" if service.engine.paused else "Pause")
        set_icon(self.pause_action, "play" if service.engine.paused else "pause", "text")
        self.tray.setToolTip(f"KeyDial: {service.status_text()}")

    # -- actions ---------------------------------------------------------------------------------
    @Slot()
    def show_editor(self) -> None:
        if self.editor is None:
            from ui.keydial_editor import KeyDialWindow

            self.editor = KeyDialWindow(self.service, self)
        self.editor.show_and_raise()

    @Slot()
    def toggle_pause(self) -> None:
        self.service.set_paused(not self.service.engine.paused)

    @Slot(bool)
    def _toggle_start(self, on: bool) -> None:
        self.service.set_setting("start_with_windows", on)
        self._tell_problem(self.service.apply_autostart())

    @Slot(bool)
    def _toggle_admin(self, on: bool) -> None:
        self.set_run_as_admin(on)

    def set_run_as_admin(self, on: bool) -> None:
        self.service.set_setting("run_as_admin", on)
        if self.admin_action.isChecked() != on:
            self.admin_action.blockSignals(True)
            self.admin_action.setChecked(on)
            self.admin_action.blockSignals(False)
        if on and not autostart.is_elevated():
            answer = QMessageBox.question(
                None, "Run as Administrator",
                "KeyDial will start again as administrator, so its shortcuts reach apps that run as administrator. "
                "Windows asks once now; after that it starts with Windows without asking.\n\nRestart KeyDial now?")
            if answer == QMessageBox.StandardButton.Yes:
                self.restart_elevated()
                return
        self._tell_problem(self.service.apply_autostart())

    def restart_elevated(self) -> None:
        if self.server is not None:
            self.server.close()  # (the new KeyDial takes over the single-instance name)
        if autostart.relaunch_elevated(("--elevated",)):
            self.quit()
        else:
            if self.server is not None:
                self.server.listen(instance_name())
            self._tell_problem("KeyDial wasn't restarted as administrator (Windows' question was declined).")

    @Slot(bool)
    def _toggle_popup(self, on: bool) -> None:
        self.service.set_setting("show_popup", on)

    def _tell_problem(self, problem: Optional[str]) -> None:
        if problem and self.tray.isVisible():
            self.tray.showMessage("KeyDial", problem, QSystemTrayIcon.MessageIcon.Warning, 6000)
        elif problem:
            log.warning("keydial: %s", problem)

    @Slot(str, object, int, int, str)
    def _on_state(self, profile: str, mode, index: int, count: int, why: str) -> None:
        self._refresh_tray()
        if self.service.settings.get("show_popup", True):
            self.popup.place = self.service.settings.get("popup_place", "bottom")
            self.popup.show_state(profile, mode, index, count)

    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_tray(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_editor()

    @Slot()
    def _on_knock(self) -> None:
        """Another KeyDial was started: show this one's editor instead (it then quits)."""
        show = False
        while self.server is not None and self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket.bytesAvailable() or socket.waitForReadyRead(300):
                show = show or bytes(socket.readAll().data()).startswith(b"show")
            socket.disconnectFromServer()
        if show:
            self.show_editor()

    def tell_still_running(self) -> None:
        """The first time the editor is closed: say KeyDial is still in the tray."""
        if not self.service.settings.get("tray_tip_seen") and self.tray.isVisible():
            self.tray.showMessage("KeyDial is still running", "Your KD100 keeps working. Open KeyDial or quit it from "
                                  "its icon in the tray.", QSystemTrayIcon.MessageIcon.Information, 5000)
            self.service.set_setting("tray_tip_seen", True)

    @Slot()
    def quit(self) -> None:
        if self.editor is not None:
            self.editor.save_now()
            self.editor.allow_close = True
            self.editor.close()
        self.popup.hide()
        self.tray.hide()
        self.service.shutdown()
        if self.server is not None:
            self.server.close()
        QApplication.quit()


# -- starting --------------------------------------------------------------------------------------
def _knock_existing(message: bytes) -> bool:
    """True if a KeyDial is already running (message b"show" asks it to show its editor)."""
    socket = QLocalSocket()
    socket.connectToServer(instance_name())
    if socket.waitForConnected(400):
        if message:
            socket.write(message)
            socket.flush()
            socket.waitForBytesWritten(400)
        socket.disconnectFromServer()
        return True
    return False


def _listen() -> Optional[QLocalServer]:
    server = QLocalServer()
    if server.listen(instance_name()):
        return server
    QLocalServer.removeServer(instance_name())  # (a name left over from a KeyDial that crashed)
    return server if server.listen(instance_name()) else None


def run_self_test(log_path: Optional[str]) -> int:
    """The release build's check: the libraries KeyDial needs load, and its windows build. No device is opened."""
    import os
    import traceback

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    lines, ok = [], True

    def step(name: str, fn) -> None:
        nonlocal ok
        try:
            detail = fn()
            lines.append(f"PASS  {name}" + (f": {detail}" if detail else ""))
        except Exception:  # pylint: disable=broad-except
            ok = False
            lines.append(f"FAIL  {name}\n{traceback.format_exc()}")

    def libraries():
        import hid
        from pynput import keyboard, mouse

        # (pynput picks its Windows backend as it's imported: a build missing it fails here)
        return f"hidapi {getattr(hid, '__version__', '')}, pynput ({keyboard.Controller.__module__}, {mouse.Controller.__module__})"

    def profiles():
        config = load_config(presets.starter_document())
        return f"{len(config.profiles)} starter profiles"

    def setup():
        from ui.themes import current_theme_id

        set_up(QApplication.instance())
        from core.keydial import browsers

        return f"theme {current_theme_id()}, {len(browsers.all_browsers())} browsers known"

    def windows():
        from ui.keydial_editor import AddProgramDialog

        class _Reader:
            def __init__(self, *a, **k): pass
            def start(self): pass
            def stop(self): pass

        class _Output:
            def __getattr__(self, name):
                return lambda *a, **k: None

        import tempfile

        real_dir = store.DATA_DIR
        with tempfile.TemporaryDirectory() as folder:
            store.DATA_DIR = Path(folder)  # (never the real profiles or settings)
            service = KeyDialService(reader_factory=_Reader, output=_Output(), start_device=False)
            keydial = None
            try:
                keydial = KeyDialApp(service)  # (the tray menu and the pop-up)
                keydial.show_editor()
                window = keydial.editor
                for button in ("K1", "DIAL", None):
                    window.select(button)
                for kind in window.key_editor.picker.kinds:  # (every kind of action's page)
                    window.select("K1")
                    window.key_editor._on_kind(kind)
                service.set_mode("manual")
                service.set_mode("auto")
                keydial.popup.show_state("Maya", "Zoom", 0, 4)
                dialog = AddProgramDialog(window)
                dialog.deleteLater()
                window.grab()
                window.allow_close = True
                window.close()
            finally:
                if keydial is not None:
                    keydial.popup.hide()
                    keydial.tray.hide()
                service.shutdown()
                store.DATA_DIR = real_dir
        return "tray, pop-up, editor"

    step("libraries", libraries)
    step("starter profiles", profiles)
    step("set-up", setup)
    step("windows", windows)
    report = "\n".join(lines)
    if log_path:
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
    else:
        print(report)
    del app
    return 0 if ok else 1


def set_up(app: QApplication) -> None:
    """KeyDial's look and behaviour (the self-test runs this too, so a build missing a library it
    needs fails there, not on your PC)."""
    from core.settings import get_theme
    from ui import main_thread_gc
    from ui.animations import install_popup_animations
    from ui.themes import apply_theme
    from ui.views import install_view_effects

    app.setApplicationName("KeyDial")
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)  # (it lives in the tray)
    app.setWindowIcon(duotone_icon("keydial"))
    apply_theme(get_theme(), app, thumbnails=False)  # (Asset Manager's theme)
    install_popup_animations(app)
    install_view_effects(app)
    main_thread_gc.install(app)


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        index = argv.index("--self-test")
        return run_self_test(argv[index + 1] if index + 1 < len(argv) else None)
    if "--remove-autostart" in argv:
        autostart.remove_all()
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AssetManager.KeyDial")
    app = QApplication(argv)
    set_up(app)

    if "--elevated" in argv:  # (restarted as administrator: the old KeyDial is quitting)
        for _ in range(20):
            if not _knock_existing(b""):
                break
            time.sleep(0.25)
    elif _knock_existing(b"show"):
        return 0
    server = _listen()

    service = KeyDialService()
    keydial = KeyDialApp(service, server)
    problem = service.apply_autostart() if service.settings.get("start_with_windows") or autostart.is_elevated() else None
    if problem and "next time" not in problem:
        keydial._tell_problem(problem)
    if "--tray" not in argv or not QSystemTrayIcon.isSystemTrayAvailable():
        keydial.show_editor()
    code = app.exec()
    return code
