"""Asset / Artbook / Split / Separate views of the main window (ui.view_modes)."""

from __future__ import annotations

import time

import pytest

from core.settings import get_view_state, set_view_state


def pump(qapp, seconds=0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


@pytest.fixture
def window(qapp):
    import main

    win = main.MainWindow()
    win.resize(1600, 800)  # room for both toolbars side by side, in any theme
    win.show()
    pump(qapp)
    yield win
    win.close()
    pump(qapp)


def controls_in(win, panel):
    return panel.isAncestorOf(win.btn_settings) and panel.isAncestorOf(win.btn_view)


def test_starts_in_asset_view(window):
    assert window.view_modes.mode == "asset"
    assert window.asset_panel.isVisible() and not window.artbook_panel.isVisible()
    assert controls_in(window, window.asset_panel)


def test_artbook_view_moves_the_shared_controls(window, qapp):
    window.view_modes.actions["artbook"].trigger()  # Ctrl+2
    pump(qapp)
    assert not window.asset_panel.isVisible() and window.artbook_panel.isVisible()
    assert controls_in(window, window.artbook_panel)
    assert get_view_state()["mode"] == "artbook"
    window.btn_view.click()  # the main part toggles back
    assert window.view_modes.mode == "asset"


def test_split_view_is_side_by_side_and_remembers_the_divider(window, qapp):
    window.view_modes.set_mode("split")
    pump(qapp)
    left, right = window.view_splitter.sizes()
    assert window.asset_panel.isVisible() and window.artbook_panel.isVisible()
    assert abs(left - right) <= 4  # 50/50
    assert controls_in(window, window.artbook_panel)  # top right of the window
    total = left + right
    window.view_splitter.setSizes([round(total * 0.65), total - round(total * 0.65)])
    window.view_modes._store_split()
    assert abs(get_view_state()["split"] - 0.65) < 0.02
    window.view_modes.set_mode("asset")
    window.view_modes.set_mode("split")
    pump(qapp)
    left, right = window.view_splitter.sizes()
    assert abs(left / (left + right) - 0.65) < 0.02


def test_separate_view_opens_the_artbook_in_its_own_window(window, qapp):
    modes = window.view_modes
    modes.set_mode("separate")
    pump(qapp)
    separate = modes.separate_window
    assert separate.isVisible() and window.artbook_panel.window() is separate
    assert controls_in(window, window.asset_panel)
    assert window.artbook_panel.isAncestorOf(modes._separate_switch)  # its own View button

    separate.close()  # closing it goes back to Asset View
    pump(qapp)
    assert modes.mode == "asset" and get_view_state()["mode"] == "asset"
    assert window.artbook_panel.window() is window
    assert window.view_splitter.indexOf(window.artbook_panel) == 1

    modes.set_mode("separate")
    modes.set_mode("split")  # docks it again
    pump(qapp)
    assert not separate.isVisible() and window.artbook_panel.window() is window


def test_quitting_in_separate_view_closes_both_and_reopens_there(qapp):
    import main

    win = main.MainWindow()
    win.show()
    pump(qapp)
    win.view_modes.set_mode("separate")
    pump(qapp)
    separate = win.view_modes.separate_window
    win.close()
    pump(qapp)
    assert not separate.isVisible()
    assert get_view_state()["mode"] == "separate"  # quitting isn't leaving the view
    assert get_view_state()["window"]  # its position and size are kept

    again = main.MainWindow()
    again.show()
    pump(qapp, 0.4)
    assert again.view_modes.mode == "separate" and again.view_modes.separate_window.isVisible()
    again.close()
    pump(qapp)


def test_narrow_asset_side_gets_a_compact_toolbar(window, qapp):
    window.view_modes.set_mode("split")
    pump(qapp)
    total = sum(window.view_splitter.sizes())
    window.view_splitter.setSizes([1, total - 1])  # as narrow as it goes
    pump(qapp)
    assert window.asset_toolbar.compact
    assert window.btn_refresh.text() == "" and window.btn_refresh.toolTip()  # icon only, tooltip kept
    assert window.asset_panel.width() >= window.asset_panel.minimumWidth() > 0  # not squashed past compact
    window.view_modes.set_mode("asset")
    pump(qapp)
    assert not window.asset_toolbar.compact and window.btn_refresh.text() == "Refresh"


def test_view_state_setting_is_validated(isolated_user_data):
    assert get_view_state() == {"mode": "asset", "split": 0.5, "window": ""}
    set_view_state(mode="nope", split=5)
    assert get_view_state()["mode"] == "asset" and get_view_state()["split"] == 0.8
