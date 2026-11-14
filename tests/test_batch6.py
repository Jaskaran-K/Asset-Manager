"""Batch 6: the AI tools. Downloads (core.ai_tools), the helper (core.ai_helper), AI Search, More Like This and
tag suggestions (core.ai_search, the library's reading, the search box, the tag window), and Remove
Background in the library and the Artbook.

The models aren't run here: the helper's stand-in engine answers (a picture's vector is its mean colour, text
names a colour, a mask is what differs from the corner). The real ones were tried on the user's library
(SigLIP: 81% of the top 8 right; BEN2: 0.74 s a picture on an RTX 4070)."""

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPointF, Qt

from core import ai_helper, ai_search, ai_tools, storage
from core.render_engine import Download


def pump(qapp, condition=lambda: False, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def fake_ai(monkeypatch):
    """The stand-in engine, in the test's own process."""
    monkeypatch.setenv(ai_helper.ENGINE_ENV, "inprocess-fake")
    ai_helper.last_device.clear()


def picture(path: Path, color, size=(64, 48), mode="RGB") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color).save(path)
    return path


# ----------------------------------------------------------------------
# Downloads
# ----------------------------------------------------------------------
def test_the_pinned_downloads_are_exact():
    for item in (*ai_tools.RUNTIME, *(d for tool in ai_tools.TOOLS.values() for d in tool.files)):
        assert item.url.startswith(("https://files.pythonhosted.org/", "https://huggingface.co/"))
        assert len(item.sha256) == 64 and int(item.sha256, 16) >= 0 and item.size > 0
        if "huggingface.co" in item.url:
            assert "/resolve/main/" not in item.url  # (a fixed commit: the files can't change under us)
    assert ai_tools.TOOLS["search"].download_bytes + ai_tools.RUNTIME_BYTES < 460e6
    assert ai_tools.TOOLS["birefnet"].download_bytes + ai_tools.RUNTIME_BYTES < 520e6
    assert ai_tools.TOOLS["ben2"].download_bytes + ai_tools.RUNTIME_BYTES < 260e6
    assert ai_tools.CUTOUT_MODELS == ("birefnet", "ben2")  # (BiRefNet first: the default)
    assert ai_tools.TOOLS["birefnet"].normalise and not ai_tools.TOOLS["ben2"].normalise


def _served(tmp_path, name: str, data: bytes, damaged: bool = False) -> Download:
    path = tmp_path / "served" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return Download(name, path.as_uri(), len(data), hashlib.sha256(data + (b"!" if damaged else b"")).hexdigest())


