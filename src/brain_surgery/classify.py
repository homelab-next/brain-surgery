"""Typed-question classifiers.

A classifier answers several typed questions about one piece of text in one call.
Question types follow the ``/v1/systemone`` wire format used by Laya and Clef:

* ``noul``   -- a yes/no question; the answer is P(yes) in [0, 1]
* ``choice`` -- one label from a closed set, with a probability per label
* ``score``  -- a number

Any classifier that implements :class:`Classifier` can sit in a cascade tier.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

QTYPES = ("noul", "choice", "score")


@dataclass(frozen=True)
class Question:
    id: str
    type: str
    text: str
    criteria: Mapping[str, str] | None = None  # choice only: label -> description


@dataclass(frozen=True)
class Answer:
    """One answer from one tier.

    ``value``: P(yes) for noul, the chosen label for choice, the number for score.
    ``confidence``: the number thresholds are compared against -- P(yes) for noul,
    the chosen label's probability for choice, the value itself for score.
    """

    value: float | str | None
    confidence: float | None
    probs: Mapping[str, float] | None = None
    confidence_kind: str = "probability"


class ClassifierError(Exception):
    """The tier could not answer: transport failure, bad status, malformed reply."""


class Classifier(Protocol):
    name: str
    model_id: str | None

    def ask(self, state: str, questions: list[Question]) -> dict[str, Answer]:
        """Answer the questions about ``state``. Omit a question to abstain on it."""
        ...


def _wire_question(q: Question) -> dict[str, Any]:
    out: dict[str, Any] = {"type": q.type, "instructions": q.text}
    if q.type == "choice":
        out["criteria"] = dict(q.criteria or {})
    return out


def parse_systemone_answer(q: Question, raw: Mapping[str, Any] | None) -> Answer | None:
    """Turn one ``answers[<qid>]`` object into an :class:`Answer`, or None if absent."""
    if not raw:
        return None
    if q.type == "noul":
        p = raw.get("noul")
        if p is None:
            return None
        p = float(p)
        return Answer(value=p, confidence=p)
    if q.type == "choice":
        choice = raw.get("choice")
        if choice is None:
            return None
        probs = {str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()}
        conf = probs.get(str(choice))
        if conf is None and probs:
            conf = max(probs.values())
        return Answer(value=str(choice), confidence=conf, probs=probs or None)
    if q.type == "score":
        s = raw.get("score")
        if s is None:
            return None
        s = float(s)
        return Answer(value=s, confidence=s, confidence_kind="score")
    raise ValueError(f"unknown question type {q.type!r}")


class SystemOneClassifier:
    """HTTP client for any server that speaks ``POST /v1/systemone``."""

    def __init__(self, name: str, url: str, model: str | None = None,
                 timeout_s: float = 10.0, state_field: str | None = None) -> None:
        url = url.rstrip("/")
        if not url.endswith("/v1/systemone"):
            url += "/v1/systemone"
        self.name = name
        self.url = url
        self.model_id = model
        self.timeout_s = timeout_s
        self.state_field = state_field  # None sends state as a string; "body" sends {"body": ...}

    def build_request(self, state: str, questions: list[Question]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "state": state if self.state_field is None else {self.state_field: state},
            "questions": {q.id: _wire_question(q) for q in questions},
        }
        if self.model_id:
            body["model"] = self.model_id
        return body

    def ask(self, state: str, questions: list[Question]) -> dict[str, Answer]:
        if not questions:
            return {}
        payload = json.dumps(self.build_request(state, questions)).encode("utf-8")
        req = urllib.request.Request(self.url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ClassifierError(f"{self.name}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ClassifierError(f"{self.name}: {exc}") from exc
        except ValueError as exc:
            raise ClassifierError(f"{self.name}: malformed JSON") from exc
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            raise ClassifierError(f"{self.name}: reply has no 'answers' object")
        out: dict[str, Answer] = {}
        for q in questions:
            try:
                a = parse_systemone_answer(q, answers.get(q.id))
            except (TypeError, ValueError) as exc:
                raise ClassifierError(f"{self.name}: bad answer for {q.id}: {exc}") from exc
            if a is not None:
                out[q.id] = a
        return out


class RuleClassifier:
    """Deterministic regex rules. A rule that matches answers with certainty;
    no matching rule means the tier abstains on that question.

    ``rules`` maps question id -> list of ``{"pattern": regex, "answer": value}``,
    where value is ``yes``/``no`` for noul, a label for choice, a number for score.
    """

    def __init__(self, name: str, rules: Mapping[str, list[Mapping[str, Any]]]) -> None:
        self.name = name
        self.model_id = None
        self._rules = {qid: [(re.compile(r["pattern"]), r["answer"]) for r in rs]
                       for qid, rs in rules.items()}

    def ask(self, state: str, questions: list[Question]) -> dict[str, Answer]:
        out: dict[str, Answer] = {}
        for q in questions:
            for pattern, answer in self._rules.get(q.id, []):
                if pattern.search(state):
                    out[q.id] = _certain(q, answer)
                    break
        return out


def _certain(q: Question, answer: Any) -> Answer:
    if q.type == "noul":
        if isinstance(answer, bool):
            p = 1.0 if answer else 0.0
        else:
            p = 1.0 if str(answer).lower() in ("yes", "true", "1") else 0.0
        return Answer(value=p, confidence=p, confidence_kind="rule")
    if q.type == "choice":
        return Answer(value=str(answer), confidence=1.0, probs={str(answer): 1.0},
                      confidence_kind="rule")
    v = float(answer)
    return Answer(value=v, confidence=v, confidence_kind="rule")


class StaticClassifier:
    """Fixed or computed answers -- for tests, dry runs and replaying logged answers.

    ``answers`` is either a mapping qid -> raw systemone-style answer object, or a
    callable ``(state, questions) -> mapping``. Set ``fail`` to simulate an outage.
    """

    def __init__(self, name: str,
                 answers: Mapping[str, Mapping[str, Any]] | Callable[..., Mapping[str, Any]],
                 model_id: str | None = None, fail: str | None = None) -> None:
        self.name = name
        self.model_id = model_id
        self._answers = answers
        self.fail = fail
        self.calls: list[list[str]] = []

    def ask(self, state: str, questions: list[Question]) -> dict[str, Answer]:
        self.calls.append([q.id for q in questions])
        if self.fail:
            raise ClassifierError(f"{self.name}: {self.fail}")
        raw = self._answers(state, questions) if callable(self._answers) else self._answers
        out: dict[str, Answer] = {}
        for q in questions:
            a = parse_systemone_answer(q, raw.get(q.id))
            if a is not None:
                out[q.id] = a
        return out


def build_classifier(name: str, cfg: Mapping[str, Any]) -> Classifier:
    """Construct a classifier from a config mapping (``kind`` selects the adapter)."""
    kind = cfg.get("kind")
    if kind == "systemone":
        return SystemOneClassifier(name, cfg["url"], model=cfg.get("model"),
                                   timeout_s=float(cfg.get("timeout_s", 10.0)),
                                   state_field=cfg.get("state_field"))
    if kind == "rules":
        return RuleClassifier(name, cfg.get("rules") or {})
    if kind == "static":
        return StaticClassifier(name, cfg.get("answers") or {}, model_id=cfg.get("model"),
                                fail=cfg.get("fail"))
    if kind == "label_oracle":  # dry runs on a synthetic lab: answers from labels.jsonl
        from .memory.synth import LabelOracle

        return LabelOracle(name, cfg["labels"], noise=float(cfg.get("noise", 0.0)),
                           band=float(cfg.get("band", 0.0)), seed=int(cfg.get("seed", 0)))
    raise ValueError(f"classifier {name!r}: unknown kind {kind!r} "
                     "(systemone | rules | static | label_oracle)")
