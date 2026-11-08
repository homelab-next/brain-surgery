"""Append-only decision log: one JSONL file per UTC day.

Rows are the per-question events from :mod:`engine` plus one summary row per decision,
so the log can be loaded into a ``decisions.cascade_events``-style table and replayed
when a classifier is swapped. Writing never raises into the caller.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from .engine import Decision


class DecisionLog:
    def __init__(self, directory: str | Path | None) -> None:
        self.directory = Path(directory) if directory else None

    def write(self, decision: Decision, extra: dict[str, Any] | None = None) -> None:
        if self.directory is None:
            return
        summary = {"kind": "decision", **decision.to_dict(), **(extra or {})}
        rows = [{"kind": "event", **e} for e in decision.events] + [summary]
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            day = dt.datetime.now(dt.timezone.utc).date().isoformat()
            with open(self.directory / f"{day}.jsonl", "a", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, default=str) + "\n")
        except OSError as exc:
            print(f"[brain-surgery] decision log write failed (ignored): {exc}", file=sys.stderr)
