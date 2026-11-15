"""The quieter look (November 2026): borderless bars, the selection bar, the regrouped right-click menu.

The bar limits here keep clutter from creeping back: a new button on the main bar goes in More, or on the
selection bar if it acts on the selected files."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QItemSelectionModel, QPoint
from PySide6.QtWidgets import QAbstractButton, QLineEdit, QMenu, QWidget

MAIN_BAR_LIMIT = 8  # controls on the main window's top bar (the search box counts as one)
MENU_TOP_LIMIT = 12  # entries at the top of a file's right-click menu
MODEL_TOOLS_LIMIT = 11  # buttons on the 3D viewer's floating tools
PDF_BAR_LIMIT = 11  # controls on the PDF's bar above the pages (Find's own arrows count)


def pump(qapp, condition=lambda: True, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def window(qapp, tmp_path):
    from core.settings import set_scan_folders
    from tests.test_main_window import open_window

    root = tmp_path / "Library"
    root.mkdir()
    for name, color in (("red.png", (200, 30, 30)), ("blue.png", (30, 30, 200)), ("green.jpg", (30, 160, 40))):
        Image.new("RGB", (64, 48), color).save(root / name)
    (root / "cube.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    set_scan_folders([root])
    win = open_window(qapp)
    yield win
    win.close()


def select(window, names):
    model, selection = window.grid_model, window.list_grid.selectionModel()
    selection.clearSelection()
    for row, asset in enumerate(model.assets()):
        if asset.name in names:
            selection.select(model.index(row), QItemSelectionModel.SelectionFlag.Select)


def bar_controls(window) -> list[QWidget]:
    bar = window.control_bar
    found = [w for w in bar.findChildren(QWidget) if isinstance(w, (QAbstractButton, QLineEdit)) and w.isVisibleTo(bar)
             and not window.search_bar.isAncestorOf(w)]
    return found + [window.search_bar]


def test_the_main_bar_is_quiet(window):
    controls = bar_controls(window)
    assert len(controls) <= MAIN_BAR_LIMIT, [c.toolTip().split("\n")[0] or c.objectName() for c in controls]
    for control in controls:
        if control is window.search_bar:
            continue
        assert control.objectName() in ("ghost", "ghostText", "ghostSplit"), control.objectName()  # borderless
        assert control.toolTip(), control.text()
    assert window.btn_library.text() == "Library" and window.btn_profile.text() == "Default"


def test_more_has_open_folder_refresh_and_the_tools(window):
    menu = window.btn_more.menu()
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts[:2] == ["Open Folder...", "Refresh"]
    assert "Find Duplicates..." in texts and any(t.startswith("Undo") for t in texts)
    # Ctrl+T still tags, with the Tag button gone from the bar
    assert window.action_tag.shortcut().toString() == "Ctrl+T" and window.action_tag in window.actions()


def test_the_selection_bar(window, qapp, monkeypatch):
    bar = window.selection_bar
    assert not bar.isVisible()
    select(window, {"red.png", "blue.png"})
    assert pump(qapp, lambda: bar.isVisible())
    assert bar.lbl_count.text() == "2 selected"
    assert not bar.btn_similar.isEnabled() and bar.btn_send.isEnabled()  # (Find Similar: one picture at a time)
    calls = []
    monkeypatch.setattr(window, "_open_tag_dialog", lambda assets=None: calls.append("tag"))
    bar.btn_tag.click()
    bar.btn_favorite.click()
    assert calls == ["tag"] and all(window.library.marks[i].favorite for i in window._selected_asset_ids())
    # Send to Artbook lists New Page and the pages
    bar.send_menu.aboutToShow.emit()
    assert [a.text() for a in bar.send_menu.actions()][0] == "New Page"
    # More: the whole right-click menu
    menu = window._selection_menu()
    assert isinstance(menu, QMenu) and any(a.text() == "Copy Full Path" for a in menu.actions())
    menu.deleteLater()
    select(window, {"red.png"})
    pump(qapp)
    assert bar.lbl_count.text() == "1 selected" and bar.btn_similar.isEnabled()
    select(window, {"cube.obj"})
    pump(qapp)
    assert not bar.btn_send.isEnabled() and "Only pictures" in bar.btn_send.toolTip()
    bar.btn_clear.click()
    assert pump(qapp, lambda: not bar.isVisible()) and not window._selected_assets()


def test_the_selection_bar_drops_its_words_when_narrow(window, qapp):
    window.resize(1500, 900)
    pump(qapp, seconds=0.3)
    select(window, {"red.png"})
    bar = window.selection_bar
    assert pump(qapp, lambda: bar.isVisible())
    assert bar.btn_tag.toolButtonStyle().name == "ToolButtonTextBesideIcon"
    window.view_modes.set_mode("split")
    total = sum(window.view_splitter.sizes())
    window.view_splitter.setSizes([1, total - 1])  # the asset side as narrow as it goes
    pump(qapp, seconds=0.5)
    bar.place()
    assert bar.btn_tag.toolButtonStyle().name == "ToolButtonIconOnly" and bar.width() <= bar.host.width()
    window.view_modes.set_mode("asset")
    pump(qapp, seconds=0.3)
    bar.place()
    assert bar.btn_tag.toolButtonStyle().name == "ToolButtonTextBesideIcon"


def test_the_place_button_says_where_you_are(window, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(tmp_path / "Concepts")))
    other = tmp_path / "Concepts"
    other.mkdir()
    Image.new("RGB", (8, 8)).save(other / "a.png")
    window.action_open_folder.trigger()
    assert pump(qapp, lambda: window.btn_library.text() == "Concepts")
    assert str(other) in window.btn_library.toolTip()
    window.btn_library.click()
    assert pump(qapp, lambda: window.btn_library.text() == "Library")


def test_the_right_click_menu_is_grouped(window):
    from tests.test_batch4 import every_action

    picture = next(a for a in window.library.assets if a.name == "red.png")
    select(window, {"red.png"})
    menu = window._asset_menu(picture)
    top = [a for a in menu.actions() if not a.isSeparator() and a.isVisible()]
    assert len(top) <= MENU_TOP_LIMIT, [a.text() for a in top]
    names = {a.text(): a for a in top}
    assert {"Find", "Edit", "Send to Artbook"} <= set(names)
    assert {"Find Similar", "Rename..."} <= set(every_action(menu))
    assert all(not names[n].icon().isNull() for n in ("Tag...", "Add to Favorites", "Find", "Edit", "Copy Full Path"))
    menu.deleteLater()


def test_every_theme_gets_the_derived_colours():
    from PySide6.QtGui import QColor

    from ui.themes import THEMES, stylesheet_from, with_derived

    for theme_id, tokens in THEMES.items():
        derived = with_derived(tokens)
        assert QColor(derived["raised"]).isValid() and QColor(derived["raised"]).alpha() == 255, theme_id
        assert derived["menu_border"]
    custom = {k: v for k, v in THEMES["light"].items()}  # (a Theme Studio theme: no raised of its own)
    custom.pop("raised", None)
    sheet = stylesheet_from(custom)
    assert "#floatingBar" in sheet and "$raised" not in sheet
    assert with_derived(THEMES["light"])["raised"] == "#ffffff"  # (light: its surface)
    assert with_derived(THEMES["dark_purple"])["raised"] == THEMES["dark_purple"]["button"]  # (dark: its button)


# ----------------------------------------------------------------------
# The viewer's frame (Batch D)
# ----------------------------------------------------------------------
VIEWER_HEADER_LIMIT = 8  # buttons in the viewer's header (previous / next included)


def viewer_for(qapp, path: Path, **kwargs):
    from core.scanner import AssetItem, determine_asset_group, determine_category_name
    from ui.viewer import AssetViewerDialog

    ext = path.suffix.lower()
    asset = AssetItem(path, path.name, ext, path.stat().st_size, 1.0, determine_asset_group(ext), determine_category_name(ext))
    dialog = AssetViewerDialog(asset, **kwargs)
    dialog.resize(1200, 800)
    dialog.show()
    pump(qapp, seconds=0.6)
    return dialog


def test_the_viewer_frame(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QDesktopServices, QKeyEvent

    from core.settings import get_viewer_details

    picture = tmp_path / "rock.png"
    Image.new("RGB", (120, 80), (90, 60, 40)).save(picture)
    dialog = viewer_for(qapp, picture, navigate=lambda step: None, position="1 of 2")
    try:
        assert not hasattr(dialog, "bottom_bar")  # (the footer is gone: its actions are in More)
        buttons = [b for b in dialog.header_bar.findChildren(QAbstractButton) if b.isVisibleTo(dialog.header_bar)]
        assert len(buttons) <= VIEWER_HEADER_LIMIT and all(b.toolTip() for b in buttons)
        more = {a.text(): a for a in dialog.btn_more.menu().actions()}
        assert set(more) == {"Open in External App", "Show in File Explorer", "Copy Path"}
        opened = []
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
        more["Open in External App"].trigger()
        more["Copy Path"].trigger()
        assert Path(opened[0]) == picture and qapp.clipboard().text() == str(picture)
        # Info (Ctrl+I): the details card, remembered
        assert not get_viewer_details() and dialog.details_card.isHidden()
        dialog.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_I, Qt.KeyboardModifier.ControlModifier))
        assert dialog.btn_info.isChecked() and not dialog.details_card.isHidden() and get_viewer_details()
        assert dialog.details_card.scroll.widget() is dialog.inspector_panel
        dialog.btn_info.click()
        assert pump(qapp, lambda: dialog.details_card.isHidden()) and not get_viewer_details()
    finally:
        dialog.close()


def test_the_image_tools_float_and_fade(qapp, tmp_path):
    picture = tmp_path / "seam.png"
    Image.new("RGB", (64, 64), (200, 200, 200)).save(picture)
    dialog = viewer_for(qapp, picture)
    try:
        tools = dialog.image_tools
        assert not tools.isHidden()
        sizes = [dialog.tile_choice.button(n) for n in (2, 3)]
        assert all(b.isHidden() for b in sizes)  # (the tile sizes only while Tile is on)
        dialog.btn_tile.setChecked(True)
        assert all(not b.isHidden() for b in sizes)
        dialog.tile_choice.button(3).click()
        assert dialog.tex_viewer.tiles == 3
        # Bottom centre of the picture
        area = tools.host
        assert abs((tools.x() + tools.width() / 2) - area.width() / 2) <= 2 and tools.y() > area.height() / 2
        tools._on_idle()  # (the mouse has been still)
        assert pump(qapp, lambda: tools.isHidden())
        tools.eventFilter(dialog.tex_viewer, QMouseMoveStub())
        assert not tools.isHidden()
    finally:
        dialog.close()


class QMouseMoveStub:
    """Stands in for a mouse move over the picture (all fade_when_idle looks at is its type)."""

    @staticmethod
    def type():
        from PySide6.QtCore import QEvent

        return QEvent.Type.MouseMove


def test_the_details_stay_below_a_pdfs_tool_bars(qapp, tmp_path):
    from core.settings import set_viewer_details
    from tests.test_pdf_viewer import make_pdf

    set_viewer_details(True)
    pdf = tmp_path / "notes.pdf"
    make_pdf(pdf)
    dialog = viewer_for(qapp, pdf)
    try:
        card = dialog.details_card
        assert pump(qapp, lambda: not card.isHidden())
        below = dialog.pdf_viewer.splitter.mapTo(dialog._body, QPoint(0, 0)).y()
        assert below > 0 and card.frame.mapTo(dialog._body, QPoint(0, 0)).y() >= below
    finally:
        dialog.close()


# ----------------------------------------------------------------------
# Batch E: the 3D viewer's tools float over the model; the material and Render windows
# ----------------------------------------------------------------------
CUBE = "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nf 1/1 2/2 3/3 4/4\n"


def model_viewer(qapp, tmp_path):
    path = tmp_path / "crate.obj"
    path.write_text(CUBE, encoding="utf-8")
    dialog = viewer_for(qapp, path)
    assert pump(qapp, lambda: dialog.viewport.face_count > 0)
    return dialog


def shown_buttons(bar) -> list[QAbstractButton]:
    return [b for b in bar.findChildren(QAbstractButton) if b.isVisibleTo(bar)]


def test_the_model_tools_float_over_the_model(qapp, tmp_path):
    dialog = model_viewer(qapp, tmp_path)
    try:
        assert not [w for w in dialog.findChildren(QWidget, "viewerToolbar") if w.isVisibleTo(dialog)]  # (no rows above it)
        tools = dialog.model_tools
        assert not tools.isHidden() and dialog._model_stack.isAncestorOf(tools)
        buttons = shown_buttons(tools)
        assert len(buttons) <= MODEL_TOOLS_LIMIT and all(b.toolTip() for b in buttons)
        assert dialog.btn_render.text() == "Render" and dialog.btn_render in buttons
        # Roughness only while the material is metal, just above the tools
        tune = tools._above[0]
        assert tune.isHidden()
        material = dialog.btn_model_color
        material.pick(dialog.cmb_material.findData("metallic"))
        assert dialog.viewport.material_id == "metallic" and material.text() == dialog.cmb_material.currentText()
        assert not tune.isHidden() and not dialog.roughness_bar.isHidden()
        assert tune.y() + tune.height() <= tools.y() + tools.layout().contentsMargins().top() + tune.layout().contentsMargins().bottom()
        material.pick(dialog.cmb_material.findData("clay"))
        assert pump(qapp, lambda: tune.isHidden())
        # They fade together while the mouse is still, and come back when it moves
        material.pick(dialog.cmb_material.findData("metallic"))
        tools._on_idle()
        assert pump(qapp, lambda: tools.isHidden() and tune.isHidden())
        tools.eventFilter(dialog.viewport, QMouseMoveStub())
        assert not tools.isHidden() and not tune.isHidden()
    finally:
        dialog.close()


def test_the_uvs_show_only_what_works_on_them(qapp, tmp_path):
    dialog = model_viewer(qapp, tmp_path)
    try:
        tools = dialog.model_tools
        before = shown_buttons(tools)
        dialog.btn_uv.click()
        assert pump(qapp, lambda: dialog.uv_view is not None and dialog.uv_view.layout_shown is not None)
        during = shown_buttons(tools)
        assert dialog.btn_uv in during and dialog.btn_model_color not in during and dialog.btn_render not in during
        assert len(during) < len(before) and not dialog.cmb_material.isEnabled()
        tools._idle.stop()
        dialog.btn_uv.click()
        assert shown_buttons(tools) == before
        assert tools._idle.isActive()  # (shown again: a whole while before they fade)
    finally:
        dialog.close()


def test_the_dropdowns_follow_their_choices(qapp, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from ui.controls import ComboDrop

    dialog = model_viewer(qapp, tmp_path)
    try:
        drops = {d.combo: d for d in dialog.model_tools.findChildren(ComboDrop)}
        shading = next(d for d in drops.values() if d.text() == "Shaded")
        dialog.viewport.setFocus()
        QTest.keyClick(dialog.viewport, Qt.Key.Key_W)  # (W: the next way of drawing it)
        assert shading.text() == "Wireframe" and dialog.viewport.render_mode == "Wireframe"
        material = dialog.btn_model_color
        material.menu().aboutToShow.emit()
        entries = [a for a in material.menu().actions() if not a.isSeparator()]
        assert entries[-1] is dialog.action_model_color and dialog.action_model_color.text() == "Colour..."
        checked = [a.text() for a in entries if a.isChecked()]
        assert checked == [dialog.cmb_material.currentText()]
        lighting = drops[dialog.cmb_lighting]
        lighting.pick(dialog.cmb_lighting.findData("dramatic"))
        assert dialog.viewport.lighting_id == "dramatic" and lighting.text() == dialog.cmb_lighting.currentText()
        QTest.keyClick(dialog.viewport, Qt.Key.Key_G)  # (G: the grid, and its button follows)
        grid = next(b for b in shown_buttons(dialog.model_tools) if "grid" in b.toolTip())
        assert not dialog.viewport.show_grid and not grid.isChecked()
    finally:
        dialog.close()


def test_the_material_window(qapp, tmp_path):
    from PySide6.QtWidgets import QPushButton

    from core.scanner import AssetItem, determine_asset_group
    from core.texture_sets import find_sets
    from ui.material_view import MaterialViewerDialog

    files = []
    for name in ("Rock_BaseColor.png", "Rock_Normal.png", "Rock_Roughness.png"):
        Image.new("RGB", (16, 16), (90, 90, 90)).save(tmp_path / name)
        files.append(AssetItem(tmp_path / name, name, ".png", 100, 1.0, determine_asset_group(".png"), "PNG"))
    revealed = []
    dialog = MaterialViewerDialog(find_sets(files)[0], send=lambda *args: None, reveal=lambda: revealed.append(True))
    dialog.resize(1200, 760)
    dialog.show()
    try:
        assert pump(qapp, lambda: bool(dialog.viewport.maps))
        assert not dialog.tools.isHidden() and all(b.toolTip() for b in shown_buttons(dialog.tools))
        filled = [b for b in dialog.findChildren(QPushButton) if b.objectName() == "primaryBtn" and b.isVisibleTo(dialog)]
        assert filled == [dialog.btn_maya]  # (Maya: the main app)
        assert not dialog.btn_blender.isHidden()
        assert not [b for b in dialog.findChildren(QAbstractButton) if b.text() == "Close"]  # (Esc, or the window's x)
        dialog.action_reveal.trigger()
        assert revealed
        dialog.chk_flip.click()
        assert dialog.chk_flip.isChecked()
    finally:
        dialog.close()


def test_the_render_window_has_one_main_button(qapp, tmp_path):
    from PySide6.QtWidgets import QPushButton

    from ui.render_window import RenderWindow

    dialog = model_viewer(qapp, tmp_path)
    window = RenderWindow(dialog.viewport, dialog._file_path(), dialog.asset.name, dialog)
    window.show()
    try:
        filled = [b for b in window.findChildren(QPushButton) if b.objectName() == "primaryBtn" and b.isVisibleTo(window)]
        assert filled == [window.btn_render] and window.btn_stop.isHidden()
        assert window.preview.width() > window.width() / 2  # (the picture gets the room)
        assert not [b for b in window.findChildren(QAbstractButton) if b.text() == "Close"]
    finally:
        window.reject()
        dialog.close()


# ----------------------------------------------------------------------
# Batch F: the PDF's slim bar and floating marking tools; the video's controls
# ----------------------------------------------------------------------
def pdf_viewer(qapp, tmp_path):
    from tests.test_pdf_viewer import make_pdf

    pdf = tmp_path / "notes.pdf"
    make_pdf(pdf)
    dialog = viewer_for(qapp, pdf)
    return dialog, dialog.pdf_viewer


def test_the_pdf_has_one_slim_bar(qapp, tmp_path):
    from PySide6.QtWidgets import QComboBox

    dialog, pdf = pdf_viewer(qapp, tmp_path)
    try:
        assert not [w for w in pdf.findChildren(QWidget, "viewerToolbar") if w.isVisibleTo(pdf)]  # (one row, not two)
        bar = pdf.find_pill.parentWidget()
        controls = [w for w in bar.findChildren(QWidget) if isinstance(w, (QAbstractButton, QLineEdit, QComboBox))
                    and w.isVisibleTo(bar) and not isinstance(w.parentWidget(), QComboBox)]
        assert len(controls) <= PDF_BAR_LIMIT and all(w.toolTip() for w in controls if isinstance(w, QAbstractButton))
        more = {a.text().split("	")[0]: a for a in pdf.btn_more.menu().actions() if a.text()}
        assert set(more) == {"Export a Copy...", "Fit Width", "Fit Page"}
        calls = []
        pdf.view.fit = lambda how: calls.append(how)
        more["Fit Page"].trigger()
        assert calls == ["page"]
        assert all(b.objectName() == "seg" for b in pdf.tab_buttons.values())  # (the sidebar's tabs: a switch)
        pdf.find_box.setFocus()
        assert pump(qapp, lambda: pdf.find_pill.property("focused") is True)
    finally:
        dialog.close()


def test_the_pdf_tools_float_and_stay(qapp, tmp_path):
    dialog, pdf = pdf_viewer(qapp, tmp_path)
    try:
        tools = pdf.marks_tools
        assert not tools.isHidden() and tools.host is pdf.view and tools._idle is None  # (never fades)
        assert tools.y() > pdf.view.height() / 2
        assert pdf.btn_color.isHidden() and pdf.size_drop.isHidden()  # (the Hand has neither)
        pdf.view.set_mode("highlight")
        assert pdf.tool_buttons["highlight"].isChecked() and not pdf.btn_color.isHidden() and pdf.size_drop.isHidden()
        pdf.view.set_mode("pen")
        assert not pdf.btn_color.isHidden() and not pdf.size_drop.isHidden()
        pdf.size_drop.pick(pdf.size_box.findData(5.0))
        assert pdf.view.pen_width == 5.0 and pdf.size_drop.text() == "5 pt"
        assert all(b.toolTip() for b in tools.findChildren(QAbstractButton) if b.isVisibleTo(tools))
        pdf.show_message("Saved")  # (a note above the tools, not under them)
        assert pdf.toast.geometry().bottom() < tools.y() + tools.layout().contentsMargins().top()
    finally:
        dialog.close()


def test_the_video_keeps_jump_and_proxy_in_more(qapp):
    from PySide6.QtWidgets import QComboBox

    from core.settings import get_video_skip_seconds
    from ui.video_player import VideoPlayer

    player = VideoPlayer()
    player.resize(900, 500)
    player.show()
    try:
        assert not [w for w in player.controls.findChildren(QComboBox) if w.isVisibleTo(player.controls)]  # (no Jump box)
        more = {a.text(): a for a in player.btn_more.menu().actions() if a.text()}
        assert "Jump Time" in more and more[player.action_proxy.text()] is player.action_proxy
        player.jump_menu.aboutToShow.emit()
        jumps = {a.text(): a for a in player.jump_menu.actions()}
        jumps["10 s"].trigger()
        assert get_video_skip_seconds() == 10 and "10 s" in player.btn_back.toolTip()
        player.jump_menu.aboutToShow.emit()
        assert [a.text() for a in player.jump_menu.actions() if a.isChecked()] == ["10 s"]
        assert player.btn_speed.text() == "1.0×" or player.btn_speed.text().endswith("×")
        buttons = [b for b in player.controls.findChildren(QAbstractButton) if b.isVisibleTo(player.controls)]
        assert all(b.toolTip() for b in buttons if b is not player.btn_play)
    finally:
        player.stop()
        player.deleteLater()


# ----------------------------------------------------------------------
# Batch G: the Artbook's quiet top bar and soft floating bars; soft selected rows everywhere
# ----------------------------------------------------------------------
def test_the_artbook_bars_are_quiet(qapp, tmp_path):
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QGraphicsDropShadowEffect, QPushButton, QToolButton

    from ui.artbook import ArtbookPanel

    picture = tmp_path / "ref.png"
    Image.new("RGB", (200, 120), (90, 60, 40)).save(picture)
    panel = ArtbookPanel()
    panel.resize(1100, 750)
    panel.show()
    try:
        bar = panel.btn_images.parentWidget()
        assert not [b for b in bar.findChildren(QPushButton) if b.isVisibleTo(bar)]  # (no boxed buttons)
        for button in (panel.btn_pages, panel.btn_images, panel.btn_fit, panel.btn_zoom):
            assert isinstance(button, QToolButton) and button.objectName() in ("ghost", "ghostText") and button.toolTip()
        board = panel.board
        board.add_files([picture])
        assert pump(qapp, lambda: board.items_list() and board.items_list()[0].pixmap is not None)
        note = board.add_note(QPointF(0, 300))
        note.setPlainText("Rim light")
        board.finish_note_edit()
        board._scene.clearSelection()
        note.setSelected(True)
        board._place_note_bar()
        board.start_draw()
        for bar in (board.note_bar, board.draw_bar):
            assert isinstance(bar.graphicsEffect(), QGraphicsDropShadowEffect)  # (soft floating bars)
        assert board.draw_bar.btn_done.objectName() == "ghostText" and board.draw_bar.btn_done.text() == "Done"
        board.stop_draw()
        board.toggle_image_list()
        assert isinstance(board._image_list.graphicsEffect(), QGraphicsDropShadowEffect)
    finally:
        panel.close()
        panel.deleteLater()


def test_the_separate_windows_window_button_is_a_dropdown(qapp):
    from ui.artbook_window import ArtbookWindow, WindowOptionsButton
    from ui.controls import DropButton

    window = ArtbookWindow("Test")
    try:
        button = WindowOptionsButton(window)
        assert isinstance(button, DropButton) and button.menu() is button.window_menu and button.text() == "Window"
    finally:
        window.close()
        window.deleteLater()


def test_selected_rows_are_a_soft_tint(qapp):
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

    from ui.views import RowHighlightDelegate

    asked = []

    class Recording(RowHighlightDelegate):
        def color(self, token):
            asked.append(token)
            return super().color(token)

    tree = QTreeWidget()
    tree.setItemDelegate(Recording(tree))
    item = QTreeWidgetItem(["Page 1"])
    tree.addTopLevelItem(item)
    tree.resize(300, 200)
    tree.show()
    item.setSelected(True)
    tree.grab()
    assert "accent_soft" in asked and "accent" not in asked  # (a soft tint, not a solid accent block)
    assert "card_selected_text" in asked
    tree.close()
    tree.deleteLater()


# ----------------------------------------------------------------------
# The Artbook's image bar (a trial: everything on it stays in the right-click menu too)
# ----------------------------------------------------------------------
@pytest.fixture
def artbook(qapp, tmp_path):
    from ui.artbook import ArtbookPanel

    paths = []
    for n, colour in enumerate(((200, 60, 40), (40, 120, 200), (60, 160, 60))):
        path = tmp_path / f"ref_{n}.png"
        Image.new("RGB", (240, 160), colour).save(path)
        paths.append(path)
    panel = ArtbookPanel()
    panel.resize(1200, 800)
    panel.show()
    panel.sidebar.set_open(False, animate=False)
    board = panel.board
    board.add_files(paths)
    assert pump(qapp, lambda: len(board.items_list()) == 3 and all(i.pixmap is not None for i in board.items_list()))
    board._scene.clearSelection()
    panel.btn_fit.click()
    yield panel
    panel.close()
    panel.deleteLater()


def select_items(board, items):
    board._scene.clearSelection()
    for item in items:
        item.setSelected(True)
    board._place_note_bar()


def test_the_image_bar_shows_what_applies(qapp, artbook):
    from PySide6.QtCore import QPoint

    board = artbook.board
    first, second, _third = board.items_list()
    select_items(board, [first])
    bar = board.image_bar
    assert not bar.isHidden() and not bar.btn_crop.isHidden() and bar.btn_arrange.isHidden() and bar.btn_group.isHidden()
    frame = board.mapFromScene(first.sceneBoundingRect()).boundingRect()
    assert bar.y() > frame.bottom() or bar.geometry().bottom() < frame.top()  # (beside the image, not over it)
    assert all(b.toolTip() for b in bar.findChildren(QAbstractButton) if b.isVisibleTo(bar))
    bar.btn_flip_h.click()
    assert first.state().flip_h
    select_items(board, [first, second])
    assert bar.btn_crop.isHidden() and not bar.btn_arrange.isHidden() and bar.btn_group.text() == "Group"
    bar.btn_group.click()
    assert first.group and first.group == second.group and bar.btn_group.text() == "Ungroup"
    bar.btn_group.click()
    assert not first.group
    board._scene.clearSelection()
    board._place_note_bar()
    assert bar.isHidden()


def test_the_image_bars_dropdowns_are_the_menus_entries(qapp, artbook):
    board = artbook.board
    first = board.items_list()[0]
    select_items(board, [first])
    bar = board.image_bar
    menu = bar.btn_adjust.menu()
    menu.aboutToShow.emit()
    entries = {a.text(): a for a in menu.actions() if a.text()}
    assert {"Grayscale", "Pixelated", "Values", "Guides", "Opacity"} <= set(entries)
    entries["Grayscale"].trigger()
    assert first.gray
    more = bar.btn_more.menu()
    more.aboutToShow.emit()
    assert {"Reset", "Bring to Front", "Send to Back", "Caption...", "File", "Delete"} <= {a.text() for a in more.actions()}
    # (and the right-click menu keeps all of it)
    context = board.context_menu(first.sceneBoundingRect().center())
    images = next(a.menu() for a in context.actions() if a.text() == "Images")
    selection = next(a.menu() for a in images.actions() if a.text() == "Selection")
    assert {"Crop", "Grayscale", "Values", "Guides", "Flip Horizontally", "Reset", "Delete"} <= {a.text() for a in selection.actions()}
    context.deleteLater()


def test_a_single_note_has_its_own_bar_not_the_image_bar(qapp, artbook):
    from PySide6.QtCore import QPointF

    board = artbook.board
    note = board.add_note(QPointF(0, 600))
    note.setPlainText("Rim light")
    board.finish_note_edit()
    select_items(board, [note])
    assert board._image_bar is None or board._image_bar.isHidden()
    board.start_draw()
    select_items(board, [board.items_list()[0]])
    assert board._image_bar is None or board._image_bar.isHidden()  # (not while drawing)
    board.stop_draw()


def test_the_size_lists_are_wide_enough_for_their_sizes(qapp, artbook):
    from PySide6.QtCore import QPointF

    board = artbook.board
    for box in (board.note_bar.size_box, board.draw_bar.size_box):
        widest = max(box.fontMetrics().horizontalAdvance(box.itemText(i)) for i in range(box.count()))
        assert box.view().minimumWidth() >= widest + 40  # (not "..." for every size)


# ----------------------------------------------------------------------
# Batch H: Settings as cards, and the dialog standard
# ----------------------------------------------------------------------
def filled_buttons(widget) -> list:
    from PySide6.QtWidgets import QPushButton

    return [b for b in widget.findChildren(QPushButton) if b.objectName() == "primaryBtn" and b.isVisibleTo(widget)]


def test_settings_pages_are_cards_and_scroll(qapp):
    from PySide6.QtWidgets import QPushButton, QScrollArea

    from ui.controls import SettingCard
    from ui.settings_dialog import NAV_ICONS, SettingsDialog
    from ui.views import HEADING_ROLE

    dialog = SettingsDialog()
    dialog.resize(dialog.minimumSize())  # (the smallest it gets: the Storage page used to squash its buttons)
    dialog.show()
    try:
        for name in ("Appearance", "Video", "Archives", "Artbook", "Browser", "Storage"):
            assert dialog.pages[name].findChildren(SettingCard), name
        dialog.show_page("Storage")
        assert pump(qapp, lambda: dialog.current_page() is dialog.page_storage)
        holder = dialog.stack.currentWidget()
        assert isinstance(holder, QScrollArea) and holder.verticalScrollBar().maximum() > 0  # (it scrolls instead)
        for button in dialog.page_storage.findChildren(QPushButton):
            assert button.height() >= button.minimumSizeHint().height(), button.text()
        # (whether a page is too wide depends on the real fonts, which tests don't have: checked by hand)
        dialog.show_page("Appearance")
        assert pump(qapp, lambda: dialog.current_page() is dialog.page_appearance)

        # The nav: an icon for every page, headings that can't be picked
        for name, item in dialog.nav_items.items():
            assert name in NAV_ICONS and not item.icon(0).isNull()
        heading = dialog.nav.topLevelItem(0)
        assert heading.text(0) == "GENERAL" and heading.data(0, HEADING_ROLE)
        assert dialog.nav_items["Folders"].flags() & heading.flags()  # (its pages stay enabled)
        dialog.nav.setCurrentItem(heading)
        assert pump(qapp, lambda: dialog.nav.currentItem() is dialog.nav_items["Appearance"])
        assert dialog.current_page() is dialog.page_appearance

        for name in dialog.nav_items:  # (Save is the one filled button, on every page)
            dialog.show_page(name)
            qapp.processEvents()
            assert filled_buttons(dialog) == [dialog.btn_save], name
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_the_alpha_thumbnails_choice_is_a_switch(qapp):
    from ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    page = dialog.page_appearance
    assert page.btn_alpha_relief.parent().objectName() == "segment"
    page.btn_alpha_normal.click()
    assert page.btn_alpha_normal.isChecked() and not page.btn_alpha_relief.isChecked()
    dialog.reject()
    dialog.deleteLater()


def standard_dialogs(qapp, tmp_path):
    from core import profiles
    from core.scanner import AssetItem, determine_asset_group
    from core.tag_repository import get_repository
    from ui.artbook_board import CustomGridDialog
    from ui.batch_dialogs import ConvertDialog, RenameDialog
    from ui.folder_rules_dialog import FolderRuleDialog
    from ui.pdf_viewer import ExportDialog
    from ui.profile_dialogs import NewProfileDialog
    from ui.report_dialog import ReportDialog
    from ui.tag_widgets import TagAssetsDialog

    Image.new("RGB", (16, 16), (90, 90, 90)).save(tmp_path / "a.png")
    files = [AssetItem(tmp_path / "a.png", "a.png", ".png", 100, 1.0, determine_asset_group(".png"), "PNG")]
    parent = QWidget()
    return parent, {
        "New Profile": (NewProfileDialog(profiles.current_profile(), parent), "Create", "Cancel"),
        "Report a Problem": (ReportDialog(parent), "Save Report...", "Close"),
        "Custom Grid": (CustomGridDialog((3, 3), parent), "OK", "Cancel"),
        "Export a Copy": (ExportDialog(parent, 2, 0, 5), "Export...", "Cancel"),
        "New Folder Rule": (FolderRuleDialog(get_repository(), folder=str(tmp_path), parent=parent), "Save Rule", "Cancel"),
        "Rename 1 File": (RenameDialog(files, parent=parent), "Rename", "Cancel"),
        "Convert and Resize 1 Picture": (ConvertDialog(files, parent=parent), "Convert", "Close"),
        "Tag File": (TagAssetsDialog(get_repository(), [1], parent), "Apply", "Cancel"),
    }


def test_dialogs_follow_the_standard(qapp, tmp_path):
    from PySide6.QtWidgets import QFrame, QLabel

    parent, dialogs = standard_dialogs(qapp, tmp_path)
    try:
        for title, (dialog, primary, cancel) in dialogs.items():
            titles = [l for l in dialog.findChildren(QLabel) if l.objectName() == "dialogTitle"]
            assert [l.text() for l in titles] == [title]
            footer = next(f for f in dialog.findChildren(QFrame) if f.objectName() == "dialogFooter")
            row = footer.layout()
            last, before = row.itemAt(row.count() - 1).widget(), row.itemAt(row.count() - 2).widget()
            assert last.text() == primary and last.objectName() == "primaryBtn", title  # (the main button: far right)
            assert before.text() == cancel, title  # (then Cancel / Close, just left of it)
            assert filled_buttons(dialog) == [last], title  # (the only filled one)
            assert all(b.toolTip() for b in (last, before))
    finally:
        parent.deleteLater()


def test_dialog_choices_are_switches(qapp, tmp_path):
    from PySide6.QtWidgets import QGroupBox, QRadioButton

    parent, dialogs = standard_dialogs(qapp, tmp_path)
    try:
        for title, (dialog, _primary, _cancel) in dialogs.items():
            assert not dialog.findChildren(QGroupBox) and not dialog.findChildren(QRadioButton), title
        export = dialogs["Export a Copy"][0]
        export.page_choices["all"].click()
        export.as_png.click()
        assert export.choice()["pages"] == "all" and export.choice()["format"] == "png"
        convert = dialogs["Convert and Resize 1 Picture"][0]
        convert.radio_folder.click()
        assert convert.folder_row.isVisibleTo(convert) and not convert.sub_row.isVisibleTo(convert)
    finally:
        parent.deleteLater()


# ----------------------------------------------------------------------
# Batch I: polish. Hover fades and press springs, the keyboard focus ring, Tab in reading order, colours
# worked out per theme (Sand, a pale accent), and windows that can't be made too small to use
# ----------------------------------------------------------------------
def test_sand_is_a_theme_and_pale_accents_get_dark_text(qapp):
    from PySide6.QtGui import QColor

    from core.settings import THEME_IDS
    from ui.duotone import tone_colors
    from ui.themes import THEMES, contrast, parse_color, with_derived

    assert "sand" in THEMES and "sand" in THEME_IDS and THEMES["sand"]["label"] == "Sand"
    for theme_id, tokens in THEMES.items():
        derived = with_derived(tokens)
        assert contrast(QColor(derived["on_accent"]), parse_color(tokens["accent"])) >= 3.0, theme_id
    assert with_derived(THEMES["dark"])["on_accent"] == "#ffffff"
    assert with_derived(THEMES["sand"])["on_accent"] == THEMES["sand"]["text_strong"]  # (amber is too pale for white)
    detail, _body = tone_colors("on_accent", tokens=THEMES["sand"])  # (a filled button's icon follows it)
    assert detail == QColor(THEMES["sand"]["text_strong"])


def test_the_switches_text_reads_on_its_track_in_every_theme(qapp):
    from PySide6.QtGui import QColor

    from ui.themes import THEMES, _solid, contrast, parse_color, with_derived

    def over(top: QColor, base: QColor) -> QColor:
        a = top.alphaF()
        return QColor(*(round(t * a + b * (1 - a)) for t, b in zip(top.getRgb()[:3], base.getRgb()[:3])))

    for theme_id, tokens in THEMES.items():
        derived = with_derived(tokens)
        for under in ("raised", "window_bg", "sidebar_bg"):  # (a card, a dialog, a side panel)
            track = over(parse_color(derived["track"]), _solid(derived[under], "#000000"))
            assert contrast(_solid(derived["text_muted"], "#888888"), track) >= 4.0, (theme_id, under)


def test_borderless_buttons_fade_and_spring(qapp):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QEnterEvent
    from PySide6.QtWidgets import QApplication

    from ui.animations import set_motion_mode
    from ui.controls import PRESS_SCALE, ghost_button

    set_motion_mode("on")
    bar = QWidget()
    try:
        button = ghost_button("more", "More", parent=bar)
        bar.show()
        qapp.processEvents()
        QApplication.sendEvent(button, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        qapp.processEvents()
        assert button.hover_level() < 1.0  # (it fades in...)
        assert pump(qapp, lambda: button.hover_level() == 1.0, 1.0)  # (...and gets there)
        QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
        assert pump(qapp, lambda: button.hover_level() == 0.0, 1.0)
        button.setDown(True)
        button.pressed.emit()
        assert pump(qapp, lambda: button.press_scale() <= PRESS_SCALE + 0.01, 1.0)  # (it dips)
        button.setDown(False)
        button.released.emit()
        assert pump(qapp, lambda: button.press_scale() == 1.0, 1.5)  # (and springs back)
        set_motion_mode("off")
        QApplication.sendEvent(button, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        assert button.hover_level() == 1.0  # (no motion: the hover simply shows)
        button.pressed.emit()
        assert button.press_scale() == 1.0  # (and nothing moves)
    finally:
        set_motion_mode("auto")
        bar.deleteLater()


def test_every_borderless_button_fades(window, qapp):
    """A ghost-styled button that isn't a GhostButton would snap its hover (and not spring)."""
    from PySide6.QtWidgets import QToolButton

    from ui.controls import GhostButton
    from ui.pdf_viewer import ExportDialog
    from ui.settings_dialog import SettingsDialog

    styled = {"ghost", "ghostText", "ghostSplit", "ghostDrop", "seg"}
    settings, export = SettingsDialog(window), ExportDialog(window, 3, 1, 12)
    try:
        for top in (window, settings, export):
            plain = [b.toolTip() or b.text() for b in top.findChildren(QToolButton)
                     if b.objectName() in styled and not isinstance(b, GhostButton)]
            assert not plain, plain
        assert isinstance(window.search_bar.btn_ai, GhostButton)  # (the search pill's buttons too)
    finally:
        settings.reject()
        export.reject()


