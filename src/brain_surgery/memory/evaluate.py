"""Grade a shadow screen against labelled blocks (e.g. a synthetic lab's labels.jsonl).

Metrics follow the lab's rule for fast classifiers: **precision when confident plus
escalation rate**, never raw accuracy. An escalation is a correct deferral, not a miss.

* per question and tier: answered, confident, correct, precision_when_confident,
  coverage (confident / answered);
* per question: escalated (asked but never resolved at any tier);
* outcomes: expected vs actual for every labelled, screened hash. "Expected" is the spec's
  own decision table run on the labels, so it follows the spec if you change it. Also
  precision on decided outcomes, the unsure rate, a confusion table and a per-scenario
  breakdown.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..engine import HIGH, LOW, QuestionState, evaluate
from ..spec import DecisionSpec
from .index import MemoryIndex


def load_labels(path: str | Path) -> dict[str, dict[str, Any]]:
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    return {r["hash"]: r for r in rows}


def expected_outcome(spec: DecisionSpec, labels: dict[str, bool],
                     facts: dict[str, Any] | None = None) -> str:
    states = {q.id: QuestionState(state=HIGH if labels.get(q.id) else LOW) for q in spec.questions}
    return evaluate(spec, states, facts or {"watchlist_match": False}) or "unsure"


def _log_rows(log_dir: Path | None) -> list[dict[str, Any]]:
    if not log_dir or not Path(log_dir).is_dir():
        return []
    rows = []
    for f in sorted(Path(log_dir).glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def evaluate_screen(index: MemoryIndex, spec: DecisionSpec, labels_path: str | Path,
                    log_dir: str | Path | None) -> dict[str, Any]:
    labels = load_labels(labels_path)
    screened = {r["hash"]: dict(r) for r in index.db.execute(
        "SELECT hash, outcome, resolved, decision_id FROM screen WHERE spec_id = ? AND spec_version = ?",
        (spec.id, spec.version))}
    hashes = [h for h in labels if h in screened]

    # -- outcomes ---------------------------------------------------------------------
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_scenario: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    decided = correct = unsure = 0
    for h in hashes:
        exp = expected_outcome(spec, labels[h]["labels"])
        act = screened[h]["outcome"]
        confusion[exp][act] += 1
        sc = per_scenario[labels[h]["scenario"]]
        for k in ("n", "decided", "correct", "unsure"):
            sc.setdefault(k, 0)
        sc["n"] += 1
        if act == "unsure":
            unsure += 1
            sc["unsure"] += 1
            continue
        decided += 1
        sc["decided"] += 1
        if act == exp:
            correct += 1
            sc["correct"] += 1
    outcomes = {
        "labelled": len(labels), "screened": len(hashes), "decided": decided, "unsure": unsure,
        "precision_when_decided": round(correct / decided, 4) if decided else None,
        "unsure_rate": round(unsure / len(hashes), 4) if hashes else None,
        "confusion": {e: dict(a) for e, a in confusion.items()},
        "by_scenario": {s: dict(v) for s, v in sorted(per_scenario.items())},
    }

    # -- questions, from the decision log ----------------------------------------------
    dec_to_hash = {screened[h]["decision_id"]: h for h in hashes}
    q_tier: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    asked: dict[str, set[str]] = defaultdict(set)
    resolved: dict[str, set[str]] = defaultdict(set)
    for row in _log_rows(Path(log_dir) if log_dir else None):
        if row.get("kind") != "event" or row.get("decision_id") not in dec_to_hash:
            continue
        h, qid, tier = dec_to_hash[row["decision_id"]], row["question_id"], row["tier"]
        truth = labels[h]["labels"].get(qid)
        if truth is None:
            continue
        m = q_tier[(qid, tier)]
        asked[qid].add(row["decision_id"])
        if row.get("answer") is not None:
            m["answered"] += 1
        if row.get("accepted"):
            m["confident"] += 1
            resolved[qid].add(row["decision_id"])
            if (row.get("state") == HIGH) == bool(truth):
                m["correct"] += 1
    questions = []
    for (qid, tier), m in sorted(q_tier.items()):
        questions.append({
            "question": qid, "tier": tier, "answered": m["answered"], "confident": m["confident"],
            "correct": m["correct"],
            "precision_when_confident": round(m["correct"] / m["confident"], 4) if m["confident"] else None,
            "coverage": round(m["confident"] / m["answered"], 4) if m["answered"] else None,
        })
    escalation = {q: round(1 - len(resolved[q]) / len(asked[q]), 4) for q in asked if asked[q]}
    return {"spec": f"{spec.id} v{spec.version}", "outcomes": outcomes, "questions": questions,
            "escalated": escalation,
            "note": None if questions else "no decision-log rows found: set decision_log_dir "
                                           "and screen again to get per-question metrics"}
