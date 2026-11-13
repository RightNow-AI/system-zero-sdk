"""RightNow decision model SDK helper."""

import json

import httpx
import pytest

from rightnow import APIConnectionError, APITimeoutError, ServiceUnavailableError, SystemZero, SystemZeroError


REQUEST = {"model": "rightnow-decision", "state": "SYNTHETIC HTTP retry parity",
           "questions": {"ready": {"type": "boolean", "instructions": "Ready?"}}}
KEY = "SYNTHETIC"
REFUSAL = {"request_id": "req_http_parity", "error": {
    "type": "api_error", "code": "service_unavailable", "message": "SYNTHETIC refusal", "retryable": True,
}}


def observe(method, respond, *, keyed=True):
    attempts = []

    def transport(request):
        attempts.append({"method": request.method, "key": request.headers.get("idempotency-key")})
        return respond(request)

    with SystemZero("SYNTHETIC", transport=httpx.MockTransport(transport), max_retries=2,
                    timeout=1, retry_budget=10, sleep=lambda _: None, random=lambda: 0) as client:
        with pytest.raises(SystemZeroError) as caught:
            if method == "GET":
                client.models.list()
            else:
                client.decide(REQUEST, idempotency_key=KEY if keyed else "")
    return attempts, caught.value


def assert_attempts(attempts, method, count):
    assert attempts == [{"method": method, "key": KEY if method == "POST" else None}] * count


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_terminal_sdk_connection_error_is_not_replayed(method):
    original = APIConnectionError("SYNTHETIC terminal transport failure", type="connection_error",
                                  retryable=False, transport_code="ECONNREFUSED", request_sent=False)

    def fail(_request):
        raise original

    attempts, failure = observe(method, fail)
    assert_attempts(attempts, method, 1)
    assert type(failure) is APIConnectionError
    assert failure.retryable is False
    assert failure.transport_code == "ECONNREFUSED"
    assert failure.request_sent is False


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_terminal_sdk_timeout_is_normalized_without_replay(method):
    original = APITimeoutError("SYNTHETIC terminal timeout", type="timeout_error",
                               retryable=False, transport_code="ETIMEDOUT", request_sent=False)

    def fail(_request):
        raise original

    attempts, failure = observe(method, fail)
    assert_attempts(attempts, method, 1)
    assert type(failure) is APIConnectionError
    assert failure.type == "timeout_error"
    assert failure.retryable is False
    assert failure.transport_code == "ETIMEDOUT"
    assert failure.request_sent is False


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_base_transport_retries_before_response_keep_one_key(method):
    def fail(_request):
        raise httpx.ReadError("SYNTHETIC reset") from ConnectionResetError(104, "SYNTHETIC reset")

    attempts, failure = observe(method, fail)
    assert_attempts(attempts, method, 3)
    assert type(failure) is APIConnectionError
    assert failure.retryable is True


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_transport_fault_after_response_byte_is_not_replayed(method):
    def fail(request):
        request.extensions["system_zero_response_started"] = True
        raise httpx.ReadError("SYNTHETIC reset after response")

    attempts, failure = observe(method, fail)
    assert_attempts(attempts, method, 1)
    assert type(failure) is APIConnectionError
    assert failure.retryable is False
    assert failure.request_sent is True


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_stream_fault_after_complete_refusal_bytes_is_not_replayed(method):
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            yield json.dumps(REFUSAL).encode("ascii")
            raise httpx.ReadError("SYNTHETIC body close fault")

    attempts, failure = observe(method, lambda _: httpx.Response(
        503, headers={"x-request-id": "req_http_parity"}, stream=BrokenBody()))
    assert_attempts(attempts, method, 1)
    assert type(failure) is APIConnectionError
    assert failure.retryable is False
    assert failure.request_sent is True
    assert failure.status == 503
    assert failure.request_id == "req_http_parity"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_base_complete_refusal_retries_keep_one_key(method):
    attempts, failure = observe(method, lambda _: httpx.Response(503, json=REFUSAL))
    assert_attempts(attempts, method, 3)
    assert type(failure) is ServiceUnavailableError
    assert failure.retryable is True
    assert failure.status == 503
    assert failure.code == "service_unavailable"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_complete_refusal_close_fault_keeps_base_terminal_policy(method):
    class CloseFailure(httpx.SyncByteStream):
        def __iter__(self):
            yield json.dumps(REFUSAL).encode("ascii")

        def close(self):
            raise httpx.ReadError("SYNTHETIC close transport failure")

    attempts, failure = observe(method, lambda _: httpx.Response(
        503, headers={"retry-after": "0"}, stream=CloseFailure()))
    # 41c89a69 made one attempt with retryable=False. Preserve that boundary
    # while retaining the complete refusal instead of losing it during cleanup.
    assert_attempts(attempts, method, 1)
    assert failure.retryable is False
    assert type(failure) is ServiceUnavailableError
    assert failure.status == 503
    assert failure.code == "service_unavailable"
    assert failure.request_id == "req_http_parity"
    assert failure.headers["retry-after"] == "0"


