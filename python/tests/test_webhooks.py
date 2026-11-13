"""Shared webhook receiver contract and authenticated management resource."""

import json
from pathlib import Path

import httpx
import pytest

from rightnow import SystemZero, SystemZeroError
from rightnow.webhook_errors import WEBHOOK_ERROR_CODES, WebhookInvalidError
from rightnow.webhooks import Webhooks, verify_webhook_signature
from rightnow import types, webhooks


def test_openapi_component_names_share_canonical_webhook_types():
    names = (
        "WebhookEndpointId WebhookSigningSecret WebhookCreateRequest WebhookEmptyRequest "
        "WebhookEndpoint WebhookEndpointWithSecret WebhookEndpointList WebhookDeleteReply "
        "WebhookDelivery WebhookDeliveryList WebhookManagementErrorCode WebhookSignatureHeader "
        "WebhookDecisionCompletedData WebhookDecisionRefusedData WebhookStreamClosedData WebhookEvent"
    ).split()
    for name in names:
        assert getattr(types, name) is getattr(webhooks, name)
    assert types.WebhookDeleteReply is webhooks.WebhookDeleted
    assert types.WebhookDecisionCompletedData is types.WebhookDecisionRefusedData is webhooks.WebhookDecisionData


FIXTURES = json.loads((Path(__file__).parents[2] / "fixtures" / "webhooks" / "signatures.json").read_text(encoding="utf-8"), object_hook=lambda obj: {key: bytes(value).decode("ascii") if key in {"secret", "signature"} and isinstance(value, list) else value for key, value in obj.items()})
MANAGEMENT = json.loads((Path(__file__).parents[2] / "fixtures" / "webhooks" / "management.json").read_text(encoding="utf-8"), object_hook=lambda obj: {key: bytes(value).decode("ascii") if key in {"secret", "signature"} and isinstance(value, list) else value for key, value in obj.items()})
ENDPOINT = MANAGEMENT["endpoint"]
ENDPOINT_ID = ENDPOINT["id"]


@pytest.mark.parametrize("entry", FIXTURES["cases"], ids=lambda entry: entry["name"])
def test_shared_signature_contract(entry):
    body = (entry["repeat"] * entry["count"] if "repeat" in entry else
            entry.get("body", FIXTURES["body"]) + entry.get("append", ""))
    signature = entry.get("signature", FIXTURES["signature"])
    secret = entry.get("secret", FIXTURES["secret"])
    now = entry.get("now", entry.get("timestamp", FIXTURES["timestamp"]))
    assert verify_webhook_signature(body, signature, secret, now=now) is entry["valid"]
    try:
        raw = body.encode("utf-8")
    except UnicodeEncodeError:
        assert entry["valid"] is False
        return
    assert verify_webhook_signature(raw, signature, secret, now=now) is entry["valid"]
    assert verify_webhook_signature(bytearray(raw), signature, secret, now=now) is entry["valid"]
    assert verify_webhook_signature(memoryview(raw), signature, secret, now=now) is entry["valid"]


@pytest.mark.parametrize("body", [b"\xff", "\ud800"])
def test_signature_invalid_or_changed_raw_bytes(body):
    assert verify_webhook_signature(body, FIXTURES["signature"], FIXTURES["secret"], now=FIXTURES["timestamp"]) is False


def test_signature_does_not_parse_or_normalize_json():
    body = json.dumps(json.loads(FIXTURES["body"]), indent=2)
    assert verify_webhook_signature(body, FIXTURES["signature"], FIXTURES["secret"], now=FIXTURES["timestamp"]) is False


def test_signature_verifies_only_visible_body_bytes():
    storage = b"x" + FIXTURES["body"].encode("utf-8") + b"y"
    assert verify_webhook_signature(memoryview(storage)[1:-1], FIXTURES["signature"], FIXTURES["secret"], now=FIXTURES["timestamp"])


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -1, 1770000000.5, 9007199254740992, True])
def test_invalid_clock(now):
    assert verify_webhook_signature(FIXTURES["body"], FIXTURES["signature"], FIXTURES["secret"], now=now) is False


def test_default_clock(monkeypatch):
    monkeypatch.setattr("rightnow.webhooks.time.time", lambda: FIXTURES["timestamp"])
    assert verify_webhook_signature(FIXTURES["body"], FIXTURES["signature"], FIXTURES["secret"])


def test_management_reuses_authenticated_transport_and_metadata():
    seen = []
    replies = iter([(201, {**ENDPOINT, "secret": FIXTURES["secret"]}), (200, {"object": "list", "data": [ENDPOINT]}),
                    (200, {"id": ENDPOINT_ID, "deleted": True}), (200, {**ENDPOINT, "secret": FIXTURES["secret"]}),
                    (200, MANAGEMENT["deliveries"])])

    def send(request):
        seen.append(request)
        status, body = next(replies)
        return httpx.Response(status, json=body, headers={"x-request-id": "req_webhook"})

    with SystemZero(api_key="test-key", transport=httpx.MockTransport(send)) as client:
        resource = Webhooks(client)
        created = resource.create_with_response({"url": ENDPOINT["url"], "events": ["decision.completed"]},
                                                client_request_id="client_webhook", headers={"x-custom": "synthetic"})
        assert created.data == {**ENDPOINT, "secret": FIXTURES["secret"]}
        assert created.status == 201
        assert created.metadata.request_id == "req_webhook"
        assert resource.list() == {"object": "list", "data": [ENDPOINT]}
        assert resource.delete(ENDPOINT_ID) == {"id": ENDPOINT_ID, "deleted": True}
        assert resource.rotate_secret(ENDPOINT_ID) == {**ENDPOINT, "secret": FIXTURES["secret"]}
        deliveries = resource.deliveries(ENDPOINT_ID)
        assert deliveries == MANAGEMENT["deliveries"]
        assert all(set(entry) == {"event_id", "type", "attempt", "status_code", "latency_ms", "outcome", "created"}
                   for entry in deliveries["data"])
    assert [(call.method, call.url.path) for call in seen] == [
        ("POST", "/v1/decision-model/webhooks"), ("GET", "/v1/decision-model/webhooks"),
        ("DELETE", "/v1/decision-model/webhooks/" + ENDPOINT_ID),
        ("POST", "/v1/decision-model/webhooks/" + ENDPOINT_ID + "/rotate-secret"),
        ("GET", "/v1/decision-model/webhooks/" + ENDPOINT_ID + "/deliveries"),
    ]
    assert all(call.headers["authorization"] == "Bearer test-key" for call in seen)
    assert all("idempotency-key" not in call.headers for call in seen)
    assert seen[0].headers["x-client-request-id"] == "client_webhook"
    assert seen[0].headers["x-custom"] == "synthetic"
    assert json.loads(seen[0].content) == {"url": ENDPOINT["url"], "events": ["decision.completed"]}
    assert seen[3].content == b"{}"


