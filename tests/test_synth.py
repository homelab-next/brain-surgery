"""Synthetic lab: generation, labels, and the whole memory lifecycle on it."""
from importlib import resources
from pathlib import Path

import pytest
import yaml

from brain_surgery.cli import main
from brain_surgery.holds import HoldQueue
from brain_surgery.memory.config import load_memory_config
from brain_surgery.memory.evaluate import evaluate_screen, expected_outcome, load_labels
from brain_surgery.memory.index import MemoryIndex
from brain_surgery.memory.quarantine import Quarantine
from brain_surgery.memory.screen import screen
from brain_surgery.memory.synth import generate
from brain_surgery.spec import load_spec

REPO = Path(__file__).resolve().parents[1]


def test_packaged_spec_matches_example():
    packaged = resources.files("brain_surgery").joinpath("data/memory.screen.yaml").read_text()
    assert packaged == (REPO / "examples" / "specs" / "memory.screen.yaml").read_text()


def test_generate_is_deterministic_and_labels_live_outside_the_tree(tmp_path):
    a = generate(tmp_path / "a", agents=6, seed=3)
    b = generate(tmp_path / "b", agents=6, seed=3)
    la, lb = load_labels(tmp_path / "a" / "labels.jsonl"), load_labels(tmp_path / "b" / "labels.jsonl")
    assert la == lb and a["labelled_blocks"] == b["labelled_blocks"]
    assert not list((tmp_path / "a" / "tree").rglob("labels.jsonl"))
    with pytest.raises(FileExistsError):
        generate(tmp_path / "a")
    for cfg in ("memory.yaml", "memory.oracle.yaml"):
        load_memory_config(tmp_path / "a" / cfg)          # state outside every root, spec valid


def test_every_canary_shape_has_the_intended_expected_outcome(tmp_path):
    generate(tmp_path / "lab", agents=30, seed=5, canary_rate=1.0)
    spec = load_spec(tmp_path / "lab" / "specs" / "memory.screen.yaml")
    assert "Kestrel" in spec.question("restricted_topic").text
    want = {"canary_summary": "review", "canary_steps": "quarantine", "canary_verbatim": "quarantine",
            "canary_steps_verbatim": "quarantine", "canary_pointer": "keep", "index_pointer": "keep",
            "hard_negative_codename": "keep", "hard_negative_steps": "keep",
            "hard_negative_verbatim": "keep", "routine": "keep", "canary_skill_listing": "review"}
    seen = set()
    for row in load_labels(tmp_path / "lab" / "labels.jsonl").values():
        if row["scenario"] in want:
            assert expected_outcome(spec, row["labels"]) == want[row["scenario"]], row
            seen.add(row["scenario"])
    assert seen == set(want)


def lab_with_oracle(tmp_path, noise=0.0, band=0.0):
    out = tmp_path / "lab"
    generate(out, agents=10, seed=11, canary_rate=1.0)
    cfg = yaml.safe_load((out / "memory.oracle.yaml").read_text())
    cfg["screen"]["classifiers"]["laya"].update(noise=noise, band=band)
    (out / "memory.oracle.yaml").write_text(yaml.safe_dump(cfg))
    c = load_memory_config(out / "memory.oracle.yaml")
    idx = MemoryIndex(c.index_db)
    idx.scan(c.sources)
    return out, c, idx


def test_perfect_oracle_grades_perfectly(tmp_path):
    out, c, idx = lab_with_oracle(tmp_path)
    from brain_surgery.decision_log import DecisionLog

    screen(idx, c.screen_spec, c.classifiers, log=DecisionLog(c.decision_log_dir))
    res = evaluate_screen(idx, c.screen_spec, out / "labels.jsonl", c.decision_log_dir)
    o = res["outcomes"]
    assert o["screened"] == o["labelled"] and o["unsure"] == 0
    assert o["precision_when_decided"] == 1.0
    assert all(q["precision_when_confident"] == 1.0 and q["coverage"] == 1.0 for q in res["questions"])
    assert set(res["escalated"].values()) == {0.0}


def test_noisy_oracle_shows_up_in_the_metrics(tmp_path):
    out, c, idx = lab_with_oracle(tmp_path, noise=0.2, band=0.2)
    from brain_surgery.decision_log import DecisionLog

    screen(idx, c.screen_spec, c.classifiers, log=DecisionLog(c.decision_log_dir))
    res = evaluate_screen(idx, c.screen_spec, out / "labels.jsonl", c.decision_log_dir)
    assert res["outcomes"]["unsure"] > 0
    pwc = [q["precision_when_confident"] for q in res["questions"]]
    assert all(0.55 < p < 0.98 for p in pwc), pwc
    assert all(0.6 < q["coverage"] < 0.95 for q in res["questions"])


def test_full_lifecycle_on_the_synthetic_lab(tmp_path, capsys):
    out, c, idx = lab_with_oracle(tmp_path)
    cfgp = str(out / "memory.oracle.yaml")
    assert main(["memory", "--config", cfgp, "screen"]) == 0
    capsys.readouterr()

    # the shared 'steps' note: one hash, copied into three agents, flagged once
    flagged = idx.blocks(outcome="quarantine")
    by_hash: dict[str, list] = {}
    for b in flagged:
        by_hash.setdefault(b["hash"], []).append(b)
    shared = [h for h, bs in by_hash.items() if len({b["agent"] for b in bs}) == 3]
    assert len(shared) == 1

    # quarantine every flagged block; the managed copy holds only a pointer, so none is skipped
    q = Quarantine(idx, HoldQueue(c.holds_db))
    res = q.quarantine([b["id"] for b in flagged], "synthetic: screen said quarantine")
    assert len(res.held) == len(flagged) and res.skipped == []
    assert idx.blocks(outcome="quarantine") == []
    assert idx.scan(c.sources).regrowth == []

    # no tree file still contains a quarantined text
    held = HoldQueue(c.holds_db).list(kind="memory")
    texts = {h.payload["text"] for h in held}
    tree_text = "\n".join(p.read_text() for p in (out / "tree").rglob("*.md"))
    assert not any(t in tree_text for t in texts)

    # release one, excise the rest
    q.release(held[0].id)
    results = {h.id: q.excise(h.id, "synthetic") for h in held[1:]}
    # excising a copy whose twin was just released reports the live twin straight away...
    twins = [h for h in held[1:] if h.payload["hash"] == held[0].payload["hash"]]
    for h in twins:
        assert results[h.id]["live_copies"], "the released twin should be listed"
    # ...and scan reports it as regrowth; so is excised text written anywhere new
    victim = next(h for h in held[1:] if h.payload["hash"] != held[0].payload["hash"])
    note = out / "tree" / "notes" / "ops" / "network.md"
    note.write_text(note.read_text() + "\n" + victim.payload["text"] + "\n")
    regrew = idx.scan(c.sources).regrowth
    assert any(r["path"] == str(note) for r in regrew)
    assert {r["hash"] for r in regrew} == {victim.payload["hash"]} | (
        {held[0].payload["hash"]} if twins else set())

    # the CLI output never carried the canary text
    assert main(["memory", "--config", cfgp, "blocks", "--contains", "kestrel"]) == 0
    printed = capsys.readouterr().out
    assert "Schedule B" not in printed and "percent" not in printed
