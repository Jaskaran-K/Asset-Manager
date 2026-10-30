"""View modes of the main window: Asset, Artbook, Split and Separate.

The asset browser always stays in the main window; only the Artbook panel moves:

- Asset View:    the asset panel only
- Artbook View:  the Artbook panel only
- Split View:    both, side by side in a splitter (assets left); the divider is remembered
- Separate View: assets in the main window, the Artbook in its own window (ui.artbook_window:
                 always on top, opacity, click-through). Closing that window goes back to
                 Asset View; quitting the app closes both.

The window's shared controls (the View button and Settings) sit at the right end of the
rightmost toolbar in the main window. The separate Artbook window gets its own View button,
and a Window button for its options.
The mode is saved, so the app reopens in it.
"""

from __future__ import annotations

import base64
from typing import Callable, Optional

from PySide6.QtCore import QByteArray, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QMenu, QSplitter, QToolButton, QWidget

from core.settings import VIEW_MODES, get_view_state, set_view_state
from ui import shortcuts
from ui.artbook_window import CLICK_THROUGH_SHORTCUT, ArtbookWindow, GlobalHotkey, WindowMenu, WindowOptionsButton
from ui.duotone import set_icon

# mode: (menu label, icon, shortcut id)
MODES: dict[str, tuple[str, str, str]] = {
    "asset": ("Asset View", "view_assets", "view.asset"),
    "artbook": ("Artbook View", "view_artbook", "view.artbook"),
    "split": ("Split View", "view_split", "view.split"),
    "separate": ("Separate View", "view_separate", "view.separate"),
}
assert tuple(MODES) == VIEW_MODES


