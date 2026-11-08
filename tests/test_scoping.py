"""Scoping: split-file move/restore, the loader's model match, and the review API."""
import json
import threading
import urllib.request
from pathlib import Path

import pytest
import yaml

from brain_surgery.memory.config import MemoryConfigError, load_memory_config
from brain_surgery.memory.index import MemoryIndex
from brain_surgery.memory.scoping import ScopeError, Scoper, scoped_for, scoped_sibling
from brain_surgery.memory.synth import generate


def open_lab(tmp_path, **cfg_over):
    out = tmp_path / "lab"
    generate(out, agents=10, seed=12, canary_rate=1.0)
    if cfg_over:
        cfg = yaml.safe_load((out / "memory.oracle.yaml").read_text())
        cfg.update(cfg_over)
        (out / "memory.oracle.yaml").write_text(yaml.safe_dump(cfg))
    c = load_memory_config(out / "memory.oracle.yaml")
    idx = MemoryIndex(c.index_db)
    idx.scan(c.sources)
    return out, c, idx


def a_block(idx, contains):
    rows = idx.blocks(contains=contains, with_text=True)
    assert rows, f"no block containing {contains!r}"
    return rows[0]


def test_scope_moves_block_to_sibling_and_out_of_live_file(tmp_path):
    out, c, idx = open_lab(tmp_path)
    b = a_block(idx, "pricing sheet")
    path = Path(b["path"])
    assert "pricing sheet" in path.read_text()
    # this exact block may be copied across several agents; scope every copy by hash
    ids = [x["id"] for x in idx.blocks(hash=b["hash"])]
    res = Scoper(idx, c.scopes).scope(ids, "restricted-cleared", "cyber")
    assert len(res.scoped) == len(ids) and res.skipped == []
    # gone from this live file...
    assert "pricing sheet" not in path.read_text()
    # ...now in the sibling, which the scan excludes (so it is not live general memory)
    sib = scoped_sibling(str(path), "restricted-cleared")
    assert sib.exists() and "pricing sheet" in sib.read_text() and "scope: restricted-cleared" in sib.read_text()
    idx.scan(c.sources)
    assert not any(x["hash"] == b["hash"] for x in idx.blocks())   # this block excluded everywhere
    assert len(idx.list_scoped()) == len(ids)


def test_unscope_restores_to_original(tmp_path):
    out, c, idx = open_lab(tmp_path)
    b = a_block(idx, "pricing sheet")
    path = Path(b["path"])
    sid = Scoper(idx, c.scopes).scope([b["id"]], "restricted-cleared").scoped[0]["scoped_id"]
    sib = scoped_sibling(str(path), "restricted-cleared")
    out_ = Scoper(idx, c.scopes).unscope(sid)
    assert "pricing sheet" in path.read_text()
    assert not sib.exists()                                   # last block out -> file removed
    assert idx.list_scoped() == []


def test_loader_matches_model_to_scope(tmp_path):
    out, c, idx = open_lab(tmp_path)
    b = a_block(idx, "pricing sheet")
    Scoper(idx, c.scopes).scope([b["id"]], "restricted-cleared")
    # a general model matches no scope -> loads nothing
    assert scoped_for(idx, c, "small-local-model")["files"] == []
    # a model the scope permits (exact and glob) -> loads the sibling
    assert len(scoped_for(idx, c, "big-local-model")["files"]) == 1
    glob = scoped_for(idx, c, "cleared-opus", with_text=True)
    assert len(glob["files"]) == 1 and "pricing sheet" in glob["files"][0]["text"]


def test_scope_refuses_unknown_scope_and_managed(tmp_path):
    out, c, idx = open_lab(tmp_path)
    b = a_block(idx, "pricing sheet")
    with pytest.raises(ScopeError):
        Scoper(idx, c.scopes).scope([b["id"]], "nope")
    managed = [x for x in idx.blocks(with_text=True) if x["managed_by"]]
    if managed:
        res = Scoper(idx, c.scopes).scope([managed[0]["id"]], "restricted-cleared")
        assert res.scoped == [] and "managed" in res.skipped[0]["reason"]


def test_config_rejects_bad_scopes(tmp_path):
    out = tmp_path / "lab"
    generate(out, agents=4, seed=1)
    cfg = yaml.safe_load((out / "memory.oracle.yaml").read_text())
    cfg["scopes"] = {"bad": {"label": "x"}}            # no models
    (out / "bad.yaml").write_text(yaml.safe_dump(cfg))
    with pytest.raises(MemoryConfigError, match="models"):
        load_memory_config(out / "bad.yaml")
    cfg["scopes"] = {"ok": {"models": ["m"]}}
    cfg["scope_proposals"] = [{"outcome": "review", "scope": "ghost"}]
    (out / "bad2.yaml").write_text(yaml.safe_dump(cfg))
    with pytest.raises(MemoryConfigError, match="unknown scope"):
        load_memory_config(out / "bad2.yaml")


def test_propose_scopes_from_screen(tmp_path):
    from brain_surgery.memory.screen import screen
    out, c, idx = open_lab(tmp_path)
    screen(idx, c.screen_spec, c.classifiers)
    from brain_surgery.memory.scoping import propose_scopes
    props = propose_scopes(idx, c.scope_proposals)
    # the rule is outcome=review -> restricted-cleared; there should be review blocks
    assert all(p["suggested_scope"] == "restricted-cleared" for p in props)


def test_web_scope_round_trip(tmp_path):
    from brain_surgery.webapp.api import WebContext
    from brain_surgery.webapp.server import make_server
    out, c, idx = open_lab(tmp_path)
    idx.close()
    ctx = WebContext(c)
    assert [s["name"] for s in ctx.scopes()["scopes"]] == ["restricted-cleared"]
    srv = make_server(ctx, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        bid = next(b["id"] for b in ctx.index.blocks(contains="Kestrel"))
        req = urllib.request.Request(base + "/api/scope",
                                     data=json.dumps({"block_ids": [bid], "scope": "restricted-cleared"}).encode(),
                                     headers={"Content-Type": "application/json"})
        res = json.loads(urllib.request.urlopen(req).read())
        assert res["scoped"]
        sid = res["scoped"][0]["scoped_id"]
        listed = json.loads(urllib.request.urlopen(base + "/api/scoped").read())["scoped"]
        assert any(s["id"] == sid for s in listed)
        req2 = urllib.request.Request(base + "/api/unscope",
                                      data=json.dumps({"scoped_id": sid}).encode(),
                                      headers={"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req2).read())["scoped_id"] == sid
    finally:
        srv.shutdown()
        srv.server_close()


def _a_canary_path(ctx):
    for b in ctx.index.blocks(contains="Kestrel"):
        return b["path"]
    raise AssertionError("no kestrel block")


import urllib.parse  # noqa: E402
