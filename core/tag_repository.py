"""SQLite storage for asset tags (standard-library sqlite3, no extra dependencies).

The UI never writes SQL: everything goes through TagRepository.

Tables:
- assets      one row per file the scanner has seen, keyed by its normalized path
- tags        default tags (one per file type, read-only) and custom tags made by the user
- asset_tags  which tags each asset has

Default tags come from the file extension (core.scanner.determine_category_name
holds the extension -> display name map, e.g. .jpg/.jpeg -> "JPEG") and are
assigned whenever a scan finishes, which also backfills existing libraries.

Threading: each thread gets its own connection (sqlite3 connections must not be
shared between threads). Worker threads call close_thread_connection() when done.

Moved / renamed files: assets are keyed by path, but files with custom tags get a
content fingerprint, and relink_moved_assets() moves a vanished file's row to the
new file with the same fingerprint, so its tags follow it.

Known limitations:
- Relinking needs the file's content to be unchanged and the new location to be
  inside a scanned folder; a file moved and edited before the next scan, or two
  identical copies, start untagged rather than risk tagging the wrong file.
- After a full library scan, forget_unscanned_assets() drops the rows of files the
  scan didn't find (folders browsed with Open Folder, deleted files) unless they have
  custom tags, a favorite or a rating. Those are kept, for relinking if the file turns
  up again, and so are files on a drive that's offline.
- Tag hierarchies (a tag inside a tag) aren't supported; searches combine tags with
  AND / OR / NOT (find_assets).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence
from urllib.parse import quote

from .fingerprint import quick_hash
from .scanner import AssetItem, determine_category_name

DB_PATH: Path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AssetManager" / "library.db"

MAX_TAG_NAME_LENGTH = 40
MAX_BACKUPS = 10  # (manual ones and those made before updates / restores)
DAILY_BACKUP = "daily"  # the automatic one (backup_if_due): kept apart, so they never push the others out
MAX_DAILY_BACKUPS = 7
DAILY_BACKUP_SECONDS = 24 * 3600
MAX_RATING = 5
EXPORT_FORMAT = "asset-manager-tags"
_CHUNK = 900  # stay well under SQLite's bound-parameter limit

# Index i upgrades a database from user_version i to i + 1. Append new steps; never edit old ones.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE assets (
        id INTEGER PRIMARY KEY,
        path_key TEXT NOT NULL UNIQUE,  -- os.path.normcase(absolute path): case-insensitive on Windows
        path TEXT NOT NULL,
        name TEXT NOT NULL,
        name_fold TEXT NOT NULL,        -- casefolded name for case-insensitive search (incl. non-ASCII)
        extension TEXT NOT NULL
    );
    CREATE TABLE tags (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        kind TEXT NOT NULL CHECK (kind IN ('default', 'custom')),
        color TEXT
    );
    CREATE TABLE asset_tags (
        asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (asset_id, tag_id)
    ) WITHOUT ROWID;
    CREATE INDEX idx_asset_tags_tag ON asset_tags(tag_id);
    """,
    # v2: size / modified time from the last scan, and a content fingerprint (core.fingerprint.quick_hash)
    # for files with custom tags, so their tags follow them when they're moved or renamed
    """
    ALTER TABLE assets ADD COLUMN size INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE assets ADD COLUMN mtime REAL NOT NULL DEFAULT 0;
    ALTER TABLE assets ADD COLUMN quick_hash TEXT;
    """,
    # v3: saved searches, shown as collections in the sidebar
    """
    CREATE TABLE saved_searches (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        query TEXT NOT NULL              -- search bar text, e.g. 'tag:Hero -tag:WIP rock'
    );
    """,
    # v4: favorites and 0-5 star ratings
    """
    ALTER TABLE assets ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE assets ADD COLUMN rating INTEGER NOT NULL DEFAULT 0;
    """,
    # v5: file details for searching (res:, dur:, poly:), read in the background by core.metadata.
    # meta_key is "size:mtime" of the file when they were read, so an edited file is read again.
    """
    ALTER TABLE assets ADD COLUMN width INTEGER;
    ALTER TABLE assets ADD COLUMN height INTEGER;
    ALTER TABLE assets ADD COLUMN duration_ms INTEGER;
    ALTER TABLE assets ADD COLUMN poly_count INTEGER;
    ALTER TABLE assets ADD COLUMN meta_key TEXT;
    """,
    # v6: whether an image is an alpha (core.alphas; is:alpha). Images' details are read again
    # once, to find out.
    """
    ALTER TABLE assets ADD COLUMN alpha INTEGER;
    UPDATE assets SET meta_key = NULL WHERE extension IN
        ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.tga', '.bmp', '.webp', '.psd', '.psb', '.exr', '.dds');
    """,
    # v7: collections belong to a profile (core.profiles; NULL: shown in every profile). The ones
    # saved before profiles belong to Default. Names are unique among those shown together, which
    # the code checks (two profiles can each have a "Heroes").
    """
    CREATE TABLE saved_searches_v7 (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL COLLATE NOCASE,
        query TEXT NOT NULL,
        profile TEXT
    );
    INSERT INTO saved_searches_v7 (id, name, query, profile) SELECT id, name, query, 'default' FROM saved_searches;
    DROP TABLE saved_searches;
    ALTER TABLE saved_searches_v7 RENAME TO saved_searches;
    """,
    # v8: what pictures look like (core.picture_look: main colours, look-alike hash, small grey copy), read
    # in the background; look_key is meta_key + the look version, as meta_key is. seen_folder: the folder
    # a file was last seen in, so folder rules (core.folder_rules) tag a file once, when it arrives in a
    # folder (NULL: not handled yet; '' for files known before rules existed: never tagged by them).
    """
    ALTER TABLE assets ADD COLUMN look_colors TEXT;
    ALTER TABLE assets ADD COLUMN look_hash INTEGER;
    ALTER TABLE assets ADD COLUMN look_grey BLOB;
    ALTER TABLE assets ADD COLUMN look_key TEXT;
    ALTER TABLE assets ADD COLUMN seen_folder TEXT;
    UPDATE assets SET seen_folder = '';
    """,
    # v9: AI Search's vectors (core.ai_search: SigLIP, 768 float16), read in the background once AI Search is
    # downloaded; ai_key is meta_key + the model's version (a new model reads every file again).
    """
    ALTER TABLE assets ADD COLUMN ai_vector BLOB;
    ALTER TABLE assets ADD COLUMN ai_key TEXT;
    """,
    # v10: the web page a picture was saved from by the browser extension (core.browser_link): Open Source Page
    """
    ALTER TABLE assets ADD COLUMN web_page TEXT;
    """,
]


class TagError(ValueError):
    """Invalid tag operation; the message is suitable to show to the user."""


@dataclass(frozen=True)
class Tag:
    id: int
    name: str
    kind: str  # "default" or "custom"
    color: Optional[str] = None
    file_count: int = 0

    @property
    def is_default(self) -> bool:
        return self.kind == "default"


@dataclass(frozen=True)
class TagChange:
    """(asset_id, tag_id) pairs that one apply_tag_changes() call actually added / removed."""

    added: tuple[tuple[int, int], ...] = ()
    removed: tuple[tuple[int, int], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.added or self.removed)

    @property
    def asset_ids(self) -> list[int]:
        return list(dict.fromkeys(a for a, _t in self.added + self.removed))


