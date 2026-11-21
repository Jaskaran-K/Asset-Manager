"""Engine: device events -> profile lookup -> actions.

Events arrive on the reader thread and are queued; a single worker thread
runs the actions in order, so a slow macro never blocks reading the device
and press/release ordering is always preserved.

The app (ui.keydial_app) also sets:
    paused       True: presses do nothing (the tray's Pause)
    pinned       manual mode's profile, used whatever app is in front; None: auto mode (the profile
                 follows the app in front)
    own_window   returns True while KeyDial's own window is in front: presses then pick keys in
                 the editor instead of sending shortcuts into it
    on_state     called (profile, dial mode name or None, mode index, mode count, why) when the
                 profile in use or its dial mode changes; why is "profile" or "dial"
    on_pin       called (profile or None) when a Profile action switches mode or profile
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional, Protocol

from .actions import Action, Delay, DialMode, Hold, Keys, Launch, Macro, ProfileSwitch, Scroll, Text
from .foreground import get_foreground_exe
from .keys import MODIFIERS
from .profiles import Config, Profile
from .protocol import ButtonEvent, DialEvent, Event

log = logging.getLogger(__name__)


class Output(Protocol):
    def press_combo(self, combo: tuple[str, ...]) -> None: ...
    def release_combo(self, combo: tuple[str, ...]) -> None: ...
    def tap_combo(self, combo: tuple[str, ...]) -> None: ...
    def type_text(self, text: str) -> None: ...
    def scroll(self, dx: int, dy: int, modifiers: tuple[str, ...] = ()) -> None: ...
    def launch(self, target: str, args: tuple[str, ...] = ()) -> None: ...


class Engine:
    def __init__(
        self,
        get_config: Callable[[], Config],
        output: Output,
        foreground: Callable[[], Optional[str]] = get_foreground_exe,
        notify: Callable[[str], None] = print,
        verbose: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        own_window: Callable[[], bool] = lambda: False,
        on_state: Optional[Callable[[str, Optional[str], int, int, str], None]] = None,
        on_pin: Optional[Callable[[Optional[str]], None]] = None,
    ) -> None:
        self._get_config = get_config
        self._out = output
        self._foreground = foreground
        self._notify = notify
        self._verbose = verbose
        self._sleep = sleep
        self._own_window = own_window
        self._on_state = on_state
        self._on_pin = on_pin
        self.paused = False
        self.pinned: Optional[str] = None
        # button -> action that ran on press (so release matches it even if focus changed)
        self._pressed: dict[str, Optional[Action]] = {}
        self._dial_mode: dict[str, int] = {}  # profile name -> mode index
        self._last_profile: Optional[str] = None
        self._queue: "queue.Queue[Optional[Event]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    # -- threading ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._worker, name="KeyDialEngine", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout)
        self.release_all()

    def handle(self, event: Event) -> None:
        """Thread-safe entry point (pass this to KD100Reader)."""
        self._queue.put(event)

    def _worker(self) -> None:
        while True:
            ev = self._queue.get()
            if ev is None:
                return
            try:
                self.process(ev)
            except Exception:
                log.exception("failed to process %r", ev)
                self._notify(f"error while handling {ev}; see log")

    # -- state -------------------------------------------------------------
    def dial_mode_name(self, profile: Profile) -> Optional[str]:
        if not profile.dial:
            return None
        return profile.dial[self._dial_mode.get(profile.name, 0) % len(profile.dial)].name

    def release_all(self) -> None:
        """Release any keys still held by 'hold' actions (safety on stop/disconnect)."""
        for btn, action in list(self._pressed.items()):
            if isinstance(action, Hold):
                self._out.release_combo(action.combo)
        self._pressed.clear()

    # -- processing (synchronous; called by the worker, or directly in tests)
    def _current_profile(self) -> tuple[Profile, Optional[str]]:
        exe = self._foreground()
        config = self._get_config()
        prof = config.named(self.pinned) or config.profile_for(exe)
        if prof.name != self._last_profile:
            self._last_profile = prof.name
            mode = self.dial_mode_name(prof)
            self._notify(f"profile: {prof.name}" + (f"  (dial: {mode})" if mode else ""))
            self._tell(prof, "profile")
        return prof, exe

    def _tell(self, prof: Profile, why: str) -> None:
        if self._on_state is None:
            return
        n = len(prof.dial)
        index = self._dial_mode.get(prof.name, 0) % n if n else 0
        try:
            self._on_state(prof.name, self.dial_mode_name(prof), index, n, why)
        except Exception:  # (never let the pop-up stop the keys)
            log.exception("on_state failed")

    def current(self) -> tuple[str, Optional[str], int, int]:
        """(profile, dial mode, mode index, mode count) for the app in front now."""
        config = self._get_config()
        prof = config.named(self.pinned) or config.profile_for(self._foreground())
        n = len(prof.dial)
        return prof.name, self.dial_mode_name(prof), (self._dial_mode.get(prof.name, 0) % n if n else 0), n

    def process(self, ev: Event) -> None:
        if isinstance(ev, ButtonEvent) and not ev.pressed and ev.button in self._pressed:
            pass  # (a release always ends what its press started, even if paused since)
        elif self.paused or self._own_window():
            return
        if isinstance(ev, ButtonEvent):
            if ev.pressed:
                prof, exe = self._current_profile()
                action = prof.buttons.get(ev.button)
                if isinstance(action, Keys) and all(key in MODIFIERS for key in action.combo):
                    # A shortcut that's only Ctrl / Shift / Alt / Win is held for as long as the key is,
                    # so it combines with the next KD100 key (a tap of Ctrl alone does nothing anyway)
                    action = Hold(action.combo)
                self._pressed[ev.button] = action
                if self._verbose:
                    self._notify(f"{ev.button} down  [{exe or '?'} -> {prof.name}]  {action}")
                self._run(action, prof, press=True)
            else:
                action = self._pressed.pop(ev.button, None)
                if isinstance(action, Hold):
                    self._out.release_combo(action.combo)
        elif isinstance(ev, DialEvent):
            prof, exe = self._current_profile()
            if not prof.dial:
                return
            mode = prof.dial[self._dial_mode.get(prof.name, 0) % len(prof.dial)]
            action = mode.cw if ev.step > 0 else mode.ccw
            if self._verbose:
                d = "cw" if ev.step > 0 else "ccw"
                self._notify(f"dial {d}  [{exe or '?'} -> {prof.name} / {mode.name}]  {action}")
            self._run(action, prof, press=True)

    def _run(self, action: Optional[Action], prof: Profile, press: bool) -> None:
        if action is None:
            return
        if isinstance(action, Hold):
            self._out.press_combo(action.combo)
        elif isinstance(action, Keys):
            self._out.tap_combo(action.combo)
        elif isinstance(action, Text):
            self._out.type_text(action.text)
        elif isinstance(action, Scroll):
            self._out.scroll(action.dx, action.dy, action.modifiers)
        elif isinstance(action, Launch):
            try:
                self._out.launch(action.target, action.args)
            except OSError as exc:
                self._notify(f"couldn't launch {action.target!r}: {exc}")
        elif isinstance(action, Delay):
            self._sleep(action.ms / 1000)
        elif isinstance(action, Macro):
            for step in action.steps:
                self._run(step, prof, press=True)
        elif isinstance(action, DialMode):
            self._switch_dial_mode(prof, action.target)
        elif isinstance(action, ProfileSwitch):
            self._switch_profile(prof, action.target)

    def _switch_profile(self, prof: Profile, target: str) -> None:
        """Manual mode with the next / previous / named profile, or ("auto") back to auto mode."""
        config = self._get_config()
        names = [p.name for p in config.profiles]
        if target == "auto":
            pinned = None
        elif target in ("next", "prev"):
            index = names.index(prof.name) if prof.name in names else 0
            pinned = names[(index + (1 if target == "next" else -1)) % len(names)]
        elif config.named(target) is not None:
            pinned = target
        else:
            self._notify(f"no profile called {target!r}")
            return
        self.pinned = pinned
        if self._on_pin is not None:
            try:
                self._on_pin(pinned)
            except Exception:
                log.exception("on_pin failed")
        self._last_profile = None  # (so the pop-up says which profile is in use now)
        self._current_profile()

    def _switch_dial_mode(self, prof: Profile, target: str) -> None:
        if not prof.dial:
            return
        n = len(prof.dial)
        cur = self._dial_mode.get(prof.name, 0) % n
        if target == "next":
            new = (cur + 1) % n
        elif target == "prev":
            new = (cur - 1) % n
        else:
            names = [m.name.lower() for m in prof.dial]
            if target.lower() not in names:
                self._notify(f"{prof.name}: no dial mode called {target!r}")
                return
            new = names.index(target.lower())
        self._dial_mode[prof.name] = new
        self._notify(f"{prof.name}: dial -> {prof.dial[new].name}")
        self._tell(prof, "dial")
