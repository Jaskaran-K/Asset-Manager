"""Phase 3: the separate Artbook window's options (always on top, opacity, click-through and
its way out: the global hotkey and the tray icon), and Send to Artbook from the asset grid."""

from __future__ import annotations

import ctypes
import sys
import time

import pytest
from PIL import Image
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QMenu

from core import artbook_store as store
from core.settings import get_artbook_window_state, set_artbook_window_state, set_scan_folders
from ui import shortcuts
from ui.animations import set_motion_mode
from ui.artbook_window import show_click_through_tip as real_heads_up  # before the fixture replaces it


def pump(qapp, condition=lambda: False, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture(autouse=True)
def no_motion():
    set_motion_mode("off")
    yield
    set_motion_mode("on")


@pytest.fixture(autouse=True)
def heads_up(monkeypatch):
    """The click-through heads-up, answered without a dialog: the answers given (whether
    "Don't show again" is ticked) are taken in order; each showing is recorded."""
    from ui import artbook_window

    shown, answers = [], []

    def fake(_parent, _hotkey_works):
        shown.append(True)
        return answers.pop(0) if answers else False

    monkeypatch.setattr(artbook_window, "show_click_through_tip", fake)
    return shown, answers


@pytest.fixture
def window(qapp):
    from ui.artbook_window import ArtbookWindow

    win = ArtbookWindow("Test")
    win.show()
    qapp.processEvents()
    yield win
    win.quitting = True
    win.close()


def flag(win, flag_name):
    return bool(win.windowFlags() & getattr(Qt.WindowType, flag_name))


# ----------------------------------------------------------------------
# Window options
# ----------------------------------------------------------------------
def test_always_on_top_and_opacity_are_remembered(qapp, window):
    from ui.artbook_window import ArtbookWindow

    window.set_on_top(True)
    window.set_opacity(55)
    assert flag(window, "WindowStaysOnTopHint") and window.isVisible()
    assert window.opacity_percent() == 55
    assert get_artbook_window_state() == {"on_top": True, "opacity": 55, "click_through_tip": True}
    window.set_opacity(5)  # too faint to find again: kept at the minimum
    assert window.opacity_percent() == 20

    again = ArtbookWindow("Test")  # next launch
    assert again.on_top and again.opacity_percent() == 20 and flag(again, "WindowStaysOnTopHint")
    assert not again.click_through  # never remembered


def test_click_through_ignores_the_mouse_and_stays_on_top(qapp, window):
    window.set_click_through(True)
    assert flag(window, "WindowTransparentForInput") and flag(window, "WindowStaysOnTopHint")
    assert window.isVisible()
    window.set_click_through(False)
    assert not flag(window, "WindowTransparentForInput")
    assert not flag(window, "WindowStaysOnTopHint")  # back to the user's choice (off)
    assert window.isVisible()


def test_closing_the_window_turns_click_through_off(qapp, window):
    window.set_click_through(True)
    window.quitting = True
    window.close()
    assert not window.click_through


def test_settings_are_validated(isolated_user_data):
    set_artbook_window_state(on_top="yes", opacity=500)
    assert get_artbook_window_state() == {"on_top": True, "opacity": 100, "click_through_tip": True}


def test_the_real_heads_up_dialog(qapp, window):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    def answer():
        box = QApplication.activeModalWidget()
        if not isinstance(box, QMessageBox):
            QTimer.singleShot(50, answer)
            return
        box.checkBox().setChecked(True)  # crashed while the check box was freed by Python early
        box.button(QMessageBox.StandardButton.Ok).click()

    QTimer.singleShot(50, answer)
    assert real_heads_up(window, True) is True


def test_heads_up_before_click_through_until_dont_show_again(qapp, window, heads_up):
    shown, answers = heads_up
    window.set_click_through(True)
    assert shown == [True] and window.click_through  # explained, then on
    window.set_click_through(False)
    assert shown == [True]  # not when turning it off

    answers.append(True)  # "Don't show again" ticked
    window.set_click_through(True)
    assert len(shown) == 2 and not get_artbook_window_state()["click_through_tip"]
    window.set_click_through(False)
    window.set_click_through(True)
    assert len(shown) == 2  # never again


def test_window_menu_shows_and_changes_the_options(qapp, window):
    from ui.artbook_window import WindowOptionsButton

    button = WindowOptionsButton(window)
    menu = button.window_menu
    assert button.toolTip()
    menu.action_on_top.trigger()
    assert window.on_top
    menu.slider.setValue(40)
    assert window.opacity_percent() == 40 and get_artbook_window_state()["opacity"] == 40
    menu.action_click_through.trigger()
    assert window.click_through
    window.set_click_through(False)  # e.g. from the hotkey: the menu follows
    assert not menu.action_click_through.isChecked()
    assert shortcuts.get("view.click_through").keys in menu.action_click_through.text()


# ----------------------------------------------------------------------
# The global hotkey
# ----------------------------------------------------------------------
def test_hotkey_parts():
    from ui.artbook_window import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, hotkey_parts

    mods, key = hotkey_parts(shortcuts.sequence("view.click_through"))
    assert mods == MOD_CONTROL | MOD_ALT | MOD_NOREPEAT and key == ord("T")
    with pytest.raises(ValueError):
        hotkey_parts(shortcuts.sequence("viewer.fullscreen"))  # F11: not a letter or digit


@pytest.mark.skipif(sys.platform != "win32", reason="Windows hotkeys")
def test_hotkey_message_calls_back_on_the_ui_thread(qapp):
    from ctypes import wintypes

    from shiboken6.Shiboken import VoidPtr

    from ui.artbook_window import WM_HOTKEY, GlobalHotkey

    calls = []
    hotkey = GlobalHotkey("view.click_through", lambda: calls.append(True))
    hotkey.registered = True  # as if registered: only the message handling is under test
    # Qt passes the message as a shiboken VoidPtr (falsy: it has no size), not as an int
    msg = wintypes.MSG(message=WM_HOTKEY, wParam=GlobalHotkey.HOTKEY_ID)
    assert hotkey.nativeEventFilter(b"windows_generic_MSG", VoidPtr(ctypes.addressof(msg))) == (True, 0)
    other = wintypes.MSG(message=WM_HOTKEY, wParam=GlobalHotkey.HOTKEY_ID + 1)
    assert hotkey.nativeEventFilter(b"windows_generic_MSG", VoidPtr(ctypes.addressof(other))) == (False, 0)
    assert pump(qapp, lambda: calls, seconds=1) and calls == [True]  # once, after the native event


@pytest.fixture
def main_window(qapp, tmp_path):
    import main

    root = tmp_path / "library"
    root.mkdir()
    Image.new("RGB", (64, 32), "red").save(root / "red.png")
    Image.new("RGB", (32, 64), "blue").save(root / "blue.png")
    (root / "chair.fbx").write_bytes(b"fbx")
    set_scan_folders([root])
    win = main.MainWindow()
    win.resize(1600, 800)
    win.show()
    assert pump(qapp, lambda: win._scan_done_once and not win.scan_thread.isRunning())
    yield win
    win.close()
    pump(qapp, seconds=0.2)


def test_separate_view_has_the_window_button_and_the_hotkey_only_while_on(qapp, main_window):
    modes = main_window.view_modes
    modes.set_mode("separate")
    pump(qapp, seconds=0.2)
    win = modes.separate_window
    assert modes._window_options.isVisible()
    assert win.hotkey_works == modes.hotkey.registered
    modes._on_click_through_hotkey()  # what the hotkey does
    assert win.click_through
    modes._on_click_through_hotkey()
    assert not win.click_through

    win.set_click_through(True)
    modes.set_mode("asset")  # leaving Separate View: never left ignoring the mouse
    pump(qapp, seconds=0.2)
    assert not win.click_through and not modes.hotkey.registered
    assert not modes._window_options.isVisible()


def test_board_right_click_has_the_window_options_in_separate_view(qapp, main_window):
    from PySide6.QtCore import QPointF

    board = main_window.artbook_panel.board

    def submenus():
        menu = board.context_menu(QPointF(0, 0))
        found = {a.text(): a.menu() for a in menu.actions() if a.menu()}
        return menu, found

    menu, found = submenus()
    assert "Window" not in found  # only the separate window has these options
    menu.deleteLater()

    modes = main_window.view_modes
    modes.set_mode("separate")
    pump(qapp, seconds=0.2)
    menu, found = submenus()
    window_menu = found["Window"]
    window_menu.action_on_top.trigger()
    assert modes.separate_window.on_top
    assert modes._window_options.window_menu.action_on_top.isChecked()  # the toolbar's menu follows
    menu.deleteLater()

    modes.set_mode("asset")
    pump(qapp, seconds=0.2)
    menu, found = submenus()
    assert "Window" not in found
    menu.deleteLater()


# ----------------------------------------------------------------------
# Send to Artbook
# ----------------------------------------------------------------------
def select(window, *names):
    model, selection = window.grid_model, window.list_grid.selectionModel()
    selection.clearSelection()
    for row, asset in enumerate(model.assets()):
        if asset.name in names:
            selection.select(model.index(row), QItemSelectionModel.SelectionFlag.Select)


def menu_titles(menu):
    rows = []
    for action in menu.actions():
        if action.isSeparator():
            continue
        rows.append(action.text() + ("/" if action.menu() else ""))
        if action.menu():
            rows += ["  " + row for row in menu_titles(action.menu())]
    return rows


def test_send_to_artbook_lists_every_page_and_new_page(qapp, main_window):
    folder = store.create_folder("Refs")
    store.create_page("Inside", folder)
    store.create_page("Loose")
    select(main_window, "red.png", "chair.fbx")
    menu = QMenu()
    main_window._add_send_to_artbook_menu(menu)
    assert menu_titles(menu) == ["Send to Artbook/", "  New Page", "  Refs/", "    Inside", "  Loose"]

    select(main_window, "chair.fbx")  # no images: nothing to send
    menu = QMenu()
    main_window._add_send_to_artbook_menu(menu)
    (send,) = menu.actions()
    assert not send.isEnabled() and "images only" in send.text()


def test_sending_adds_the_images_to_the_chosen_page(qapp, main_window):
    other = store.create_page("Loose")
    library = main_window.grid_model.assets()
    images = sorted(a.path for a in library if a.path.suffix == ".png")
    main_window._send_to_artbook(images, other["id"])
    panel = main_window.artbook_panel
    assert panel.current_page_id() == other["id"]
    assert pump(qapp, lambda: len(panel.board.items_list()) == 2)
    assert '"Loose"' in main_window.status_label.text() and "Ctrl+2" in main_window.status_label.text()

    main_window._send_to_artbook(images[:1], None)  # a new page
    assert panel.page["name"] == "Page 1"  # the only other page is "Loose"
    assert panel.current_page_id() != other["id"]
    assert pump(qapp, lambda: len(panel.board.items_list()) == 1)
