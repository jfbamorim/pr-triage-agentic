import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

VALIDITY_DAYS = 90
ALLOWED_KEYS = {"cost_center": re.compile(r"^CC-[A-Z]{2,4}-\d{3}$")}

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requester TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    origin_run_id TEXT,
    origin_pr_id INTEGER,
    created_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    status_reason TEXT,
    superseded_by INTEGER
);
CREATE INDEX IF NOT EXISTS memories_by_requester ON memories (requester, key, status);
"""


class InvalidMemory(ValueError):
    pass


class MemoryEntry(BaseModel):
    id: int
    requester: str
    key: str
    value: str
    source: str
    origin_run_id: str | None
    origin_pr_id: int | None
    created_at: datetime
    valid_until: datetime
    status: str
    status_reason: str | None
    superseded_by: int | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _entry(row: sqlite3.Row) -> MemoryEntry:
    return MemoryEntry(
        **{**dict(row), "created_at": datetime.fromisoformat(row["created_at"]),
           "valid_until": datetime.fromisoformat(row["valid_until"])}
    )


class MemoryStore:
    def __init__(self, path: str | Path, clock: Callable[[], datetime] = _utc_now):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def remember(
        self,
        requester: str,
        key: str,
        value: str,
        source: str,
        origin_run_id: str | None = None,
        origin_pr_id: int | None = None,
    ) -> MemoryEntry:
        pattern = ALLOWED_KEYS.get(key)
        if pattern is None:
            raise InvalidMemory(f"Memory key {key!r} is not allowed.")
        if not requester or not pattern.fullmatch(value):
            raise InvalidMemory(f"Value {value!r} is not valid for {key}.")
        now = self.clock()
        with closing(self.connect()) as conn:
            cursor = conn.execute(
                "INSERT INTO memories (requester, key, value, source, origin_run_id, origin_pr_id, "
                "created_at, valid_until) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (requester, key, value, source, origin_run_id, origin_pr_id, now.isoformat(),
                 (now + timedelta(days=VALIDITY_DAYS)).isoformat()),
            )
            new_id = cursor.lastrowid
            conn.execute(
                "UPDATE memories SET status = 'superseded', status_reason = 'replaced by a newer entry', "
                "superseded_by = ? WHERE requester = ? AND key = ? AND status = 'active' AND id != ?",
                (new_id, requester, key, new_id),
            )
            conn.commit()
            return _entry(conn.execute("SELECT * FROM memories WHERE id = ?", (new_id,)).fetchone())

    def recall(self, requester: str, key: str) -> MemoryEntry | None:
        now = self.clock()
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE requester = ? AND key = ? AND status = 'active' "
                "ORDER BY id DESC LIMIT 1",
                (requester, key),
            ).fetchone()
            if row is None:
                return None
            entry = _entry(row)
            if entry.valid_until <= now:
                conn.execute(
                    "UPDATE memories SET status = 'expired', status_reason = 'validity period ended' WHERE id = ?",
                    (entry.id,),
                )
                conn.commit()
                return None
            return entry

    def invalidate(self, requester: str, entry_id: int, reason: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE memories SET status = 'invalidated', status_reason = ? "
                "WHERE id = ? AND requester = ? AND status = 'active'",
                (reason, entry_id, requester),
            )
            conn.commit()

    def forget(self, requester: str, key: str) -> int:
        with closing(self.connect()) as conn:
            cursor = conn.execute(
                "UPDATE memories SET status = 'forgotten', status_reason = 'forgotten on request' "
                "WHERE requester = ? AND key = ? AND status = 'active'",
                (requester, key),
            )
            conn.commit()
            return cursor.rowcount

    def history(self, requester: str) -> list[MemoryEntry]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE requester = ? ORDER BY id", (requester,)
            ).fetchall()
        return [_entry(r) for r in rows]


class MemoryView:
    def __init__(self, store: MemoryStore, requester: str):
        self._store = store
        self.requester = requester

    def recall(self, key: str) -> MemoryEntry | None:
        return self._store.recall(self.requester, key)

    def invalidate(self, entry: MemoryEntry, reason: str) -> None:
        self._store.invalidate(self.requester, entry.id, reason)