class ViewSwitchButton(QToolButton):
    """Split button: click toggles Asset / Artbook view, the arrow lists every view."""

    def __init__(self, modes: "ViewModes", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.modes = modes
        self.setObjectName("splitBtn")
        self.setText("View")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        for action in modes.actions.values():
            menu.addAction(action)
        self.setMenu(menu)
        self.clicked.connect(self._on_clicked)
        modes.mode_changed.connect(self.show_mode)
        self.show_mode(modes.mode)

    def _on_clicked(self) -> None:
        self.modes.set_mode("artbook" if self.modes.mode == "asset" else "asset")

    def show_mode(self, mode: str) -> None:
        label, icon, _shortcut = MODES[mode]
        set_icon(self, icon, size=16)
        target = "Artbook" if mode == "asset" else "Asset"
        self.setToolTip(
            f"{label}. Click for {target} View; the arrow lists every view (Ctrl+1 to Ctrl+4)"
        )


class ViewModes(QObject):
    """Switches the main window between the four views (see the module docstring)."""

    mode_changed = Signal(str)

    def __init__(
        self,
        window: QMainWindow,
        splitter: QSplitter,
        asset_panel: QWidget,
        artbook_panel: QWidget,
        asset_slot: QHBoxLayout,
        artbook_slot: QHBoxLayout,
        controls: Callable[["ViewModes"], QWidget],
    ):
        super().__init__(window)
        self.window = window
        self.splitter = splitter
        self.asset_panel = asset_panel
        self.artbook_panel = artbook_panel
        self.asset_slot = asset_slot
        self.artbook_slot = artbook_slot
        self.mode = "asset"
        self.separate_window: Optional[ArtbookWindow] = None
        self._separate_switch: Optional[ViewSwitchButton] = None
        self._window_options: Optional[WindowOptionsButton] = None
        # Separate View only: turns the Artbook window's click-through on / off from any app
        self.hotkey = GlobalHotkey(CLICK_THROUGH_SHORTCUT, self._on_click_through_hotkey)

        self.actions: dict[str, QAction] = {}
        group = QActionGroup(self)
        group.setExclusive(True)
        for mode, (label, icon, shortcut_id) in MODES.items():
            action = QAction(label, self)
            action.setCheckable(True)
            action.setShortcut(shortcuts.sequence(shortcut_id))
            action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)  # from either window
            action.setToolTip(shortcuts.get(shortcut_id).action)
            set_icon(action, icon)
            action.triggered.connect(self._on_action)
            group.addAction(action)
            window.addAction(action)
            self.actions[mode] = action

        # The shared View / Settings buttons, built once the actions exist
        self.controls = controls(self)

        self._save_split = QTimer(self)
        self._save_split.setSingleShot(True)
        self._save_split.setInterval(400)
        self._save_split.timeout.connect(self._store_split)
        splitter.splitterMoved.connect(self._on_splitter_moved)
        self.set_mode("asset", save=False)

    # ------------------ switching ------------------
    def _on_action(self) -> None:
        for mode, action in self.actions.items():
            if action is self.sender():
                self.set_mode(mode)

    def restore(self) -> None:
        """Back to the view the app was left in (call once the main window is shown)."""
        self.set_mode(get_view_state()["mode"], save=False)

    def set_mode(self, mode: str, save: bool = True) -> None:
        if mode not in MODES:
            mode = "asset"
        leaving_separate = self.mode == "separate" and mode != "separate"
        self.mode = mode
        if leaving_separate:
            self._dock_artbook()
        if mode == "separate":
            self._undock_artbook()

        self.asset_panel.setVisible(mode != "artbook")
        self.artbook_panel.setVisible(mode != "asset")
        if mode == "split":
            self._apply_split()
            # Again once the just-shown panel is laid out: the splitter resizes it to its own taste first
            QTimer.singleShot(0, self._apply_split)
        # Shared controls at the top right of the main window
        (self.artbook_slot if mode in ("artbook", "split") else self.asset_slot).addWidget(self.controls)

        self.actions[mode].setChecked(True)
        if save:
            set_view_state(mode=mode)
        self.mode_changed.emit(mode)

    # ------------------ split ------------------
    def _apply_split(self) -> None:
        if self.mode != "split":
            return
        total = max(2, self.splitter.width())
        share = get_view_state()["split"]
        self.splitter.setSizes([round(total * share), total - round(total * share)])

    def _on_splitter_moved(self, *_args) -> None:
        self._save_split.start()

    def _store_split(self) -> None:
        if self.mode == "split":
            left, right = self.splitter.sizes()
            if left + right > 0:
                set_view_state(split=left / (left + right))

    # ------------------ separate window ------------------
    def _undock_artbook(self) -> None:
        if self.separate_window is None:
            self.separate_window = ArtbookWindow(self.window.windowTitle())
            self.separate_window.closed_by_user.connect(self._on_separate_closed)
            for action in self.actions.values():
                self.separate_window.addAction(action)
        win = self.separate_window
        geometry = get_view_state()["window"]
        if geometry:
            win.restoreGeometry(QByteArray(base64.b64decode(geometry)))
        win.setCentralWidget(self.artbook_panel)
        win.hotkey_works = self.hotkey.register()
        if self._window_options is None:
            self._window_options = WindowOptionsButton(win)
        self._window_options.sync()
        if self._separate_switch is None:
            self._separate_switch = ViewSwitchButton(self)
        for button in (self._window_options, self._separate_switch):
            self.artbook_slot.addWidget(button)
            button.show()
        self.artbook_panel.board.window_menu = self._board_window_menu  # the options on right-click too
        self.artbook_panel.show()
        win.show()

    def _dock_artbook(self) -> None:
        win = self.separate_window
        if win is None:
            return
        self._store_window_geometry()
        win.set_click_through(False)
        self.hotkey.unregister()
        self.artbook_panel.board.window_menu = None
        for button in (self._window_options, self._separate_switch):
            if button is not None:
                button.hide()
        win.takeCentralWidget()
        self.splitter.insertWidget(1, self.artbook_panel)
        win.quitting = True  # closing it here is part of switching, not the user leaving the view
        win.close()
        win.quitting = False

    def _on_separate_closed(self) -> None:
        self.set_mode("asset")

    def _board_window_menu(self, parent: QMenu) -> WindowMenu:
        return WindowMenu(self.separate_window, parent)

    def _on_click_through_hotkey(self) -> None:
        if self.mode == "separate" and self.separate_window is not None:
            self.separate_window.toggle_click_through()

    def _store_window_geometry(self) -> None:
        if self.separate_window is not None:
            data = bytes(self.separate_window.saveGeometry().data())
            set_view_state(window=base64.b64encode(data).decode("ascii"))

    def shutdown(self) -> None:
        """The app is quitting: close the separate window too, keeping the mode for next time."""
        self.hotkey.unregister()
        if self.separate_window is not None and self.mode == "separate":
            self._store_window_geometry()
            self.separate_window.quitting = True
            self.separate_window.close()
