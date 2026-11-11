"""Files inside archives (zip, RAR, 7z): listing them, reading them and unpacking them.

Every read of a file inside an archive goes through here. Zips are read by the app itself; RAR and
7z through the user's own 7-Zip (find_seven_zip), run as a hidden process off the UI thread.

- A file inside an archive is an AssetItem whose path is virtual: the archive's path, SEPARATOR,
  then the member's name (D:\\packs\\foo.zip|textures\\rock.png). Windows paths can't contain "|",
  so it never names a real file: anything that opens it by mistake fails (OSError) instead of
  reading something else. Its .archive (ArchiveMember) has what's needed to read it.
- Each archive's listing is cached on disk (index_dir) by its size and modified time, so a rescan
  doesn't read the zips again (about 9 s for a 215-zip library from a cold disk).
- Zip members are read straight from the archive file: stored, deflated and Deflate64 (method 9,
  which Python's zipfile can't read: the inflate64 package) are streamed and checked against
  their checksum and size; other methods go through zipfile. RAR / 7z members are streamed from
  7-Zip's output (7z e -so), which checks their checksums itself.
- A multi-part RAR (name.part1.rar, name.part2.rar, ...) is one archive: its first part.
- Files that need a real path (models, PDFs, videos, other apps) are unpacked into a size-capped
  cache (extract_dir) by materialize().
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import threading
import time
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Iterator, Optional, Sequence

from .scanner import ARCHIVE_EXTENSIONS, AssetItem, archive_stamp, determine_asset_group, determine_category_name

SEPARATOR = "|"
NO_SEVEN_ZIP = "Install 7-Zip to look inside"  # a RAR / 7z without 7-Zip (Settings > Archives)
LIST_TIMEOUT = 120  # seconds for 7-Zip to list an archive  # between the archive's path and the member's name in a virtual path

MAX_PREVIEW_BYTES = 100 * 1024 * 1024  # bigger members get a thumbnail once they've been opened (unpacked)
MAX_CARD_PICTURE_BYTES = 20 * 1024 * 1024  # an archive's card picture: an image inside up to this size
MAX_DRAG_BYTES = 50 * 1024 * 1024  # dragging out unpacks first: bigger ones go through Extract

_CHUNK = 1024 * 1024
_DEFLATE64_CHUNK = 64 * 1024  # (inflate64 has no output limit: small inputs keep each output small)
_INDEX_VERSION = 2  # 2: RAR / 7z (each member's kind)
_PREVIEW_NAME = re.compile(r"preview|cover|thumb|icon", re.IGNORECASE)
_PICTURE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tga", ".tif", ".tiff", ".psd")
_UNSAFE_PART = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = re.compile(r"(con|prn|aux|nul|com\d|lpt\d)(\..*)?", re.IGNORECASE)


class ArchiveError(Exception):
    """An archive or a file in it can't be read (the message says why, for the user)."""


class Cancelled(Exception):
    """should_stop() said to stop."""


@dataclass(frozen=True)
class ArchiveMember:
    """A file inside an archive, with what's needed to read it without listing the archive again."""

    archive: Path
    name: str  # as stored ("/" between folders)
    size: int  # unpacked
    compressed_size: int
    method: int  # 0 stored, 8 deflated, 9 Deflate64, ...
    crc: int
    offset: int  # of its local header in the archive
    encrypted: bool
    modified: float
    archive_size: int  # (a multi-part RAR: all its parts)
    archive_mtime: float
    kind: str = "zip"  # "zip": read by the app; "7z": through 7-Zip (RAR, 7z)

    @property
    def folder(self) -> str:
        """The folder inside the archive ("" at its top), "/" between folders."""
        return self.name.replace("\\", "/").rpartition("/")[0]

    @property
    def base_name(self) -> str:
        return self.name.replace("\\", "/").rpartition("/")[2]

    @property
    def identity(self) -> str:
        """Names this member's content: changes when the archive is replaced or edited."""
        return f"{_key(self.archive)}|{self.archive_size}|{self.archive_mtime!r}|{self.name}|{self.crc}"