def fake_downloads(tmp_path, monkeypatch, damaged=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("onnxruntime/__init__.py", "")
        z.writestr("onnxruntime/capi/onnxruntime.dll", "a library")
        z.writestr("onnxruntime/tools/convert.py", "not needed")
        z.writestr("onnxruntime_directml-1.24.4.dist-info/RECORD", "not needed")
    runtime = (_served(tmp_path, "runtime.whl", buffer.getvalue()),)
    model = _served(tmp_path, "BEN2_Base.onnx", b"a model" * 100, damaged)
    monkeypatch.setattr(ai_tools, "RUNTIME", runtime)
    monkeypatch.setattr(ai_tools, "RUNTIME_BYTES", runtime[0].size)
    tools = dict(ai_tools.TOOLS)
    tools["ben2"] = ai_tools.Tool("ben2", "Remove Background", "BEN2 (Prama LLC)", "ben2-test", (model,), "A second try")
    monkeypatch.setattr(ai_tools, "TOOLS", tools)
    monkeypatch.setattr(ai_tools, "supported", lambda: True)


def test_install_checks_each_file_and_keeps_only_what_runs(tmp_path, monkeypatch):
    fake_downloads(tmp_path, monkeypatch)
    assert not ai_tools.is_installed("ben2") and ai_tools.needed_bytes("ben2") > 700
    seen = []
    ai_tools.install(["ben2"], lambda done, total: seen.append((done, total)))
    assert ai_tools.is_installed("ben2") and ai_tools.needed_bytes("ben2") == 0
    assert seen[-1][0] == seen[-1][1]
    kept = {p.relative_to(ai_tools.runtime_folder()).as_posix() for p in ai_tools.runtime_folder().rglob("*") if p.is_file()}
    assert kept == {"onnxruntime/__init__.py", "onnxruntime/capi/onnxruntime.dll", "installed.json"}
    assert (ai_tools.model_folder("ben2") / "BEN2_Base.onnx").read_bytes() == b"a model" * 100
    assert not ai_tools.is_installed("birefnet")  # (each model on its own)
    assert not (ai_tools.root() / "downloading").exists()
    # Settings > Storage: each part, removed on its own
    sizes = storage.measure()
    assert sizes["ai_ben2"] > 0 and sizes["ai_runtime"] > 0 and sizes["ai_search"] == 0 and sizes["ai_birefnet"] == 0
    storage.free("ai_ben2")
    assert not ai_tools.is_installed("ben2") and ai_tools.runtime_installed()
    assert ai_tools.needed_bytes("ben2") == 700


def test_a_damaged_or_stopped_download_leaves_nothing(tmp_path, monkeypatch):
    fake_downloads(tmp_path, monkeypatch, damaged=True)
    with pytest.raises(ai_tools.ToolError, match="didn't download correctly"):
        ai_tools.install(["ben2"])
    assert not ai_tools.is_installed("ben2") and not ai_tools.model_folder("ben2").exists()
    assert ai_tools.runtime_installed()  # (it came first and was right: kept)
    from core.archives import Cancelled

    ai_tools.remove("runtime")
    fake_downloads(tmp_path, monkeypatch)
    with pytest.raises(Cancelled):
        ai_tools.install(["ben2"], should_stop=lambda: True)
    assert not ai_tools.runtime_installed() and not (ai_tools.root() / "downloading").exists()


def test_the_download_window(qapp, tmp_path, monkeypatch):
    from ui.ai_download import AIDownloadDialog

    fake_downloads(tmp_path, monkeypatch)
    dialog = AIDownloadDialog("ben2")
    assert "BEN2" in dialog.findChildren(type(dialog.status))[0].text() and dialog.btn_download.isEnabled()
    assert dialog.windowTitle() == "Download Remove Background (BEN2)"
    dialog.start()
    assert pump(qapp, lambda: dialog.result() == dialog.DialogCode.Accepted)
    assert ai_tools.is_installed("ben2")
    dialog.deleteLater()


# ----------------------------------------------------------------------
# The helper
# ----------------------------------------------------------------------
def test_the_stand_in_engine_answers_in_process(fake_ai):
    pixels = np.zeros((3, ai_helper.PICTURE_SIDE, ai_helper.PICTURE_SIDE, 3), np.uint8)
    pixels[0, ..., 0] = 255  # red
    pixels[1, ..., 2] = 255  # blue
    vectors = ai_helper.picture_vectors(pixels).astype(np.float32)
    assert vectors.shape == (3, ai_helper.VECTOR_SIZE) and np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-2)
    red, blue = ai_helper.text_vector("something red"), ai_helper.text_vector("blue")
    assert np.argmax(vectors @ red) == 0 and np.argmax(vectors @ blue) == 1
    assert ai_helper.last_device["search"] == "stand-in"


def test_the_helper_is_a_program_of_its_own(monkeypatch):
    import os

    monkeypatch.setenv(ai_helper.ENGINE_ENV, "fake")
    try:
        pid = ai_helper._reader().request({"op": "ping"}, 30)["pong"]
        mask = ai_helper.cutout_mask(np.zeros((ai_helper.CUTOUT_SIDE, ai_helper.CUTOUT_SIDE, 3), np.uint8))
    finally:
        ai_helper.close()
    assert pid != os.getpid() and mask.shape == (ai_helper.CUTOUT_SIDE, ai_helper.CUTOUT_SIDE) and mask.max() == 0


def test_without_the_download_it_says_so(monkeypatch):
    monkeypatch.delenv(ai_helper.ENGINE_ENV, raising=False)
    with pytest.raises(ai_helper.AIFailed, match="isn't downloaded"):
        ai_helper.text_vector("a skull")


# ----------------------------------------------------------------------
# Vectors, results, suggestions
# ----------------------------------------------------------------------
def _unit(*values) -> np.ndarray:
    v = np.zeros(ai_helper.VECTOR_SIZE, np.float32)
    v[:len(values)] = values
    return (v / np.linalg.norm(v)).astype(np.float16)


