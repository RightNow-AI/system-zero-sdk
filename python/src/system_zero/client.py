"""Synchronous server-side RightNow System Zero HTTP API client."""

from ._redaction import redact, Secret, protected, reveal

import ipaddress
import json
import os
import random as random_module
import sys
import time
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Optional, Union, cast
from uuid import uuid4
from urllib.parse import urlsplit

import httpx

from ._transport import AttemptState, DeadlineTransport, attempt_state
from ._connection import ResponseCloseGuard, connection_error, is_transport_error
from ._retry import _recover_workflow, _retry_context, _retry_response, _retry_wait
from .errors import APIConnectionError, APIError, APITimeoutError, InvalidRequestError, SystemZeroError, error_class
from .response import ResponseResult, response_metadata
from .types import DecideReply, DecideRequest, ModelList
from .enabled_types import EnabledDecideRequest, VideoRequest


VERSION = "0.4.2"
DEFAULT_BASE_URL = "https://api.rightnowai.co/v1"
_MAX_TIMEOUT_SECONDS = 2_147_483_647 / 1000


def _checked_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        local = host == "".join(("local", "host"))
        if host and not local:
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)
                or not host or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "?" in value or "#" in value):
            raise ValueError()
        parsed.port
    except (AttributeError, TypeError, ValueError):
        raise ValueError("base_url must be HTTPS or loopback HTTP without credentials, query or fragment.") from None
    return value.rstrip("/")


def _validate_retry_budget(value: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not 0 <= value <= _MAX_TIMEOUT_SECONDS):
        raise ValueError("retry_budget must be nonnegative, finite and at most 2147483.647 seconds.")
    return value


def _api_error(response: httpx.Response, body: Any) -> SystemZeroError:
    headers = dict(response.headers)
    envelope = body if isinstance(body, dict) else {}
    error = envelope.get("error")
    if isinstance(error, dict):
        kind = error.get("type") if isinstance(error.get("type"), str) else "api_error"
        message = error.get("message") if isinstance(error.get("message"), str) else "RightNow System Zero API request failed."
        code = error.get("code") if isinstance(error.get("code"), str) else None
        cls = error_class(kind, code)
        param = error.get("param") if isinstance(error.get("param"), str) else None
        retryable = error.get("retryable") if isinstance(error.get("retryable"), bool) else _retry_response(response.status_code, code, headers)
        request_id = envelope.get("request_id") if isinstance(envelope.get("request_id"), str) else headers.get("x-request-id")
        details = error.get("details") if isinstance(error.get("details"), dict) else None
        return cls(message, status=response.status_code, type=kind, code=code, param=param,
                   retryable=retryable, details=details, request_id=request_id, headers=headers)
    return APIError(
        "RightNow System Zero API returned a non-JSON or invalid response.", status=response.status_code,
        retryable=_retry_response(response.status_code, None, headers),
        request_id=headers.get("x-request-id"), headers=headers,
    )


class Models:
    """The authenticated GET /models resource."""

    def __init__(self, client: "SystemZero"):
        self._client = client

    def list(self, *, client_request_id: Optional[str] = None, headers: Optional[Mapping[str, str]] = None,
             retry_budget: Optional[float] = None) -> ModelList:
        return self.list_with_response(client_request_id=client_request_id, headers=headers, retry_budget=retry_budget).data

    def list_with_response(self, *, client_request_id: Optional[str] = None,
                           headers: Optional[Mapping[str, str]] = None,
                           retry_budget: Optional[float] = None) -> ResponseResult[ModelList]:
        return cast(ResponseResult[ModelList], self._client._request(
            "GET", "/models", client_request_id=client_request_id, headers=headers, retry_budget=retry_budget,
        ))


