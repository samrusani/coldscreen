"""SQLite HTTP cache keyed by URL plus sorted query params, with a TTL.

Only successful (HTTP 200) JSON responses are cached. The cache stores the
URL, the auth-free params, the response body, and the original retrieval
time, so cache hits keep their true retrieval timestamp for the audit trail.
Credentials travel in request headers and are never part of the cache key,
the stored params, or the stored body.

A corrupt cache file never breaks a run: on sqlite errors the cache warns,
deletes the file, reinitializes it, and continues.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_TTL_SECONDS = 7 * 86400.0

Params = Mapping[str, str | int]


def cache_key(url: str, params: Params | None) -> str:
    """Deterministic key: URL plus params sorted by name."""
    canonical = json.dumps(
        {"url": url, "params": sorted((k, str(v)) for k, v in (params or {}).items())},
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachedResponse:
    url: str
    params: dict[str, str]
    status: int
    body: str
    retrieved_at: datetime


class CacheClearRefused(Exception):
    """Clear will not proceed because the cache path is unsafe to wipe."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        super().__init__(reason)


@dataclass(frozen=True)
class CacheClearResult:
    """Outcome of clearing the HTTP cache file at a configured path."""

    path: Path
    entries_removed: int
    missing: bool
    unreadable_removed: bool = False


@dataclass(frozen=True)
class CacheStats:
    """Operator-facing cache facts. Never includes URLs, params, or bodies."""

    path: Path
    exists: bool
    entry_count: int | None
    size_bytes: int
    ttl_days: float
    unreadable: bool = False


def _http_cache_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'http_cache'"
    ).fetchone()
    return row is not None


def _entry_count(conn: sqlite3.Connection) -> int:
    if not _http_cache_table_exists(conn):
        return 0
    row = conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()
    return int(row[0]) if row is not None else 0


def _unlink_regular_file(path: Path) -> None:
    """Unlink a regular file. Never follows a symlink to remove its target."""
    if path.is_symlink() or not path.is_file():
        return
    path.unlink()


def clear_http_cache(path: Path) -> CacheClearResult:
    """Clear the configured cache file. Missing file is success.

    Refuses if `path` itself is a symbolic link, so a link at the sqlite
    name cannot be followed to wipe another file. Does not take a
    caller-supplied path at the CLI; this function is the library half of
    that rule. Does not delete the parent directory.
    """
    if path.is_symlink():
        raise CacheClearRefused(
            path,
            f"{path} is a symbolic link. coldscreen will not follow a link at"
            " the cache file to wipe another file. Remove the link, or set"
            " COLDSCREEN_CACHE_DIR to a directory you control.",
        )
    if not path.exists():
        return CacheClearResult(path=path, entries_removed=0, missing=True)
    if not path.is_file():
        raise CacheClearRefused(
            path,
            f"{path} is not a cache file. coldscreen will not delete it.",
        )
    try:
        conn = sqlite3.connect(str(path))
        try:
            removed = _entry_count(conn)
            if _http_cache_table_exists(conn):
                conn.execute("DELETE FROM http_cache")
                conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        _unlink_regular_file(path)
        _unlink_regular_file(Path(str(path) + "-wal"))
        _unlink_regular_file(Path(str(path) + "-shm"))
        return CacheClearResult(
            path=path, entries_removed=0, missing=False, unreadable_removed=True
        )
    return CacheClearResult(path=path, entries_removed=removed, missing=False)


def http_cache_stats(path: Path, ttl_days: float) -> CacheStats:
    """Read path, existence, entry count, size, and TTL. Never dumps payloads.

    A corrupt file is reported as unreadable rather than raised, and is not
    deleted or recreated: that would be a surprise from a stats command.
    """
    if not path.exists():
        return CacheStats(
            path=path,
            exists=False,
            entry_count=0,
            size_bytes=0,
            ttl_days=ttl_days,
        )
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return CacheStats(
            path=path,
            exists=True,
            entry_count=None,
            size_bytes=0,
            ttl_days=ttl_days,
            unreadable=True,
        )
    if not path.is_file() and not path.is_symlink():
        return CacheStats(
            path=path,
            exists=True,
            entry_count=None,
            size_bytes=size_bytes,
            ttl_days=ttl_days,
            unreadable=True,
        )
    try:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("SELECT 1").fetchone()
            count = _entry_count(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return CacheStats(
            path=path,
            exists=True,
            entry_count=None,
            size_bytes=size_bytes,
            ttl_days=ttl_days,
            unreadable=True,
        )
    return CacheStats(
        path=path,
        exists=True,
        entry_count=count,
        size_bytes=size_bytes,
        ttl_days=ttl_days,
    )


class HttpCache:
    """A small persistent cache for GET responses."""

    def __init__(
        self,
        path: Path,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = self._connect()
        except sqlite3.DatabaseError:
            self._conn = self._recreate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS http_cache (
                    key TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    params TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    stored_at REAL NOT NULL
                )
                """
            )
            conn.commit()
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return conn

    def _recreate(self) -> sqlite3.Connection:
        """Drop the unreadable cache file and start fresh. Cache loss is fine."""
        print(
            f"warning: HTTP cache at {self.path} is unreadable; recreating it",
            file=sys.stderr,
        )
        with contextlib.suppress(Exception):
            self._conn.close()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        return self._connect()

    def get(self, url: str, params: Params | None = None) -> CachedResponse | None:
        """Return the cached response, or None on miss, expiry, or db error."""
        key = cache_key(url, params)
        try:
            row = self._conn.execute(
                "SELECT url, params, status, body, retrieved_at, stored_at"
                " FROM http_cache WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            stored_at = float(row[5])
            if self._clock() - stored_at > self.ttl_seconds:
                self._conn.execute("DELETE FROM http_cache WHERE key = ?", (key,))
                self._conn.commit()
                return None
        except sqlite3.DatabaseError:
            self._conn = self._recreate()
            return None
        return CachedResponse(
            url=row[0],
            params=dict(json.loads(row[1])),
            status=int(row[2]),
            body=row[3],
            retrieved_at=datetime.fromisoformat(row[4]),
        )

    def put(
        self,
        url: str,
        params: Params | None,
        status: int,
        body: str,
        retrieved_at: datetime,
    ) -> None:
        """Store a response. Only HTTP 200 responses are kept."""
        if status != 200:
            return
        key = cache_key(url, params)
        row = (
            key,
            url,
            json.dumps({k: str(v) for k, v in (params or {}).items()}, sort_keys=True),
            status,
            body,
            retrieved_at.isoformat(),
            self._clock(),
        )
        statement = (
            "INSERT OR REPLACE INTO http_cache"
            " (key, url, params, status, body, retrieved_at, stored_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        try:
            self._conn.execute(statement, row)
            self._conn.commit()
        except sqlite3.DatabaseError:
            self._conn = self._recreate()
            with contextlib.suppress(sqlite3.DatabaseError):
                self._conn.execute(statement, row)
                self._conn.commit()

    def clear(self) -> int:
        """Drop every cached entry. Returns how many rows were removed."""
        try:
            removed = _entry_count(self._conn)
            self._conn.execute("DELETE FROM http_cache")
            self._conn.commit()
            return removed
        except sqlite3.DatabaseError:
            self._conn = self._recreate()
            return 0

    def close(self) -> None:
        with contextlib.suppress(sqlite3.DatabaseError):
            self._conn.close()
