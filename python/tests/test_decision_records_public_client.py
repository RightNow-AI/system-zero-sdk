"""Public client integration with synthetic transports and retained fixtures."""

import json
from pathlib import Path
from typing import Literal, get_type_hints

import httpx
import pytest

import system_zero as sdk
from system_zero.error_codes import (
    GATEWAY_ERROR_CODES as PINNED_GATEWAY_CODES,
    SYSTEM_ZERO_ERROR_CODES as PINNED_SYSTEM_CODES,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
CONTRACT = json.loads((FIXTURES / "decision-records" / "contract.json").read_text(encoding="utf-8"))
DECISION = json.loads((FIXTURES / "decide-v1-boolean.json").read_text(encoding="utf-8"))
METHODS = [
    ("get_decision", "decision_path", DECISION, False),
    ("getDecision", "decision_path", DECISION, False),
    ("get_decision_trace", "trace_path", CONTRACT["trace"], False),
    ("getDecisionTrace", "trace_path", CONTRACT["trace"], False),
    ("get_decision_with_response", "decision_path", DECISION, True),
    ("get_decision_trace_with_response", "trace_path", CONTRACT["trace"], True),
]
HEADERS = {
    "x-request-id": "req_public_lookup", "x-client-request-id": "public-client",
    "x-ratelimit-limit": "120", "x-ratelimit-remaining": "119", "x-ratelimit-reset": "1800000000",
}


@pytest.mark.parametrize("method,path_name,payload,with_response", METHODS)
def test_public_methods_use_configured_get_transport_and_response_metadata(method, path_name, payload, with_response):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json=payload, headers=HEADERS)

    options = {"authorization": "Bearer test", "x-synthetic": "public"}
    with sdk.SystemZero(api_key="test-key", base_url="https://sdk.example.com/v1/",
                        transport=httpx.MockTransport(send)) as client:
        result = getattr(client, method)(CONTRACT["id"], client_request_id="public-client", headers=options)
    if with_response:
        assert result.data == payload
        assert result.status == 200
        assert result.metadata.request_id == "req_public_lookup"
        assert result.metadata.client_request_id == "public-client"
        assert result.metadata.rate_limit.limit == 120
        assert result.metadata.rate_limit.remaining == 119
        assert result.metadata.rate_limit.reset == 1800000000
        assert result.metadata.replayed is False
        assert result.metadata.replay_header is None
        assert "x-rightnow-idempotent-replay" not in result.headers
        assert "x-should-retry" not in result.headers
    else:
        assert result == payload
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://sdk.example.com/v1" + CONTRACT[path_name]
    assert request.method == "GET"
    assert request.content == b""
    assert request.headers["authorization"] == "Bearer test-key"
    assert request.headers["x-client-request-id"] == "public-client"
    assert request.headers["x-synthetic"] == "public"
    assert "idempotency-key" not in request.headers
    assert options["authorization"] == "Bearer test"


@pytest.mark.parametrize("method,payload", [("get_decision", DECISION), ("get_decision_trace", CONTRACT["trace"])])
def test_public_methods_reuse_existing_get_retries_and_server_pacing(method, payload):
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {
                "type": "api_error", "code": "service_unavailable", "message": "Synthetic capacity outage", "retryable": True,
            }}, headers={"retry-after-ms": "5"})
        return httpx.Response(200, json=payload)

    with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(send),
                        sleep=sleeps.append, random=lambda: 0) as client:
        assert getattr(client, method)(CONTRACT["id"]) == payload
    assert len(calls) == 2
    assert sleeps == [0.005]
    assert all(request.method == "GET" and not request.content and "idempotency-key" not in request.headers for request in calls)


@pytest.mark.parametrize("method", ["get_decision", "get_decision_trace"])
def test_public_lookup_retries_marked_store_failure_inside_shared_bounds(method):
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {
            "type": "api_error", "code": "idempotency_unavailable", "message": "Synthetic store outage", "retryable": True,
        }}, headers={"retry-after-ms": "5"})

    with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(send), sleep=sleeps.append) as client:
        with pytest.raises(sdk.APIError) as caught:
            getattr(client, method)(CONTRACT["id"])
    error = caught.value
    assert (error.status, error.code, error.retryable, error.message) == (503, "idempotency_unavailable", True, "Synthetic store outage")
    assert len(calls) == 6
    assert len(sleeps) == 5


