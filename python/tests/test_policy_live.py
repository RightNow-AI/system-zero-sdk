"""Ordinary request policy preserves abstentions and uncharged refusals."""
import asyncio
import json
import httpx
import pytest
from rightnow import SystemZero, stream_client, decisions_for, InvalidRequestError, PolicyMaxCostExceededError
from rightnow import stream as stream_module

REQUEST = {"model": "rightnow-decision", "state": "SYNTHETIC shipment.",
           "questions": {"ready": {"type": "boolean", "instructions": "Has it arrived?"}}}
POLICY = {"min_confidence": 0.8, "max_cost_usd": 0.01}


def test_policy_round_trip_and_abstention():
    sent = []
    answer = {"type": "boolean", "abstained": True, "abstention_reason": "low_confidence",
              "probability": 0.6, "confidence": 0.6,
              "alternatives": [{"decision": False, "confidence": 0.4}],
              "calibrated": False, "calibration_version": "synthetic"}
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"answers": {"ready": answer}, "policy": POLICY})
    with SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(respond)) as client:
        reply = client.decide({**REQUEST, "policy": POLICY})
    assert sent[0]["policy"] == reply["policy"] == POLICY
    result = decisions_for(reply)["ready"]
    assert result.kind == "abstained" and result.confidence == 0.6
    with pytest.raises(AttributeError):
        _ = result.decision


@pytest.mark.parametrize("policy,param", [
    ({"on_low_confidence": "error"}, "policy.on_low_confidence"),
    ({"require_calibrated": False}, "policy.require_calibrated"),
    ({"min_confidence": 0}, "policy.min_confidence"),
])
def test_invalid_policy_never_sends(policy, param):
    def unexpected(request):
        raise AssertionError("Unexpected HTTP attempt")
    with SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(InvalidRequestError) as caught:
            client.decide({**REQUEST, "policy": policy})
    assert caught.value.code == "invalid_request" and caught.value.param == param


def test_over_budget_is_uncharged():
    body = {"error": {"code": "policy_max_cost_exceeded", "type": "invalid_request_error",
                      "message": "SYNTHETIC cap.", "param": "policy.max_cost_usd",
                      "retryable": False, "details": {"counted": False}}}
    with SystemZero(api_key="SYNTHETIC", max_retries=0,
                    transport=httpx.MockTransport(lambda request: httpx.Response(400, json=body))) as client:
        with pytest.raises(PolicyMaxCostExceededError) as caught:
            client.decide({**REQUEST, "policy": POLICY})
    assert caught.value.status == 400 and caught.value.counted is False
    assert caught.value.param == "policy.max_cost_usd"


def test_stream_policy_is_validated_before_connect(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Unexpected stream connection")
    monkeypatch.setattr(stream_module, "connect", unexpected)
    async def scenario():
        client = stream_client(api_key="SYNTHETIC")
        try:
            with pytest.raises(InvalidRequestError) as caught:
                await client.start({**REQUEST, "policy": {"on_low_confidence": "error"}})
            assert caught.value.param == "policy.on_low_confidence"
            assert client.state_version == 0
        finally:
            await client.close()
    asyncio.run(scenario())
