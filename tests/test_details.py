"""Favorites / ratings, file detail reading, and detail filters in the search box."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from PIL import Image

from core.metadata import read_cost, read_details
from core.scanner import AssetScanner
from core.search_query import parse_query
from core.tag_repository import NO_MARKS, AssetMeta, Marks, TagRepository
from tests.test_tag_repository import make_asset


# ----------------------------------------------------------------------
# Favorites & ratings
# ----------------------------------------------------------------------
def test_favorites_and_ratings(tmp_path):
    repo = TagRepository(":memory:")
    ids = list(repo.sync_assets([make_asset(r"C:\a.png"), make_asset(r"C:\b.png"), make_asset(r"C:\c.png")]).values())
    repo.set_favorite(ids[:2], True)
    repo.set_rating([ids[1]], 4)
    repo.set_rating([ids[2]], 9)  # clamped
    assert repo.marks_by_asset(ids) == {ids[0]: Marks(True, 0), ids[1]: Marks(True, 4), ids[2]: Marks(False, 5)}
    repo.set_favorite([ids[0]], False)
    repo.set_rating([ids[2]], 0)
    assert ids[0] not in repo.marks_by_asset(ids) and ids[2] not in repo.marks_by_asset(ids)

    export = tmp_path / "tags.json"
    repo.export_tags(export)
    entries = {Path(f["path"]).name: f for f in json.loads(export.read_text(encoding="utf-8"))["files"]}
    assert entries["b.png"]["favorite"] is True and entries["b.png"]["rating"] == 4
    fresh = TagRepository(":memory:")
    fresh.import_tags(export)
    new_ids = fresh.sync_assets([make_asset(r"C:\b.png")])
    assert fresh.marks_by_asset(list(new_ids.values())) == {new_ids[r"C:\b.png"]: Marks(True, 4)}


def test_favorite_files_are_relinked_when_moved(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.png").write_bytes(b"unique pixels")
    repo = TagRepository(":memory:")
    ids = repo.sync_assets(AssetScanner().scan(tmp_path))
    repo.set_rating(list(ids.values()), 5)  # rated, no tags
    repo.fill_missing_hashes()
    (tmp_path / "a" / "x.png").rename(tmp_path / "x_moved.png")
    assets = AssetScanner().scan(tmp_path)
    assert repo.relink_moved_assets(assets) == 1


# ----------------------------------------------------------------------
# Reading details
# ----------------------------------------------------------------------
def test_reads_image_sizes(tmp_path, qapp):
    png = tmp_path / "wide.png"
    Image.new("RGB", (640, 200)).save(png)
    assert read_details(str(png), ".png") == AssetMeta(width=640, height=200)

    psd = tmp_path / "deep.psd"  # header of a 16-bit PSD: Qt can't open it, the header reader can
    psd.write_bytes(b"8BPS" + struct.pack(">H6xHIIHH", 1, 3, 300, 4000, 16, 3) + b"\0" * 64)
    assert read_details(str(psd), ".psd") == AssetMeta(width=4000, height=300)

    hdr = tmp_path / "sky.hdr"
    hdr.write_bytes(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 1024 +X 2048\n" + b"\0" * 16)
    assert read_details(str(hdr), ".hdr") == AssetMeta(width=2048, height=1024)


def test_unreadable_files_have_empty_details(tmp_path, qapp):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"nope")
    assert read_details(str(broken), ".png") == AssetMeta()


def test_cheap_files_are_read_first():
    assert read_cost(".png") < read_cost(".mp4") < read_cost(".fbx") < read_cost(".pdf")


def test_details_are_reread_after_the_file_changes():
    repo = TagRepository(":memory:")
    asset = make_asset(r"C:\a.png")
    ids = repo.sync_assets([asset])
    todo = repo.assets_needing_meta(list(ids.values()))
    assert [row[0] for row in todo] == list(ids.values())
    asset_id, _path, _ext, key = todo[0]
    repo.store_meta({asset_id: (AssetMeta(width=10, height=20), key)})
    assert repo.assets_needing_meta([asset_id]) == []
    assert repo.meta_by_asset([asset_id]) == {asset_id: AssetMeta(width=10, height=20)}

    asset.modified_timestamp = 99.5  # edited
    repo.sync_assets([asset])
    assert repo.meta_by_asset([asset_id]) == {}
    assert len(repo.assets_needing_meta([asset_id])) == 1


# ----------------------------------------------------------------------
# Detail filters
# ----------------------------------------------------------------------
def big_texture():
    a = make_asset(r"C:\lib\rock_4k.png")
    a.size_bytes = 12 * 1024 * 1024
    return a


@pytest.mark.parametrize("query, marks, meta, expected", [
    ("is:fav", Marks(True, 0), None, True),
    ("is:fav", NO_MARKS, None, False),
    ("rating:3", Marks(False, 4), None, True),  # bare number = at least
    ("rating:=3", Marks(False, 4), None, False),
    ("rating:<2", NO_MARKS, None, True),
    ("ext:jpg,png", NO_MARKS, None, True),
    ("ext:fbx", NO_MARKS, None, False),
    ("size:>10mb", NO_MARKS, None, True),
    ("size:<10", NO_MARKS, None, False),  # default unit is MB
    ("res:4k", NO_MARKS, AssetMeta(4096, 4096), True),
    ("res:>4096", NO_MARKS, AssetMeta(4096, 2048), False),
    ("res:>=2048", NO_MARKS, None, False),  # not read yet: doesn't match
    ("dur:<30s", NO_MARKS, AssetMeta(duration_ms=12_000), True),
    ("dur:>1:30", NO_MARKS, AssetMeta(duration_ms=60_000), False),
    ("poly:<10k", NO_MARKS, AssetMeta(poly_count=9_500), True),
    ("poly:>1m", NO_MARKS, AssetMeta(poly_count=9_500), False),
])
def test_detail_filters(query, marks, meta, expected):
    parsed = parse_query(query)
    assert not parsed.errors and parsed.text == ""
    assert parsed.matches(big_texture(), marks, meta) is expected


def test_query_keeps_name_text_and_reports_mistakes():
    parsed = parse_query("rock res:>=2k size:lots ext: moss")
    assert parsed.text == "rock moss"
    assert parsed.errors == ["size:lots"]  # "ext:" is still being typed, not a mistake
    assert parsed.uses_meta and not parsed.uses_marks
