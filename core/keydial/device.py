"""Read events from a Huion KD100 over its vendor HID interface.

Usage:
    from keydial_core.device import KD100Reader

    def handle(event):
        print(event)

    reader = KD100Reader(handle)
    reader.start()      # background thread, reconnects automatically
    ...
    reader.stop()
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

try:
    import hid  # pip package name: hidapi
except ImportError:  # (reported as the status "hidapi missing"; nothing else breaks)
    hid = None

from .protocol import Event, ReportDecoder, is_input_report

log = logging.getLogger(__name__)

HUION_VID = 0x256C
KD100_PIDS = {0x006D}
VENDOR_USAGE_PAGE = 0xFF00

# Reading string descriptor 200 switches Huion devices into raw vendor mode,
# which is what makes every key/dial step show up on the 0xFF00 interface.
INIT_STRING_INDEX = 200

# Follow another interface once the one followed has been quiet this long while that one sends (seconds)
SWITCH_AFTER = 1.0


def find_vendor_interfaces() -> list[dict]:
    if hid is None:
        return []
    return [
        d
        for d in hid.enumerate(HUION_VID)
        if d["product_id"] in KD100_PIDS and d["usage_page"] == VENDOR_USAGE_PAGE
    ]


class KD100Reader:
    """Background reader. Calls `on_event` from its own thread for every event.

    `on_status` (optional) is called with a short string when the connection
    state changes ("connected", "disconnected", "waiting for device").
    """

    def __init__(
        self,
        on_event: Callable[[Event], None],
        on_status: Optional[Callable[[str], None]] = None,
        retry_interval: float = 1.0,
    ) -> None:
        self._on_event = on_event
        self._on_status = on_status or (lambda s: None)
        self._retry = retry_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._decoder = ReportDecoder()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="KD100Reader", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def run_forever(self) -> None:
        """Blocking variant of start(); returns after stop() or Ctrl+C."""
        self._run()

    # -- internals ---------------------------------------------------------
    def _emit(self, events: list[Event]) -> None:
        for ev in events:
            try:
                self._on_event(ev)
            except Exception:  # never let a bad handler kill the reader
                log.exception("event handler failed for %r", ev)

    def _open_all(self) -> list:
        handles = []
        for info in find_vendor_interfaces():
            dev = hid.device()
            try:
                dev.open_path(info["path"])
            except OSError:
                continue
            try:
                dev.get_indexed_string(INIT_STRING_INDEX)
            except Exception:
                pass  # not fatal; some interfaces/instances don't answer
            dev.set_nonblocking(True)
            handles.append(dev)
        return handles

    def _run(self) -> None:
        if hid is None:
            self._on_status("hidapi missing")
            return
        waiting_reported = False
        while not self._stop.is_set():
            handles = self._open_all()
            if not handles:
                if not waiting_reported:
                    self._on_status("waiting for device")
                    waiting_reported = True
                self._stop.wait(self._retry)
                continue
            waiting_reported = False
            self._on_status("connected")
            try:
                self._read_loop(handles)
            finally:
                for h in handles:
                    try:
                        h.close()
                    except Exception:
                        pass
                self._emit(self._decoder.reset())
            if not self._stop.is_set():
                self._on_status("disconnected")
                self._stop.wait(self._retry)

    def _read_loop(self, handles: list) -> None:
        """Read every interface, but decode only one: the first to send a key or dial report.

        Windows can list the KD100 twice (two instances of its vendor interface), and both may carry the
        same reports. Decoding both, interleaved, turned a quick tap into press / release / press and
        broke chords (two keys held). The others are still read, so nothing old piles up in them, and
        if the one followed goes quiet while another carries on (the KD100 moved to the other
        connection), that one is followed instead.
        """
        live = list(handles)
        active = None
        last_heard: dict[int, float] = {}
        while live and not self._stop.is_set():
            got_any = False
            order = sorted(live, key=lambda handle: handle is not active)  # (the one followed first: see above)
            for h in order:
                try:
                    data = h.read(64)
                except OSError:
                    # Unreadable instance (e.g. a stale duplicate) or unplugged.
                    live.remove(h)
                    if h is active:
                        active = None
                        self._emit(self._decoder.reset())
                    continue
                if not data:
                    continue
                got_any = True
                if not is_input_report(data):
                    continue
                now = time.monotonic()
                last_heard[id(h)] = now
                if active is not None and h is not active and now - last_heard.get(id(active), 0.0) > SWITCH_AFTER:
                    active = None
                    self._emit(self._decoder.reset())
                if active is None:
                    active = h
                if h is active:
                    self._emit(self._decoder.feed(data))
            if not got_any:
                time.sleep(0.002)
