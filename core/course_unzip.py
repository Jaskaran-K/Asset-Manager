"""Unzipping a course's archives where they are, and checking every file.

A course downloaded as zips (often one per run of lessons, and its scene files apart) is unzipped beside each zip: into
its folder when everything in it sits in one folder of its own, else into a folder named after it (so nothing spills
out loose). Each file is then read back from the disk and checked against the archive's own record of it (its size
and CRC-32). A file already there is left alone: if it's the same it counts as checked, if not, yours is kept and that
archive doesn't count as checked. Only archives whose every file checks out are offered for the Recycle Bin.

RAR and 7z archives go through 7-Zip (core.archives finds it); zips need nothing. Runs on a worker thread (no Qt
widgets); progress(done, total) and should_stop() as in core.archives.
"""

from __future__ import annotations

import os
import re
import shutil
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QFile

from core import archives
from core.scanner import ARCHIVE_EXTENSIONS

CHUNK = 1024 * 1024
_LATER_PART = re.compile(r"\.part0*([2-9]|\d{2,})\.rar$", re.IGNORECASE)  # (part 2 on of a multi-part RAR)
_ANY_PART = re.compile(r"\.part\d+\.rar$", re.IGNORECASE)


def to_recycle_bin(path: Path) -> bool:
    """(Tests replace this.)"""
    return QFile.moveToTrash(str(path))


@dataclass
class ArchivePlan:
    """One archive: what's in it, where it goes, and how much room it needs."""

    path: Path
    target: Path  # the folder it's unzipped into
    members: tuple = ()
    size: int = 0  # unpacked
    error: str = ""  # why it can't be unzipped (no 7-Zip for a RAR, damaged...)

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class ArchiveResult:
    path: Path
    files: int = 0  # files in it
    checked: int = 0  # read back and matching the archive
    kept: list[str] = field(default_factory=list)  # already there, but different: yours kept
    failed: list[str] = field(default_factory=list)  # "name: why"

    @property
    def verified(self) -> bool:
        """Every file is on the disk and matches the archive: safe to delete the archive."""
        return self.files > 0 and self.checked == self.files and not self.kept and not self.failed


def find_archives(folder: Path) -> list[Path]:
    """The archives in a folder and its folders, in name order (a multi-part RAR once: its first part)."""
    found = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            if Path(name).suffix.lower() in ARCHIVE_EXTENSIONS and not _LATER_PART.search(name):
                found.append(Path(root, name))
    return found


def archive_parts(path: Path) -> list[Path]:
    """The archive's files: a multi-part RAR's every part, else just itself."""
    if not _ANY_PART.search(path.name):
        return [path]
    stem = _ANY_PART.sub("", path.name)
    return sorted(p for p in path.parent.iterdir() if p.name.lower().startswith(stem.lower()) and _ANY_PART.search(p.name))


def target_for(path: Path, members) -> Path:
    """Where an archive's files go: its own folder when they all sit in one folder of their own, else a folder named
    after it (the way 7-Zip's "Extract to" does)."""
    tops = {m.name.replace("\\", "/").split("/", 1)[0] for m in members}
    nested = len(tops) == 1 and all("/" in m.name.replace("\\", "/") for m in members)
    stem = _ANY_PART.sub("", path.name) if _ANY_PART.search(path.name) else path.stem
    return path.parent if nested else path.parent / stem


def plan(paths: list[Path]) -> list[ArchivePlan]:
    """Read each archive's list of files (slow for big RARs: a worker thread)."""
    out = []
    for path in paths:
        listing = archives.listing(path)
        if listing.error:
            out.append(ArchivePlan(path, path.parent, error=listing.error))
            continue
        members = tuple(m for m in listing.members if not m.encrypted)
        error = "It needs a password." if len(members) < len(listing.members) else ""
        out.append(ArchivePlan(path, target_for(path, members), members, sum(m.size for m in members), error))
    return out


def space_needed(plans: list[ArchivePlan]) -> int:
    return sum(p.size for p in plans if not p.error)


def free_space(folder: Path) -> int:
    try:
        return shutil.disk_usage(folder).free
    except OSError:
        return 0


def file_crc(path: Path, should_stop: Optional[Callable[[], bool]] = None,
             step: Optional[Callable[[int], None]] = None) -> int:
    crc = 0
    with open(path, "rb") as f:
        while True:
            if should_stop is not None and should_stop():
                raise archives.Cancelled()
            chunk = f.read(CHUNK)
            if not chunk:
                return crc & 0xFFFFFFFF
            crc = zlib.crc32(chunk, crc)
            if step is not None:
                step(len(chunk))


def unzip(item: ArchivePlan, progress: Optional[Callable[[int, int], None]] = None,
          should_stop: Optional[Callable[[], bool]] = None) -> ArchiveResult:
    """Unzip one archive into its target, then check every file (see the module docstring). progress: unzipping is
    the first half of total, checking the second. Raises archives.Cancelled."""
    result = ArchiveResult(item.path, files=len(item.members))
    if item.error:
        result.failed.append(item.error)
        return result
    total = max(1, 2 * item.size)
    extract_plan = archives.plan_extract(item.members, item.target)
    result.failed += [f"{name}: its name can't be used on Windows" for name in extract_plan.unsafe]
    done = archives.extract(extract_plan, replace=False,
                            progress=(lambda made, _all: progress(made, total)) if progress else None,
                            should_stop=should_stop)
    result.failed += list(done.failed)
    existing = set(extract_plan.existing)
    for member, target in extract_plan.targets:  # (each file keeps the date it has in the archive, not today's)
        if target not in existing and member.modified and target.is_file():
            try:
                os.utime(target, (member.modified, member.modified))
            except OSError:
                pass
    checked_bytes = [item.size]

    def step(count: int) -> None:
        checked_bytes[0] += count
        if progress is not None:
            progress(min(total, checked_bytes[0]), total)

    for member, target in extract_plan.targets:
        if any(f.startswith(f"{member.name}:") for f in done.failed):
            continue
        try:
            if not target.is_file() or target.stat().st_size != member.size:
                matches = False
                step(member.size)
            elif member.crc == 0 and (member.size == 0 or member.kind == "7z"):
                matches = True  # (empty, or a RAR5 that lists no CRC-32: its size is all there is to check)
                step(member.size)
            else:
                matches = file_crc(target, should_stop, step) == member.crc
        except OSError as exc:
            result.failed.append(f"{member.name}: {exc}")
            continue
        if matches:
            result.checked += 1
        elif target in extract_plan.existing:
            result.kept.append(member.name)  # (a different file of that name was already there: yours is kept)
        else:
            result.failed.append(f"{member.name}: doesn't match the archive")
    if progress is not None:
        progress(total, total)
    return result


def trash_archives(paths: list[Path]) -> tuple[int, int, list[str]]:
    """Move archives (each with its other parts) to the Recycle Bin: (how many, bytes freed, what couldn't be)."""
    moved, freed, failed = 0, 0, []
    for path in paths:
        for part in archive_parts(path):
            try:
                size = part.stat().st_size
            except OSError:
                continue
            if to_recycle_bin(part):
                freed += size
            else:
                failed.append(part.name)
        moved += 1
    return moved, freed, failed


def format_size(count: int) -> str:
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if count >= scale:
            return f"{count / scale:.1f} {unit}"
    return f"{count} bytes"
