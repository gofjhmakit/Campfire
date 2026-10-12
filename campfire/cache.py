"""SQLite cache of library metadata (never audio, never credentials or stream URLs).

The cache is only a snapshot to make startup fast; Bandcamp stays the source of
truth. A corrupt or incompatible cache file is discarded and rebuilt.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .library import Library
from .models import Album, Track

SCHEMA_VERSION = 2  # 2: albums carry cover_art

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS albums (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tracks (
    album_id TEXT NOT NULL, position INTEGER NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (album_id, position));
"""


class Cache:
    def __init__(self, path: Path, account: str, server: str):
        self.path = path
        # The cache belongs to one account on one server; anything else is ignored.
        self.owner = f"{account}@{server}"

    def _open(self) -> sqlite3.Connection:
        # The cache describes the user's collection: keep it private.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        return sqlite3.connect(self.path, timeout=5)

    def _connect(self) -> sqlite3.Connection:
        conn = self._open()
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                conn.close()
                self._discard()
                conn = self._open()
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def _discard(self) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    def load(self) -> Library | None:
        if not self.path.exists():
            return None
        try:
            conn = self._connect()
            try:
                meta = dict(conn.execute("SELECT key, value FROM meta"))
                if meta.get("owner") != self.owner:
                    return None
                albums = [Album.from_dict(json.loads(d)) for (d,) in conn.execute("SELECT data FROM albums")]
                tracks: dict[str, list[Track]] = {}
                for album_id, data in conn.execute(
                        "SELECT album_id, data FROM tracks ORDER BY album_id, position"):
                    tracks.setdefault(album_id, []).append(Track.from_dict(json.loads(data)))
                synced_at = float(meta["synced_at"]) if "synced_at" in meta else None
            finally:
                conn.close()
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            self._discard()
            return None
        return Library(albums, tracks, synced_at)

    def save(self, library: Library, complete: bool = True) -> None:
        """Replace the snapshot. `complete=False` keeps synced_at unset so a
        partial sync (e.g. interrupted) is refreshed again next time."""
        try:
            conn = self._connect()
        except sqlite3.Error:
            self._discard()
            conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM meta")
                conn.execute("DELETE FROM albums")
                conn.execute("DELETE FROM tracks")
                conn.executemany("INSERT INTO albums VALUES (?, ?)",
                                 [(a.id, json.dumps(a.to_dict())) for a in library.albums()])
                rows = []
                for album in library.albums():
                    for pos, track in enumerate(library.tracks_of(album.id) or []):
                        rows.append((album.id, pos, json.dumps(track.to_dict())))
                conn.executemany("INSERT INTO tracks VALUES (?, ?, ?)", rows)
                meta = [("owner", self.owner)]
                if complete:
                    meta.append(("synced_at", str(library.synced_at or time.time())))
                conn.executemany("INSERT INTO meta VALUES (?, ?)", meta)
        finally:
            conn.close()

    def clear(self) -> None:
        self._discard()
