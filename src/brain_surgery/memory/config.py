"""``memory.yaml``: sources, state files, and the optional shadow-screen spec."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..classify import Classifier, build_classifier
from ..spec import DecisionSpec, load_spec
from .sources import SourceConfig, check_outside_roots


class MemoryConfigError(ValueError):
    pass


@dataclass
class MemoryConfig:
    sources: list[SourceConfig]
    index_db: Path
    holds_db: Path
    decision_log_dir: Path | None = None
    screen_spec: DecisionSpec | None = None
    classifiers: dict[str, Classifier] = field(default_factory=dict)
    prov_logs: list[str] = field(default_factory=list)
    prov_index_db: Path | None = None
    prov_git: bool = False
    scopes: dict[str, dict[str, Any]] = field(default_factory=dict)
    scope_proposals: list[dict[str, Any]] = field(default_factory=list)

    def scopes_for_model(self, model: str) -> list[str]:
        """Scope names whose model list matches ``model`` (exact or fnmatch)."""
        import fnmatch
        out = []
        for name, spec in self.scopes.items():
            pats = spec.get("models") or []
            if any(model == p or fnmatch.fnmatch(model, p) for p in pats):
                out.append(name)
        return out


def load_memory_config(path: str | Path) -> MemoryConfig:
    p = Path(path)
    raw: dict[str, Any] = yaml.safe_load(p.read_text()) or {}
    base = p.parent
    try:
        sources = [SourceConfig.from_dict(s) for s in raw.get("sources") or []]
    except ValueError as exc:
        raise MemoryConfigError(str(exc)) from exc
    ids = [s.id for s in sources]
    if len(ids) != len(set(ids)):
        raise MemoryConfigError("source ids must be unique")
    if not sources:
        raise MemoryConfigError("no sources configured")

    def state(key: str, default: str) -> Path:
        return (base / str(raw.get(key) or default)).expanduser()

    index_db, holds_db = state("index_db", "state/memory.sqlite"), state("holds_db", "state/holds.sqlite")
    log_dir = state("decision_log_dir", "state/decisions") if raw.get("decision_log_dir") else None
    prov = raw.get("provenance") or {}
    prov_logs = [str((base / p).expanduser()) for p in (prov.get("logs") or [])]
    prov_index_db = state("x", prov.get("index_db") or "state/provenance.sqlite") \
        if prov else None
    check = {"index_db": index_db, "holds_db": holds_db}
    if log_dir:
        check["decision_log_dir"] = log_dir
    if prov_index_db:
        check["provenance.index_db"] = prov_index_db
    problems = check_outside_roots(sources, check)
    if problems:
        raise MemoryConfigError("; ".join(problems))

    spec = None
    classifiers: dict[str, Classifier] = {}
    scr = raw.get("screen") or {}
    if scr.get("spec"):
        spec = load_spec(base / scr["spec"])
        def resolve(c: dict[str, Any]) -> dict[str, Any]:
            if c.get("kind") == "label_oracle" and c.get("labels"):
                return {**c, "labels": str((base / c["labels"]).expanduser())}
            return c

        try:
            classifiers = {n: build_classifier(n, resolve(c))
                           for n, c in (scr.get("classifiers") or {}).items()}
        except (KeyError, ValueError) as exc:
            raise MemoryConfigError(f"screen classifiers: {exc}") from exc
    scopes = raw.get("scopes") or {}
    if not isinstance(scopes, dict):
        raise MemoryConfigError("scopes must be a mapping of name -> {models, label}")
    for name, spec_ in scopes.items():
        if not isinstance(spec_, dict) or not (spec_.get("models")):
            raise MemoryConfigError(f"scope {name!r} needs a non-empty models list")
    proposals = raw.get("scope_proposals") or []
    for p in proposals:
        if p.get("scope") not in scopes:
            raise MemoryConfigError(f"scope_proposals references unknown scope {p.get('scope')!r}")
    return MemoryConfig(sources=sources, index_db=index_db, holds_db=holds_db,
                        decision_log_dir=log_dir, screen_spec=spec, classifiers=classifiers,
                        prov_logs=prov_logs, prov_index_db=prov_index_db,
                        prov_git=bool(prov.get("git", False)), scopes=scopes,
                        scope_proposals=list(proposals))