def test_rows_fade_their_hover(qapp):
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

    from ui.animations import set_motion_mode
    from ui.views import HoverFade, install_view_effects

    install_view_effects(qapp)
    set_motion_mode("on")
    tree = QTreeWidget()
    try:
        tree.setHeaderHidden(True)
        first, second = QTreeWidgetItem(tree, ["First"]), QTreeWidgetItem(tree, ["Second"])
        tree.ensurePolished()  # (not shown: where the real mouse is doesn't come into it)
        fade = tree.findChild(HoverFade, "hoverFade")
        assert fade is not None
        one, two = tree.indexFromItem(first), tree.indexFromItem(second)
        fade.hover(one)
        assert fade.level(one) < 1.0 and pump(qapp, lambda: fade.level(one) == 1.0, 1.0)
        fade.hover(two)  # (the mouse moves on: one fades out as two fades in)
        qapp.processEvents()
        assert 0.0 < fade.level(one) <= 1.0 and fade.level(two) < 1.0
        assert pump(qapp, lambda: fade.level(one) == 0.0 and fade.level(two) == 1.0, 1.0)
        set_motion_mode("off")
        fade.hover(one)
        assert fade.level(one) == 1.0 and fade.level(two) == 0.0
    finally:
        set_motion_mode("auto")
        tree.deleteLater()