@dataclass(frozen=True)
class Listing:
    """What an archive holds (members), or why it can't be read (error)."""

    members: tuple[ArchiveMember, ...] = ()
    error: str = ""


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def is_archive_path(path: Path | str) -> bool:
    return os.path.splitext(str(path))[1].lower() in ARCHIVE_EXTENSIONS and os.path.isfile(path)


def virtual_path(archive: Path, name: str) -> Path:
    return Path(f"{archive}{SEPARATOR}{name}")


def _cache_root() -> Path:
    from . import video_tools  # (read when used, so tests can redirect it)

    return video_tools.CACHE_ROOT


def index_dir() -> Path:
    """Cached archive listings."""
    return _cache_root() / "archive-index"


def extract_dir() -> Path:
    """Unpacked copies of files inside archives (materialize)."""
    return _cache_root() / "archive-files"


def _temp_dir() -> Path:
    return _cache_root() / "archive-temp"


# ----------------------------------------------------------------------
# Listing
# ----------------------------------------------------------------------
def _skipped(name: str) -> bool:
    """macOS leftovers that aren't anyone's files."""
    lower = name.replace("\\", "/").lower()
    base = lower.rpartition("/")[2]
    return lower.startswith("__macosx/") or "/__macosx/" in lower or base == ".ds_store" or base.startswith("._")


def _timestamp(date_time: tuple, fallback: float) -> float:
    try:
        return time.mktime((*date_time, 0, 0, -1))
    except (OverflowError, ValueError):
        return fallback


def read_listing(archive: Path) -> Listing:
    """Read an archive's own list of files (a zip's central directory; 7-Zip's listing for the
    others). Never raises."""
    archive = Path(archive)
    if archive.suffix.lower() != ".zip":
        return _seven_listing(archive)
    try:
        size, mtime = archive_stamp(archive)
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError, NotImplementedError) as exc:
        return Listing(error=_damage_text(exc))
    members = []
    for item in infos:
        if item.is_dir() or _skipped(item.filename):
            continue
        members.append(ArchiveMember(
            archive=archive, name=item.filename, size=item.file_size, compressed_size=item.compress_size,
            method=item.compress_type, crc=item.CRC, offset=item.header_offset, encrypted=bool(item.flag_bits & 1),
            modified=_timestamp(item.date_time, mtime), archive_size=size, archive_mtime=mtime,
        ))
    return Listing(tuple(members))


# ----------------------------------------------------------------------
# 7-Zip (RAR, 7z)
# ----------------------------------------------------------------------
_seven_zip_setting = ""  # Settings > Archives (configure_seven_zip); "" = find it
_seven_zip_found: Optional[tuple[str, Optional[str]]] = None  # (setting, the 7z.exe it gave)
_seven_zip_lock = threading.Lock()


def find_seven_zip(configured: str = "") -> Optional[str]:
    """7z.exe: the configured file or folder (None if it isn't there: no silent fallback), else
    on PATH, in Program Files, where its installer registered it, or in Scoop's apps."""
    from .video_tools import resolve_shim

    configured = configured.strip()
    if configured:
        path = Path(configured)
        if path.is_dir():
            path = path / "7z.exe"
        return resolve_shim(str(path)) if path.is_file() else None
    found = shutil.which("7z")
    if found:
        return resolve_shim(found)
    candidates = [
        Path(os.environ[var]) / "7-Zip" / "7z.exe"
        for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)") if os.environ.get(var)
    ]
    candidates += [Path(folder) / "7z.exe" for folder in _registered_seven_zip_folders()]
    scoop = os.environ.get("SCOOP") or str(Path.home() / "scoop")
    candidates.append(Path(scoop) / "apps" / "7zip" / "current" / "7z.exe")
    return next((str(c) for c in candidates if c.is_file()), None)


