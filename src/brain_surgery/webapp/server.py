"""stdlib HTTP server: static SPA at ``/`` and the JSON API at ``/api/*``.

Binds to 127.0.0.1 by default and has no auth of its own. It can read every agent's
memory and, unless ``--read-only``, quarantine/excise it -- put it behind your own
proxy and login (Caddy + Authentik on the reference fleet).
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse
from typing import Any

from .api import ApiError, WebContext

MAX_BODY = 8 * 1024 * 1024
_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def _static(name: str) -> tuple[bytes, str] | None:
    # only serve known files from the package's static dir; no path traversal
    if name in ("", "/"):
        name = "index.html"
    name = name.lstrip("/")
    if "/" in name or ".." in name or name.startswith("."):
        return None
    ext = name[name.rfind("."):] if "." in name else ""
    ctype = _STATIC_TYPES.get(ext)
    if not ctype:
        return None
    res = resources.files("brain_surgery.webapp").joinpath("static", name)
    if not res.is_file():
        return None
    return res.read_bytes(), ctype


def make_server(ctx: WebContext, host: str = "127.0.0.1", port: int = 8795) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path, q = parsed.path, parse_qs(parsed.query)
            if path.startswith("/api/"):
                return self._api_get(path[5:], q)
            served = _static(path)
            if served is None:
                self._send(404, b"not found", "text/plain")
            else:
                self._send(200, served[0], served[1])

        def _api_get(self, name: str, q: dict[str, list[str]]) -> None:
            one = {k: v[0] for k, v in q.items()}
            try:
                if name == "overview":
                    return self._json(200, ctx.overview())
                if name == "tree":
                    return self._json(200, ctx.tree())
                if name == "groups":
                    return self._json(200, ctx.groups())
                if name == "file":
                    return self._json(200, ctx.file(one["path"]))
                if name == "block":
                    return self._json(200, ctx.block(one["id"]))
                if name == "holds":
                    return self._json(200, ctx.holds_list(one.get("status", "held")))
                if name == "hold":
                    return self._json(200, ctx.hold(int(one["id"])))
                if name == "trace":
                    return self._json(200, ctx.trace(one["id"]))
                if name == "roots":
                    return self._json(200, ctx.roots(one.get("scope", "flagged")))
                self._json(404, {"error": "unknown endpoint"})
            except KeyError as exc:
                self._json(400, {"error": f"missing parameter {exc}"})
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            name = urlparse(self.path).path[5:]
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "body too large"})
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid JSON"})
            try:
                if name == "quarantine":
                    return self._json(200, ctx.do_quarantine(
                        req.get("block_ids") or [], req.get("reason") or "",
                        bool(req.get("force", False))))
                if name == "release":
                    return self._json(200, ctx.do_release(int(req["hold_id"]), req.get("note"),
                                                          req.get("replacement")))
                if name == "rescreen":
                    if req.get("hold_id") is not None:
                        return self._json(200, ctx.rescreen_hold(int(req["hold_id"]),
                                                                 req.get("replacement")))
                    return self._json(200, ctx.rescreen(
                        req.get("text") or "", role=req.get("role", "note"),
                        agent=req.get("agent", "*"), section=req.get("section", "")))
                if name == "excise":
                    return self._json(200, ctx.do_excise(int(req["hold_id"]), req.get("note")))
                if name == "dismiss":
                    return self._json(200, ctx.do_dismiss(int(req["hold_id"]), req.get("note")))
                if name == "rescan":
                    return self._json(200, ctx.rescan())
                self._json(404, {"error": "unknown endpoint"})
            except KeyError as exc:
                self._json(400, {"error": f"missing field {exc}"})
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})

        do_HEAD = do_GET

    return ThreadingHTTPServer((host, port), Handler)
