"""SYNTHETIC release-scope and shared abstention reader controls."""
import json
from pathlib import Path
import pytest
from rightnow.decisions import decision_for, decisions_for
from rightnow.errors import InvalidRequestError
from rightnow.policy import policy_decision_for, validate_policy
from rightnow.evaluate import score_reply
from rightnow.flows import run_flow
from rightnow import SystemZero
import httpx

FIXTURES = json.loads((Path(__file__).parents[2] / "fixtures/policy/answers.json").read_text(encoding="utf-8"))

@pytest.mark.parametrize("policy,param", [
    ({"on_low_confidence": "error"}, "policy.on_low_confidence"),
    ({"require_calibrated": True}, "policy.require_calibrated"),
    ({"require_calibrated": False}, "policy.require_calibrated"),
])
def test_held_members_are_refused(policy, param):
    with pytest.raises(InvalidRequestError) as caught:
        validate_policy(policy)
    assert caught.value.param == param

@pytest.mark.parametrize("case", FIXTURES["abstained"], ids=lambda case: case["name"])
def test_shared_reader_preserves_abstention(case):
    result = decision_for(case["answer"])
    assert result == policy_decision_for(case["answer"])
    assert not hasattr(result, "decision")
    assert decisions_for({"answers": {"q": case["answer"]}})["q"].kind == "abstained"

def test_evaluation_keeps_abstention_separate():
    scores = score_reply({"questions": {"q": {"type": "boolean", "instructions": "SYNTHETIC"}}},
                         {"q": True}, {"answers": {"q": FIXTURES["abstained"][0]["answer"]}})
    assert scores[0]["outcome"] == "abstained"
    assert scores[0]["correct"] is False
    assert scores[0]["confidence"] is None

def test_flow_preserves_abstention_without_a_decision():
    reply = {"id": "szd_synthetic", "model": "rightnow-decision", "served_by": "synthetic", "created": 1,
             "context": {"route": "direct", "tokens": 1}, "answers": {"q": FIXTURES["abstained"][0]["answer"]},
             "usage": {"input_tokens": 1, "cached_input_tokens": 0, "media_tokens": 0,
                       "long_input_tokens": 0, "media_items": 0, "thought_tokens": 1}}
    with SystemZero(api_key="SYNTHETIC", max_retries=0, timeout=1,
                    transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply))) as client:
        trace = run_flow(client, {"version": 1, "request": {"model": "rightnow-decision", "state": "SYNTHETIC"},
                                  "steps": [{"id": "first", "questions": {"q": {"type": "boolean", "instructions": "SYNTHETIC"}},
                                             "next": {"rules": [], "default": None}}]},
                         run_id="policy-on-flow", deadline_ms=10000, include_content=True, now=lambda: 0)
    assert trace["status"] == "completed"
    assert trace["steps"][0]["answers"][0]["kind"] == "abstained"
    assert "decision" not in trace["steps"][0]["answers"][0]
