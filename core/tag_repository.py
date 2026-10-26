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
- Rows for files that disappear are kept (a library drive may just be offline),
  so the database can accumulate stale assets until a cleanup feature exists.
- Tag hierarchies (a tag inside a tag) aren't supported; searches combine tags with
  AND / OR / NOT (find_assets).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from .fingerprint import quick_hash
from .scanner import AssetItem, determine_category_name

DB_PATH: Path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AssetManager" / "library.db"

MAX_TAG_NAME_LENGTH = 40
MAX_BACKUPS = 10
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


def meta_key(size: int, mtime: float) -> str:
    return f"{size}:{mtime!r}"


@dataclass(frozen=True)
class SavedSearch:
    id: int
    name: str
    query: str


def default_tag_name(extension: str) -> str:
    """Display name of the default tag for a file extension (".jpeg" -> "JPEG")."""
    return determine_category_name(extension)


def path_key(path: Path | str) -> str:
    """Stable lookup key for a file path (absolute, Windows case-insensitive)."""
    return os.path.normcase(os.path.abspath(str(path)))


def _is_missing(path: str) -> bool:
    """The file is gone, and not just on a drive or network share that's offline."""
    return not os.path.exists(path) and os.path.exists(Path(path).anchor or ".")


def _chunks(items: Sequence, size: int = _CHUNK) -> Iterator[Sequence]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _placeholders(count: int) -> str:
    return ",".join("?" * count)


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
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            self._migrate(conn)
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
        dest = sqlite3.connect(target)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        backups = sorted(folder.glob("library_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[MAX_BACKUPS:]:
            try:
                old.unlink()
            except OSError:
                pass
        return target

    def list_backups(self) -> list[Path]:
        """Backups, newest first."""
        if not self.backup_dir().is_dir():
            return []
        return sorted(self.backup_dir().glob("library_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)

    def restore_database(self, backup: Path | str) -> None:
        """Replace the library's contents with a backup (a backup of the current state is made first)."""
        source = sqlite3.connect(f"file:{Path(backup).as_posix()}?mode=ro", uri=True)
        try:
            version = source.execute("PRAGMA user_version").fetchone()[0]
            if not 1 <= version <= len(MIGRATIONS):
                raise TagError("That file isn't an Asset Manager library backup, or it's from a newer version.")
            self.backup_database("before-restore")
            source.backup(self._conn())
        except sqlite3.DatabaseError as exc:
            raise TagError(f"The backup couldn't be read: {exc}") from exc
        finally:
            source.close()
        self._migrate(self._conn())  # an older backup is upgraded to the current schema

    # ------------------------------------------------------------------
    # Export / import (JSON)
    # ------------------------------------------------------------------
    def export_tags(self, target: Path | str) -> int:
        """Write custom tags and the files they're on to a JSON file. Returns the number of files."""
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
            "searches": [{"name": s.name, "query": s.query} for s in self.list_saved_searches()],
        }
        Path(target).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return len(files)

    def import_tags(self, source: Path | str) -> dict[str, int]:
        """Merge an export into the library: missing tags are created and added to the listed files,
        and saved searches with new names are added (nothing is removed).
        Returns counts: tags_created, files, skipped_tags, searches."""
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

            saved_names = {s.name.casefold() for s in self.list_saved_searches()}
            searches = 0
            for entry in data.get("searches", []):
                name = " ".join(str(entry.get("name", "")).split())[:MAX_TAG_NAME_LENGTH]
                query = str(entry.get("query", "")).strip()
                if name and query and name.casefold() not in saved_names:
                    conn.execute("INSERT INTO saved_searches (name, query) VALUES (?, ?)", (name, query))
                    saved_names.add(name.casefold())
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
    def sync_assets(self, assets: Iterable[AssetItem]) -> dict[str, int]:
        """Record scanned assets and give each its default file-type tag.

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
                "size = excluded.size, mtime = excluded.mtime",
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
        return ids

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
        return len(moves)

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
        self._require_custom(tag_id)
        new_name = self.validate_tag_name(new_name, exclude_tag_id=tag_id)
        with self._transaction() as conn:
            conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, tag_id))
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
                f"SELECT id, size, mtime, width, height, duration_ms, poly_count, meta_key FROM assets "
                f"WHERE meta_key IS NOT NULL AND id IN ({_placeholders(len(chunk))})",
                chunk,
            ):
                if row["meta_key"] == meta_key(row["size"], row["mtime"]):
                    result[row["id"]] = AssetMeta(row["width"], row["height"], row["duration_ms"], row["poly_count"])
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
                "UPDATE assets SET width = ?, height = ?, duration_ms = ?, poly_count = ?, meta_key = ? WHERE id = ?",
                [
                    (m.width, m.height, m.duration_ms, m.poly_count, key, asset_id)
                    for asset_id, (m, key) in details.items()
                ],
            )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
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
        any_of, none of none_of, and (if given) text in the file name, ignoring case.
        With no conditions at all, returns an empty set."""
        folded = (text or "").strip().casefold()
        any_of = [list(dict.fromkeys(group)) for group in any_of if group]
        if not (all_of or any_of or none_of or folded):
            return set()
        has_tag = "EXISTS (SELECT 1 FROM asset_tags at WHERE at.asset_id = a.id AND at.tag_id {})"
        conditions: list[str] = []
        params: list = []
        if folded:
            conditions.append("instr(a.name_fold, ?) > 0")
            params.append(folded)
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
        return [
            SavedSearch(row["id"], row["name"], row["query"])
            for row in self._conn().execute("SELECT id, name, query FROM saved_searches ORDER BY name COLLATE NOCASE")
        ]

    def _clean_search_name(self, name: str, exclude_id: Optional[int] = None) -> str:
        name = " ".join((name or "").split())
        if not name:
            raise TagError("Collection names can't be empty.")
        if len(name) > MAX_TAG_NAME_LENGTH:
            raise TagError(f"Collection names can be at most {MAX_TAG_NAME_LENGTH} characters.")
        for saved in self.list_saved_searches():
            if saved.id != exclude_id and saved.name.casefold() == name.casefold():
                raise TagError(f'A collection named "{saved.name}" already exists.')
        return name

    def save_search(self, name: str, query: str) -> SavedSearch:
        name = self._clean_search_name(name)
        if not query.strip():
            raise TagError("There's nothing to save: the search is empty.")
        with self._transaction() as conn:
            search_id = conn.execute("INSERT INTO saved_searches (name, query) VALUES (?, ?)", (name, query.strip())).lastrowid
        return SavedSearch(search_id, name, query.strip())

    def rename_saved_search(self, search_id: int, name: str) -> None:
        name = self._clean_search_name(name, exclude_id=search_id)
        with self._transaction() as conn:
            conn.execute("UPDATE saved_searches SET name = ? WHERE id = ?", (name, search_id))

    def delete_saved_search(self, search_id: int) -> None:
        with self._transaction() as conn:
            conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))


_repository: Optional[TagRepository] = None


def get_repository() -> TagRepository:
    """The app-wide repository for the library database."""
    global _repository
    if _repository is None:
        _repository = TagRepository(DB_PATH)  # read at call time, so tests can point it elsewhere
    return _repository
