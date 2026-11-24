import sqlite3
from contextlib import closing
from pathlib import Path

from pr_triage.supervisor.state import RunState, RunStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    updated_at TEXT NOT NULL, state_json TEXT NOT NULL
);
"""


class RunStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def save(self, state: RunState) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO runs (run_id, status, updated_at, state_json) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(run_id) DO UPDATE SET status = excluded.status,"
                " updated_at = excluded.updated_at, state_json = excluded.state_json",
                (state.run_id, state.status.value, state.updated_at.isoformat(), state.model_dump_json()),
            )

    def load(self, run_id: str) -> RunState | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT state_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return RunState.model_validate_json(row[0]) if row else None

    def list_by_status(self, status: RunStatus) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT run_id FROM runs WHERE status = ? ORDER BY updated_at", (status.value,)
            ).fetchall()
        return [row[0] for row in rows]

    def list_recent(self, limit: int = 10) -> list[RunState]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT state_json FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [RunState.model_validate_json(row[0]) for row in rows]
