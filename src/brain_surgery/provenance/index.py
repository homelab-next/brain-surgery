"""Provenance index: writes and inputs pulled out of session logs, in SQLite.

* ``writes`` -- one row per block of every file-write seen in the logs, keyed by the
  block's content hash. This answers "who wrote a block, and when".
* ``inputs`` -- one row per thing a session read (file, web fetch, tool result,
  instruction file, user text), with its text kept for matching. This answers "what was
  in front of the session before it wrote".

Local only, and it stores log-derived text; keep it outside every scanned root and with
the same care as the memory it describes.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..memory.blocks import split_blocks
from .session_logs import Session, WriteAction

_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (path TEXT PRIMARY KEY, session_id TEXT, mtime REAL);
CREATE TABLE IF NOT EXISTS writes (
    hash TEXT NOT NULL, path TEXT NOT NULL, session_id TEXT NOT NULL, write_uuid TEXT,
    ts TEXT NOT NULL, kind TEXT NOT NULL, is_sidechain INTEGER NOT NULL, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS writes_hash ON writes(hash);
CREATE INDEX IF NOT EXISTS writes_path ON writes(path);
CREATE TABLE IF NOT EXISTS inputs (
    session_id TEXT NOT NULL, ts TEXT NOT NULL, origin TEXT NOT NULL, ref TEXT,
    is_sidechain INTEGER NOT NULL, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inputs_session ON inputs(session_id, ts);
"""


@dataclass
class WriteRow:
    hash: str
    path: str
    session_id: str
    ts: str
    kind: str
    is_sidechain: bool
    text: str


class ProvenanceIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def build(self, sessions: Iterable[Session], *, rebuild: bool = False) -> dict[str, int]:
        if rebuild:
            for t in ("logs", "writes", "inputs"):
                self.db.execute(f"DELETE FROM {t}")
        n_logs = n_writes = n_inputs = 0
        for sess in sessions:
            mtime = Path(sess.path).stat().st_mtime if Path(sess.path).exists() else 0.0
            row = self.db.execute("SELECT mtime FROM logs WHERE path = ?", (sess.path,)).fetchone()
            if row and abs(row["mtime"] - mtime) < 1e-6:
                continue
            self.db.execute("DELETE FROM writes WHERE session_id = ? AND path IN "
                            "(SELECT path FROM writes WHERE session_id = ?)",
                            (sess.session_id, sess.session_id))
            self.db.execute("DELETE FROM inputs WHERE session_id = ?", (sess.session_id,))
            self.db.execute("INSERT OR REPLACE INTO logs VALUES (?,?,?)",
                            (sess.path, sess.session_id, mtime))
            n_logs += 1
            n_writes += self._add_writes(sess.writes)
            for a in sess.inputs:
                self.db.execute("INSERT INTO inputs VALUES (?,?,?,?,?,?)",
                                (a.session_id, a.ts, a.origin, a.ref, int(a.is_sidechain), a.text))
                n_inputs += 1
        self.db.commit()
        return {"logs": n_logs, "writes": n_writes, "inputs": n_inputs}

    def _add_writes(self, writes: list[WriteAction]) -> int:
        n = 0
        for w in writes:
            if not w.text:
                continue
            for b in split_blocks(w.text):
                self.db.execute("INSERT INTO writes VALUES (?,?,?,?,?,?,?,?)",
                                (b.hash, w.path, w.session_id, w.uuid, w.ts, w.kind,
                                 int(w.is_sidechain), b.text))
                n += 1
        return n

    def is_empty(self) -> bool:
        return self.db.execute("SELECT 1 FROM logs LIMIT 1").fetchone() is None

    def writes_of(self, block_hash: str) -> list[WriteRow]:
        rows = self.db.execute(
            "SELECT hash, path, session_id, ts, kind, is_sidechain, text FROM writes "
            "WHERE hash = ? ORDER BY ts", (block_hash,))
        return [WriteRow(r["hash"], r["path"], r["session_id"], r["ts"], r["kind"],
                         bool(r["is_sidechain"]), r["text"]) for r in rows]

    def writes_to(self, path: str) -> list[WriteRow]:
        rows = self.db.execute(
            "SELECT hash, path, session_id, ts, kind, is_sidechain, text FROM writes "
            "WHERE path = ? ORDER BY ts", (path,))
        return [WriteRow(r["hash"], r["path"], r["session_id"], r["ts"], r["kind"],
                         bool(r["is_sidechain"]), r["text"]) for r in rows]

    def path_ever_written(self, path: str) -> bool:
        return self.db.execute("SELECT 1 FROM writes WHERE path = ? LIMIT 1",
                               (path,)).fetchone() is not None

    def inputs_before(self, session_id: str, ts: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT ts, origin, ref, text FROM inputs WHERE session_id = ? AND ts < ? "
            "ORDER BY ts DESC", (session_id, ts))
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.db.close()