class SystemZero:
    """Server-side client. Do not ship API keys to a browser.

    Timeouts and retry waits are seconds. The client owns its HTTPX client and
    transport (including an injected transport); close it or use a context manager.
    The sleep/random/now hooks let tests control pacing without real delays.
    """

    def __init__(
        self, api_key: Optional[str] = None, *, base_url: str = DEFAULT_BASE_URL,
        timeout: float = 150.0, max_retries: int = 5, max_retry_wait: float = 60.0,
        retry_budget: float = 60.0,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = random_module.random,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        key = api_key if api_key is not None else os.environ.get("SYSTEM_ZERO_API_KEY")
        if key is None:
            raise ValueError("Set api_key or SYSTEM_ZERO_API_KEY.")
        if not isinstance(key, str) or not key or any(not 0x21 <= ord(character) <= 0x7e for character in key):
            raise ValueError("api_key must contain only visible ASCII characters.")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= _MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout must be positive, finite and at most 2147483.647 seconds.")
        if (isinstance(max_retry_wait, bool) or not isinstance(max_retry_wait, (int, float)) or
                not 0 <= max_retry_wait <= _MAX_TIMEOUT_SECONDS):
            raise ValueError("max_retry_wait must be nonnegative, finite and at most 2147483.647 seconds.")
        checked_base_url = _checked_base_url(base_url)
        self._api_key = Secret(key)
        self.base_url = checked_base_url
        self.timeout = timeout
        self.max_retries = min(max_retries, 5)
        self.max_retry_wait = max_retry_wait
        self.retry_budget = _validate_retry_budget(retry_budget)
        self._sleep = sleep
        self._random = random
        self._now = now
        self._monotonic = monotonic
        self._closed = False
        self._http = httpx.Client(
            transport=transport if transport is not None else DeadlineTransport(),
            timeout=timeout, follow_redirects=False, trust_env=False,
        )
        self.models = Models(self)

    def __enter__(self) -> "SystemZero":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @protected
    def close(self) -> None:
        if not self._closed:
            self._closed = True
            context_boundary = sys.exc_info()[1]
            try:
                self._http.close()
            except Exception as cause:
                if not is_transport_error(cause, context_boundary=context_boundary):
                    raise
                raise connection_error(cause, "RightNow connection cleanup failed.",
                                       context_boundary=context_boundary) from None

    def decide(
        self, request: Union[DecideRequest, EnabledDecideRequest, VideoRequest, Mapping[str, Any]], *,
        idempotency_key: Optional[str] = None, client_request_id: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        retry_budget: Optional[float] = None,
        danger_zone: Optional[Literal["accept"]] = None,
    ) -> DecideReply:
        return self.decide_with_response(request, idempotency_key=idempotency_key,
                                         client_request_id=client_request_id, headers=headers,
                                         retry_budget=retry_budget, danger_zone=danger_zone).data

    def decide_with_response(
        self, request: Union[DecideRequest, EnabledDecideRequest, VideoRequest, Mapping[str, Any]], *,
        idempotency_key: Optional[str] = None, client_request_id: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        retry_budget: Optional[float] = None,
        danger_zone: Optional[Literal["accept"]] = None,
    ) -> ResponseResult[DecideReply]:
        outgoing = httpx.Headers(headers)
        if "SZ-Danger-Zone" in outgoing or (danger_zone is not None and danger_zone != "accept"):
            raise InvalidRequestError("Use danger_zone with the exact value accept; do not supply its header.",
                                      status=400, type="invalid_request_error", code="invalid_request", param="SZ-Danger-Zone")
        return cast(ResponseResult[DecideReply], self._request(
            "POST", "/system-zero/decide", body=request,
            idempotency_key=idempotency_key, client_request_id=client_request_id,
            headers=outgoing, retry_budget=retry_budget, _danger_zone=danger_zone,
        ))

    @protected
    def _request(
        self, method: str, path: str, *, body: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None, client_request_id: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        generate_idempotency: bool = True, retries: Optional[int] = None,
        _danger_zone: Optional[Literal["accept"]] = None,
        retry_budget: Optional[float] = None,
    ) -> ResponseResult[Any]:
        _checked_base_url(self.base_url)
        context_boundary = sys.exc_info()[1]
        budget = self.retry_budget if retry_budget is None else _validate_retry_budget(retry_budget)
        max_retries = min(5, self.max_retries if retries is None else retries) if budget > 0 else 0
        context = _retry_context.get()
        if context is not None:
            # One decision consumes this context; reentrant hook calls own theirs.
            _retry_context.set(None)
        context = context or {}
        max_retries = min(max_retries, context.get("max_retries", max_retries))
        sleep = context.get("sleep", self._sleep)
        draw = context.get("random", self._random)
        external_clock = context.get("remaining", lambda: float("inf"))
        on_deadline = context.get("on_deadline", lambda: None)
        outgoing = httpx.Headers(headers)
        if "SZ-Danger-Zone" in outgoing or (_danger_zone is not None and _danger_zone != "accept"):
            raise InvalidRequestError("Use danger_zone with the exact value accept; do not supply its header.",
                                      status=400, type="invalid_request_error", code="invalid_request", param="SZ-Danger-Zone")
        if _danger_zone == "accept" and method == "POST" and path == "/system-zero/decide":
            outgoing["SZ-Danger-Zone"] = "accept"
        outgoing["Authorization"] = "Bearer " + reveal(self._api_key)
        outgoing["User-Agent"] = "system-zero-python/" + VERSION + " python/" + ".".join(map(str, sys.version_info[:3]))
        outgoing["Accept"] = "application/json"
        if body is not None:
            outgoing["Content-Type"] = "application/json"
        if client_request_id is not None:
            outgoing["X-Client-Request-Id"] = client_request_id
        if method == "POST" and generate_idempotency:
            if idempotency_key is None:
                idempotency_key = outgoing.get("Idempotency-Key")
            if idempotency_key is None and max_retries > 0:
                idempotency_key = "szsdk_" + uuid4().hex
            if idempotency_key is not None:
                outgoing["Idempotency-Key"] = idempotency_key
        if "Idempotency-Key" in outgoing and not outgoing["Idempotency-Key"].strip():
            del outgoing["Idempotency-Key"]
        if "X-Client-Request-Id" in outgoing and not outgoing["X-Client-Request-Id"].strip():
            del outgoing["X-Client-Request-Id"]
        used_key = outgoing.get("Idempotency-Key")
        # Python dict insertion order is preserved. Serialize once for all attempts.
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if body is not None else None
        safe_replay = method in {"GET", "HEAD", "OPTIONS"} or bool(outgoing.get("Idempotency-Key", "").strip())
        started = self._monotonic()
        retry_deadline = started + budget
        external_deadline = started + external_clock()

        def external_remaining() -> float:
            # Wall-clock rollback cannot extend a batch deadline or replay key.
            return min(external_deadline - self._monotonic(), external_clock())

        last_error: Optional[SystemZeroError] = None
        for attempt in range(max_retries + 1):
            outside = external_remaining()
            remaining = min(retry_deadline - self._monotonic(), outside)
            if outside <= 0 or attempt and remaining < 0.001:
                if outside <= 0:
                    on_deadline()
                if last_error is not None:
                    raise last_error
                raise APITimeoutError("RightNow request deadline exceeded.",
                                      type="timeout_error", code="system_zero_timeout", idempotency_key=used_key)
            timeout = min(self.timeout, remaining) if attempt else min(self.timeout, outside)
            outgoing["X-SDK-Retry-Count"] = str(attempt)
            request = self._http.build_request(method, _checked_base_url(self.base_url) + path, content=payload, headers=outgoing, timeout=timeout)
            state = AttemptState(time.monotonic() + timeout)
            token = attempt_state.set(state)
            response = None
            close_guard = None
            error = None
            result = None
            propagating = False
            dispatched = False
            should_retry = False
            retry_headers: Mapping[str, str] = {}
            try:
                outside = external_remaining()
                remaining = min(retry_deadline - self._monotonic(), outside)
                if outside <= 0 or attempt and remaining < 0.001:
                    if outside <= 0:
                        on_deadline()
                    if last_error is not None:
                        raise last_error
                    raise APITimeoutError("RightNow request deadline exceeded.",
                                          type="timeout_error", code="system_zero_timeout", idempotency_key=used_key)
                dispatch_timeout = min(timeout, remaining) if attempt else min(timeout, outside)
                request.extensions["timeout"] = httpx.Timeout(dispatch_timeout).as_dict()
                state.deadline = min(state.deadline, time.monotonic() + dispatch_timeout)
                if "on_attempt" in context:
                    context["on_attempt"]()
                dispatched = True
                response = self._http.send(request, stream=True)
                close_guard = ResponseCloseGuard(cast(httpx.SyncByteStream, response.stream),
                                                context_boundary=context_boundary)
                response.stream = close_guard
                state.response_started = True
                state.request_sent = True
                response.read()
                if time.monotonic() >= state.deadline:
                    raise httpx.ReadTimeout("RightNow attempt deadline exceeded", request=request)
                try:
                    parsed = response.json()
                except (ValueError, UnicodeError):
                    parsed = None
                if response.is_success and isinstance(parsed, dict):
                    result_headers = MappingProxyType(dict(response.headers))
                    result = ResponseResult(parsed, response_metadata(redact(result_headers), redact(used_key)), response.status_code, result_headers)
                else:
                    error = _api_error(response, parsed)
                    envelope_error = parsed.get("error") if isinstance(parsed, dict) else None
                    marker = envelope_error.get("retryable") if isinstance(envelope_error, dict) else None
                    workflow_marker = marker if isinstance(marker, bool) else None
                    # No replay after an invalid JSON success: the server already answered.
                    should_retry = (safe_replay and not response.is_success and
                                    error.type not in {"authentication_error", "permission_error"} and
                                    (_recover_workflow(response.status_code, error.code, response.headers, workflow_marker, error.details)
                                     if context.get("workflow_recovery") else
                                     _retry_response(response.status_code, error.code, response.headers, error.retryable)))
                    if context.get("workflow_recovery") and workflow_marker is None and should_retry:
                        error.retryable = True
                    retry_headers = response.headers
            except httpx.DecodingError:
                error = APIError(
                    "RightNow System Zero API returned an invalid encoded response.",
                    status=response.status_code if response is not None else None,
                    request_id=response.headers.get("x-request-id") if response is not None else None,
                    headers=dict(response.headers) if response is not None else None,
                )
            except Exception as cause:
                if (not is_transport_error(cause, context_boundary=context_boundary) or
                        isinstance(cause, APITimeoutError) and not dispatched):
                    propagating = True
                    if isinstance(cause, SystemZeroError):
                        cause.idempotency_key = used_key
                    raise
                started = state.response_started or bool(request.extensions.get("system_zero_response_started"))
                # Keep the existing ordinary retry classes and response-byte gate.
                # Newly normalized native errors do not add an implicit replay policy.
                transport_retryable = safe_replay and not started and isinstance(
                    cause, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
                )
                should_retry = not context.get("workflow_recovery") and transport_retryable
                error = connection_error(
                    cause, "RightNow connection failed.",
                    request_sent=True if started else state.request_sent,
                    context_boundary=context_boundary,
                    status=response.status_code if response is not None else None,
                    retryable=transport_retryable,
                    request_id=response.headers.get("x-request-id") if response is not None else None,
                    headers=dict(response.headers) if response is not None else None,
                )
            except BaseException:
                propagating = True
                raise
            finally:
                try:
                    if response is not None:
                        cleanup_error = None
                        try:
                            response.close()
                        except Exception as cause:
                            if not is_transport_error(cause, context_boundary=context_boundary):
                                raise
                            cleanup_error = cause
                        if cleanup_error is None and close_guard is not None:
                            cleanup_error = close_guard.error
                        if cleanup_error is not None and not propagating:
                            # Cleanup cannot replace an API refusal or the original failure.
                            if error is None:
                                error = connection_error(
                                    cleanup_error, "RightNow connection cleanup failed.", request_sent=True,
                                    context_boundary=context_boundary,
                                    status=response.status_code, request_id=response.headers.get("x-request-id"),
                                    headers=dict(response.headers), retryable=False,
                                )
                            # A response-close fault was terminal under the base policy.
                            error.retryable = False
                            should_retry = False
                finally:
                    attempt_state.reset(token)
            if error is None and result is not None:
                return result
            if error is None:
                raise RuntimeError("RightNow transport did not return a result.")
            error.idempotency_key = used_key
            last_error = error
            if external_remaining() <= 0:
                on_deadline()
                raise error
            if not should_retry or attempt >= max_retries:
                raise error
            wait = _retry_wait(retry_headers, attempt, self._now, draw)
            if wait > self.max_retry_wait:
                raise error
            outside = external_remaining()
            if wait >= min(retry_deadline - self._monotonic(), outside):
                if wait >= outside:
                    on_deadline()
                raise error
            sleep(wait)
        raise RuntimeError("RightNow retry loop did not terminate.")