def test_the_index_compares_with_every_file():
    index = ai_search.VectorIndex({1: _unit(1, 0), 2: _unit(0, 1)})
    index.update({3: _unit(1, 1), 1: _unit(0.9, 0.1)})  # (one new, one replaced)
    assert len(index) == 3 and 3 in index and 4 not in index
    ids, scores = index.scores(_unit(1, 0).astype(np.float32))
    assert dict(zip(ids.tolist(), scores.tolist())) == pytest.approx({1: 0.99, 2: 0.0, 3: 0.71}, abs=0.01)
    ids, _scores = index.scores(_unit(1, 0).astype(np.float32), allowed={2, 3})
    assert sorted(ids.tolist()) == [2, 3]


def test_results_stop_where_the_matches_do():
    ids = np.arange(100)
    scores = np.linspace(0.13, -0.05, 100).astype(np.float32)  # (falls 0.0018 a place)
    assert len(ai_search.pick(ids, scores, 0.05)) == 28  # (within 0.05 of the best)
    assert len(ai_search.pick(ids, scores, 0.001)) == ai_search.MIN_RESULTS
    assert len(ai_search.pick(ids, scores, 1.0, maximum=40)) == 40
    assert ai_search.pick(ids, scores, 0.05)[:3] == [0, 1, 2]
    like = ai_search.pick(ids, scores, 0.05, first=50)
    assert like[0] == 50 and like.count(50) == 1 and like[1:4] == [0, 1, 2]
    assert ai_search.pick(np.array([], np.int64), np.array([], np.float32), 0.05) == []


def test_tags_are_suggested_from_similar_tagged_files():
    index = ai_search.VectorIndex({
        1: _unit(1, 0, 0), 2: _unit(1, 0.1, 0), 3: _unit(0.9, 0, 0.1),  # skin-like
        4: _unit(0, 1, 0), 5: _unit(0, 1, 0.1),  # ornament-like
        10: _unit(1, 0.05, 0.02), 11: _unit(0, 0.95, 0.05),  # untagged
    })
    tags_of = {1: ["Skin"], 2: ["Skin"], 3: ["Skin", "Cloth"], 4: ["Ornaments"], 5: ["Ornaments"], 10: [], 11: ["Ornaments"]}
    assert ai_search.suggest_tags(index, [10], tags_of, neighbours=3) == ["Skin", "Cloth"]
    assert ai_search.suggest_tags(index, [11], tags_of, neighbours=2) == []  # (it has the only one suggested)
    assert ai_search.suggest_tags(index, [99], tags_of) == []  # (no vector yet)


def test_pictures_for_the_models(tmp_path):
    rgba = Image.new("RGBA", (300, 100), (255, 0, 0, 0))
    rgba.paste((0, 0, 255, 255), (0, 0, 150, 100))
    path = tmp_path / "half.png"
    rgba.save(path)
    pixels = ai_search.picture_pixels(str(path))
    assert pixels.shape == (224, 224, 3) and pixels.dtype == np.uint8
    assert tuple(pixels[100, 10]) == (0, 0, 255) and tuple(pixels[100, 200]) == (128, 128, 128)  # (clear: on grey)
    assert ai_search.thumbnail_pixels(path).shape == (224, 224, 3)
    assert ai_search.cutout_input(rgba).shape == (1024, 1024, 3)


def test_the_model_chosen_does_the_cut_out(fake_ai, monkeypatch):
    asked = []
    real = ai_helper.handle
    monkeypatch.setattr(ai_helper, "handle", lambda request: asked.append(request.get("model")) or real(request))
    picture_ = Image.new("RGB", (40, 30), (10, 10, 10))
    ai_search.cut_out(picture_)
    ai_search.cut_out(picture_, "ben2")
    assert asked == ["birefnet", "ben2"] and set(ai_helper.last_device) == {"birefnet", "ben2"}


def test_a_cut_out_keeps_the_picture_and_its_transparency(fake_ai):
    photo = Image.new("RGBA", (400, 200), (30, 30, 30, 255))
    photo.paste((220, 220, 220, 255), (100, 50, 300, 150))
    photo.paste((220, 220, 220, 128), (100, 50, 140, 150))  # (half see-through already)
    cut = ai_search.cut_out(photo)
    assert cut.size == (400, 200) and cut.mode == "RGBA"
    assert cut.getpixel((10, 10))[3] == 0 and cut.getpixel((200, 100)) == (220, 220, 220, 255)
    assert 120 <= cut.getpixel((120, 100))[3] <= 136


