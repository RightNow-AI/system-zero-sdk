import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from system_zero import APIError, InvalidRequestError, SystemZero, select_tool, tool_questions


FIXTURE = json.loads((Path(__file__).parents[2] / "fixtures/tool-selection/sz-tools-v1.json").read_text(encoding="utf-8"))
TOOLS = [dict(name="lookup"), dict(name="refund")]
REQUEST = dict(model="system-zero", state="The order is damaged.")


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_compile_fixture(case):
    assert FIXTURE["format"] == "sz-tools-v1"
    if "error" in case:
        with pytest.raises(InvalidRequestError) as caught:
            tool_questions(case["tools"], case["mode"])
        assert caught.value.code == case["error"]
    else:
        compiled = tool_questions(case["tools"], case["mode"])
        assert compiled == case["question"]
        assert json.dumps(compiled) == json.dumps(case["question"])


def reply(winner):
    probabilities = {name: 0.8 if name == winner else 0.1 for name in ("lookup", "refund", "none")}
    return dict(marker="untouched", answers=dict(sz_tool=dict(
        type="choice", choice=winner, probabilities=probabilities, decision=winner, confidence=0.8,
        alternatives=[dict(decision=name, confidence=0.1) for name in sorted(probabilities) if name != winner],
        calibrated=False, calibration_version="unpublished")))


@pytest.mark.parametrize("winner", ["lookup", "none"])
def test_picked_tool_and_none_with_original_reply(winner):
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=reply(winner)))) as client:
        with patch.object(client, "decide", wraps=client.decide) as decide:
            seen = []
            def capture(*args, **kwargs):
                value = client.__class__.decide(client, *args, **kwargs)
                seen.append(value)
                return value
            decide.side_effect = capture
            result = select_tool(client, REQUEST, TOOLS)
    assert result["decision"] == ("none" if winner == "none" else "call")
    assert result["tool"] == (None if winner == "none" else winner)
    assert result["confidence"] == 0.8
    assert result["reply"] is seen[0]
    assert result["reply"] == reply(winner)
    assert result["alternatives"] == [dict(tool=item["decision"], confidence=item["confidence"]) for item in reply(winner)["answers"]["sz_tool"]["alternatives"]]


def test_forced_single_tool_sends_nothing():
    seen = []
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(lambda request: seen.append(request))) as client:
        assert select_tool(client, REQUEST, TOOLS[:1], "required") == dict(decision="call", tool="lookup", confidence=None, alternatives=[], reply=None, forced=True)
    assert seen == []


def test_required_two_tools_keeps_legacy_confidence_unavailable():
    def send(request):
        assert json.loads(request.content)["questions"] == tool_questions(TOOLS, "required")
        return httpx.Response(200, json=dict(answers=dict(sz_tool=dict(type="choice", choice="lookup", probabilities=dict(lookup=0.8, refund=0.2), confidence=0.6, calibration_version="unpublished"))))
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(send)) as client:
        result = select_tool(client, REQUEST, TOOLS, "required")
    assert result["tool"] == "lookup"
    assert result["confidence"] is None
    assert result["alternatives"] == []
    assert result["forced"] is False


def test_invalid_definition_never_sends():
    seen = []
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(lambda request: seen.append(request))) as client:
        with pytest.raises(InvalidRequestError):
            select_tool(client, REQUEST, [dict(name="none")])
    assert seen == []


def test_options_attachments_and_retries_pass_through():
    seen = []
    def send(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(503, json={"error": dict(code="service_unavailable", message="Retry.", retryable=True)})
        return httpx.Response(200, json=reply("refund"))
    with SystemZero(api_key="synthetic", max_retries=1, sleep=lambda _: None, random=lambda: 0, transport=httpx.MockTransport(send)) as client:
        select_tool(client, REQUEST, TOOLS, danger_zone="long_context_1m", idempotency_key="selection-key", client_request_id="selection-client", retry_budget=1, headers={"x-example": "kept"})
        attachments = [dict(name="paper", type="document", data="data:application/pdf;base64,JVBERi0xLjcK")]
        select_tool(client, {**REQUEST, "attachments": attachments}, TOOLS)
    assert len(seen) == 3
    for request in seen[:2]:
        assert json.loads(request.content) == {**REQUEST, "questions": tool_questions(TOOLS)}
        assert request.headers["sz-danger-zone"] == "long_context_1m"
        assert request.headers["idempotency-key"] == "selection-key"
        assert request.headers["x-example"] == "kept"
    assert json.loads(seen[2].content)["attachments"] == attachments


def test_refused_question_stops_at_client_budget():
    seen = []
    def send(request):
        seen.append(request)
        return httpx.Response(503, json={"error": dict(code="service_unavailable", message="Retry.", retryable=True)})
    with SystemZero(api_key="synthetic", max_retries=2, sleep=lambda _: None, random=lambda: 0, transport=httpx.MockTransport(send)) as client:
        with pytest.raises(APIError, match="No decision, possibly refused.") as caught:
            select_tool(client, REQUEST, TOOLS)
    assert caught.value.retryable is False
    assert len(seen) == 3


@pytest.mark.parametrize("answers", [{}, {"sz_tool": {"error": dict(code="question_not_grounded", retryable=False)}}])
def test_no_selection_does_not_retry(answers):
    seen = []
    def send(request):
        seen.append(request)
        return httpx.Response(200, json=dict(answers=answers))
    with SystemZero(api_key="synthetic", transport=httpx.MockTransport(send)) as client:
        with pytest.raises(APIError, match="No decision, possibly refused."):
            select_tool(client, REQUEST, TOOLS)
    assert len(seen) == 1