def _registered_seven_zip_folders() -> list[str]:
    try:
        import winreg
    except ImportError:
        return []
    folders = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\7-Zip") as key:
                for value in ("Path64", "Path"):
                    try:
                        folders.append(str(winreg.QueryValueEx(key, value)[0]))
                    except OSError:
                        pass
        except OSError:
            pass
    return folders


def configure_seven_zip(setting: str) -> None:
    """Use this 7-Zip (Settings > Archives; "" finds it). Call at startup and when it changes."""
    global _seven_zip_setting, _seven_zip_found
    with _seven_zip_lock:
        _seven_zip_setting, _seven_zip_found = setting, None


def seven_zip() -> Optional[str]:
    """The 7z.exe to use, or None (found once per setting)."""
    global _seven_zip_found
    with _seven_zip_lock:
        if _seven_zip_found is None or _seven_zip_found[0] != _seven_zip_setting:
            _seven_zip_found = (_seven_zip_setting, find_seven_zip(_seven_zip_setting))
        return _seven_zip_found[1]


def _hidden() -> dict:
    """subprocess options: no console window flashing up, no waiting on input (a password prompt)."""
    return {"stdin": subprocess.DEVNULL, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _seven_error(stderr: bytes) -> str:
    text = stderr.decode("utf-8", "replace")
    if "password" in text.lower() or "encrypted" in text.lower():
        return "Password-protected"
    return "Damaged or not an archive"


def _seven_listing(archive: Path) -> Listing:
    exe = seven_zip()
    if exe is None:
        return Listing(error=NO_SEVEN_ZIP)
    try:
        size, mtime = archive_stamp(archive)
        # -slt: a block of "Key = value" lines per file; -p: an empty password (never a prompt)
        done = subprocess.run(
            [exe, "l", "-slt", "-ba", "-sccUTF-8", "-p", "--", str(archive)],
            capture_output=True, timeout=LIST_TIMEOUT, **_hidden(),
        )
    except subprocess.TimeoutExpired:
        return Listing(error="Took too long to list")
    except OSError as exc:
        return Listing(error=_damage_text(exc))
    if done.returncode not in (0, 1):  # (1: a warning, the listing is still good)
        return Listing(error=_seven_error(done.stderr))
    members = []
    for block in done.stdout.decode("utf-8", "replace").replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.split("\n") if " = " in line)
        name = fields.get("Path", "")
        if not name or "Type" in fields or fields.get("Folder") == "+" or "D" in fields.get("Attributes", "").split(" ")[0]:
            continue  # (the archive's own details, or a folder)
        if _skipped(name):
            continue
        try:
            modified = time.mktime(time.strptime(fields.get("Modified", "")[:19], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            modified = mtime
        members.append(ArchiveMember(
            archive=archive, name=name, size=_int(fields.get("Size")), compressed_size=_int(fields.get("Packed Size")),
            method=-1, crc=_int(fields.get("CRC"), 16), offset=0, encrypted=fields.get("Encrypted") == "+",
            modified=modified, archive_size=size, archive_mtime=mtime, kind="7z",
        ))
    return Listing(tuple(members))


def _int(text: Optional[str], base: int = 10) -> int:
    try:
        return int(text or "0", base)
    except ValueError:
        return 0


def _seven_chunks(member: ArchiveMember, should_stop: Optional[Callable[[], bool]]) -> Iterator[bytes]:
    """A RAR / 7z member's bytes, streamed from 7-Zip (stopped if the reader stops early)."""
    exe = seven_zip()
    if exe is None:
        raise ArchiveError(NO_SEVEN_ZIP)
    try:
        # -spd: the name is the name (no wildcards); -bd: no progress output
        process = subprocess.Popen(  # pylint: disable=consider-using-with
            [exe, "e", "-so", "-spd", "-bd", "-sccUTF-8", "-p", "--", str(member.archive), member.name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_hidden(),
        )
    except OSError as exc:
        raise ArchiveError(f"7-Zip couldn't be run: {exc.strerror or exc}") from exc
    made, finished = 0, False
    try:
        while True:
            if should_stop is not None and should_stop():
                raise Cancelled()
            piece = process.stdout.read1(_CHUNK)
            if not piece:
                break
            made += len(piece)
            if made > member.size:
                raise ArchiveError(f"{member.base_name} unpacks bigger than the archive says (damaged).")
            yield piece
        code = process.wait(timeout=60)
        errors = process.stderr.read()
        finished = True
        if code != 0 and _seven_error(errors) == "Password-protected":
            raise ArchiveError(f"{member.base_name} is password-protected.")
        if code != 0 or made != member.size:
            raise ArchiveError(f"{member.base_name} is damaged in {member.archive.name}.")
    finally:
        if not finished:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def _damage_text(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return f"Can't be read: {exc.strerror or exc}"
    return "Damaged or not a zip file"


_memory: dict[str, tuple[tuple[int, float], Listing]] = {}
_memory_lock = threading.Lock()


def _index_file(archive: Path) -> Path:
    return index_dir() / f"{hashlib.sha1(_key(archive).encode('utf-8')).hexdigest()}.json"


def listing(archive: Path, size: Optional[int] = None, mtime: Optional[float] = None) -> Listing:
    """The archive's listing, from memory or the disk cache when the archive hasn't changed (by size and
    modified time; pass them if known, saving a look at the disk). Blocks: call off the UI thread,
    except where only a known listing is wanted (known_listing)."""
    archive = Path(archive)
    if size is None or mtime is None:
        try:
            size, mtime = archive_stamp(archive)
        except OSError as exc:
            return Listing(error=_damage_text(exc))
    stamp = (size, mtime)
    key = _key(archive)
    with _memory_lock:
        known = _memory.get(key)
    if known is not None and known[0] == stamp:
        return known[1]
    result = _read_index(archive, stamp)
    if result is None:
        result = read_listing(archive)
        if result.error == NO_SEVEN_ZIP:
            return result  # (not remembered: it lists once 7-Zip is there)
        _write_index(archive, stamp, result)
    with _memory_lock:
        _memory[key] = (stamp, result)
    return result


def known_listing(archive: Path) -> Optional[Listing]:
    """The listing if it's been read this session (never touches the disk: safe on the UI thread)."""
    with _memory_lock:
        known = _memory.get(_key(archive))
    return known[1] if known is not None else None


def forget_listings() -> None:
    """Drop the listings kept in memory (tests)."""
    with _memory_lock:
        _memory.clear()


def _read_index(archive: Path, stamp: tuple[int, float]) -> Optional[Listing]:
    try:
        data = json.loads(_index_file(archive).read_text(encoding="utf-8"))
        if data.get("version") != _INDEX_VERSION or data.get("size") != stamp[0] or data.get("mtime") != stamp[1]:
            return None
        members = tuple(
            ArchiveMember(archive, name, size, csize, method, crc, offset, bool(encrypted), modified, stamp[0], stamp[1], kind)
            for name, size, csize, method, crc, offset, encrypted, modified, kind in data["members"]
        )
        return Listing(members, str(data.get("error", "")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_index(archive: Path, stamp: tuple[int, float], result: Listing) -> None:
    data = {
        "version": _INDEX_VERSION, "path": str(archive), "size": stamp[0], "mtime": stamp[1], "error": result.error,
        "members": [
            [m.name, m.size, m.compressed_size, m.method, m.crc, m.offset, int(m.encrypted), m.modified, m.kind]
            for m in result.members
        ],
    }
    target = _index_file(archive)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(f".{threading.get_ident()}.partial")
        partial.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(partial, target)
    except OSError:
        pass  # only a speed-up


# ----------------------------------------------------------------------
# Assets
# ----------------------------------------------------------------------
def member_asset(member: ArchiveMember) -> AssetItem:
    ext = os.path.splitext(member.base_name)[1].lower()
    return AssetItem(
        path=virtual_path(member.archive, member.name), name=member.base_name, extension=ext,
        size_bytes=member.size, modified_timestamp=member.modified,
        asset_group=determine_asset_group(ext), category_name=determine_category_name(ext), archive=member,
    )


def entry_assets(archive: Path, extensions: Iterable[str], size: Optional[int] = None, mtime: Optional[float] = None) -> list[AssetItem]:
    """The archive's files with one of these extensions, as assets."""
    wanted = {e.lower() for e in extensions}
    return [
        member_asset(m) for m in listing(archive, size, mtime).members
        if os.path.splitext(m.name)[1].lower() in wanted
    ]


def expand_archives(
    assets: Sequence[AssetItem], extensions: Iterable[str], should_stop: Optional[Callable[[], bool]] = None
) -> list[AssetItem]:
    """The files inside the archives among assets (not inside nested archives)."""
    wanted = list(extensions)
    found: list[AssetItem] = []
    for asset in assets:
        if asset.is_archive and not asset.in_archive:
            if should_stop is not None and should_stop():
                break
            found.extend(entry_assets(asset.path, wanted, asset.size_bytes, asset.modified_timestamp))
    return found


def location_key(asset: AssetItem) -> str:
    """The file that holds the asset on disk (its archive, for a file inside one), for path_key comparisons."""
    return _key(asset.archive.archive if asset.archive is not None else asset.path)


def member_for_path(path: Path | str) -> Optional[ArchiveMember]:
    """The member a virtual path names, if its archive's listing is at hand (None for real paths)."""
    text = str(path)
    archive, sep, rest = text.partition(SEPARATOR)
    if not sep:
        return None
    name = rest.replace("\\", "/")
    for member in listing(Path(archive)).members:
        if member.name.replace("\\", "/") == name:
            return member
    return None


@dataclass(frozen=True)
class Summary:
    """An archive's card and tooltip: how many files, of which types, or what's wrong with it."""

    files: int
    by_type: tuple[tuple[str, int], ...]  # (TYPE, count), most first
    error: str = ""

    def badge(self, category: str) -> str:
        return f"{category} · {self.files}" if not self.error else category


def summarize(result: Listing) -> Summary:
    if result.error:
        return Summary(0, (), result.error)
    counts: dict[str, int] = {}
    for member in result.members:
        ext = os.path.splitext(member.base_name)[1].lstrip(".").upper() or "other"
        counts[ext] = counts.get(ext, 0) + 1
    ordered = tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    return Summary(len(result.members), ordered)


def card_picture_candidates(result: Listing) -> list[ArchiveMember]:
    """Images inside that could be the archive's card picture, best first: ones named like a
    preview (preview, cover, thumb, icon), then common formats, then the shallowest."""
    images = [
        m for m in result.members
        if m.base_name.lower().endswith(_PICTURE_EXTENSIONS) and 0 < m.size <= MAX_CARD_PICTURE_BYTES and not m.encrypted
    ]

    def rank(m: ArchiveMember) -> tuple:
        ext = os.path.splitext(m.base_name)[1].lower()
        return (
            not _PREVIEW_NAME.search(m.base_name), ext not in (".png", ".jpg", ".jpeg", ".webp"),
            m.name.count("/"), m.name.lower(),
        )

    return sorted(images, key=rank)


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------
def _check_readable(member: ArchiveMember) -> None:
    if member.encrypted:
        raise ArchiveError(f"{member.base_name} is password-protected.")


def iter_chunks(member: ArchiveMember, should_stop: Optional[Callable[[], bool]] = None) -> Iterator[bytes]:
    """The member's unpacked bytes, a piece at a time. Raises ArchiveError if the archive is
    damaged (bad header, cut short, wrong checksum, or more data than it says), Cancelled."""
    _check_readable(member)
    if member.kind == "7z":
        yield from _seven_chunks(member, should_stop)
        return
    if member.method not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, 9):
        yield from _zipfile_chunks(member, should_stop)
        return
    try:
        source = open(member.archive, "rb")  # pylint: disable=consider-using-with
    except OSError as exc:
        raise ArchiveError(f"{member.archive.name} can't be read: {exc.strerror or exc}") from exc
    with source:
        source.seek(member.offset)
        head = source.read(30)
        if len(head) < 30 or head[:4] != b"PK\x03\x04":
            raise ArchiveError(f"{member.archive.name} is damaged.")
        name_length, extra_length = struct.unpack("<HH", head[26:30])
        source.seek(member.offset + 30 + name_length + extra_length)
        unpack = _unpacker(member.method)
        left, made, crc = member.compressed_size, 0, 0
        step = _DEFLATE64_CHUNK if member.method == 9 else _CHUNK
        while left > 0:
            if should_stop is not None and should_stop():
                raise Cancelled()
            data = source.read(min(left, step))
            if not data:
                raise ArchiveError(f"{member.archive.name} is cut short (damaged).")
            left -= len(data)
            for piece in unpack(data):
                made += len(piece)
                if made > member.size:
                    raise ArchiveError(f"{member.base_name} unpacks bigger than the archive says (damaged).")
                crc = zlib.crc32(piece, crc)
                yield piece
        for piece in unpack(None):
            made += len(piece)
            if made > member.size:
                raise ArchiveError(f"{member.base_name} unpacks bigger than the archive says (damaged).")
            crc = zlib.crc32(piece, crc)
            yield piece
    if made != member.size or crc != member.crc:
        raise ArchiveError(f"{member.base_name} is damaged in {member.archive.name} (checksum).")


def _unpacker(method: int) -> Callable[[Optional[bytes]], Iterable[bytes]]:
    """unpack(data) -> pieces; unpack(None) at the end -> what's left."""
    if method == zipfile.ZIP_STORED:
        return lambda data: (data,) if data else ()
    if method == zipfile.ZIP_DEFLATED:
        inflater = zlib.decompressobj(-15)

        def inflate(data: Optional[bytes]) -> Iterator[bytes]:
            try:
                if data is None:
                    rest = inflater.flush()
                    if rest:
                        yield rest
                    return
                while data:  # (a limit on each piece: a zip bomb can't take all the memory at once)
                    piece = inflater.decompress(data, _CHUNK)
                    if piece:
                        yield piece
                    data = inflater.unconsumed_tail
            except zlib.error as exc:
                raise ArchiveError(f"Damaged data: {exc}") from exc

        return inflate
    try:
        import inflate64
    except ImportError as exc:  # (bundled with the app; only a broken install lacks it)
        raise ArchiveError("Deflate64 isn't supported by this install.") from exc
    inflater64 = inflate64.Inflater()

    def inflate_64(data: Optional[bytes]) -> Iterator[bytes]:
        if data is None:
            return
        try:
            piece = inflater64.inflate(data)
        except Exception as exc:  # pylint: disable=broad-except
            raise ArchiveError(f"Damaged data: {exc}") from exc
        if piece:
            yield piece

    return inflate_64


def _zipfile_chunks(member: ArchiveMember, should_stop: Optional[Callable[[], bool]]) -> Iterator[bytes]:
    """Methods other than stored / deflated / Deflate64 (bzip2, LZMA): Python's zipfile reads them."""
    made = 0
    try:
        with zipfile.ZipFile(member.archive) as zf, zf.open(member.name) as source:
            while True:
                if should_stop is not None and should_stop():
                    raise Cancelled()
                piece = source.read(_CHUNK)
                if not piece:
                    break
                made += len(piece)
                if made > member.size:
                    raise ArchiveError(f"{member.base_name} unpacks bigger than the archive says (damaged).")
                yield piece
    except NotImplementedError as exc:
        raise ArchiveError(f"{member.base_name} is packed in a way this app can't unpack.") from exc
    except (OSError, zipfile.BadZipFile, KeyError, EOFError, zlib.error) as exc:
        raise ArchiveError(f"{member.base_name} can't be read from {member.archive.name}: {exc}") from exc


def read_bytes(member: ArchiveMember, limit: int = MAX_PREVIEW_BYTES, should_stop: Optional[Callable[[], bool]] = None) -> bytes:
    """The member's content (ArchiveError if it's bigger than limit)."""
    if member.size > limit:
        raise ArchiveError(f"{member.base_name} is too big to read here.")
    return b"".join(iter_chunks(member, should_stop))


def _copy_to(member: ArchiveMember, target: Path, progress: Optional[Callable[[int, int], None]],
             should_stop: Optional[Callable[[], bool]]) -> None:
    """Unpack into target, whole or not at all (a .partial file is renamed when it's complete)."""
    partial = target.with_name(f"{target.name}.{threading.get_ident()}.partial")
    done = 0
    try:
        with open(partial, "wb") as out:
            if progress is not None:
                progress(0, member.size)
            for piece in iter_chunks(member, should_stop):
                out.write(piece)
                done += len(piece)
                if progress is not None:
                    progress(done, member.size)
        os.replace(partial, target)
    except BaseException:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise


def _cached_copy_path(member: ArchiveMember) -> Path:
    digest = hashlib.sha1(member.identity.encode("utf-8")).hexdigest()[:16]
    return extract_dir() / digest / (_safe_part(member.base_name) or "file")


def cached_copy(member: ArchiveMember) -> Optional[Path]:
    """The member's unpacked copy if there is one already (no unpacking)."""
    target = _cached_copy_path(member)
    try:
        if target.stat().st_size == member.size:
            return target
    except OSError:
        pass
    return None


def materialize(
    member: ArchiveMember, progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Path:
    """A real file with the member's content, in the unpacked-copies cache (unpacked once; the
    oldest copies are deleted beyond extract_limit_bytes()). Keeps the member's file name, so other
    apps and the video player see the right name and type. Raises ArchiveError, Cancelled."""
    target = _cached_copy_path(member)
    existing = cached_copy(member)
    if existing is not None:
        try:
            os.utime(existing)  # recently used: pruned last
        except OSError:
            pass
        if progress is not None:
            progress(member.size, member.size)
        return existing
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveError(f"Couldn't unpack {member.base_name}: {exc.strerror or exc}") from exc
    try:
        _copy_to(member, target, progress, should_stop)
    except OSError as exc:
        raise ArchiveError(f"Couldn't unpack {member.base_name}: {exc.strerror or exc}") from exc
    prune_extracted(keep=target)
    return target


@contextmanager
def local_file(member: ArchiveMember, should_stop: Optional[Callable[[], bool]] = None) -> Iterator[Path]:
    """A real file with the member's content while the block runs: its unpacked copy if it has one,
    else a temporary copy deleted afterwards (thumbnails: they'd fill the cache otherwise)."""
    existing = cached_copy(member)
    if existing is not None:
        yield existing
        return
    folder = _temp_dir() / f"{threading.get_ident()}-{time.monotonic_ns()}"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        target = folder / (_safe_part(member.base_name) or "file")
        _copy_to(member, target, None, should_stop)
        yield target
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def extracted_bytes() -> int:
    """Size of the unpacked copies."""
    total = 0
    for root, _dirs, files in os.walk(extract_dir()):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def extract_limit_bytes() -> int:
    """How much the unpacked copies may take (Settings > Storage)."""
    from .settings import get_unpacked_limit_gb

    return get_unpacked_limit_gb() * 1024 ** 3


def prune_extracted(max_bytes: Optional[int] = None, keep: Optional[Path] = None) -> int:
    """Delete the least recently used unpacked copies beyond max_bytes (default: the limit set in
    Settings > Storage; never keep). Returns how many."""
    if max_bytes is None:
        max_bytes = extract_limit_bytes()
    copies = []
    for root, _dirs, files in os.walk(extract_dir()):
        for name in files:
            path = os.path.join(root, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            copies.append((info.st_mtime, info.st_size, path))
    total = sum(size for _mtime, size, _path in copies)
    removed = 0
    keep_key = _key(keep) if keep is not None else ""
    for _mtime, size, path in sorted(copies):
        if total <= max_bytes:
            break
        if _key(path) == keep_key:
            continue
        try:
            os.unlink(path)
            total -= size
            removed += 1
            try:
                os.rmdir(os.path.dirname(path))
            except OSError:
                pass
        except OSError:
            pass  # (in use: a video playing)
    return removed


def clear_extracted() -> None:
    """Delete every unpacked copy (files in use are skipped)."""
    prune_extracted(max_bytes=0)
    shutil.rmtree(_temp_dir(), ignore_errors=True)


# ----------------------------------------------------------------------
# Extracting (the user's own copies)
# ----------------------------------------------------------------------
def _safe_part(part: str) -> str:
    """A name for one file or folder on Windows, or "" if it can't be one safely."""
    part = part.strip()
    if part in ("", ".", "..") or _UNSAFE_PART.search(part):
        return ""
    part = part.rstrip(". ")  # (Windows drops these itself: two names would land on one file)
    if not part or _RESERVED.fullmatch(part):
        return ""
    return part


def safe_relative_path(name: str) -> Optional[PurePosixPath]:
    """Where a member goes under an extraction folder, or None if its name could escape it or isn't
    valid on Windows (an absolute path, a drive, "..", reserved names or characters)."""
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        return None
    clean = [_safe_part(p) for p in parts]
    if not all(clean):
        return None
    return PurePosixPath(*clean)


@dataclass(frozen=True)
class ExtractPlan:
    """Extracting an archive into a folder: where each member goes, which are already there,
    and which can't be extracted safely (their names)."""

    targets: tuple[tuple[ArchiveMember, Path], ...]
    existing: tuple[Path, ...]
    unsafe: tuple[str, ...]


def plan_extract(members: Sequence[ArchiveMember], folder: Path) -> ExtractPlan:
    targets, existing, unsafe = [], [], []
    for member in members:
        relative = safe_relative_path(member.name)
        if relative is None:
            unsafe.append(member.name)
            continue
        target = Path(folder, *relative.parts)
        targets.append((member, target))
        if target.exists():
            existing.append(target)
    return ExtractPlan(tuple(targets), tuple(existing), tuple(unsafe))


@dataclass(frozen=True)
class ExtractResult:
    written: int
    skipped: int  # already there (and not to be replaced), or unsafe names
    failed: tuple[str, ...]  # "name: why"


def extract(
    plan: ExtractPlan, replace: bool = False, progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ExtractResult:
    """Extract a plan's members. Files already there are only replaced if replace is True (the user
    was asked); each file is written whole or not at all. Raises Cancelled."""
    existing = {_key(p) for p in plan.existing}
    todo = [(m, t) for m, t in plan.targets if replace or _key(t) not in existing]
    total = sum(m.size for m, _t in todo)
    done, written, failed = 0, 0, []
    for member, target in todo:
        if should_stop is not None and should_stop():
            raise Cancelled()

        def step(made: int, _size: int, base: int = done) -> None:
            if progress is not None:
                progress(base + made, total)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if replace and target.exists():
                os.chmod(target, stat.S_IWRITE)  # (a read-only file in the way)
            _copy_to(member, target, step, should_stop)
            written += 1
        except (ArchiveError, OSError) as exc:
            failed.append(f"{member.name}: {exc}")
        done += member.size
    skipped = len(plan.targets) - len(todo) + len(plan.unsafe)
    return ExtractResult(written, skipped, tuple(failed))


def extract_one(member: ArchiveMember, target: Path, progress: Optional[Callable[[int, int], None]] = None,
                should_stop: Optional[Callable[[], bool]] = None) -> Path:
    """Save one member as target (the user picked it, and was asked about replacing it)."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_to(member, target, progress, should_stop)
    except OSError as exc:
        raise ArchiveError(f"Couldn't save {target.name}: {exc.strerror or exc}") from exc
    return target
