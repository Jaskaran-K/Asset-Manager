"""Notices files being added, removed, renamed or edited in the library folders.

Wraps QFileSystemWatcher (which watches single folders, so every subfolder is
added) and batches bursts of changes, e.g. a big copy, into one report per
DEBOUNCE_MS. The main window rescans just the reported folders.

Limitation: each watched folder uses an OS handle, so at most MAX_WATCHED_DIRS
folders are watched; beyond that, changes in the rest show up on the next
Refresh (is_complete() tells which case applies).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from .scanner import AssetItem, AssetScanner

if TYPE_CHECKING:
    from .tag_repository import Marks, Tag, TagRepository

DEBOUNCE_MS = 1500
MAX_WATCHED_DIRS = 4000


def path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


_key = path_key


@dataclass
class FolderRescan:
    """What changed in some folders; merge_into() applies it to the library's asset list."""

    folders: list[Path] = field(default_factory=list)  # existing folders: their files are replaced
    removed_trees: list[Path] = field(default_factory=list)  # folders gone: everything under them goes
    new_trees: list[Path] = field(default_factory=list)  # new folders, scanned with all their subfolders
    assets: list[AssetItem] = field(default_factory=list)  # files now in folders + new_trees
    new_dirs: set[Path] = field(default_factory=set)  # folders to start watching
    ids: dict[str, int] = field(default_factory=dict)
    tags: dict[int, list["Tag"]] = field(default_factory=dict)
    marks: dict[int, "Marks"] = field(default_factory=dict)
    relinked: int = 0

    def merge_into(self, assets: list[AssetItem]) -> list[AssetItem]:
        replaced = {path_key(f) for f in self.folders}
        trees = [path_key(t) for t in self.removed_trees + self.new_trees]

        def keep(asset: AssetItem) -> bool:
            key = path_key(asset.path)
            if os.path.dirname(key) in replaced:
                return False
            return not any(key.startswith(tree + os.sep) for tree in trees)

        merged = [a for a in assets if keep(a)] + self.assets
        merged.sort(key=lambda a: a.name.lower())
        return merged


def rescan_folders(
    folders: Iterable[Path], extensions: list[str], watched: set[str], repo: Optional["TagRepository"] = None
) -> FolderRescan:
    """Rescan changed folders (runs on a worker thread). watched: path_key()s currently watched."""
    scanner = AssetScanner(supported_extensions=extensions)
    result = FolderRescan()
    watched_children: dict[str, set[str]] = {}
    for key in watched:
        watched_children.setdefault(os.path.dirname(key), set()).add(key)

    seen_trees: set[str] = set()
    for folder in folders:
        key = path_key(folder)
        if not os.path.isdir(folder):
            result.removed_trees.append(Path(folder))
            continue
        result.folders.append(Path(folder))
        result.assets.extend(scanner.scan(folder, recursive=False))
        try:
            subdirs = {path_key(p): p for p in Path(folder).iterdir() if p.is_dir() and not scanner.is_excluded_dir(p)}
        except OSError:
            subdirs = {}
        for child in watched_children.get(key, set()) - set(subdirs):
            result.removed_trees.append(Path(child))
        for child_key, child in subdirs.items():
            if child_key not in watched and child_key not in seen_trees:
                seen_trees.add(child_key)
                result.new_trees.append(child)
                result.assets.extend(scanner.scan(child, recursive=True, directories=result.new_dirs))

    if repo is not None and result.assets:
        result.relinked = repo.relink_moved_assets(result.assets)
        result.ids = repo.sync_assets(result.assets)
        repo.fill_missing_hashes()
        result.tags = repo.tags_by_asset(list(result.ids.values()))
        result.marks = repo.marks_by_asset(list(result.ids.values()))
    return result


class LibraryWatcher(QObject):
    folders_changed = Signal(list)  # list[Path]: folders whose contents changed

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_directory_changed)
        self._watched: dict[str, Path] = {}
        self._dirty: dict[str, Path] = {}
        self._complete = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._flush)

    def watch(self, directories: Iterable[Path]) -> None:
        """Replace the watched folders (call after a full scan)."""
        self.clear()
        self.add_directories(directories)

    def clear(self) -> None:
        if self._watcher.directories():
            self._watcher.removePaths(self._watcher.directories())
        self._watched.clear()
        self._dirty.clear()
        self._timer.stop()
        self._complete = True

    def add_directories(self, directories: Iterable[Path]) -> None:
        new = []
        for directory in directories:
            key = _key(directory)
            if key in self._watched:
                continue
            if len(self._watched) + len(new) >= MAX_WATCHED_DIRS:
                self._complete = False
                break
            new.append((key, Path(directory)))
        if new:
            failed = set(self._watcher.addPaths([str(p) for _k, p in new]))
            for key, path in new:
                if str(path) not in failed:
                    self._watched[key] = path

    def remove_tree(self, root: Path) -> None:
        """Stop watching a folder and everything under it (it was deleted or moved away)."""
        prefix = _key(root)
        gone = [k for k in self._watched if k == prefix or k.startswith(prefix + os.sep)]
        paths = [str(self._watched.pop(k)) for k in gone]
        if paths:
            self._watcher.removePaths(paths)

    def watched_keys(self) -> set[str]:
        return set(self._watched)

    def is_complete(self) -> bool:
        return self._complete

    def _on_directory_changed(self, path: str) -> None:
        self._dirty[_key(path)] = Path(path)
        self._timer.start()  # restart: wait until the burst of changes is over

    def _flush(self) -> None:
        dirty, self._dirty = list(self._dirty.values()), {}
        if dirty:
            self.folders_changed.emit(dirty)
