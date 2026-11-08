"""Memory index, quarantine lifecycle, shadow screen and CLI on a harmless fake lab."""
import json
from pathlib import Path

import pytest
import yaml

from brain_surgery.classify import StaticClassifier
from brain_surgery.cli import main
from brain_surgery.decision_log import DecisionLog
from brain_surgery.holds import HoldQueue
from brain_surgery.memory.config import MemoryConfigError, load_memory_config
from brain_surgery.memory.index import MemoryIndex
from brain_surgery.memory.quarantine import Quarantine, QuarantineError, _write_if_unchanged
from brain_surgery.memory.screen import screen
from brain_surgery.spec import load_spec

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SHARED = "Nightly jobs run at 02:00 and keep 14 days."

TOPIC = f"""---
name: backup schedule
type: project
---

# Backups

{SHARED}

- Job A copies the photo library.
- Job B copies the documents share.

## Retention

Monthly snapshots are kept for a year.
"""


@pytest.fixture
def lab(tmp_path):
    mem = tmp_path / "home" / ".claude" / "projects" / "p" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("# Memory index\n\n- [Backup schedule](backup.md) - nightly jobs\n")
    (mem / "backup.md").write_text(TOPIC)
    (mem / "old.archive-2026-01-01.md").write_text("archived, not scanned\n")
    agents = tmp_path / "agents"
    for name in ("alpha", "beta"):
        (agents / name).mkdir(parents=True)
        (agents / name / "MEMORY.md").write_text(f"# {name}\n\n- [notes](notes.md)\n")
        (agents / name / "notes.md").write_text(f"# Notes\n\n{SHARED}\n\nResolver is the Pi.\n")
    skills = tmp_path / "skills" / "report"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: report\ndescription: Write the weekly report.\n---\n\n"
                                     "# Report\n\nCollect the numbers first.\n")
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    (deployed / "CLAUDE.md").write_text("# Rules\n\nAlways run the linter.\n")
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "memory.screen.yaml").write_text(
        (EXAMPLES / "specs" / "memory.screen.yaml").read_text())
    cfg = {
        "sources": [
            {"id": "interactive", "kind": "claude_memory_dir", "path": str(mem)},
            {"id": "agents", "kind": "agent_memory_root", "path": str(agents)},
            {"id": "skills", "kind": "skills", "path": str(tmp_path / "skills")},
            {"id": "deployed", "kind": "files", "paths": [str(deployed / "CLAUDE.md")],
             "managed_by": "roles/app/files/CLAUDE.md"},
        ],
        "index_db": "state/memory.sqlite",
        "holds_db": "state/holds.sqlite",
        "decision_log_dir": "state/decisions",
        "screen": {"spec": "specs/memory.screen.yaml",
                   "classifiers": {"laya": {"kind": "static", "answers": {}}}},
    }
    (tmp_path / "memory.yaml").write_text(yaml.safe_dump(cfg))
    return tmp_path


def open_lab(lab):
    cfg = load_memory_config(lab / "memory.yaml")
    idx = MemoryIndex(cfg.index_db)
    return cfg, idx, HoldQueue(cfg.holds_db), Quarantine(idx, HoldQueue(cfg.holds_db))


def topic_path(lab):
    return lab / "home" / ".claude" / "projects" / "p" / "memory" / "backup.md"


def test_scan_roles_loaded_levels_and_incremental(lab):
    cfg, idx, _, _ = open_lab(lab)
    st = idx.scan(cfg.sources)
    assert st.files_seen == 8 and st.files_changed == 8 and st.missing_sources == []
    tree = {Path(r["path"]).name + "@" + r["agent"]: r for r in idx.tree()}
    assert "old.archive-2026-01-01.md@interactive" not in tree
    assert tree["MEMORY.md@interactive"]["loaded"] == "always"
    assert tree["backup.md@interactive"]["loaded"] == "on_demand"
    assert tree["notes.md@alpha"]["role"] == "topic"
    assert tree["CLAUDE.md@*"]["managed_by"] == "roles/app/files/CLAUDE.md"
    skill = idx.blocks(file_path=str(lab / "skills" / "report" / "SKILL.md"))
    assert {b["kind"]: b["loaded"] for b in skill}["frontmatter"] == "always"
    assert skill[-1]["loaded"] == "on_demand"

    again = idx.scan(cfg.sources)
    assert again.files_changed == 0 and again.files_removed == 0
    (lab / "agents" / "beta" / "notes.md").unlink()
    gone = idx.scan(cfg.sources)
    assert gone.files_removed == 1


def test_duplicates_across_agents(lab):
    cfg, idx, _, _ = open_lab(lab)
    idx.scan(cfg.sources)
    d = [r for r in idx.duplicates() if r["chars"] == len(SHARED)]
    assert len(d) == 1 and d[0]["files"] == 3 and d[0]["agents"] == 3