@dataclass(frozen=True)
class Marks:
    """A file's favorite flag and 0-5 star rating."""

    favorite: bool = False
    rating: int = 0


NO_MARKS = Marks()


@dataclass(frozen=True)
class AssetMeta:
    """File details read from inside the file (None = not applicable or unreadable)."""

    width: Optional[int] = None
    height: Optional[int] = None
    duration_ms: Optional[int] = None
    poly_count: Optional[int] = None
    alpha: Optional[bool] = None  # an image that's an alpha (core.alphas); None for other files


def meta_key(size: int, mtime: float) -> str:
    return f"{size}:{mtime!r}"


@dataclass(frozen=True)
class SavedSearch:
    id: int
    name: str
    query: str
    profile: Optional[str] = None  # the profile it belongs to (core.profiles); None: every profile


EVERY_PROFILE = None  # (a collection's profile: shown in all of them)


def default_tag_name(extension: str) -> str:
    """Display name of the default tag for a file extension (".jpeg" -> "JPEG")."""
    return determine_category_name(extension)


def path_key(path: Path | str) -> str:
    """Stable lookup key for a file path (absolute, Windows case-insensitive)."""
    return os.path.normcase(os.path.abspath(str(path)))


def folder_of_key(key: str) -> str:
    """The folder a file is in, by its path_key (a file inside an archive: the archive's folder)."""
    return os.path.dirname(key.partition("|")[0])


def look_key(size: int, mtime: float) -> str:
    from .picture_look import LOOK_VERSION

    return f"{meta_key(size, mtime)}:v{LOOK_VERSION}"


def ai_key(size: int, mtime: float) -> str:
    from .ai_tools import TOOLS

    return f"{meta_key(size, mtime)}:{TOOLS['search'].version}"


def _is_missing(path: str) -> bool:
    """The file is gone, and not just on a drive or network share that's offline."""
    return not os.path.exists(path) and os.path.exists(Path(path).anchor or ".")


def _chunks(items: Sequence, size: int = _CHUNK) -> Iterator[Sequence]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _placeholders(count: int) -> str:
    return ",".join("?" * count)


def is_damage_error(message: str) -> bool:
    """Whether a database error means the file itself is damaged (not, say, locked or busy)."""
    return any(sign in message.lower() for sign in ("malformed", "not a database", "file is encrypted", "corrupt"))


