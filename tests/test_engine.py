from brain_surgery.classify import StaticClassifier
from brain_surgery.engine import decide

TEXT = {"text": "Rename the backup job and update its schedule."}


def noul(**p):
    return {k: {"noul": v} for k, v in p.items()}


def test_confident_first_tier_stops_cascade(size_spec):
    fast = StaticClassifier("fast", noul(multi_step=0.02, writes_code=0.03))
    slow = StaticClassifier("slow", noul(multi_step=0.5, writes_code=0.5))
    d = decide(size_spec, {"fast": fast, "slow": slow}, TEXT, {"prompt_chars": 40})
    assert d.outcome == "small" and d.resolved and d.tier_resolved == "fast"
    assert fast.calls == [["multi_step", "writes_code"]]      # both questions, one call
    assert slow.calls == []


def test_band_answers_escalate_only_pending_questions(size_spec):
    # fast: multi_step confidently high, writes_code in the band -> only writes_code goes on
    fast = StaticClassifier("fast", noul(multi_step=0.95, writes_code=0.5))
    slow = StaticClassifier("slow", noul(multi_step=0.0, writes_code=0.8))
    d = decide(size_spec, {"fast": fast, "slow": slow}, TEXT, {"prompt_chars": 40})
    assert slow.calls == [["writes_code"]]
    assert d.outcome == "large" and d.tier_resolved == "slow"
    assert d.answers["multi_step"].tier == "fast" and d.answers["writes_code"].tier == "slow"


def test_unknown_earlier_row_blocks_a_looser_later_row(size_spec):
    # multi_step high is known; the stricter 'large' row needs writes_code, still unknown.
    # The looser 'medium' row matches already, but must NOT fire before writes_code resolves.
    fast = StaticClassifier("fast", noul(multi_step=0.95, writes_code=0.5))
    d = decide(size_spec, {"fast": fast}, TEXT, {"prompt_chars": 40})
    assert d.outcome == "unsure" and not d.resolved
    assert d.escalate_to == "large"


def test_fact_decides_without_asking(size_spec):
    fast = StaticClassifier("fast", noul(multi_step=0.0, writes_code=0.0))
    d = decide(size_spec, {"fast": fast}, TEXT, {"prompt_chars": 5000})
    assert d.outcome == "large" and fast.calls == [] and d.tiers_asked == []


def test_all_tiers_down_escalates(size_spec):
    tiers = {"fast": StaticClassifier("fast", {}, fail="offline"),
             "slow": StaticClassifier("slow", {}, fail="offline")}
    d = decide(size_spec, tiers, TEXT, {"prompt_chars": 40})
    assert d.outcome == "unsure" and set(d.errors) == {"fast", "slow"}
    assert all(e["abstain_reason"] == "error" for e in d.events)


def test_broken_adapter_does_not_raise(size_spec):
    class Broken:
        name, model_id = "fast", None

        def ask(self, state, questions):
            raise RuntimeError("bug")

    d = decide(size_spec, {"fast": Broken()}, TEXT, {"prompt_chars": 40})
    assert d.outcome == "unsure" and "RuntimeError" in d.errors["fast"]


def test_unconfigured_tiers_are_skipped(size_spec):
    slow = StaticClassifier("slow", noul(multi_step=0.0, writes_code=0.1))
    d = decide(size_spec, {"slow": slow}, TEXT, {"prompt_chars": 40})
    assert d.tiers_asked == ["slow"] and d.outcome == "small"


def test_missing_threshold_never_confident(size_spec):
    size_spec.thresholds["fast"]["multi_step"] = {"hi": None, "lo": None}
    fast = StaticClassifier("fast", noul(multi_step=0.99, writes_code=0.99))
    d = decide(size_spec, {"fast": fast}, TEXT, {"prompt_chars": 40})
    assert d.outcome == "unsure"
    reasons = {e["question_id"]: e["abstain_reason"] for e in d.events}
    assert reasons["multi_step"] == "no_threshold"


def test_events_have_cascade_row_shape(size_spec):
    fast = StaticClassifier("fast", noul(multi_step=0.02, writes_code=0.03), model_id="f-1")
    d = decide(size_spec, {"fast": fast}, TEXT, {"prompt_chars": 40})
    e = d.events[0]
    for key in ("event_id", "decision_id", "spec_id", "spec_version", "question_id", "tier",
                "model_id", "qtype", "answer", "answer_confidence", "confidence_kind",
                "accepted", "abstain_reason", "outcome", "latency_ms"):
        assert key in e
    assert e["model_id"] == "f-1" and e["outcome"] == "small" and e["accepted"] is True
    assert e["decision_id"] == d.decision_id
