"""Which files the grid shows: the sidebar row, tag search matches, file name words and detail
filters (is:fav, rating:, res: ...), applied in that order. A plain function of the library's
data, so it's tested without a window."""

from __future__ import annotations

from typing import Mapping, Optional, Sequence, Union

from .category_rules import RowFilter, Rule, names_of
from .scanner import AssetItem
from .picture_look import Look
from .search_query import ParsedQuery
from .tag_repository import NO_MARKS, AssetMeta, Marks


def filter_assets(
    assets: Sequence[AssetItem],
    *,
    mode: str = "all",
    extensions: Optional[frozenset[str]] = None,
    row: Optional[RowFilter] = None,
    categorized: Union[RowFilter, frozenset[str]] = RowFilter(),
    tag_names: Mapping[int, frozenset[str]] = {},  # noqa: B006
    tag_matches: Optional[set[int]] = None,
    query: Optional[ParsedQuery] = None,
    ids: Mapping[str, int] = {},  # noqa: B006 (read only)
    marks: Mapping[int, Marks] = {},  # noqa: B006
    meta: Mapping[int, AssetMeta] = {},  # noqa: B006
    looks: Mapping[int, Look] = {},  # noqa: B006
    similar: Optional[Mapping[int, float]] = None,
) -> list[AssetItem]:
    """The assets to show.

    mode: the sidebar row: "all", "category" (files row passes: core.category_rules), "exts"
    (files with one of extensions), "uncategorized" (files no category shows: categorized doesn't
    pass, or a set of file types) or "favorites". tag_names: {asset id: its folded tag names},
    for category rules with tags. tag_matches: ids of the files a tag search found, or None when
    no tag filter is active. ids: database ids by str(path), for tag_names, tag_matches, marks
    (favorites / ratings), meta (resolution, length, faces) and looks (colours: core.picture_look).
    similar: {id: closeness} of a picture and its look-alikes (Find Similar; the picture itself at 0):
    only those are shown, closest first.
    """
    filtered = list(assets)
    if mode == "category" and row is not None:
        filtered = [a for a in filtered if row.matches(a.extension, names_of(ids.get(str(a.path)), tag_names))]
    elif mode == "exts" and extensions is not None:
        filtered = [a for a in filtered if a.extension in extensions]
    elif mode == "uncategorized":
        if not isinstance(categorized, RowFilter):
            categorized = RowFilter((Rule(frozenset(categorized)),))
        filtered = [
            a for a in filtered if not categorized.matches(a.extension, names_of(ids.get(str(a.path)), tag_names))
        ]
    elif mode == "favorites":
        filtered = [a for a in filtered if marks.get(ids.get(str(a.path)), NO_MARKS).favorite]

    if tag_matches is not None:
        filtered = [a for a in filtered if ids.get(str(a.path)) in tag_matches]

    if query is not None and query.words:
        filtered = [a for a in filtered if query.name_matches(a.name)]

    if similar is not None:
        filtered = [a for a in filtered if ids.get(str(a.path)) in similar]
        filtered.sort(key=lambda a: similar[ids[str(a.path)]])

    if query is not None and query.filters:
        filtered = [
            a for a in filtered
            if query.matches(
                a, marks.get(asset_id := ids.get(str(a.path)), NO_MARKS), meta.get(asset_id), looks.get(asset_id)
            )
        ]
    return filtered
