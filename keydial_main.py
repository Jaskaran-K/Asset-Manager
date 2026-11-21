"""KeyDial: the Huion KD100's keys and dial, a tray app shipped with Asset Manager (ui/keydial_app.py).

    python keydial_main.py          the tray and the editor
    python keydial_main.py --tray   the tray only (how Windows starts it)
"""

import sys

from ui.keydial_app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