def test_blocks_are_metadata_unless_text_requested(lab):
    cfg, idx, _, _ = open_lab(lab)
    idx.scan(cfg.sources)
    rows = idx.blocks(contains="02:00")
    assert len(rows) == 3 and all("text" not in r for r in rows)
    assert all(r["text"] == SHARED for r in idx.blocks(contains="02:00", with_text=True))


def test_quarantine_release_round_trip(lab):
    cfg, idx, holds, q = open_lab(lab)
    idx.scan(cfg.sources)
    path = topic_path(lab)
    original = path.read_text()
    target = [b for b in idx.blocks(file_path=str(path)) if b["kind"] == "item"][0]
    res = q.quarantine([target["id"]], "test")
    assert len(res.held) == 1 and res.skipped == []
    assert "Job A" not in path.read_text() and "Job B" in path.read_text()
    h = holds.get(res.held[0]["hold_id"])
    assert h.kind == "memory" and h.payload["text"] == "- Job A copies the photo library."
    assert not any("Job A" in (b.get("section") or "") for b in idx.blocks(file_path=str(path)))
    out = q.release(h.id)
    assert out["restored"].startswith("after line")
    assert path.read_text() == original
    assert holds.get(h.id).status == "released"


def test_release_finds_its_place_after_the_file_moved_on(lab):
    cfg, idx, _, q = open_lab(lab)
    idx.scan(cfg.sources)
    path = topic_path(lab)
    tgt = [b for b in idx.blocks(file_path=str(path)) if b["kind"] == "paragraph"
           and b["chars"] == len(SHARED)][0]
    hid = q.quarantine([tgt["id"]], "test").held[0]["hold_id"]
    # an agent writes more to the file in the meantime
    path.write_text(path.read_text().replace("# Backups\n", "# Backups\n\nAdded later by an agent.\n"))
    q.release(hid)
    text = path.read_text()
    assert text.index("# Backups") < text.index(SHARED) < text.index("- Job A")


def test_quarantine_every_copy_by_hash(lab):
    cfg, idx, holds, q = open_lab(lab)
    idx.scan(cfg.sources)
    h = idx.blocks(contains="02:00")[0]["hash"]
    ids = [b["id"] for b in idx.blocks(hash=h[:12])]
    res = q.quarantine(ids, "duplicate across agents")
    assert len(res.held) == 3
    assert idx.blocks(contains="02:00") == []
    assert len(holds.list(kind="memory")) == 3


def test_one_of_two_identical_blocks(lab):
    cfg, idx, _, q = open_lab(lab)
    path = topic_path(lab)
    path.write_text(path.read_text() + "\n- Job A copies the photo library.\n")
    idx.scan(cfg.sources)
    copies = [b for b in idx.blocks(file_path=str(path)) if b["chars"] == 33 and b["kind"] == "item"]
    assert [c["occ"] for c in copies] == [0, 1]
    hid = q.quarantine([copies[1]["id"]], "dup").held[0]["hold_id"]
    assert path.read_text().count("Job A") == 1
    q.release(hid)
    assert path.read_text().count("Job A") == 2


def test_excise_scrubs_and_watches_for_regrowth(lab):
    cfg, idx, holds, q = open_lab(lab)
    idx.scan(cfg.sources)
    path = topic_path(lab)
    tgt = [b for b in idx.blocks(file_path=str(path)) if b["section"] == "Backups > Retention"
           and b["kind"] == "paragraph"][0]
    hid = q.quarantine([tgt["id"]], "test").held[0]["hold_id"]
    q.excise(hid, "confirmed")
    h = holds.get(hid)
    assert h.status == "excised" and h.payload["text"] is None and h.payload["excised"]
    with pytest.raises(QuarantineError, match="already excised"):
        q.release(hid)
    assert idx.scan(cfg.sources).regrowth == []
    path.write_text(path.read_text() + "\nMonthly snapshots are kept for a year.\n")
    regrew = idx.scan(cfg.sources).regrowth
    assert len(regrew) == 1 and regrew[0]["hold_id"] == hid


def test_managed_copy_refused_unless_forced(lab):
    cfg, idx, _, q = open_lab(lab)
    idx.scan(cfg.sources)
    p = lab / "deployed" / "CLAUDE.md"
    ids = [b["id"] for b in idx.blocks(file_path=str(p)) if b["kind"] == "paragraph"]
    res = q.quarantine(ids, "test")
    assert res.held == [] and "roles/app/files/CLAUDE.md" in res.skipped[0]["reason"]
    assert "linter" in p.read_text()
    assert len(q.quarantine(ids, "test", force=True).held) == 1
    assert "linter" not in p.read_text()


