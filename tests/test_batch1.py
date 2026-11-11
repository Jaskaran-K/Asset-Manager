"""Protecting your work: earlier versions of Artbook pages (core.artbook_store, ui.artbook_versions),
Settings > Storage (core.storage) and Report a Problem (core.problem_report, ui.report_dialog)."""

from __future__ import annotations

import json
import time
import zipfile

import pytest
from PySide6.QtCore import QEvent

from core import archives, artbook_store as store, problem_report, render_engine, settings, storage, upscaler, video_tools


def pump(qapp, condition=lambda: False, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def image_item(blob: str, x: float = 0.0) -> dict:
    return {"id": blob[:8], "kind": "image", "source": {"path": None, "blob": blob}, "size": [10, 10], "x": x, "y": 0}


def save(page: dict, **changes) -> dict:
    page = {**page, **changes, "modified": time.time()}
    store.save_page(page)
    return page


def as_yesterday(page_id: str) -> None:
    """Make the page's versions look a day old (as if today's first change hasn't happened)."""
    for path in store.history_dir(page_id).iterdir():
        stamp = path.stem
        older = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(time.mktime(time.strptime(stamp, "%Y-%m-%d_%H%M%S")) - 86400))
        path.rename(path.with_name(older + path.suffix))


# ----------------------------------------------------------------------
# Earlier versions
# ----------------------------------------------------------------------
def test_a_version_is_kept_before_the_first_change_of_each_day():
    page = store.create_page("Heads")
    page = save(page, items=[image_item("a" * 64 + ".png")])
    assert len(store.list_versions(page["id"])) == 1  # (the empty page it was before today's first change)
    page = save(page, items=[image_item("a" * 64 + ".png", 5)])
    page = save(page, view={"zoom": 2})
    assert len(store.list_versions(page["id"])) == 1  # (one a day)
    as_yesterday(page["id"])
    save(page, view={"zoom": 3})
    assert len(store.list_versions(page["id"])) == 1  # (only the view changed: not a change to keep)
    save(page, items=[])
    versions = store.list_versions(page["id"])
    assert len(versions) == 2 and versions[0]["images"] == 1 and versions[0]["name"] == "Heads"


def test_only_the_last_ten_are_kept():
    page = store.create_page("Busy")
    for i in range(store.MAX_VERSIONS + 3):
        page = save(page, items=[image_item(f"{i:064d}.png")])
        as_yesterday(page["id"])
    assert len(store.list_versions(page["id"])) == store.MAX_VERSIONS
    assert len(list(store.history_dir(page["id"]).glob("*.json"))) == store.MAX_VERSIONS


def test_images_an_earlier_version_uses_are_kept(monkeypatch):
    page = store.create_page("Refs")
    blob = store.store_blob(page["id"], b"old picture", ".png")
    page = save(page, items=[image_item(blob)])
    as_yesterday(page["id"])
    page = save(page, items=[])  # (the image deleted today: yesterday's version still has it)
    page = save(page, items=[], name="Refs again")  # (and the backup no longer does)
    monkeypatch.setattr(store, "UNUSED_IMAGE_AGE", -1.0)
    store.remove_unused_images(page)
    assert store.blob_path(page["id"], blob).is_file()
    for path in store.history_dir(page["id"]).iterdir():
        path.unlink()
    store.remove_unused_images(page)
    assert not store.blob_path(page["id"], blob).is_file()  # (no version needs it any more)


