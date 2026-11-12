"""Folder rules: "files added to this folder get the tags WIP and Kitbash".

A rule tags a file once, when the library first sees it in the rule's folder: a new file, one copied
or moved in, or one found by a scan after being added while the app was closed. Tags taken off a file
stay off (core.tag_repository remembers the folder each file was last seen in: seen_folder). Making a
rule can also tag the files already in the folder (TagRepository.apply_folder_rule).

Rules belong to every profile, as tags do, and are kept in the settings file. They name their tags,
so renaming or deleting a tag updates them (Settings > Folder Rules), as it does categories.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


def _key(folder: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(folder))).rstrip("\\/")


@dataclass(frozen=True)
class FolderRule:
    folder: str  # as chosen (shown as is)
    tags: tuple[str, ...]  # names, as written
    subfolders: bool = True  # files in its subfolders too

    @property
    def folder_key(self) -> str:
        return _key(self.folder)

    def covers(self, key: str) -> bool:
        """Whether a file (by its path_key: normcased absolute path) is in the rule's folder. A file
        inside an archive counts as being where its archive is."""
        real = key.partition("|")[0]
        folder = self.folder_key
        if self.subfolders:
            return real.startswith(folder + os.sep)
        return os.path.dirname(real) == folder

    def describe(self) -> str:
        where = "this folder and its subfolders" if self.subfolders else "this folder only"
        return f"{', '.join(self.tags)} ({where})"

    def to_dict(self) -> dict:
        return {"folder": self.folder, "tags": list(self.tags), "subfolders": self.subfolders}


def load() -> list[FolderRule]:
    from . import settings

    rules = []
    for entry in settings.get_folder_rules():
        tags = tuple(dict.fromkeys(" ".join(t.split()) for t in entry["tags"] if t.strip()))
        if tags:
            rules.append(FolderRule(entry["folder"], tags, entry["subfolders"]))
    return rules


def save(rules: Sequence[FolderRule]) -> None:
    from . import settings

    settings.set_folder_rules([rule.to_dict() for rule in rules if rule.tags])


def rules_for(folder: Path | str, rules: Sequence[FolderRule]) -> list[FolderRule]:
    """The rules made for exactly this folder."""
    key = _key(folder)
    return [rule for rule in rules if rule.folder_key == key]

