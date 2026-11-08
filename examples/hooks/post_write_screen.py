#!/usr/bin/env python3
"""Claude Code PostToolUse hook: screen a memory right after an agent writes it.

Wire it on Write/Edit so a just-written memory block is screened and, in enforce mode,
auto-quarantined before the next session loads it. The production fleet runs no hooks —
there, call `brain-surgery memory screen-write --file <path>` (or import
`brain_surgery.memory.write_screen.screen_write`) from the dispatch step instead.

Install (in .claude/settings.json), pointing BS_CONFIG at your memory.yaml:

    {
      "hooks": {
        "PostToolUse": [
          { "matcher": "Write|Edit",
            "hooks": [ { "type": "command",
                         "command": "BS_CONFIG=/path/to/memory.yaml python3 /path/to/post_write_screen.py" } ] }
        ]
      }
    }

It only acts on files under a scanned memory root, stays silent otherwise, and never fails
the tool call (a screening error must not block the agent's write).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    tool_input = (event.get("tool_input") or {})
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    cfg_path = os.environ.get("BS_CONFIG")
    if not path or not cfg_path:
        return 0
    try:
        from brain_surgery.memory.config import load_memory_config
        from brain_surgery.memory.index import MemoryIndex
        from brain_surgery.memory.quarantine import Quarantine
        from brain_surgery.holds import HoldQueue
        from brain_surgery.memory.write_screen import screen_write
        from brain_surgery.memory.sources import iter_files

        cfg = load_memory_config(cfg_path)
        # only act on a file under one of the configured sources
        p = Path(path).expanduser().resolve()
        known = {Path(mf.path).resolve() for s in cfg.sources for mf in iter_files(s)}
        if p not in known:
            return 0
        idx = MemoryIndex(cfg.index_db)
        res = screen_write(cfg, idx, Quarantine(idx, HoldQueue(cfg.holds_db)), paths=[str(p)])
    except Exception:
        return 0  # never block the write on a screening failure

    q = res.get("quarantined") or []
    if q:
        msg = (f"brain-surgery auto-quarantined {len(q)} block(s) just written to "
               f"{Path(path).name} pending review — they were removed from the live file.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": msg}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