def test_cut_outs_go_beside_the_picture_and_never_over_a_file(tmp_path, fake_ai):
    from tests.test_batch5 import asset

    source = picture(tmp_path / "skull.jpg", (30, 30, 30), (200, 120))
    with Image.open(source) as im:
        im = im.copy()
    im.paste((240, 240, 240), (50, 30, 150, 90))
    im.save(source)
    target = ai_search.cutout_target(asset(source))
    assert target == tmp_path / "skull_cutout.png"
    target.write_bytes(b"taken")
    second = ai_search.cutout_target(asset(source))
    assert second == tmp_path / "skull_cutout (2).png"
    ai_search.make_cutout(asset(source), second)
    with Image.open(second) as made:
        assert made.mode == "RGBA" and made.size == (200, 120) and made.getpixel((5, 5))[3] == 0
    assert target.read_bytes() == b"taken" and not list(tmp_path.glob("*.partial"))


# ----------------------------------------------------------------------
# The library's vectors
# ----------------------------------------------------------------------
def test_vectors_are_kept_until_the_file_changes(tmp_path):
    from core.tag_repository import TagRepository

    repo = TagRepository(tmp_path / "t.db")
    a, b = picture(tmp_path / "a.png", "red"), picture(tmp_path / "b.png", "blue")
    from tests.test_batch5 import asset

    found = repo.sync_assets([asset(a), asset(b)])
    ids = [found[str(a)], found[str(b)]]
    todo = repo.assets_needing_vector(ids)
    assert sorted(row[1] for row in todo) == [str(a), str(b)]
    repo.store_vectors({todo[0][0]: (_unit(1, 0), todo[0][3]), todo[1][0]: (None, todo[1][3])})  # (b unreadable)
    assert repo.assets_needing_vector(ids) == []  # (an unreadable file isn't tried again until it changes)
    stored = repo.vectors_by_asset(ids)
    assert list(stored) == [todo[0][0]] and stored[todo[0][0]].dtype == np.float16
    changed = asset(a)
    changed.modified_timestamp += 10
    repo.sync_assets([changed, asset(b)])
    assert [row[0] for row in repo.assets_needing_vector(ids)] == [todo[0][0]]
    assert repo.vectors_by_asset(ids) == {}


# ----------------------------------------------------------------------
# The window: AI Search, More Like This, tag suggestions, Remove Background
# ----------------------------------------------------------------------
@pytest.fixture
def colour_library(tmp_path, fake_ai):
    from core.settings import set_scan_folders

    root = tmp_path / "library"
    for name, color in (("crimson.png", (200, 20, 20)), ("scarlet.png", (230, 40, 30)), ("navy.png", (20, 20, 160)),
                        ("sky.png", (40, 60, 230)), ("grass.png", (30, 180, 40))):
        picture(root / name, color)
    set_scan_folders([root])
    return root


def grid_order(window):
    return [a.name for a in window.grid_model.assets()]


def test_ai_search_finds_pictures_by_what_is_in_them(qapp, colour_library):
    from tests.test_main_window import open_window

    window = open_window(qapp)
    assert pump(qapp, lambda: len(window.library.vectors) == 5 and window.library.vectors_todo == 0)
    window.search_bar.set_ai(True)
    assert window.search_bar.btn_ai.isChecked() and "Describe" in window.search_bar.edit.placeholderText()
    window.search_bar.edit.setText("something red")  # (no file is called that: the words describe the pictures)
    assert pump(qapp, lambda: grid_order(window)[:2] in (["crimson.png", "scarlet.png"], ["scarlet.png", "crimson.png"]))
    assert grid_order(window)[-1] in ("grass.png", "navy.png", "sky.png")
    assert "closest matches" in window.status_label.text()
    # Saved searches keep it on
    assert window.search_bar.query_text() == "ai:on something red"
    window.search_bar.set_query_text("blue")
    assert not window.search_bar.btn_ai.isChecked() and grid_order(window) == []  # (by name again: none)
    window.search_bar.set_query_text("ai:on blue")
    assert window.search_bar.ai_on() and window.search_bar.edit.text() == "blue"
    assert pump(qapp, lambda: grid_order(window)[:2] in (["navy.png", "sky.png"], ["sky.png", "navy.png"]))
    window.close()


