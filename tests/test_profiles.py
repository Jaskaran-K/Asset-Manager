"""Profiles (core.profiles): each has its own folders, file types, categories, collections and
snapshot; tags, favorites and ratings are shared. Switching swaps the lists at the config's top."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QEvent

from core import profiles, settings
from core.profiles import ProfileError
from core.tag_repository import EVERY_PROFILE, MIGRATIONS, TagError, TagRepository, path_key
from tests.test_main_window import names_in_grid, open_window, pump
from tests.test_tag_repository import make_asset


def config():
    return json.loads(settings.CONFIG_FILE_PATH.read_text(encoding="utf-8"))


ZBRUSH_CATEGORIES = [{"name": "Sculpts", "extensions": [".ztl"], "subcategories": []}]


# ----------------------------------------------------------------------
# The config
# ----------------------------------------------------------------------
def test_a_config_from_before_profiles_is_the_default_profile(tmp_path):
    settings.set_scan_folders([tmp_path])
    assert profiles.list_profiles() == [profiles.Profile("default", "Default")]
    assert profiles.current_profile().id == "default"
    assert "profiles" not in config()  # (nothing written until a second profile is made)


def test_profiles_have_their_own_folders_types_and_categories(tmp_path):
    settings.apply_new_defaults()  # (as at startup)
    main_folder, zbrush_folder = tmp_path / "main", tmp_path / "zbrush"
    settings.set_scan_folders([main_folder])
    settings.set_file_extensions([".png", ".fbx"])
    zbrush = profiles.create_profile("ZBrush", copy_of=None)
    painter = profiles.create_profile("  Substance   Painter ", copy_of="default")
    assert [p.name for p in profiles.list_profiles()] == ["Default", "ZBrush", "Substance Painter"]
    assert profiles.current_profile().name == "Default" and config()["profile"] == "default"

    profiles.switch_profile(zbrush.id)
    assert settings.get_scan_folders() == [] and settings.get_categories() == []  # (empty, with the default types)
    assert settings.get_file_extensions() == settings.DEFAULT_FILE_EXTENSIONS
    assert profiles.every_profiles_categories()[1] == []  # (not the defaults)
    settings.set_scan_folders([zbrush_folder])
    settings.set_categories(ZBRUSH_CATEGORIES)
    assert profiles.other_profiles_folders() == [main_folder.resolve(), main_folder.resolve()]

    profiles.switch_profile(painter.id)  # (a copy of Default as it was)
    assert settings.get_scan_folders() == [main_folder.resolve()] and settings.get_file_extensions() == [".png", ".fbx"]
    profiles.switch_profile(zbrush.id)
    assert settings.get_scan_folders() == [zbrush_folder.resolve()]
    assert settings.get_categories() == ZBRUSH_CATEGORIES
    profiles.switch_profile("default")
    assert settings.get_scan_folders() == [main_folder.resolve()] and settings.get_file_extensions() == [".png", ".fbx"]
    assert settings.get_categories()[0]["name"] == settings.DEFAULT_CATEGORIES[0]["name"]
    assert sorted(map(str, profiles.other_profiles_folders())) == sorted([str(main_folder.resolve()), str(zbrush_folder.resolve())])
    # Shared settings stay put
    settings.set_theme("light")
    profiles.switch_profile(zbrush.id)
    assert settings.get_theme() == "light"


def test_names_are_checked_and_found_by_name_or_id():
    zbrush = profiles.create_profile("ZBrush")
    for bad, message in (("", "needs a name"), ("zbrush", "already a profile"), ("a/b", "can't contain"), ("x" * 41, "up to 40")):
        with pytest.raises(ProfileError, match=message):
            profiles.create_profile(bad)
    assert profiles.find_profile("zBRUSH ") == zbrush and profiles.find_profile(zbrush.id) == zbrush
    assert profiles.find_profile("Maya") is None
    assert profiles.rename_profile(zbrush.id, "ZBrush 2025").name == "ZBrush 2025"
    profiles.rename_profile("default", "Everything")
    assert [p.name for p in profiles.list_profiles()] == ["Everything", "ZBrush 2025"]


def test_deleting_a_profile_takes_its_snapshot_but_never_the_one_in_use():
    from core.library_snapshot import snapshot_path

    zbrush = profiles.create_profile("ZBrush")
    with pytest.raises(ProfileError, match="in use"):
        profiles.delete_profile("default")
    assert snapshot_path("default").name == "library_snapshot.json"  # (the one there was before profiles)
    snapshot_path(zbrush.id).write_text("{}", encoding="utf-8")
    profiles.delete_profile(zbrush.id)
    assert not snapshot_path(zbrush.id).exists() and [p.id for p in profiles.list_profiles()] == ["default"]


def test_new_default_types_reach_a_profile_when_it_is_switched_to():
    zbrush = profiles.create_profile("ZBrush", copy_of=None)
    data = config()
    entry = next(e for e in data["profiles"] if e["id"] == zbrush.id)
    entry.update(file_extensions=[".png"], categories=[{"name": "Mine", "extensions": [".png"], "subcategories": []}], defaults_seen=1)
    settings._save_raw_config(data)
    profiles.switch_profile(zbrush.id)
    assert ".zip" in settings.get_file_extensions() and "Archives" in [c["name"] for c in settings.get_categories()]


def test_tag_renames_reach_every_profiles_categories_and_icons_used_anywhere_stay(tmp_path):
    from core.category_rules import renamed

    zbrush = profiles.create_profile("ZBrush", copy_of=None)
    icon = tmp_path / "icon.png"
    Image.new("RGB", (8, 8)).save(icon)
    kept = settings.import_category_icon(icon)
    profiles.switch_profile(zbrush.id)
    settings.set_categories([{"name": "Heroes", "extensions": [], "tags": ["Hero", "WIP"], "icon": str(kept), "subcategories": []}])
    profiles.switch_profile("default")
    profiles.edit_other_profiles_categories(lambda tags: renamed(tags, "hero", "Main Hero"))
    settings.remove_unused_category_icons(settings.get_categories())
    assert kept.exists()  # (used in ZBrush's categories)
    profiles.switch_profile(zbrush.id)
    assert settings.get_categories()[0]["tags"] == ["Main Hero", "WIP"]


# ----------------------------------------------------------------------
# The database: collections and what a scan forgets
# ----------------------------------------------------------------------
def test_collections_from_before_profiles_belong_to_default(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    for version, step in enumerate(MIGRATIONS[:6], start=1):
        conn.executescript(f"BEGIN; {step} PRAGMA user_version = {version}; COMMIT;")
    conn.execute("INSERT INTO saved_searches (name, query) VALUES ('Heroes', 'tag:Hero')")
    conn.commit()
    conn.close()
    repo = TagRepository(db)
    assert repo.schema_version() == len(MIGRATIONS)
    assert [(s.name, s.profile) for s in repo.list_saved_searches()] == [("Heroes", "default")]
    repo.close_thread_connection()


def test_collections_belong_to_a_profile_or_to_all_of_them():
    repo = TagRepository(":memory:")
    mine = repo.save_search("Heroes", "tag:Hero", "default")
    repo.save_search("heroes", "tag:Hero ztl", "zbrush")  # (another profile's may share the name)
    shared = repo.save_search("Props", "tag:Prop", EVERY_PROFILE)
    assert [s.name for s in repo.collections_for("default")] == ["Heroes", "Props"]
    assert [s.name for s in repo.collections_for("zbrush")] == ["heroes", "Props"]
    with pytest.raises(TagError, match="already exists"):
        repo.save_search("HEROES", "x", "default")
    with pytest.raises(TagError, match="already exists"):
        repo.save_search("props", "x", "zbrush")  # (Props shows there too)
    with pytest.raises(TagError, match="already exists"):
        repo.move_saved_search(mine.id, EVERY_PROFILE)  # (it would sit beside ZBrush's heroes)
    repo.move_saved_search(shared.id, "zbrush")
    assert [s.name for s in repo.collections_for("default")] == ["Heroes"]
    repo.rename_saved_search(mine.id, "Props")  # (free in Default now)
    assert repo.delete_profile_searches("zbrush") == 2
    assert [(s.name, s.profile) for s in repo.list_saved_searches()] == [("Props", "default")]


def test_exported_collections_keep_their_profile_by_name(tmp_path):
    repo = TagRepository(":memory:")
    repo.save_search("Heroes", "tag:Hero", "zb1")
    repo.save_search("Props", "tag:Prop", EVERY_PROFILE)
    repo.save_search("Old", "tag:Old", "gone")
    export = tmp_path / "tags.json"
    repo.export_tags(export, {"zb1": "ZBrush"})
    fresh = TagRepository(":memory:")
    result = fresh.import_tags(export, {"zbrush": "zb2"}, current_profile="default")
    assert result["searches"] == 3
    assert {s.name: s.profile for s in fresh.list_saved_searches()} == {"Heroes": "zb2", "Props": None, "Old": "default"}


def test_a_scan_keeps_what_is_known_about_other_profiles_files(tmp_path):
    repo = TagRepository(":memory:")
    mine, theirs, shared = tmp_path / "mine", tmp_path / "theirs", tmp_path / "shared"
    files = [make_asset(str(p)) for p in (mine / "a.png", theirs / "b.png", shared / "c.png", shared / "gone.png", tmp_path / "browsed.png")]
    for folder in (mine, theirs, shared):
        folder.mkdir()
    repo.sync_assets(files)
    scanned = [files[0], files[2]]  # (this profile: mine and shared; gone.png was deleted)
    assert repo.forget_unscanned_assets(scanned, keep_folders=[theirs, shared], scanned_folders=[mine, shared]) == 2
    left = {Path(row["path"]).name for row in repo._conn().execute("SELECT path FROM assets")}
    assert left == {"a.png", "b.png", "c.png"}  # (gone.png: in a folder this scan looked through)
    assert path_key(theirs / "b.png").startswith(path_key(theirs))


# ----------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------
@pytest.fixture
def two_libraries(tmp_path):
    main_folder, zbrush_folder = tmp_path / "main", tmp_path / "zbrush"
    main_folder.mkdir()
    zbrush_folder.mkdir()
    Image.new("RGB", (64, 32), "gray").save(main_folder / "wall.png")
    (main_folder / "chair.fbx").write_bytes(b"fbx")
    (zbrush_folder / "head.ztl").write_bytes(b"ztl")
    Image.new("L", (16, 16), "white").save(zbrush_folder / "alpha.png")
    settings.apply_new_defaults()  # (as at startup)
    settings.set_scan_folders([main_folder])
    zbrush = profiles.create_profile("ZBrush", copy_of="default")
    data = config()
    entry = next(e for e in data["profiles"] if e["id"] == zbrush.id)
    entry["scan_folders"] = [str(zbrush_folder)]
    entry["categories"] = ZBRUSH_CATEGORIES
    settings._save_raw_config(data)
    return main_folder, zbrush_folder, zbrush


def test_switching_profile_shows_its_library(two_libraries, qapp):
    main_folder, _zbrush_folder, zbrush = two_libraries
    window = open_window(qapp)
    assert names_in_grid(window) == ["chair.fbx", "wall.png"]
    assert window.windowTitle() == "Asset Manager - Default" and window.asset_toolbar.buttons[0][1] == "Default"
    window.repo.save_search("Walls", "wall", "default")
    window.sidebar_tree.reload_collections()
    wall_id = window.library.ids[str((main_folder / "wall.png").resolve())]

    window.switch_profile(zbrush.id)
    assert pump(qapp, lambda: window.library.scan_done_once and not window.library.is_scanning())
    assert names_in_grid(window) == ["alpha.png", "head.ztl"]
    assert window.asset_toolbar.buttons[0][1] == "ZBrush" and window.windowTitle() == "Asset Manager - ZBrush"
    assert [name for _item, name, _shows in window.sidebar_tree.rows()] == ["Sculpts"]
    assert window.sidebar_tree.item_collections.isHidden()  # (Walls is Default's)
    # The main library's files are still known (details aren't read again on switching back)
    assert window.repo._conn().execute("SELECT 1 FROM assets WHERE id = ?", (wall_id,)).fetchone() is not None

    window.switch_profile("default")  # (its snapshot shows straight away)
    assert names_in_grid(window) == ["chair.fbx", "wall.png"]
    assert window.sidebar_tree.item_collections.child(0).text(0) == "Walls"
    assert pump(qapp, lambda: window.library.scan_done_once and not window.library.is_scanning())
    assert window.library.ids[str((main_folder / "wall.png").resolve())] == wall_id
    window.close()


def test_the_profile_menu_and_another_copy_of_the_app_switching(two_libraries, qapp, monkeypatch):
    _main, _zbrush_folder, zbrush = two_libraries
    window = open_window(qapp)
    window._fill_profile_menu()
    labels = [a.text() for a in window.profile_menu.actions() if not a.isSeparator()]
    assert labels == ["Default", "ZBrush", "New Profile...", "Manage Profiles..."]
    assert [a.isChecked() for a in window.profile_menu.actions()[:2]] == [True, False]
    window.profile_menu.actions()[1].trigger()
    assert profiles.current_profile().id == zbrush.id and window.asset_toolbar.buttons[0][1] == "ZBrush"

    profiles.switch_profile("default")  # (as another copy of the app would)
    monkeypatch.setattr(window, "isActiveWindow", lambda: True)
    window.changeEvent(QEvent(QEvent.Type.ActivationChange))  # (coming back to this window)
    assert window.asset_toolbar.buttons[0][1] == "Default" and window._profile_id == "default"
    window.close()


def test_a_new_profile_from_the_menu_starts_empty_or_as_a_copy(two_libraries, qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from ui import profile_dialogs

    window = open_window(qapp)

    def answer(name, copy):
        def exec_(dialog):
            dialog.txt_name.setText(name)
            (dialog.btn_copy if copy else dialog.btn_empty).setChecked(True)
            return QDialog.DialogCode.Accepted
        return exec_

    monkeypatch.setattr(profile_dialogs.NewProfileDialog, "exec", answer("Painter", copy=False))
    window._new_profile()
    assert window.asset_toolbar.buttons[0][1] == "Painter" and settings.get_scan_folders() == []
    assert "Add its folders" in window.status_label.text()
    # Just All Assets and Favorites: no categories, and no Uncategorized repeating All Assets
    settings.set_scan_folders([two_libraries[0]])
    window._load_library()
    assert pump(qapp, lambda: window.library.scan_done_once and not window.library.is_scanning())
    window._update_sidebar_counts()
    assert window.sidebar_tree.rows() == [] and window.sidebar_tree.item_uncategorized.isHidden()
    assert window.sidebar_tree.item_all.text(0) == "All Assets (2)"
    monkeypatch.setattr(profile_dialogs.NewProfileDialog, "exec", answer("painter", copy=True))
    dialog = profile_dialogs.NewProfileDialog(profiles.current_profile(), window)
    dialog.txt_name.setText("painter")
    assert not dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled() and "already" in dialog.lbl_error.text()
    dialog.deleteLater()
    window.close()


def test_sidebar_categories_start_closed_and_stay_as_the_user_left_them(two_libraries, qapp):
    _main, _zbrush_folder, zbrush = two_libraries
    window = open_window(qapp)
    sidebar = window.sidebar_tree

    def main_rows():
        return {name: item for item, name, _shows in sidebar.rows() if item.parent() is None}

    assert not any(item.isExpanded() for item in main_rows().values())  # (at launch)
    window.repo.save_search("Walls", "wall", "default")
    window._rebuild_sidebar()
    assert not sidebar.item_collections.isHidden() and not sidebar.item_collections.isExpanded()
    sidebar.item_collections.setExpanded(True)
    window._rebuild_sidebar()
    assert sidebar.item_collections.isExpanded()  # (as the user left it)
    with_subs = [item for item in main_rows().values() if item.childCount()]
    assert with_subs
    opened = with_subs[0]
    opened_name = next(name for name, item in main_rows().items() if item is opened)
    opened.setExpanded(True)
    categories = settings.get_categories()
    settings.set_categories(categories + [{"name": "New One", "extensions": [], "subcategories": []}])
    window._rebuild_sidebar()  # (as after Settings > Categories)
    assert [name for name, item in main_rows().items() if item.isExpanded()] == [opened_name]
    main_rows()[opened_name].setExpanded(False)
    window._rebuild_sidebar()
    assert not any(item.isExpanded() for item in main_rows().values())

    main_rows()[opened_name].setExpanded(True)
    window.switch_profile(zbrush.id)
    window.switch_profile("default")  # (another profile: closed again)
    assert not any(item.isExpanded() for item in main_rows().values())
    assert not sidebar.item_collections.isExpanded()
    window.close()


def test_a_rescan_for_the_profile_shown_before_is_dropped(qapp):
    from core.library_watcher import FolderRescan
    from ui.library_state import LibraryState

    state = LibraryState(TagRepository(":memory:"))
    generation = state._view_generation
    state.reset()
    state._on_folders_rescanned(generation, FolderRescan(new_trees=[Path(r"C:\old")], assets=[make_asset(r"C:\old\a.png")]))
    assert state.assets == []
    state.deleteLater()


# ----------------------------------------------------------------------
# Settings > Profiles
# ----------------------------------------------------------------------
def test_settings_make_rename_and_delete_profiles_on_save(two_libraries, qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

    from ui import profile_dialogs
    from ui.settings_dialog import SettingsDialog

    main_folder, _zbrush_folder, zbrush = two_libraries
    from core.tag_repository import get_repository

    get_repository().save_search("Heads", "head", zbrush.id)
    dialog = SettingsDialog()
    page = dialog.page_profiles
    assert [page.list_profiles.item(i).text() for i in range(2)] == ["Default   (in use)", "ZBrush"]
    page.list_profiles.setCurrentRow(0)
    assert not page.btn_delete.isEnabled()  # (in use)
    note = dialog.page_folders.layout().itemAt(1).widget()
    assert "Default" in note.text()

    def exec_(new_dialog):
        new_dialog.txt_name.setText("Painter")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(profile_dialogs.NewProfileDialog, "exec", exec_)
    page._on_new()
    assert page.list_profiles.item(2).text() == "Painter   (new: made when you click Save)"
    assert not page.btn_shortcut.isEnabled()
    page.list_profiles.setCurrentRow(1)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Sculpting", True)))
    page._on_rename()
    page.list_profiles.setCurrentRow(1)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    page._on_delete()
    assert [p.name for p in profiles.list_profiles()] == ["Default", "ZBrush"]  # (nothing until Save)
    dialog._on_save()
    assert dialog.profiles_changed and [p.name for p in profiles.list_profiles()] == ["Default", "Painter"]
    assert get_repository().list_saved_searches() == []  # (ZBrush's collection went with it)
    painter = profiles.find_profile("Painter")
    profiles.switch_profile(painter.id)
    assert settings.get_scan_folders() == [main_folder.resolve()]  # (a copy of Default's)
    dialog.deleteLater()


def test_cancel_keeps_the_profiles(two_libraries, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    page = dialog.page_profiles
    page.list_profiles.setCurrentRow(1)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    page._on_delete()
    dialog.reject()
    assert len(profiles.list_profiles()) == 2
    dialog.deleteLater()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_a_desktop_shortcut_opens_the_app_in_the_profile(tmp_path):
    from core.apps import read_shortcut

    zbrush = profiles.create_profile("ZBrush")
    made = profiles.create_desktop_shortcut(zbrush, tmp_path)
    assert made == tmp_path / "Asset Manager - ZBrush.lnk" and made.is_file()
    program, arguments, _folder = profiles.launch_command(zbrush)
    assert arguments.endswith(f"--profile {zbrush.id}")
    assert Path(read_shortcut(made)).name.lower() == Path(program).name.lower()
