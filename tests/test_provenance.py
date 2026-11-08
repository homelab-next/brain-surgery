"""Session-log reader, matcher, and the tracer -- on hand-built logs and the synth lab."""
import json
from pathlib import Path

import pytest

from brain_surgery.provenance.index import ProvenanceIndex
from brain_surgery.provenance.match import similarity
from brain_surgery.provenance.session_logs import iter_sessions, parse_session
from brain_surgery.provenance.trace import Tracer


def write_log(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def tool_use(uuid, ts, name, tid, inp):
    return {"type": "assistant", "uuid": uuid, "timestamp": ts, "sessionId": "s1",
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def result(uuid, ts, tid, tur, content="ok"):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "sessionId": "s1", "isSidechain": False,
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]},
            "toolUseResult": tur}


# --- matcher -------------------------------------------------------------------------
def test_similarity_exact_and_overlap():
    assert similarity("Job A copies the library.",
                      "Notes:\n\nJob A copies the library.\nmore") == (1.0, "exact")
    score, method = similarity("the quick brown fox jumps",
                               "a quick brown fox leaps over")
    assert method == "overlap" and 0 < score < 1
    assert similarity("", "x")[0] == 0.0


# --- reader --------------------------------------------------------------------------
def test_reader_normalizes_write_read_fetch_and_user(tmp_path):
    f = tmp_path / "s1.jsonl"
    write_log(f, [
        tool_use("a1", "t1", "Read", "r1", {"file_path": "/src/brief.md"}),
        result("u1", "t2", "r1", {"type": "text", "file": {"filePath": "/src/brief.md",
                                                            "content": "the brief body"}}),
        tool_use("a2", "t3", "WebFetch", "f1", {"url": "https://x/y", "prompt": "read"}),
        result("u2", "t4", "f1", {"content": "fetched text"}, content="fetched text"),
        {"type": "attachment", "timestamp": "t0", "uuid": "at1", "sessionId": "s1",
         "attachment": {"type": "instructions", "files": [{"path": "/proj/CLAUDE.md",
                                                            "content": "always lint"}]}},
        tool_use("a3", "t5", "Write", "w1", {"file_path": "/mem/note.md", "content": "# N\n\nbody"}),
        result("u3", "t6", "w1", {"type": "create", "filePath": "/mem/note.md",
                                  "content": "# N\n\nbody"}),
    ])
    sess = parse_session(f)
    assert len(sess.writes) == 1 and sess.writes[0].path == "/mem/note.md"
    assert "body" in sess.writes[0].text and sess.writes[0].kind == "create"
    origins = {(i.origin, i.ref) for i in sess.inputs}
    assert ("read", "/src/brief.md") in origins
    assert ("web", "https://x/y") in origins
    assert ("instruction", "/proj/CLAUDE.md") in origins


