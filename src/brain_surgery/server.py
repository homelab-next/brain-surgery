"""Minimal HTTP front for callers in other languages (e.g. a Go orchestrator).

``POST /v1/route``    {"text": ..., "facts": {...}}            -> the routing decision only
``POST /v1/dispatch`` {"text": ..., "system": ..., "facts": {...}} -> decision + run (or hold)
``GET  /healthz``     -> {"ok": true, "spec": ..., "targets": [...]}

Binds to 127.0.0.1 by default and has no authentication: put it behind your own
proxy/auth before exposing it. Standard library only.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .router import Router

MAX_BODY = 4 * 1024 * 1024


def make_server(router: Router, host: str = "127.0.0.1", port: int = 8790) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            pass

        def _send(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send(200, {"ok": True, "spec": router.spec.id,
                                 "targets": sorted(router.targets)})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in ("/v1/route", "/v1/dispatch"):
                self._send(404, {"error": "not found"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                self._send(413 if n > MAX_BODY else 400, {"error": "body required (<= 4 MiB)"})
                return
            try:
                req = json.loads(self.rfile.read(n))
                text = req["text"]
                if not isinstance(text, str):
                    raise TypeError("text must be a string")
                facts = req.get("facts") or {}
                if not isinstance(facts, dict):
                    raise TypeError("facts must be an object")
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": f"bad request: {exc}"})
                return
            if self.path == "/v1/route":
                self._send(200, router.decide(text, facts=facts).to_dict())
            else:
                res = router.dispatch(text, system=req.get("system"),
                                      max_tokens=req.get("max_tokens"), facts=facts)
                self._send(200, res.to_dict())

    return ThreadingHTTPServer((host, port), Handler)
