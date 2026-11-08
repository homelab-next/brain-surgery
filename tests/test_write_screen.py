"""Write-time screening: shadow records only, enforce auto-quarantines, with the guards."""
from pathlib import Path

import pytest
import yaml

from brain_surgery.holds import HoldQueue
from brain_surgery.memory.config import MemoryConfigError, load_memory_config
from brain_surgery.memory.index import MemoryIndex
from brain_surgery.memory.quarantine import Quarantine
from brain_surgery.memory.synth import generate
from brain_surgery.memory.write_screen import screen_write

TRIGGER = "Resolver and the exact steps to reach it are recorded here in detail."


def lab(tmp_path, mode="shadow"):
    """A lab whose screen deterministically quarantines a block mentioning Resolver + steps,
    via a regex `rule` tier written into the config file (so the CLI loads it too)."""
    out = tmp_path / "lab"
    generate(out, agents=6, seed=2)
    cfg = yaml.safe_load((out / "memory.oracle.yaml").read_text())
    cfg["write_screen"] = {"mode": mode, "actions": ["quarantine"]}
    # every question gets a definite answer (first matching rule wins), so the three-valued
    # table is never left blocked on an unanswered question
    cfg["screen"]["classifiers"] = {"rule": {"kind": "rules", "rules": {
        "restricted_topic": [{"pattern": "Resolver", "answer": True}, {"pattern": ".", "answer": False}],
        "step_detail": [{"pattern": "steps", "answer": True}, {"pattern": ".", "answer": False}],
        "verbatim_copy": [{"pattern": ".", "answer": False}],
        "pointer_only": [{"pattern": ".", "answer": False}]}}}
    (out / "memory.oracle.yaml").write_text(yaml.safe_dump(cfg))
    c = load_memory_config(out / "memory.oracle.yaml")
    idx = MemoryIndex(c.index_db)
    idx.scan(c.sources)
    return out, c, idx


def write_block(path: Path, text: str) -> None:
    path.write_text(path.read_text() + "\n\n" + text + "\n")


def a_live_topic_file(out):
    return next(p for p in (out / "tree" / "agent-memory").rglob("*.md") if p.name != "MEMORY.md")


def test_shadow_records_but_quarantines_nothing(tmp_path):
    out, c, idx = lab(tmp_path, mode="shadow")
    f = a_live_topic_file(out)
    write_block(f, TRIGGER)
    res = screen_write(c, idx, Quarantine(idx, HoldQueue(c.holds_db)), paths=[str(f)])
    assert res["mode"] == "shadow"
    assert any(w["outcome"] == "quarantine" for w in res["would_quarantine"])
    assert res["quarantined"] == []
    assert TRIGGER in f.read_text()                       # still in the file
    assert HoldQueue(c.holds_db).list(kind="memory") == []


def test_enforce_auto_quarantines_the_bad_block_only(tmp_path):
    out, c, idx = lab(tmp_path, mode="enforce")
    f = a_live_topic_file(out)
    before = f.read_text()
    write_block(f, "Routine note: nightly jobs run at 02:00.")
    write_block(f, TRIGGER)
    res = screen_write(c, idx, Quarantine(idx, HoldQueue(c.holds_db)), paths=[str(f)])
    assert len(res["quarantined"]) == 1
    txt = f.read_text()
    assert TRIGGER not in txt and "Routine note" in txt   # only the bad block removed
    held = HoldQueue(c.holds_db).list(kind="memory", status="held")
    assert len(held) == 1 and held[0].reason.startswith("auto: write-time screen")


def test_enforce_skips_already_watchlisted(tmp_path):
    out, c, idx = lab(tmp_path, mode="enforce")
    f = a_live_topic_file(out)
    write_block(f, TRIGGER)
    q = Quarantine(idx, HoldQueue(c.holds_db))
    first = screen_write(c, idx, q, paths=[str(f)])
    hid = first["quarantined"][0]["hold_id"]
    # excise it (watchlists the hash), then the agent writes it again
    q.excise(hid, "confirmed")
    write_block(f, TRIGGER)
    second = screen_write(c, idx, q, paths=[str(f)])
    assert second["quarantined"] == []
    assert any("already excised" in s.get("reason", "") for s in second["skipped"])


def test_enforce_skips_managed_copies(tmp_path):
    out, c, idx = lab(tmp_path, mode="enforce")
    managed = out / "tree" / "deployed" / "CLAUDE.md"
    write_block(managed, TRIGGER)
    res = screen_write(c, idx, Quarantine(idx, HoldQueue(c.holds_db)), paths=[str(managed)])
    assert res["quarantined"] == []
    assert any("managed copy" in s.get("reason", "") for s in res["skipped"])
    assert TRIGGER in managed.read_text()                 # untouched


def test_mode_override_and_config_validation(tmp_path):
    out, c, idx = lab(tmp_path, mode="shadow")
    f = a_live_topic_file(out)
    write_block(f, TRIGGER)
    # config says shadow, but an explicit enforce override acts
    res = screen_write(c, idx, Quarantine(idx, HoldQueue(c.holds_db)), paths=[str(f)], mode="enforce")
    assert len(res["quarantined"]) == 1

    cfg = yaml.safe_load((out / "memory.oracle.yaml").read_text())
    cfg["write_screen"] = {"mode": "wat"}
    (out / "bad.yaml").write_text(yaml.safe_dump(cfg))
    with pytest.raises(MemoryConfigError, match="mode"):
        load_memory_config(out / "bad.yaml")


def test_cli_screen_write(tmp_path, capsys):
    out, c, idx = lab(tmp_path, mode="shadow")
    idx.close()
    f = a_live_topic_file(out)
    write_block(f, TRIGGER)
    from brain_surgery.cli import main
    # the real config file has shadow mode; --enforce overrides
    rc = main(["memory", "--config", str(out / "memory.oracle.yaml"), "screen-write",
               "--file", str(f), "--enforce"])
    import json
    res = json.loads(capsys.readouterr().out)
    assert rc == 10 and res["quarantined"]
    assert TRIGGER not in f.read_text()