def test_reader_skips_garbage_lines(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text('not json\n{"type":"user","message":{"content":"hi"},"isSidechain":false}\n')
    assert parse_session(f).writes == []


# --- tracer on hand-built logs -------------------------------------------------------
def build(tmp_path, records):
    write_log(tmp_path / "s1.jsonl", records)
    prov = ProvenanceIndex(":memory:")
    prov.build(iter_sessions([tmp_path]))
    return prov


def test_trace_authored_root(tmp_path):
    block = "Do step one, then step two, then step three."
    prov = build(tmp_path, [
        tool_use("a1", "t1", "Read", "r1", {"file_path": "/src/playbook.md"}),
        result("u1", "t2", "r1", {"type": "text", "file": {"filePath": "/src/playbook.md",
                                                            "content": f"# Playbook\n\n{block}"}}),
        tool_use("a2", "t3", "Write", "w1", {"file_path": "/mem/a.md",
                                             "content": f"# A\n\n{block}"}),
        result("u2", "t4", "w1", {"type": "create", "filePath": "/mem/a.md",
                                  "content": f"# A\n\n{block}"}),
    ])
    from brain_surgery.memory.blocks import block_hash
    tr = Tracer(prov, agent_of=lambda p: "agent-x" if "/mem/" in p else None)
    t = tr.trace(block_hash(block), block, "/mem/a.md")
    assert t.root_kind == "authored" and t.root_where == "/src/playbook.md"
    assert [s.node for s in t.steps] == ["block", "input"]
    assert t.steps[0].agent == "agent-x" and t.steps[1].origin == "read"


def test_trace_external_user_and_composed(tmp_path):
    from brain_surgery.memory.blocks import block_hash
    quote, summ, own = "Confidential pricing valid thirty days.", "We hold at two years.", "My own note."
    prov = build(tmp_path, [
        tool_use("a1", "t1", "WebFetch", "f1", {"url": "https://v/p"}),
        result("u1", "t2", "f1", {"content": f"page: {quote}"}, content=f"page: {quote}"),
        {"type": "user", "uuid": "um", "timestamp": "t3", "sessionId": "s1", "isSidechain": False,
         "message": {"role": "user", "content": [{"type": "text", "text": summ}]}},
        tool_use("a2", "t4", "Write", "w1", {"file_path": "/mem/a.md",
                 "content": f"# A\n\n{quote}\n\n{summ}\n\n{own}"}),
        result("u2", "t5", "w1", {"type": "create", "filePath": "/mem/a.md",
                 "content": f"# A\n\n{quote}\n\n{summ}\n\n{own}"}),
    ])
    tr = Tracer(prov)
    assert tr.trace(block_hash(quote), quote, "/mem/a.md").root_kind == "external"
    assert tr.trace(block_hash(summ), summ, "/mem/a.md").root_kind == "user"
    assert tr.trace(block_hash(own), own, "/mem/a.md").root_kind == "composed"


def test_trace_managed_short_circuits_without_logs(tmp_path):
    from brain_surgery.memory.blocks import block_hash
    prov = ProvenanceIndex(":memory:")        # no logs at all
    tr = Tracer(prov, managed_of=lambda p: "roles/app/CLAUDE.md" if p == "/opt/CLAUDE.md" else None)
    t = tr.trace(block_hash("x"), "x", "/opt/CLAUDE.md")
    assert t.root_kind == "managed" and t.root_where == "roles/app/CLAUDE.md"
    assert t.steps[0].method == "deploy-map"


def test_trace_unknown_when_no_log(tmp_path):
    from brain_surgery.memory.blocks import block_hash
    prov = build(tmp_path, [])
    t = Tracer(prov).trace(block_hash("orphan"), "orphan", "/mem/a.md")
    assert t.root_kind == "unknown" and "no session log" in t.notes[0]


def test_incremental_build_skips_unchanged(tmp_path):
    recs = [tool_use("a", "t1", "Write", "w", {"file_path": "/m/a.md", "content": "# A\n\nb"}),
            result("u", "t2", "w", {"type": "create", "filePath": "/m/a.md", "content": "# A\n\nb"})]
    write_log(tmp_path / "s1.jsonl", recs)
    prov = ProvenanceIndex(tmp_path / "p.sqlite")
    assert prov.build(iter_sessions([tmp_path]))["logs"] == 1
    assert prov.build(iter_sessions([tmp_path]))["logs"] == 0      # unchanged mtime


# --- end to end on the synthetic lab -------------------------------------------------
def test_synth_lab_provenance_is_perfect_on_ground_truth(tmp_path):
    from brain_surgery.memory.config import load_memory_config
    from brain_surgery.memory.index import MemoryIndex
    from brain_surgery.memory.synth import generate
    from brain_surgery.provenance.evaluate import evaluate_traces
    from brain_surgery.provenance.synth_logs import generate_logs

    out = tmp_path / "lab"
    generate(out, agents=12, seed=4, canary_rate=1.0)
    info = generate_logs(out)
    assert info["truth_blocks"] > 0
    cfg = load_memory_config(out / "memory.oracle.yaml")
    assert cfg.prov_logs and cfg.prov_index_db
    idx = MemoryIndex(cfg.index_db)
    idx.scan(cfg.sources)
    prov = ProvenanceIndex(cfg.prov_index_db)
    prov.build(iter_sessions(cfg.prov_logs))
    tr = Tracer(prov, agent_of=idx.agent_for_path, managed_of=idx.managed_for_path)
    res = evaluate_traces(idx, tr, out / "provenance_truth.jsonl")
    assert res["accuracy"] == 1.0 and res["missing"] == 0
    # every expected root kind was exercised
    assert set(res["confusion"]) == {"authored", "external", "user", "composed", "managed"}


def test_synth_lab_cli_trace_and_roots(tmp_path, capsys):
    from brain_surgery.cli import main
    from brain_surgery.memory.synth import generate
    from brain_surgery.provenance.synth_logs import generate_logs

    out = tmp_path / "lab"
    generate(out, agents=10, seed=9, canary_rate=1.0)
    generate_logs(out)
    cfgp = str(out / "memory.oracle.yaml")
    assert main(["memory", "--config", cfgp, "scan"]) == 0
    capsys.readouterr()
    assert main(["memory", "--config", cfgp, "roots", "--all", "--json"]) == 0
    out_json = capsys.readouterr().out
    kinds = {r["root_kind"] for r in json.loads(out_json)}
    assert {"authored", "external", "user"} <= kinds
    # a managed block (the deployed copy's pointer) traces to its source, not a file edit
    assert main(["memory", "--config", cfgp, "blocks", "--contains", "kestrel", "--json"]) == 0
    blocks = json.loads(capsys.readouterr().out)
    deployed = [b for b in blocks if "deployed" in b["path"]]
    if deployed:
        assert main(["memory", "--config", cfgp, "trace", "--id", deployed[0]["id"]]) == 0
        assert json.loads(capsys.readouterr().out)["root"]["kind"] == "managed"
