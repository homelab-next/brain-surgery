"""Shared fixtures: tiny in-process fake servers, and a small benign spec."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest

from brain_surgery.spec import parse_spec


class FakeServer:
    """Run ``handler(path, body) -> (status, obj)`` on a random local port."""

    def __init__(self, handler: Callable[[str, Any], tuple[int, Any]]) -> None:
        self.requests: list[tuple[str, Any]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = raw
                outer.requests.append((self.path, body))
                status, obj = handler(self.path, body)
                out = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake_server():
    servers: list[FakeServer] = []

    def make(handler):
        s = FakeServer(handler)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


SIZE_SPEC = {
    "id": "test.size",
    "version": 1,
    "error_direction": "noise_gate",
    "on_error": "escalate",
    "tiers": ["fast", "slow"],
    "state_card": "REQUEST: {text}",
    "questions": [
        {"id": "multi_step", "type": "noul", "text": "Does this need several steps?"},
        {"id": "writes_code", "type": "noul", "text": "Does this ask for code to be written?"},
    ],
    "thresholds": {
        "fast": {"model": "f", "multi_step": {"hi": 0.9, "lo": 0.1},
                 "writes_code": {"hi": 0.9, "lo": 0.1}},
        "slow": {"model": "s", "multi_step": {"hi": 0.7, "lo": 0.3},
                 "writes_code": {"hi": 0.7, "lo": 0.3}},
    },
    "outcomes": [
        {"id": "small"}, {"id": "medium"}, {"id": "large"},
        {"id": "unsure", "escalate_to": "large"},
    ],
    "decision_table": [
        {"when": {"prompt_chars": ">1000"}, "outcome": "large"},
        {"when": {"multi_step": ">=hi", "writes_code": ">=hi"}, "outcome": "large"},
        {"when": {"multi_step": ">=hi"}, "outcome": "medium"},
        {"when": {"multi_step": "<=lo", "writes_code": "<=lo"}, "outcome": "small"},
        {"default": "unsure"},
    ],
}


@pytest.fixture
def size_spec():
    return parse_spec(json.loads(json.dumps(SIZE_SPEC)))
