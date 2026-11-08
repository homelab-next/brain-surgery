"""Grade provenance tracing against known roots (``provenance_truth.jsonl`` from synth)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..memory.index import MemoryIndex
from .trace import Tracer


def evaluate_traces(index: MemoryIndex, tracer: Tracer, truth_path: str | Path) -> dict[str, Any]:
    truth = {}
    for line in Path(truth_path).read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            truth[r["hash"]] = r["root_kind"]
    rep: dict[str, dict[str, Any]] = {}
    for b in index.blocks(with_text=True):
        if b["hash"] in truth and b["hash"] not in rep:
            rep[b["hash"]] = b
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    correct = 0
    for h, b in rep.items():
        got = tracer.trace(h, b["text"], b["path"]).root_kind
        confusion[truth[h]][got] += 1
        correct += (got == truth[h])
    n = len(rep)
    return {"traced": n, "correct": correct, "accuracy": round(correct / n, 4) if n else None,
            "confusion": {k: dict(v) for k, v in confusion.items()},
            "missing": len(truth) - n}
