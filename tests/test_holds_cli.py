from pathlib import Path

import pytest

from brain_surgery.cli import main
from brain_surgery.holds import HoldQueue

EXAMPLE_SPEC = Path(__file__).resolve().parents[1] / "examples" / "specs" / "route.prompt_size.yaml"


def test_hold_lifecycle(tmp_path):
    q = HoldQueue(tmp_path / "h.sqlite")
    i = q.add("prompt", "route holds", {"text": "Tidy the release notes."}, spec_id="s",
              decision_id="d", outcome="unsure")
    assert [h.id for h in q.list()] == [i]
    h = q.resolve(i, "released", "ran on the large local model")
    assert h.status == "released" and h.resolved_at and h.note
    assert q.list() == [] and len(q.list(status=None)) == 1
    with pytest.raises(ValueError, match="already released"):
        q.resolve(i, "dismissed")
    with pytest.raises(ValueError):
        q.resolve(q.add("memory", "x", {}), "held")
    with pytest.raises(KeyError):
        q.resolve(999, "dismissed")


def test_cli_lint(capsys, tmp_path):
    assert main(["lint", str(EXAMPLE_SPEC)]) == 0
    assert "ok" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text("id: x\nerror_direction: nope\n")
    assert main(["lint", str(bad)]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_holds(capsys, tmp_path):
    db = tmp_path / "h.sqlite"
    i = HoldQueue(db).add("prompt", "no target in the route was available", {"text": "hi"})
    assert main(["holds", "--db", str(db), "list"]) == 0
    assert "no target" in capsys.readouterr().out
    assert main(["holds", "--db", str(db), "resolve", str(i), "dismissed", "--note", "dup"]) == 0
    assert main(["holds", "--db", str(db), "show", str(i)]) == 0
    assert '"dismissed"' in capsys.readouterr().out


def test_cli_config_error_exit_code(tmp_path, capsys):
    cfg = tmp_path / "routing.yaml"
    cfg.write_text("spec: missing.yaml\n")
    with pytest.raises(FileNotFoundError):
        main(["route", "--config", str(cfg), "--text", "hi"])
    (tmp_path / "s.yaml").write_text(EXAMPLE_SPEC.read_text())
    cfg.write_text("spec: s.yaml\nroutes: {small: hold}\n")
    assert main(["route", "--config", str(cfg), "--text", "hi"]) == 2
    assert "has no route" in capsys.readouterr().err
