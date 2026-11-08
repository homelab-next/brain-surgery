"""``brain-surgery memory ...`` subcommands. Metadata only unless ``--text``."""
from __future__ import annotations

import argparse
import json
from typing import Any

from ..decision_log import DecisionLog
from ..holds import HoldQueue
from .config import load_memory_config
from .index import MemoryIndex
from .quarantine import Quarantine
from .screen import screen


def _open(args: argparse.Namespace):
    cfg = load_memory_config(args.config)
    return cfg, MemoryIndex(cfg.index_db), HoldQueue(cfg.holds_db)


def _out(args: argparse.Namespace, rows: list[dict[str, Any]], cols: list[str]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        print("(none)")
        return
    def cell(c: str, v: Any) -> str:
        s = "" if v is None else str(v)
        if len(s) <= 60:
            return s
        return "…" + s[-59:] if c == "path" else s[:59] + "…"   # paths: keep the file name

    widths = {c: max(len(c), *(len(cell(c, r.get(c))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(cell(c, r.get(c)).ljust(widths[c]) for c in cols).rstrip())
        if "text" in r:
            for line in str(r["text"]).split("\n"):
                print(f"    | {line}")


def cmd_scan(args: argparse.Namespace) -> int:
    cfg, idx, _ = _open(args)
    stats = idx.scan(cfg.sources).to_dict()
    print(json.dumps(stats, indent=2, default=str))
    return 10 if stats["regrowth"] else 0


def cmd_tree(args: argparse.Namespace) -> int:
    _, idx, _ = _open(args)
    rows = [r for r in idx.tree()
            if (not args.agent or r["agent"] == args.agent)
            and (not args.source or r["source_id"] == args.source)]
    _out(args, rows, ["source_id", "agent", "loaded", "role", "blocks", "chars", "flagged",
                      "managed_by", "path"])
    return 0


def cmd_dupes(args: argparse.Namespace) -> int:
    _, idx, _ = _open(args)
    rows = idx.duplicates(min_files=args.min)
    for r in rows:
        r["hash"] = r["hash"][:12]
    _out(args, rows, ["hash", "files", "agents", "chars", "kind", "sample_block", "agent_list"])
    return 0


def cmd_blocks(args: argparse.Namespace) -> int:
    _, idx, _ = _open(args)
    rows = idx.blocks(file_path=args.file, block_ids=args.id, hash=args.hash,
                      contains=args.contains, outcome=args.outcome, with_text=args.text,
                      limit=args.limit)
    _out(args, rows, ["id", "agent", "loaded", "kind", "start_line", "end_line", "chars",
                      "screen_outcome", "section", "path"])
    return 0


def cmd_quarantine(args: argparse.Namespace) -> int:
    _, idx, holds = _open(args)
    ids = list(args.id or [])
    if args.file or args.hash:
        ids += [r["id"] for r in idx.blocks(file_path=args.file, hash=args.hash)]
    if not ids:
        print("nothing selected (give --id, --file or --hash)")
        return 1
    res = Quarantine(idx, holds).quarantine(ids, args.reason, force=args.force)
    print(json.dumps(res.to_dict(), indent=2))
    return 0 if res.held else 1


def cmd_release(args: argparse.Namespace) -> int:
    _, idx, holds = _open(args)
    print(json.dumps(Quarantine(idx, holds).release(args.hold_id, args.note), indent=2))
    return 0


def cmd_excise(args: argparse.Namespace) -> int:
    _, idx, holds = _open(args)
    print(json.dumps(Quarantine(idx, holds).excise(args.hold_id, args.note), indent=2))
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    cfg, idx, _ = _open(args)
    if cfg.screen_spec is None:
        print("no screen.spec in the memory config")
        return 1
    if args.summary:
        _out(args, idx.screen_summary(), ["spec_id", "spec_version", "outcome", "hashes"])
        return 0
    res = screen(idx, cfg.screen_spec, cfg.classifiers, limit=args.limit,
                 log=DecisionLog(cfg.decision_log_dir))
    print(json.dumps(res, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    m = sub.add_parser("memory", help="memory index, quarantine and shadow screening")
    m.add_argument("--config", required=True, help="memory.yaml")
    ms = m.add_subparsers(dest="memory_cmd", required=True)

    s = ms.add_parser("scan", help="index every source (exit 10 if excised content regrew)")
    s.set_defaults(func=cmd_scan)

    s = ms.add_parser("tree", help="files by source and agent, with block counts")
    s.add_argument("--agent")
    s.add_argument("--source")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_tree)

    s = ms.add_parser("dupes", help="identical blocks held in several files")
    s.add_argument("--min", type=int, default=2)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_dupes)

    s = ms.add_parser("blocks", help="list blocks (metadata; --text to include content)")
    s.add_argument("--file")
    s.add_argument("--id", nargs="+")
    s.add_argument("--hash", help="hash prefix")
    s.add_argument("--contains", help="case-insensitive substring of the block text")
    s.add_argument("--outcome", help="shadow-screen outcome")
    s.add_argument("--limit", type=int)
    s.add_argument("--text", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_blocks)

    s = ms.add_parser("quarantine", help="take blocks out of their files and hold them")
    s.add_argument("--id", nargs="+", help="block ids")
    s.add_argument("--file", help="every block of this file")
    s.add_argument("--hash", help="every copy of this block, in every file (hash prefix)")
    s.add_argument("--reason", required=True)
    s.add_argument("--force", action="store_true", help="also edit managed (deployed) copies")
    s.set_defaults(func=cmd_quarantine)

    s = ms.add_parser("release", help="put a quarantined block back")
    s.add_argument("hold_id", type=int)
    s.add_argument("--note")
    s.set_defaults(func=cmd_release)

    s = ms.add_parser("excise", help="confirm removal: scrub the text, watch for regrowth")
    s.add_argument("hold_id", type=int)
    s.add_argument("--note")
    s.set_defaults(func=cmd_excise)

    s = ms.add_parser("screen", help="shadow-screen unscreened blocks (records only)")
    s.add_argument("--limit", type=int)
    s.add_argument("--summary", action="store_true", help="counts by outcome so far")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_screen)
