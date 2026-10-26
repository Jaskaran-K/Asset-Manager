"""Tag search with AND / OR / NOT, the search bar's token parsing, and saved searches."""

from __future__ import annotations

import pytest

from core.tag_repository import TagError, TagRepository
from tests.test_tag_repository import make_asset
from ui.tag_widgets import TagFilter, TagToken, filter_token, parse_search_text


@pytest.fixture
def lib():
    repo = TagRepository(":memory:")
    ids = repo.sync_assets([make_asset(p) for p in (r"C:\l\hero.fbx", r"C:\l\rock.fbx", r"C:\l\tree.png", r"C:\l\sky.png")])
    ids = {k.rsplit("\\", 1)[1]: v for k, v in ids.items()}
    tags = {n: repo.create_custom_tag(n) for n in ("Hero", "WIP", "Nature")}
    repo.add_tags([ids["hero.fbx"]], [tags["Hero"].id, tags["WIP"].id])
    repo.add_tags([ids["rock.fbx"], ids["tree.png"]], [tags["Nature"].id])
    repo.add_tags([ids["tree.png"]], [tags["WIP"].id])
    return repo, ids, {n: t.id for n, t in tags.items()}


def names(ids, found):
    return sorted(n for n, i in ids.items() if i in found)


def test_and_or_not(lib):
    repo, ids, t = lib
    assert names(ids, repo.find_assets(all_of=[t["WIP"]])) == ["hero.fbx", "tree.png"]
    assert names(ids, repo.find_assets(all_of=[t["WIP"], t["Nature"]])) == ["tree.png"]
    assert names(ids, repo.find_assets(any_of=[[t["Hero"], t["Nature"]]])) == ["hero.fbx", "rock.fbx", "tree.png"]
    assert names(ids, repo.find_assets(none_of=[t["WIP"]])) == ["rock.fbx", "sky.png"]
    assert names(ids, repo.find_assets(any_of=[[t["Hero"], t["Nature"]]], none_of=[t["WIP"]])) == ["rock.fbx"]
    assert names(ids, repo.find_assets(none_of=[t["WIP"], t["Nature"]], text="SK")) == ["sky.png"]
    assert repo.find_assets() == set()


@pytest.mark.parametrize("text, tokens, rest", [
    ("tag:Hero rock", [TagToken(("Hero",))], "rock"),
    ('-tag:"Work in progress"', [TagToken(("Work in progress",), True)], ""),
    ('tag:a|"b c"|d', [TagToken(("a", "b c", "d"))], ""),
    ("tag:a|", [TagToken(("a",))], ""),  # trailing | while typing the next name
    ('rock tag:"two wo', [], "rock"),  # unfinished quoted token doesn't filter names yet
    ("mytag:x", [], "mytag:x"),  # only whole words are tokens
])
def test_parse_search_text(text, tokens, rest):
    assert parse_search_text(text) == (tokens, rest)


def test_filter_tokens_round_trip(lib):
    repo, _ids, _t = lib
    hero, nature = repo.find_tag("Hero"), repo.find_tag("Nature")
    spaced = repo.create_custom_tag("Old stuff")
    for f in (TagFilter((hero,)), TagFilter((hero, spaced), exclude=True), TagFilter((nature, spaced))):
        tokens, rest = parse_search_text(filter_token(f))
        assert tokens == [TagToken(tuple(t.name for t in f.tags), f.exclude)] and rest == ""


def test_search_bar_builds_queries(lib, qapp):
    from ui.tag_widgets import TagSearchBar

    repo, _ids, t = lib
    bar = TagSearchBar()
    bar.set_available_tags(repo.list_tags())
    bar.edit.setText("tag:hero -tag:wip tag:nature|hero rock")
    bar._commit_tokens()
    q = bar.tag_query()
    assert q.all_of == (t["Hero"],) and q.none_of == (t["WIP"],) and q.any_of == ((t["Nature"], t["Hero"]),)
    assert bar.text() == "rock"
    assert bar.query_text() == "tag:Hero -tag:WIP tag:Nature|Hero rock"

    bar.toggle_filter(bar.filters()[1])  # click the "not WIP" chip
    assert bar.tag_query().none_of == () and set(bar.tag_query().all_of) == {t["Hero"], t["WIP"]}

    saved = bar.query_text()
    bar.clear()
    bar.set_query_text(saved)  # opening a collection
    assert bar.query_text() == saved


def test_completion_finishes_an_or_group(lib, qapp):
    from ui.tag_widgets import TagSearchBar

    repo, _ids, t = lib
    bar = TagSearchBar()
    bar.set_available_tags(repo.list_tags())
    bar.edit.setText("tag:Hero|Nat")
    bar._on_completion("Nature")
    assert bar.tag_query().any_of == ((t["Hero"], t["Nature"]),)


def test_saved_searches(lib):
    repo, _ids, _t = lib
    saved = repo.save_search("  Hero  props ", "tag:Hero rock")
    assert saved.name == "Hero props"
    with pytest.raises(TagError):
        repo.save_search("hero PROPS", "x")  # names are unique, ignoring case
    with pytest.raises(TagError):
        repo.save_search("Empty", "  ")
    repo.rename_saved_search(saved.id, "Heroes")
    assert [(s.name, s.query) for s in repo.list_saved_searches()] == [("Heroes", "tag:Hero rock")]
    repo.delete_saved_search(saved.id)
    assert repo.list_saved_searches() == []