def test_explicitly_unkeyed_request_is_terminal_before_response():
    def fail(_request):
        raise httpx.ReadError("SYNTHETIC before-response reset")

    attempts, failure = observe("POST", fail, keyed=False)
    assert attempts == [{"method": "POST", "key": None}]
    assert type(failure) is APIConnectionError
    assert failure.retryable is False

@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("headers,wait", [({"Retry-After-Ms": "240000", "Retry-After": "1"}, 240000), ({"Retry-After": "180"}, 180000)])
def test_server_wait_beyond_default_budget_raises_without_sleep(status, headers, wait):
    attempts, sleeps = [], []
    def send(request):
        attempts.append(request)
        return httpx.Response(status, json={"error": {"code": "rate_limit_exceeded" if status == 429 else "model_unavailable", "retryable": True}}, headers=headers)
    with SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(send), random=lambda: 0, sleep=sleeps.append) as client:
        with pytest.raises(SystemZeroError) as caught:
            client.decide(REQUEST)
    assert caught.value.retry_after_ms == wait
    assert len(attempts) == 1 and not sleeps

@pytest.mark.parametrize("status", [429, 503])
def test_server_wait_within_budget_is_honored(status):
    attempts, sleeps, clock = [], [], [0.0]
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    def send(request):
        attempts.append(request)
        return httpx.Response(status, json={"error": {"code": "rate_limit_exceeded" if status == 429 else "model_unavailable", "retryable": True}}, headers={"Retry-After-Ms": "500", "Retry-After": "180"}) if len(attempts) == 1 else httpx.Response(200, json={"answers": {}})
    with SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(send), retry_budget=2, monotonic=lambda: clock[0], random=lambda: 0, sleep=sleep) as client:
        client.decide(REQUEST)
    assert sleeps == [0.5] and len(attempts) == 2

@pytest.mark.parametrize("status,code", [(503, "idempotency_unavailable"), (503, "limiter_unavailable"), (500, "internal_error"), (503, "starter_unavailable"), (502, None), (503, None)])
def test_safe_server_failure_recovers_with_one_key(status, code):
    seen = []
    def send(request):
        seen.append(request.headers["idempotency-key"])
        if len(seen) > 1:
            return httpx.Response(200, json={"answers": {}})
        return httpx.Response(status, text="upstream unavailable") if code is None else httpx.Response(status, json={"error": {"code": code, "retryable": True}})
    with SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(send), random=lambda: 0, sleep=lambda _: None) as client:
        client.decide(REQUEST, idempotency_key="same-recovery-key")
    assert seen == ["same-recovery-key", "same-recovery-key"]


def test_initial_attempt_time_does_not_consume_recovery_budget():
    clock, attempts = [0.0], []
    def send(request):
        attempts.append(request)
        if len(attempts) == 1:
            clock[0] += 70
            return httpx.Response(503, json=REFUSAL)
        return httpx.Response(200, json={"answers": {}})
    with SystemZero(api_key="SYNTHETIC", transport=httpx.MockTransport(send),
                    monotonic=lambda: clock[0], random=lambda: 0, sleep=lambda _: None) as client:
        client.decide(REQUEST)
    assert len(attempts) == 2
