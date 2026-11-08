import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from brain_surgery.classify import StaticClassifier
from brain_surgery.decision_log import DecisionLog
from brain_surgery.holds import HoldQueue
from brain_surgery.router import ConfigError, Router, parse_routes
from brain_surgery.targets import (SERVER_FALLBACK_BETA, AnthropicTarget, OpenAICompatTarget,
                                     build_target)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
PROMPT = "Summarise last night's backup report in two sentences."


def openai_reply(text="ok", finish="stop", model="m"):
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": text},
                                         "finish_reason": finish}]}


class FakeMessages:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.resp


class FakeAnthropic:
    def __init__(self, resp=None, exc=None):
        self.messages = FakeMessages(resp, exc)
        self.beta = SimpleNamespace(messages=self.messages)


def msg(text="done", stop="end_turn", model="claude-opus-5-5", category=None):
    details = SimpleNamespace(category=category, explanation="") if stop == "refusal" else None
    content = [] if stop == "refusal" else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop, stop_details=details, model=model, content=content)


def small_router(size_spec, targets, routes, holds=None):
    clf = {"fast": StaticClassifier("fast", {"multi_step": {"noul": 0.0},
                                             "writes_code": {"noul": 0.0}})}
    return Router(size_spec, clf, targets, parse_routes(routes), holds=holds)


def all_routes(small):
    return {"small": small, "medium": ["b"], "large": ["b"], "unsure": "hold"}


def test_cheapest_available_target_runs(fake_server, size_spec):
    a = fake_server(lambda p, b: (200, openai_reply("from a")))
    b = fake_server(lambda p, b: (200, openai_reply("from b")))
    r = small_router(size_spec, {"a": OpenAICompatTarget("a", a.url + "/v1", "small"),
                                 "b": OpenAICompatTarget("b", b.url + "/v1", "big")},
                     all_routes(["a", "b"]))
    res = r.dispatch(PROMPT, system="Be brief.")
    assert res.status == "completed" and res.completion.text == "from a"
    assert a.requests[0][0] == "/v1/chat/completions"
    sent = a.requests[0][1]
    assert sent["model"] == "small" and sent["messages"][0] == {"role": "system", "content": "Be brief."}
    assert b.requests == []


def test_unavailable_target_moves_to_next(fake_server, size_spec):
    b = fake_server(lambda p, body: (200, openai_reply("from b")))
    targets = {"a": OpenAICompatTarget("a", "http://127.0.0.1:9/v1", "small", timeout_s=0.5),
               "b": OpenAICompatTarget("b", b.url + "/v1", "big")}
    res = small_router(size_spec, targets, all_routes(["a", "b"])).dispatch(PROMPT)
    assert res.status == "completed" and res.completion.target == "b"
    assert [x["result"] for x in res.attempts] == ["unavailable", "completed"]


def test_server_error_moves_to_next(fake_server, size_spec):
    a = fake_server(lambda p, body: (503, {"error": "busy"}))
    b = fake_server(lambda p, body: (200, openai_reply("from b")))
    targets = {"a": OpenAICompatTarget("a", a.url + "/v1", "small"),
               "b": OpenAICompatTarget("b", b.url + "/v1", "big")}
    res = small_router(size_spec, targets, all_routes(["a", "b"])).dispatch(PROMPT)
    assert res.completion.target == "b"


def test_refusal_holds_and_does_not_try_other_targets(fake_server, size_spec, tmp_path):
    a = fake_server(lambda p, body: (200, openai_reply("", finish="content_filter")))
    b = fake_server(lambda p, body: (200, openai_reply("from b")))
    holds = HoldQueue(tmp_path / "h.sqlite")
    targets = {"a": OpenAICompatTarget("a", a.url + "/v1", "small"),
               "b": OpenAICompatTarget("b", b.url + "/v1", "big")}
    res = small_router(size_spec, targets, all_routes(["a", "b"]), holds).dispatch(PROMPT)
    assert res.status == "held" and "declined by a" in res.reason
    assert b.requests == []
    h = holds.get(res.hold_id)
    assert h.kind == "prompt" and h.payload["text"] == PROMPT and h.outcome == "small"


def test_anthropic_refusal_holds_with_category(size_spec, tmp_path):
    fake = FakeAnthropic(msg(stop="refusal", category="example_category"))
    t = AnthropicTarget("cloud", "claude-opus-5-5", client=fake)
    other = FakeAnthropic(msg("never"))
    t2 = AnthropicTarget("cloud2", "claude-opus-5-5", client=other)
    holds = HoldQueue(tmp_path / "h.sqlite")
    res = small_router(size_spec, {"cloud": t, "b": t2}, all_routes(["cloud", "b"]),
                       holds).dispatch(PROMPT)
    assert res.status == "held" and "example_category" in res.reason
    assert other.messages.calls == []
    assert res.attempts == [{"target": "cloud", "result": "refused", "category": "example_category"}]


