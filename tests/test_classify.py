import pytest

from brain_surgery.classify import (ClassifierError, Question, RuleClassifier,
                                    StaticClassifier, SystemOneClassifier, build_classifier)

QS = [
    Question("multi_step", "noul", "Does this need several steps?"),
    Question("kind", "choice", "What kind of request is this?",
             criteria={"question": "asks for information", "task": "asks for work to be done"}),
    Question("length", "score", "How long should the answer be, in paragraphs?"),
]


def test_systemone_wire_format_and_parsing(fake_server):
    def handler(path, body):
        return 200, {"answers": {
            "multi_step": {"noul": 0.92},
            "kind": {"choice": "task", "probabilities": {"question": 0.2, "task": 0.8}},
            "length": {"score": 3},
        }}

    srv = fake_server(handler)
    clf = SystemOneClassifier("laya", srv.url, model="laya-1.0")
    out = clf.ask("Summarise the backup report and list failed jobs.", QS)

    path, body = srv.requests[0]
    assert path == "/v1/systemone"
    assert body["model"] == "laya-1.0"
    assert body["state"] == "Summarise the backup report and list failed jobs."
    assert set(body["questions"]) == {"multi_step", "kind", "length"}   # one call, all questions
    assert body["questions"]["multi_step"] == {"type": "noul",
                                               "instructions": "Does this need several steps?"}
    assert body["questions"]["kind"]["criteria"] == {"question": "asks for information",
                                                     "task": "asks for work to be done"}
    assert out["multi_step"].value == pytest.approx(0.92)
    assert out["kind"].value == "task" and out["kind"].confidence == pytest.approx(0.8)
    assert out["length"].value == 3.0 and out["length"].confidence_kind == "score"


def test_systemone_state_field_and_missing_answers(fake_server):
    srv = fake_server(lambda p, b: (200, {"answers": {"multi_step": {"noul": 0.5}}}))
    clf = SystemOneClassifier("x", srv.url + "/v1/systemone", state_field="body")
    out = clf.ask("hello", QS)
    assert srv.requests[0][1]["state"] == {"body": "hello"}
    assert "model" not in srv.requests[0][1]
    assert set(out) == {"multi_step"}          # absent answers are abstentions, not errors


@pytest.mark.parametrize("status,obj", [(500, {"error": "boom"}), (200, b"not json"),
                                        (200, {"no_answers": True})])
def test_systemone_failures_raise_classifier_error(fake_server, status, obj):
    srv = fake_server(lambda p, b: (status, obj))
    with pytest.raises(ClassifierError):
        SystemOneClassifier("x", srv.url).ask("hello", QS)


def test_systemone_unreachable():
    with pytest.raises(ClassifierError):
        SystemOneClassifier("x", "http://127.0.0.1:9", timeout_s=0.5).ask("hello", QS)


def test_rule_classifier_matches_or_abstains():
    clf = RuleClassifier("rule", {"multi_step": [{"pattern": r"(?i)\bthen\b", "answer": "yes"}],
                                  "kind": [{"pattern": r"\?$", "answer": "question"}]})
    out = clf.ask("Draft the release notes, then email them.", QS)
    assert out["multi_step"].value == 1.0 and "kind" not in out
    out = clf.ask("What time is the backup?", QS)
    assert out["kind"].value == "question" and "multi_step" not in out


def test_static_and_build():
    clf = build_classifier("s", {"kind": "static", "answers": {"multi_step": {"noul": 0.1}}})
    assert clf.ask("x", QS)["multi_step"].value == 0.1
    with pytest.raises(ValueError):
        build_classifier("bad", {"kind": "nope"})
    with pytest.raises(ClassifierError):
        StaticClassifier("down", {}, fail="offline").ask("x", QS)
