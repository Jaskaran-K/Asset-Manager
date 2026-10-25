"""Tests for core.tag_repository (in-memory SQLite). Run with:  python -m pytest"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from core.scanner import AssetItem, determine_asset_group
from core.tag_repository import MIGRATIONS, TagError, TagRepository, default_tag_name


def make_asset(path: str) -> AssetItem:
    p = Path(path)
    ext = p.suffix.lower()
    return AssetItem(
        path=p, name=p.name, extension=ext, size_bytes=1, modified_timestamp=0.0,
        asset_group=determine_asset_group(ext), category_name=default_tag_name(ext),
    )


@pytest.fixture
def repo() -> TagRepository:
    return TagRepository(":memory:")


@pytest.fixture
def library(repo):
    """Five assets synced into the repo; returns {file name: asset id}."""
    assets = [make_asset(p) for p in (
        r"C:\lib\hero.fbx", r"C:\lib\hero_diffuse.png", r"C:\lib\rock.fbx",
        r"C:\lib\sky.jpeg", r"C:\lib\Ünïcode Folder\tree bark.jpg",
    )]
    ids = repo.sync_assets(assets)
    return {Path(path).name: asset_id for path, asset_id in ids.items()}


def tag_names(repo, asset_id):
    return [t.name for t in repo.tags_by_asset([asset_id])[asset_id]]


# ----------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------
def test_schema_is_migrated_and_foreign_keys_on(repo):
    assert repo.schema_version() == len(MIGRATIONS)
    assert repo._conn().execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_file_database_uses_wal_and_reopens(tmp_path):
    db = tmp_path / "library.db"
    first = TagRepository(db)
    first.create_custom_tag("Hero")
    assert first._conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    first.close_thread_connection()
    second = TagRepository(db)  # migration must not re-run on an up-to-date database
    assert [t.name for t in second.list_tags()] == ["Hero"]


def test_each_thread_gets_its_own_connection(tmp_path):
    repo = TagRepository(tmp_path / "library.db")
    main_conn = repo._conn()
    other = []

    def worker():
        other.append(repo._conn())
        repo.create_custom_tag("FromWorker")
        repo.close_thread_connection()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert other and other[0] is not main_conn
    assert repo.find_tag("fromworker") is not None


# ----------------------------------------------------------------------
# Default tags
# ----------------------------------------------------------------------
@pytest.mark.parametrize("ext, expected", [
    (".png", "PNG"), (".fbx", "FBX"), (".jpg", "JPEG"), (".jpeg", "JPEG"),
    (".gltf", "glTF"), (".glb", "glTF"), (".xyz", "XYZ"),
])
def test_extension_map(ext, expected):
    assert default_tag_name(ext) == expected


def test_scanned_files_get_default_tags(repo, library):
    assert tag_names(repo, library["hero.fbx"]) == ["FBX"]
    assert tag_names(repo, library["hero_diffuse.png"]) == ["PNG"]
    assert tag_names(repo, library["sky.jpeg"]) == ["JPEG"]
    assert tag_names(repo, library["tree bark.jpg"]) == ["JPEG"]  # .jpg and .jpeg share one tag
    defaults = {t.name: t.file_count for t in repo.list_tags(kind="default")}
    assert defaults == {"FBX": 2, "JPEG": 2, "PNG": 1}


def test_sync_is_idempotent_and_backfills(repo, library):
    again = repo.sync_assets([make_asset(r"C:\lib\hero.fbx"), make_asset(r"C:\lib\new.png")])
    assert again[r"C:\lib\hero.fbx"] == library["hero.fbx"]  # same row, not a duplicate
    assert tag_names(repo, again[r"C:\lib\new.png"]) == ["PNG"]
    assert {t.name: t.file_count for t in repo.list_tags(kind="default")}["PNG"] == 2


def test_paths_differing_only_in_case_are_the_same_file(repo, library):
    ids = repo.sync_assets([make_asset(r"c:\LIB\HERO.FBX")])
    assert list(ids.values()) == [library["hero.fbx"]]


def test_default_tags_are_read_only(repo, library):
    fbx = repo.find_tag("FBX")
    with pytest.raises(TagError):
        repo.rename_custom_tag(fbx.id, "Mesh")
    with pytest.raises(TagError):
        repo.delete_custom_tag(fbx.id)
    with pytest.raises(TagError):
        repo.set_tag_color(fbx.id, "#ff0000")
    with pytest.raises(TagError):
        repo.remove_tags([library["hero.fbx"]], [fbx.id])
    with pytest.raises(TagError):
        repo.add_tags([library["sky.jpeg"]], [fbx.id])
    assert tag_names(repo, library["hero.fbx"]) == ["FBX"]


# ----------------------------------------------------------------------
# Custom tags & validation
# ----------------------------------------------------------------------
def test_create_custom_tag_trims_and_validates(repo, library):
    tag = repo.create_custom_tag("   Hero   Props  ", "#ff8800")
    assert tag.name == "Hero Props" and tag.kind == "custom" and tag.color == "#ff8800"
    with pytest.raises(TagError, match="empty"):
        repo.create_custom_tag("   ")
    with pytest.raises(TagError, match="at most"):
        repo.create_custom_tag("x" * 41)


@pytest.mark.parametrize("name", ["hero props", "HERO PROPS", "png", "Png", "jpeg"])
def test_names_collide_case_insensitively_including_default_tags(repo, library, name):
    repo.create_custom_tag("Hero Props")
    with pytest.raises(TagError, match="already exists"):
        repo.create_custom_tag(name)


def test_non_ascii_names_collide_case_insensitively(repo):
    repo.create_custom_tag("Ünïcode")
    with pytest.raises(TagError):
        repo.create_custom_tag("üNÏCODE")


def test_rename_custom_tag(repo):
    hero = repo.create_custom_tag("Hero")
    repo.create_custom_tag("Villain")
    assert repo.rename_custom_tag(hero.id, "HERO").name == "HERO"  # changing only the case is fine
    with pytest.raises(TagError):
        repo.rename_custom_tag(hero.id, "villain")


# ----------------------------------------------------------------------
# Bulk tagging, tri-state counts, cascade delete
# ----------------------------------------------------------------------
def test_bulk_add_and_remove(repo, library):
    hero = repo.create_custom_tag("Hero")
    all_ids = list(library.values())
    repo.add_tags(all_ids, [hero.id])
    repo.add_tags(all_ids, [hero.id])  # adding again is harmless
    assert repo.get_tag(hero.id).file_count == 5
    repo.remove_tags([library["rock.fbx"], library["sky.jpeg"]], [hero.id])
    assert repo.get_tag(hero.id).file_count == 3
    assert tag_names(repo, library["hero.fbx"]) == ["FBX", "Hero"]


def test_apply_tag_changes_is_atomic(repo, library):
    hero = repo.create_custom_tag("Hero")
    png = repo.find_tag("PNG")
    with pytest.raises(TagError):
        repo.apply_tag_changes(list(library.values()), add_tag_ids=[hero.id], remove_tag_ids=[png.id])
    assert repo.get_tag(hero.id).file_count == 0  # nothing from the failed batch was applied


def test_tri_state_counts(repo, library):
    hero, prop = repo.create_custom_tag("Hero"), repo.create_custom_tag("Prop")
    selection = [library["hero.fbx"], library["hero_diffuse.png"], library["rock.fbx"]]
    repo.add_tags(selection, [hero.id])              # all three
    repo.add_tags([library["rock.fbx"]], [prop.id])  # one of three
    counts = repo.get_tags_for_assets(selection)
    assert counts[hero.id] == 3       # checked
    assert counts[prop.id] == 1       # partially checked
    assert repo.find_tag("FBX").id in counts and counts[repo.find_tag("FBX").id] == 2
    assert repo.find_tag("JPEG").id not in counts  # unchecked


def test_large_selections_are_chunked(repo):
    assets = [make_asset(rf"C:\big\file_{i}.png") for i in range(2500)]
    ids = list(repo.sync_assets(assets).values())
    tag = repo.create_custom_tag("Batch")
    repo.add_tags(ids, [tag.id])
    assert repo.get_tags_for_assets(ids)[tag.id] == 2500
    assert len(repo.find_assets_by_tags([tag.id])) == 2500


def test_deleting_a_tag_removes_it_from_all_files(repo, library):
    hero = repo.create_custom_tag("Hero")
    repo.add_tags(list(library.values()), [hero.id])
    assert repo.delete_custom_tag(hero.id) == 5  # reports how many files were affected
    assert repo.get_tag(hero.id) is None
    remaining = repo._conn().execute("SELECT COUNT(*) FROM asset_tags WHERE tag_id = ?", (hero.id,)).fetchone()[0]
    assert remaining == 0
    assert tag_names(repo, library["hero.fbx"]) == ["FBX"]


def test_deleting_an_asset_cascades_to_its_tags(repo, library):
    hero = repo.create_custom_tag("Hero")
    repo.add_tags([library["hero.fbx"]], [hero.id])
    repo._conn().execute("DELETE FROM assets WHERE id = ?", (library["hero.fbx"],))
    assert repo.get_tag(hero.id).file_count == 0


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------
def test_multi_tag_and_search(repo, library):
    hero, prop = repo.create_custom_tag("Hero"), repo.create_custom_tag("Prop")
    repo.add_tags([library["hero.fbx"], library["hero_diffuse.png"]], [hero.id])
    repo.add_tags([library["hero.fbx"], library["rock.fbx"]], [prop.id])
    fbx = repo.find_tag("FBX")

    assert repo.find_assets_by_tags([hero.id]) == {library["hero.fbx"], library["hero_diffuse.png"]}
    assert repo.find_assets_by_tags([hero.id, prop.id]) == {library["hero.fbx"]}
    assert repo.find_assets_by_tags([prop.id, fbx.id]) == {library["hero.fbx"], library["rock.fbx"]}
    assert repo.find_assets_by_tags([hero.id, prop.id], match_all=False) == {
        library["hero.fbx"], library["hero_diffuse.png"], library["rock.fbx"]
    }
    assert repo.find_assets_by_tags([]) == set()


def test_tag_search_combined_with_text(repo, library):
    hero = repo.create_custom_tag("Hero")
    repo.add_tags([library["hero.fbx"], library["hero_diffuse.png"]], [hero.id])
    jpeg = repo.find_tag("JPEG")
    assert repo.find_assets_by_tags([hero.id], text="DIFFUSE") == {library["hero_diffuse.png"]}
    assert repo.find_assets_by_tags([jpeg.id], text="BARK") == {library["tree bark.jpg"]}
    assert repo.find_assets_by_tags([jpeg.id], text="nothing") == set()
    assert repo.find_assets_by_tags([], text="tree b") == {library["tree bark.jpg"]}
