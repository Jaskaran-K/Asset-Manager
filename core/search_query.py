"""Search filters on file details, typed into the search box next to the file name text.

    is:fav            favorites                 rating:3   3 stars or more (rating:=3, rating:<2 ...)
    ext:png           file type (ext:png,jpg)   size:>10mb  file size (b, kb, mb, gb; default mb)
    res:>=2048        larger side in pixels (k = 1024, so res:4k = 4096)
    dur:<30s          video length (s, m, h, or 1:30)
    poly:<10k         3D model faces (k = 1,000, m = 1,000,000)

Comparisons: = < <= > >=; a bare number means "at least" for rating and res, and
"equal" otherwise. Files whose details aren't known yet (still being read) don't
match a res / dur / poly filter.
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .scanner import AssetItem
from .tag_repository import NO_MARKS, AssetMeta, Marks

_TOKEN = re.compile(r"(?<!\S)(is|rating|ext|type|size|res|dur|poly):(\S*)", re.IGNORECASE)
_COMPARISON = re.compile(r"^(<=|>=|<|>|=)?(.+)$")
_OPS: dict[str, Callable[[float, float], bool]] = {
    "=": operator.eq, "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
}
_SIZE_UNITS = {"b": 1, "kb": 1024, "k": 1024, "mb": 1024 ** 2, "m": 1024 ** 2, "gb": 1024 ** 3, "g": 1024 ** 3}


@dataclass(frozen=True)
class DetailFilter:
    field: str  # "favorite", "rating", "ext", "size", "res", "dur", "poly"
    op: str
    value: object

    def matches(self, asset: AssetItem, marks: Marks, meta: Optional[AssetMeta]) -> bool:
        if self.field == "favorite":
            return marks.favorite
        if self.field == "ext":
            return asset.extension.lstrip(".") in self.value
        if self.field == "rating":
            actual: Optional[float] = marks.rating
        elif self.field == "size":
            actual = asset.size_bytes
        elif meta is None:
            return False
        elif self.field == "res":
            actual = max(meta.width or 0, meta.height or 0) or None
        elif self.field == "dur":
            actual = meta.duration_ms
        else:
            actual = meta.poly_count
        return actual is not None and _OPS[self.op](actual, self.value)


@dataclass
class ParsedQuery:
    text: str = ""  # what's left: matched against file names
    filters: list[DetailFilter] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def uses_marks(self) -> bool:
        return any(f.field in ("favorite", "rating") for f in self.filters)

    @property
    def uses_meta(self) -> bool:
        return any(f.field in ("res", "dur", "poly") for f in self.filters)

    def matches(self, asset: AssetItem, marks: Marks = NO_MARKS, meta: Optional[AssetMeta] = None) -> bool:
        return all(f.matches(asset, marks, meta) for f in self.filters)


def _number(text: str, units: dict[str, float], default_unit: float = 1) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]*)", text.lower())
    if not match or (match.group(2) and match.group(2) not in units):
        raise ValueError(text)
    return float(match.group(1)) * (units[match.group(2)] if match.group(2) else default_unit)


def _duration_ms(text: str) -> float:
    if ":" in text:  # 1:30 or 1:02:03
        seconds = 0.0
        for part in text.split(":"):
            seconds = seconds * 60 + float(part)
        return seconds * 1000
    return _number(text, {"s": 1000, "sec": 1000, "m": 60_000, "min": 60_000, "h": 3_600_000}, 1000)


def _parse_filter(key: str, raw: str) -> DetailFilter:
    key = key.lower()
    if key == "is":
        if raw.lower() in ("fav", "favorite", "favourite", "starred"):
            return DetailFilter("favorite", "=", True)
        raise ValueError(raw)
    if key in ("ext", "type"):
        exts = frozenset(e.strip().lstrip(".").lower() for e in raw.split(",") if e.strip())
        if not exts:
            raise ValueError(raw)
        return DetailFilter("ext", "=", exts)
    match = _COMPARISON.match(raw)
    if not match:
        raise ValueError(raw)
    op, value = match.group(1), match.group(2)
    if key == "rating":
        number, default_op = float(value), ">="
        if not 0 <= number <= 5:
            raise ValueError(raw)
    elif key == "size":
        number, default_op = _number(value, _SIZE_UNITS, 1024 ** 2), "="
    elif key == "res":
        number, default_op = _number(value, {"k": 1024, "px": 1}), ">="
    elif key == "dur":
        number, default_op = _duration_ms(value), "="
    else:  # poly
        number, default_op = _number(value, {"k": 1000, "m": 1_000_000}), "="
    return DetailFilter(key, op or default_op, number)


def parse_query(text: str) -> ParsedQuery:
    """Split the search text (tag tokens already removed) into detail filters and name text."""
    result = ParsedQuery()
    for match in _TOKEN.finditer(text):
        key, raw = match.group(1), match.group(2)
        if not raw:
            continue  # still being typed
        try:
            result.filters.append(_parse_filter(key, raw))
        except ValueError:
            result.errors.append(match.group(0))
    result.text = " ".join(_TOKEN.sub(" ", text).split())
    return result
