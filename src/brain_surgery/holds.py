"""The review queue: anything held for a human, prompts now and memories later.

Statuses follow one lifecycle for every kind of item:
``held`` -> ``released`` (run / restored) | ``excised`` (removed for good) | ``dismissed``.
The payload (e.g. the prompt text) is stored so a released item can be acted on; keep
the database on local disk with the same care as the content it holds.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATUSES = ("held", "released", "excised", "dismissed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS holds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    spec_id     TEXT,
    decision_id TEXT,
    outcome     TEXT,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'held',
    resolved_at TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS holds_status ON holds(status, kind);
"""


@dataclass
class Hold:
    id: int
    created_at: str
    kind: str
    reason: str
    spec_id: str | None
    decision_id: str | None
    outcome: str | None
    payload: dict[str, Any]
    status: str
    resolved_at: str | None
    note: str | None

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class HoldQueue:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)

    def add(self, kind: str, reason: str, payload: dict[str, Any], *, spec_id: str | None = None,
            decision_id: str | None = None, outcome: str | None = None) -> int:
        cur = self._db.execute(
            "INSERT INTO holds(created_at, kind, reason, spec_id, decision_id, outcome, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), kind, reason, spec_id, decision_id, outcome, json.dumps(payload, default=str)))
        self._db.commit()
        return int(cur.lastrowid)

    def get(self, hold_id: int) -> Hold | None:
        row = self._db.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        return _row(row) if row else None

    def list(self, status: str | None = "held", kind: str | None = None) -> list[Hold]:
        sql, args = "SELECT * FROM holds WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        return [_row(r) for r in self._db.execute(sql + " ORDER BY id", args).fetchall()]

    def resolve(self, hold_id: int, status: str, note: str | None = None) -> Hold:
        if status not in STATUSES or status == "held":
            raise ValueError(f"status must be one of {STATUSES[1:]}")
        h = self.get(hold_id)
        if h is None:
            raise KeyError(hold_id)
        if h.status != "held":
            raise ValueError(f"hold {hold_id} is already {h.status}")
        self._db.execute("UPDATE holds SET status = ?, resolved_at = ?, note = ? WHERE id = ?",
                         (status, _now(), note, hold_id))
        self._db.commit()
        return self.get(hold_id)  # type: ignore[return-value]

    def close(self) -> None:
        self._db.close()


def _row(r: sqlite3.Row) -> Hold:
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    return Hold(**d)
