"""Which files a sidebar category shows: its file types, or its tags (any of them, or all of them).

A file shows in a category when its type is one of the category's file types OR it passes the
tag condition, so "Main Project" with FBX and the tag "Main" shows every FBX file plus anything
tagged Main. A main category also shows everything its subcategories show. Uncategorized shows
the files no category shows. Tags are kept by name, compared ignoring case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

TAG_MATCHES = ("any", "all")


def fold(name: str) -> str:
    """A tag name as compared: spaces collapsed, case ignored (as tag names are)."""
    return " ".join(name.split()).casefold()


@dataclass(frozen=True)
class Rule:
    """One category's own condition (without its subcategories)."""

    extensions: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()  # folded names
    match_all: bool = False  # True: every tag in tags; False: any of them
    labels: tuple[str, ...] = field(default=(), compare=False)  # the tag names as written, for tooltips

    def matches(self, extension: str, tag_names: frozenset[str]) -> bool:
        if extension in self.extensions:
            return True
        if not self.tags:
            return False
        return self.tags <= tag_names if self.match_all else not self.tags.isdisjoint(tag_names)


@dataclass(frozen=True)
class RowFilter:
    """A sidebar row: files that pass any of its rules (a main category's, then its subcategories')."""

    rules: tuple[Rule, ...] = ()

    @property
    def uses_tags(self) -> bool:
        return any(rule.tags for rule in self.rules)

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset().union(*(rule.extensions for rule in self.rules))

    def matches(self, extension: str, tag_names: frozenset[str]) -> bool:
        return any(rule.matches(extension, tag_names) for rule in self.rules)

    def describe(self) -> str:
        """The row's tooltip: its file types and tag conditions."""
        lines = []
        if self.extensions:
            lines.append(", ".join(sorted(self.extensions)))
        for rule in self.rules:
            if rule.tags:
                both = "all of " if rule.match_all and len(rule.tags) > 1 else ""
                lines.append(f"Tagged {both}{', '.join(rule.labels or sorted(rule.tags))}")
        return "\n".join(dict.fromkeys(lines)) or "No file types or tags assigned"


def rule_of(entry: Mapping[str, Any]) -> Rule:
    """The condition stored on one category or subcategory (core.settings)."""
    labels = tuple(dict.fromkeys(" ".join(t.split()) for t in entry.get("tags", []) if fold(t)))
    return Rule(
        frozenset(entry.get("extensions", [])),
        frozenset(fold(t) for t in labels),
        entry.get("tag_match") == "all",
        labels,
    )


def row_filter(category: Mapping[str, Any], *, with_subcategories: bool = True) -> RowFilter:
    """What a category's sidebar row shows: its own rule, plus its subcategories' for a main category."""
    rules = [rule_of(category)]
    if with_subcategories:
        rules += [rule_of(sub) for sub in category.get("subcategories", [])]
    return RowFilter(tuple(r for r in rules if r.extensions or r.tags))


def everything_in(categories: Sequence[Mapping[str, Any]]) -> RowFilter:
    """Files some category shows (the rest are Uncategorized)."""
    return RowFilter(tuple(rule for main in categories for rule in row_filter(main).rules))


def tag_names_by_id(tags: Mapping[int, Iterable[Any]]) -> dict[int, frozenset[str]]:
    """{asset id: its folded tag names} from {asset id: [Tag]}."""
    return {asset_id: frozenset(fold(t.name) for t in tag_list) for asset_id, tag_list in tags.items()}


def names_of(asset_id: Optional[int], tag_names: Mapping[int, frozenset[str]]) -> frozenset[str]:
    return tag_names.get(asset_id, frozenset()) if asset_id is not None else frozenset()


def renamed(tags: Sequence[str], old: str, new: str) -> list[str]:
    """A category's tag list after the tag old was renamed to new."""
    return list(dict.fromkeys(new if fold(t) == fold(old) else t for t in tags))


def without(tags: Sequence[str], name: str) -> list[str]:
    """A category's tag list after the tag name was deleted."""
    return [t for t in tags if fold(t) != fold(name)]
