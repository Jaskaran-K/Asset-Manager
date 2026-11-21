"""Decoder tests using reports captured from a real KD100."""
import unittest

from core.keydial.protocol import ButtonEvent, DialEvent, ReportDecoder, key_name


def r(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr)


RELEASE = r("08 e0 01 01 00 00 00 00 00 00 00 00")

# (press report, expected button) in the order they were pressed on the device
CAPTURED_PRESSES = [
    ("08 e0 01 01 01 00 00 00 00 00 00 00", "K1"),
    ("08 e0 01 01 02 00 00 00 00 00 00 00", "K2"),
    ("08 e0 01 01 04 00 00 00 00 00 00 00", "K3"),
    ("08 e0 01 01 08 00 00 00 00 00 00 00", "K4"),
    ("08 e0 01 01 10 00 00 00 00 00 00 00", "K5"),
    ("08 e0 01 01 20 00 00 00 00 00 00 00", "K6"),
    ("08 e0 01 01 40 00 00 00 00 00 00 00", "K7"),
    ("08 e0 01 01 80 00 00 00 00 00 00 00", "K8"),
    ("08 e0 01 01 00 01 00 00 00 00 00 00", "K9"),
    ("08 e0 01 01 00 02 00 00 00 00 00 00", "K10"),
    ("08 e0 01 01 00 04 00 00 00 00 00 00", "K11"),
    ("08 e0 01 01 00 08 00 00 00 00 00 00", "K12"),
    ("08 e0 01 01 00 10 00 00 00 00 00 00", "K13"),
    ("08 e0 01 01 00 20 00 00 00 00 00 00", "K14"),
    ("08 e0 01 01 00 40 00 00 00 00 00 00", "K15"),
    ("08 e0 01 01 00 80 00 00 00 00 00 00", "K16"),
    ("08 e0 01 01 00 00 01 00 00 00 00 00", "K17"),
    ("08 e0 01 01 00 00 02 00 00 00 00 00", "K18"),
    ("08 e0 01 01 00 00 04 00 00 00 00 00", "DIAL"),
]


class DecoderTests(unittest.TestCase):
    def test_every_captured_key(self):
        dec = ReportDecoder()
        for hexstr, name in CAPTURED_PRESSES:
            self.assertEqual(dec.feed(r(hexstr)), [ButtonEvent(name, True)], name)
            self.assertEqual(dec.feed(RELEASE), [ButtonEvent(name, False)], name)

    def test_dial(self):
        dec = ReportDecoder()
        self.assertEqual(dec.feed(r("08 f1 01 01 00 01 00 00 00 00 00 00")), [DialEvent(+1)])
        self.assertEqual(dec.feed(r("08 f1 01 01 00 02 00 00 00 00 00 00")), [DialEvent(-1)])

    def test_chord(self):
        dec = ReportDecoder()
        self.assertEqual(dec.feed(r("08 e0 01 01 01 00 00 00 00 00 00 00")), [ButtonEvent("K1", True)])
        self.assertEqual(dec.feed(r("08 e0 01 01 03 00 00 00 00 00 00 00")), [ButtonEvent("K2", True)])
        self.assertEqual(dec.held, {"K1", "K2"})
        self.assertEqual(dec.feed(r("08 e0 01 01 02 00 00 00 00 00 00 00")), [ButtonEvent("K1", False)])
        self.assertEqual(dec.reset(), [ButtonEvent("K2", False)])

    def test_one_key_at_a_time_chord(self):
        """The user's KD100 (2026-11-21): hold K2, tap K3, let go of K2. It reports one key at a time, and
        repeats a held key's report: K2 K2 K2, K3 K3, K2, none."""
        dec = ReportDecoder()
        k2, k3 = r("08 e0 01 01 02 00 00 00 00 00 00 00"), r("08 e0 01 01 04 00 00 00 00 00 00 00")
        events = []
        for report in (k2, k2, k2, k3, k3, k2, RELEASE):
            events += dec.feed(report)
        self.assertEqual(events, [ButtonEvent("K2", True), ButtonEvent("K3", True), ButtonEvent("K3", False),
                                  ButtonEvent("K2", False)])

    def test_one_key_at_a_time_lets_go_of_all_on_none(self):
        dec = ReportDecoder()
        dec.feed(r("08 e0 01 01 02 00 00 00 00 00 00 00"))
        dec.feed(r("08 e0 01 01 04 00 00 00 00 00 00 00"))
        dec.feed(r("08 e0 01 01 08 00 00 00 00 00 00 00"))
        self.assertEqual(dec.held, {"K2", "K3", "K4"})
        self.assertEqual(dec.feed(RELEASE), [ButtonEvent("K4", False), ButtonEvent("K3", False),
                                             ButtonEvent("K2", False)])

    def test_ignores_garbage(self):
        dec = ReportDecoder()
        self.assertEqual(dec.feed(b""), [])
        self.assertEqual(dec.feed(r("01 02 03 04 05 06 07")), [])
        self.assertEqual(dec.feed(r("08 aa 01 01 ff ff ff 00")), [])

    def test_key_names(self):
        self.assertEqual(key_name(1), "K1")
        self.assertEqual(key_name(18), "K18")


if __name__ == "__main__":
    unittest.main()
