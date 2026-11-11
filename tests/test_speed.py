"""Speed tests: a made-up library of 50,000 files (names, types, tags, favourites, ratings and details), and
how long the things you wait for take on it. Each has a budget; going over it fails, so a change that makes
the app noticeably slower is caught. Not in the normal run (pytest.ini): GitHub runs them as a step of their
own (python -m pytest -m speed), and the timings are printed as a table (and shown on the run's page).

Budgets are about 3 times what GitHub's machines take (they're slower than a desktop), so only a real
slowdown fails, not a busy machine. When a change makes something slower on purpose (more work for a good
reason), its budget is raised here, in the same commit, saying why.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.scanner import AssetItem, determine_asset_group
from core.tag_repository import AssetMeta, Marks, TagRepository, default_tag_name

pytestmark = pytest.mark.speed

FILES = 50_000
TAGS = 40
TAG_LINKS = 60_000
# Seconds (the best of a few runs): about 7.5 times what a desktop takes (GitHub's machines are 2-3 times
# slower, then 3 times that), at least 0.1 s (timer noise). See the module docstring.
BUDGETS = {
    "app imports": 7.0,  # (a desktop: 0.9 s)
    "saved scan: write": 0.3,  # (0.04 s)
    "saved scan: read": 1.5,  # (0.19 s)
    "grid: fill with every file": 0.1,  # (0.006 s)
    "sidebar counts (with a tag category)": 0.6,  # (0.08 s)
    "tag search: one tag": 0.15,  # (0.017 s)
    "tag search: any of two, not a third": 0.2,  # (0.028 s)
    "tag search: a tag and a name": 0.1,  # (0.009 s)
}
SEARCHES = {  # what's typed: its budget (a desktop: 0.014-0.040 s)
    "skull": 0.3, "head ref": 0.3, "ext:fbx": 0.3, "poly:>100000": 0.3, "res:>2000": 0.3,
    "rating:>=3": 0.3, "is:favorite": 0.3, "dragon ext:obj size:>10mb": 0.3,
}
TIMINGS: dict[str, tuple[float, float]] = {}  # name: (seconds, budget)

WORDS = ["skull", "head", "ref", "dragon", "armor", "rock", "wood", "metal", "hand", "eye", "tree", "crate", "katana",
         "brush", "alpha", "sculpt", "base", "mesh", "texture", "final", "wip", "v2", "hero", "prop", "creature"]
TYPES = [".png"] * 10 + [".jpg"] * 6 + [".tif", ".exr", ".psd", ".fbx", ".fbx", ".obj", ".obj", ".pdf", ".mp4", ".zip", ".ztl"]


def timed(name: str, work, budget: float, repeat: int = 3) -> float:
    best = float("inf")
    for _ in range(repeat):
        start = time.perf_counter()
        work()
        best = min(best, time.perf_counter() - start)
    TIMINGS[name] = (best, budget)
    assert best <= budget, f"{name}: {best:.3f} s, over its budget of {budget} s"
    return best


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    """The made-up library: (assets, ids, repository, tag ids, marks, meta, folded tag names by id)."""
    rng = random.Random(7)
    root = tmp_path_factory.mktemp("speed")
    assets = []
    for i in range(FILES):
        ext = rng.choice(TYPES)
        name = "_".join(rng.sample(WORDS, 3)) + f"_{i}{ext}"
        path = Path(f"D:/Library/{rng.choice(WORDS)}/{rng.choice(WORDS)}") / name
        assets.append(AssetItem(path, name, ext, rng.randint(1_000, 400_000_000), 1_700_000_000.0 + i,
                                determine_asset_group(ext), default_tag_name(ext)))
    repo = TagRepository(root / "library.db")
    ids = repo.sync_assets(assets)
    tags = [repo.create_custom_tag(f"Tag {n}").id for n in range(TAGS)]
    asset_ids = list(ids.values())
    by_tag: dict[int, list[int]] = {}
    for _ in range(TAG_LINKS):
        by_tag.setdefault(rng.choice(tags), []).append(rng.choice(asset_ids))
    for tag_id, tagged in by_tag.items():
        repo.add_tags(list(dict.fromkeys(tagged)), [tag_id])
    marks = {i: Marks(favorite=rng.random() < 0.02, rating=rng.choice([0, 0, 0, 3, 4, 5])) for i in asset_ids}
    meta = {}
    for asset in assets:
        asset_id = ids[str(asset.path)]
        if asset.extension in (".fbx", ".obj"):
            meta[asset_id] = AssetMeta(poly_count=rng.randint(100, 5_000_000))
        elif asset.extension in (".png", ".jpg", ".tif", ".exr", ".psd"):
            side = rng.choice([512, 1024, 2048, 4096])
            meta[asset_id] = AssetMeta(width=side, height=side)
    names = {tag_id: f"tag {n}" for n, tag_id in enumerate(tags)}
    tag_names: dict[int, frozenset[str]] = {}
    for tag_id, tagged in by_tag.items():
        for asset_id in tagged:
            tag_names[asset_id] = tag_names.get(asset_id, frozenset()) | {names[tag_id]}
    yield assets, ids, repo, tags, marks, meta, tag_names
    repo.close() if hasattr(repo, "close") else None


def test_app_imports():
    """Python, Qt and the app's modules, in a fresh process (what start-up waits for first)."""
    root = Path(__file__).resolve().parent.parent

    def start():
        subprocess.run([sys.executable, "-c", "import main"], cwd=root, check=True,
                       env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, capture_output=True)

    timed("app imports", start, BUDGETS["app imports"], repeat=2)


