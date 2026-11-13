"""The library's files and what's known about them, kept up to date in the background.

LibraryState scans folders (ScanWorker, on its own thread), follows changes made while the app
is open (core.library_watcher), starts from last session's snapshot, and reads file details
(resolution, length, faces) for searching, then what pictures look like (core.picture_look: colours
and look-alike data). It keeps, by database id, each file's tags, favorite / rating, details and
look. It never touches widgets: the main window shows what it holds and listens to its signals.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable, Optional, Sequence

import shiboken6
from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal, Slot

from core import archives, folder_rules
from core.category_rules import tag_names_by_id
from core.library_snapshot import save_snapshot
from core.library_watcher import FolderRescan, LibraryWatcher, rescan_folders
from core.metadata import read_cost, read_details, read_order
from core.picture_look import Look, read_look
from core.profiles import current_profile, other_profiles_folders
from core.scanner import AssetItem, AssetScanner, asset_sort_key
from core.settings import get_archive_contents, get_file_extensions
from core.tag_repository import NO_MARKS, AssetMeta, Marks, Tag, TagRepository, get_repository
from ui.tag_widgets import run_in_background


class ScanWorker(QObject):
    """Background worker to scan folders without blocking the UI thread."""

    finished = Signal(list, object)  # assets, info ({"ids", "tags", "relinked", "error", "dirs"})
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self, folders: list[Path], extensions: list[str], save_snapshot: bool = False, archive_contents: bool = False,
        profile_id: Optional[str] = None, keep_folders: Optional[list[Path]] = None,
    ):
        """folders: folders to scan with their subfolders, or an archive (its files are listed).
        archive_contents: also list the files inside the archives found. profile_id: the profile a
        library scan (save_snapshot) is for; keep_folders: the other profiles' library folders, whose
        files the database keeps (core.profiles)."""
        super().__init__()
        self.folders = folders
        self.extensions = extensions
        self.save_snapshot = save_snapshot  # library scans are remembered for an instant next launch
        self.archive_contents = archive_contents
        self.profile_id = profile_id
        self.keep_folders = list(keep_folders or [])
        self._stop = threading.Event()

    def cancel(self) -> None:
        """Stop soon (a newer scan replaced this one): nothing is reported or saved. Any thread."""
        self._stop.set()

    def run(self) -> None:
        try:
            scanner = AssetScanner(supported_extensions=self.extensions)
            assets: list[AssetItem] = []
            seen: set[Path] = set()
            directories: set[Path] = set()  # every folder walked, for the library watcher
            # Overlapping folders (e.g. a parent and its subfolder) must not duplicate files
            for folder in self.folders:
                if archives.is_archive_path(folder):  # an opened archive: its files
                    found = archives.entry_assets(Path(folder), self.extensions)
                else:
                    found = scanner.scan(folder, recursive=True, directories=directories, should_stop=self._stop.is_set)
                for asset in found:
                    if asset.path not in seen:
                        seen.add(asset.path)
                        assets.append(asset)
            if self.archive_contents:
                # (each archive's listing is cached by its size and date: only new or changed ones are read)
                assets.extend(archives.expand_archives(list(assets), self.extensions, should_stop=self._stop.is_set))
            if self._stop.is_set():
                self.cancelled.emit()
                return
            assets.sort(key=asset_sort_key)
            # A library scan also drops database rows of files it didn't find (folders browsed before)
            info = self._sync_tags(
                assets, forget_others=self.save_snapshot, should_stop=self._stop.is_set,
                keep_folders=self.keep_folders, scanned_folders=self.folders,
            )
            info["dirs"] = directories
            if self.save_snapshot and info["error"] is None:
                save_snapshot(self.folders, self.extensions, assets, info["ids"], self.archive_contents, self.profile_id)
            self.finished.emit(assets, info)
        except Exception as exc:  # pylint: disable=broad-except
            self.failed.emit(str(exc))
        finally:
            # Back to the UI thread, to be freed there. Freed on its own thread, Qt holds its signal lock
            # while PySide wants Python's lock; the UI thread holds Python's and wants the signal lock for
            # any connect (creating a menu is enough), and the app freezes for good if they meet.
            self.moveToThread(QCoreApplication.instance().thread())

    @staticmethod
    def _sync_tags(
        assets: list[AssetItem], forget_others: bool = False, should_stop: Optional[Callable[[], bool]] = None,
        keep_folders: Sequence[Path] = (), scanned_folders: Sequence[Path] = (),
    ) -> dict:
        """Record the assets and their default file type tags (also backfills older libraries).
        forget_others: remove rows of files not in assets that have nothing worth keeping (but not
        those in keep_folders, other profiles' folders, unless also in scanned_folders)."""
        repo = get_repository()
        try:
            relinked = repo.relink_moved_assets(assets)  # before sync, so moved files keep their rows
            ids = repo.sync_assets(assets, folder_rules.load())  # (files new to a folder get its rules' tags)
            if forget_others and not (should_stop and should_stop()):  # (a replaced scan leaves them alone)
                repo.forget_unscanned_assets(assets, keep_folders, scanned_folders)
            repo.fill_missing_hashes()
            asset_ids = list(ids.values())
            return {
                "ids": ids, "tags": repo.tags_by_asset(asset_ids), "marks": repo.marks_by_asset(asset_ids),
                "relinked": relinked, "error": None,
            }
        except Exception as exc:  # pylint: disable=broad-except
            # Browsing still works without the tag database
            return {"ids": {}, "tags": {}, "marks": {}, "relinked": 0, "error": str(exc)}
        finally:
            repo.close_thread_connection()


@dataclass(frozen=True)
class ScanReport:
    """A finished scan, for the window: what to say and how much of the grid to redo."""

    assets: list[AssetItem]
    unchanged: bool  # the same files with the same modified times as before
    same_files: bool  # the same files (some may have been edited)
    overlap: bool  # some files were there before too (keep the selection and scroll position)
    had_files: bool  # anything was shown before (the snapshot, or an earlier scan)
    relinked: int  # moved / renamed files that kept their tags
    error: Optional[str]  # the tag database couldn't be used (browsing still works)


@dataclass(frozen=True)
class SavedView:
    """The files shown and what's known about them (LibraryState.save_view)."""

    assets: list[AssetItem]
    ids: dict[str, int]
    tags: dict[int, list[Tag]]
    marks: dict[int, Marks]
    meta: dict[int, AssetMeta]
    looks: dict[int, Look] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveUpdate:
    """Files added, removed or edited while the app was open (only reported when something changed)."""

    added: int
    removed: int


class LibraryState(QObject):
    """The files shown (assets) and, by database id, their tags, marks and details."""

    scan_finished = Signal(object)  # ScanReport
    scan_failed = Signal(str)
    snapshot_details = Signal()  # the snapshot's tags and marks arrived (before the first scan finished)
    live_update = Signal(object)  # LiveUpdate
    live_update_failed = Signal(str)
    details_loaded = Signal()  # file details (resolution, length, faces) were read
    looks_loaded = Signal()  # more pictures were looked at (colours, look-alikes)
    files_edited = Signal(list)  # str paths whose contents changed: their thumbnails are out of date

    def __init__(self, repo: Optional[TagRepository] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.repo = repo or get_repository()
        self.assets: list[AssetItem] = []
        self.ids: dict[str, int] = {}  # database ids by str(asset.path)
        self._tags: dict[int, list[Tag]] = {}
        self._tag_names: Optional[dict[int, frozenset[str]]] = None  # (tag_names(), built when needed)
        self.marks: dict[int, Marks] = {}  # favorites / ratings (unmarked files are left out)
        self.meta: dict[int, AssetMeta] = {}  # resolution / duration / faces, read in the background
        self.meta_todo = 0  # files whose details are still to be read
        self._meta_generation = 0
        self.looks: dict[int, Look] = {}  # what pictures look like, read after the details
        self.looks_todo = 0  # pictures still to be looked at
        self._view_generation = 0  # (reset(): results for what was shown before are dropped)
        self.scan_done_once = False  # a real scan has finished (snapshot data is outdated then)

        # Background scan worker thread
        self.scan_thread: Optional[QThread] = None
        self.scan_worker: Optional[ScanWorker] = None
        # Replaced scans still winding down, with their workers: a worker freed before its thread
        # ran it crashed the thread (or left it running forever), so it's kept until the thread ends
        self._stopping_scans: list[tuple[QThread, ScanWorker]] = []

        # Picks up files added / removed / edited in the scanned folders while the app is open
        self.watcher = LibraryWatcher(self)
        self.watcher.folders_changed.connect(self._on_folders_changed)
        self._rescan_busy = False
        self._queued_folders: list[Path] = []

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def id_of(self, asset: AssetItem) -> Optional[int]:
        return self.ids.get(str(asset.path))

    def marks_of(self, asset: AssetItem) -> Marks:
        return self.marks.get(self.ids.get(str(asset.path)), NO_MARKS)

    def meta_of(self, asset: AssetItem) -> Optional[AssetMeta]:
        return self.meta.get(self.ids.get(str(asset.path)))

    def look_of(self, asset: AssetItem) -> Optional[Look]:
        return self.looks.get(self.ids.get(str(asset.path)))

    def tags_of(self, asset: AssetItem) -> list[Tag]:
        return self.tags.get(self.ids.get(str(asset.path)), [])

    def is_scanning(self) -> bool:
        return self.scan_thread is not None and shiboken6.isValid(self.scan_thread) and self.scan_thread.isRunning()

    # ------------------------------------------------------------------
    # Marks and tags (written by the window: favorites, ratings, the tag dialog)
    # ------------------------------------------------------------------
    def set_marks(self, marks: dict[int, Marks]) -> None:
        """Save files' favorite flag and rating, and keep them."""
        self.repo.restore_marks(marks)
        for asset_id, m in marks.items():
            if m == NO_MARKS:
                self.marks.pop(asset_id, None)
            else:
                self.marks[asset_id] = m

    def reload_marks(self) -> None:
        """Read every file's favorite and rating from the database again (after an import)."""
        self.marks = self.repo.marks_by_asset(list(self.ids.values()))

    @property
    def tags(self) -> dict[int, list[Tag]]:
        """{database id: the file's tags}. Change it with update_tags() or by setting it."""
        return self._tags

    @tags.setter
    def tags(self, tags: dict[int, list[Tag]]) -> None:
        self._tags = tags
        self._tag_names = None

    def update_tags(self, tags_by_asset: dict[int, list[Tag]]) -> None:
        self._tags.update(tags_by_asset)
        self._tag_names = None

    def tag_names(self) -> dict[int, frozenset[str]]:
        """{database id: the file's tag names, folded}, for sidebar categories with tags."""
        if self._tag_names is None:
            self._tag_names = tag_names_by_id(self._tags)
        return self._tag_names

    # ------------------------------------------------------------------
    # The snapshot (last session's library, shown while the first scan runs)
    # ------------------------------------------------------------------
    def show_snapshot(self, assets: list[AssetItem], ids: dict[str, int]) -> None:
        self.assets, self.ids = assets, ids
        asset_ids = list(ids.values())
        run_in_background(
            self.repo,
            lambda repo: (repo.tags_by_asset(asset_ids), repo.marks_by_asset(asset_ids)),
            partial(self._on_snapshot_details, self._view_generation),
            _ignore_result,
        )

    def _on_snapshot_details(self, generation: int, result) -> None:
        if self.scan_done_once or generation != self._view_generation:
            return  # the real scan already delivered newer data (or another profile is shown)
        self.tags, self.marks = result
        self.snapshot_details.emit()

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------
    def start_scan(self, folders: list[Path], extensions: list[str], save_snapshot: bool = False) -> None:
        """Scan folders (or an archive) on a background thread; replaces a scan still running (its
        results are dropped)."""
        self._retire_scan()

        self.scan_thread = QThread(self)
        profile, keep = (current_profile().id, other_profiles_folders()) if save_snapshot else (None, [])
        self.scan_worker = ScanWorker(
            folders, extensions, save_snapshot=save_snapshot, archive_contents=get_archive_contents(),
            profile_id=profile, keep_folders=keep,
        )
        self.scan_worker.moveToThread(self.scan_thread)

        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.finished.connect(self._on_scan_finished)
        self.scan_worker.failed.connect(self._on_scan_failed)
        for done in (self.scan_worker.finished, self.scan_worker.failed, self.scan_worker.cancelled):
            done.connect(self.scan_thread.quit)
        self.scan_thread.finished.connect(self.scan_worker.deleteLater)  # (with its thread; on the UI thread: run)

        self.scan_thread.start()

    def _retire_scan(self) -> None:
        """Let go of the previous scan: a running one is told to stop and its results are ignored
        (waiting for it here froze the window until it finished); its thread is freed once done."""
        thread, worker = self.scan_thread, self.scan_worker
        self.scan_thread = self.scan_worker = None
        if thread is None or not shiboken6.isValid(thread):
            return
        if worker is not None and shiboken6.isValid(worker):
            worker.cancel()
            for signal, slot in ((worker.finished, self._on_scan_finished), (worker.failed, self._on_scan_failed)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        if thread.isRunning():
            self._stopping_scans.append((thread, worker))
            thread.finished.connect(self._on_old_scan_stopped)
        else:
            thread.deleteLater()

    @Slot()
    def _on_old_scan_stopped(self) -> None:
        for entry in [e for e in self._stopping_scans if not shiboken6.isValid(e[0]) or not e[0].isRunning()]:
            self._stopping_scans.remove(entry)
            thread = entry[0]
            if shiboken6.isValid(thread):
                thread.deleteLater()  # (its worker is freed with it: thread.finished -> deleteLater)

    def _on_scan_finished(self, assets: list[AssetItem], tag_info: dict) -> None:
        self.scan_done_once = True
        before = {str(a.path): a.modified_timestamp for a in self.assets}
        after = {str(a.path): a.modified_timestamp for a in assets}
        edited = [path for path, mtime in after.items() if before.get(path, mtime) != mtime]
        report = ScanReport(
            assets, unchanged=before == after, same_files=before.keys() == after.keys(),
            overlap=bool(before.keys() & after.keys()), had_files=bool(before),
            relinked=tag_info["relinked"], error=tag_info["error"],
        )
        if edited:
            self.files_edited.emit(edited)  # edited since they were last drawn
        self.assets = assets
        self.ids = tag_info["ids"]
        self.tags = tag_info["tags"]
        self.marks = tag_info["marks"]
        self.watcher.watch(tag_info["dirs"])
        self.scan_finished.emit(report)
        self.start_meta_indexing()

    def _on_scan_failed(self, error_msg: str) -> None:
        self.scan_failed.emit(error_msg)

    def clear(self) -> None:
        """No folders to show (none set up)."""
        self.assets = []

    def reset(self) -> None:
        """Forget everything shown (another profile is shown next): its scan, folder rescans,
        snapshot details and file details still being read are dropped when they arrive."""
        self._retire_scan()
        self._view_generation += 1
        self._meta_generation += 1
        self.meta_todo = 0
        self._rescan_busy = False
        self._queued_folders.clear()
        self.watcher.clear()
        self.scan_done_once = False
        self.assets, self.ids, self.marks, self.meta, self.looks = [], {}, {}, {}, {}
        self.looks_todo = 0
        self.tags = {}

    def save_view(self) -> "SavedView":
        """What's shown now, to come back to (leaving an opened archive) without waiting for a scan."""
        return SavedView(
            list(self.assets), dict(self.ids), dict(self.tags), dict(self.marks), dict(self.meta), dict(self.looks)
        )

    def restore_view(self, saved: "SavedView") -> None:
        """Show a saved view again (a scan then brings it up to date)."""
        self._retire_scan()
        self._meta_generation += 1  # (the details being read belong to what was shown)
        self.meta_todo = self.looks_todo = 0
        self.assets, self.ids, self.tags, self.marks, self.meta, self.looks = (
            saved.assets, saved.ids, saved.tags, saved.marks, saved.meta, saved.looks,
        )

    # ------------------------------------------------------------------
    # Live updates (LibraryWatcher)
    # ------------------------------------------------------------------
    def rescan_folders(self, folders: list[Path]) -> None:
        """Bring these folders up to date now (files renamed by the app), as the watcher would."""
        self._on_folders_changed(folders)

    def _on_folders_changed(self, folders: list[Path]) -> None:
        """Rescan just the folders whose contents changed."""
        if self.is_scanning():
            return  # the full scan in progress picks the change up
        if self._rescan_busy:
            self._queued_folders.extend(folders)
            return
        self._rescan_busy = True
        watched, extensions, archive_contents = self.watcher.watched_keys(), get_file_extensions(), get_archive_contents()
        run_in_background(
            self.repo,
            lambda repo: rescan_folders(folders, extensions, watched, repo, archive_contents=archive_contents),
            partial(self._on_folders_rescanned, self._view_generation),
            partial(self._on_folders_rescan_failed, self._view_generation),
        )

    def _on_folders_rescanned(self, generation: int, result: FolderRescan) -> None:
        if generation != self._view_generation:
            return  # (folders of the profile shown before)
        self._rescan_busy = False
        for tree in result.removed_trees:
            self.watcher.remove_tree(tree)
        self.watcher.add_directories(result.new_dirs)

        before = {str(a.path): a.modified_timestamp for a in self.assets}
        self.assets = result.merge_into(self.assets)
        after = {str(a.path) for a in self.assets}
        edited = [
            str(a.path) for a in result.assets
            if before.get(str(a.path)) is not None and before[str(a.path)] != a.modified_timestamp
        ]
        if edited:
            self.files_edited.emit(edited)  # edited: redraw their thumbnails
        self.ids.update(result.ids)
        self.update_tags(result.tags)
        self.marks.update(result.marks)

        added, removed = len(after - before.keys()), len(before.keys() - after)
        changed = added or removed or any(
            before.get(str(a.path)) != a.modified_timestamp for a in result.assets
        )
        if changed:
            self.live_update.emit(LiveUpdate(added, removed))
            self.start_meta_indexing()

        if self._queued_folders:
            folders, self._queued_folders = self._queued_folders, []
            self._on_folders_changed(folders)

    def _on_folders_rescan_failed(self, generation: int, error: str) -> None:
        if generation != self._view_generation:
            return
        self._rescan_busy = False
        self._queued_folders.clear()
        self.live_update_failed.emit(error)

    # ------------------------------------------------------------------
    # File details for searching (core.metadata), read in the background
    # ------------------------------------------------------------------
    def start_meta_indexing(self) -> None:
        self._meta_generation += 1
        generation, ids = self._meta_generation, list(self.ids.values())

        def load(repo):
            todo = repo.assets_needing_meta(ids)
            todo = [row for row in todo if read_cost(row[2]) < 3]
            todo.sort(key=lambda row: read_order(row[1], row[2]))  # images first: they take milliseconds; big models last
            return repo.meta_by_asset(ids), todo

        run_in_background(self.repo, load, partial(self._on_meta_loaded, generation), _ignore_result, slow=True)
        self._start_looking(generation)

    def _on_meta_loaded(self, generation: int, result) -> None:
        if generation != self._meta_generation:
            return
        self.meta, todo = result
        self.meta_todo = len(todo)
        self.details_loaded.emit()
        self._read_meta_batch(generation, todo)

    def _read_meta_batch(self, generation: int, todo: list) -> None:
        if not todo or generation != self._meta_generation:
            self.meta_todo = 0
            return

        def read(repo):
            details, deadline, done = {}, time.monotonic() + 0.5, 0
            for asset_id, path, ext, key in todo:  # stop after ~0.5 s so a new scan can take over quickly
                done += 1
                meta = read_details(path, ext)
                if meta is not None:
                    details[asset_id] = (meta, key)
                if time.monotonic() > deadline:
                    break
            if details:
                repo.store_meta(details)
            return details, todo[done:]

        def finished(result) -> None:
            details, rest = result
            if generation != self._meta_generation:
                return
            self.meta.update({asset_id: meta for asset_id, (meta, _key) in details.items()})
            self.meta_todo = len(rest)
            if details:
                self.details_loaded.emit()
            self._read_meta_batch(generation, rest)

        run_in_background(self.repo, read, finished, _ignore_result, slow=True)

    # ------------------------------------------------------------------
    # What pictures look like (core.picture_look), read after the details
    # ------------------------------------------------------------------
    def _start_looking(self, generation: int) -> None:
        ids = list(self.ids.values())

        def load(repo):
            todo = repo.assets_needing_look(ids)
            todo.sort(key=lambda row: ("|" in row[1], row[1]))  # (inside archives last: they're unpacked first)
            return repo.looks_by_asset(ids), todo

        run_in_background(self.repo, load, partial(self._on_looks_loaded, generation), _ignore_result, slow=LOOK_LANE)

    def _on_looks_loaded(self, generation: int, result) -> None:
        if generation != self._meta_generation:
            return
        self.looks, todo = result
        self.looks_todo = len(todo)
        self.looks_loaded.emit()
        self._look_batch(generation, todo)

    def _look_batch(self, generation: int, todo: list) -> None:
        if not todo or generation != self._meta_generation:
            self.looks_todo = 0
            return

        def read(repo):
            found, done, deadline = {}, 0, time.monotonic() + 0.5
            with ThreadPoolExecutor(LOOK_THREADS, thread_name_prefix="look") as pool:
                while done < len(todo) and time.monotonic() < deadline:  # (~0.5 s: a new scan can take over)
                    chunk = todo[done:done + LOOK_THREADS]
                    for (asset_id, path, key), look in zip(chunk, pool.map(read_look, [row[1] for row in chunk])):
                        found[asset_id] = (look, key)
                    done += len(chunk)
            if found:
                repo.store_looks(found)
            return found, todo[done:]

        def finished(result) -> None:
            found, rest = result
            if generation != self._meta_generation:
                return
            self.looks.update({asset_id: look for asset_id, (look, _key) in found.items()})
            self.looks_todo = len(rest)
            if found:
                self.looks_loaded.emit()
            self._look_batch(generation, rest)

        run_in_background(self.repo, read, finished, _ignore_result, slow=LOOK_LANE)

    def look_now(self, asset: AssetItem, on_done: Callable[[Optional[Look]], None]) -> None:
        """Look at one picture right away (Find Similar on one not looked at yet); on_done(look) on the
        UI thread (None if it isn't in the library)."""
        asset_id = self.id_of(asset)
        if asset_id is None:
            on_done(None)
            return
        from core.tag_repository import look_key

        path, key = str(asset.path), look_key(asset.size_bytes, asset.modified_timestamp)

        def read(repo):
            look = read_look(path)
            repo.store_looks({asset_id: (look, key)})
            return look

        def finished(look: Look) -> None:
            self.looks[asset_id] = look
            on_done(look)

        run_in_background(self.repo, read, finished, lambda _error: on_done(None))

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop the scan and the background reading (before the app quits)."""
        if self.scan_worker is not None and shiboken6.isValid(self.scan_worker):
            self.scan_worker.cancel()
        for thread in [self.scan_thread, *(thread for thread, _worker in self._stopping_scans)]:
            if thread is not None and shiboken6.isValid(thread) and thread.isRunning():
                thread.quit()
                thread.wait(2000)  # (cancelled: it stops after the folder it's in)
        self._meta_generation += 1  # ends the file-details reading chain
        self.watcher.clear()


LOOK_THREADS = 2  # pictures looked at side by side (thumbnails keep the rest of the processor)
LOOK_LANE = "looks"  # (a background lane of its own: a big 3D model's details can take a minute)


def _ignore_result(_result) -> None:
    pass
