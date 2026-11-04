"""New default file types and categories reach a config that stores its own lists, once
(core.settings.apply_new_defaults): the user's own edits, removals included, are kept."""

from __future__ import annotations

import json

from core import settings


def write_config(data):
    settings.CONFIG_FILE_PATH.write_text(json.dumps(data), encoding="utf-8")


def read_config():
    return json.loads(settings.CONFIG_FILE_PATH.read_text(encoding="utf-8"))


OLD_CATEGORIES = [
    {"name": "3D Models", "extensions": [".fbx"], "subcategories": []},
    {"name": "Videos", "extensions": [".mp4"], "subcategories": []},
]


def test_new_types_and_categories_are_added_once():
    write_config({"file_extensions": [".png", ".fbx"], "categories": OLD_CATEGORIES, "theme": "light"})
    assert settings.apply_new_defaults()
    assert settings.get_file_extensions()[:5] == [".png", ".fbx", ".zip", ".rar", ".7z"]
    assert {".ztl", ".spp", ".pur", ".psb"} <= set(settings.get_file_extensions())
    names = [c["name"] for c in settings.get_categories()]
    assert names == ["3D Models", "Videos", "Archives", "ZBrush", "App Files"]
    assert settings.get_categories()[2]["extensions"] == [".zip", ".rar", ".7z"]
    assert read_config()["theme"] == "light" and read_config()["defaults_seen"] == settings.DEFAULTS_VERSION

    # Removed afterwards: stays removed
    settings.set_file_extensions([".png", ".fbx"])
    settings.set_categories(settings.get_categories()[:2])
    assert not settings.apply_new_defaults()
    assert ".zip" not in settings.get_file_extensions()
    assert [c["name"] for c in settings.get_categories()] == ["3D Models", "Videos"]


def test_app_files_reach_a_config_that_had_archives():
    """(the user's config: version 3, with its own PSD subcategory and everything else as it was)"""
    psd = {"name": "PSD", "extensions": [".psd"], "subcategories": []}
    textures = {"name": "Textures & Images", "extensions": [], "subcategories": [psd]}
    mine = {"name": "Painter", "extensions": [".spp"], "subcategories": []}  # (a category of their own has .spp)
    write_config({"file_extensions": [".png", ".psd", ".spp"], "categories": [textures, mine], "defaults_seen": 3})
    assert settings.apply_new_defaults()
    extensions = settings.get_file_extensions()
    assert extensions[:3] == [".png", ".psd", ".spp"] and extensions.count(".spp") == 1 and ".ztl" in extensions
    categories = settings.get_categories()
    assert [c["name"] for c in categories] == ["Textures & Images", "Painter", "ZBrush", "App Files"]
    assert categories[0]["subcategories"][0]["extensions"] == [".psd", ".psb"]  # (beside PSD, wherever that's kept)
    assert categories[1]["extensions"] == [".spp"]
    substance = next(s for s in categories[3]["subcategories"] if s["name"] == "Substance")
    assert substance["extensions"] == [".sbsar", ".spsm"]  # (.spp stays in their category: no type shows twice)
    zbrush = {s["name"]: s["extensions"] for s in categories[2]["subcategories"]}
    assert zbrush == {"Brushes": [".zbp"], "Tools": [".ztl"], "Projects": [".zpr"]} and categories[2]["extensions"] == [".zmt"]

    # Once only: removed afterwards, it stays removed
    settings.set_categories(categories[:2])
    assert not settings.apply_new_defaults()
    assert [c["name"] for c in settings.get_categories()] == ["Textures & Images", "Painter"]


def test_a_config_that_had_zip_gets_rar_and_7z():
    """(Chunk A's set, version 2, reached the user's config before RAR and 7z were added)"""
    archives_category = {"name": "Archives", "extensions": [".zip"], "subcategories": []}
    write_config({"file_extensions": [".png", ".zip"], "categories": OLD_CATEGORIES + [archives_category], "defaults_seen": 2})
    assert settings.apply_new_defaults()
    assert settings.get_file_extensions()[:4] == [".png", ".zip", ".rar", ".7z"]
    assert settings.get_categories()[2]["extensions"] == [".zip", ".rar", ".7z"]

    # Archives category removed before: not brought back (the types still reach the file list)
    write_config({"file_extensions": [".png", ".zip"], "categories": OLD_CATEGORIES, "defaults_seen": 2})
    settings.apply_new_defaults()
    assert [c["name"] for c in settings.get_categories()] == ["3D Models", "Videos", "ZBrush", "App Files"]


def test_a_category_already_named_like_a_new_one_keeps_its_own_types():
    mine = {"name": "archives", "extensions": [".rar"], "subcategories": []}
    write_config({"categories": OLD_CATEGORIES + [mine]})
    settings.apply_new_defaults()
    categories = settings.get_categories()
    assert [c["name"] for c in categories] == ["3D Models", "Videos", "archives", "ZBrush", "App Files"]
    assert categories[2]["extensions"] == [".rar", ".7z"]  # (its own, plus the new types no category had)


def test_lists_not_stored_follow_the_defaults():
    write_config({"theme": "dark"})
    settings.apply_new_defaults()
    assert "file_extensions" not in read_config() and "categories" not in read_config()
    assert ".zip" in settings.get_file_extensions()
    assert "Archives" in [c["name"] for c in settings.get_categories()]


def test_a_new_install_starts_with_the_newest_defaults():
    assert not settings.CONFIG_FILE_PATH.exists()
    settings.apply_new_defaults()
    assert read_config() == {"defaults_seen": settings.DEFAULTS_VERSION}
    settings.set_file_extensions([".png"])  # (removes zip)
    assert not settings.apply_new_defaults()
    assert settings.get_file_extensions() == [".png"]


def test_an_unreadable_config_is_never_overwritten():
    settings.CONFIG_FILE_PATH.write_text("{ damaged", encoding="utf-8")
    assert not settings.apply_new_defaults()
    assert settings.CONFIG_FILE_PATH.read_text(encoding="utf-8") == "{ damaged"


def test_files_inside_archives_are_shown_by_default():
    assert settings.get_archive_contents()
    settings.set_archive_contents(False)
    assert not settings.get_archive_contents()
