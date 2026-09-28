"""Small SQLite cache for paid API responses.

Every PDL page and Hunter lookup is stored under a hash of its request, so
re-running after a crash, or re-running to rebuild the sheet, replays saved
responses instead of spending credits again.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class Cache:
    def __init__(self, path: str | Path, ttl_seconds: float, enabled: bool = True, refresh: bool = False):
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds
        self.refresh = refresh  # ignore stored entries but still save new ones
        self.hits = 0
        self._conn: sqlite3.Connection | None = None
        if enabled:
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path))
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cache ("
                " namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,"
                " created REAL NOT NULL, PRIMARY KEY (namespace, key))"
            )
            self._conn.commit()

    @classmethod
    def disabled(cls) -> "Cache":
        return cls(":memory:", 0, enabled=False)

    @staticmethod
    def _key(key: Any) -> str:
        return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()

    def get(self, namespace: str, key: Any) -> Any | None:
        if not self._conn or self.refresh:
            return None
        row = self._conn.execute(
            "SELECT value, created FROM cache WHERE namespace = ? AND key = ?",
            (namespace, self._key(key)),
        ).fetchone()
        if row is None or time.time() - row[1] > self.ttl_seconds:
            return None
        self.hits += 1
        return json.loads(row[0])

    def set(self, namespace: str, key: Any, value: Any) -> None:
        if not self._conn:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (namespace, key, value, created) VALUES (?, ?, ?, ?)",
            (namespace, self._key(key), json.dumps(value), time.time()),
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