def test_restoring_in_place_and_as_a_new_page():
    page = store.create_page("Creature")
    blob = store.store_blob(page["id"], b"picture", ".png")
    store.save_thumbnail(page["id"], b"thumb then")
    page = save(page, items=[image_item(blob)])
    as_yesterday(page["id"])
    page = save(page, items=[], name="Creature v2")
    stamp = store.list_versions(page["id"])[0]["stamp"]

    new = store.restore_version_as_new(page["id"], stamp)
    assert new["id"] != page["id"] and new["name"].startswith("Creature (") and len(new["items"]) == 1
    assert store.blob_path(new["id"], blob).is_file() and not store.history_dir(new["id"]).exists()
    order = store.ordered_page_ids()
    assert order.index(new["id"]) == order.index(page["id"]) + 1
    assert store.load_page(page["id"])["items"] == []  # (this one as it was)

    restored = store.restore_version(page["id"], stamp)
    assert len(restored["items"]) == 1 and store.load_page(page["id"])["items"][0]["source"]["blob"] == blob
    newest = store.list_versions(page["id"])[0]
    assert newest["name"] == "Creature v2" and newest["images"] == 0  # (how it was before: restorable too)
    with pytest.raises(store.PageError):
        store.restore_version(page["id"], "../../evil")


def test_duplicating_or_exporting_leaves_versions_and_originals_behind(tmp_path):
    from core import artbook_share

    page = store.create_page("Shared")
    blob = store.store_blob(page["id"], b"upscaled", ".png")
    original = store.store_blob(page["id"], b"original", ".png")
    item = {**image_item(blob), "original": {"source": {"path": None, "blob": original}, "size": [5, 5], "name": ""}}
    page = save(page, items=[item])
    copy = store.duplicate_page(page["id"])
    assert not store.history_dir(copy["id"]).exists()
    artbook_share.export_page(page["id"], tmp_path / "shared.artbook")
    with zipfile.ZipFile(tmp_path / "shared.artbook") as archive:
        shared = json.loads(archive.read("page.json"))
        assert "original" not in shared["items"][0] and f"images/{original}" not in archive.namelist()


def test_the_versions_window_restores_the_open_page(qapp):
    from ui.artbook import ArtbookPanel
    from ui.artbook_versions import when_text

    panel = ArtbookPanel()
    panel.show()
    try:
        page_id = panel.current_page_id()
        panel.board.add_note().setPlainText("First idea")
        panel.board.finish_note_edit()
        panel.save_now()
        as_yesterday(page_id)
        panel.board.add_note().setPlainText("Second idea")
        panel.board.finish_note_edit()
        panel.save_now()
        panel.show_versions(page_id)
        dialog = panel.versions_dialog
        assert dialog.list.count() == 2 and "1 note" in dialog.list.item(0).text()
        dialog.list.setCurrentRow(0)
        dialog.btn_restore.click()
        notes = [item for item in panel.board.items_list() if item.__class__.__name__ == "NoteItem"]
        assert [n.toPlainText() for n in notes] == ["First idea"]
        assert len(store.list_versions(page_id)) == 3  # (with the two notes, kept before restoring)
        assert when_text(time.time()).startswith("Today") and when_text(time.time() - 86400).startswith("Yesterday")
    finally:
        panel.close()
        panel.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ----------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------
def fill(folder, name="file.bin", size=1000):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"x" * size)


def test_storage_sizes_and_freeing():
    fill(video_tools.THUMB_DIR, size=3000)
    fill(archives.index_dir(), size=500)
    fill(video_tools.artbook_cache_dir(), size=200)
    fill(archives.extract_dir() / "abc", size=4000)
    fill(render_engine._root() / "lux-1", size=7000)
    fill(upscaler._root() / "realesrgan-1", size=6000)
    sizes = storage.measure()
    assert (sizes["previews"], sizes["artbook_small"], sizes["unpacked"], sizes["luxcore"], sizes["upscaler"]) == (3500, 200, 4000, 7000, 6000)
    for key in ("previews", "artbook_small", "unpacked", "luxcore", "upscaler"):
        storage.free(key)
    sizes = storage.measure()
    assert all(sizes[key] == 0 for key in ("previews", "artbook_small", "unpacked", "luxcore", "upscaler"))
    page = store.create_page("Mine")
    storage.free("pages")  # ("open" areas: nothing is deleted)
    assert store.page_dir(page["id"]).is_dir()
    assert storage.size_text(0) == "Nothing" and storage.size_text(1536) == "2 KB" and storage.size_text(3 * storage.GB) == "3.0 GB"


