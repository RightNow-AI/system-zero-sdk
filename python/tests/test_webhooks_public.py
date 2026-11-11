"""Public package/client webhook wiring uses the unchanged authenticated transport."""

import json
from pathlib import Path

import httpx
import pytest

import system_zero as sdk
from system_zero.webhooks import Webhooks, verify_webhook_signature
from system_zero.webhook_errors import WEBHOOK_ERROR_CODES
from system_zero._retry import _retry_context


FIXTURES = Path(__file__).parents[2] / "fixtures" / "webhooks"
MANAGEMENT = json.loads((FIXTURES / "management.json").read_text(encoding="utf-8"), object_hook=lambda obj: {key: bytes(value).decode("ascii") if key in {"secret", "signature"} and isinstance(value, list) else value for key, value in obj.items()})
SIGNATURES = json.loads((FIXTURES / "signatures.json").read_text(encoding="utf-8"), object_hook=lambda obj: {key: bytes(value).decode("ascii") if key in {"secret", "signature"} and isinstance(value, list) else value for key, value in obj.items()})
ENDPOINT = MANAGEMENT["endpoint"]
CREATE = {"url": ENDPOINT["url"], "events": ["decision.completed"]}


def test_public_resource_helpers_errors_and_types_are_exported():
    assert sdk.Webhooks is Webhooks
    assert sdk.verify_webhook_signature is verify_webhook_signature
    assert sdk.WEBHOOK_ERROR_CODES is WEBHOOK_ERROR_CODES
    assert sdk.WEBHOOK_MAX_BODY_BYTES == 16384
    assert sdk.WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS == 300
    for name in ["Webhooks", "verify_webhook_signature", "WEBHOOK_ERROR_CODES", "WebhookInvalidError",
                 "WebhookNotFoundError", "WebhookLimitExceededError", "WebhookUnavailableError", "WebhookEndpoint",
                 "WebhookEndpointWithSecret", "WebhookCreateRequest", "WebhookEndpointList", "WebhookDeleted",
                 "WebhookDelivery", "WebhookDeliveryList", "WebhookUsageUnits", "WebhookInputClass", "WebhookDecisionData",
                 "WebhookStreamClosedData", "WebhookDecisionEvent", "WebhookStreamClosedEvent", "WebhookEvent",
                 "WebhookEventType", "WebhookStreamCloseReason", "WebhookBody"]:
        assert getattr(sdk, name) is not None
        assert name in sdk.__all__
    assert sdk.verify_webhook_signature(SIGNATURES["body"], SIGNATURES["signature"], SIGNATURES["secret"], now=SIGNATURES["timestamp"])


def test_public_client_all_operations_authentication_and_metadata():
    calls = []
    replies = iter([(201, {**ENDPOINT, "secret": SIGNATURES["secret"]}), (200, {"object": "list", "data": [ENDPOINT]}),
                    (200, {"id": ENDPOINT["id"], "deleted": True}), (200, {**ENDPOINT, "secret": SIGNATURES["secret"]}),
                    (200, MANAGEMENT["deliveries"])])

    def send(request):
        calls.append(request)
        status, data = next(replies)
        return httpx.Response(status, json=data, headers={"x-request-id": "req_webhook", "x-client-request-id": "client_webhook",
                                                        "x-ratelimit-limit": "100", "x-ratelimit-remaining": "99",
                                                        "x-ratelimit-reset": "1770000060", "x-latency-ms": "7"})

    with sdk.SystemZero(api_key="test-key", base_url="https://gateway.example/v1", transport=httpx.MockTransport(send)) as client:
        assert isinstance(client.webhooks, sdk.Webhooks)
        created = client.webhooks.create_with_response(CREATE, client_request_id="client_webhook", headers={"x-custom": "synthetic"})
        assert created.data == {**ENDPOINT, "secret": SIGNATURES["secret"]}
        assert created.status == 201
        assert created.metadata.request_id == "req_webhook"
        assert created.metadata.client_request_id == "client_webhook"
        assert created.metadata.rate_limit.limit == 100
        assert created.metadata.rate_limit.remaining == 99
        assert created.metadata.latency_ms == 7
        assert client.webhooks.list() == {"object": "list", "data": [ENDPOINT]}
        assert client.webhooks.delete(ENDPOINT["id"]) == {"id": ENDPOINT["id"], "deleted": True}
        assert client.webhooks.rotate_secret(ENDPOINT["id"]) == {**ENDPOINT, "secret": SIGNATURES["secret"]}
        assert client.webhooks.deliveries(ENDPOINT["id"], limit=1) == MANAGEMENT["deliveries"]
    assert [(call.method, call.url.path) for call in calls] == [
        ("POST", "/v1/system-zero/webhooks"), ("GET", "/v1/system-zero/webhooks"),
        ("DELETE", "/v1/system-zero/webhooks/" + ENDPOINT["id"]),
        ("POST", "/v1/system-zero/webhooks/" + ENDPOINT["id"] + "/rotate-secret"),
        ("GET", "/v1/system-zero/webhooks/" + ENDPOINT["id"] + "/deliveries"),
    ]
    assert all(call.headers["authorization"] == "Bearer test-key" for call in calls)
    assert all("idempotency-key" not in call.headers for call in calls)
    assert calls[0].headers["x-custom"] == "synthetic"
    assert calls[0].headers["x-client-request-id"] == "client_webhook"
    assert json.loads(calls[0].content) == CREATE
    assert calls[3].content == b"{}"
    assert calls[4].url.params["limit"] == "1"