def test_more_like_this_and_tag_suggestions(qapp, colour_library):
    from tests.test_main_window import open_window

    window = open_window(qapp)
    assert pump(qapp, lambda: len(window.library.vectors) == 5)
    crimson = next(a for a in window.library.assets if a.name == "crimson.png")
    window._more_like(crimson)
    assert window.search_bar.like_id() == window.library.id_of(crimson)
    assert pump(qapp, lambda: grid_order(window)[:2] == ["crimson.png", "scarlet.png"])
    assert window.search_bar.query_text().startswith("like:")
    window.search_bar.set_like(None)
    assert pump(qapp, lambda: len(grid_order(window)) == 5)

    tag = window.repo.create_custom_tag("Warm", "#ff0000")
    window.repo.apply_tag_changes([window.library.id_of(crimson)], [tag.id], [])
    window._after_tags_changed([window.library.id_of(crimson)])
    scarlet = next(a for a in window.library.assets if a.name == "scarlet.png")
    assert window._tag_suggestions([window.library.id_of(scarlet)]) == ["Warm"]
    from ui.tag_widgets import TagAssetsDialog

    dialog = TagAssetsDialog(window.repo, [window.library.id_of(scarlet)], window, suggested=["Warm"])
    dialog.suggestion_buttons["Warm"].click()
    assert dialog._changes() == ([tag.id], []) and not dialog.suggestion_buttons["Warm"].isEnabled()
    dialog.deleteLater()
    window.close()


def test_remove_background_in_the_library(qapp, tmp_path, fake_ai, monkeypatch):
    from tests.test_batch5 import asset
    from ui import ai_download
    from ui.batch_dialogs import CutoutDialog

    wanted = []
    monkeypatch.setattr(ai_download, "ensure_tool", lambda key, parent=None: wanted.append(key) or True)
    files = []
    for name in ("one.png", "two.jpg"):
        path = picture(tmp_path / name, (20, 20, 20), (120, 80))
        with Image.open(path) as im:
            im = im.convert("RGB")
        im.paste((230, 230, 230), (30, 20, 90, 60))
        im.save(path)
        files.append(asset(path))
    model = tmp_path / "x.fbx"
    model.write_text("not a picture")
    dialog = CutoutDialog(files + [asset(model)])
    assert len(dialog.assets) == 2 and dialog.btn_ok.text() == "Remove Backgrounds"
    assert dialog.model() == "birefnet" and dialog.cmb_model.count() == 2  # (BiRefNet first; BEN2 the second try)
    dialog.cmb_model.setCurrentIndex(dialog.cmb_model.findData("ben2"))
    dialog.btn_ok.click()
    assert pump(qapp, lambda: not dialog._running and len(dialog.created) == 2)
    assert sorted(p.name for p in dialog.created) == ["one_cutout.png", "two_cutout.png"]
    assert "Made 2 cut-outs" in dialog.lbl_summary.text() and not dialog.btn_trash.isHidden()
    assert dialog.tree.topLevelItem(0).text(2).startswith("Done") and wanted == ["ben2"]
    from core.settings import get_cutout_model

    assert get_cutout_model() == "ben2"  # (remembered for next time)
    dialog.deleteLater()


def test_the_menu_offers_more_like_this_and_remove_background(qapp, tmp_path, monkeypatch):
    from tests.test_batch5 import asset
    from ui.asset_menu import ArtbookSender, AssetMenuActions, build_asset_menu
    from PySide6.QtWidgets import QWidget

    sent = []
    file = asset(picture(tmp_path / "rock.png", "gray"))
    actions = AssetMenuActions(open_viewer=lambda: None, reveal=lambda: None, tag=lambda: None, toggle_favorite=lambda: None,
                               set_rating=lambda _r: None, more_like=lambda: sent.append("like"),
                               remove_background=lambda: sent.append("cutout"))
    parent = QWidget()
    monkeypatch.setattr(ArtbookSender, "add_send_menu", lambda self, menu, paths, resolve=None: None)
    menu = build_asset_menu(parent, file, [file], [], actions, ArtbookSender(None, lambda *_a: None))
    texts = {a.text(): a for a in menu.actions()}
    texts["More Like This"].trigger()
    texts["Remove Background..."].trigger()
    assert sent == ["like", "cutout"]
    parent.deleteLater()


