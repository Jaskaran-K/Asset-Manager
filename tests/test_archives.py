"""Files inside zips (core.archives): listing, the index cache, reading every packing method, unpacking
with progress and cancel, safe extraction, thumbnails, scanning, search, tags and the snapshot."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from core import archives
from core.scanner import AssetScanner
from zip_maker import DEFLATE64, DEFLATED, STORED, Entry, make_zip, png_bytes


@pytest.fixture(autouse=True)
def fresh_listings():
    archives.forget_listings()
    yield
    archives.forget_listings()


def sample_zip(path: Path) -> Path:
    return make_zip(path, [
        Entry("textures/rock.png", png_bytes(color="gray"), DEFLATED),
        Entry("textures/moss.png", png_bytes(color="green"), DEFLATE64),
        Entry("preview.jpg", b"not really a jpeg", STORED),
        Entry("notes/readme.txt", b"hello", DEFLATED),
        Entry("__MACOSX/textures/._rock.png", b"junk", STORED),
        Entry("textures/.DS_Store", b"junk", STORED),
    ])


def member(listing, name):
    return next(m for m in listing.members if m.name == name)


# ----------------------------------------------------------------------
# Listing and the index cache
# ----------------------------------------------------------------------
def test_listing_skips_mac_leftovers_and_keeps_what_reading_needs(tmp_path):
    result = archives.read_listing(sample_zip(tmp_path / "pack.zip"))
    assert not result.error
    assert sorted(m.name for m in result.members) == [
        "notes/readme.txt", "preview.jpg", "textures/moss.png", "textures/rock.png",
    ]
    moss = member(result, "textures/moss.png")
    assert moss.method == DEFLATE64 and moss.folder == "textures" and moss.base_name == "moss.png"
    assert time.localtime(moss.modified)[:3] == (2024, 5, 17)


def test_files_inside_become_assets_with_virtual_paths(tmp_path):
    archive = sample_zip(tmp_path / "pack.zip")
    assets = archives.entry_assets(archive, [".png", ".jpg"])
    assert sorted(a.name for a in assets) == ["moss.png", "preview.jpg", "rock.png"]
    rock = next(a for a in assets if a.name == "rock.png")
    assert rock.in_archive and rock.is_texture and rock.category_name == "PNG"
    assert str(rock.path) == f"{archive}|textures\\rock.png"
    assert not os.path.exists(rock.path)  # never a real file
    assert archives.member_for_path(rock.path) == rock.archive
    assert archives.location_key(rock) == os.path.normcase(str(archive))


def test_the_index_cache_is_used_until_the_archive_changes(tmp_path, monkeypatch):
    archive = sample_zip(tmp_path / "pack.zip")
    reads = []
    real = archives.read_listing
    monkeypatch.setattr(archives, "read_listing", lambda path: reads.append(path) or real(path))

    archives.listing(archive)
    archives.listing(archive)
    archives.forget_listings()  # (a new session: from the disk cache)
    assert len(archives.listing(archive).members) == 4
    assert len(reads) == 1

    make_zip(archive, [Entry("only.png", png_bytes())])
    os.utime(archive, (time.time() + 5, time.time() + 5))
    assert [m.name for m in archives.listing(archive).members] == ["only.png"]
    assert len(reads) == 2


def test_a_damaged_zip_lists_as_an_error(tmp_path):
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"PK\x03\x04 this is not a zip")
    result = archives.listing(bad)
    assert result.error and not result.members
    assert archives.summarize(result).badge("ZIP") == "ZIP"


def test_summary_counts_files_by_type(tmp_path):
    summary = archives.summarize(archives.listing(sample_zip(tmp_path / "pack.zip")))
    assert summary.files == 4
    assert summary.by_type[0] == ("PNG", 2)
    assert summary.badge("ZIP") == "ZIP · 4"


def test_card_picture_prefers_a_preview_then_common_formats(tmp_path):
    archive = make_zip(tmp_path / "pack.zip", [
        Entry("deep/folder/a.psd", b"x" * 10),
        Entry("b.png", png_bytes()),
        Entry("renders/Cover.jpg", png_bytes()),
        Entry("huge.png", b"x", STORED),
    ])
    candidates = archives.card_picture_candidates(archives.listing(archive))
    assert [m.name for m in candidates][:3] == ["renders/Cover.jpg", "b.png", "huge.png"]


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------
@pytest.mark.parametrize("method", [STORED, DEFLATED, DEFLATE64])
def test_every_packing_method_reads_back_exactly(tmp_path, method):
    data = os.urandom(300_000) + b"repeat" * 200_000
    archive = make_zip(tmp_path / "pack.zip", [Entry("big.bin", data, method)])
    (item,) = archives.listing(archive).members
    assert archives.read_bytes(item, limit=len(data)) == data


def test_damage_is_caught_by_the_checksum(tmp_path):
    data = b"0123456789" * 1000
    archive = make_zip(tmp_path / "pack.zip", [Entry("a.bin", data, STORED)])
    raw = bytearray(archive.read_bytes())
    raw[raw.index(b"0123456789") + 5] ^= 0xFF
    archive.write_bytes(bytes(raw))
    (item,) = archives.read_listing(archive).members
    with pytest.raises(archives.ArchiveError, match="damaged"):
        archives.read_bytes(item)


def test_more_data_than_the_archive_says_is_refused(tmp_path):
    archive = make_zip(tmp_path / "pack.zip", [Entry("bomb.bin", b"\0" * 2_000_000, DEFLATED)])
    (item,) = archives.read_listing(archive).members
    lying = archives.ArchiveMember(**{**item.__dict__, "size": 1000})
    with pytest.raises(archives.ArchiveError, match="bigger"):
        archives.read_bytes(lying)


def test_password_protected_files_say_so(tmp_path):
    archive = make_zip(tmp_path / "pack.zip", [Entry("secret.png", png_bytes(), STORED, encrypted=True)])
    (item,) = archives.listing(archive).members
    with pytest.raises(archives.ArchiveError, match="password-protected"):
        archives.read_bytes(item)


def test_too_big_to_read_into_memory(tmp_path):
    archive = make_zip(tmp_path / "pack.zip", [Entry("a.bin", b"x" * 5000)])
    (item,) = archives.listing(archive).members
    with pytest.raises(archives.ArchiveError, match="too big"):
        archives.read_bytes(item, limit=100)


# ----------------------------------------------------------------------
# Unpacking (the cache) and extracting (the user's copies)
# ----------------------------------------------------------------------
def test_materialize_unpacks_once_with_progress(tmp_path):
    data = os.urandom(3 * 1024 * 1024)
    archive = make_zip(tmp_path / "pack.zip", [Entry("videos/lesson 01.mp4", data, DEFLATE64)])
    (item,) = archives.listing(archive).members
    seen = []
    path = archives.materialize(item, lambda done, total: seen.append((done, total)))
    assert path.name == "lesson 01.mp4" and path.read_bytes() == data
    assert archives.extract_dir() in path.parents
    assert seen[0] == (0, len(data)) and seen[-1] == (len(data), len(data))
    assert archives.cached_copy(item) == path
    assert archives.materialize(item) == path  # (the copy, not unpacked again)


def test_materialize_can_be_cancelled_and_leaves_nothing(tmp_path):
    archive = make_zip(tmp_path / "pack.zip", [Entry("big.bin", os.urandom(4 * 1024 * 1024), STORED)])
    (item,) = archives.listing(archive).members
    calls = []
    with pytest.raises(archives.Cancelled):
        archives.materialize(item, should_stop=lambda: calls.append(1) or len(calls) > 2)
    assert archives.cached_copy(item) is None
    assert not [p for p in archives.extract_dir().rglob("*") if p.is_file()]


def test_unpacked_copies_are_pruned_oldest_first(tmp_path):
    archive = make_zip(tmp_path / "pack.zip", [Entry(f"{n}.bin", bytes([n]) * 1000) for n in range(3)])
    items = archives.listing(archive).members
    paths = [archives.materialize(m) for m in items]
    for age, path in enumerate(paths):
        os.utime(path, (time.time() - 100 + age, time.time() - 100 + age))
    assert archives.extracted_bytes() == 3000
    assert archives.prune_extracted(max_bytes=2000) == 1
    assert not paths[0].exists() and paths[1].exists() and paths[2].exists()


def test_local_file_is_temporary_unless_already_unpacked(tmp_path):
    archive = make_zip(tmp_path / "pack.zip", [Entry("sky.exr", b"exr data")])
    (item,) = archives.listing(archive).members
    with archives.local_file(item) as path:
        assert path.read_bytes() == b"exr data"
    assert not path.exists()
    copy = archives.materialize(item)
    with archives.local_file(item) as path:
        assert path == copy
    assert copy.exists()


@pytest.mark.parametrize("name", [
    "../evil.png", "a/../../evil.png", "/abs.png", "C:/windows/x.png", "C:x.png", "a/CON.txt", "a/b:stream.png",
    "a/what?.png", "",
])
def test_unsafe_names_are_refused(name):
    assert archives.safe_relative_path(name) is None


def test_safe_names_keep_their_folders():
    assert str(archives.safe_relative_path("textures/rock.png")) == "textures/rock.png"
    assert str(archives.safe_relative_path("a\\b\\c.png")) == "a/b/c.png"


def test_extracting_skips_unsafe_names_and_never_replaces_unasked(tmp_path):
    archive = make_zip(tmp_path / "pack.zip", [
        Entry("textures/rock.png", b"new rock"),
        Entry("readme.txt", b"new readme"),
        Entry("../escape.txt", b"bad"),
    ])
    folder = tmp_path / "out" / "pack"
    (folder / "textures").mkdir(parents=True)
    (folder / "textures" / "rock.png").write_bytes(b"my edited rock")

    plan = archives.plan_extract(archives.listing(archive).members, folder)
    assert plan.unsafe == ("../escape.txt",)
    assert [p.name for p in plan.existing] == ["rock.png"]

    result = archives.extract(plan, replace=False)
    assert (result.written, result.skipped, result.failed) == (1, 2, ())
    assert (folder / "textures" / "rock.png").read_bytes() == b"my edited rock"
    assert (folder / "readme.txt").read_bytes() == b"new readme"
    assert not (tmp_path / "out" / "escape.txt").exists()

    result = archives.extract(archives.plan_extract(archives.listing(archive).members, folder), replace=True)
    assert (folder / "textures" / "rock.png").read_bytes() == b"new rock"


# ----------------------------------------------------------------------
# Thumbnails
# ----------------------------------------------------------------------
def render(asset, size=110):
    from core.thumbnail_manager import _Request, _ThumbnailRunnable, card_style

    runnable = _ThumbnailRunnable(_Request("x"), asset, size, None, card_style())
    return runnable._render()


def test_thumbnails_of_images_inside_including_deflate64(tmp_path, qapp):
    archive = sample_zip(tmp_path / "pack.zip")
    for asset in archives.entry_assets(archive, [".png"]):
        picture, _note = render(asset)
        assert picture is not None and not picture.isNull(), asset.name


def test_a_big_file_inside_waits_until_it_is_opened(tmp_path, qapp, monkeypatch):
    archive = make_zip(tmp_path / "pack.zip", [Entry("a.png", png_bytes()), Entry("lesson.mp4", b"video")])
    monkeypatch.setattr(archives, "MAX_PREVIEW_BYTES", 10)
    image, video = sorted(archives.entry_assets(archive, [".png", ".mp4"]), key=lambda a: a.name)
    assert render(image) == (None, "Open to preview")
    assert render(video) == (None, "Play to preview")
    archives.materialize(image.archive)  # (opened in the viewer)
    picture, _note = render(image)
    assert picture is not None and not picture.isNull()


def test_an_archive_card_shows_the_preview_inside_and_its_count(tmp_path, qapp):
    from core.thumbnail_manager import archive_label, disk_cache_path

    archive = make_zip(tmp_path / "pack.zip", [Entry("cover.png", png_bytes(color="blue")), Entry("model.obj", b"v 0 0 0")])
    (card,) = AssetScanner(supported_extensions=[".zip"]).scan(tmp_path)
    assert card.is_archive and not card.in_archive
    picture, _note = render(card)
    assert picture is not None and picture.pixelColor(picture.width() // 2, picture.height() // 2).blue() > 200
    assert archive_label(card) == "ZIP · 2"
    assert disk_cache_path(card, 110) is not None

    make_zip(archive, [Entry("lesson.mp4", b"v"), Entry("lesson.srt", b"s")])
    (card,) = AssetScanner(supported_extensions=[".zip"]).scan(tmp_path)
    assert render(card) == (None, "MP4, SRT")


def test_a_damaged_archive_card_says_so(tmp_path, qapp):
    (tmp_path / "broken.zip").write_bytes(b"not a zip at all")
    (card,) = AssetScanner(supported_extensions=[".zip"]).scan(tmp_path)
    picture, note = render(card)
    assert picture is None and "Damaged" in note


def test_entry_thumbnails_are_cached_by_content(tmp_path):
    from core.thumbnail_manager import disk_cache_path

    archive = make_zip(tmp_path / "pack.zip", [Entry("a.png", b"one")])
    (first,) = archives.entry_assets(archive, [".png"])
    archives.forget_listings()
    make_zip(archive, [Entry("a.png", b"two")])
    (second,) = archives.entry_assets(archive, [".png"])
    assert disk_cache_path(first, 110) != disk_cache_path(second, 110)


# ----------------------------------------------------------------------
# Scanning, details, search, tags, the snapshot
# ----------------------------------------------------------------------
def scan(folders, extensions, archive_contents=True):
    from ui.library_state import ScanWorker

    worker = ScanWorker(folders, extensions, archive_contents=archive_contents)
    results = []
    worker.finished.connect(lambda assets, info: results.append((assets, info)))
    worker.failed.connect(lambda error: results.append(error))
    worker.run()  # (on this thread: the signals are delivered directly)
    assert results and not isinstance(results[0], str), results
    return results[0]


EXTS = [".png", ".jpg", ".obj", ".zip"]


def test_a_scan_lists_files_inside_archives_with_database_rows(tmp_path, qapp):
    library = tmp_path / "library"
    sample_zip(library / "packs" / "pack.zip")
    (library / "loose.png").write_bytes(png_bytes())
    (library / "broken.zip").write_bytes(b"nope")
    assets, info = scan([library], EXTS)
    names = sorted(a.name for a in assets)
    assert names == ["broken.zip", "loose.png", "moss.png", "pack.zip", "preview.jpg", "rock.png"]
    assert all(str(a.path) in info["ids"] for a in assets)

    assets, _info = scan([library], EXTS, archive_contents=False)
    assert sorted(a.name for a in assets) == ["broken.zip", "loose.png", "pack.zip"]


def test_an_opened_archive_lists_its_own_files(tmp_path, qapp):
    archive = sample_zip(tmp_path / "pack.zip")
    assets, _info = scan([archive], EXTS, archive_contents=False)
    assert sorted(a.name for a in assets) == ["moss.png", "preview.jpg", "rock.png"]


def test_tags_on_files_inside_survive_a_rescan_and_a_move(tmp_path, qapp):
    from core.tag_repository import get_repository

    library = tmp_path / "library"
    sample_zip(library / "pack.zip")
    assets, info = scan([library], EXTS)
    rock = next(a for a in assets if a.name == "rock.png")
    repo = get_repository()
    tag = repo.create_custom_tag("Stone")
    repo.add_tags([info["ids"][str(rock.path)]], [tag.id])

    assets, info = scan([library], EXTS)
    rock = next(a for a in assets if a.name == "rock.png")
    assert [t.name for t in info["tags"][info["ids"][str(rock.path)]] if t.kind == "custom"] == ["Stone"]

    # The archive moves: its tagged file is matched by size and checksum, and keeps the tag
    (library / "moved").mkdir()
    os.replace(library / "pack.zip", library / "moved" / "pack.zip")
    archives.forget_listings()
    assets, info = scan([library], EXTS)
    rock = next(a for a in assets if a.name == "rock.png")
    assert "moved" in str(rock.path)
    assert [t.name for t in info["tags"][info["ids"][str(rock.path)]] if t.kind == "custom"] == ["Stone"]
    repo.close_thread_connection()


def test_search_finds_files_inside_archives(tmp_path, qapp):
    from core.asset_filter import filter_assets
    from core.search_query import parse_query

    assets, _info = scan([sample_zip(tmp_path / "lib" / "pack.zip").parent], EXTS)
    assert [a.name for a in filter_assets(assets, query=parse_query("moss"))] == ["moss.png"]
    assert sorted(a.name for a in filter_assets(assets, mode="exts", extensions=frozenset({".zip"}))) == ["pack.zip"]


def test_image_details_are_read_from_inside(tmp_path):
    from core.metadata import read_details

    archive = make_zip(tmp_path / "pack.zip", [Entry("wide.png", png_bytes((640, 128)), DEFLATE64)])
    (asset,) = archives.entry_assets(archive, [".png"])
    meta = read_details(str(asset.path), ".png")
    assert (meta.width, meta.height) == (640, 128)


def test_duplicates_leave_files_inside_archives_alone(tmp_path):
    from core.duplicates import find_duplicates

    data = png_bytes()
    archive = make_zip(tmp_path / "pack.zip", [Entry("a.png", data), Entry("b/a.png", data)])
    assert find_duplicates(archives.entry_assets(archive, [".png"])) == []


def test_the_snapshot_keeps_files_inside_archives(tmp_path):
    from core.library_snapshot import load_snapshot, save_snapshot

    archive = sample_zip(tmp_path / "lib" / "pack.zip")
    assets = archives.entry_assets(archive, [".png"])
    ids = {str(a.path): n for n, a in enumerate(assets, 1)}
    save_snapshot([tmp_path / "lib"], [".png"], assets, ids, archive_contents=True)
    assert load_snapshot([tmp_path / "lib"], [".png"], archive_contents=False) is None
    loaded, loaded_ids = load_snapshot([tmp_path / "lib"], [".png"], archive_contents=True)
    assert loaded == assets and loaded_ids == ids
    assert archives.read_bytes(loaded[0].archive)  # (readable without listing the archive again)


def test_live_updates_replace_an_edited_archives_files(tmp_path):
    from core.library_watcher import rescan_folders

    library = tmp_path / "library"
    archive = make_zip(library / "pack.zip", [Entry("old.png", png_bytes())])
    (library / "keep").mkdir()
    (library / "keep" / "other.png").write_bytes(png_bytes())
    before = AssetScanner(supported_extensions=EXTS).scan(library)
    before += archives.expand_archives(before, EXTS)
    assert sorted(a.name for a in before) == ["old.png", "other.png", "pack.zip"]

    make_zip(archive, [Entry("new.png", png_bytes())])
    os.utime(archive, (time.time() + 5, time.time() + 5))
    result = rescan_folders([library], EXTS, set(), archive_contents=True)
    assert sorted(a.name for a in result.merge_into(before)) == ["new.png", "other.png", "pack.zip"]
