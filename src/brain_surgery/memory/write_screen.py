"""Write-time screening: screen a memory as it is written, and (in enforce mode)
auto-quarantine a high scorer before the next session ever loads it.

This is the proactive twin of the batch ``screen``. Call it right after an agent writes
a memory file — from a Claude Code ``PostToolUse`` hook on Write/Edit, or from the
orchestrator's dispatch step after a run (the production fleet runs no hooks). It:

1. re-indexes the written file(s);
2. screens each block not yet screened for the spec, records the outcome and logs it;
3. in **enforce** mode, auto-quarantines any block whose outcome is in the configured
   actions (default: ``quarantine`` only) — never a managed copy, never content already
   under review or removed (watchlisted).

It is a **safety gate**: it can only take a memory OUT (to the review queue), never
release or excise. Default mode is **shadow** (record only, quarantine nothing); switch to
enforce only after the screen has been calibrated and cleared a promotion gate — run it in
shadow, grade it with ``eval`` against your release/excise labels, then flip the mode.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..decision_log import DecisionLog
from ..engine import decide
from .config import MemoryConfig
from .index import MemoryIndex
from .quarantine import Quarantine


def screen_write(cfg: MemoryConfig, index: MemoryIndex, quarantine: Quarantine, *,
                 paths: list[str] | None = None, block_ids: list[str] | None = None,
                 mode: str | None = None, log: DecisionLog | None = None) -> dict[str, Any]:
    if cfg.screen_spec is None:
        return {"error": "no screen.spec configured", "screened": 0}
    mode = mode or cfg.write_mode
    spec, classifiers = cfg.screen_spec, cfg.classifiers
    log = log or DecisionLog(cfg.decision_log_dir)

    for p in paths or []:
        index.reindex_path(Path(p))

    # candidate blocks: explicit ids, or every block of the given files not yet screened
    if block_ids:
        rows = index.blocks(block_ids=block_ids, with_text=True)
    else:
        rows = []
        for p in paths or []:
            rows += index.blocks(file_path=str(Path(p).expanduser()), with_text=True)

    seen_hash: set[str] = set()
    result = {"mode": mode, "screened": 0, "would_quarantine": [], "quarantined": [],
              "skipped": [], "errors": {}}
    to_quarantine: list[str] = []
    for r in rows:
        h = r["hash"]
        if h in seen_hash:
            continue
        seen_hash.add(h)
        already = index.any_watch(h)
        inputs = {"text": r["text"], "path": r["path"], "agent": r["agent"],
                  "section": r["section"], "kind": r["kind"], "role": r["role"]}
        facts = {"watchlist_match": index.watched(h), "exposure": r["exposure"],
                 "copies": index.count_hash(h), "managed": bool(r["managed_by"]),
                 "chars": r["chars"], "kind": r["kind"], "role": r["role"]}
        # if not yet screened for this spec, decide and record; else reuse the stored outcome
        existing = index.blocks(block_ids=[r["id"]])  # outcome via join
        prior = existing[0].get("screen_outcome") if existing else None
        if prior is None:
            d = decide(spec, classifiers, inputs, facts)
            index.record_screen(h, spec.id, spec.version, d.outcome, d.resolved, d.decision_id)
            log.write(d, {"mode": "write_screen", "run_mode": mode, "hash": h,
                          "block_id": r["id"], "path": r["path"]})
            outcome, errors = d.outcome, d.errors
        else:
            outcome, errors = prior, {}
        result["screened"] += 1
        if errors:
            result["errors"][r["id"]] = errors
        if outcome in cfg.write_actions:
            item = {"block_id": r["id"], "hash": h[:12], "outcome": outcome,
                    "agent": r["agent"], "path": r["path"]}
            result["would_quarantine"].append(item)
            if mode == "enforce":
                if r["managed_by"]:
                    result["skipped"].append({**item, "reason": f"managed copy ({r['managed_by']})"})
                elif already:
                    result["skipped"].append({**item, "reason": f"already {already}"})
                else:
                    to_quarantine.append(r["id"])

    if mode == "enforce" and to_quarantine:
        reason = f"{cfg.write_reason_prefix} ({spec.id} v{spec.version})"
        res = quarantine.quarantine(to_quarantine, reason)
        result["quarantined"] = res.held
        result["skipped"] += res.skipped
    return result