def test_the_unpacked_files_limit(monkeypatch):
    assert settings.get_unpacked_limit_gb() == 5  # (was 20)
    settings.set_unpacked_limit_gb(7)  # (not a choice)
    assert settings.get_unpacked_limit_gb() == 5
    settings.set_unpacked_limit_gb(2)
    assert archives.extract_limit_bytes() == 2 * storage.GB
    for i in range(3):
        fill(archives.extract_dir() / str(i), size=1000)
    monkeypatch.setattr(archives, "extract_limit_bytes", lambda: 1500)
    assert archives.prune_extracted() == 2


def test_the_storage_page(qapp):
    from ui.settings_dialog import SettingsDialog

    fill(video_tools.THUMB_DIR, size=2 * 1024 * 1024)
    dialog = SettingsDialog()
    try:
        dialog.show()
        page = dialog.page_storage
        assert not page.sizes  # (only added up when the page is first shown)
        dialog.show_page("Storage")
        assert pump(qapp, lambda: page.sizes)
        assert page.size_labels["previews"].text() == "2 MB" and page.buttons["previews"].isEnabled()
        assert not page.buttons["luxcore"].isEnabled()  # (nothing to remove)
        page.buttons["previews"].click()
        assert "Cleared Thumbnails and preview copies (2 MB)" in page.lbl_status.text()
        assert pump(qapp, lambda: page.size_labels["previews"].text() == "Nothing")
        page.cb_limit.setCurrentIndex(page.cb_limit.findData(10))
        dialog._on_save()
        assert settings.get_unpacked_limit_gb() == 10
    finally:
        dialog.deleteLater()


# ----------------------------------------------------------------------
# Report a Problem
# ----------------------------------------------------------------------
def test_the_report_has_what_helps_and_settings_only_when_asked(tmp_path):
    from core import crash_log

    crash_log.crash_log_path().write_text("a crash", encoding="utf-8")
    secret = tmp_path / "My Secret Project"
    settings.set_scan_folders([secret])
    report = problem_report.make_report(tmp_path / "report.zip", cards=["Test GPU (driver 1)"])
    names = problem_report.contents(report)
    assert "about.txt" in names and "logs/crash.log" in names and "settings.json" not in names
    with zipfile.ZipFile(report) as archive:
        about = archive.read("about.txt").decode("utf-8")
    assert "Graphics: Test GPU (driver 1)" in about and "library folders: 1" in about
    assert "My Secret Project" not in about
    with_settings = problem_report.make_report(tmp_path / "report2.zip", include_settings=True, cards=[])
    with zipfile.ZipFile(with_settings) as archive:
        assert "My Secret Project" in archive.read("settings.json").decode("utf-8")
    assert "issues/new?" in problem_report.issue_url() and "App+version" in problem_report.issue_url()


def test_the_report_window(qapp, tmp_path, monkeypatch):
    from ui import report_dialog

    monkeypatch.setattr(problem_report, "graphics_cards", lambda: ["Test GPU"])
    revealed = []
    monkeypatch.setattr(report_dialog.file_actions, "reveal", revealed.append)
    dialog = report_dialog.ReportDialog()
    assert not dialog.chk_settings.isChecked() and dialog.suggested_path().name.startswith("AssetManager-report-")
    dialog.save_to(tmp_path / "bug")
    assert pump(qapp, lambda: dialog.saved is not None)
    assert dialog.saved == tmp_path / "bug.zip" and revealed == [dialog.saved]
    assert "settings.json" not in problem_report.contents(dialog.saved)
    dialog.deleteLater()


def test_report_a_problem_is_on_the_about_page(qapp):
    from ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    try:
        dialog.page_about.btn_report.click()
        assert dialog.page_about.report_dialog.isVisible()
        dialog.page_about.report_dialog.reject()
    finally:
        dialog.deleteLater()


def test_a_report_in_the_real_graphics_query_does_not_hang():
    started = time.monotonic()
    cards = problem_report.graphics_cards()
    assert isinstance(cards, list) and time.monotonic() - started < problem_report._GPU_TIMEOUT + 5