def test_stale_block_is_skipped(lab):
    cfg, idx, _, q = open_lab(lab)
    idx.scan(cfg.sources)
    path = topic_path(lab)
    tgt = [b for b in idx.blocks(file_path=str(path)) if b["kind"] == "item"][0]
    path.write_text(path.read_text().replace("- Job A copies the photo library.\n", ""))
    res = q.quarantine([tgt["id"]], "test")
    assert res.held == [] and "no longer in the file" in res.skipped[0]["reason"]
    assert q.quarantine(["nope:0:0"], "x").skipped[0]["reason"].startswith("not in index")


def test_compare_and_swap_refuses_a_changed_file(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("one\n")
    with pytest.raises(QuarantineError, match="changed while"):
        _write_if_unchanged(p, "0" * 64, "two\n")
    assert p.read_text() == "one\n"


def test_state_inside_a_scanned_root_is_rejected(lab):
    cfg = yaml.safe_load((lab / "memory.yaml").read_text())
    cfg["index_db"] = str(lab / "agents" / "index.sqlite")
    (lab / "bad.yaml").write_text(yaml.safe_dump(cfg))
    with pytest.raises(MemoryConfigError, match="inside scanned root"):
        load_memory_config(lab / "bad.yaml")


def test_shadow_screen_records_by_hash_and_never_logs_text(lab):
    cfg, idx, holds, _ = open_lab(lab)
    idx.scan(cfg.sources)
    spec = load_spec(lab / "specs" / "memory.screen.yaml")

    def answers(state, questions):
        flagged = "Resolver" in state
        return {"restricted_topic": {"noul": 0.95 if flagged else 0.02},
                "step_detail": {"noul": 0.9}, "verbatim_copy": {"noul": 0.1},
                "pointer_only": {"noul": 0.02}}

    log = DecisionLog(lab / "state" / "decisions")
    res = screen(idx, spec, {"laya": StaticClassifier("laya", answers)}, log=log)
    distinct = len({b["hash"] for b in idx.blocks()})
    assert res["screened"] == distinct
    assert res["outcomes"].get("quarantine") == 1        # the one 'Resolver' block (2 copies)
    flagged = idx.blocks(outcome="quarantine")
    assert len(flagged) == 2 and {b["agent"] for b in flagged} == {"alpha", "beta"}
    assert screen(idx, spec, {"laya": StaticClassifier("laya", answers)})["screened"] == 0
    logged = "".join(f.read_text() for f in (lab / "state" / "decisions").glob("*.jsonl"))
    assert "Resolver is the Pi" not in logged and SHARED not in logged
    assert holds.list(kind="memory") == []                  # shadow: nothing quarantined


def test_cli_memory_flow_is_metadata_only(lab, capsys):
    c = str(lab / "memory.yaml")
    assert main(["memory", "--config", c, "scan"]) == 0
    assert main(["memory", "--config", c, "blocks", "--contains", "resolver"]) == 0
    out = capsys.readouterr().out
    assert "Resolver is the Pi" not in out and "alpha" in out
    assert main(["memory", "--config", c, "blocks", "--contains", "resolver", "--text"]) == 0
    assert "Resolver is the Pi" in capsys.readouterr().out

    assert main(["memory", "--config", c, "blocks", "--contains", "resolver", "--json"]) == 0
    ids = [r["id"] for r in json.loads(capsys.readouterr().out)]
    assert main(["memory", "--config", c, "quarantine", "--id", *ids, "--reason", "cli test"]) == 0
    held = json.loads(capsys.readouterr().out)["held"]
    assert len(held) == 2

    db = str(lab / "state" / "holds.sqlite")
    assert main(["holds", "--db", db, "show", str(held[0]["hold_id"])]) == 0
    shown = capsys.readouterr().out
    assert "Resolver is the Pi" not in shown and "--text to show" in shown
    assert main(["holds", "--db", db, "resolve", str(held[0]["hold_id"]), "released"]) == 1
    assert "memory release" in capsys.readouterr().err

    assert main(["memory", "--config", c, "release", str(held[0]["hold_id"])]) == 0
    assert main(["memory", "--config", c, "excise", str(held[1]["hold_id"])]) == 0
    assert main(["memory", "--config", c, "release", str(held[1]["hold_id"])]) == 1
    capsys.readouterr()
    assert main(["memory", "--config", c, "tree", "--agent", "alpha"]) == 0
    assert "notes.md" in capsys.readouterr().out
    assert main(["memory", "--config", c, "dupes"]) == 0
    assert main(["memory", "--config", c, "screen"]) == 0
    assert main(["memory", "--config", c, "screen", "--summary"]) == 0
