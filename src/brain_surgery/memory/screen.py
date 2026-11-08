"""Shadow screening: run a decision spec over every not-yet-screened block.

Shadow means **record only**: the outcome is stored against the block's content hash
(so identical copies across agents are screened once) and written to the decision log.
Nothing is quarantined automatically. Your quarantine / release / excise decisions on
those blocks become the labels used to calibrate the spec before anything acts on it.

Facts given to the spec's table for each block:

* ``watchlist_match`` -- the hash was excised before and has grown back;
* ``exposure``        -- 3 always loaded, 1 on demand, 0 never;
* ``copies``          -- how many files hold this exact block;
* ``managed``         -- the block lives in a deployed / generated copy;
* ``chars``, ``kind`` and ``role`` (index / topic / instructions / skill / note).
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from ..classify import Classifier
from ..decision_log import DecisionLog
from ..engine import decide
from ..spec import DecisionSpec
from .index import MemoryIndex


def screen(index: MemoryIndex, spec: DecisionSpec, classifiers: Mapping[str, Classifier], *,
           limit: int | None = None, log: DecisionLog | None = None) -> dict[str, Any]:
    log = log or DecisionLog(None)
    counts: Counter[str] = Counter()
    unresolved = 0
    rows = index.unscreened(spec.id, spec.version, limit=limit)
    for r in rows:
        inputs = {"text": r["text"], "path": r["path"], "agent": r["agent"],
                  "section": r["section"], "kind": r["kind"], "role": r["role"]}
        facts = {"watchlist_match": index.watched(r["hash"]), "exposure": r["exposure"],
                 "copies": r["copies"], "managed": bool(r["managed"]), "chars": r["chars"],
                 "kind": r["kind"], "role": r["role"]}
        d = decide(spec, classifiers, inputs, facts)
        index.record_screen(r["hash"], spec.id, spec.version, d.outcome, d.resolved, d.decision_id)
        # the log carries the hash and block id, never the text
        log.write(d, {"mode": "memory_screen", "hash": r["hash"], "block_id": r["block_id"]})
        counts[d.outcome] += 1
        unresolved += 0 if d.resolved else 1
    return {"screened": len(rows), "outcomes": dict(counts), "unresolved": unresolved,
            "spec": f"{spec.id} v{spec.version}"}
