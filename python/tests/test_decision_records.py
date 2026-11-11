"""Shared lookup fixtures; every transport is synthetic and in process."""

import json
from pathlib import Path

import httpx
import pytest

from system_zero import AuthenticationError, NotFoundError, SystemZero
from system_zero.decision_records import (
    DECISION_RECORD_ERROR_CODES, DecisionNotFoundError, DecisionRecords,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
CONTRACT = json.loads((FIXTURES / "decision-records" / "contract.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", CONTRACT["decision_fixtures"])
def test_retained_reply_is_unchanged_and_uses_authenticated_get(name):
    decision = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json=decision, headers=CONTRACT["response_headers"])

    with SystemZero(api_key="test-key", base_url="https://sdk.example.com/v1/",
                    transport=httpx.MockTransport(send)) as client:
        records = DecisionRecords(client)
        result = records.get_decision_with_response(CONTRACT["id"], client_request_id="synthetic-client")
        assert records.getDecision(CONTRACT["id"]) == decision
    assert result.data == decision
    assert result.metadata.request_id == "req_synthetic_lookup"
    assert result.metadata.client_request_id == "synthetic-client"
    assert result.metadata.replayed is False
    assert result.headers["x-request-id"] == "req_synthetic_lookup"
    assert len(calls) == 2
    for request in calls:
        assert str(request.url) == "https://sdk.example.com/v1" + CONTRACT["decision_path"]
        assert request.method == "GET"
        assert request.content == b""
        assert request.headers["authorization"] == "Bearer test-key"
        assert "idempotency-key" not in request.headers


@pytest.mark.parametrize("variant", [{}, *CONTRACT["trace_variants"]])
def test_trace_keeps_open_nulls_and_refusal_codes(variant):
    trace = {**CONTRACT["trace"], **variant}
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json=trace, headers=CONTRACT["response_headers"])

    with SystemZero(api_key="test-key", transport=httpx.MockTransport(send)) as client:
        records = DecisionRecords(client)
        assert records.get_decision_trace(CONTRACT["id"]) == trace
        assert records.getDecisionTrace(CONTRACT["id"]) == trace
        result = records.get_decision_trace_with_response(CONTRACT["id"])
    assert result.data == trace
    assert result.data["usage_units"]["cached_media_tokens"] is None
    assert result.data["timings"] == {"queue_ms": None, "node_ms": None, "settle_ms": None}
    assert result.data["attachments"] is None
    assert "answers" not in result.data
    assert all(request.url.path == "/v1" + CONTRACT["trace_path"] for request in calls)


@pytest.mark.parametrize("method", ["get_decision", "get_decision_trace", "getDecision", "getDecisionTrace"])
def test_absent_or_expired_decision_is_typed_nonretryable_not_found(method):
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        return httpx.Response(404, json=CONTRACT["not_found"], headers=CONTRACT["not_found_headers"])

    with SystemZero(api_key="test-key", transport=httpx.MockTransport(send), sleep=sleeps.append) as client:
        with pytest.raises(DecisionNotFoundError) as caught:
            getattr(DecisionRecords(client), method)(CONTRACT["id"])
    error = caught.value
    assert isinstance(error, NotFoundError)
    assert error.status == 404
    assert error.type == "not_found_error"
    assert error.code == "decision_not_found"
    assert error.retryable is False
    assert error.request_id == CONTRACT["not_found"]["request_id"]
    assert error.headers["x-request-id"] == "req_synthetic_lookup"
    assert len(calls) == 1
    assert sleeps == []


def test_existing_transport_retries_read_only_lookup_without_a_key():
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"message": "Synthetic capacity outage", "type": "api_error",
                                                    "code": "service_unavailable", "retryable": True}},
                                  headers={"retry-after-ms": "5"})
        return httpx.Response(200, json=CONTRACT["trace"])

    with SystemZero(api_key="test-key", transport=httpx.MockTransport(send),
                    sleep=sleeps.append, random=lambda: 0) as client:
        assert DecisionRecords(client).get_decision_trace(CONTRACT["id"]) == CONTRACT["trace"]
    assert len(calls) == 2
    assert sleeps == [0.005]
    assert all(request.method == "GET" and not request.content and "idempotency-key" not in request.headers for request in calls)


def test_other_typed_errors_are_unchanged():
    body = {"error": {"message": "Synthetic auth refusal", "type": "authentication_error",
                      "code": "invalid_api_key", "retryable": False}, "request_id": "req_synthetic_auth"}
    with SystemZero(api_key="test-key", transport=httpx.MockTransport(lambda _: httpx.Response(401, json=body))) as client:
        with pytest.raises(AuthenticationError) as caught:
            DecisionRecords(client).get_decision(CONTRACT["id"])
    assert caught.value.code == "invalid_api_key"
    assert caught.value.request_id == "req_synthetic_auth"


@pytest.mark.parametrize("value", CONTRACT["invalid_ids"])
def test_invalid_id_never_dispatches(value):
    calls = []
    with SystemZero(api_key="test-key", transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
        records = DecisionRecords(client)
        with pytest.raises(ValueError):
            records.get_decision(value)
        with pytest.raises(ValueError):
            records.get_decision_trace(value)
    assert calls == []


def test_stream_decision_id_uses_the_same_lookup():
    decision = json.loads((FIXTURES / "decide-v1-boolean.json").read_text(encoding="utf-8"))
    frame = {"type": "decision", "state_version": 7, "reply": decision}
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json=decision)

    with SystemZero(api_key="test-key", transport=httpx.MockTransport(send)) as client:
        assert DecisionRecords(client).get_decision(frame["reply"]["id"]) == decision
    assert calls[0].url.path == "/v1" + CONTRACT["decision_path"]


def test_new_code_contract():
    assert DECISION_RECORD_ERROR_CODES["decision_not_found"] == {"status": 404, "type": "not_found_error", "retryable": False}
