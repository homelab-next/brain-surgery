"""``brain-surgery memory ...`` subcommands. Metadata only unless ``--text``."""
from __future__ import annotations

import argparse
import json
import sys
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


def cmd_eval(args: argparse.Namespace) -> int:
    from .evaluate import evaluate_screen

    cfg, idx, _ = _open(args)
    if cfg.screen_spec is None:
        print("no screen.spec in the memory config")
        return 1
    res = evaluate_screen(idx, cfg.screen_spec, args.labels, cfg.decision_log_dir)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0
    o = res["outcomes"]
    print(f"{res['spec']}: {o['screened']} of {o['labelled']} labelled blocks screened; "
          f"decided {o['decided']}, unsure {o['unsure']} (unsure rate {o['unsure_rate']}); "
          f"precision when decided {o['precision_when_decided']}")
    print("\nexpected -> actual")
    for exp, acts in sorted(o["confusion"].items()):
        print(f"  {exp:<11} " + "  ".join(f"{a}={n}" for a, n in sorted(acts.items())))
    print("\nby scenario")
    _out(args, [{"scenario": s, **v} for s, v in o["by_scenario"].items()],
         ["scenario", "n", "decided", "correct", "unsure"])
    print("\nquestions (precision when confident, coverage = confident / answered)")
    _out(args, res["questions"], ["question", "tier", "answered", "confident", "correct",
                                  "precision_when_confident", "coverage"])
    print("\nescalated (never resolved): " + ", ".join(f"{q}={v}" for q, v in res["escalated"].items()))
    if res["note"]:
        print(res["note"])
    return 0


def _tracer(cfg, idx, *, rebuild: bool = False):
    from ..provenance.index import ProvenanceIndex
    from ..provenance.session_logs import iter_sessions
    from ..provenance.trace import Tracer

    if not cfg.prov_index_db:
        return None, "no provenance config (add a `provenance:` block with `logs:` to memory.yaml)"
    prov = ProvenanceIndex(cfg.prov_index_db)
    if rebuild or prov.is_empty():
        if not cfg.prov_logs:
            return None, "provenance has no `logs:` roots to read session logs from"
        stats = prov.build(iter_sessions(cfg.prov_logs), rebuild=rebuild)
        print(json.dumps({"built": stats}), file=sys.stderr)
    tr = Tracer(prov, agent_of=idx.agent_for_path, managed_of=idx.managed_for_path,
                git=cfg.prov_git)
    return tr, None


def cmd_trace(args: argparse.Namespace) -> int:
    cfg, idx, _ = _open(args)
    tr, err = _tracer(cfg, idx, rebuild=args.rebuild)
    if err:
        print(err)
        return 1
    rows = idx.blocks(block_ids=[args.id], with_text=True)
    if not rows:
        print(f"no block {args.id} (rescan?)")
        return 1
    b = rows[0]
    result = tr.trace(b["hash"], b["text"], b["path"]).to_dict()
    if args.text:
        result["block_text"] = b["text"]
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_roots(args: argparse.Namespace) -> int:
    from collections import defaultdict

    cfg, idx, _ = _open(args)
    tr, err = _tracer(cfg, idx, rebuild=args.rebuild)
    if err:
        print(err)
        return 1
    if args.all:
        blocks = idx.blocks(with_text=True)
    else:
        blocks = [b for o in (args.outcome or ["review", "quarantine"])
                  for b in idx.blocks(outcome=o, with_text=True)]
    # group block rows by hash so agent counts span every copy, then trace once per hash
    by_hash: dict[str, dict] = {}
    for b in blocks:
        h = by_hash.setdefault(b["hash"], {"agents": set(), "rep": b})
        h["agents"].add(b["agent"])
    groups: dict[tuple, dict] = defaultdict(lambda: {"blocks": 0, "agents": set(), "action": ""})
    for h, info in by_hash.items():
        b = info["rep"]
        t = tr.trace(h, b["text"], b["path"])
        g = groups[(t.root_kind, t.root_where)]
        g["blocks"] += 1
        g["agents"] |= info["agents"]
        g["action"] = t.action
    rows = [{"root_kind": k, "root_where": w, "blocks": v["blocks"],
             "agents": len(v["agents"]), "action": v["action"]}
            for (k, w), v in sorted(groups.items(), key=lambda kv: -kv[1]["blocks"])]
    _out(args, rows, ["root_kind", "blocks", "agents", "root_where", "action"])
    return 0


def cmd_trace_eval(args: argparse.Namespace) -> int:
    from ..provenance.evaluate import evaluate_traces

    cfg, idx, _ = _open(args)
    tr, err = _tracer(cfg, idx, rebuild=args.rebuild)
    if err:
        print(err)
        return 1
    print(json.dumps(evaluate_traces(idx, tr, args.truth), indent=2))
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from ..webapp.api import WebContext
    from ..webapp.server import make_server

    cfg = load_memory_config(args.config)
    ctx = WebContext(cfg, read_only=args.read_only)
    srv = make_server(ctx, args.host, args.port)
    mode = " (read-only)" if args.read_only else ""
    print(f"brain-surgery memory review on http://{args.host}:{srv.server_address[1]}{mode}")
    print("  no auth of its own — keep it behind your proxy/login; it can read and change memory")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    from .synth import generate

    res = generate(args.out, agents=args.agents, seed=args.seed, canary_rate=args.canary_rate)
    if args.with_logs:
        from ..provenance.synth_logs import generate_logs

        res["provenance"] = generate_logs(args.out)
    print(json.dumps(res, indent=2))
    return 0


def register_synth(sub: argparse._SubParsersAction) -> None:
    s = sub.add_parser("synth", help="write a synthetic memory lab with known labels "
                                     "(canary topic, no real sensitive content)")
    s.add_argument("--out", required=True, help="empty or new directory")
    s.add_argument("--agents", type=int, default=8)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--canary-rate", type=float, default=0.5,
                   help="share of agents (after the first three) given extra canary notes")
    s.add_argument("--with-logs", action="store_true",
                   help="also fabricate session logs with known provenance")
    s.set_defaults(func=cmd_synth)


def register(sub: argparse._SubParsersAction) -> None:
    register_synth(sub)
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

    s = ms.add_parser("eval", help="grade the shadow screen against labelled blocks")
    s.add_argument("--labels", required=True, help="labels.jsonl (e.g. from `synth`)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_eval)

    s = ms.add_parser("trace", help="trace one block back to its source (metadata only)")
    s.add_argument("--id", required=True, help="block id")
    s.add_argument("--rebuild", action="store_true", help="rebuild the provenance index from logs")
    s.add_argument("--text", action="store_true", help="include the block's own text")
    s.set_defaults(func=cmd_trace)

    s = ms.add_parser("roots", help="group blocks by the source they trace back to")
    s.add_argument("--all", action="store_true", help="every block (default: review+quarantine)")
    s.add_argument("--outcome", nargs="+", help="screen outcomes to include")
    s.add_argument("--rebuild", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_roots)

    s = ms.add_parser("trace-eval", help="grade provenance roots against a truth file")
    s.add_argument("--truth", required=True, help="provenance_truth.jsonl (from `synth --with-logs`)")
    s.add_argument("--rebuild", action="store_true")
    s.set_defaults(func=cmd_trace_eval)

    s = ms.add_parser("web", help="serve the review web page (treemap, groups, quarantine, trace)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8795)
    s.add_argument("--read-only", action="store_true", help="disable quarantine/release/excise/rescan")
    s.set_defaults(func=cmd_web)