@pytest.fixture
def board(qapp, tmp_path):
    from ui.animations import set_motion_mode
    from ui.artbook import ArtbookPanel
    from ui.themes import apply_theme

    set_motion_mode("off")
    apply_theme("dark", qapp)
    path = tmp_path / "statue.png"
    im = Image.new("RGB", (120, 80), (20, 20, 20))
    im.paste((230, 230, 230), (40, 20, 80, 60))
    im.save(path)
    panel = ArtbookPanel()
    panel.resize(1100, 750)
    panel.show()
    panel.board.add_files([path])
    assert pump(qapp, lambda: len(panel.board.items_list()) == 1 and panel.board.items_list()[0].base is not None)
    yield panel
    if panel.board.upscale_dialog is not None:
        panel.board.upscale_dialog.reject()
    panel.close()
    panel.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    set_motion_mode("on")


def test_remove_background_on_the_board(qapp, board, fake_ai, monkeypatch):
    from ui.artbook_upscale import CutoutDialog

    monkeypatch.setattr(CutoutDialog, "_tool_installed", lambda self: True)
    item = board.board.items_list()[0]
    item.setSelected(True)
    board.board.upscale_selection(cutout=True)
    dialog = board.board.upscale_dialog
    assert isinstance(dialog, CutoutDialog) and dialog.windowTitle() == "Remove Background"
    assert dialog.options_box.isVisible() and dialog.compare.checker_after
    assert [b.text() for b in dialog.model_choice.buttons()] == ["BiRefNet", "BEN2"] and dialog.model() == "birefnet"
    assert dialog.btn_upscale.text() == "Remove Background" and dialog.rows[0]["eligible"]
    dialog.model_choice.buttons()[1].click()  # (BEN2: remembered, and the window speaks of it)
    from core.settings import get_cutout_model

    assert get_cutout_model() == "ben2" and dialog.btn_remove.text() == "Remove BEN2"
    dialog.btn_upscale.click()
    assert pump(qapp, lambda: not dialog.busy() and dialog.rows[0]["result"])
    assert "Cut out by BEN2" in dialog.list.item(0).text() and dialog.btn_apply.text() == "Use 1 Cut-out Picture"
    steps = board.board.undo_stack.count()
    dialog.btn_apply.click()
    assert board.board.undo_stack.count() == steps + 1 and item.original is not None and item.natural == (120, 80)
    assert board.board.undo_stack.undoText() == "remove background 1 image"
    board.board.undo_stack.undo()
    assert item.original is None


def test_the_board_menu_offers_remove_background(qapp, board):
    board.board.items_list()[0].setSelected(True)
    menu = board.board.context_menu(QPointF(0, 0))
    images = next(a.menu() for a in menu.actions() if a.text() == "Images")
    assert {a.text(): a for a in images.actions()}["Remove Background..."].isEnabled()
    menu.deleteLater()


def test_the_helper_is_told_where_the_tools_are(monkeypatch, tmp_path):
    # (it found its own folder once, and missed the app's when that was elsewhere: "No module named onnxruntime")
    monkeypatch.delenv(ai_tools.ROOT_ENV, raising=False)
    told = ai_helper._reader().extra_env()
    assert told == {ai_tools.ROOT_ENV: str(ai_tools.root())} and str(tmp_path) in told[ai_tools.ROOT_ENV]
    monkeypatch.setenv(ai_tools.ROOT_ENV, str(tmp_path / "elsewhere"))
    assert ai_tools.root() == tmp_path / "elsewhere"


def test_a_saved_ai_search_offers_the_download(qapp, tmp_path, monkeypatch):
    from core.settings import set_scan_folders
    from tests.test_main_window import open_window
    from ui import ai_download

    monkeypatch.delenv(ai_helper.ENGINE_ENV, raising=False)  # (AI Search isn't downloaded)
    asked = []
    monkeypatch.setattr(ai_download, "ensure_tool", lambda key, parent=None: asked.append(key) or False)
    picture(tmp_path / "library" / "a.png", "red")
    set_scan_folders([tmp_path / "library"])
    window = open_window(qapp)
    window.search_bar.set_query_text("ai:on a skull")
    assert pump(qapp, lambda: asked == ["search"] and not window.search_bar.ai_on())  # (declined: it's off again)
    assert window.search_bar.edit.text() == "a skull"
    window.close()
