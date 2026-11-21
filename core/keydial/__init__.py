r"""KeyDial: custom keys and dial for the Huion Mini KeyDial KD100 (a tray app of its own, shipped with
Asset Manager: keydial_main.py, ui/keydial_*.py).

No Qt here, so it runs and tests without a window:
    protocol    the device's raw reports -> press / release / dial-turn events
    device      reads the device in the background (raw-mode init, reconnects)
    keys        key names, combos, and the editor's keyboard capture / display
    actions     what a key can do (shortcut, hold, text, scroll, open, macro, dial mode)
    profiles    per-app profiles from profiles.json (reloaded when saved)
    engine      events -> the profile for the app in front -> actions, on its own thread
    output      sends keystrokes, wheel turns and launches to Windows
    foreground  which program is in front / running
    presets     the starter profiles
    store       where KeyDial's files live (%APPDATA%\KeyDial)
    autostart   starting with Windows, and running as administrator
"""
