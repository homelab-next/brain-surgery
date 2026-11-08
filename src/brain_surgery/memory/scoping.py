"""Scoping: keep a memory, but load it only into sessions on a designated model.

A legitimate memory that trips a general model's safety classifier doesn't have to be
deleted — it can be **scoped**. The block is moved out of its live file into a sibling
``<stem>.scoped.<scope>.md`` that the agent's normal memory glob excludes, and recorded in
a manifest. A small **loader** (``scoped_for``) concatenates the scoped file back only when
the session's model matches the scope.

So in a general-model session the block is simply absent (no classifier to trip); in a
session on a model the scope permits, it loads as before. Reversible: ``unscope`` moves the
block back to its original file.

Writes are compare-and-swap, like quarantine, and reuse its file helpers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..holds import HoldQueue  # noqa: F401  (kept for symmetry / future hold integration)
from .blocks import TIGHT, Block, block_hash, insert_block, remove_lines, split_blocks
from .index import MemoryIndex
from .quarantine import Quarantine, QuarantineError, _locate, _sha, _write_if_unchanged

_SCOPED_STEM = re.compile(r"^(?P<stem>.+)\.md$")


class ScopeError(RuntimeError):
    pass


@dataclass
class ScopeResult:
    scoped: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"scoped": self.scoped, "skipped": self.skipped}


def scoped_sibling(orig_path: str, scope: str) -> Path:
    p = Path(orig_path)
    m = _SCOPED_STEM.match(p.name)
    stem = m.group("stem") if m else p.name
    return p.with_name(f"{stem}.scoped.{scope}.md")


def _frontmatter(scope: str, label: str, orig_name: str) -> str:
    return (f"---\nscope: {scope}\nlabel: {label}\nsource: {orig_name}\n"
            "note: loaded only into sessions whose model this scope permits; "
            "managed by brain-surgery — do not hand-edit\n---\n")


class Scoper:
    def __init__(self, index: MemoryIndex, scopes: dict[str, dict[str, Any]]) -> None:
        self.index = index
        self.scopes = scopes

    def scope(self, block_ids: list[str], scope: str, note: str | None = None) -> ScopeResult:
        if scope not in self.scopes:
            raise ScopeError(f"unknown scope {scope!r} (configure it under scopes:)")
        res = ScopeResult()
        rows = self.index.blocks(block_ids=block_ids, with_text=True)
        found = {r["id"] for r in rows}
        for bid in block_ids:
            if bid not in found:
                res.skipped.append({"block_id": bid, "reason": "not in index (rescan?)"})
        by_file: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_file.setdefault(r["path"], []).append(r)
        for path_s, wanted in by_file.items():
            if wanted[0]["managed_by"]:
                for r in wanted:
                    res.skipped.append({"block_id": r["id"], "reason":
                                        f"managed copy: scope it at its source ({wanted[0]['managed_by']})"})
                continue
            try:
                self._scope_file(Path(path_s), wanted, scope, note, res)
            except (ScopeError, QuarantineError, OSError) as exc:
                for r in wanted:
                    res.skipped.append({"block_id": r["id"], "reason": str(exc)})
            self.index.reindex_path(Path(path_s))
        return res

    def _scope_file(self, path: Path, wanted: list[dict[str, Any]], scope: str,
                    note: str | None, res: ScopeResult) -> None:
        raw = path.read_bytes()
        sha_before = _sha(raw)
        text = raw.decode("utf-8", errors="replace")
        current = split_blocks(text)
        targets: list[tuple[dict[str, Any], Block]] = []
        for r in wanted:
            b = _locate(current, r["hash"], r["occ"])
            if b is None:
                res.skipped.append({"block_id": r["id"], "reason": "no longer in the file (rescan)"})
            else:
                targets.append((r, b))
        if not targets:
            return
        removing = {b.ord for _, b in targets}
        records = []
        for r, b in targets:
            prev = next((c for c in reversed(current[:b.ord]) if c.ord not in removing), None)
            nxt = next((c for c in current[b.ord + 1:] if c.ord not in removing), None)
            records.append((r, b, prev, nxt))
        # 1. append to the scoped sibling (create with frontmatter if new)
        scoped_path = scoped_sibling(str(path), scope)
        sraw = scoped_path.read_bytes() if scoped_path.exists() else b""
        ssha = _sha(sraw)
        stext = sraw.decode("utf-8", errors="replace")
        if not stext.strip():
            label = self.scopes[scope].get("label", scope)
            stext = _frontmatter(scope, label, path.name)
        add = "\n".join(b.text for _, b, _, _ in records)
        new_scoped = stext.rstrip("\n") + "\n\n" + add + "\n"
        # 2. remove from the live file
        new_live = remove_lines(text, [(b.start, b.end) for _, b, _, _ in records])
        _write_if_unchanged(path, sha_before, new_live)
        try:
            if scoped_path.exists():
                _write_if_unchanged(scoped_path, ssha, new_scoped)
            else:
                scoped_path.parent.mkdir(parents=True, exist_ok=True)
                _write_if_unchanged(scoped_path, _sha(b""), new_scoped)
        except QuarantineError:
            _write_if_unchanged(path, _sha(new_live.encode()), text)  # roll the live file back
            raise
        for r, b, prev, nxt in records:
            sid = self.index.add_scoped({
                "created_at": _now(), "scope": scope, "orig_path": str(path),
                "scoped_path": str(scoped_path), "agent": r["agent"], "hash": b.hash,
                "kind": b.kind, "prev_hash": prev.hash if prev else None,
                "prev_occ": prev.occ if prev else None, "prev_kind": prev.kind if prev else None,
                "next_hash": nxt.hash if nxt else None, "next_occ": nxt.occ if nxt else None,
                "next_kind": nxt.kind if nxt else None,
                "copies_before": sum(c.hash == b.hash for c in current), "text": b.text, "note": note})
            res.scoped.append({"scoped_id": sid, "block_id": r["id"], "scope": scope,
                               "scoped_path": str(scoped_path)})

    def unscope(self, scoped_id: int, note: str | None = None) -> dict[str, Any]:
        row = self.index.get_scoped(scoped_id)
        if row is None:
            raise ScopeError(f"no scoped block {scoped_id}")
        orig = Path(row["orig_path"])
        scoped_path = Path(row["scoped_path"])
        # 1. remove from the scoped file
        if scoped_path.exists():
            sraw = scoped_path.read_bytes()
            sblocks = split_blocks(sraw.decode("utf-8", errors="replace"))
            b = _locate(sblocks, row["hash"], 0)
            if b is not None:
                new_scoped = remove_lines(sraw.decode("utf-8", errors="replace"), [(b.start, b.end)])
                body = [x for x in split_blocks(new_scoped) if x.kind != "frontmatter"]
                if not body:
                    scoped_path.unlink()            # nothing left but frontmatter -> drop the file
                else:
                    _write_if_unchanged(scoped_path, _sha(sraw), new_scoped)
        # 2. restore into the original file at its old anchors
        raw = orig.read_bytes() if orig.exists() else b""
        text = raw.decode("utf-8", errors="replace")
        blocks = split_blocks(text)
        if sum(x.hash == row["hash"] for x in blocks) >= (row.get("copies_before") or 1):
            where = "already present"
        else:
            after = Quarantine._insertion_line(blocks, row)
            new_text = insert_block(text, row["text"], after, row.get("kind") or "paragraph")
            if orig.exists():
                _write_if_unchanged(orig, _sha(raw), new_text)
            else:
                orig.parent.mkdir(parents=True, exist_ok=True)
                _write_if_unchanged(orig, _sha(b""), new_text)
            where = f"after line {after}"
        self.index.del_scoped(scoped_id)
        self.index.reindex_path(orig)
        return {"scoped_id": scoped_id, "orig_path": str(orig), "restored": where,
                "scoped_path": str(scoped_path)}


def _now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat()


def scoped_for(index: MemoryIndex, cfg, model: str, *, agent: str | None = None,
               with_text: bool = False) -> dict[str, Any]:
    """The loader: which scoped files a session on ``model`` should load, and (with
    ``with_text``) their content. The fleet concatenates these into the agent's memory
    at session start for that model; a general-model session matches no scope and gets
    nothing extra."""
    scopes = cfg.scopes_for_model(model)
    rows = [r for r in index.scoped_paths_for_scopes(scopes)
            if agent is None or r.get("agent") == agent]
    if with_text:
        for r in rows:
            p = Path(r["scoped_path"])
            r["text"] = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    return {"model": model, "scopes": scopes, "files": rows}


def propose_scopes(index: MemoryIndex, proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blocks the screen suggests scoping, per the config rules (outcome -> scope).

    A rule is ``{outcome: <screen outcome>, scope: <name>}``. The operator confirms.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in proposals:
        outcome, scope = rule.get("outcome"), rule.get("scope")
        if not outcome or not scope:
            continue
        for b in index.blocks(outcome=outcome, with_text=False):
            key = b["hash"]
            if key in seen:
                continue
            seen.add(key)
            out.append({"block_id": b["id"], "hash": b["hash"], "agent": b["agent"],
                        "path": b["path"], "section": b["section"], "outcome": outcome,
                        "suggested_scope": scope})
    return out
