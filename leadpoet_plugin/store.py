"""Local desk state. Contact details are cleared as soon as a reply is closed."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  task_id TEXT PRIMARY KEY,
  enrollment_id TEXT NOT NULL,
  channel TEXT,
  sendable INTEGER NOT NULL DEFAULT 0,
  name TEXT,
  company TEXT,
  classification TEXT,
  urgent INTEGER NOT NULL DEFAULT 0,
  received_at TEXT,
  inbound_id TEXT,
  inbound_text TEXT,
  draft TEXT,
  state TEXT NOT NULL DEFAULT 'new',
  alerted INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

# A send that may have gone out stays open so it can only be retried safely.
OPEN_STATES = ("new", "unknown")


class Store:
    def __init__(self, path: Path | str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def put(self, key: str, value: str | None) -> None:
        with self._lock, self._db:
            if value is None:
                self._db.execute("DELETE FROM kv WHERE key=?", (key,))
            else:
                self._db.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def try_lock(self, name: str, now: float, ttl: float = 300) -> str:
        """Take a lock shared with the scheduled job and return its token. A stale one, from a crash, expires."""
        token = f"{now + ttl}:{uuid.uuid4().hex}"
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                "WHERE CAST(kv.value AS REAL) < ?", (f"lock:{name}", token, now))
            return token if cursor.rowcount == 1 else ""

    def unlock(self, name: str, token: str) -> None:
        """Release only your own lock, so a slow holder can't free the one that replaced it."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM kv WHERE key=? AND value=?", (f"lock:{name}", token))

    def item(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM items WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def upsert(self, item: dict[str, Any]) -> None:
        """Insert a reply or refresh its server fields. Desk state is left alone."""
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO items(task_id,enrollment_id,channel,sendable,name,company,classification,
                                     urgent,received_at,inbound_id,inbound_text)
                   VALUES(:task_id,:enrollment_id,:channel,:sendable,:name,:company,:classification,
                          :urgent,:received_at,:inbound_id,:inbound_text)
                   ON CONFLICT(task_id) DO UPDATE SET
                     channel=excluded.channel, sendable=excluded.sendable, name=excluded.name,
                     company=excluded.company, classification=excluded.classification,
                     urgent=excluded.urgent, received_at=excluded.received_at,
                     inbound_id=excluded.inbound_id, inbound_text=excluded.inbound_text
                   WHERE items.state IN ('new','unknown')""", item)

    def update(self, task_id: str, **fields: Any) -> None:
        columns = ",".join(f"{name}=?" for name in fields)
        with self._lock, self._db:
            self._db.execute(f"UPDATE items SET {columns} WHERE task_id=?", (*fields.values(), task_id))

    def close_item(self, task_id: str, state: str) -> None:
        """Finish a reply and clear everything that identifies the contact."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE items SET state=?, name=NULL, company=NULL, inbound_text=NULL, draft=NULL "
                "WHERE task_id=?", (state, task_id))

    def open_items(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM items WHERE state IN ('new','unknown') "
                "ORDER BY skipped ASC, urgent DESC, received_at ASC").fetchall()
        return [dict(row) for row in rows]

    def reset(self) -> None:
        """Empty this file's replies. Only the demo uses this, on its own fictional data."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM items")
            self._db.execute("DELETE FROM kv")

    def all_task_ids(self) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT task_id FROM items WHERE state != 'closed'").fetchall()
        return [row["task_id"] for row in rows]

    def unalerted(self) -> list[dict]:
        return [item for item in self.open_items() if not item["alerted"]]

    def mark_alerted(self, task_ids: list[str]) -> None:
        with self._lock, self._db:
            self._db.executemany("UPDATE items SET alerted=1 WHERE task_id=?", [(i,) for i in task_ids])
