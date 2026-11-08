"""Prompt router: one fast decision, then the cheapest configured target that works.

1. The routing spec's cascade decides an outcome (e.g. small / medium / large).
2. ``routes`` maps every outcome to an ordered list of targets, cheapest first, or to
   ``hold``.
3. Dispatch tries the list in order. An unavailable target moves on to the next one.
   A refusal stops the request and holds it for review -- it is not retried on
   another target. An empty list, or every target unavailable, also holds.

A route may declare ``data_tier: local-only``; config loading then refuses any
cloud target in it, so local-only work cannot reach a cloud model by fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .classify import Classifier, build_classifier
from .decision_log import DecisionLog
from .engine import Decision, decide
from .holds import HoldQueue
from .spec import DecisionSpec, load_spec
from .targets import Completion, Target, TargetRefused, TargetUnavailable, build_target


class ConfigError(ValueError):
    pass


@dataclass
class Route:
    name: str
    targets: list[str]
    hold: bool = False
    data_tier: str | None = None


@dataclass
class RouteDecision:
    decision: Decision
    route: Route
    facts: dict[str, Any]

    @property
    def hold(self) -> bool:
        return self.route.hold or not self.route.targets

    def to_dict(self) -> dict[str, Any]:
        return {"route": self.route.name, "targets": self.route.targets, "hold": self.hold,
                "facts": self.facts, "decision": self.decision.to_dict()}


@dataclass
class DispatchResult:
    status: str                       # completed | held
    route: str
    decision_id: str
    completion: Completion | None = None
    hold_id: int | None = None
    reason: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        c = self.completion
        return {"status": self.status, "route": self.route, "decision_id": self.decision_id,
                "hold_id": self.hold_id, "reason": self.reason, "attempts": self.attempts,
                "completion": None if c is None else {
                    "target": c.target, "model": c.model, "served_by": c.served_by,
                    "stop_reason": c.stop_reason, "text": c.text}}


def computed_facts(text: str) -> dict[str, Any]:
    """Deterministic facts every routing decision gets for free."""
    return {"prompt_chars": len(text), "prompt_lines": text.count("\n") + 1 if text else 0}


class Router:
    def __init__(self, spec: DecisionSpec, classifiers: Mapping[str, Classifier],
                 targets: Mapping[str, Target], routes: Mapping[str, Route], *,
                 holds: HoldQueue | None = None, log: DecisionLog | None = None) -> None:
        self.spec = spec
        self.classifiers = dict(classifiers)
        self.targets = dict(targets)
        self.routes = dict(routes)
        self.holds = holds
        self.log = log or DecisionLog(None)
        problems = validate_routes(spec, self.targets, self.routes)
        if problems:
            raise ConfigError("; ".join(problems))

    # -- construction ---------------------------------------------------------------
    @classmethod
    def from_config(cls, path: str | Path, *, overrides: Mapping[str, Any] | None = None) -> "Router":
        p = Path(path)
        cfg = yaml.safe_load(p.read_text()) or {}
        cfg.update(overrides or {})
        return cls.from_dict(cfg, base=p.parent)

    @classmethod
    def from_dict(cls, cfg: Mapping[str, Any], base: str | Path = ".") -> "Router":
        base = Path(base)
        try:
            spec = load_spec(base / cfg["spec"])
            classifiers = {n: build_classifier(n, c) for n, c in (cfg.get("classifiers") or {}).items()}
            targets = {n: build_target(n, c) for n, c in (cfg.get("targets") or {}).items()}
        except KeyError as exc:
            raise ConfigError(f"missing config key {exc}") from exc
        except ValueError as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(str(exc)) from exc
        routes = parse_routes(cfg.get("routes") or {})
        holds = HoldQueue(base / cfg["holds_db"]) if cfg.get("holds_db") else None
        log = DecisionLog(base / cfg["decision_log_dir"]) if cfg.get("decision_log_dir") else None
        return cls(spec, classifiers, targets, routes, holds=holds, log=log)

    # -- deciding ---------------------------------------------------------------------
    def _decide(self, text: str, facts: Mapping[str, Any] | None,
                inputs: Mapping[str, Any] | None) -> RouteDecision:
        all_facts = {**dict(facts or {}), **computed_facts(text)}
        d = decide(self.spec, self.classifiers, {**dict(inputs or {}), "text": text}, all_facts)
        route = self.routes.get(d.outcome) or Route(name=d.outcome, targets=[], hold=True)
        return RouteDecision(decision=d, route=route, facts=all_facts)

    def decide(self, text: str, facts: Mapping[str, Any] | None = None,
               inputs: Mapping[str, Any] | None = None) -> RouteDecision:
        rd = self._decide(text, facts, inputs)
        self.log.write(rd.decision, {"route": rd.route.name, "hold": rd.hold, "mode": "decide"})
        return rd

    # -- dispatching ------------------------------------------------------------------
    def dispatch(self, text: str, *, system: str | None = None, max_tokens: int | None = None,
                 facts: Mapping[str, Any] | None = None,
                 inputs: Mapping[str, Any] | None = None) -> DispatchResult:
        rd = self._decide(text, facts, inputs)
        d = rd.decision
        attempts: list[dict[str, Any]] = []

        def held(reason: str) -> DispatchResult:
            hold_id = None
            if self.holds is not None:
                hold_id = self.holds.add(
                    "prompt", reason,
                    {"text": text, "system": system, "facts": rd.facts, "route": rd.route.name,
                     "attempts": attempts},
                    spec_id=d.spec_id, decision_id=d.decision_id, outcome=d.outcome)
            res = DispatchResult(status="held", route=rd.route.name, decision_id=d.decision_id,
                                 hold_id=hold_id, reason=reason, attempts=attempts)
            self.log.write(d, {"route": rd.route.name, "mode": "dispatch", "status": "held",
                               "reason": reason, "hold_id": hold_id, "attempts": attempts})
            return res

        if rd.hold:
            return held(f"route '{rd.route.name}' holds for review")
        for name in rd.route.targets:
            target = self.targets[name]
            try:
                c = target.complete(text, system=system, max_tokens=max_tokens)
            except TargetRefused as r:
                attempts.append({"target": name, "result": "refused", "category": r.category})
                return held(f"declined by {name}" + (f" ({r.category})" if r.category else ""))
            except TargetUnavailable as u:
                attempts.append({"target": name, "result": "unavailable", "detail": str(u)})
                continue
            attempts.append({"target": name, "result": "completed", "served_by": c.served_by})
            self.log.write(d, {"route": rd.route.name, "mode": "dispatch", "status": "completed",
                               "target": name, "served_by": c.served_by, "attempts": attempts})
            return DispatchResult(status="completed", route=rd.route.name,
                                  decision_id=d.decision_id, completion=c, attempts=attempts)
        return held("no target in the route was available")


def parse_routes(raw: Mapping[str, Any]) -> dict[str, Route]:
    routes: dict[str, Route] = {}
    for name, v in raw.items():
        if v == "hold":
            routes[name] = Route(name=name, targets=[], hold=True)
        elif isinstance(v, list):
            routes[name] = Route(name=name, targets=[str(t) for t in v])
        elif isinstance(v, dict):
            routes[name] = Route(name=name, targets=[str(t) for t in v.get("targets") or []],
                                 hold=bool(v.get("hold", False)), data_tier=v.get("data_tier"))
        else:
            raise ConfigError(f"route {name!r}: expected a list of targets, 'hold', or a mapping")
    return routes


def validate_routes(spec: DecisionSpec, targets: Mapping[str, Target],
                    routes: Mapping[str, Route]) -> list[str]:
    problems: list[str] = []
    for outcome in spec.outcomes:
        if outcome not in routes:
            problems.append(f"spec outcome {outcome!r} has no route (map it to targets or 'hold')")
    for r in routes.values():
        for t in r.targets:
            if t not in targets:
                problems.append(f"route {r.name!r}: unknown target {t!r}")
            elif r.data_tier == "local-only" and targets[t].data_tier != "local-only":
                problems.append(f"route {r.name!r} is local-only but lists {t!r} "
                                f"({targets[t].data_tier})")
        if r.data_tier not in (None, "local-only", "cloud-allowed"):
            problems.append(f"route {r.name!r}: unknown data_tier {r.data_tier!r}")
    return problems
