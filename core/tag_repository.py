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

Known limitations:
- Assets are tracked by file path, so moving or renaming a file outside the app
  orphans its tags (the old row stays; the new path starts untagged). There is
  no relink mechanism yet; that's a follow-up.
- Rows for files that disappear are kept (a library drive may just be offline),
  so the database can accumulate stale assets until a cleanup feature exists.
- Only AND search across tags; OR search and tag hierarchies are out of scope.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from .scanner import AssetItem, determine_category_name

DB_PATH: Path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AssetManager" / "library.db"

MAX_TAG_NAME_LENGTH = 40
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


def default_tag_name(extension: str) -> str:
    """Display name of the default tag for a file extension (".jpeg" -> "JPEG")."""
    return determine_category_name(extension)


def path_key(path: Path | str) -> str:
    """Stable lookup key for a file path (absolute, Windows case-insensitive)."""
    return os.path.normcase(os.path.abspath(str(path)))


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

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for target in range(version, len(MIGRATIONS)):
            # One atomic script per step; user_version can't be a bound parameter
            conn.executescript(f"BEGIN; {MIGRATIONS[target]} PRAGMA user_version = {target + 1}; COMMIT;")

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
        rows = [(path_key(a.path), str(a.path), a.name, a.name.casefold(), a.extension.lower()) for a in assets]
        default_names = {ext: default_tag_name(ext) for ext in {row[4] for row in rows}}
        with self._transaction() as conn:
            conn.executemany(
                "INSERT INTO assets (path_key, path, name, name_fold, extension) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(path_key) DO UPDATE SET path = excluded.path, name = excluded.name, "
                "name_fold = excluded.name_fold, extension = excluded.extension",
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
    ) -> None:
        """Add and remove custom tags on many files in a single transaction."""
        asset_ids, add_tag_ids, remove_tag_ids = list(asset_ids), list(add_tag_ids), list(remove_tag_ids)
        with self._transaction() as conn:
            self._require_custom_ids(conn, add_tag_ids + remove_tag_ids)
            conn.executemany(
                "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) VALUES (?, ?)",
                [(a, t) for t in add_tag_ids for a in asset_ids],
            )
            conn.executemany(
                "DELETE FROM asset_tags WHERE asset_id = ? AND tag_id = ?",
                [(a, t) for t in remove_tag_ids for a in asset_ids],
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
    # Search
    # ------------------------------------------------------------------
    def find_assets_by_tags(
        self, tag_ids: Sequence[int], match_all: bool = True, text: Optional[str] = None
    ) -> set[int]:
        """Ids of assets with all (or, with match_all=False, any) of tag_ids,
        optionally also requiring text in the file name (case-insensitive)."""
        tag_ids = list(dict.fromkeys(tag_ids))
        folded = (text or "").strip().casefold()
        params: list = []
        if tag_ids:
            sql = (
                "SELECT at.asset_id FROM asset_tags at JOIN assets a ON a.id = at.asset_id "
                f"WHERE at.tag_id IN ({_placeholders(len(tag_ids))})"
            )
            params += tag_ids
            if folded:
                sql += " AND instr(a.name_fold, ?) > 0"
                params.append(folded)
            sql += " GROUP BY at.asset_id"
            if match_all:
                sql += " HAVING COUNT(DISTINCT at.tag_id) = ?"
                params.append(len(tag_ids))
        elif folded:
            sql = "SELECT id AS asset_id FROM assets WHERE instr(name_fold, ?) > 0"
            params.append(folded)
        else:
            return set()
        return {row["asset_id"] for row in self._conn().execute(sql, params)}


_repository: Optional[TagRepository] = None


def get_repository() -> TagRepository:
    """The app-wide repository for the library database."""
    global _repository
    if _repository is None:
        _repository = TagRepository()
    return _repository
