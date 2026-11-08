"""The memory index: every block of every memory file, keyed by content hash.

SQLite, local. It stores block text (screening and the review page need it), so keep it
outside every scanned root and treat it with the same care as the memories themselves.

Scans are incremental: a file whose sha256 is unchanged keeps its blocks; a changed file
has its blocks replaced; a file that disappeared is dropped. Each scan also reports
**regrowth**: an excised block reappearing anywhere, or a quarantined block reappearing
in the file it was taken from (typically an agent saving back a copy it had read before
the quarantine).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .blocks import Block, split_blocks
from .sources import MemoryFile, SourceConfig, iter_files

EXPOSURE = {"always": 3, "on_demand": 1, "never": 0}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, kind TEXT NOT NULL, role TEXT NOT NULL,
    agent TEXT NOT NULL, path TEXT NOT NULL UNIQUE, loaded TEXT NOT NULL, managed_by TEXT,
    sha TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL, scanned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL, kind TEXT NOT NULL, start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL, section TEXT NOT NULL, hash TEXT NOT NULL, occ INTEGER NOT NULL,
    chars INTEGER NOT NULL, loaded TEXT NOT NULL, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS blocks_hash ON blocks(hash);
CREATE INDEX IF NOT EXISTS blocks_file ON blocks(file_id, ord);
CREATE TABLE IF NOT EXISTS screen (
    hash TEXT NOT NULL, spec_id TEXT NOT NULL, spec_version INTEGER NOT NULL,
    outcome TEXT NOT NULL, resolved INTEGER NOT NULL, decision_id TEXT NOT NULL,
    ts TEXT NOT NULL, PRIMARY KEY (hash, spec_id, spec_version)
);
CREATE TABLE IF NOT EXISTS watchlist (
    hash TEXT NOT NULL, hold_id INTEGER NOT NULL, kind TEXT NOT NULL, path TEXT,
    added_at TEXT NOT NULL, note TEXT, PRIMARY KEY (hash, hold_id)
);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def file_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:12]


def block_id(fid: str, b: Block) -> str:
    return f"{fid}:{b.hash[:12]}:{b.occ}"


@dataclass
class ScanStats:
    files_seen: int = 0
    files_changed: int = 0
    files_removed: int = 0
    blocks: int = 0
    regrowth: list[dict[str, Any]] = field(default_factory=list)
    missing_sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


class MemoryIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_SCHEMA)

    # -- scanning ---------------------------------------------------------------------
    def scan(self, sources: Iterable[SourceConfig]) -> ScanStats:
        stats = ScanStats()
        seen_ids: set[str] = set()
        scanned_sources: list[str] = []
        for src in sources:
            roots = src.roots()
            if roots and not any(r.exists() for r in roots):
                stats.missing_sources.append(src.id)
                continue
            scanned_sources.append(src.id)
            for mf in iter_files(src):
                fid = self.index_file(mf, stats)
                seen_ids.add(fid)
        if scanned_sources:
            marks = ",".join("?" * len(scanned_sources))
            stale = [r["id"] for r in self.db.execute(
                f"SELECT id FROM files WHERE source_id IN ({marks})", scanned_sources)
                if r["id"] not in seen_ids]
            for fid in stale:
                self.db.execute("DELETE FROM files WHERE id = ?", (fid,))
            stats.files_removed = len(stale)
        self.db.commit()
        stats.blocks = self.db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        stats.regrowth = self.regrowth()
        return stats

    def index_file(self, mf: MemoryFile, stats: ScanStats | None = None) -> str:
        data = mf.path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        fid = file_id(mf.path)
        st = mf.path.stat()
        if stats is not None:
            stats.files_seen += 1
        row = self.db.execute("SELECT sha FROM files WHERE id = ?", (fid,)).fetchone()
        meta = (mf.source_id, mf.kind, mf.role, mf.agent, str(mf.path), mf.loaded, mf.managed_by)
        if row and row["sha"] == sha:
            self.db.execute(
                "UPDATE files SET source_id=?, kind=?, role=?, agent=?, path=?, loaded=?,"
                " managed_by=?, scanned_at=? WHERE id=?", (*meta, _now(), fid))
            return fid
        if stats is not None:
            stats.files_changed += 1
        text = data.decode("utf-8", errors="replace")
        self.db.execute("DELETE FROM blocks WHERE file_id = ?", (fid,))
        self.db.execute(
            "INSERT OR REPLACE INTO files(id, source_id, kind, role, agent, path, loaded,"
            " managed_by, sha, size, mtime, scanned_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, *meta, sha, len(data), st.st_mtime, _now()))
        rows = []
        for b in split_blocks(text):
            loaded = mf.frontmatter_loaded if (b.kind == "frontmatter" and mf.frontmatter_loaded) \
                else mf.loaded
            rows.append((block_id(fid, b), fid, b.ord, b.kind, b.start, b.end, b.section,
                         b.hash, b.occ, b.chars, loaded, b.text))
        self.db.executemany(
            "INSERT INTO blocks(id, file_id, ord, kind, start_line, end_line, section, hash, occ,"
            " chars, loaded, text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return fid

    def reindex_path(self, path: Path) -> None:
        """Re-read one already-indexed file after this tool changed it."""
        row = self.db.execute("SELECT * FROM files WHERE id = ?", (file_id(path),)).fetchone()
        if row is None:
            return
        if not path.exists():
            self.db.execute("DELETE FROM files WHERE id = ?", (row["id"],))
        else:
            mf = MemoryFile(row["source_id"], row["kind"], row["role"], row["agent"], path,
                            row["loaded"], row["managed_by"],
                            frontmatter_loaded="always" if row["kind"] == "skills" else None)
            self.index_file(mf)
        self.db.commit()

    # -- queries (metadata first; text only when asked) --------------------------------
    def tree(self) -> list[dict[str, Any]]:
        sql = """
        SELECT f.source_id, f.agent, f.path, f.role, f.loaded, f.managed_by,
               COUNT(b.id) AS blocks, COALESCE(SUM(b.chars), 0) AS chars,
               COALESCE(SUM(CASE WHEN s.outcome IS NOT NULL AND s.outcome NOT IN ('keep')
                                 THEN 1 ELSE 0 END), 0) AS flagged
        FROM files f LEFT JOIN blocks b ON b.file_id = f.id
        LEFT JOIN (SELECT hash, outcome FROM screen GROUP BY hash) s ON s.hash = b.hash
        GROUP BY f.id ORDER BY f.source_id, f.agent, f.path"""
        return [dict(r) for r in self.db.execute(sql)]

    def duplicates(self, min_files: int = 2) -> list[dict[str, Any]]:
        sql = """
        SELECT b.hash, COUNT(DISTINCT b.file_id) AS files, COUNT(DISTINCT f.agent) AS agents,
               GROUP_CONCAT(DISTINCT f.agent) AS agent_list, MIN(b.id) AS sample_block,
               MAX(b.chars) AS chars, MAX(b.kind) AS kind
        FROM blocks b JOIN files f ON f.id = b.file_id
        WHERE b.kind NOT IN ('heading') AND b.chars >= 20
        GROUP BY b.hash HAVING COUNT(DISTINCT b.file_id) >= ?
        ORDER BY files DESC, chars DESC"""
        return [dict(r) for r in self.db.execute(sql, (min_files,))]

    def blocks(self, *, file_path: str | None = None, block_ids: list[str] | None = None,
               hash: str | None = None, contains: str | None = None, outcome: str | None = None,
               with_text: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
        cols = ("b.id, b.file_id, f.source_id, f.agent, f.path, f.role, f.managed_by, b.ord, b.kind,"
                " b.start_line, b.end_line, b.section, b.hash, b.occ, b.chars, b.loaded,"
                " s.outcome AS screen_outcome")
        if with_text:
            cols += ", b.text"
        sql = (f"SELECT {cols} FROM blocks b JOIN files f ON f.id = b.file_id "
               "LEFT JOIN (SELECT hash, outcome FROM screen GROUP BY hash) s ON s.hash = b.hash "
               "WHERE 1=1")
        args: list[Any] = []
        if file_path:
            sql += " AND f.path = ?"
            args.append(str(Path(file_path).expanduser()))
        if block_ids:
            sql += f" AND b.id IN ({','.join('?' * len(block_ids))})"
            args.extend(block_ids)
        if hash:
            sql += " AND b.hash LIKE ?"
            args.append(hash + "%")
        if contains:
            sql += " AND instr(lower(b.text), lower(?)) > 0"
            args.append(contains)
        if outcome:
            sql += " AND s.outcome = ?"
            args.append(outcome)
        sql += " ORDER BY f.path, b.ord"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = [dict(r) for r in self.db.execute(sql, args)]
        for r in rows:
            r["exposure"] = EXPOSURE.get(r["loaded"], 0)
        return rows

    def unscreened(self, spec_id: str, spec_version: int, limit: int | None = None):
        sql = """
        SELECT b.hash, MIN(b.id) AS block_id, MAX(b.text) AS text, MAX(b.kind) AS kind,
               MAX(b.section) AS section, MAX(f.path) AS path, MAX(f.agent) AS agent,
               MAX(f.role) AS role, MAX(b.chars) AS chars,
               MAX(CASE b.loaded WHEN 'always' THEN 3 WHEN 'on_demand' THEN 1 ELSE 0 END) AS exposure,
               MAX(CASE WHEN f.managed_by IS NOT NULL THEN 1 ELSE 0 END) AS managed,
               COUNT(DISTINCT b.file_id) AS copies
        FROM blocks b JOIN files f ON f.id = b.file_id
        WHERE NOT EXISTS (SELECT 1 FROM screen s WHERE s.hash = b.hash AND s.spec_id = ?
                          AND s.spec_version = ?)
        GROUP BY b.hash ORDER BY exposure DESC, copies DESC"""
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.db.execute(sql, (spec_id, spec_version))]

    def record_screen(self, hash: str, spec_id: str, spec_version: int, outcome: str,
                      resolved: bool, decision_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO screen VALUES (?,?,?,?,?,?,?)",
                        (hash, spec_id, spec_version, outcome, int(resolved), decision_id, _now()))
        self.db.commit()

    def screen_summary(self) -> list[dict[str, Any]]:
        sql = """SELECT spec_id, spec_version, outcome, COUNT(*) AS hashes
                 FROM screen GROUP BY spec_id, spec_version, outcome ORDER BY spec_id, outcome"""
        return [dict(r) for r in self.db.execute(sql)]

    # -- watchlist ---------------------------------------------------------------------
    # kind 'quarantined': watched in its own file until released or excised.
    # kind 'excised':     watched everywhere, for good.
    def watch(self, hash: str, hold_id: int, kind: str, path: str | None = None,
              note: str | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO watchlist VALUES (?,?,?,?,?,?)",
                        (hash, hold_id, kind, path, _now(), note))
        self.db.commit()

    def unwatch(self, hash: str, hold_id: int) -> None:
        self.db.execute("DELETE FROM watchlist WHERE hash = ? AND hold_id = ?", (hash, hold_id))
        self.db.commit()

    def watched(self, hash: str, kind: str = "excised") -> bool:
        return self.db.execute("SELECT 1 FROM watchlist WHERE hash = ? AND kind = ?",
                               (hash, kind)).fetchone() is not None

    def regrowth(self) -> list[dict[str, Any]]:
        # Known edge: two identical blocks quarantined from the SAME file under two holds;
        # releasing one reports the other as regrown until it, too, is resolved.
        sql = """SELECT b.id, f.path, f.agent, b.start_line, b.end_line, b.hash,
                        MIN(w.hold_id) AS hold_id, MIN(w.kind) AS kind
                 FROM blocks b JOIN files f ON f.id = b.file_id JOIN watchlist w ON w.hash = b.hash
                 WHERE w.kind = 'excised' OR (w.kind = 'quarantined' AND w.path = f.path)
                 GROUP BY b.id ORDER BY f.path, b.ord"""
        return [dict(r) for r in self.db.execute(sql)]

    def close(self) -> None:
        self.db.close()
