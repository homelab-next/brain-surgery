"""``brain-surgery`` command line.

  brain-surgery lint SPEC [SPEC ...]
  brain-surgery route  --config routing.yaml (--text T | --file F) [--fact k=v ...] [--dispatch]
  brain-surgery holds  --db holds.sqlite list [--status S] [--kind K]
  brain-surgery holds  --db holds.sqlite show ID
  brain-surgery holds  --db holds.sqlite resolve ID {released,excised,dismissed} [--note N]
  brain-surgery serve  --config routing.yaml [--host H] [--port P]
  brain-surgery memory --config memory.yaml {scan,tree,dupes,blocks,quarantine,release,excise,screen}

Nothing prints memory or prompt text unless --text is given, so an agent session that
runs these commands does not pull the content being reviewed into its own context.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .holds import HoldQueue
from .router import ConfigError, Router
from .spec import SpecError, load_spec


def _fact(s: str) -> tuple[str, Any]:
    if "=" not in s:
        raise argparse.ArgumentTypeError(f"--fact expects key=value, got {s!r}")
    k, v = s.split("=", 1)
    try:
        return k, json.loads(v)
    except ValueError:
        return k, v


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_lint(args: argparse.Namespace) -> int:
    rc = 0
    for path in args.specs:
        try:
            s = load_spec(path)
        except SpecError as exc:
            rc = 1
            print(f"FAIL {path}")
            for p in exc.problems:
                print(f"  - {p}")
            continue
        except (OSError, ValueError) as exc:
            rc = 1
            print(f"FAIL {path}: {exc}")
            continue
        untuned = [f"{t}.{q}" for t, qs in s.thresholds.items() for q, v in qs.items()
                   if v.get("hi") is None]
        note = f" (no threshold yet: {', '.join(untuned)})" if untuned else ""
        print(f"ok   {path}  [{s.id} v{s.version}, {len(s.questions)} questions, "
              f"{len(s.table)} rows]{note}")
    return rc


def cmd_route(args: argparse.Namespace) -> int:
    router = Router.from_config(args.config)
    text = Path(args.file).read_text() if args.file else args.text
    facts = dict(args.fact or [])
    if args.dispatch:
        res = router.dispatch(text, system=args.system, facts=facts)
        _print(res.to_dict())
        return 0 if res.status == "completed" else 3
    _print(router.decide(text, facts=facts).to_dict())
    return 0


def cmd_holds(args: argparse.Namespace) -> int:
    q = HoldQueue(args.db)
    if args.action == "list":
        for h in q.list(status=None if args.status == "all" else args.status, kind=args.kind):
            print(f"{h.id:>5}  {h.status:<9} {h.kind:<7} {h.created_at[:19]}  {h.reason}")
        return 0
    if args.action == "show":
        h = q.get(args.id)
        if h is None:
            print(f"no hold {args.id}", file=sys.stderr)
            return 1
        d = h.to_dict()
        if not args.text and isinstance(d["payload"], dict) and d["payload"].get("text"):
            d["payload"] = {**d["payload"], "text": f"<{len(d['payload']['text'])} chars; --text to show>"}
        _print(d)
        return 0
    held = q.get(args.id)
    if held is not None and held.kind == "memory":
        print("memory holds are resolved with `brain-surgery memory release|excise`, which also "
              "restore the text or watchlist it", file=sys.stderr)
        return 1
    h = q.resolve(args.id, args.status, args.note)
    print(f"hold {h.id} -> {h.status}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import make_server

    srv = make_server(Router.from_config(args.config), args.host, args.port)
    print(f"brain-surgery listening on http://{args.host}:{srv.server_address[1]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brain-surgery")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("lint", help="validate decision specs")
    s.add_argument("specs", nargs="+")
    s.set_defaults(func=cmd_lint)

    s = sub.add_parser("route", help="decide a route (and optionally run it)")
    s.add_argument("--config", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--file")
    s.add_argument("--fact", type=_fact, action="append", help="extra fact, key=value")
    s.add_argument("--system")
    s.add_argument("--dispatch", action="store_true", help="run on the chosen target")
    s.set_defaults(func=cmd_route)

    s = sub.add_parser("holds", help="review queue")
    s.add_argument("--db", required=True)
    hs = s.add_subparsers(dest="action", required=True)
    h = hs.add_parser("list")
    h.add_argument("--status", default="held", help="held|released|excised|dismissed|all")
    h.add_argument("--kind")
    h = hs.add_parser("show")
    h.add_argument("id", type=int)
    h.add_argument("--text", action="store_true", help="include held text (off by default)")
    h = hs.add_parser("resolve")
    h.add_argument("id", type=int)
    h.add_argument("status", choices=["released", "excised", "dismissed"])
    h.add_argument("--note")
    s.set_defaults(func=cmd_holds)

    s = sub.add_parser("serve", help="HTTP front: /v1/route, /v1/dispatch, /healthz")
    s.add_argument("--config", required=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)
    s.set_defaults(func=cmd_serve)

    from .memory.cli import register as register_memory

    register_memory(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    from .memory.config import MemoryConfigError
    from .memory.quarantine import QuarantineError

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, SpecError, MemoryConfigError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except QuarantineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
