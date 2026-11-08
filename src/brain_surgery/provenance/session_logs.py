"""Read Claude Code session logs (`*.jsonl`) into normalized actions.

Each session is a list of records. We care about two kinds of thing an agent did:

* **writes** -- a Write or Edit tool call. The log carries the file path and, in
  ``toolUseResult``, the file's full content *after* the change. A write is the evidence
  of who put a block into a memory file and when.
* **inputs** -- everything the session read *before* a write and could have copied from:
  Read results, WebFetch / WebSearch results, the text of tool results, the CLAUDE.md /
  skill files loaded as an ``instructions`` attachment, and user message text.

Only the fields we need are read; unknown record types and keys are ignored, so a newer
log format degrades rather than crashes. Nothing here prints log content.

Record shapes (observed): ``user`` / ``assistant`` carry ``uuid``, ``timestamp``,
``sessionId``, ``cwd``, ``gitBranch``, ``isSidechain`` and ``message`` (content is a
string or a list of blocks). ``assistant`` blocks include ``tool_use`` (``name``,
``input``, ``id``); the matching ``user`` record carries ``toolUseResult`` and a
``tool_result`` block. ``attachment`` records carry an ``attachment`` object; the
``instructions`` type lists ``files: [{path, content}]``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}
# tool name -> how to describe where its result came from
FETCH_TOOLS = {"WebFetch": "web", "WebSearch": "web"}
READ_TOOLS = {"Read"}


@dataclass
class WriteAction:
    path: str                 # absolute file path written
    text: str                 # full file content after the write (may be "")
    session_id: str
    uuid: str
    ts: str                   # ISO timestamp
    kind: str                 # create | edit
    cwd: str | None = None
    git_branch: str | None = None
    is_sidechain: bool = False
    agent: str | None = None  # filled by the caller's agent map


@dataclass
class InputAction:
    """Something the session read and could have copied from."""
    text: str
    origin: str               # read | web | tool | instruction | user
    ref: str | None           # file path, URL, tool name, ...
    session_id: str
    ts: str
    uuid: str
    is_sidechain: bool = False


@dataclass
class Session:
    session_id: str
    path: str
    writes: list[WriteAction] = field(default_factory=list)
    inputs: list[InputAction] = field(default_factory=list)


def _blocks(message: Any) -> list[dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _result_text(tur: Any, block: dict[str, Any]) -> str:
    """Text of a tool result: prefer structured toolUseResult, else the tool_result block."""
    if isinstance(tur, dict):
        f = tur.get("file")
        if isinstance(f, dict) and isinstance(f.get("content"), str):
            return f["content"]
        if isinstance(tur.get("content"), str):
            return tur["content"]
        if isinstance(tur.get("stdout"), str):
            return tur["stdout"]
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def parse_session(path: str | Path) -> Session:
    path = Path(path)
    sid = path.stem
    sess = Session(session_id=sid, path=str(path))
    tool_names: dict[str, str] = {}      # tool_use id -> tool name (assistant side)
    tool_targets: dict[str, dict[str, Any]] = {}   # tool_use id -> its input

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rtype = r.get("type")
            if rtype == "assistant":
                for b in _blocks(r.get("message")):
                    if b.get("type") == "tool_use" and b.get("id"):
                        tool_names[b["id"]] = b.get("name") or ""
                        tool_targets[b["id"]] = b.get("input") or {}
            elif rtype == "user":
                _parse_user(r, sess, tool_names, tool_targets)
            elif rtype == "attachment":
                _parse_attachment(r, sess)
    return sess


def _parse_user(r: dict[str, Any], sess: Session, tool_names: dict[str, str],
                tool_targets: dict[str, dict[str, Any]]) -> None:
    meta = dict(session_id=sess.session_id, ts=r.get("timestamp") or "", uuid=r.get("uuid") or "",
                is_sidechain=bool(r.get("isSidechain")))
    tur = r.get("toolUseResult")
    for b in _blocks(r.get("message")):
        btype = b.get("type")
        if btype == "tool_result":
            tid = b.get("tool_use_id")
            name = tool_names.get(tid, "")
            if name in WRITE_TOOLS:
                inp = tool_targets.get(tid, {})
                path = inp.get("file_path") or inp.get("notebook_path")
                if path:
                    text = _written_text(name, inp, tur)
                    sess.writes.append(WriteAction(
                        path=path, text=text, kind="create" if name == "Write" else "edit",
                        cwd=r.get("cwd"), git_branch=r.get("gitBranch"),
                        session_id=sess.session_id, ts=r.get("timestamp") or "",
                        uuid=r.get("uuid") or "", is_sidechain=bool(r.get("isSidechain"))))
            elif name in READ_TOOLS:
                ref = tool_targets.get(tid, {}).get("file_path")
                sess.inputs.append(InputAction(text=_result_text(tur, b), origin="read",
                                               ref=ref, **meta))
            elif name in FETCH_TOOLS:
                ref = tool_targets.get(tid, {}).get("url") or tool_targets.get(tid, {}).get("query")
                sess.inputs.append(InputAction(text=_result_text(tur, b),
                                               origin=FETCH_TOOLS[name], ref=ref, **meta))
            elif name:
                sess.inputs.append(InputAction(text=_result_text(tur, b), origin="tool",
                                               ref=name, **meta))
        elif btype == "text" and not r.get("isMeta"):
            txt = b.get("text") or ""
            if txt.strip():
                sess.inputs.append(InputAction(text=txt, origin="user", ref=None, **meta))


def _written_text(name: str, inp: dict[str, Any], tur: Any) -> str:
    if isinstance(tur, dict):
        if isinstance(tur.get("content"), str):     # Write result carries the new content
            return tur["content"]
        if isinstance(tur.get("newString"), str) and name == "Edit":
            # fall back to the post-edit file if present, else the inserted text
            pass
    if name == "Write":
        return inp.get("content") or ""
    if name == "Edit":
        return inp.get("new_string") or ""
    return inp.get("new_source") or ""


def _parse_attachment(r: dict[str, Any], sess: Session) -> None:
    a = r.get("attachment")
    if not isinstance(a, dict):
        return
    meta = dict(session_id=sess.session_id, ts=r.get("timestamp") or "", uuid=r.get("uuid") or "",
                is_sidechain=bool(r.get("isSidechain")))
    if a.get("type") == "instructions":
        for f in a.get("files") or []:
            if isinstance(f, dict) and isinstance(f.get("content"), str):
                sess.inputs.append(InputAction(text=f["content"], origin="instruction",
                                               ref=f.get("path"), **meta))


def iter_sessions(roots: list[str | Path]) -> Iterator[Session]:
    """Every ``*.jsonl`` under each root (recursively), oldest file first."""
    seen: set[str] = set()
    for root in roots:
        root = Path(root).expanduser()
        if root.is_file() and root.suffix == ".jsonl":
            files = [root]
        elif root.is_dir():
            files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        else:
            files = []
        for f in files:
            key = str(f.resolve())
            if key in seen:
                continue
            seen.add(key)
            yield parse_session(f)