@pytest.mark.parametrize("method,path_name,payload,with_response", METHODS)
def test_public_lookup_forwards_per_call_budget_to_existing_transport(method, path_name, payload, with_response):
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"code": "service_unavailable", "retryable": True}},
                                  headers={"retry-after-ms": "5"})
        return httpx.Response(200, json=payload)

    with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(send),
                        sleep=sleeps.append, random=lambda: 0, monotonic=lambda: 0, retry_budget=0) as client:
        # Keep the forwarding check independent of a 10 ms scheduler pause.
        # The following test covers exact refusal at the deadline boundary.
        result = getattr(client, method)(CONTRACT["id"], retry_budget=1)
    assert (result.data if with_response else result) == payload
    assert len(calls) == 2
    assert sleeps == [0.005]
    assert all(str(request.url).endswith(CONTRACT[path_name]) and "idempotency-key" not in request.headers for request in calls)


@pytest.mark.parametrize("method", ["get_decision", "get_decision_trace"])
@pytest.mark.parametrize("retry_budget", [0, 0.005])
def test_public_lookup_budget_cannot_admit_a_wait_that_reaches_its_deadline(method, retry_budget):
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"code": "service_unavailable", "retryable": True}},
                              headers={"retry-after-ms": "5"})

    with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(send),
                        sleep=sleeps.append, random=lambda: 0, monotonic=lambda: 0) as client:
        with pytest.raises(sdk.SystemZeroError) as caught:
            getattr(client, method)(CONTRACT["id"], retry_budget=retry_budget)
    assert (caught.value.status, caught.value.code) == (503, "service_unavailable")
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("method", [row[0] for row in METHODS])
def test_public_methods_raise_public_typed_404_without_retrying(method):
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        return httpx.Response(404, json=CONTRACT["not_found"], headers={**CONTRACT["not_found_headers"], **HEADERS})

    with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(send), sleep=sleeps.append) as client:
        with pytest.raises(sdk.DecisionNotFoundError) as caught:
            getattr(client, method)(CONTRACT["id"])
    error = caught.value
    assert isinstance(error, sdk.NotFoundError)
    assert (error.status, error.type, error.code, error.retryable) == (404, "not_found_error", "decision_not_found", False)
    assert error.request_id == CONTRACT["not_found"]["request_id"]
    assert error.headers["x-request-id"] == "req_public_lookup"
    assert len(calls) == 1
    assert sleeps == []


def test_public_lookup_preserves_revocation_response():
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(403, json={
            "error": {"type": "authentication_error", "code": "auth_error", "message": "Synthetic revoked key", "retryable": False},
            "request_id": "req_public_revoked",
        }, headers={"x-should-retry": "false"})

    with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(send)) as client:
        with pytest.raises(sdk.AuthenticationError) as caught:
            client.get_decision(CONTRACT["id"])
    error = caught.value
    assert not isinstance(error, sdk.DecisionNotFoundError)
    assert (error.status, error.type, error.code, error.retryable) == (403, "authentication_error", "auth_error", False)
    assert error.request_id == "req_public_revoked"
    assert len(calls) == 1


@pytest.mark.parametrize("value", CONTRACT["invalid_ids"])
def test_public_methods_reject_invalid_id_before_dispatch(value):
    calls = []
    with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
        for method, _, _, _ in METHODS:
            with pytest.raises(ValueError):
                getattr(client, method)(value)
    assert calls == []


def test_public_exports_include_trace_types_and_all_existing_error_codes():
    for name in ("DecisionRecords", "DecisionNotFoundError", "DecisionRecordErrorCode", "DECISION_RECORD_ERROR_CODES",
                 "DecisionTrace", "DecisionTraceAttachment", "DecisionTraceTimings", "DecisionTraceUsage"):
        assert hasattr(sdk, name)
        assert name in sdk.__all__
    assert sdk.SYSTEM_ZERO_ERROR_CODES == PINNED_SYSTEM_CODES
    assert sdk.GATEWAY_ERROR_CODES == PINNED_GATEWAY_CODES
    assert sdk.KNOWN_ERROR_CODES == frozenset(PINNED_SYSTEM_CODES + PINNED_GATEWAY_CODES + ("decision_not_found",))
    assert {item.value for item in sdk.ErrorCode} == sdk.KNOWN_ERROR_CODES
    assert sdk.ErrorCode.DECISION_NOT_FOUND.value == "decision_not_found"
    assert sdk.ErrorCode("decision_not_found") is sdk.ErrorCode.DECISION_NOT_FOUND
    assert sdk.ErrorCode.STREAM_LIMIT_EXCEEDED.value == "stream_limit_exceeded"
    assert get_type_hints(sdk.SystemZero.get_decision)["return"] == sdk.DecideReply
    assert get_type_hints(sdk.SystemZero.get_decision_trace)["return"] == sdk.DecisionTrace
    assert get_type_hints(sdk.SystemZero.get_decision_trace_with_response)["return"] == sdk.ResponseResult[sdk.DecisionTrace]
    trace_hints = get_type_hints(sdk.DecisionTrace)
    assert trace_hints["created"] is int
    assert trace_hints["model"] is str
    assert trace_hints["model_version"] is str
    assert trace_hints["route"] == Literal["direct", "long"]
