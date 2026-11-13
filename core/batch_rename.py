"""Batch rename: new names for many files from a pattern, checked before anything changes, and done so
it can be undone.

Pattern words: {name} the file's name now (without its type), {set} its texture set's name (core.
texture_sets; else its name), {map} what map it is, in a chosen style, {n} a counter ({n:3} = 001),
{folder} the folder it's in. Then a find and replace (ignoring case), and a letter case. The file type
stays, and a UDIM tile (".1001") is kept on the end if the pattern doesn't place it ({tile}).

Renaming moves the files on disk; their database rows are moved with them (TagRepository.
move_asset_paths), so tags, favorites, ratings and what's known about them stay. Files inside archives
can't be renamed.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .scanner import AssetItem
from .texture_sets import TextureSet, read_name

# {map} styles: the word for each kind of map (None: as it's written now)
MAP_STYLES: dict[str, Optional[dict[str, str]]] = {
    "keep": None,
    "substance": {"color": "BaseColor", "normal": "Normal", "roughness": "Roughness", "gloss": "Glossiness",
                  "metal": "Metallic", "ao": "AO", "height": "Height", "opacity": "Opacity", "emissive": "Emissive",
                  "specular": "Specular", "packed": "ORM"},
    "short": {"color": "BC", "normal": "N", "roughness": "R", "gloss": "G", "metal": "M", "ao": "AO", "height": "H",
              "opacity": "O", "emissive": "E", "specular": "S", "packed": "ORM"},
}
MAP_STYLE_NAMES = {"keep": "As they are", "substance": "BaseColor, Normal, Roughness ...", "short": "BC, N, R, M ..."}
CASES = ("keep", "lower", "upper")
_TOKEN = re.compile(r"\{(name|set|map|n|folder|tile)(?::(\d))?\}")
_BAD_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


@dataclass(frozen=True)
class RenameOptions:
    pattern: str = "{name}"
    find: str = ""
    replace: str = ""
    map_style: str = "keep"
    case: str = "keep"
    start: int = 1  # {n} starts at this


@dataclass
class RenameRow:
    asset: AssetItem
    new_name: str
    problem: str = ""  # why it can't be renamed ("" when it can)

    @property
    def changes(self) -> bool:
        return not self.problem and self.new_name != self.asset.name


@dataclass
class RenamePlan:
    rows: list[RenameRow] = field(default_factory=list)

    @property
    def renames(self) -> list[RenameRow]:
        return [row for row in self.rows if row.changes]

    @property
    def problems(self) -> list[RenameRow]:
        return [row for row in self.rows if row.problem]


def _map_word(asset: AssetItem, style: Optional[dict[str, str]]) -> str:
    found = read_name(asset.name)
    if found is None:
        return ""
    role, word, _set_name, _tile = found
    if style is None or role not in style:
        return word
    new = style[role]
    if role == "normal":  # (the OpenGL / DirectX one keep saying which they are)
        if re.search(r"(?i)open_?gl|(?<![a-z])gl(?![a-z])", asset.name):
            new += "_OpenGL"
        elif re.search(r"(?i)direct_?x|(?<![a-z])dx(?![a-z])", asset.name):
            new += "_DirectX"
    return new


def new_name(asset: AssetItem, options: RenameOptions, counter: int, texture_set: Optional[TextureSet] = None) -> str:
    """The name a file gets (its type kept)."""
    stem, ext = os.path.splitext(asset.name)
    found = read_name(asset.name)
    tile = found[3] if found else None
    style = MAP_STYLES.get(options.map_style)
    folder = Path(str(asset.path).split("|")[0]).parent.name if not asset.in_archive else asset.archive.folder.split("/")[-1]

    def fill(match: re.Match) -> str:
        word, width = match.group(1), match.group(2)
        if word == "name":
            return stem
        if word == "set":
            return texture_set.name if texture_set is not None else (found[2] if found else stem)
        if word == "map":
            return _map_word(asset, style)
        if word == "n":
            return str(counter).zfill(int(width or 1))
        if word == "folder":
            return folder
        return str(tile) if tile is not None else ""  # {tile}

    result = _TOKEN.sub(fill, options.pattern)
    if options.find:
        result = re.sub(re.escape(options.find), lambda _m: options.replace, result, flags=re.IGNORECASE)
    if options.case == "lower":
        result = result.lower()
    elif options.case == "upper":
        result = result.upper()
    result = re.sub(r"([_\-. ])\1+", r"\1", result).strip(" ._-")  # (an empty {map}: no "Rock__2")
    if tile is not None and "{tile}" not in options.pattern and not result.endswith(str(tile)):
        result += f".{tile}"
    return result + ext


def plan(assets: Sequence[AssetItem], options: RenameOptions, sets_of: Optional[dict[str, TextureSet]] = None) -> RenamePlan:
    """What each file would be called, and what's in the way (nothing is changed)."""
    sets_of = sets_of or {}
    result = RenamePlan()
    targets: dict[str, list[RenameRow]] = {}
    moving = {os.path.normcase(str(a.path)) for a in assets}
    for counter, asset in enumerate(assets, start=options.start):
        name = new_name(asset, options, counter, sets_of.get(str(asset.path)))
        row = RenameRow(asset, name)
        stem = os.path.splitext(name)[0]
        if asset.in_archive:
            row.problem = "Inside an archive: can't be renamed"
        elif not stem:
            row.problem = "The name would be empty"
        elif _BAD_CHARACTERS.search(name):
            row.problem = 'Names can\'t have < > : " / \\ | ? *'
        elif stem.lower().split(".")[0] in _RESERVED:
            row.problem = "Windows keeps that name for itself"
        elif len(str(Path(asset.path).parent / name)) > 255:
            row.problem = "The path would be too long"
        if not row.problem and name != asset.name:
            target = os.path.normcase(str(Path(asset.path).parent / name))
            targets.setdefault(target, []).append(row)
            if os.path.exists(target) and target not in moving and target != os.path.normcase(str(asset.path)):
                row.problem = "A file with that name is already there"
        result.rows.append(row)
    for rows in targets.values():
        if len(rows) > 1:
            for row in rows:
                row.problem = row.problem or f"{len(rows)} files would get this name"
    return result


