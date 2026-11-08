import copy
from pathlib import Path

import pytest

from conftest import SIZE_SPEC
from brain_surgery.spec import SpecError, fact_matches, load_spec, parse_spec

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "specs" / "route.prompt_size.yaml"


def spec_with(**changes):
    d = copy.deepcopy(SIZE_SPEC)
    d.update(changes)
    return d


def test_example_spec_loads():
    s = load_spec(EXAMPLE)
    assert s.id == "route.prompt_size"
    assert s.facts_used() == {"prompt_chars"}
    assert s.threshold("laya", "multi_step") == (0.85, 0.15)
    assert s.threshold_meta["laya"]["model"] == "laya-1.0"


def test_lab_template_extras_are_tolerated():
    d = spec_with(status="shadow", consequence_floor="LOW", labels={"count_today": 0},
                  results=[], inputs=[{"name": "text"}], max_len={"laya": 1024})
    d["questions"][0]["mask"] = []
    d["questions"][0]["variants"] = []
    assert parse_spec(d).status == "shadow"


def test_state_card_renders_missing_fields_blank():
    s = parse_spec(spec_with(state_card="A: {text} B: {missing}"))
    assert s.render_state({"text": "hi"}) == "A: hi B: "


@pytest.mark.parametrize("change,needle", [
    ({"error_direction": "sideways"}, "error_direction"),
    ({"on_error": "allow"}, "on_error"),
    ({"outcomes": [{"id": "small"}]}, "unsure"),
    ({"outcomes": [{"id": "small"}, {"id": "unsure"}]}, "escalate_to"),
    ({"decision_table": [{"when": {"multi_step": ">=hi"}, "outcome": "small"}]}, "default"),
    ({"decision_table": [{"default": "unsure"},
                         {"when": {"multi_step": ">=hi"}, "outcome": "small"}]}, "last row"),
    ({"decision_table": [{"when": {"multi_step": ">=hi"}, "outcome": "huge"},
                         {"default": "unsure"}]}, "undeclared outcome"),
    ({"decision_table": [{"when": {"multi_step": "maybe"}, "outcome": "small"},
                         {"default": "unsure"}]}, "must be one of"),
    ({"questions": []}, "no questions"),
])
def test_invalid_specs_are_rejected(change, needle):
    with pytest.raises(SpecError) as ei:
        parse_spec(spec_with(**change))
    assert any(needle in p for p in ei.value.problems), ei.value.problems


def test_all_problems_reported_at_once():
    with pytest.raises(SpecError) as ei:
        parse_spec(spec_with(error_direction="x", on_error="y", questions=[]))
    assert len(ei.value.problems) >= 3


def test_choice_limits():
    q = {"id": "k", "type": "choice", "text": "Which?", "criteria": {f"o{i}": "x" for i in range(15)}}
    with pytest.raises(SpecError, match="15 options"):
        parse_spec(spec_with(questions=[q]))
    q["criteria"] = {"a": "x", "b": "y"}
    table = [{"when": {"k": "c"}, "outcome": "small"}, {"default": "unsure"}]
    with pytest.raises(SpecError, match="label"):
        parse_spec(spec_with(questions=[q], decision_table=table, thresholds={}))


def test_safety_gate_cannot_allow_on_model_answers_alone():
    outcomes = [{"id": "allow"}, {"id": "unsure", "escalate_to": "human"}]
    table = [{"when": {"multi_step": "<=lo"}, "outcome": "allow"}, {"default": "unsure"}]
    with pytest.raises(SpecError, match="permissive"):
        parse_spec(spec_with(error_direction="safety_gate", outcomes=outcomes, decision_table=table))
    table[0]["when"]["on_allowlist"] = True        # a deterministic fact corroborates it
    assert parse_spec(spec_with(error_direction="safety_gate", outcomes=outcomes,
                                decision_table=table))


@pytest.mark.parametrize("cond,value,expected", [
    (">1000", 1500, True), (">1000", 999, False), ("<=3", 3, True), ("==2", 2.0, True),
    (True, True, True), (True, False, False), (True, "true", True), ("x", "x", True),
    (">1", None, False), (True, None, False), (">1", "abc", False),
])
def test_fact_conditions(cond, value, expected):
    assert fact_matches(cond, value) is expected
