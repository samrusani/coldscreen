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

    def close(self) -> None:
        with contextlib.suppress(sqlite3.DatabaseError):
            self._conn.close()