def apply(rows: Sequence[RenameRow]) -> list[tuple[Path, Path]]:
    """Rename the files (rows that change and have no problem): first each to a temporary name, then to
    its new one, so names can be swapped. Returns (old, new) of those renamed; stops at the first error
    (OSError, after putting the ones moved to temporary names back)."""
    todo = [(Path(row.asset.path), Path(row.asset.path).parent / row.new_name) for row in rows if row.changes]
    parked: list[tuple[Path, Path, Path]] = []
    try:
        for old, new in todo:
            temporary = old.with_name(f".renaming-{uuid.uuid4().hex[:8]}{old.suffix}")
            os.rename(old, temporary)
            parked.append((old, temporary, new))
    except OSError:
        for old, temporary, _new in reversed(parked):
            os.rename(temporary, old)
        raise
    done: list[tuple[Path, Path]] = []
    for index, (old, temporary, new) in enumerate(parked):
        try:
            os.rename(temporary, new)
        except OSError:
            for left_old, left_temporary, _left_new in parked[index:]:  # (the rest go back as they were)
                try:
                    os.rename(left_temporary, left_old)
                except OSError:
                    pass
            if done:
                break
            raise
        done.append((old, new))
    return done


def undo(done: Sequence[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    """Rename them back (as apply does, so swapped names swap back). Returns (now, before) of those moved."""
    rows = [RenameRow(_FakeAsset(new), old.name) for old, new in done]
    return apply(rows)


@dataclass
class _FakeAsset:
    """What apply() needs of a file: its path and name."""

    path: Path
    archive: None = None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def in_archive(self) -> bool:
        return False
