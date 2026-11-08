"""The cascade: ask tiers in order, stop as soon as the decision table can decide.

Per tier, every still-unresolved question goes in ONE call (Clef scores them all in a
single forward pass; Laya is one round trip instead of N). A question resolves at the
first tier whose answer clears that tier's threshold; anything in the band between
``lo`` and ``hi`` -- or with no calibrated threshold -- is left for the next tier.

The table is evaluated three-valued after each tier: a row is TRUE, FALSE, or UNKNOWN
(it depends on a question not resolved yet). First match wins, so an UNKNOWN row above
a TRUE one blocks the decision and forces escalation -- otherwise a later, looser row
could fire before an earlier, stricter one had its answer.

Errors never raise out of :func:`decide`. A tier that fails contributes nothing and the
cascade moves on; if nothing resolves, the outcome is the spec's ``unsure``.
"""
from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from .classify import Answer, Classifier, ClassifierError, Question
from .spec import DecisionSpec, fact_matches

# Per-question states.
PENDING, HIGH, LOW, UNSURE = "pending", "high", "low", "unsure"


@dataclass
class QuestionState:
    state: str = PENDING          # pending | high | low | unsure | <choice label>
    value: Any = None
    confidence: float | None = None
    tier: str | None = None


@dataclass
class Decision:
    spec_id: str
    spec_version: int
    decision_id: str
    outcome: str
    means: str
    escalate_to: str | None
    resolved: bool                # False when the outcome is the fallback 'unsure'
    answers: dict[str, QuestionState]
    tiers_asked: list[str]
    tier_resolved: str | None
    errors: dict[str, str]
    latency_ms: int
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id, "spec_version": self.spec_version,
            "decision_id": self.decision_id, "outcome": self.outcome, "means": self.means,
            "escalate_to": self.escalate_to, "resolved": self.resolved,
            "answers": {k: vars(v) for k, v in self.answers.items()},
            "tiers_asked": self.tiers_asked, "tier_resolved": self.tier_resolved,
            "errors": self.errors, "latency_ms": self.latency_ms,
        }


def _classify(q: Question, a: Answer, hi: float | None, lo: float | None) -> str:
    """Map one answer to a state using this tier's thresholds."""
    conf = a.confidence
    if conf is None:
        return PENDING
    if q.type == "choice":
        return str(a.value) if hi is not None and conf >= hi else PENDING
    if hi is not None and conf >= hi:
        return HIGH
    if lo is not None and conf <= lo:
        return LOW
    return PENDING


def _cond(q: Question, cond: Any, st: QuestionState) -> bool | None:
    """TRUE / FALSE, or None when the answer is not known. An UNSURE question stays
    unknown for every condition except an explicit ``unsure`` test, so exhausting the
    tiers can never let a looser later row fire in place of a blocked earlier one."""
    if st.state == PENDING:
        return None
    if cond == "unsure":
        return st.state == UNSURE
    if st.state == UNSURE:
        return None
    if q.type == "choice":
        return st.state == cond
    if cond == ">=hi":
        return st.state == HIGH
    if cond == "<=lo":
        return st.state == LOW
    return False


def evaluate(spec: DecisionSpec, states: Mapping[str, QuestionState],
             facts: Mapping[str, Any]) -> str | None:
    """Return the outcome, or None if an UNKNOWN row blocks the decision."""
    qids = spec.question_ids
    for row in spec.table:
        verdict: bool | None = True
        for key, cond in row.when.items():
            v = _cond(spec.question(key), cond, states[key]) if key in qids \
                else fact_matches(cond, facts.get(key))
            if v is False:
                verdict = False
                break
            if v is None:
                verdict = None
        if verdict is True:
            return row.outcome
        if verdict is None:
            return None
    return spec.default


def _needed(spec: DecisionSpec, states: Mapping[str, QuestionState],
            facts: Mapping[str, Any]) -> list[Question]:
    """Pending questions in rows that can still match. Rows already FALSE on a fact or
    a resolved question cannot be revived, so their questions need not be asked."""
    qids = spec.question_ids
    need: list[str] = []
    for row in spec.table:
        dead = False
        for key, cond in row.when.items():
            if key in qids:
                if _cond(spec.question(key), cond, states[key]) is False:
                    dead = True
                    break
            elif not fact_matches(cond, facts.get(key)):
                dead = True
                break
        if dead:
            continue
        for key in row.when:
            if key in qids and states[key].state == PENDING and key not in need:
                need.append(key)
    return [spec.question(k) for k in need]


