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
    check = {"index_db": index_db, "holds_db": holds_db}
    if log_dir:
        check["decision_log_dir"] = log_dir
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
    return MemoryConfig(sources=sources, index_db=index_db, holds_db=holds_db,
                        decision_log_dir=log_dir, screen_spec=spec, classifiers=classifiers)