@pytest.mark.parametrize("method", ["create", "rotate_secret"])
@pytest.mark.parametrize("failure", ["status", "connection"])
def test_public_secret_returning_methods_never_retry(method, failure):
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        if failure == "connection":
            raise httpx.ConnectError("SYNTHETIC ambiguous result", request=request)
        return httpx.Response(503, headers={"x-should-retry": "true", "retry-after-ms": "0"}, json={
            "error": {"message": "SYNTHETIC unavailable", "code": "webhook_unavailable", "type": "overloaded_error", "retryable": True}})

    with sdk.SystemZero(api_key="test-key", base_url="https://gateway.example/v1", transport=httpx.MockTransport(send),
                        max_retries=3, sleep=sleeps.append) as client:
        with pytest.raises(sdk.APIConnectionError if failure == "connection" else sdk.WebhookUnavailableError):
            if method == "create":
                client.webhooks.create(CREATE)
            else:
                client.webhooks.rotate_secret(ENDPOINT["id"])
    assert len(calls) == 1
    assert sleeps == []


def test_public_reads_keep_existing_bounded_retry_policy():
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        if len(calls) == 1:
            return capacity_unavailable()
        return httpx.Response(200, json={"object": "list", "data": [ENDPOINT]})

    with sdk.SystemZero(api_key="test-key", base_url="https://gateway.example/v1", transport=httpx.MockTransport(send),
                        max_retries=2, sleep=sleeps.append, random=lambda: 0) as client:
        assert client.webhooks.list() == {"object": "list", "data": [ENDPOINT]}
    assert len(calls) == 2
    assert sleeps == [0.005]


@pytest.mark.parametrize("entry", SIGNATURES["errors"], ids=lambda entry: entry["code"])
def test_public_typed_http_error_preserves_fields_and_retry_metadata(entry):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(entry["status"], headers={"x-request-id": "req_header", "x-should-retry": "false", "retry-after-ms": "17"}, json={
            "request_id": "req_body", "error": {"message": "SYNTHETIC refusal", "code": entry["code"], "type": entry["type"],
                "retryable": entry["retryable"], "param": "url", "details": {"limit": 5}}})

    with sdk.SystemZero(api_key="test-key", base_url="https://gateway.example/v1", transport=httpx.MockTransport(send), max_retries=3) as client:
        with pytest.raises(getattr(sdk, entry["class_name"])) as caught:
            client.webhooks.list()
    error = caught.value
    assert error.code == entry["code"]
    assert error.status == entry["status"]
    assert error.type == entry["type"]
    assert error.retryable is entry["retryable"]
    assert error.param == "url"
    assert error.details == {"limit": 5}
    assert error.request_id == "req_body"
    assert error.headers["x-request-id"] == "req_header"
    assert error.headers["retry-after-ms"] == "17"
    assert error.headers["x-should-retry"] == "false"
    assert len(calls) == 1


