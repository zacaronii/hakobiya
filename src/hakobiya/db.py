"""SQLite cache: the Minerva index, each opened set's file list, and small key/value state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = [
    """
    CREATE TABLE sets (
        name TEXT PRIMARY KEY, href TEXT NOT NULL, source TEXT NOT NULL, platform TEXT NOT NULL,
        modified TEXT, size_text TEXT, torrent_path TEXT, infohash TEXT, file_count INTEGER, total_size INTEGER,
        opened_at TEXT);
    CREATE TABLE files (
        set_name TEXT NOT NULL, idx INTEGER NOT NULL, path TEXT NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL,
        title TEXT NOT NULL, game TEXT NOT NULL, regions TEXT NOT NULL, languages TEXT NOT NULL, tags TEXT NOT NULL,
        disc INTEGER, rev TEXT, PRIMARY KEY (set_name, idx));
    CREATE INDEX files_title ON files(set_name, title);
    CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);
    """,
    # v2: the basket (files ticked per set, kept across pages and filters) and files sent to qBittorrent
    """
    CREATE TABLE picks (set_name TEXT NOT NULL, idx INTEGER NOT NULL, PRIMARY KEY (set_name, idx));
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY, set_name TEXT NOT NULL, idx INTEGER NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
        infohash TEXT NOT NULL, state TEXT NOT NULL, note TEXT, sent_by TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE (set_name, idx));
    """,
    # v3: where a finished file went in the library
    """
    ALTER TABLE jobs ADD COLUMN target TEXT;
    """,
    # v4: art: libretro lookups that found nothing, cover shapes, and covers replaced (with a backup for undo)
    """
    CREATE TABLE art_misses (slug TEXT NOT NULL, name TEXT NOT NULL, fetched_at REAL NOT NULL, PRIMARY KEY (slug, name));
    CREATE TABLE art_sizes (path TEXT PRIMARY KEY, width INTEGER, height INTEGER);
    CREATE TABLE art_changes (
        id INTEGER PRIMARY KEY, rom_id INTEGER NOT NULL, platform_id INTEGER NOT NULL, name TEXT NOT NULL,
        source TEXT NOT NULL, backup_path TEXT, applied_at TEXT NOT NULL, applied_by TEXT, undone_at TEXT);
    """,
    # v5: generated disc/cart art saved into RomM (old file and old ss_metadata kept for undo)
    """
    CREATE TABLE make_changes (
        id INTEGER PRIMARY KEY, rom_id INTEGER NOT NULL, platform_id INTEGER NOT NULL, name TEXT NOT NULL,
        settings TEXT NOT NULL, physical_path TEXT NOT NULL, backup_path TEXT, old_ss_metadata TEXT NOT NULL,
        applied_at TEXT NOT NULL, applied_by TEXT, undone_at TEXT);
    """,
    # v6: Hakobiya manages generated art after saving it: its own copy (to restore after a RomM refresh), the
    # weekly check for a genuine Japanese scan, and when one replaced the generated image
    """
    ALTER TABLE make_changes ADD COLUMN generated_path TEXT;
    ALTER TABLE make_changes ADD COLUMN checked_at TEXT;
    ALTER TABLE make_changes ADD COLUMN superseded_at TEXT;
    ALTER TABLE make_changes ADD COLUMN superseded_by TEXT;
    """,
    # v7: images from other regions taken out of RomM (file kept for undo; removed_by NULL = the automatic check)
    """
    CREATE TABLE media_changes (
        id INTEGER PRIMARY KEY, rom_id INTEGER NOT NULL, platform_id INTEGER NOT NULL, name TEXT NOT NULL,
        kind TEXT NOT NULL, region TEXT NOT NULL, path TEXT NOT NULL, backup_path TEXT, removed_at TEXT NOT NULL,
        removed_by TEXT, undone_at TEXT);
    """,
    # v8: manuals: the Internet Archive's answers (searches, items' file lists) and manuals put into RomM (the old
    # file kept for undo; sha256 and size are the uploaded file's, to tell whether it is still the one in RomM)
    """
    CREATE TABLE manual_cache (key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body TEXT NOT NULL);
    CREATE TABLE manual_changes (
        id INTEGER PRIMARY KEY, rom_id INTEGER NOT NULL, platform_id INTEGER NOT NULL, name TEXT NOT NULL,
        identifier TEXT NOT NULL, file TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
        old_state TEXT NOT NULL, backup_path TEXT, applied_at TEXT NOT NULL, applied_by TEXT, undone_at TEXT);
    """,
]


class DB:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(SCHEMA[version:], start=version + 1):
            self.conn.execute("BEGIN")
            for stmt in script.split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
            self.conn.execute(f"PRAGMA user_version={i}")
            self.conn.execute("COMMIT")

    def execute(self, sql: str, params=()):
        return self.conn.execute(sql, tuple(params))

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def get(self, key: str) -> str | None:
        row = self.one("SELECT value FROM state WHERE key=?", (key,))
        return row["value"] if row else None

    def put(self, key: str, value: str | None) -> None:
        self.execute("INSERT INTO state(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))
