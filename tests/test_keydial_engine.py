"""Tests for key parsing, action/config parsing and the engine (no hardware, no real input)."""
import json
import unittest

from core.keydial.actions import (
    ConfigError, Delay, DialMode, Hold, Keys, Launch, Macro, Scroll, Text, parse_action,
)
from core.keydial.engine import Engine
from core.keydial.keys import ComboError, parse_combo
from core.keydial.profiles import load_config
from core.keydial.protocol import ButtonEvent, DialEvent


class FakeOutput:
    def __init__(self):
        self.log = []

    def press_combo(self, c): self.log.append(("press", c))
    def release_combo(self, c): self.log.append(("release", c))
    def tap_combo(self, c): self.log.append(("tap", c))
    def type_text(self, t): self.log.append(("text", t))
    def scroll(self, dx, dy, modifiers=()): self.log.append(("scroll", dx, dy, modifiers))
    def launch(self, target, args=()): self.log.append(("launch", target, args))


class KeyTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_combo("Ctrl+Shift+Z"), ("ctrl", "shift", "z"))
        self.assertEqual(parse_combo("z+ctrl"), ("ctrl", "z"))
        self.assertEqual(parse_combo("control+plus"), ("ctrl", "plus"))
        self.assertEqual(parse_combo("[" ), ("[",))
        self.assertEqual(parse_combo("PgUp"), ("page_up",))
        self.assertEqual(parse_combo("f13"), ("f13",))

    def test_errors(self):
        for bad in ("", "ctrl+", "ctrl++", "ctrl+banana"):
            with self.assertRaises(ComboError, msg=bad):
                parse_combo(bad)


class ActionTests(unittest.TestCase):
    def test_forms(self):
        self.assertIsNone(parse_action(None))
        self.assertIsNone(parse_action("none"))
        self.assertEqual(parse_action("ctrl+z"), Keys(("ctrl", "z")))
        self.assertEqual(parse_action({"hold": "space"}), Hold(("space",)))
        self.assertEqual(parse_action({"text": "hi"}), Text("hi"))
        self.assertEqual(parse_action({"scroll": 2, "with": "ctrl"}), Scroll(dy=2, modifiers=("ctrl",)))
        self.assertEqual(parse_action({"hscroll": -1}), Scroll(dx=-1))
        self.assertEqual(parse_action({"launch": "calc.exe"}), Launch("calc.exe"))
        self.assertEqual(parse_action({"dial_mode": "next"}), DialMode("next"))
        self.assertEqual(
            parse_action({"macro": ["ctrl+a", {"delay": 20}, {"text": "x"}]}),
            Macro((Keys(("ctrl", "a")), Delay(20), Text("x"))),
        )

    def test_errors(self):
        bad = [
            {"keys": "ctrl+z", "text": "x"},
            {"bogus": 1},
            {"macro": []},
            {"macro": [{"hold": "ctrl"}]},
            {"delay": -5},
            {"scroll": "lots"},
            {"keys": "ctrl+z", "extra": 1},
            42,
        ]
        for spec in bad:
            with self.assertRaises(ConfigError, msg=repr(spec)):
                parse_action(spec)


CONFIG = {
    "profiles": [
        {
            "name": "Default",
            "buttons": {
                "K1": "ctrl+z",
                "K2": {"hold": "space"},
                "K3": {"macro": ["ctrl+a", {"delay": 10}, "ctrl+c"]},
                "DIAL": {"dial_mode": "next"},
            },
            "dial": [
                {"name": "Volume", "cw": "volume_up", "ccw": "volume_down"},
                {"name": "Zoom", "cw": {"scroll": 1, "with": "ctrl"}, "ccw": {"scroll": -1, "with": "ctrl"}},
            ],
        },
        {"name": "Krita", "apps": ["Krita.exe"], "inherits": "Default", "buttons": {"K1": "e"}},
    ]
}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(CONFIG)
        self.out = FakeOutput()
        self.exe = "explorer.exe"
        self.msgs = []
        self.sleeps = []
        self.engine = Engine(
            lambda: self.cfg, self.out, foreground=lambda: self.exe,
            notify=self.msgs.append, sleep=self.sleeps.append,
        )

    def tap(self, btn):
        self.engine.process(ButtonEvent(btn, True))
        self.engine.process(ButtonEvent(btn, False))

    def test_tap_and_macro(self):
        self.tap("K1")
        self.tap("K3")
        self.assertEqual(self.out.log, [("tap", ("ctrl", "z")), ("tap", ("ctrl", "a")), ("tap", ("ctrl", "c"))])
        self.assertEqual(self.sleeps, [0.01])

    def test_hold(self):
        self.engine.process(ButtonEvent("K2", True))
        self.assertEqual(self.out.log, [("press", ("space",))])
        self.exe = "krita.exe"  # focus changes while held: release must still match
        self.engine.process(ButtonEvent("K2", False))
        self.assertEqual(self.out.log[-1], ("release", ("space",)))

    def test_release_all(self):
        self.engine.process(ButtonEvent("K2", True))
        self.engine.release_all()
        self.assertEqual(self.out.log[-1], ("release", ("space",)))

    def test_app_profile_and_inheritance(self):
        self.exe = "krita.exe"
        self.tap("K1")  # overridden
        self.tap("K3")  # inherited
        self.assertEqual(self.out.log[0], ("tap", ("e",)))
        self.assertEqual(self.out.log[1], ("tap", ("ctrl", "a")))
        self.assertIn("profile: Krita  (dial: Volume)", self.msgs)

    def test_dial_modes(self):
        self.engine.process(DialEvent(+1))
        self.tap("DIAL")
        self.engine.process(DialEvent(-1))
        self.tap("DIAL")  # wraps back to Volume
        self.engine.process(DialEvent(-1))
        self.assertEqual(self.out.log, [
            ("tap", ("volume_up",)),
            ("scroll", 0, -1, ("ctrl",)),
            ("tap", ("volume_down",)),
        ])
        self.assertIn("Default: dial -> Zoom", self.msgs)

    def test_dial_mode_per_profile(self):
        self.tap("DIAL")  # Default -> Zoom
        self.exe = "krita.exe"
        self.engine.process(DialEvent(+1))  # Krita still on Volume
        self.assertEqual(self.out.log[-1], ("tap", ("volume_up",)))


class ConfigTests(unittest.TestCase):
    def test_starter_profiles_are_valid(self):
        from core.keydial import presets

        cfg = load_config(presets.starter_document())
        self.assertEqual(cfg.profile_for("MAYA.EXE").name, "Maya")
        self.assertEqual(cfg.profile_for("vlc.exe").name, "Multimedia")
        self.assertEqual(cfg.profile_for("unknown.exe").name, "Default")
        self.assertEqual(cfg.profile_for(None).name, "Default")

    def test_errors(self):
        bad = [
            {},
            {"profiles": [{"name": "Other"}]},
            {"profiles": [{"name": "Default", "buttons": {"K19": "a"}}]},
            {"profiles": [{"name": "Default"}, {"name": "X", "inherits": "Nope"}]},
            {"profiles": [{"name": "Default"}, {"name": "Default"}]},
            {"profiles": [{"name": "Default", "dial": [{"name": "M", "cw": {"hold": "a"}}]}]},
        ]
        for data in bad:
            with self.assertRaises(ConfigError, msg=json.dumps(data)):
                load_config(data)


if __name__ == "__main__":
    unittest.main()