def test_saved_scan(library):
    from core.library_snapshot import load_snapshot, save_snapshot

    assets, ids = library[0], library[1]
    folders, extensions = [Path("D:/Library")], sorted(set(TYPES))
    timed("saved scan: write", lambda: save_snapshot(folders, extensions, assets, ids), BUDGETS["saved scan: write"])
    loaded = []
    timed("saved scan: read", lambda: loaded.append(load_snapshot(folders, extensions)), BUDGETS["saved scan: read"])
    assert len(loaded[-1][0]) == FILES


def test_grid_fill(qapp, library):
    from ui.asset_grid import AssetListModel

    model = AssetListModel(lambda asset: None, lambda asset: "")
    timed("grid: fill with every file", lambda: model.set_assets(library[0]), BUDGETS["grid: fill with every file"])
    assert model.rowCount() == FILES


def test_searches(library):
    from core.asset_filter import filter_assets
    from core.search_query import parse_query

    assets, ids, _repo, _tags, marks, meta, _names = library
    for text, budget in SEARCHES.items():
        query = parse_query(text)
        assert not query.errors, text
        found = []
        timed(f"search: {text}", lambda: found.append(filter_assets(assets, query=query, ids=ids, marks=marks, meta=meta)), budget)
        assert found[-1] is not None


def test_tag_searches(library):
    repo, tags = library[2], library[3]
    timed("tag search: one tag", lambda: repo.find_assets(all_of=[tags[0]]), BUDGETS["tag search: one tag"])
    timed("tag search: any of two, not a third",
          lambda: repo.find_assets(any_of=[[tags[1], tags[2]]], none_of=[tags[3]]), BUDGETS["tag search: any of two, not a third"])
    timed("tag search: a tag and a name", lambda: repo.find_assets(all_of=[tags[4]], text="skull"),
          BUDGETS["tag search: a tag and a name"])


def test_sidebar_counts(qapp, library):
    from core import settings
    from ui.library_sidebar import LibrarySidebar

    assets, ids, repo, _tags, marks, _meta, tag_names = library
    categories = settings.get_categories()
    categories.append({"name": "Tagged", "extensions": [".fbx"], "tags": ["Tag 1", "Tag 2"], "subcategories": []})
    settings.set_categories(categories)
    sidebar = LibrarySidebar(repo)
    favourite = lambda asset: marks[ids[str(asset.path)]].favorite  # noqa: E731
    timed("sidebar counts (with a tag category)", lambda: sidebar.update_counts(assets, favourite, ids, tag_names),
          BUDGETS["sidebar counts (with a tag category)"])
    sidebar.deleteLater()


def test_zz_report():
    """The table of timings (last: pytest runs a module's tests in order)."""
    lines = [f"Speed on a made-up library of {FILES:,} files ({sys.platform}, Python {sys.version.split()[0]})", "",
             "| What | Took | Budget | Share of budget |", "|---|---:|---:|---:|"]
    for name, (took, budget) in TIMINGS.items():
        lines.append(f"| {name} | {took * 1000:,.0f} ms | {budget * 1000:,.0f} ms | {took / budget:.0%} |")
    report = "\n".join(lines)
    print("\n" + report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write(report + "\n")
