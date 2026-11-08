"""Give a synthetic lab fabricated session logs with known provenance.

Reads an already-generated lab (tree + ``labels.jsonl``) and writes ``logs/*.jsonl`` plus
``provenance_truth.jsonl`` (block hash -> intended root kind), so provenance tracing can
be graded against ground truth. It also adds a ``provenance:`` block to the lab's two
configs.

Each memory file becomes a Write in its owning session. Canary blocks get a source read
just before the write, chosen by scenario so the trace lands on a known root:

* ``canary_steps``          -> read of an authored brief file (never itself written) -> authored
* ``canary_verbatim``       -> a web fetch whose result contains the quote            -> external
* ``canary_summary``        -> a user message containing the summary                  -> user
* others / routine          -> no source                                             -> composed
* a ``managed_by`` file (the deployed copy)                                           -> managed (no log needed)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..memory.blocks import split_blocks

SCENARIO_ROOT = {
    "canary_steps": "authored", "canary_verbatim": "external", "canary_summary": "user",
    "canary_steps_verbatim": "composed", "canary_pointer": "composed",
}
BRIEF_REL = "_contracts/kestrel-brief.md"       # authored source, read but never written
FETCH_URL = "https://vendor.example.com/kestrel/proposal"


def _clock():
    n = [0]

    def tick() -> str:
        n[0] += 1
        return f"2026-11-{(n[0] // 86400) % 28 + 1:02d}T{(n[0] // 3600) % 24:02d}:" \
               f"{(n[0] // 60) % 60:02d}:{n[0] % 60:02d}Z"
    return tick


def _assistant(uuid: str, ts: str, sid: str, tool: str, tid: str, inp: dict) -> dict:
    return {"type": "assistant", "uuid": uuid, "timestamp": ts, "sessionId": sid,
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": tid, "name": tool, "input": inp}]}}


def _write_pair(tick, uuid, sid, path: str, content: str) -> list[dict]:
    t1, t2, tid = tick(), tick(), f"tu_{uuid}"
    return [
        _assistant(f"a_{uuid}", t1, sid, "Write", tid, {"file_path": path, "content": content}),
        {"type": "user", "uuid": f"u_{uuid}", "timestamp": t2, "sessionId": sid,
         "cwd": str(Path(path).parent), "gitBranch": "main", "isSidechain": False,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                   "content": "wrote the file"}]},
         "toolUseResult": {"type": "create", "filePath": path, "content": content}}]


def _read_pair(tick, uuid, sid, path: str, content: str) -> list[dict]:
    t1, t2, tid = tick(), tick(), f"tr_{uuid}"
    return [
        _assistant(f"ar_{uuid}", t1, sid, "Read", tid, {"file_path": path}),
        {"type": "user", "uuid": f"ur_{uuid}", "timestamp": t2, "sessionId": sid,
         "isSidechain": False,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                   "content": "(read)"}]},
         "toolUseResult": {"type": "text", "file": {"filePath": path, "content": content}}}]


def _fetch_pair(tick, uuid, sid, url: str, content: str) -> list[dict]:
    t1, t2, tid = tick(), tick(), f"tf_{uuid}"
    return [
        _assistant(f"af_{uuid}", t1, sid, "WebFetch", tid, {"url": url, "prompt": "read it"}),
        {"type": "user", "uuid": f"uf_{uuid}", "timestamp": t2, "sessionId": sid,
         "isSidechain": False,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                   "content": content}]},
         "toolUseResult": {"content": content}}]


def _user_msg(tick, uuid, sid, text: str) -> list[dict]:
    return [{"type": "user", "uuid": f"um_{uuid}", "timestamp": tick(), "sessionId": sid,
             "isSidechain": False,
             "message": {"role": "user", "content": [{"type": "text", "text": text}]}}]


def generate_logs(lab: str | Path) -> dict[str, Any]:
    lab = Path(lab)
    tree = lab / "tree"
    labels = {}
    for line in (lab / "labels.jsonl").read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            labels[r["hash"]] = r
    tick = _clock()
    logs_dir = lab / "logs"
    logs_dir.mkdir(exist_ok=True)

    # Which session owns a file: an agent-memory file -> that agent; else "interactive".
    def session_of(path: Path) -> str:
        parts = path.parts
        if "agent-memory" in parts:
            return "agent-" + parts[parts.index("agent-memory") + 1]
        return "interactive"

    brief_blocks: list[str] = []
    fetch_blocks: list[str] = []
    sessions: dict[str, list[dict]] = {}
    truth: dict[str, str] = {}
    managed_paths = {str((tree / "deployed" / "CLAUDE.md").resolve())}
    uid = 0

    files = sorted(p for p in tree.rglob("*.md") if ".trash" not in p.parts
                   and ".archive-" not in p.name)
    for path in files:
        rpath = str(path.resolve())
        sid = session_of(path)
        log = sessions.setdefault(sid, [])
        content = path.read_text()
        pre: list[dict] = []
        for b in split_blocks(content):
            lab_row = labels.get(b.hash)
            scenario = lab_row["scenario"] if lab_row else "routine"
            if rpath in managed_paths:
                truth[b.hash] = "managed"
                continue
            root = SCENARIO_ROOT.get(scenario, "composed" if scenario.startswith("canary") else None)
            if root is None:
                continue
            truth[b.hash] = root
            uid += 1
            if root == "authored":
                brief_blocks.append(b.text)
                pre += _read_pair(tick, uid, sid, str((tree / BRIEF_REL).resolve()), "__BRIEF__")
            elif root == "external":
                fetch_blocks.append(b.text)
                pre += _fetch_pair(tick, uid, sid, FETCH_URL, "__FETCH__")
            elif root == "user":
                pre += _user_msg(tick, uid, sid, b.text)
        uid += 1
        log.extend(pre)
        log.extend(_write_pair(tick, uid, sid, rpath, content))

    # Fill the shared authored brief and the fetched page with the real block texts, then
    # substitute them into the placeholder reads/fetches.
    brief_text = "# Kestrel brief (authored source)\n\n" + "\n\n".join(dict.fromkeys(brief_blocks))
    fetch_text = "Kestrel vendor proposal page\n\n" + "\n\n".join(dict.fromkeys(fetch_blocks))
    for sid, log in sessions.items():
        for rec in log:
            tur = rec.get("toolUseResult")
            if isinstance(tur, dict):
                f = tur.get("file")
                if isinstance(f, dict) and f.get("content") == "__BRIEF__":
                    f["content"] = brief_text
                if tur.get("content") == "__FETCH__":
                    tur["content"] = fetch_text
            for b in (rec.get("message", {}).get("content") or []):
                if isinstance(b, dict) and b.get("content") == "__FETCH__":
                    b["content"] = fetch_text
        (logs_dir / f"{sid}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in log) + "\n")

    with open(lab / "provenance_truth.jsonl", "w") as fh:
        for h, kind in truth.items():
            fh.write(json.dumps({"hash": h, "root_kind": kind}) + "\n")

    # Add a provenance block to both configs.
    import yaml

    for name in ("memory.yaml", "memory.oracle.yaml"):
        cfg = yaml.safe_load((lab / name).read_text())
        cfg["provenance"] = {"logs": ["logs"], "index_db": "state/provenance.sqlite", "git": False}
        (lab / name).write_text(yaml.safe_dump(cfg, sort_keys=False))
    return {"sessions": len(sessions), "logs_dir": str(logs_dir),
            "truth_blocks": len(truth), "brief_blocks": len(set(brief_blocks))}