def test_the_focus_ring_shows_for_the_keyboard_only(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton

    from ui.animations import set_motion_mode
    from ui.focus_ring import FocusRing, install_focus_ring

    install_focus_ring(qapp)
    set_motion_mode("off")
    host = QWidget()
    try:
        row = QHBoxLayout(host)
        first, second, check, edit = QPushButton("One", host), QPushButton("Two", host), QCheckBox("Three", host), QLineEdit(host)
        for widget in (first, second, check, edit):
            row.addWidget(widget)
        host.show()
        host.activateWindow()
        assert pump(qapp, lambda: host.isActiveWindow())

        def ring():
            found = host.findChild(FocusRing, "focusRing")
            return found if found is not None and found.isVisible() else None

        first.setFocus()
        qapp.processEvents()
        assert ring() is None  # (not until the keyboard's used)
        QTest.keyClick(first, Qt.Key.Key_Tab)
        assert pump(qapp, lambda: ring() is not None)
        assert ring().geometry().contains(second.geometry()) and not ring().geometry().contains(first.geometry())
        QTest.keyClick(second, Qt.Key.Key_Tab)  # (it follows Tab)
        assert pump(qapp, lambda: ring() is not None and ring().geometry().contains(check.geometry().topLeft()))
        QTest.mouseClick(first, Qt.MouseButton.LeftButton)  # (a click: gone)
        assert pump(qapp, lambda: ring() is None)
        edit.setFocus(Qt.FocusReason.TabFocusReason)  # (a text field shows its own focus)
        assert not pump(qapp, lambda: ring() is not None, 0.3)
    finally:
        set_motion_mode("auto")
        host.deleteLater()


def test_the_focus_ring_follows_the_grids_arrow_keys(window, qapp):
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtTest import QTest

    from ui.animations import set_motion_mode
    from ui.focus_ring import FocusRing, install_focus_ring

    install_focus_ring(qapp)
    set_motion_mode("off")
    try:
        window.activateWindow()
        assert pump(qapp, lambda: window.isActiveWindow())
        grid = window.list_grid
        grid.setCurrentIndex(grid.model().index(0, 0))
        grid.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(grid, Qt.Key.Key_Right)
        card = grid.visualRect(grid.model().index(1, 0))
        card = QRect(grid.viewport().mapTo(window, card.topLeft()), card.size())

        def ringed() -> bool:
            ring = window.findChild(FocusRing, "focusRing")
            return ring is not None and ring.isVisible() and abs(ring.geometry().center().x() - card.center().x()) <= 2

        assert pump(qapp, ringed)
    finally:
        set_motion_mode("auto")


def test_tab_in_the_main_window_goes_as_you_read_it(window, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    window.activateWindow()
    assert pump(qapp, lambda: window.isActiveWindow())
    window.btn_sidebar.setFocus(Qt.FocusReason.TabFocusReason)
    visited = []
    for _ in range(14):
        visited.append(QApplication.focusWidget())
        window.focusNextPrevChild(True)
        qapp.processEvents()
    assert visited[:4] == [window.btn_sidebar, window.btn_profile, window.btn_library, window.search_bar.edit]
    bar_end = visited.index(window.btn_settings)
    assert visited.index(window.btn_more) < visited.index(window.btn_view) < bar_end  # (the bar, left to right)
    assert visited.index(window.list_grid) > bar_end  # (then the sidebar and the grid)
    window.btn_library.setFocus(Qt.FocusReason.TabFocusReason)
    window.focusNextPrevChild(False)  # (Shift+Tab goes back)
    assert QApplication.focusWidget() is window.btn_profile


def test_the_material_and_render_windows_keep_their_parts_apart_when_small(qapp, tmp_path):
    from core.scanner import AssetItem, determine_asset_group
    from core.texture_sets import find_sets
    from ui.material_view import MIN_SIZE, MaterialViewerDialog
    from ui.render_window import PANEL_WIDTH, RenderWindow

    files = []
    for name in ("Rock_BaseColor.png", "Rock_Normal.png"):
        Image.new("RGB", (16, 16), (90, 90, 90)).save(tmp_path / name)
        files.append(AssetItem(tmp_path / name, name, ".png", 100, 1.0, determine_asset_group(".png"), "PNG"))
    material = MaterialViewerDialog(find_sets(files)[0], send=lambda *args: None, reveal=lambda: None)
    material.show()
    material.resize(300, 300)
    qapp.processEvents()
    assert material.width() >= MIN_SIZE.width() and material.height() >= MIN_SIZE.height()
    material.close()

    dialog = model_viewer(qapp, tmp_path)
    render = RenderWindow(dialog.viewport, dialog._file_path(), dialog.asset.name, dialog)
    try:
        render.status.setText("LuxCore is free and downloads once: 157 MB, about 300 MB once unpacked. It's only needed for this.")
        render.show()
        render.resize(render.minimumSizeHint())
        qapp.processEvents()
        assert render.status.height() >= render.status.heightForWidth(PANEL_WIDTH - 36)  # (the note isn't cut off)
    finally:
        render.reject()
        dialog.close()
