"""core.asset_filter: what the grid shows for a sidebar row, tag matches, name words and detail filters."""

from __future__ import annotations

from core.asset_filter import filter_assets
from core.search_query import parse_query
from core.tag_repository import AssetMeta, Marks
from tests.test_tag_repository import make_asset

ASSETS = [make_asset(p) for p in (
    r"C:\lib\Rock_Wall_01.png", r"C:\lib\rock.fbx", r"C:\lib\notes.pdf", r"C:\lib\scan.xyz", r"C:\lib\sky.jpg",
)]
IDS = {str(a.path): i for i, a in enumerate(ASSETS, start=1)}


def names(found):
    return [a.name for a in found]


def test_sidebar_rows():
    assert names(filter_assets(ASSETS)) == names(ASSETS)
    assert names(filter_assets(ASSETS, mode="exts", extensions=frozenset({".png", ".jpg"}))) == ["Rock_Wall_01.png", "sky.jpg"]
    categorized = frozenset({".png", ".fbx", ".pdf", ".jpg"})
    assert names(filter_assets(ASSETS, mode="uncategorized", categorized=categorized)) == ["scan.xyz"]
    marks = {IDS[r"C:\lib\rock.fbx"]: Marks(favorite=True)}
    assert names(filter_assets(ASSETS, mode="favorites", ids=IDS, marks=marks)) == ["rock.fbx"]


def test_tag_matches_words_and_details_combine():
    tagged = {IDS[r"C:\lib\Rock_Wall_01.png"], IDS[r"C:\lib\rock.fbx"], IDS[r"C:\lib\sky.jpg"]}
    assert names(filter_assets(ASSETS, tag_matches=tagged, ids=IDS)) == ["Rock_Wall_01.png", "rock.fbx", "sky.jpg"]
    assert names(filter_assets(ASSETS, tag_matches=set(), ids=IDS)) == []  # (a tag search that found nothing)
    query = parse_query("wall rock")
    assert names(filter_assets(ASSETS, tag_matches=tagged, query=query, ids=IDS)) == ["Rock_Wall_01.png"]
    meta = {IDS[r"C:\lib\rock.fbx"]: AssetMeta(poly_count=500)}
    assert names(filter_assets(ASSETS, query=parse_query("rock poly:<1k"), ids=IDS, meta=meta)) == ["rock.fbx"]
    marks = {IDS[r"C:\lib\sky.jpg"]: Marks(rating=4)}
    assert names(filter_assets(ASSETS, query=parse_query("rating:4"), ids=IDS, marks=marks)) == ["sky.jpg"]


def test_files_not_in_the_database_only_match_without_tag_or_mark_filters():
    unknown = [make_asset(r"C:\elsewhere\new.png")]
    assert names(filter_assets(unknown, query=parse_query("new"), ids=IDS)) == ["new.png"]
    assert filter_assets(unknown, tag_matches={1}, ids=IDS) == []
    assert filter_assets(unknown, query=parse_query("is:fav"), ids=IDS) == []