def test_public_management_refuses_idempotency_before_transport():
    calls = []
    with sdk.SystemZero(api_key="test-key", base_url="https://gateway.example/v1", transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
        with pytest.raises(sdk.WebhookInvalidError):
            client.webhooks.list(headers={"Idempotency-Key": "forbidden"})
    assert calls == []


def test_ordinary_decide_retry_bytes_and_generated_idempotency_are_unchanged():
    calls = []

    def send(request):
        calls.append(request)
        if len(calls) == 1:
            return capacity_unavailable()
        return httpx.Response(200, json={"object": "synthetic_decision"})

    with sdk.SystemZero(api_key="test-key", base_url="https://gateway.example/v1", transport=httpx.MockTransport(send),
                        max_retries=2, sleep=lambda _: None, random=lambda: 0) as client:
        client.decide({"state": "SYNTHETIC state", "questions": []})
    assert len(calls) == 2
    assert calls[0].url.path == "/v1/system-zero/decide"
    assert calls[0].content == calls[1].content
    assert calls[0].headers["idempotency-key"].startswith("sz-sdk-")
    assert calls[1].headers["idempotency-key"] == calls[0].headers["idempotency-key"]


METHODS = [
    ("create", (CREATE,)), ("create_with_response", (CREATE,)),
    ("list", ()), ("list_with_response", ()),
    ("delete", (ENDPOINT["id"],)), ("delete_with_response", (ENDPOINT["id"],)),
    ("rotate_secret", (ENDPOINT["id"],)), ("rotate_secret_with_response", (ENDPOINT["id"],)),
    ("deliveries", (ENDPOINT["id"],)), ("deliveries_with_response", (ENDPOINT["id"],)),
]


def capacity_unavailable():
    return httpx.Response(503, headers={"retry-after-ms": "5"}, json={
        "error": {"message": "SYNTHETIC capacity", "code": "service_unavailable",
                  "type": "server_error", "retryable": True}})


@pytest.mark.parametrize("method,args", METHODS, ids=[method for method, _ in METHODS])
def test_c_rebase_every_webhook_method_accepts_and_preserves_zero_retry_budget(method, args):
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        return capacity_unavailable()

    with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(send),
                        max_retries=3, sleep=sleeps.append, random=lambda: 0) as client:
        with pytest.raises(sdk.SystemZeroError) as caught:
            getattr(client.webhooks, method)(*args, retry_budget=0)
    assert caught.value.code == "service_unavailable"
    assert len(calls) == 1
    assert sleeps == []
    assert "idempotency-key" not in calls[0].headers
    assert "x-sz-capabilities" not in calls[0].headers


@pytest.mark.parametrize("method,args", [("list", ()), ("deliveries", (ENDPOINT["id"],))])
def test_c_rebase_gets_can_override_client_budget_and_preserve_pacing(method, args):
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        return capacity_unavailable() if len(calls) == 1 else httpx.Response(200, json={"object": "list", "data": []})

    with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(send),
                        max_retries=2, retry_budget=0, sleep=sleeps.append, random=lambda: 0) as client:
        assert getattr(client.webhooks, method)(*args, retry_budget=1) == {"object": "list", "data": []}
    assert len(calls) == 2
    assert sleeps == [0.005]


@pytest.mark.parametrize("method,args", [("list", ()), ("deliveries", (ENDPOINT["id"],))])
def test_c_rebase_webhook_reads_keep_workflow_context(method, args):
    calls, sleeps, attempts = [], [], []

    def send(request):
        calls.append(request)
        return capacity_unavailable()

    token = _retry_context.set({"workflow_recovery": True, "on_attempt": lambda: attempts.append(True)})
    try:
        with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(send),
                            max_retries=2, sleep=sleeps.append) as client:
            with pytest.raises(sdk.SystemZeroError) as caught:
                getattr(client.webhooks, method)(*args)
        assert caught.value.code == "service_unavailable"
        assert len(calls) == 1
        assert attempts == [True]
        assert sleeps == []
    finally:
        _retry_context.reset(token)


@pytest.mark.parametrize("method,args", [("create", (CREATE,)), ("rotate_secret", (ENDPOINT["id"],))])
def test_c_rebase_secret_returning_methods_never_repeat_eligible_capacity_failure_with_context(method, args):
    calls, sleeps, attempts = [], [], []

    def send(request):
        calls.append(request)
        return capacity_unavailable()

    token = _retry_context.set({"max_retries": 5, "on_attempt": lambda: attempts.append(True)})
    try:
        with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(send),
                            max_retries=5, sleep=sleeps.append) as client:
            with pytest.raises(sdk.SystemZeroError) as caught:
                getattr(client.webhooks, method)(*args)
        assert caught.value.code == "service_unavailable"
        assert len(calls) == 1
        assert attempts == [True]
        assert sleeps == []
    finally:
        _retry_context.reset(token)


def test_c_rebase_webhook_unavailable_retries_within_bounds_with_original_retryable_metadata():
    calls, sleeps = [], []

    def send(request):
        calls.append(request)
        return httpx.Response(503, headers={"x-should-retry": "true", "retry-after-ms": "0"}, json={
            "error": {"message": "SYNTHETIC unavailable", "code": "webhook_unavailable",
                      "type": "overloaded_error", "retryable": True}})

    with sdk.SystemZero(api_key="test-key", transport=httpx.MockTransport(send),
                        max_retries=3, sleep=sleeps.append) as client:
        with pytest.raises(sdk.WebhookUnavailableError) as caught:
            client.webhooks.list()
    assert caught.value.retryable is True
    assert len(calls) == 4
    assert len(sleeps) == 3