def decide(spec: DecisionSpec, classifiers: Mapping[str, Classifier],
           inputs: Mapping[str, Any], facts: Mapping[str, Any] | None = None) -> Decision:
    """Run the cascade over ``inputs`` (fills the state card) and ``facts`` (deterministic
    values the table may test). Tier names with no configured classifier are skipped."""
    t0 = time.perf_counter()
    facts = dict(facts or {})
    decision_id = uuid.uuid4().hex
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    state = spec.render_state(inputs)
    states = {q.id: QuestionState() for q in spec.questions}
    events: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    asked: list[str] = []
    tier_resolved: str | None = None

    outcome = evaluate(spec, states, facts)
    for tier in spec.tiers:
        if outcome is not None:
            break
        clf = classifiers.get(tier)
        if clf is None:
            continue
        pending = _needed(spec, states, facts)
        if not pending:
            break
        asked.append(tier)
        t_call = time.perf_counter()
        try:
            answers = clf.ask(state, pending)
            err = None
        except ClassifierError as exc:
            answers, err = {}, str(exc)
            errors[tier] = err
        except Exception as exc:  # noqa: BLE001 - a broken adapter must not break the caller
            answers, err = {}, f"{type(exc).__name__}: {exc}"
            errors[tier] = err
        ms = int((time.perf_counter() - t_call) * 1000)
        for q in pending:
            a = answers.get(q.id)
            hi, lo = spec.threshold(tier, q.id)
            new = _classify(q, a, hi, lo) if a is not None else PENDING
            if new != PENDING:
                states[q.id] = QuestionState(state=new, value=a.value,
                                             confidence=a.confidence, tier=tier)
            elif a is not None and states[q.id].value is None:
                # keep the last unresolved answer for the record
                states[q.id].value, states[q.id].confidence = a.value, a.confidence
            events.append(_event(spec, decision_id, ts, q, tier, clf, a, new, hi, err, ms))
        outcome = evaluate(spec, states, facts)
        if outcome is not None:
            tier_resolved = tier

    resolved = outcome is not None
    if not resolved:
        # Tiers exhausted. Unanswered questions become UNSURE; rows that test `unsure`
        # explicitly may still decide, anything else still blocked escalates.
        for st in states.values():
            if st.state == PENDING:
                st.state = UNSURE
        outcome = evaluate(spec, states, facts)
        resolved = outcome is not None
        if outcome is None:
            outcome = "unsure"
    o = spec.outcomes.get(outcome)
    for e in events:
        e["outcome"] = outcome
    return Decision(
        spec_id=spec.id, spec_version=spec.version, decision_id=decision_id,
        outcome=outcome, means=o.means if o else "", escalate_to=o.escalate_to if o else None,
        resolved=resolved, answers=states, tiers_asked=asked, tier_resolved=tier_resolved,
        errors=errors, latency_ms=int((time.perf_counter() - t0) * 1000), events=events,
    )


def _event(spec: DecisionSpec, decision_id: str, ts: str, q: Question, tier: str,
           clf: Classifier, a: Answer | None, new_state: str, hi: float | None,
           err: str | None, ms: int) -> dict[str, Any]:
    """One row per question per tier, in the shape of the lab's decisions.cascade_events."""
    options = list(q.criteria) if q.type == "choice" and q.criteria else None
    if err:
        abstain = "error"
    elif a is None:
        abstain = "not_answered"
    elif hi is None:
        abstain = "no_threshold"
    elif new_state == PENDING:
        abstain = "unsure_band"
    else:
        abstain = None
    return {
        "event_id": uuid.uuid4().hex, "decision_id": decision_id, "ts": ts,
        "spec_id": spec.id, "spec_version": spec.version, "question_id": q.id,
        "tier": tier, "model_id": getattr(clf, "model_id", None), "qtype": q.type,
        "options": options,
        "probs": [a.probs.get(k) for k in options] if a and a.probs and options else None,
        "answer": None if a is None else a.value,
        "answer_confidence": None if a is None else a.confidence,
        "confidence_kind": None if a is None else a.confidence_kind,
        "accepted": new_state != PENDING, "abstain_reason": abstain,
        "state": None if new_state == PENDING else new_state,
        "error": err, "outcome": None, "latency_ms": ms,
    }
