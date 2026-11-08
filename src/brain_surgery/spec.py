"""Decision specs.

Reads the decision-spec YAML shape used by the homelab's ``decisions/specs/`` (see
``_template.yaml`` there). Keys this engine does not use (``mask``, ``variants``,
``labels``, ``results``, ``inputs``, ...) are accepted and ignored, so one file can be
read by both.

Validation is fatal and collects every problem before raising, so a broken spec is
reported in full rather than one error per run.
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .classify import QTYPES, Question

ERROR_DIRECTIONS = ("noise_gate", "safety_gate")
MAX_CHOICE = 14
# Outcomes a safety gate may never produce from model answers alone (needs a fact too).
PERMISSIVE = {"yes", "approve", "approved", "allow", "grant", "auto_close", "agent",
              "local_ok", "benign", "release", "released"}
QUESTION_CONDITIONS = (">=hi", "<=lo", "unsure")
_FACT_CMP = re.compile(r"^\s*(>=|<=|>|<|==|!=)\s*(-?\d+(?:\.\d+)?)\s*$")


class SpecError(ValueError):
    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        super().__init__(f"{source}: " + "; ".join(problems))


@dataclass(frozen=True)
class Row:
    when: Mapping[str, Any]
    outcome: str


@dataclass(frozen=True)
class Outcome:
    id: str
    means: str = ""
    escalate_to: str | None = None


@dataclass
class DecisionSpec:
    id: str
    version: int
    status: str
    error_direction: str
    tiers: list[str]
    state_card: str
    questions: list[Question]
    thresholds: dict[str, dict[str, dict[str, float | None]]]
    threshold_meta: dict[str, dict[str, Any]]
    outcomes: dict[str, Outcome]
    table: list[Row]
    default: str
    source: str = "<spec>"
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def question_ids(self) -> set[str]:
        return {q.id for q in self.questions}

    def question(self, qid: str) -> Question:
        for q in self.questions:
            if q.id == qid:
                return q
        raise KeyError(qid)

    def facts_used(self) -> set[str]:
        qids = self.question_ids
        return {k for r in self.table for k in r.when if k not in qids}

    def threshold(self, tier: str, qid: str) -> tuple[float | None, float | None]:
        t = self.thresholds.get(tier, {}).get(qid) or {}
        return t.get("hi"), t.get("lo")

    def render_state(self, inputs: Mapping[str, Any]) -> str:
        """Fill ``state_card`` from inputs; a missing field renders as empty."""
        return string.Formatter().vformat(self.state_card, (), _Blank(inputs))


class _Blank(dict):
    def __init__(self, src: Mapping[str, Any]) -> None:
        super().__init__({k: ("" if v is None else v) for k, v in src.items()})

    def __missing__(self, key: str) -> str:
        return ""


def load_spec(path: str | Path) -> DecisionSpec:
    p = Path(path)
    data = yaml.safe_load(p.read_text())
    return parse_spec(data, source=str(p))


def parse_spec(data: Any, source: str = "<spec>") -> DecisionSpec:
    problems: list[str] = []
    if not isinstance(data, dict):
        raise SpecError(source, ["spec is not a mapping"])

    sid = data.get("id")
    if not isinstance(sid, str) or not sid:
        problems.append("missing id")
    err_dir = data.get("error_direction")
    if err_dir not in ERROR_DIRECTIONS:
        problems.append(f"error_direction must be one of {ERROR_DIRECTIONS}, got {err_dir!r}")
    if data.get("on_error", "escalate") != "escalate":
        problems.append("on_error must be 'escalate' (a timeout, crash or malformed reply escalates)")

    tiers = data.get("tiers") or []
    if not isinstance(tiers, list) or not all(isinstance(t, str) for t in tiers):
        problems.append("tiers must be a list of tier names")
        tiers = []

    questions: list[Question] = []
    seen: set[str] = set()
    for i, q in enumerate(data.get("questions") or []):
        if not isinstance(q, dict):
            problems.append(f"questions[{i}] is not a mapping")
            continue
        qid, qtype, text = q.get("id"), q.get("type"), q.get("text")
        if not qid:
            problems.append(f"questions[{i}] has no id")
            continue
        if qid in seen:
            problems.append(f"duplicate question id {qid!r}")
        seen.add(qid)
        if qtype not in QTYPES:
            problems.append(f"question {qid!r}: type must be one of {QTYPES}, got {qtype!r}")
        if not text:
            problems.append(f"question {qid!r}: no text")
        criteria = q.get("criteria")
        if qtype == "choice":
            if not isinstance(criteria, dict) or len(criteria) < 2:
                problems.append(f"question {qid!r}: choice needs a criteria mapping of 2+ labels")
            elif len(criteria) > MAX_CHOICE:
                problems.append(f"question {qid!r}: {len(criteria)} options > {MAX_CHOICE}")
        questions.append(Question(id=qid, type=qtype, text=str(text or ""),
                                  criteria=dict(criteria) if isinstance(criteria, dict) else None))
    if not questions:
        problems.append("no questions")

    thresholds: dict[str, dict[str, dict[str, float | None]]] = {}
    meta: dict[str, dict[str, Any]] = {}
    for tier, block in (data.get("thresholds") or {}).items():
        if not isinstance(block, dict):
            problems.append(f"thresholds.{tier} is not a mapping")
            continue
        thresholds[tier] = {}
        meta[tier] = {k: v for k, v in block.items() if k not in seen}
        for k, v in block.items():
            if k in seen:
                if not isinstance(v, dict):
                    problems.append(f"thresholds.{tier}.{k} must be {{hi, lo}}")
                    continue
                hi, lo = v.get("hi"), v.get("lo")
                for name, val in (("hi", hi), ("lo", lo)):
                    if val is not None and not isinstance(val, (int, float)):
                        problems.append(f"thresholds.{tier}.{k}.{name} must be a number or null")
                if isinstance(hi, (int, float)) and isinstance(lo, (int, float)) and lo > hi:
                    problems.append(f"thresholds.{tier}.{k}: lo {lo} > hi {hi}")
                thresholds[tier][k] = {"hi": hi, "lo": lo}

    outcomes: dict[str, Outcome] = {}
    for i, o in enumerate(data.get("outcomes") or []):
        if not isinstance(o, dict) or not o.get("id"):
            problems.append(f"outcomes[{i}] needs an id")
            continue
        outcomes[o["id"]] = Outcome(id=o["id"], means=str(o.get("means") or ""),
                                    escalate_to=o.get("escalate_to"))
    if "unsure" not in outcomes:
        problems.append("outcomes must include 'unsure' (what an unresolved decision means)")
    elif not outcomes["unsure"].escalate_to:
        problems.append("outcome 'unsure' needs escalate_to")

    table: list[Row] = []
    default: str | None = None
    rows = data.get("decision_table") or []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            problems.append(f"decision_table[{i}] is not a mapping")
            continue
        if "default" in r:
            if i != len(rows) - 1:
                problems.append("decision_table: 'default' must be the last row")
            default = r["default"]
            continue
        when, outcome = r.get("when"), r.get("outcome")
        if not isinstance(when, dict) or not when:
            problems.append(f"decision_table[{i}] needs a non-empty when")
            continue
        if not outcome:
            problems.append(f"decision_table[{i}] needs an outcome")
            continue
        for key, cond in when.items():
            if key in seen:
                q = next(q for q in questions if q.id == key)
                problems.extend(_check_question_condition(q, cond, i))
            else:
                problems.extend(_check_fact_condition(key, cond, i))
        if err_dir == "safety_gate" and str(outcome).lower() in PERMISSIVE \
                and all(k in seen for k in when):
            problems.append(f"decision_table[{i}]: safety gate produces permissive outcome "
                            f"{outcome!r} from model answers alone; add a deterministic fact")
        table.append(Row(when=dict(when), outcome=str(outcome)))
    if default is None:
        problems.append("decision_table needs a final 'default' row")
    for o in [r.outcome for r in table] + ([default] if default else []):
        if outcomes and o not in outcomes:
            problems.append(f"decision_table uses undeclared outcome {o!r}")

    if problems:
        raise SpecError(source, problems)
    return DecisionSpec(
        id=sid, version=int(data.get("version") or 1), status=str(data.get("status") or "draft"),
        error_direction=err_dir, tiers=list(tiers), state_card=str(data.get("state_card") or "{text}"),
        questions=questions, thresholds=thresholds, threshold_meta=meta, outcomes=outcomes,
        table=table, default=str(default), source=source, raw=data,
    )


def _check_question_condition(q: Question, cond: Any, i: int) -> list[str]:
    if q.type == "choice":
        allowed = set(q.criteria or {}) | {"unsure"}
        if cond not in allowed:
            return [f"decision_table[{i}]: {q.id} must be a label or 'unsure', got {cond!r}"]
        return []
    if cond not in QUESTION_CONDITIONS:
        return [f"decision_table[{i}]: {q.id} must be one of {QUESTION_CONDITIONS}, got {cond!r}"]
    return []


def _check_fact_condition(key: str, cond: Any, i: int) -> list[str]:
    if isinstance(cond, (bool, int, float)) or cond is None:
        return []
    if isinstance(cond, str) and (_FACT_CMP.match(cond) or not cond.startswith((">", "<", "=", "!"))):
        return []
    return [f"decision_table[{i}]: fact {key!r} has an unreadable condition {cond!r}"]


def fact_matches(cond: Any, value: Any) -> bool:
    """Evaluate a fact condition. A missing fact never matches (cannot corroborate)."""
    if value is None:
        return False
    if isinstance(cond, str):
        m = _FACT_CMP.match(cond)
        if m:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return False
            op, n = m.group(1), float(m.group(2))
            return {">": v > n, "<": v < n, ">=": v >= n, "<=": v <= n,
                    "==": v == n, "!=": v != n}[op]
    if isinstance(cond, bool):
        return bool(value) is cond if isinstance(value, bool) else str(value).lower() == str(cond).lower()
    return value == cond or str(value) == str(cond)
