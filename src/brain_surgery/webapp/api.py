"""The JSON API behind the review page.

``WebContext`` opens the memory index, the hold queue, the quarantine engine and (if
provenance is configured) the tracer, and exposes one method per endpoint, each returning
a plain dict. This is the single place that shows memory text: it is the operator's
review surface, so the detail views (file, block, hold) include content. Put the server
behind your own auth; it can see every agent's memory and, unless ``read_only``, change it.
"""
from __future__ import annotations

from typing import Any

from ..memory.config import MemoryConfig
from ..memory.index import MemoryIndex
from ..memory.quarantine import Quarantine
from ..holds import HoldQueue

RISK_NAME = {0: "none", 1: "unsure", 2: "review", 3: "quarantine"}
OUTCOME_RISK = {"quarantine": 3, "review": 2, "unsure": 1, "keep": 0}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message
        super().__init__(message)


class WebContext:
    def __init__(self, cfg: MemoryConfig, *, read_only: bool = False) -> None:
        self.cfg = cfg
        self.read_only = read_only
        self.index = MemoryIndex(cfg.index_db)
        self.holds = HoldQueue(cfg.holds_db)
        self.quarantine_engine = Quarantine(self.index, self.holds)
        self._tracer = None
        self._tracer_tried = False

    # -- provenance (lazy) ------------------------------------------------------------
    def tracer(self):
        if self._tracer_tried:
            return self._tracer
        self._tracer_tried = True
        if not self.cfg.prov_index_db:
            return None
        from ..provenance.index import ProvenanceIndex
        from ..provenance.session_logs import iter_sessions
        from ..provenance.trace import Tracer

        prov = ProvenanceIndex(self.cfg.prov_index_db)
        if prov.is_empty() and self.cfg.prov_logs:
            prov.build(iter_sessions(self.cfg.prov_logs))
        self._tracer = Tracer(prov, agent_of=self.index.agent_for_path,
                              managed_of=self.index.managed_for_path, git=self.cfg.prov_git)
        return self._tracer

    def _guard(self) -> None:
        if self.read_only:
            raise ApiError(403, "this review server is read-only")

    # -- read endpoints ---------------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        tree = self.index.tree()
        agents = {r["agent"] for r in tree}
        sources = {r["source_id"] for r in tree}
        blocks = sum(r["blocks"] for r in tree)
        chars = sum(r["chars"] for r in tree)
        holds = {s: len(self.holds.list(status=s, kind="memory"))
                 for s in ("held", "released", "excised", "dismissed")}
        return {"files": len(tree), "agents": len(agents), "sources": len(sources),
                "blocks": blocks, "chars": chars,
                "screen": self.index.screen_summary(),
                "regrowth": len(self.index.regrowth()), "holds": holds,
                "provenance": bool(self.cfg.prov_index_db), "read_only": self.read_only}

    def tree(self) -> dict[str, Any]:
        """Nested source -> agent -> file for the treemap; bytes size it, risk colors it."""
        risks = self.index.file_risks()
        root: dict[str, Any] = {"name": "memory", "kind": "root", "children": {}}
        for r in self.index.tree():
            src = root["children"].setdefault(
                r["source_id"], {"name": r["source_id"], "kind": "source", "children": {}})
            agent = src["children"].setdefault(
                r["agent"], {"name": r["agent"], "kind": "agent", "children": {}})
            agent["children"][r["path"]] = {
                "name": r["path"].rsplit("/", 1)[-1], "path": r["path"], "kind": "file",
                "bytes": r["chars"], "blocks": r["blocks"], "loaded": r["loaded"],
                "managed_by": r["managed_by"], "flagged": r["flagged"],
                "risk": risks.get(r["path"], 0)}
        return _finalize(root)

    def groups(self) -> dict[str, Any]:
        """The top band: risk groups, cross-agent duplicates, and regrowth."""
        flagged = []
        for outcome in ("quarantine", "review"):
            rows = self.index.blocks(outcome=outcome, with_text=True)
            by_hash: dict[str, dict[str, Any]] = {}
            for b in rows:
                g = by_hash.setdefault(b["hash"], {"hash": b["hash"], "outcome": outcome,
                                                   "sample": _one_line(b["text"]), "copies": 0,
                                                   "agents": set(), "chars": b["chars"],
                                                   "block_ids": [], "exposure": b["exposure"],
                                                   "section": b["section"]})
                g["copies"] += 1
                g["agents"].add(b["agent"])
                g["block_ids"].append(b["id"])
            for g in by_hash.values():
                g["agents"] = sorted(a for a in g["agents"] if a)
            flagged += sorted(by_hash.values(),
                              key=lambda g: (-OUTCOME_RISK[outcome], -len(g["agents"]), -g["chars"]))
        dupes = self.index.duplicates()
        regrowth = self.index.regrowth()
        return {"flagged": flagged, "duplicates": dupes, "regrowth": regrowth}

    def file(self, path: str) -> dict[str, Any]:
        rows = self.index.blocks(file_path=path, with_text=True)
        if not rows:
            raise ApiError(404, "no such file in the index")
        for r in rows:
            r["risk"] = OUTCOME_RISK.get(r.get("screen_outcome") or "", 0)
            if self.index.watched(r["hash"], "excised"):
                r["risk"] = 3
                r["regrew"] = True
        meta = rows[0]
        return {"path": path, "agent": meta["agent"], "source_id": meta["source_id"],
                "role": meta["role"], "managed_by": meta["managed_by"], "blocks": rows}

    def block(self, block_id: str) -> dict[str, Any]:
        rows = self.index.blocks(block_ids=[block_id], with_text=True)
        if not rows:
            raise ApiError(404, "no such block")
        return rows[0]

    def holds_list(self, status: str = "held") -> dict[str, Any]:
        items = []
        for h in self.holds.list(status=None if status == "all" else status, kind="memory"):
            p = h.payload
            items.append({"id": h.id, "status": h.status, "reason": h.reason,
                          "created_at": h.created_at, "resolved_at": h.resolved_at,
                          "agent": p.get("agent"), "path": p.get("path"),
                          "section": p.get("section"), "chars": p.get("chars"),
                          "has_text": bool(p.get("text")), "sample": _one_line(p.get("text") or "")})
        return {"status": status, "holds": items}

    def hold(self, hold_id: int) -> dict[str, Any]:
        h = self.holds.get(hold_id)
        if h is None:
            raise ApiError(404, "no such hold")
        return {"id": h.id, "status": h.status, "reason": h.reason, "kind": h.kind,
                "created_at": h.created_at, "resolved_at": h.resolved_at, "note": h.note,
                "payload": h.payload}

    def trace(self, block_id: str) -> dict[str, Any]:
        tr = self.tracer()
        if tr is None:
            raise ApiError(400, "no provenance configured (add a provenance block to memory.yaml)")
        b = self.block(block_id)
        result = tr.trace(b["hash"], b["text"], b["path"]).to_dict()
        return result

    def roots(self, scope: str = "flagged") -> dict[str, Any]:
        from collections import defaultdict

        tr = self.tracer()
        if tr is None:
            raise ApiError(400, "no provenance configured")
        if scope == "all":
            blocks = self.index.blocks(with_text=True)
        else:
            blocks = [b for o in ("review", "quarantine")
                      for b in self.index.blocks(outcome=o, with_text=True)]
        by_hash: dict[str, dict[str, Any]] = {}
        for b in blocks:
            h = by_hash.setdefault(b["hash"], {"agents": set(), "rep": b})
            h["agents"].add(b["agent"])
        groups: dict[tuple, dict[str, Any]] = defaultdict(
            lambda: {"blocks": 0, "agents": set(), "action": "", "block_ids": []})
        for h, info in by_hash.items():
            b = info["rep"]
            t = tr.trace(h, b["text"], b["path"])
            g = groups[(t.root_kind, t.root_where)]
            g["blocks"] += 1
            g["agents"] |= info["agents"]
            g["action"] = t.action
            g["block_ids"].append(b["id"])
        rows = [{"root_kind": k, "root_where": w, "blocks": v["blocks"],
                 "agents": sorted(a for a in v["agents"] if a), "action": v["action"],
                 "block_ids": v["block_ids"]}
                for (k, w), v in sorted(groups.items(), key=lambda kv: -kv[1]["blocks"])]
        return {"scope": scope, "roots": rows}

    # -- mutations --------------------------------------------------------------------
    def do_quarantine(self, block_ids: list[str], reason: str, force: bool = False) -> dict[str, Any]:
        self._guard()
        if not block_ids:
            raise ApiError(400, "no block_ids")
        if not reason or not reason.strip():
            raise ApiError(400, "a reason is required")
        return self.quarantine_engine.quarantine(block_ids, reason, force=force).to_dict()

    def do_release(self, hold_id: int, note: str | None = None,
                   replacement: str | None = None) -> dict[str, Any]:
        self._guard()
        from ..memory.quarantine import QuarantineError
        try:
            return self.quarantine_engine.release(hold_id, note, replacement=replacement)
        except QuarantineError as exc:
            raise ApiError(409, str(exc)) from exc

    def rescreen(self, text: str, *, role: str = "note", agent: str = "*",
                 section: str = "") -> dict[str, Any]:
        """Preview what the screen would say about candidate text (a held block, or a
        reworded version of it) -- recorded nowhere, changes nothing."""
        if self.cfg.screen_spec is None:
            raise ApiError(400, "no screen configured (add a screen.spec to memory.yaml)")
        from ..memory.screen import screen_one
        return screen_one(self.cfg.screen_spec, self.cfg.classifiers, text,
                          role=role, agent=agent, section=section)

    def rescreen_hold(self, hold_id: int, replacement: str | None = None) -> dict[str, Any]:
        h = self.holds.get(hold_id)
        if h is None or h.kind != "memory":
            raise ApiError(404, "no such memory hold")
        p = h.payload
        text = replacement if (replacement and replacement.strip()) else p.get("text")
        if not text:
            raise ApiError(400, "nothing to screen (the hold's text was excised)")
        return self.rescreen(text, role=p.get("role") or "note", agent=p.get("agent") or "*",
                             section=p.get("section") or "")

    def do_excise(self, hold_id: int, note: str | None = None) -> dict[str, Any]:
        self._guard()
        from ..memory.quarantine import QuarantineError
        try:
            return self.quarantine_engine.excise(hold_id, note)
        except QuarantineError as exc:
            raise ApiError(409, str(exc)) from exc

    def do_dismiss(self, hold_id: int, note: str | None = None) -> dict[str, Any]:
        self._guard()
        h = self.holds.get(hold_id)
        if h is not None and h.kind == "memory":
            raise ApiError(409, "a memory hold is resolved by release (restore / reword) or "
                                "excise, not dismiss")
        try:
            h = self.holds.resolve(hold_id, "dismissed", note)
        except (KeyError, ValueError) as exc:
            raise ApiError(409, str(exc)) from exc
        return {"hold_id": h.id, "status": h.status}

    def rescan(self) -> dict[str, Any]:
        self._guard()
        return self.index.scan(self.cfg.sources).to_dict()

    # -- scoping ----------------------------------------------------------------------
    def scopes(self) -> dict[str, Any]:
        return {"scopes": [{"name": n, "label": s.get("label", n), "models": s.get("models", [])}
                           for n, s in self.cfg.scopes.items()],
                "proposals_configured": bool(self.cfg.scope_proposals)}

    def scoped_list(self, scope: str | None = None) -> dict[str, Any]:
        return {"scoped": self.index.list_scoped(scope=scope)}

    def scope_proposals(self) -> dict[str, Any]:
        from ..memory.scoping import propose_scopes
        return {"proposals": propose_scopes(self.index, self.cfg.scope_proposals)}

    def do_scope(self, block_ids: list[str], scope: str, note: str | None = None) -> dict[str, Any]:
        self._guard()
        from ..memory.scoping import ScopeError, Scoper
        if not block_ids:
            raise ApiError(400, "no block_ids")
        try:
            return Scoper(self.index, self.cfg.scopes).scope(block_ids, scope, note).to_dict()
        except ScopeError as exc:
            raise ApiError(400, str(exc)) from exc

    def do_unscope(self, scoped_id: int, note: str | None = None) -> dict[str, Any]:
        self._guard()
        from ..memory.scoping import ScopeError, Scoper
        try:
            return Scoper(self.index, self.cfg.scopes).unscope(scoped_id, note)
        except ScopeError as exc:
            raise ApiError(404, str(exc)) from exc


def _one_line(text: str, limit: int = 140) -> str:
    line = " ".join(text.split())
    return line[:limit] + ("…" if len(line) > limit else "")


def _finalize(node: dict[str, Any]) -> dict[str, Any]:
    """Turn the dict-of-children into a list, and roll bytes/risk up the tree."""
    children = node.get("children")
    if children is None:
        return node
    kids = [_finalize(c) for c in children.values()]
    node["children"] = kids
    node["bytes"] = sum(k.get("bytes", 0) for k in kids)
    node["blocks"] = sum(k.get("blocks", 0) for k in kids)
    node["risk"] = max((k.get("risk", 0) for k in kids), default=0)
    node["flagged"] = sum(k.get("flagged", 0) for k in kids)
    return node
