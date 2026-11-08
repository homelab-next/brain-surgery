"""Quarantine, release and excise memory blocks.

* **quarantine** -- take blocks out of the live file, so no agent loads them, and hold
  them (text and position) in the shared hold queue. Reversible. Until it is resolved,
  a scan reports the block if it reappears in that file.
* **release**    -- put a quarantined block back where it was. Its old neighbours'
  hashes are used as anchors, so it lands in the right place even if the file moved on.
* **excise**     -- confirm the removal. The text is scrubbed from the hold, and the
  content hash goes on the watchlist so a scan reports it if it ever grows back.

Writes are compare-and-swap: the file is re-read right before writing and the write
is abandoned if an agent changed it in between. Each write goes to a temporary file in
the same directory and is renamed into place, so a reader never sees a half-written file.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..holds import HoldQueue
from .blocks import TIGHT, Block, insert_block, remove_lines, split_blocks
from .index import MemoryIndex


class QuarantineError(RuntimeError):
    pass


@dataclass
class QuarantineResult:
    held: list[dict[str, Any]] = field(default_factory=list)      # {hold_id, block_id, path}
    skipped: list[dict[str, Any]] = field(default_factory=list)   # {block_id, reason}

    def to_dict(self) -> dict[str, Any]:
        return {"held": self.held, "skipped": self.skipped}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_if_unchanged(path: Path, expected_sha: str, new_text: str) -> None:
    current = path.read_bytes() if path.exists() else b""
    if _sha(current) != expected_sha:
        raise QuarantineError(f"{path} changed while it was being edited; nothing written, retry")
    mode = path.stat().st_mode if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        os.chmod(tmp, mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _locate(blocks: list[Block], hash_: str, occ: int) -> Block | None:
    hits = [b for b in blocks if b.hash == hash_]
    if not hits:
        return None
    return hits[occ] if occ < len(hits) else hits[-1]


class Quarantine:
    def __init__(self, index: MemoryIndex, holds: HoldQueue) -> None:
        self.index = index
        self.holds = holds

    # -- quarantine ---------------------------------------------------------------------
    def quarantine(self, block_ids: list[str], reason: str, *, force: bool = False) -> QuarantineResult:
        res = QuarantineResult()
        rows = self.index.blocks(block_ids=block_ids, with_text=True)
        found = {r["id"] for r in rows}
        for bid in block_ids:
            if bid not in found:
                res.skipped.append({"block_id": bid, "reason": "not in index (rescan?)"})
        by_file: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_file.setdefault(r["path"], []).append(r)
        for path_s, wanted in by_file.items():
            path = Path(path_s)
            managed = wanted[0]["managed_by"]
            if managed and not force:
                for r in wanted:
                    res.skipped.append({"block_id": r["id"], "reason":
                                        f"managed copy: change it at its source ({managed}); "
                                        "the next deploy would restore it here"})
                continue
            try:
                self._quarantine_file(path, wanted, reason, res)
            except (QuarantineError, OSError) as exc:
                for r in wanted:
                    res.skipped.append({"block_id": r["id"], "reason": str(exc)})
            self.index.reindex_path(path)
        return res

    def _quarantine_file(self, path: Path, wanted: list[dict[str, Any]], reason: str,
                         res: QuarantineResult) -> None:
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
            records.append((r, b, {
                "source_id": r["source_id"], "agent": r["agent"], "path": str(path),
                "kind": b.kind, "section": b.section, "start_line": b.start, "end_line": b.end,
                "hash": b.hash, "occ": b.occ, "chars": b.chars, "text": b.text,
                "copies_before": sum(c.hash == b.hash for c in current),
                "prev_hash": prev.hash if prev else None, "prev_occ": prev.occ if prev else None,
                "prev_kind": prev.kind if prev else None,
                "next_hash": nxt.hash if nxt else None, "next_occ": nxt.occ if nxt else None,
                "next_kind": nxt.kind if nxt else None,
                "file_sha_before": sha_before,
            }))
        new_text = remove_lines(text, [(b.start, b.end) for _, b in targets])
        _write_if_unchanged(path, sha_before, new_text)
        for r, _b, payload in records:
            payload["file_sha_after"] = _sha(new_text.encode("utf-8"))
            hid = self.holds.add("memory", reason, payload)
            self.index.watch(payload["hash"], hid, "quarantined", str(path), reason)
            res.held.append({"hold_id": hid, "block_id": r["id"], "path": str(path)})

    # -- release ------------------------------------------------------------------------
    def release(self, hold_id: int, note: str | None = None, *, replacement: str | None = None,
                watch_original: bool = True) -> dict[str, Any]:
        """Put a quarantined block back where it was. With ``replacement`` (a reworded,
        clarified or pointer-only version), the new text goes in instead of the original,
        and -- unless ``watch_original`` is false -- the original wording is watchlisted so
        a scan reports it if it ever comes back. Plain release restores the original text."""
        h = self._open_hold(hold_id)
        p = h.payload
        if p.get("text") is None:
            raise QuarantineError(f"hold {hold_id} has no text to restore")
        reword = replacement is not None and replacement.strip() and replacement != p["text"]
        restore_text = replacement if reword else p["text"]
        restore_kind = split_blocks(restore_text)[0].kind if reword and split_blocks(restore_text) \
            else p.get("kind", "paragraph")
        path = Path(p["path"])
        raw = path.read_bytes() if path.exists() else b""
        sha = _sha(raw)
        text = raw.decode("utf-8", errors="replace")
        blocks = split_blocks(text)
        if not reword and sum(b.hash == p["hash"] for b in blocks) >= p.get("copies_before", 1):
            where = "already present"
        else:
            after = self._insertion_line(blocks, p)
            new_text = insert_block(text, restore_text, after, restore_kind)
            if path.exists():
                _write_if_unchanged(path, sha, new_text)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_if_unchanged(path, _sha(b""), new_text)
            where = f"after line {after}"
        self.index.unwatch(p["hash"], hold_id)
        if reword and watch_original:
            # tell me if the ORIGINAL wording reappears anywhere after a reword
            self.index.watch(p["hash"], hold_id, "superseded", p.get("path"),
                             f"superseded by reword (hold {hold_id})")
            p = {**p, "released_as": restore_text}
            self.holds.update_payload(hold_id, p)
        self.holds.resolve(hold_id, "released", note)
        self.index.reindex_path(path)
        return {"hold_id": hold_id, "path": str(path), "restored": where,
                "reworded": bool(reword), "watch_original": bool(reword and watch_original)}

    @staticmethod
    def _insertion_line(blocks: list[Block], p: dict[str, Any]) -> int:
        # A list item / table row whose old next neighbour was the same kind goes back
        # just before that neighbour, so it rejoins its list rather than trailing the
        # paragraph above it.
        if p.get("kind") in TIGHT and p.get("next_kind") == p.get("kind") and p.get("next_hash"):
            b = _locate(blocks, p["next_hash"], p.get("next_occ") or 0)
            if b:
                return max(0, b.start - 1)
        if p.get("prev_hash"):
            b = _locate(blocks, p["prev_hash"], p.get("prev_occ") or 0)
            if b:
                return b.end
        if p.get("next_hash"):
            b = _locate(blocks, p["next_hash"], p.get("next_occ") or 0)
            if b:
                return max(0, b.start - 1)
        return max(0, int(p.get("start_line", 1)) - 1)

    # -- excise -------------------------------------------------------------------------
    def excise(self, hold_id: int, note: str | None = None) -> dict[str, Any]:
        h = self._open_hold(hold_id)
        payload = dict(h.payload)
        self.index.watch(payload["hash"], hold_id, "excised", payload.get("path"), note)
        payload["text"] = None
        payload["excised"] = True
        self.holds.update_payload(hold_id, payload)
        self.holds.resolve(hold_id, "excised", note)
        # Excise applies to the content everywhere: any copy still live (never selected, or
        # released from another hold) will now be reported as regrowth by every scan.
        live = [b["id"] for b in self.index.blocks(hash=payload["hash"])]
        return {"hold_id": hold_id, "hash": payload["hash"], "watchlisted": True,
                "live_copies": live}

    def _open_hold(self, hold_id: int):
        h = self.holds.get(hold_id)
        if h is None:
            raise QuarantineError(f"no hold {hold_id}")
        if h.kind != "memory":
            raise QuarantineError(f"hold {hold_id} is a {h.kind}, not a memory")
        if h.status != "held":
            raise QuarantineError(f"hold {hold_id} is already {h.status}")
        return h
