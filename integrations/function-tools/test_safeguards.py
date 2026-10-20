"""Synthetic regression cases for tool output and gateway code preservation."""

import importlib.resources
import json

import httpx
import pytest

from system_zero import SystemZero
from system_zero.tools import create_tool_handler


ARGS = {"state": "SYNTHETIC", "questions": [{"name": "ready", "type": "boolean", "instructions": "Ready?"}]}
LIMITS = {"max_decide_calls": 10, }
GATEWAY_CODES = [
    ("invalid_api_key", 401), ("monthly_cap_reached", 429), ("idempotency_conflict", 409),
    ("auth_error", 403), ("account_suspended", 403), ("model_not_found", 404),
    ("idempotency_replayed_error", 500),
]


@pytest.mark.parametrize("tool", ["system_zero_decide", "system_zero_list_models"])
def test_tool_redacts_every_field_and_object_key(tool, monkeypatch):
    api_key = "SYNTHETIC"
    environment_key = "ENV_KEY"
    shaped = "rnsk_" + "Q" * 40
    bearer = "TOKEN"
    monkeypatch.setenv("SYSTEM_ZERO_API_KEY", environment_key)
    echo = "%s %s %s Bearer %s" % (api_key, environment_key, shaped, bearer)
    clean = "[redacted] [redacted] [redacted] Bearer [redacted]"
    body = {"id": echo, "answers": {}, "data": [{echo: {"nested": [echo]}}]}
    with SystemZero(api_key=api_key, max_retries=0, transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=body, headers={"x-request-id": echo}))) as client:
        result = create_tool_handler(client, **LIMITS)(tool, ARGS if tool == "system_zero_decide" else {})
    assert result == {"id": clean, "answers": {}, "data": [{clean: {"nested": [clean]}}],
                      "status": "ok", "request_id": clean}
    for secret in (api_key, environment_key, shaped, bearer):
        assert secret not in json.dumps(result)
    assert body["id"] == echo


@pytest.mark.parametrize("status", [400, 200])
def test_tool_redacts_client_credentials_from_errors_and_refusal_payloads(status):
    api_key = "SYNTHETIC"
    shaped = "rnsk_" + "Z" * 32
    echo = api_key + " " + shaped + " Bearer TOKEN"
    refusal = {"code": "invalid_request", "message": echo, "type": "invalid_request_error", "retryable": False}
    body = {"request_id": echo, "error": refusal} if status == 400 else {
        "id": echo, "answers": {echo: {"error": refusal, "extra": [echo]}},
    }
    with SystemZero(api_key=api_key, max_retries=0, transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, json=body))) as client:
        result = create_tool_handler(client, **LIMITS)("system_zero_decide", ARGS)
    assert result["status"] == "error"
    assert result["error"]["code"] == "invalid_request"
    for secret in (api_key, shaped, "TOKEN"):
        assert secret not in json.dumps(result)


@pytest.mark.parametrize("code,status", GATEWAY_CODES)
def test_tool_catalog_publishes_and_preserves_gateway_code(code, status):
    catalog = json.loads(importlib.resources.files("system_zero").joinpath("error-catalog.json").read_text(encoding="utf-8"))
    assert code in catalog
    assert catalog[code]["status"] == status
    for tool in ("system_zero_decide", "system_zero_list_models"):
        with SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(
                lambda _request: httpx.Response(status, json={"request_id": "req_synthetic", "error": {
                    "code": code, "type": "api_error", "message": "Synthetic refusal.", "retryable": False}}))) as client:
            result = create_tool_handler(client, **LIMITS)(tool, ARGS if tool == "system_zero_decide" else {})
        assert result == {"status": "error", "request_id": "req_synthetic", "error": {
            "code": code, "message": "Synthetic refusal.",
        }}


@pytest.mark.parametrize("code,_status", GATEWAY_CODES[3:])
def test_tool_keeps_gateway_code_when_every_answer_is_refused(code, _status):
    with SystemZero(api_key="SYNTHETIC", max_retries=0, transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"answers": {"ready": {"error": {
                "code": code, "message": "Synthetic refusal.", "retryable": False,
            }}}}, headers={"x-request-id": "req_synthetic"}))) as client:
        result = create_tool_handler(client, **LIMITS)("system_zero_decide", ARGS)
    assert result["status"] == "error"
    assert result["request_id"] == "req_synthetic"
    assert result["error"]["code"] == code
    assert result["answers"]["ready"]["error"]["code"] == code
