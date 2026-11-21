"""Decode raw HID reports from the Huion KD100 vendor interface (usage page 0xFF00).

Report layout (12 bytes, captured on Windows with the receiver):

    Buttons:  08 e0 01 01 B0 B1 B2 00 00 00 00 00
        B0..B2 form a little-endian bitmask:
            bit 0..17  -> keys 1..18 (numbered left-to-right, top-to-bottom)
            bit 18     -> dial (center) button
        All bits zero = everything released.

    Dial:     08 f1 01 01 00 D  00 00 00 00 00 00
        D = 0x01 -> one detent clockwise
        D = 0x02 -> one detent counter-clockwise

This module is pure (no I/O) so it can be unit-tested without hardware.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Union

NUM_KEYS = 18
DIAL_BUTTON = "DIAL"

REPORT_ID = 0x08
TYPE_BUTTONS = 0xE0
TYPE_DIAL = 0xF1

_DIAL_BUTTON_BIT = 18
_ALL_BUTTON_BITS = (1 << (NUM_KEYS + 1)) - 1  # keys + dial button


def key_name(index: int) -> str:
    """1-based key index -> stable name used in config files ("K1".."K18")."""
    return f"K{index}"


ALL_BUTTONS: tuple[str, ...] = tuple(key_name(i) for i in range(1, NUM_KEYS + 1)) + (DIAL_BUTTON,)


@dataclass(frozen=True)
class ButtonEvent:
    button: str  # "K1".."K18" or "DIAL"
    pressed: bool


@dataclass(frozen=True)
class DialEvent:
    step: int  # +1 clockwise, -1 counter-clockwise


Event = Union[ButtonEvent, DialEvent]


def _bit_to_button(bit: int) -> str:
    return DIAL_BUTTON if bit == _DIAL_BUTTON_BIT else key_name(bit + 1)


def is_input_report(report) -> bool:
    """A key or dial report (not some other report an interface may send)."""
    data = bytes(report)
    return len(data) >= 7 and data[0] == REPORT_ID and data[1] in (TYPE_BUTTONS, TYPE_DIAL)


class ReportDecoder:
    """Turns a stream of raw reports into press/release/turn events.

    The KD100 reports one key at a time (seen on the user's device): holding K2 and pressing K3 sends
    "K3" alone, letting go of K3 sends "K2" again (while held, a key's report also repeats every ~0.1 s),
    and "none" only once every key is up. So a new single key means one more key held, not that the
    one before was let go: going back to an earlier key lets go of the keys pressed after it, and
    "none" lets go of all. That makes chords work (a key holding Ctrl, then another for A).

    Should a report ever carry several keys at once, it's taken as exactly what's held from then on.
    """

    def __init__(self) -> None:
        self._mask = 0
        self._order: list[int] = []  # (held keys' bits, in the order they were pressed)
        self._exact = False  # (True once a report has carried several keys)

    @property
    def held(self) -> frozenset[str]:
        return frozenset(_bit_to_button(b) for b in range(NUM_KEYS + 1) if self._mask >> b & 1)

    def reset(self) -> list[Event]:
        """Release everything (e.g. on disconnect). Returns the release events."""
        return self._release_from(0)

    def feed(self, report: Iterable[int]) -> list[Event]:
        data = bytes(report)
        if len(data) < 7 or data[0] != REPORT_ID:
            return []
        kind = data[1]
        if kind == TYPE_BUTTONS:
            mask = int.from_bytes(data[4:7], "little") & _ALL_BUTTON_BITS
            if mask & (mask - 1):  # (several keys in one report)
                self._exact = True
            if mask == 0:
                return self._release_from(0)
            if self._exact:
                events = self._diff(mask)
                self._mask = mask
                self._order = [b for b in self._order if mask >> b & 1] + [
                    b for b in range(NUM_KEYS + 1) if mask >> b & 1 and b not in self._order]
                return events
            bit = mask.bit_length() - 1
            if bit in self._order:  # (back to a key still held: the ones pressed after it were let go)
                return self._release_from(self._order.index(bit) + 1)
            self._order.append(bit)
            self._mask |= 1 << bit
            return [ButtonEvent(_bit_to_button(bit), True)]
        if kind == TYPE_DIAL:
            d = data[5]
            if d == 0x01:
                return [DialEvent(+1)]
            if d == 0x02:
                return [DialEvent(-1)]
        return []

    def _release_from(self, index: int) -> list[Event]:
        """Let go of the held keys from the index-th pressed on, the last pressed first."""
        gone = self._order[index:]
        self._order = self._order[:index]
        for bit in gone:
            self._mask &= ~(1 << bit)
        return [ButtonEvent(_bit_to_button(bit), False) for bit in reversed(gone)]

    def _diff(self, new_mask: int) -> list[Event]:
        changed = self._mask ^ new_mask
        events: list[Event] = []
        for bit in range(NUM_KEYS + 1):
            if changed >> bit & 1:
                events.append(ButtonEvent(_bit_to_button(bit), bool(new_mask >> bit & 1)))
        return events
