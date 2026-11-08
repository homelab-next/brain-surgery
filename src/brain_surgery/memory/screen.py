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
from .blocks import block_hash, split_blocks
from .index import MemoryIndex


def screen_one(spec: DecisionSpec, classifiers: Mapping[str, Classifier], text: str, *,
               role: str = "note", agent: str = "*", section: str = "",
               watchlist_match: bool = False, exposure: int = 1, copies: int = 1,
               managed: bool = False) -> dict[str, Any]:
    """Run the screen on one candidate string -- a held block, or a reworded replacement
    for it -- WITHOUT storing anything. Use it to preview what outcome a block would get
    before releasing it. If the candidate is multi-block, each block is screened and the
    worst outcome is returned."""
    severity = {"quarantine": 3, "review": 2, "unsure": 1, "keep": 0}
    worst = None
    per_block = []
    for b in split_blocks(text) or [_whole(text)]:
        inputs = {"text": b.text, "agent": agent, "section": section or b.section,
                  "kind": b.kind, "role": role}
        facts = {"watchlist_match": watchlist_match, "exposure": exposure, "copies": copies,
                 "managed": managed, "chars": len(b.text), "kind": b.kind, "role": role}
        d = decide(spec, classifiers, inputs, facts)
        answers = {q: {"state": st.state, "confidence": st.confidence, "tier": st.tier}
                   for q, st in d.answers.items()}
        per_block.append({"kind": b.kind, "outcome": d.outcome, "resolved": d.resolved,
                          "answers": answers, "errors": d.errors})
        if worst is None or severity.get(d.outcome, 1) > severity.get(worst["outcome"], 1):
            worst = per_block[-1]
    return {"spec": f"{spec.id} v{spec.version}", "outcome": worst["outcome"],
            "resolved": worst["resolved"], "worst": worst, "blocks": per_block,
            "hash": block_hash(text)}


class _whole:
    def __init__(self, text: str) -> None:
        self.text, self.kind, self.section = text, "paragraph", ""


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
