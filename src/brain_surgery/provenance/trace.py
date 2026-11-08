"""Trace a memory block back to its source, step by step, to a root.

For a block:

1. **Who wrote it** -- the earliest write in the logs whose file content contained it
   (``writes`` table). That names the session, time and, via the memory index, the agent.
2. **What it came from** -- among the inputs that session read *before* that write, the
   one whose text best contains the block (:mod:`match`). That is the parent, with a
   confidence.
3. **Recurse** -- if the parent is a read of another file that an agent also wrote, repeat
   on that file. Stop at a root: an authored file, outside input, a user message, an
   instruction/skill file, or "no source found".

Fallbacks when the logs don't answer: a ``managed_by`` memory file is rooted at its named
source without logs at all; otherwise git (optional) or the file's mtime give a weak hint.

The result is metadata (paths, sessions, times, methods, confidences). Matched text is
included only when the caller asks for it.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .index import ProvenanceIndex, WriteRow
from .match import similarity

MAX_DEPTH = 8

# root kind -> what to do about it
ACTIONS = {
    "authored": "edit the source file, regenerate or redeploy if it is generated, then quarantine the copies",
    "managed": "change it at the named source; a deploy would otherwise restore it",
    "external": "cannot be deleted at source; add a filter where this feed enters the pipeline",
    "user": "this came from a chat message; quarantine it and add it to the watchlist",
    "composed": "the agent wrote this itself with no copied source found; if it should not persist, "
                "quarantine it and consider the instruction that makes the agent record it",
    "unknown": "no session log explains this block; quarantine and watch, or widen the log window",
}


@dataclass
class Step:
    node: str                 # block | input
    path: str | None
    ref: str | None
    session_id: str | None
    ts: str | None
    agent: str | None
    origin: str | None        # for an input: read | web | tool | instruction | user
    method: str               # exact | overlap | git | mtime | deploy-map | none
    confidence: float


@dataclass
class Trace:
    block_hash: str
    path: str
    steps: list[Step] = field(default_factory=list)
    root_kind: str = "unknown"
    root_where: str | None = None
    action: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self, with_text: bool = False) -> dict[str, Any]:
        return {
            "block_hash": self.block_hash[:12], "path": self.path,
            "root": {"kind": self.root_kind, "where": self.root_where, "action": self.action},
            "steps": [vars(s) for s in self.steps], "notes": self.notes,
        }


class Tracer:
    def __init__(self, prov: ProvenanceIndex, *, agent_of: Callable[[str], str | None] | None = None,
                 managed_of: Callable[[str], str | None] | None = None,
                 git: bool = False, min_confidence: float = 0.4) -> None:
        self.prov = prov
        self.agent_of = agent_of or (lambda _p: None)
        self.managed_of = managed_of or (lambda _p: None)
        self.git = git
        self.min_confidence = min_confidence

    def trace(self, block_hash: str, block_text: str, path: str) -> Trace:
        tr = Trace(block_hash=block_hash, path=path)

        # A managed (deployed/generated) copy is rooted at its source with no logs needed.
        managed = self.managed_of(path)
        if managed:
            tr.steps.append(Step("block", path, None, None, None, self.agent_of(path), None,
                                 "deploy-map", 1.0))
            tr.root_kind, tr.root_where = "managed", managed
            tr.action = ACTIONS["managed"]
            return self._finish(tr)

        visited: set[tuple[str, str]] = set()
        cur_hash, cur_text, cur_path = block_hash, block_text, path
        for _ in range(MAX_DEPTH):
            if (cur_path, cur_hash) in visited:
                tr.notes.append("stopped: a cycle in the write/read chain")
                break
            visited.add((cur_path, cur_hash))
            writes = self.prov.writes_of(cur_hash)
            writes = [w for w in writes if w.path == cur_path] or writes
            if not writes:
                self._root_without_write(tr, cur_path)
                return self._finish(tr)
            w = writes[0]                       # earliest appearance = origin write
            tr.steps.append(Step("block", w.path, None, w.session_id, w.ts,
                                 self.agent_of(w.path), None, "exact", 1.0))
            parent = self._best_input(w, cur_text)
            if parent is None:
                tr.root_kind, tr.root_where = "composed", w.path
                tr.action = ACTIONS["composed"]
                return self._finish(tr)
            score, method, inp = parent
            tr.steps.append(Step("input", inp.get("ref"), inp.get("ref"), w.session_id,
                                 inp["ts"], None, inp["origin"], method, round(score, 3)))
            origin, ref = inp["origin"], inp.get("ref")
            if origin == "read" and ref and self.prov.path_ever_written(ref):
                nxt = self._block_in(ref, cur_text)
                if nxt is not None:
                    cur_hash, cur_text, cur_path = nxt.hash, nxt.text, ref
                    continue
                tr.root_kind, tr.root_where = "authored", ref
                tr.action = ACTIONS["authored"]
                return self._finish(tr)
            tr.root_kind, tr.root_where = self._classify(origin, ref)
            tr.action = ACTIONS[tr.root_kind]
            return self._finish(tr)
        return self._finish(tr)

    # -- helpers ----------------------------------------------------------------------
    def _best_input(self, w: WriteRow, block_text: str):
        best = None
        for inp in self.prov.inputs_before(w.session_id, w.ts):
            score, method = similarity(block_text, inp["text"])
            if score >= self.min_confidence and (best is None or score > best[0]):
                best = (score, method, inp)
        return best

    def _block_in(self, path: str, block_text: str):
        """The write-block in ``path`` that best matches the block text (to recurse)."""
        best, best_score = None, 0.0
        for wr in self.prov.writes_to(path):
            score, _ = similarity(block_text, wr.text)
            if score > best_score:
                best, best_score = wr, score
        return best if best_score >= self.min_confidence else None

    def _classify(self, origin: str, ref: str | None) -> tuple[str, str | None]:
        if origin == "web":
            return "external", ref
        if origin == "user":
            return "user", None
        if origin == "instruction":
            return "managed", ref       # a CLAUDE.md / skill loaded as instructions
        if origin == "read":
            return "authored", ref      # read of a file no agent wrote in the logs
        return "composed", ref

    def _root_without_write(self, tr: Trace, path: str) -> None:
        hint = self._git_hint(path) if self.git else None
        if hint:
            tr.steps.append(Step("block", path, None, None, hint.get("ts"),
                                 self.agent_of(path), None, "git", 0.5))
            tr.root_kind, tr.root_where = "authored", path
            tr.action = ACTIONS["authored"]
            tr.notes.append(f"no session log; git commit {hint['commit'][:10]} by {hint['author']}")
            return
        mtime = self._mtime(path)
        tr.root_kind, tr.root_where = "unknown", path
        tr.action = ACTIONS["unknown"]
        tr.notes.append("no session log covers this write"
                        + (f"; file last modified {mtime}" if mtime else ""))

    def _git_hint(self, path: str) -> dict[str, str] | None:
        p = Path(path)
        try:
            out = subprocess.run(
                ["git", "-C", str(p.parent), "log", "-1", "--format=%H%x00%an%x00%aI",
                 "--", p.name],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0 or not out.stdout.strip():
            return None
        commit, author, ts = (out.stdout.strip().split("\x00") + ["", "", ""])[:3]
        return {"commit": commit, "author": author, "ts": ts}

    @staticmethod
    def _mtime(path: str) -> str | None:
        import datetime as dt
        p = Path(path)
        if p.exists():
            return dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc).date().isoformat()
        return None

    @staticmethod
    def _finish(tr: Trace) -> Trace:
        return tr