@pytest.mark.parametrize("method", ["create", "rotate_secret"])
@pytest.mark.parametrize("failure", ["status", "connection"])
def test_secret_returning_calls_never_retry(method, failure):
    calls = []

    def send(request):
        calls.append(request)
        if failure == "connection":
            raise httpx.ConnectError("SYNTHETIC ambiguous result", request=request)
        return httpx.Response(503, json={"error": {"message": "SYNTHETIC unavailable", "code": "webhook_unavailable",
                                                    "type": "overloaded_error", "retryable": True}})

    with SystemZero(api_key="test-key", transport=httpx.MockTransport(send), max_retries=3,
                    sleep=lambda _: None) as client:
        resource = Webhooks(client)
        with pytest.raises(SystemZeroError):
            if method == "create":
                resource.create({"url": ENDPOINT["url"], "events": ["stream.closed"]})
            else:
                resource.rotate_secret(ENDPOINT_ID)
    assert len(calls) == 1


@pytest.mark.parametrize("endpoint_id", ["", "../other", "wh_ABC", ENDPOINT_ID + "/rotate-secret", ENDPOINT_ID + "\n"])
def test_invalid_endpoint_id_before_transport(endpoint_id):
    calls = []
    with SystemZero(api_key="test-key", transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
        resource = Webhooks(client)
        for method in [resource.delete, resource.rotate_secret, resource.deliveries]:
            with pytest.raises(WebhookInvalidError):
                method(endpoint_id)
    assert calls == []


def test_management_refuses_idempotency_headers():
    calls = []
    with SystemZero(api_key="test-key", transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
        resource = Webhooks(client)
        for action in [
            lambda: resource.create({"url": ENDPOINT["url"], "events": ["stream.closed"]}, headers={"Idempotency-Key": "bad"}),
            lambda: resource.list(headers={"IDEMPOTENCY-KEY": "bad"}),
            lambda: resource.delete(ENDPOINT_ID, headers={"idempotency-key": "bad"}),
            lambda: resource.rotate_secret(ENDPOINT_ID, headers={"idempotency-key": "bad"}),
            lambda: resource.deliveries(ENDPOINT_ID, headers={"idempotency-key": "bad"}),
        ]:
            with pytest.raises(WebhookInvalidError):
                action()
    assert calls == []


@pytest.mark.parametrize("limit", [1, 100])
def test_delivery_log_limit(limit):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json={"object": "list", "data": []})

    with SystemZero(api_key="test-key", transport=httpx.MockTransport(send)) as client:
        assert Webhooks(client).deliveries(ENDPOINT_ID, limit=limit, client_request_id="req_log") == {"object": "list", "data": []}
    assert calls[0].url.params["limit"] == str(limit)
    assert calls[0].headers["x-client-request-id"] == "req_log"


@pytest.mark.parametrize("limit", [0, 101, -1, 1.5, float("nan"), float("inf"), True])
def test_invalid_delivery_log_limit(limit):
    calls = []
    with SystemZero(api_key="test-key", transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
        with pytest.raises(WebhookInvalidError):
            Webhooks(client).deliveries(ENDPOINT_ID, limit=limit)
    assert calls == []


@pytest.mark.parametrize("entry", FIXTURES["errors"], ids=lambda entry: entry["code"])
def test_typed_envelope_fields(entry):
    def send(_request):
        return httpx.Response(entry["status"], headers={"x-request-id": "req_header", "x-should-retry": "false"}, json={
            "error": {"message": "SYNTHETIC webhook failure", "code": entry["code"], "type": entry["type"],
                      "retryable": entry["retryable"], "param": "url", "details": {"limit": 5}},
            "request_id": "req_body",
        })

    with SystemZero(api_key="test-key", transport=httpx.MockTransport(send)) as client:
        with pytest.raises(SystemZeroError) as caught:
            Webhooks(client).list()
    error = caught.value
    assert type(error).__name__ == entry["class_name"]
    assert error.code == entry["code"]
    assert error.type == entry["type"]
    assert error.status == entry["status"]
    assert error.retryable is entry["retryable"]
    assert error.param == "url"
    assert error.details == {"limit": 5}
    assert error.request_id == "req_body"
    assert error.headers["x-request-id"] == "req_header"
    assert WEBHOOK_ERROR_CODES[entry["code"]] == {key: entry[key] for key in ("status", "type", "retryable")}