def test_anthropic_server_fallback_defaults():
    fake = FakeAnthropic(msg("done", model="claude-opus-4-8"))
    t = AnthropicTarget("cloud", "claude-opus-5-5", client=fake)
    c = t.complete(PROMPT, system="Be brief.")
    kw = fake.messages.calls[0]
    assert kw["betas"] == [SERVER_FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["system"] == "Be brief." and kw["max_tokens"] == 16000
    assert c.text == "done" and c.served_by == "claude-opus-4-8"   # served after a fallback

    plain = FakeAnthropic(msg("done", model="claude-haiku-5-5"))
    AnthropicTarget("h", "claude-haiku-5-5", client=plain).complete(PROMPT)
    assert "fallbacks" not in plain.messages.calls[0] and "betas" not in plain.messages.calls[0]

    off = FakeAnthropic(msg("done"))
    AnthropicTarget("o", "claude-opus-5-5", server_fallback="off", client=off).complete(PROMPT)
    assert "fallbacks" not in off.messages.calls[0]


def test_anthropic_connection_error_is_unavailable(size_spec, fake_server):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    down = AnthropicTarget("cloud", "claude-opus-5-5",
                           client=FakeAnthropic(exc=anthropic.APIConnectionError(request=req)))
    b = fake_server(lambda p, body: (200, openai_reply("local answer")))
    targets = {"cloud": down, "b": OpenAICompatTarget("b", b.url + "/v1", "big")}
    res = small_router(size_spec, targets, all_routes(["cloud", "b"])).dispatch(PROMPT)
    assert res.completion.target == "b"
    assert res.attempts[0] == {"target": "cloud", "result": "unavailable",
                               "detail": "cloud: connection error"}


def test_hold_route_and_exhausted_route(size_spec, tmp_path):
    holds = HoldQueue(tmp_path / "h.sqlite")
    t = OpenAICompatTarget("a", "http://127.0.0.1:9/v1", "small", timeout_s=0.5)
    r = small_router(size_spec, {"a": t, "b": t}, {"small": "hold", "medium": ["a"],
                                                   "large": ["a"], "unsure": "hold"}, holds)
    res = r.dispatch(PROMPT)
    assert res.status == "held" and "holds for review" in res.reason and res.attempts == []
    r2 = small_router(size_spec, {"a": t, "b": t}, all_routes(["a"]), holds)
    res2 = r2.dispatch(PROMPT)
    assert res2.status == "held" and "no target" in res2.reason
    assert len(holds.list()) == 2


def test_config_validation(size_spec):
    t_local = OpenAICompatTarget("a", "http://x/v1", "m")
    t_cloud = AnthropicTarget("c", "claude-opus-5-5", client=FakeAnthropic(msg()))
    with pytest.raises(ConfigError, match="has no route"):
        small_router(size_spec, {"a": t_local}, {"small": ["a"]})
    with pytest.raises(ConfigError, match="unknown target"):
        small_router(size_spec, {"a": t_local}, all_routes(["zzz"]))
    routes = all_routes(["a"])
    routes["medium"] = {"targets": ["a", "c"], "data_tier": "local-only"}
    with pytest.raises(ConfigError, match="local-only but lists 'c'"):
        small_router(size_spec, {"a": t_local, "b": t_local, "c": t_cloud}, routes)
    with pytest.raises(ValueError, match="cloud"):
        build_target("c", {"kind": "anthropic", "model": "claude-opus-5-5", "data_tier": "local-only"})


def test_from_config_example_with_classifiers_down(tmp_path):
    cfg = (EXAMPLES / "routing.yaml").read_text()
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "route.prompt_size.yaml").write_text(
        (EXAMPLES / "specs" / "route.prompt_size.yaml").read_text())
    (tmp_path / "routing.yaml").write_text(cfg.replace("127.0.0.1:8899", "127.0.0.1:9")
                                              .replace("127.0.0.1:8080", "127.0.0.1:9"))
    r = Router.from_config(tmp_path / "routing.yaml")
    rd = r.decide("Please implement a function that parses dates, then write tests for it.")
    # rule tier answers writes_code; the down tiers leave the rest unknown -> escalate
    assert rd.route.name == "unsure" and rd.decision.answers["writes_code"].state == "high"
    rd2 = r.decide("x" * 30000)
    assert rd2.route.name == "large" and rd2.decision.tiers_asked == []
    logs = list((tmp_path / "state" / "decisions").glob("*.jsonl"))
    rows = [json.loads(l) for l in logs[0].read_text().splitlines()]
    assert sum(1 for x in rows if x["kind"] == "decision") == 2


def test_http_server(size_spec, fake_server):
    import threading
    from brain_surgery.server import make_server

    a = fake_server(lambda p, b: (200, openai_reply("hi")))
    r = small_router(size_spec, {"a": OpenAICompatTarget("a", a.url + "/v1", "m"),
                                 "b": OpenAICompatTarget("b", a.url + "/v1", "m")},
                     all_routes(["a"]))
    srv = make_server(r, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        health = json.loads(urllib.request.urlopen(base + "/healthz").read())
        assert health["ok"] and health["spec"] == "test.size"
        for path in ("/v1/route", "/v1/dispatch"):
            req = urllib.request.Request(base + path, data=json.dumps({"text": PROMPT}).encode(),
                                         headers={"Content-Type": "application/json"})
            out = json.loads(urllib.request.urlopen(req).read())
            assert out["route"] == "small"
        assert out["status"] == "completed" and out["completion"]["text"] == "hi"
        bad = urllib.request.Request(base + "/v1/route", data=b'{"nope": 1}')
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(bad)
        assert ei.value.code == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_decision_log_never_raises(size_spec, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    r = small_router(size_spec, {"a": OpenAICompatTarget("a", "http://x/v1", "m"),
                                 "b": OpenAICompatTarget("b", "http://x/v1", "m")},
                     all_routes(["a"]))
    r.log = DecisionLog(blocker / "sub")
    assert r.decide(PROMPT).route.name == "small"
