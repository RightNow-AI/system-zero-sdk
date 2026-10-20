"""One-item batches keep gateway codes and existing held/refused outcomes."""

import json

import httpx
import pytest

from system_zero import (
    AuthenticationError, InvalidRequestError, NotFoundError, PermissionError,
    ServerError, SystemZero, SystemZeroError, run_batch,
)


@pytest.mark.parametrize("status,code,error_type,outcome", [
    (404, "model_not_found", "not_found_error", "error"),
    (403, "auth_error", "authentication_error", "error"),
    (403, "account_suspended", "permission_error", "error"),
    (500, "idempotency_replayed_error", "server_error", "error"),
    (422, "request_refused", "invalid_request_error", "refused"),
])
def test_batch_keeps_gateway_code(tmp_path, status, code, error_type, outcome):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {
            "message": "Synthetic gateway response.", "code": code, "type": error_type,
            "retryable": False, "param": "model" if code == "model_not_found" else None,
        }})

    path = tmp_path / "rows.jsonl"
    request = {"model": "system-zero", "state": "synthetic", "questions": {
        "q": {"type": "boolean", "instructions": "Is it set?"},
    }}
    with SystemZero(api_key="synthetic", base_url="https://api.example.com/v1", timeout=1,
                    max_retries=0, transport=httpx.MockTransport(send)) as client:
        result = run_batch(client, [{"id": "one", "request": request, "expected": {"q": True}}],
                           run_id="gateway-code", results_path=path,
                           deadline_ms=10000, concurrency=1)
    saved = json.loads(path.read_text(encoding="ascii"))
    assert len(calls) == saved["attempts"] == 1
    assert saved["code"] == code
    assert saved["outcome"] == outcome
    assert result["records"][0] == saved
    assert len(saved["questions"]) == (1 if outcome == "refused" else 0)
    if outcome == "held":
        assert "cost_nanocents" not in saved and "accounted_nanocents" not in saved
        assert saved["usage"] is None


@pytest.mark.parametrize("status,code,error_type,error_class", [
    (403, "auth_error", "authentication_error", AuthenticationError),
    (403, "account_suspended", "permission_error", PermissionError),
    (404, "model_not_found", "not_found_error", NotFoundError),
    (422, "idempotency_replayed_error", "invalid_request_error", InvalidRequestError),
    (503, "idempotency_replayed_error", "server_error", ServerError),
])
def test_enabled_batch_retries_keep_exact_gateway_error(tmp_path, status, code, error_type, error_class):
    calls, observed, waits = [], [], []

    class RecordingSystemZero(SystemZero):
        def decide(self, *args, **kwargs):
            try:
                return super().decide(*args, **kwargs)
            except SystemZeroError as error:
                observed.append(error)
                raise

    def send(request):
        calls.append(request)
        # Retry hints never widen the workflow admission policy for these codes.
        return httpx.Response(status, json={"request_id": "req_batch_code", "error": {
            "message": "Synthetic gateway response.", "code": code, "type": error_type, "retryable": True,
        }}, headers={"x-should-retry": "true", "retry-after-ms": "1"})

    path = tmp_path / "rows.jsonl"
    request = {"model": "system-zero", "state": "synthetic", "questions": {
        "q": {"type": "boolean", "instructions": "Is it set?"},
    }}
    with RecordingSystemZero(api_key="synthetic", base_url="https://api.example.com/v1", timeout=1,
                             max_retries=3, sleep=waits.append, transport=httpx.MockTransport(send)) as client:
        result = run_batch(client, [{"id": "one", "request": request, "expected": {"q": True}}],
                           run_id="gateway-code-retries", results_path=path,
                           deadline_ms=10000, concurrency=1, max_retries=3, sleep_ms=waits.append)
    assert len(calls) == len(observed) == 1
    assert waits == []
    error = observed[0]
    assert type(error) is error_class
    assert (error.code, error.status, error.type, error.request_id) == (code, status, error_type, "req_batch_code")
    saved = json.loads(path.read_text(encoding="ascii"))
    assert (saved["code"], saved["outcome"], saved["attempts"]) == (code, "error", 1)
    assert result["records"] == [saved]