def _healthy(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        return False


def _read_only_uri(backup: Path) -> str:
    """Open a backup only to read it, leaving no -wal / -shm files beside it (a finished backup
    has nothing waiting in a write-ahead log, so it's safe to treat as unchanging)."""
    return f"file:{quote(backup.as_posix(), safe='/:')}?mode=ro&immutable=1"  # (a "#" or "%" in the path is escaped)


def _is_daily(backup: Path) -> bool:
    return backup.stem.endswith(f"_{DAILY_BACKUP}")


class TagRepository:
    """Data access for assets and tags. Safe to use from several threads."""

    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = str(db_path)
        self._local = threading.local()

    # ------------------------------------------------------------------
    # Connections & schema
    # ------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self.db_path != ":memory:":
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None: transactions are explicit (see _transaction)
            conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                if self.db_path != ":memory:":
                    conn.execute("PRAGMA journal_mode = WAL")
                self._migrate(conn)
            except BaseException:
                conn.close()  # (now: a damaged file stays open otherwise, until garbage collection, and can't be replaced)
                raise
            self._local.conn = conn
        return conn

    def close_thread_connection(self) -> None:
        """Close this thread's connection (worker threads call this when they finish)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if 0 < version < len(MIGRATIONS) and self.db_path != ":memory:":
            # Keep a copy of the old database in case an upgrade goes wrong
            self._backup_connection(conn, f"before-upgrade-v{version}")
        for target in range(version, len(MIGRATIONS)):
            # One atomic script per step; user_version can't be a bound parameter
            conn.executescript(f"BEGIN; {MIGRATIONS[target]} PRAGMA user_version = {target + 1}; COMMIT;")

    # ------------------------------------------------------------------
    # Backups
    # ------------------------------------------------------------------
    def backup_dir(self) -> Path:
        return Path(self.db_path).parent / "backups"

    def backup_database(self, reason: str = "manual") -> Optional[Path]:
        """Copy the database to backups/ (consistent even while it's in use). Returns the copy."""
        if self.db_path == ":memory:":
            return None
        return self._backup_connection(self._conn(), reason)

    def _backup_connection(self, conn: sqlite3.Connection, reason: str) -> Path:
        folder = self.backup_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        safe_reason = "".join(c if c.isalnum() or c in "-." else "-" for c in reason)
        target = folder / f"library_{stamp}_{safe_reason}.db"
        count = 1
        while target.exists():  # (two in the same second: neither replaces the other)
            count += 1
            target = folder / f"library_{stamp}-{count}_{safe_reason}.db"
        dest = sqlite3.connect(target)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        daily = _is_daily(target)
        backups = [b for b in self.list_backups() if _is_daily(b) == daily]  # (each kind has its own limit)
        for old in backups[MAX_DAILY_BACKUPS if daily else MAX_BACKUPS:]:
            try:
                old.unlink()
            except OSError:
                pass
        return target

    def backup_if_due(self) -> Optional[Path]:
        """The automatic backup: made when the newest one is a day old (or there's none), and only
        of a healthy database, so a damaged one never replaces good copies. Returns the copy, or
        None if none was due. TagError if the database is damaged. Safe off the UI thread."""
        if self.db_path == ":memory:":
            return None
        daily = [b for b in self.list_backups() if _is_daily(b)]
        if daily and time.time() - daily[0].stat().st_mtime < DAILY_BACKUP_SECONDS:
            return None
        if not _healthy(self._conn()):
            raise TagError("The library database is damaged.")
        return self.backup_database(DAILY_BACKUP)

    def newest_good_backup(self) -> Optional[Path]:
        """The newest backup that's a readable, undamaged library (to offer when the database is damaged)."""
        for backup in self.list_backups():
            try:
                source = sqlite3.connect(_read_only_uri(backup), uri=True)
            except sqlite3.Error:
                continue
            try:
                version = source.execute("PRAGMA user_version").fetchone()[0]
                if 1 <= version <= len(MIGRATIONS) and _healthy(source):
                    return backup
            except sqlite3.DatabaseError:
                pass
            finally:
                source.close()
        return None

    def list_backups(self) -> list[Path]:
        """Backups, newest first."""
        if not self.backup_dir().is_dir():
            return []
        return sorted(self.backup_dir().glob("library_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)

    def restore_database(self, backup: Path | str) -> None:
        """Replace the library's contents with a backup (a backup of the current state is made first).
        A damaged database can't be copied or written over page by page: it's kept as it is in
        backups/ (library_..._damaged.db) and the backup goes into a fresh database instead."""
        source = sqlite3.connect(_read_only_uri(Path(backup)), uri=True)
        try:
            try:
                version = source.execute("PRAGMA user_version").fetchone()[0]
                readable = 1 <= version <= len(MIGRATIONS) and _healthy(source)
            except sqlite3.DatabaseError as exc:
                raise TagError(f"The backup couldn't be read: {exc}") from exc
            if not readable:
                raise TagError("That file isn't an Asset Manager library backup, it's damaged, or it's from a newer version.")
            if self._is_healthy():
                self.backup_database("before-restore")
                source.backup(self._conn())
            else:
                self._start_afresh_keeping_the_damaged_file()
                source.backup(self._conn())
        finally:
            source.close()
        self._migrate(self._conn())  # an older backup is upgraded to the current schema

    def _is_healthy(self) -> bool:
        try:
            return _healthy(self._conn())
        except sqlite3.DatabaseError:
            return False

    def _start_afresh_keeping_the_damaged_file(self) -> None:
        self.close_thread_connection()
        path = Path(self.db_path)
        folder = self.backup_dir()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(path, folder / f"library_{datetime.now():%Y-%m-%d_%H%M%S}_damaged.db")
            for part in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                part.unlink(missing_ok=True)
        except OSError as exc:  # (still open in a background job)
            raise TagError("The damaged database is still in use. Close and reopen Asset Manager, then restore again "
                           f"(Tools > Restore Library Backup). ({exc})") from exc

    # ------------------------------------------------------------------
    # Export / import (JSON)
    # ------------------------------------------------------------------
    def export_tags(self, target: Path | str, profile_names: Optional[dict[str, str]] = None) -> int:
        """Write custom tags and the files they're on to a JSON file. Returns the number of files.
        profile_names: {profile id: name}, so collections keep their profile (by name) on import."""
        conn = self._conn()
        tags = [
            {"name": row["name"], "color": row["color"]}
            for row in conn.execute("SELECT name, color FROM tags WHERE kind = 'custom' ORDER BY name COLLATE NOCASE")
        ]
        files: dict[str, dict] = {}
        for row in conn.execute(
            "SELECT a.path, t.name FROM asset_tags at JOIN assets a ON a.id = at.asset_id "
            "JOIN tags t ON t.id = at.tag_id WHERE t.kind = 'custom' ORDER BY a.path, t.name COLLATE NOCASE"
        ):
            files.setdefault(row["path"], {"path": row["path"], "tags": []})["tags"].append(row["name"])
        for row in conn.execute("SELECT path, favorite, rating FROM assets WHERE favorite OR rating ORDER BY path"):
            entry = files.setdefault(row["path"], {"path": row["path"], "tags": []})
            entry.update(favorite=bool(row["favorite"]), rating=row["rating"])
        data = {
            "format": EXPORT_FORMAT,
            "version": 1,
            "exported": datetime.now().isoformat(timespec="seconds"),
            "tags": tags,
            "files": list(files.values()),
            "searches": [
                {"name": s.name, "query": s.query, "profile": (profile_names or {}).get(s.profile, s.profile) if s.profile else None}
                for s in self.list_saved_searches()
            ],
        }
        Path(target).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return len(files)

    def import_tags(
        self, source: Path | str, profile_ids: Optional[dict[str, str]] = None, current_profile: Optional[str] = None,
    ) -> dict[str, int]:
        """Merge an export into the library: missing tags are created and added to the listed files,
        and saved searches with new names are added (nothing is removed). A collection goes to the
        profile of its name (profile_ids: {folded name: id}), else to current_profile; one shown in
        every profile stays so. Returns counts: tags_created, files, skipped_tags, searches."""
        try:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TagError(f"The file couldn't be read: {exc}") from exc
        if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT:
            raise TagError("That file isn't an Asset Manager tag export.")

        existing = {t.name.casefold(): t for t in self.list_tags()}
        tag_ids: dict[str, int] = {}
        created = skipped = 0
        with self._transaction() as conn:
            for entry in data.get("tags", []):
                name = " ".join(str(entry.get("name", "")).split())[:MAX_TAG_NAME_LENGTH]
                if not name:
                    continue
                tag = existing.get(name.casefold())
                if tag is not None and tag.is_default:
                    skipped += 1  # a file type tag has that name now
                    continue
                if tag is None:
                    color = entry.get("color") if isinstance(entry.get("color"), str) else None
                    tag_id = conn.execute(
                        "INSERT INTO tags (name, kind, color) VALUES (?, 'custom', ?)", (name, color)
                    ).lastrowid
                    tag = Tag(tag_id, name, "custom", color)
                    existing[name.casefold()] = tag
                    created += 1
                tag_ids[name.casefold()] = tag.id

            files = 0
            for entry in data.get("files", []):
                path = entry.get("path")
                names = [n for n in entry.get("tags", []) if isinstance(n, str) and n.casefold() in tag_ids]
                favorite = entry.get("favorite") is True
                rating = entry.get("rating") if isinstance(entry.get("rating"), int) else 0
                rating = max(0, min(MAX_RATING, rating))
                if not isinstance(path, str) or not path or not (names or favorite or rating):
                    continue
                p = Path(path)
                conn.execute(
                    "INSERT OR IGNORE INTO assets (path_key, path, name, name_fold, extension) VALUES (?, ?, ?, ?, ?)",
                    (path_key(p), str(p), p.name, p.name.casefold(), p.suffix.lower()),
                )
                asset_id = conn.execute("SELECT id FROM assets WHERE path_key = ?", (path_key(p),)).fetchone()[0]
                conn.executemany(
                    "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) VALUES (?, ?)",
                    [(asset_id, tag_ids[n.casefold()]) for n in names],
                )
                if favorite or rating:  # merge: never un-favorite or lower a rating that's set
                    conn.execute(
                        "UPDATE assets SET favorite = MAX(favorite, ?), "
                        "rating = CASE WHEN ? > 0 THEN ? ELSE rating END WHERE id = ?",
                        (int(favorite), rating, rating, asset_id),
                    )
                files += 1

            saved = self.list_saved_searches()
            searches = 0
            for entry in data.get("searches", []):
                name = " ".join(str(entry.get("name", "")).split())[:MAX_TAG_NAME_LENGTH]
                query = str(entry.get("query", "")).strip()
                if "profile" in entry and entry["profile"] is None:
                    profile = EVERY_PROFILE
                else:
                    wanted = " ".join(str(entry.get("profile") or "").split()).casefold()
                    profile = (profile_ids or {}).get(wanted, current_profile)
                if name and query and not _name_clash(saved, name, profile):
                    search_id = conn.execute(
                        "INSERT INTO saved_searches (name, query, profile) VALUES (?, ?, ?)", (name, query, profile)
                    ).lastrowid
                    saved.append(SavedSearch(search_id, name, query, profile))
                    searches += 1
        return {"tags_created": created, "files": files, "skipped_tags": skipped, "searches": searches}

    def schema_version(self) -> int:
        return self._conn().execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------
    def sync_assets(self, assets: Iterable[AssetItem], folder_rules: Sequence = ()) -> dict[str, int]:
        """Record scanned assets and give each its default file-type tag, and files that arrived in a
        folder since the last scan the tags of the folder rules (core.folder_rules) that cover them.

        Returns {str(asset.path): asset_id}. Runs as one transaction, so it's
        cheap to call after every scan; it also backfills older libraries.
        """
        assets = list(assets)
        rows = [
            (path_key(a.path), str(a.path), a.name, a.name.casefold(), a.extension.lower(), a.size_bytes, a.modified_timestamp)
            for a in assets
        ]
        default_names = {ext: default_tag_name(ext) for ext in {row[4] for row in rows}}
        with self._transaction() as conn:
            conn.executemany(
                "INSERT INTO assets (path_key, path, name, name_fold, extension, size, mtime) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path_key) DO UPDATE SET path = excluded.path, name = excluded.name, "
                "name_fold = excluded.name_fold, extension = excluded.extension, "
                # an edited file needs a new fingerprint
                "quick_hash = CASE WHEN assets.size = excluded.size AND assets.mtime = excluded.mtime "
                "THEN assets.quick_hash END, "
                "size = excluded.size, mtime = excluded.mtime "
                # unchanged rows aren't rewritten (most of them, on every scan)
                "WHERE assets.path IS NOT excluded.path OR assets.size IS NOT excluded.size "
                "OR assets.mtime IS NOT excluded.mtime",
                rows,
            )
            tag_ids: dict[str, int] = {}
            for name in set(default_names.values()):
                existing = conn.execute("SELECT id, kind FROM tags WHERE name = ?", (name,)).fetchone()
                if existing is None:
                    tag_ids[name] = conn.execute(
                        "INSERT INTO tags (name, kind) VALUES (?, 'default')", (name,)
                    ).lastrowid
                else:
                    if existing["kind"] != "default":
                        # A custom tag already uses a newly seen file type's name: it becomes that type's tag
                        conn.execute("UPDATE tags SET kind = 'default', color = NULL WHERE id = ?", (existing["id"],))
                    tag_ids[name] = existing["id"]
            conn.executemany(
                "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) "
                "SELECT id, ? FROM assets WHERE path_key = ?",
                [(tag_ids[default_names[row[4]]], row[0]) for row in rows],
            )
            ids: dict[str, int] = {}
            by_key = {row[0]: row[1] for row in rows}
            keys = list(by_key)
            for chunk in _chunks(keys):
                for found in conn.execute(
                    f"SELECT path_key, id FROM assets WHERE path_key IN ({_placeholders(len(chunk))})", chunk
                ):
                    ids[by_key[found["path_key"]]] = found["id"]
            self._apply_folder_rules(conn, set(by_key), folder_rules)
        return ids

    def _apply_folder_rules(self, conn: sqlite3.Connection, keys: set[str], folder_rules: Sequence) -> None:
        """Files among keys not yet seen in their folder: tag them by the rules covering it, and remember
        the folder (files known before rules existed only have their folder remembered)."""
        arrived = [
            (row["id"], row["path_key"], row["seen_folder"]) for row in conn.execute(
                "SELECT id, path_key, seen_folder FROM assets WHERE seen_folder IS NULL OR seen_folder = ''"
            ) if row["path_key"] in keys
        ]
        if not arrived:
            return
        links: list[tuple[int, int]] = []
        tag_ids: dict[str, Optional[int]] = {}
        for asset_id, key, seen in arrived:
            if seen is not None:
                continue  # (known before folder rules: never tagged by them)
            for name in dict.fromkeys(n for rule in folder_rules if rule.covers(key) for n in rule.tags):
                if name not in tag_ids:
                    tag_ids[name] = self._rule_tag_id(conn, name)
                if tag_ids[name] is not None:
                    links.append((asset_id, tag_ids[name]))
        conn.executemany("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) VALUES (?, ?)", links)
        conn.executemany(
            "UPDATE assets SET seen_folder = ? WHERE id = ?", [(folder_of_key(key), asset_id) for asset_id, key, _seen in arrived]
        )

    @staticmethod
    def _rule_tag_id(conn: sqlite3.Connection, name: str) -> Optional[int]:
        """A folder rule's tag (made if it doesn't exist yet; None if it's a file type tag's name)."""
        name = " ".join(name.split())[:MAX_TAG_NAME_LENGTH]
        if not name:
            return None
        folded = name.casefold()  # (NOCASE only folds ASCII)
        row = next((r for r in conn.execute("SELECT id, kind, name FROM tags") if r["name"].casefold() == folded), None)
        if row is None:
            return conn.execute("INSERT INTO tags (name, kind) VALUES (?, 'custom')", (name,)).lastrowid
        return row["id"] if row["kind"] == "custom" else None

    def files_in_folder_rule(self, rule) -> int:
        """How many files the library knows in a rule's folder."""
        return sum(1 for row in self._conn().execute("SELECT path_key FROM assets") if rule.covers(row["path_key"]))

    def apply_folder_rule(self, rule, asset_keys: Optional[Iterable[str]] = None) -> int:
        """Give the files already in a rule's folder its tags (rule: a core.folder_rules.FolderRule).
        asset_keys: the path_keys to consider (default: every file the library knows). Returns how many
        files got a tag they didn't have."""
        wanted = set(asset_keys) if asset_keys is not None else None
        with self._transaction() as conn:
            ids = [
                row["id"] for row in conn.execute("SELECT id, path_key FROM assets")
                if (wanted is None or row["path_key"] in wanted) and rule.covers(row["path_key"])
            ]
            tag_ids = [t for t in (self._rule_tag_id(conn, name) for name in rule.tags) if t is not None]
            if not ids or not tag_ids:
                return 0
            tagged: set[int] = set()
            for chunk in _chunks(ids):
                for row in conn.execute(
                    f"SELECT asset_id, COUNT(*) AS n FROM asset_tags WHERE tag_id IN ({_placeholders(len(tag_ids))}) "
                    f"AND asset_id IN ({_placeholders(len(chunk))}) GROUP BY asset_id",
                    [*tag_ids, *chunk],
                ):
                    if row["n"] == len(tag_ids):
                        tagged.add(row["asset_id"])
                conn.executemany(
                    "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) VALUES (?, ?)",
                    [(asset_id, tag_id) for asset_id in chunk for tag_id in tag_ids],
                )
        return len(ids) - len(tagged)

    # Files with something worth carrying over when they move: custom tags, favorite, rating
    _CUSTOM_TAGGED = (
        "(a.favorite OR a.rating OR EXISTS (SELECT 1 FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
        "WHERE at.asset_id = a.id AND t.kind = 'custom'))"
    )

    def relink_moved_assets(self, assets: Iterable[AssetItem]) -> int:
        """Carry custom tags over to files that were moved or renamed outside the app.

        Call with a scan's results before sync_assets(). A tagged file whose old path is
        gone is matched to a newly found file with the same extension, size and content
        fingerprint; when exactly one such file exists, the old database row is moved to
        the new path (so its tags, favorite and rating come along). Returns how many
        files were relinked.
        """
        scanned = {path_key(a.path): a for a in assets}
        conn = self._conn()
        tagged = conn.execute(
            f"SELECT a.id, a.path_key, a.path, a.extension, a.size, a.quick_hash FROM assets a "
            f"WHERE a.quick_hash IS NOT NULL AND {self._CUSTOM_TAGGED}"
        ).fetchall()
        missing = [r for r in tagged if r["path_key"] not in scanned and _is_missing(r["path"])]
        if not missing:
            return 0

        known: set[str] = set()
        keys = list(scanned)
        for chunk in _chunks(keys):
            known.update(
                row[0] for row in conn.execute(
                    f"SELECT path_key FROM assets WHERE path_key IN ({_placeholders(len(chunk))})", chunk
                )
            )
        candidates: dict[tuple[str, int], list[AssetItem]] = {}
        for key, asset in scanned.items():
            if key not in known:
                candidates.setdefault((asset.extension.lower(), asset.size_bytes), []).append(asset)

        hashes: dict[str, Optional[str]] = {}
        claimed: set[str] = set()
        moves: list[tuple] = []
        for row in missing:
            matches = []
            for asset in candidates.get((row["extension"], row["size"]), []):
                key = path_key(asset.path)
                if key in claimed:
                    continue
                if key not in hashes:
                    hashes[key] = quick_hash(asset.path)
                if hashes[key] == row["quick_hash"]:
                    matches.append(asset)
            if len(matches) == 1:  # ambiguous matches are left alone rather than guessed
                new = matches[0]
                claimed.add(path_key(new.path))
                moves.append((path_key(new.path), str(new.path), new.name, new.name.casefold(), row["id"]))
        if moves:
            with self._transaction() as tx:
                tx.executemany(
                    "UPDATE assets SET path_key = ?, path = ?, name = ?, name_fold = ? WHERE id = ?", moves
                )
                # moved to another folder: it arrived there (its folder rules apply in sync_assets)
                tx.executemany(
                    "UPDATE assets SET seen_folder = NULL WHERE id = ? AND seen_folder != '' AND seen_folder IS NOT ?",
                    [(move[4], folder_of_key(move[0])) for move in moves],
                )
        return len(moves)

    def move_asset_paths(self, moves: Sequence[tuple[Path | str, Path | str]]) -> int:
        """Files renamed or moved by the app (core.batch_rename): their rows follow them, so tags, favorites,
        ratings and details stay. A stale row already at a new path (a file long gone) makes way. Returns
        how many rows moved."""
        moved = 0
        with self._transaction() as conn:
            for old, new in moves:
                old_key, new_key, new = path_key(old), path_key(new), Path(new)
                if old_key == new_key:  # (only the letter case changed: the key is the same)
                    conn.execute("UPDATE assets SET path = ?, name = ?, name_fold = ? WHERE path_key = ?",
                                 (str(new), new.name, new.name.casefold(), old_key))
                    moved += 1
                    continue
                conn.execute("DELETE FROM assets WHERE path_key = ?", (new_key,))
                moved += conn.execute(
                    "UPDATE assets SET path_key = ?, path = ?, name = ?, name_fold = ? WHERE path_key = ?",
                    (new_key, str(new), new.name, new.name.casefold(), old_key),
                ).rowcount
        return moved

    def forget_unscanned_assets(
        self, assets: Iterable[AssetItem], keep_folders: Sequence[Path | str] = (),
        scanned_folders: Sequence[Path | str] = (),
    ) -> int:
        """Delete the rows of files that a full library scan (assets) didn't find, e.g. files in
        folders opened with Open Folder, or deleted ones, unless they have custom tags, a favorite
        or a rating to keep. Files on a drive that's offline are kept, and so are files in
        keep_folders (other profiles' library folders: their details aren't read again on
        switching back) unless they're in scanned_folders too, where the scan found them gone.
        Returns how many were removed."""
        scanned = {path_key(a.path) for a in assets}
        drives: dict[str, bool] = {}

        def within(folders: Sequence[Path | str]) -> tuple[str, ...]:
            return tuple(path_key(f).rstrip("\\/") + os.sep for f in folders)

        keep, looked = within(keep_folders), within(scanned_folders)

        def kept(key: str) -> bool:
            return key.startswith(keep) and not key.startswith(looked) if keep else False

        def drive_online(path: str) -> bool:
            anchor = Path(path).anchor or "."
            if anchor not in drives:
                drives[anchor] = os.path.exists(anchor)
            return drives[anchor]

        with self._transaction() as conn:  # (a file tagged meanwhile can't be removed)
            gone = [
                row["id"] for row in conn.execute(
                    f"SELECT a.id, a.path_key, a.path FROM assets a WHERE NOT {self._CUSTOM_TAGGED}"
                )
                if row["path_key"] not in scanned and not kept(row["path_key"]) and drive_online(row["path"])
            ]
            for chunk in _chunks(gone):
                conn.execute(f"DELETE FROM assets WHERE id IN ({_placeholders(len(chunk))})", chunk)
        return len(gone)

    def fill_missing_hashes(self) -> int:
        """Fingerprint tagged / favorite / rated files that don't have one yet (so they can be relinked if moved)."""
        rows = self._conn().execute(
            f"SELECT a.id, a.path FROM assets a WHERE a.quick_hash IS NULL AND {self._CUSTOM_TAGGED}"
        ).fetchall()
        updates = [(h, row["id"]) for row in rows if (h := quick_hash(row["path"])) is not None]
        if updates:
            with self._transaction() as conn:
                conn.executemany("UPDATE assets SET quick_hash = ? WHERE id = ?", updates)
        return len(updates)

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------
    def validate_tag_name(self, name: str, exclude_tag_id: Optional[int] = None) -> str:
        """Trimmed name if it's usable for a custom tag, else raise TagError."""
        name = " ".join((name or "").split())  # trim and collapse inner whitespace
        if not name:
            raise TagError("Tag names can't be empty.")
        if len(name) > MAX_TAG_NAME_LENGTH:
            raise TagError(f"Tag names can be at most {MAX_TAG_NAME_LENGTH} characters.")
        folded = name.casefold()
        for row in self._conn().execute("SELECT id, name, kind FROM tags"):
            if row["id"] != exclude_tag_id and row["name"].casefold() == folded:
                kind = "file type tag" if row["kind"] == "default" else "tag"
                raise TagError(f'A {kind} named "{row["name"]}" already exists.')
        return name

    def create_custom_tag(self, name: str, color: Optional[str] = None) -> Tag:
        name = self.validate_tag_name(name)
        with self._transaction() as conn:
            tag_id = conn.execute("INSERT INTO tags (name, kind, color) VALUES (?, 'custom', ?)", (name, color)).lastrowid
        return Tag(tag_id, name, "custom", color, 0)

    def rename_custom_tag(self, tag_id: int, new_name: str) -> Tag:
        """Rename a custom tag; saved searches that use it are changed to the new name."""
        from .search_query import rename_tag_in_query  # (it imports this module)

        old_name = self._require_custom(tag_id).name
        new_name = self.validate_tag_name(new_name, exclude_tag_id=tag_id)
        with self._transaction() as conn:
            conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, tag_id))
            searches = conn.execute("SELECT id, query FROM saved_searches").fetchall()
            conn.executemany(
                "UPDATE saved_searches SET query = ? WHERE id = ?",
                [
                    (query, row["id"]) for row in searches
                    if (query := rename_tag_in_query(row["query"], old_name, new_name)) != row["query"]
                ],
            )
        return self.get_tag(tag_id)

    def set_tag_color(self, tag_id: int, color: Optional[str]) -> Tag:
        self._require_custom(tag_id)
        with self._transaction() as conn:
            conn.execute("UPDATE tags SET color = ? WHERE id = ?", (color, tag_id))
        return self.get_tag(tag_id)

    def delete_custom_tag(self, tag_id: int) -> int:
        """Delete a custom tag (and, by cascade, its assignments). Returns the number of files it was on."""
        tag = self._require_custom(tag_id)
        with self._transaction() as conn:
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        return tag.file_count

    def get_tag(self, tag_id: int) -> Optional[Tag]:
        row = self._conn().execute(
            "SELECT t.id, t.name, t.kind, t.color, COUNT(at.asset_id) AS file_count "
            "FROM tags t LEFT JOIN asset_tags at ON at.tag_id = t.id WHERE t.id = ? GROUP BY t.id",
            (tag_id,),
        ).fetchone()
        return self._row_to_tag(row) if row else None

    def find_tag(self, name: str) -> Optional[Tag]:
        """Tag by name, ignoring case (including non-ASCII letters)."""
        folded = " ".join(name.split()).casefold()
        return next((t for t in self.list_tags() if t.name.casefold() == folded), None)

    def list_tags(self, kind: Optional[str] = None) -> list[Tag]:
        """All tags (or one kind) with how many files each is on, sorted by name."""
        sql = (
            "SELECT t.id, t.name, t.kind, t.color, COUNT(at.asset_id) AS file_count "
            "FROM tags t LEFT JOIN asset_tags at ON at.tag_id = t.id "
        )
        params: tuple = ()
        if kind is not None:
            sql += "WHERE t.kind = ? "
            params = (kind,)
        sql += "GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
        return [self._row_to_tag(row) for row in self._conn().execute(sql, params)]

    def _require_custom(self, tag_id: int) -> Tag:
        tag = self.get_tag(tag_id)
        if tag is None:
            raise TagError("That tag no longer exists.")
        if tag.is_default:
            raise TagError(f'"{tag.name}" is a file type tag; it can\'t be changed or removed.')
        return tag

    def _require_custom_ids(self, conn: sqlite3.Connection, tag_ids: Sequence[int]) -> None:
        if not tag_ids:
            return
        rows = conn.execute(
            f"SELECT name FROM tags WHERE kind = 'default' AND id IN ({_placeholders(len(tag_ids))})", list(tag_ids)
        ).fetchall()
        if rows:
            raise TagError(f'"{rows[0]["name"]}" is a file type tag; it can\'t be added or removed manually.')

    @staticmethod
    def _row_to_tag(row: sqlite3.Row) -> Tag:
        return Tag(row["id"], row["name"], row["kind"], row["color"], row["file_count"])

    # ------------------------------------------------------------------
    # Tagging files
    # ------------------------------------------------------------------
    def add_tags(self, asset_ids: Sequence[int], tag_ids: Sequence[int]) -> None:
        self.apply_tag_changes(asset_ids, add_tag_ids=tag_ids, remove_tag_ids=())

    def remove_tags(self, asset_ids: Sequence[int], tag_ids: Sequence[int]) -> None:
        self.apply_tag_changes(asset_ids, add_tag_ids=(), remove_tag_ids=tag_ids)

    def apply_tag_changes(
        self, asset_ids: Sequence[int], add_tag_ids: Sequence[int], remove_tag_ids: Sequence[int]
    ) -> TagChange:
        """Add and remove custom tags on many files in a single transaction.

        Returns exactly which (file, tag) pairs changed, so it can be undone precisely
        (a file that already had an added tag keeps it on undo)."""
        asset_ids, add_tag_ids, remove_tag_ids = list(asset_ids), list(add_tag_ids), list(remove_tag_ids)
        with self._transaction() as conn:
            self._require_custom_ids(conn, add_tag_ids + remove_tag_ids)
            existing: set[tuple[int, int]] = set()
            tag_ids = list(dict.fromkeys(add_tag_ids + remove_tag_ids))
            if tag_ids:
                for chunk in _chunks(asset_ids, _CHUNK - len(tag_ids)):
                    existing.update(
                        (row[0], row[1]) for row in conn.execute(
                            f"SELECT asset_id, tag_id FROM asset_tags WHERE tag_id IN ({_placeholders(len(tag_ids))}) "
                            f"AND asset_id IN ({_placeholders(len(chunk))})",
                            [*tag_ids, *chunk],
                        )
                    )
            added = [(a, t) for t in add_tag_ids for a in asset_ids if (a, t) not in existing]
            removed = [(a, t) for t in remove_tag_ids for a in asset_ids if (a, t) in existing]
            conn.executemany("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) VALUES (?, ?)", added)
            conn.executemany("DELETE FROM asset_tags WHERE asset_id = ? AND tag_id = ?", removed)
        return TagChange(tuple(added), tuple(removed))

    def undo_tag_change(self, change: TagChange) -> None:
        self._swap_pairs(add=change.removed, remove=change.added)

    def redo_tag_change(self, change: TagChange) -> None:
        self._swap_pairs(add=change.added, remove=change.removed)

    def _swap_pairs(self, add: Sequence[tuple[int, int]], remove: Sequence[tuple[int, int]]) -> None:
        with self._transaction() as conn:
            # Tags or files deleted since are skipped rather than failing the whole undo
            conn.executemany(
                "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) SELECT ?, ? "
                "WHERE EXISTS (SELECT 1 FROM assets WHERE id = ?1) AND EXISTS (SELECT 1 FROM tags WHERE id = ?2)",
                list(add),
            )
            conn.executemany("DELETE FROM asset_tags WHERE asset_id = ? AND tag_id = ?", list(remove))

    def custom_tag_snapshot(self, tag_id: int) -> tuple[Tag, list[int]]:
        """A custom tag and the files it's on, to restore_custom_tag() after deleting it."""
        tag = self._require_custom(tag_id)
        asset_ids = [row[0] for row in self._conn().execute("SELECT asset_id FROM asset_tags WHERE tag_id = ?", (tag_id,))]
        return tag, asset_ids

    def restore_custom_tag(self, tag: Tag, asset_ids: Sequence[int]) -> None:
        """Bring back a deleted custom tag with its id, color and files."""
        name = self.validate_tag_name(tag.name)
        with self._transaction() as conn:
            conn.execute("INSERT INTO tags (id, name, kind, color) VALUES (?, ?, 'custom', ?)", (tag.id, name, tag.color))
            conn.executemany(
                "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) SELECT ?, ? WHERE EXISTS (SELECT 1 FROM assets WHERE id = ?1)",
                [(a, tag.id) for a in asset_ids],
            )

    def get_tags_for_assets(self, asset_ids: Sequence[int]) -> dict[int, int]:
        """{tag_id: how many of asset_ids have it}. Compare with len(asset_ids) for all / some / none."""
        counts: dict[int, int] = {}
        for chunk in _chunks(list(asset_ids)):
            for row in self._conn().execute(
                f"SELECT tag_id, COUNT(*) AS n FROM asset_tags WHERE asset_id IN ({_placeholders(len(chunk))}) "
                "GROUP BY tag_id",
                chunk,
            ):
                counts[row["tag_id"]] = counts.get(row["tag_id"], 0) + row["n"]
        return counts

    def tags_by_asset(self, asset_ids: Sequence[int]) -> dict[int, list[Tag]]:
        """{asset_id: its tags}, default (file type) tags first, then custom tags by name."""
        tags = {t.id: t for t in self.list_tags()}
        result: dict[int, list[Tag]] = {a: [] for a in asset_ids}
        for chunk in _chunks(list(asset_ids)):
            for row in self._conn().execute(
                f"SELECT asset_id, tag_id FROM asset_tags WHERE asset_id IN ({_placeholders(len(chunk))})", chunk
            ):
                result[row["asset_id"]].append(tags[row["tag_id"]])
        for tag_list in result.values():
            tag_list.sort(key=lambda t: (not t.is_default, t.name.casefold()))
        return result

    # ------------------------------------------------------------------
    # Favorites & ratings
    # ------------------------------------------------------------------
    def set_favorite(self, asset_ids: Sequence[int], favorite: bool) -> None:
        with self._transaction() as conn:
            conn.executemany("UPDATE assets SET favorite = ? WHERE id = ?", [(int(favorite), a) for a in asset_ids])

    def set_rating(self, asset_ids: Sequence[int], rating: int) -> None:
        rating = max(0, min(MAX_RATING, int(rating)))
        with self._transaction() as conn:
            conn.executemany("UPDATE assets SET rating = ? WHERE id = ?", [(rating, a) for a in asset_ids])

    def restore_marks(self, marks: dict[int, Marks]) -> None:
        """Set each file's favorite and rating back (for undo)."""
        with self._transaction() as conn:
            conn.executemany(
                "UPDATE assets SET favorite = ?, rating = ? WHERE id = ?",
                [(int(m.favorite), max(0, min(MAX_RATING, m.rating)), a) for a, m in marks.items()],
            )

    def marks_by_asset(self, asset_ids: Sequence[int]) -> dict[int, Marks]:
        """{asset_id: Marks} for assets that are a favorite or rated (others are left out)."""
        result: dict[int, Marks] = {}
        for chunk in _chunks(list(asset_ids)):
            for row in self._conn().execute(
                f"SELECT id, favorite, rating FROM assets WHERE (favorite OR rating) "
                f"AND id IN ({_placeholders(len(chunk))})",
                chunk,
            ):
                result[row["id"]] = Marks(bool(row["favorite"]), row["rating"])
        return result

    # ------------------------------------------------------------------
    # File details (core.metadata)
    # ------------------------------------------------------------------
    def meta_by_asset(self, asset_ids: Sequence[int]) -> dict[int, AssetMeta]:
        """Details of assets that were read since the file last changed."""
        result: dict[int, AssetMeta] = {}
        for chunk in _chunks(list(asset_ids)):
            for row in self._conn().execute(
                f"SELECT id, size, mtime, width, height, duration_ms, poly_count, alpha, meta_key FROM assets "
                f"WHERE meta_key IS NOT NULL AND id IN ({_placeholders(len(chunk))})",
                chunk,
            ):
                if row["meta_key"] == meta_key(row["size"], row["mtime"]):
                    alpha = None if row["alpha"] is None else bool(row["alpha"])
                    result[row["id"]] = AssetMeta(row["width"], row["height"], row["duration_ms"], row["poly_count"], alpha)
        return result

    def assets_needing_meta(self, asset_ids: Sequence[int]) -> list[tuple[int, str, str, str]]:
        """(id, path, extension, key) of assets whose details are missing or out of date;
        pass key back to store_meta() with the details."""
        rows = []
        for chunk in _chunks(list(asset_ids)):
            for row in self._conn().execute(
                f"SELECT id, path, extension, size, mtime, meta_key FROM assets WHERE id IN ({_placeholders(len(chunk))})",
                chunk,
            ):
                key = meta_key(row["size"], row["mtime"])
                if row["meta_key"] != key:
                    rows.append((row["id"], row["path"], row["extension"], key))
        return rows

    def store_meta(self, details: dict[int, tuple[AssetMeta, str]]) -> None:
        """Save details read for {asset_id: (details, key from assets_needing_meta)}."""
        with self._transaction() as conn:
            conn.executemany(
                "UPDATE assets SET width = ?, height = ?, duration_ms = ?, poly_count = ?, alpha = ?, meta_key = ? WHERE id = ?",
                [
                    (m.width, m.height, m.duration_ms, m.poly_count, None if m.alpha is None else int(m.alpha), key, asset_id)
                    for asset_id, (m, key) in details.items()
                ],
            )

    # ------------------------------------------------------------------
    # What pictures look like (core.picture_look)
    # ------------------------------------------------------------------
    def looks_by_asset(self, asset_ids: Sequence[int]) -> dict:
        """{id: core.picture_look.Look} of pictures looked at since the file last changed."""
        from .picture_look import Look

        result = {}
        for chunk in _chunks(list(asset_ids)):
            for row in self._conn().execute(
                f"SELECT id, size, mtime, look_colors, look_hash, look_grey, look_key FROM assets "
                f"WHERE look_key IS NOT NULL AND id IN ({_placeholders(len(chunk))})",
                chunk,
            ):
                if row["look_key"] == look_key(row["size"], row["mtime"]):
                    result[row["id"]] = Look.from_stored(row["look_colors"], row["look_hash"], row["look_grey"])
        return result

    def assets_needing_look(self, asset_ids: Sequence[int]) -> list[tuple[int, str, str]]:
        """(id, path, key) of pictures not looked at since they last changed; pass key back to store_looks()."""
        from .picture_look import needs_look

        rows = []
        for chunk in _chunks(list(asset_ids)):
            for row in self._conn().execute(
                f"SELECT id, path, extension, size, mtime, look_key FROM assets WHERE id IN ({_placeholders(len(chunk))})",
                chunk,
            ):
                key = look_key(row["size"], row["mtime"])
                if row["look_key"] != key and needs_look(row["extension"]):
                    rows.append((row["id"], row["path"], key))
        return rows

    def store_looks(self, looks: dict) -> None:
        """Save {asset_id: (Look, key from assets_needing_look)}."""
        with self._transaction() as conn:
            conn.executemany(
                "UPDATE assets SET look_colors = ?, look_hash = ?, look_grey = ?, look_key = ? WHERE id = ?",
                [(look.colors_text(), look.hash, look.grey, key, asset_id) for asset_id, (look, key) in looks.items()],
            )

    # ------------------------------------------------------------------
    # AI Search's vectors (core.ai_search)
    # ------------------------------------------------------------------
    def vectors_by_asset(self, asset_ids: Sequence[int]) -> dict:
        """{id: vector (float16)} of files read since they last changed."""
        from .ai_search import vector_from

        result = {}
        for chunk in _chunks(list(asset_ids)):
            for row in self._conn().execute(
                f"SELECT id, size, mtime, ai_vector, ai_key FROM assets WHERE ai_key IS NOT NULL AND id IN ({_placeholders(len(chunk))})",
                chunk,
            ):
                if row["ai_key"] == ai_key(row["size"], row["mtime"]) and (vector := vector_from(row["ai_vector"])) is not None:
                    result[row["id"]] = vector
        return result

    def assets_needing_vector(self, asset_ids: Sequence[int]) -> list[tuple[int, str, str, str]]:
        """(id, path, extension, key) of files without a vector since they last changed; pass key back to
        store_vectors(). (Which of them can be read is up to the caller: a model needs its thumbnail.)"""
        rows = []
        for chunk in _chunks(list(asset_ids)):
            for row in self._conn().execute(
                f"SELECT id, path, extension, size, mtime, ai_key FROM assets WHERE id IN ({_placeholders(len(chunk))})",
                chunk,
            ):
                key = ai_key(row["size"], row["mtime"])
                if row["ai_key"] != key:
                    rows.append((row["id"], row["path"], row["extension"], key))
        return rows

    def store_vectors(self, vectors: dict) -> None:
        """Save {asset_id: (vector, key from assets_needing_vector)} (vector None: the file couldn't be read)."""
        from .ai_search import vector_bytes

        with self._transaction() as conn:
            conn.executemany(
                "UPDATE assets SET ai_vector = ?, ai_key = ? WHERE id = ?",
                [(b"" if vector is None else vector_bytes(vector), key, asset_id) for asset_id, (vector, key) in vectors.items()],
            )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def set_web_page(self, path: Path | str, page: str) -> None:
        """The web page a picture came from (the browser extension saved it). Its row is made now if the library
        hasn't seen the file yet (the next scan fills in the rest, and folder rules still treat it as new)."""
        p = Path(path)
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO assets (path_key, path, name, name_fold, extension) VALUES (?, ?, ?, ?, ?)",
                (path_key(p), str(p), p.name, p.name.casefold(), p.suffix.lower()),
            )
            conn.execute("UPDATE assets SET web_page = ? WHERE path_key = ?", (page, path_key(p)))

    def web_page_of(self, path: Path | str) -> Optional[str]:
        """The page a picture was saved from by the browser extension, if it was."""
        row = self._conn().execute("SELECT web_page FROM assets WHERE path_key = ?", (path_key(path),)).fetchone()
        return row["web_page"] if row is not None and row["web_page"] else None

    def find_assets_by_tags(
        self, tag_ids: Sequence[int], match_all: bool = True, text: Optional[str] = None
    ) -> set[int]:
        """Ids of assets with all (or, with match_all=False, any) of tag_ids,
        optionally also requiring text in the file name (case-insensitive)."""
        tag_ids = list(dict.fromkeys(tag_ids))
        if match_all:
            return self.find_assets(all_of=tag_ids, text=text)
        return self.find_assets(any_of=[tag_ids] if tag_ids else [], text=text)

    def find_assets(
        self,
        all_of: Sequence[int] = (),
        any_of: Sequence[Sequence[int]] = (),
        none_of: Sequence[int] = (),
        text: Optional[str] = None,
    ) -> set[int]:
        """Ids of assets that have every tag in all_of, at least one tag from each group in
        any_of, none of none_of, and (if given) every word of text in the file name, in any
        order and ignoring case. With no conditions at all, returns an empty set."""
        words = list(dict.fromkeys((text or "").casefold().split()))
        any_of = [list(dict.fromkeys(group)) for group in any_of if group]
        if not (all_of or any_of or none_of or words):
            return set()
        has_tag = "EXISTS (SELECT 1 FROM asset_tags at WHERE at.asset_id = a.id AND at.tag_id {})"
        conditions: list[str] = []
        params: list = []
        for word in words:
            conditions.append("instr(a.name_fold, ?) > 0")
            params.append(word)
        for tag_id in dict.fromkeys(all_of):
            conditions.append(has_tag.format("= ?"))
            params.append(tag_id)
        for group in any_of:
            conditions.append(has_tag.format(f"IN ({_placeholders(len(group))})"))
            params += group
        if none_of:
            none_of = list(dict.fromkeys(none_of))
            conditions.append("NOT " + has_tag.format(f"IN ({_placeholders(len(none_of))})"))
            params += none_of
        sql = "SELECT a.id FROM assets a WHERE " + " AND ".join(conditions)
        return {row["id"] for row in self._conn().execute(sql, params)}

    # ------------------------------------------------------------------
    # Saved searches (sidebar collections)
    # ------------------------------------------------------------------
    def list_saved_searches(self) -> list[SavedSearch]:
        """Every collection, of every profile."""
        return [
            SavedSearch(row["id"], row["name"], row["query"], row["profile"])
            for row in self._conn().execute(
                "SELECT id, name, query, profile FROM saved_searches ORDER BY name COLLATE NOCASE"
            )
        ]

    def collections_for(self, profile: Optional[str]) -> list[SavedSearch]:
        """The collections a profile shows: its own, and those shown in every profile."""
        return [s for s in self.list_saved_searches() if s.profile in (profile, EVERY_PROFILE)]

    def _clean_search_name(self, name: str, profile: Optional[str] = EVERY_PROFILE, exclude_id: Optional[int] = None) -> str:
        name = " ".join((name or "").split())
        if not name:
            raise TagError("Collection names can't be empty.")
        if len(name) > MAX_TAG_NAME_LENGTH:
            raise TagError(f"Collection names can be at most {MAX_TAG_NAME_LENGTH} characters.")
        clash = _name_clash([s for s in self.list_saved_searches() if s.id != exclude_id], name, profile)
        if clash is not None:
            raise TagError(f'A collection named "{clash.name}" already exists.')
        return name

    def save_search(self, name: str, query: str, profile: Optional[str] = EVERY_PROFILE) -> SavedSearch:
        """Save a collection in a profile (EVERY_PROFILE: shown in all of them)."""
        name = self._clean_search_name(name, profile)
        if not query.strip():
            raise TagError("There's nothing to save: the search is empty.")
        with self._transaction() as conn:
            search_id = conn.execute(
                "INSERT INTO saved_searches (name, query, profile) VALUES (?, ?, ?)", (name, query.strip(), profile)
            ).lastrowid
        return SavedSearch(search_id, name, query.strip(), profile)

    def _saved_search(self, search_id: int) -> SavedSearch:
        saved = next((s for s in self.list_saved_searches() if s.id == search_id), None)
        if saved is None:
            raise TagError("That collection no longer exists.")
        return saved

    def rename_saved_search(self, search_id: int, name: str) -> None:
        name = self._clean_search_name(name, self._saved_search(search_id).profile, exclude_id=search_id)
        with self._transaction() as conn:
            conn.execute("UPDATE saved_searches SET name = ? WHERE id = ?", (name, search_id))

    def move_saved_search(self, search_id: int, profile: Optional[str]) -> None:
        """Give a collection to another profile, or (EVERY_PROFILE) show it in all of them."""
        saved = self._saved_search(search_id)
        self._clean_search_name(saved.name, profile, exclude_id=search_id)
        with self._transaction() as conn:
            conn.execute("UPDATE saved_searches SET profile = ? WHERE id = ?", (profile, search_id))

    def delete_profile_searches(self, profile: str) -> int:
        """Delete a deleted profile's own collections. Returns how many."""
        with self._transaction() as conn:
            return conn.execute("DELETE FROM saved_searches WHERE profile = ?", (profile,)).rowcount

    def delete_saved_search(self, search_id: int) -> None:
        with self._transaction() as conn:
            conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))


def _name_clash(saved: Sequence[SavedSearch], name: str, profile: Optional[str]) -> Optional[SavedSearch]:
    """A collection that would be shown beside one named name in profile, with the same name."""
    folded = name.casefold()
    for other in saved:
        together = profile is EVERY_PROFILE or other.profile in (profile, EVERY_PROFILE)
        if together and other.name.casefold() == folded:
            return other
    return None


_repository: Optional[TagRepository] = None


def get_repository() -> TagRepository:
    """The app-wide repository for the library database."""
    global _repository
    if _repository is None:
        _repository = TagRepository(DB_PATH)  # read at call time, so tests can point it elsewhere
    return _repository
