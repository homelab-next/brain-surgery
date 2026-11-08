"""The review web API and server, over the synthetic lab (with logs for provenance)."""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from brain_surgery.memory.config import load_memory_config
from brain_surgery.memory.index import MemoryIndex
from brain_surgery.memory.synth import generate
from brain_surgery.provenance.synth_logs import generate_logs
from brain_surgery.webapp.api import ApiError, WebContext
from brain_surgery.webapp.server import make_server


@pytest.fixture
def ctx(tmp_path):
    out = tmp_path / "lab"
    generate(out, agents=10, seed=6, canary_rate=1.0)
    generate_logs(out)
    cfg = load_memory_config(out / "memory.oracle.yaml")
    idx = MemoryIndex(cfg.index_db)
    idx.scan(cfg.sources)
    # screen so there are flagged blocks to review
    from brain_surgery.memory.screen import screen
    screen(idx, cfg.screen_spec, cfg.classifiers)
    idx.close()
    return WebContext(cfg)


def test_overview_and_tree(ctx):
    ov = ctx.overview()
    assert ov["files"] > 0 and ov["blocks"] > 0 and ov["provenance"] is True
    tree = ctx.tree()
    assert tree["kind"] == "root" and tree["bytes"] > 0
    # nesting is source -> agent -> file, with risk rolled up
    src = tree["children"][0]
    assert src["kind"] == "source"
    agent = src["children"][0]
    assert agent["kind"] == "agent" and agent["children"][0]["kind"] == "file"
    assert 0 <= tree["risk"] <= 3


def test_groups_are_metadata_with_a_sample(ctx):
    g = ctx.groups()
    assert g["flagged"], "expected flagged blocks after screening"
    one = g["flagged"][0]
    assert set(["hash", "outcome", "sample", "copies", "agents", "block_ids"]) <= set(one)
    assert one["outcome"] in ("quarantine", "review")
    assert isinstance(one["agents"], list)


def test_file_view_carries_text_and_risk(ctx):
    tree = ctx.tree()
    # find a file path
    def first_file(n):
        if n["kind"] == "file":
            return n["path"]
        for c in n.get("children", []):
            p = first_file(c)
            if p:
                return p
    path = first_file(tree)
    f = ctx.file(path)
    assert f["blocks"] and "text" in f["blocks"][0]
    assert all("risk" in b for b in f["blocks"])


def test_quarantine_release_excise_through_the_api(ctx):
    g = ctx.groups()
    quar = next(x for x in g["flagged"] if x["outcome"] == "quarantine")
    res = ctx.do_quarantine(quar["block_ids"], "web test")
    assert res["held"]
    hid = res["held"][0]["hold_id"]
    held = {h["id"] for h in ctx.holds_list("held")["holds"]}
    assert hid in held
    # the hold detail carries the text; release puts it back
    assert ctx.hold(hid)["payload"]["text"]
    ctx.do_release(hid)
    assert hid not in {h["id"] for h in ctx.holds_list("held")["holds"]}
    # excise another and confirm the text is scrubbed in the hold record
    if len(res["held"]) > 1:
        hid2 = res["held"][1]["hold_id"]
        ctx.do_excise(hid2, "confirmed")
        assert ctx.hold(hid2)["payload"]["text"] is None


def test_trace_and_roots(ctx):
    g = ctx.groups()
    bid = g["flagged"][0]["block_ids"][0]
    t = ctx.trace(bid)
    assert t["root"]["kind"] in ("authored", "external", "user", "composed", "managed", "unknown")
    r = ctx.roots("flagged")
    assert "roots" in r


def test_read_only_blocks_mutations(tmp_path):
    out = tmp_path / "lab"
    generate(out, agents=6, seed=1)
    cfg = load_memory_config(out / "memory.oracle.yaml")
    MemoryIndex(cfg.index_db).scan(cfg.sources)
    ro = WebContext(cfg, read_only=True)
    assert ro.overview()["read_only"] is True
    with pytest.raises(ApiError) as ei:
        ro.do_quarantine(["x"], "r")
    assert ei.value.status == 403


def test_http_server_serves_spa_and_api_round_trip(ctx):
    srv = make_server(ctx, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        home = urllib.request.urlopen(base + "/").read().decode()
        assert "brain-surgery" in home and "<title>" in home
        assert urllib.request.urlopen(base + "/app.js").status == 200
        assert urllib.request.urlopen(base + "/style.css").status == 200
        ov = json.loads(urllib.request.urlopen(base + "/api/overview").read())
        assert ov["files"] > 0
        groups = json.loads(urllib.request.urlopen(base + "/api/groups").read())
        ids = groups["flagged"][0]["block_ids"]
        # quarantine over HTTP
        req = urllib.request.Request(base + "/api/quarantine",
                                     data=json.dumps({"block_ids": ids, "reason": "http"}).encode(),
                                     headers={"Content-Type": "application/json"})
        res = json.loads(urllib.request.urlopen(req).read())
        assert res["held"]
        # path traversal and unknown static are refused
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/../pyproject.toml")
        assert ei.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_http_rejects_bad_requests(ctx):
    srv = make_server(ctx, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/api/file?path=/nope")
        assert ei.value.code == 404
        req = urllib.request.Request(base + "/api/quarantine", data=b'{"block_ids":[],"reason":""}',
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_static_js_parses():
    """Catch a syntax error in app.js (the API tests don't execute the browser JS)."""
    import shutil, subprocess
    from importlib import resources
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")
    js = resources.files("brain_surgery.webapp").joinpath("static/app.js")
    with resources.as_file(js) as path:
        r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
