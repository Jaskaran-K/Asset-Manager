"""Search filters on file details, typed into the search box next to the file name text.

    is:fav            favorites                 rating:3   3 stars or more (rating:=3, rating:<2 ...)
    is:alpha          alphas: grey height images (core.alphas)
    ext:png           file type (ext:png,jpg)   size:>10mb  file size (b, kb, mb, gb; default mb)
    res:>=2048        larger side in pixels (k = 1024, so res:4k = 4096)
    dur:<30s          video length (s, m, h, or 1:30)
    poly:<10k         3D model faces (k = 1,000, m = 1,000,000)
    color:red         pictures with that colour on 15% of them or more (core.picture_look.COLOR_NAMES,
                      or an exact one: color:#c04020); colour: works too
    similar:123       the picture with database id 123 and its look-alikes (Find Similar; the search
                      bar shows it as a chip, and the grid lists them closest first)

Comparisons: = < <= > >=; a bare number means "at least" for rating and res, and
"equal" otherwise. "Equal" sizes, lengths and face counts match to the precision
typed: size:10mb is 9.5-10.5 MB, size:10.2mb is 10.15-10.25 MB, poly:10k is
9,500-10,500. A minus in front excludes: -ext:png, -is:fav. Files whose details
aren't known yet (still being read) don't match a res / dur / poly filter, with
or without the minus; pictures not looked at yet don't match a color filter.

Name text: every word must be in the file name, in any order ("rock wall" finds
Wall_Rock_02.png); "quoted words" must appear together.

Tag tokens (tag:name, -tag:name, tag:a|b) are parsed by the search bar
(ui.tag_widgets); their grammar is here so saved searches can be updated when a
tag is renamed (rename_tag_in_query).
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .picture_look import Look, parse_color
from .scanner import AssetItem
from .tag_repository import NO_MARKS, AssetMeta, Marks

_TOKEN = re.compile(r"(?<!\S)(-?)(is|rating|ext|type|size|res|dur|poly|colou?r|similar):(\S*)", re.IGNORECASE)
_WORD = re.compile(r'"([^"]*)"|(\S+)')

# Tag tokens:  tag:name   tag:"two words"   -tag:name (must not have)   tag:a|b|"c d" (any of)
TAG_NAME = r'(?:"[^"]+"|[^\s|"]+)'
TAG_TOKEN = re.compile(rf'(?<!\S)(-?)tag:({TAG_NAME}(?:\|{TAG_NAME})*)\|?(?!\S)', re.IGNORECASE)
_COMPARISON = re.compile(r"^(<=|>=|<|>|=)?(.+)$")
_META_FIELDS = ("res", "dur", "poly", "alpha")  # read from inside the files, in the background
_OPS: dict[str, Callable[[float, float], bool]] = {
    "=": operator.eq, "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
}
_SIZE_UNITS = {"b": 1, "kb": 1024, "k": 1024, "mb": 1024 ** 2, "m": 1024 ** 2, "gb": 1024 ** 3, "g": 1024 ** 3}


@dataclass(frozen=True)
class DetailFilter:
    field: str  # "favorite", "alpha", "rating", "ext", "size", "res", "dur", "poly", "color"
    op: str
    value: object
    negate: bool = False  # typed with a minus: files that don't match
    tolerance: float = 0  # "=" matches value +- tolerance (half a step of the last digit typed)

    def matches(self, asset: AssetItem, marks: Marks, meta: Optional[AssetMeta], look: Optional[Look] = None) -> bool:
        if self.field == "color":
            return look is not None and look.matches_color(self.value) != self.negate  # (not looked at: unknown)
        if meta is None and self.field in _META_FIELDS:
            return False  # not read yet: unknown either way
        return self._test(asset, marks, meta) != self.negate

    def _test(self, asset: AssetItem, marks: Marks, meta: Optional[AssetMeta]) -> bool:
        if self.field == "favorite":
            return marks.favorite
        if self.field == "alpha":
            return bool(meta.alpha)
        if self.field == "ext":
            return asset.extension.lstrip(".") in self.value
        if self.field == "rating":
            actual: Optional[float] = marks.rating
        elif self.field == "size":
            actual = asset.size_bytes
        elif self.field == "res":
            actual = max(meta.width or 0, meta.height or 0) or None
        elif self.field == "dur":
            actual = meta.duration_ms
        else:
            actual = meta.poly_count
        if actual is None:
            return False
        if self.op == "=" and self.tolerance:
            return self.value - self.tolerance <= actual < self.value + self.tolerance
        return _OPS[self.op](actual, self.value)


@dataclass
class ParsedQuery:
    text: str = ""  # what's left: matched against file names
    words: list[str] = field(default_factory=list)  # text as casefolded words / "quoted phrases"
    filters: list[DetailFilter] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    similar_to: Optional[int] = None  # similar:<id>: that picture and its look-alikes

    @property
    def uses_marks(self) -> bool:
        return any(f.field in ("favorite", "rating") for f in self.filters)

    @property
    def uses_meta(self) -> bool:
        return any(f.field in _META_FIELDS for f in self.filters)

    @property
    def uses_looks(self) -> bool:
        return self.similar_to is not None or any(f.field == "color" for f in self.filters)

    def name_matches(self, name: str) -> bool:
        folded = name.casefold()
        return all(word in folded for word in self.words)

    def matches(
        self, asset: AssetItem, marks: Marks = NO_MARKS, meta: Optional[AssetMeta] = None, look: Optional[Look] = None
    ) -> bool:
        return all(f.matches(asset, marks, meta, look) for f in self.filters)


def _number(text: str, units: dict[str, float], default_unit: float = 1) -> tuple[float, float]:
    """(value, size of a step in the last digit typed): "10mb" -> (10 MB, 1 MB), "10.5mb" -> (10.5 MB, 0.1 MB)."""
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?\s*([a-z]*)", text.lower())
    if not match or (match.group(3) and match.group(3) not in units):
        raise ValueError(text)
    whole, decimals, unit = match.group(1), match.group(2) or "", match.group(3)
    scale = units[unit] if unit else default_unit
    return float(f"{whole}.{decimals or 0}") * scale, scale / 10 ** len(decimals)


def _duration_ms(text: str) -> tuple[float, float]:
    if ":" in text:  # 1:30 or 1:02:03
        seconds = 0.0
        for part in text.split(":"):
            seconds = seconds * 60 + float(part)
        return seconds * 1000, 1000
    return _number(text, {"s": 1000, "sec": 1000, "m": 60_000, "min": 60_000, "h": 3_600_000}, 1000)


def _parse_filter(key: str, raw: str, negate: bool = False) -> DetailFilter:
    key = key.lower()
    if key == "is":
        if raw.lower() in ("fav", "favorite", "favourite", "starred"):
            return DetailFilter("favorite", "=", True, negate)
        if raw.lower() in ("alpha", "alphas", "height", "heightmap"):
            return DetailFilter("alpha", "=", True, negate)
        raise ValueError(raw)
    if key in ("color", "colour"):
        return DetailFilter("color", "=", parse_color(raw), negate)
    if key in ("ext", "type"):
        exts = frozenset(e.strip().lstrip(".").lower() for e in raw.split(",") if e.strip())
        if not exts:
            raise ValueError(raw)
        return DetailFilter("ext", "=", exts, negate)
    match = _COMPARISON.match(raw)
    if not match:
        raise ValueError(raw)
    op, value = match.group(1), match.group(2)
    step = 0.0  # exact: ratings, and resolutions (res:=4k is 4096 pixels)
    if key == "rating":
        number, default_op = float(value), ">="
        if not 0 <= number <= 5:
            raise ValueError(raw)
    elif key == "size":
        (number, step), default_op = _number(value, _SIZE_UNITS, 1024 ** 2), "="
    elif key == "res":
        (number, _step), default_op = _number(value, {"k": 1024, "px": 1}), ">="
    elif key == "dur":
        (number, step), default_op = _duration_ms(value), "="
    else:  # poly
        (number, step), default_op = _number(value, {"k": 1000, "m": 1_000_000}), "="
    return DetailFilter(key, op or default_op, number, negate, step / 2)


def _words(text: str) -> list[str]:
    words = (" ".join((quoted or plain).strip('"').split()).casefold() for quoted, plain in _WORD.findall(text))
    return [w for w in words if w]


def parse_query(text: str) -> ParsedQuery:
    """Split the search text (tag tokens already removed) into detail filters and name text."""
    result = ParsedQuery()
    for match in _TOKEN.finditer(text):
        minus, key, raw = match.groups()
        if not raw:
            continue  # still being typed
        if key.lower() == "similar":
            if raw.isdigit() and not minus:
                result.similar_to = int(raw)
            else:
                result.errors.append(match.group(0))
            continue
        try:
            result.filters.append(_parse_filter(key, raw, negate=bool(minus)))
        except ValueError:
            result.errors.append(match.group(0))
    result.text = " ".join(_TOKEN.sub(" ", text).split())
    result.words = _words(result.text)
    return result


def quote_tag_name(name: str) -> str:
    return f'"{name}"' if re.search(r'[\s|]', name) else name


def rename_tag_in_query(query: str, old: str, new: str) -> str:
    """Saved search text with tag:old (in any tag token, any case) changed to tag:new."""
    folded = " ".join(old.split()).casefold()

    def renamed(match: re.Match) -> str:
        names = [
            quote_tag_name(new) if name.strip('"').casefold() == folded else name
            for name in re.findall(TAG_NAME, match.group(2))
        ]
        return f"{match.group(1)}tag:{'|'.join(names)}"

    return TAG_TOKEN.sub(renamed, query)
