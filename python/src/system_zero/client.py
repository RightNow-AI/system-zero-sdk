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

from ._transport import _request
from ._transport import AttemptState, default_transport, attempt_state
from ._connection import ResponseCloseGuard, connection_error, is_transport_error
from ._retry import _recover_workflow, _retry_context, _retry_response, _retry_wait
from .decision_records import DecisionRecords, DecisionTrace
from .errors import APIConnectionError, APIError, APITimeoutError, InvalidRequestError, LongContextNotAvailableError, SystemZeroError, error_class
from .policy import validate_request_policy
from .response import ResponseResult, response_metadata
from .types import DecideReply, DecideRequest, ModelList
from .webhooks import Webhooks
from .enabled_types import EnabledDecideRequest, VideoRequest


VERSION = "0.4.7"
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


def _long_context_unavailable(param: str) -> None:
    raise LongContextNotAvailableError("This request shape is not available for the selected long context class.",
        status=404, type="not_found_error", code="long_context_not_available", param=param)


class SystemZero:
    """Server-side client. Do not ship API keys to a browser.

    Timeouts and retry waits are seconds. The client owns its HTTPX client and
    injected transport; close it or use a context manager. Default clients share
    a process-wide connection pool that remains open until process shutdown.
    The sleep/random/now hooks let tests control pacing without real delays.
    """

    def __init__(
        self, api_key: Optional[str] = None, *, base_url: str = DEFAULT_BASE_URL,
        timeout: float = 150.0, max_retries: int = 5, max_retry_wait: float = 60.0,
        retry_budget: float = 60.0, compress_requests: bool = True,
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
        if not isinstance(compress_requests, bool):
            raise ValueError("compress_requests must be a boolean.")
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
        self._compress_requests = compress_requests
        self._sleep = sleep
        self._random = random
        self._now = now
        self._monotonic = monotonic
        self._closed = False
        self._http = httpx.Client(
            transport=transport if transport is not None else default_transport(),
            timeout=timeout, follow_redirects=False, trust_env=False,
        )
        self.models = Models(self)
        self.webhooks = Webhooks(self)

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

    def get_decision(self, id: str, *, client_request_id: Optional[str] = None,
                     headers: Optional[Mapping[str, str]] = None,
                     retry_budget: Optional[float] = None) -> DecideReply:
        """Read a retained decision made with this API key."""
        return DecisionRecords(self).get_decision(id, client_request_id=client_request_id,
                                                 headers=headers, retry_budget=retry_budget)

    def get_decision_with_response(self, id: str, *, client_request_id: Optional[str] = None,
                                   headers: Optional[Mapping[str, str]] = None,
                                   retry_budget: Optional[float] = None) -> ResponseResult[DecideReply]:
        """Read a retained decision with HTTP response metadata."""
        return DecisionRecords(self).get_decision_with_response(id, client_request_id=client_request_id,
                                                               headers=headers, retry_budget=retry_budget)

    def get_decision_trace(self, id: str, *, client_request_id: Optional[str] = None,
                           headers: Optional[Mapping[str, str]] = None,
                           retry_budget: Optional[float] = None) -> DecisionTrace:
        """Read the content-free trace for a retained decision."""
        return DecisionRecords(self).get_decision_trace(id, client_request_id=client_request_id,
                                                       headers=headers, retry_budget=retry_budget)

    def get_decision_trace_with_response(self, id: str, *, client_request_id: Optional[str] = None,
                                         headers: Optional[Mapping[str, str]] = None,
                                         retry_budget: Optional[float] = None) -> ResponseResult[DecisionTrace]:
        """Read a retained trace with HTTP response metadata."""
        return DecisionRecords(self).get_decision_trace_with_response(id, client_request_id=client_request_id,
                                                                     headers=headers, retry_budget=retry_budget)

    getDecision = get_decision
    getDecisionTrace = get_decision_trace

    def decide(
        self, request: Union[DecideRequest, EnabledDecideRequest, VideoRequest, Mapping[str, Any]], *,
        idempotency_key: Optional[str] = None, client_request_id: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        retry_budget: Optional[float] = None,
        danger_zone: Optional[Literal["accept", "long_context_64k", "long_context_128k", "long_context_1m"]] = None,
    ) -> DecideReply:
        return self.decide_with_response(request, idempotency_key=idempotency_key,
                                         client_request_id=client_request_id, headers=headers,
                                         retry_budget=retry_budget, danger_zone=danger_zone).data

    def decide_with_response(
        self, request: Union[DecideRequest, EnabledDecideRequest, VideoRequest, Mapping[str, Any]], *,
        idempotency_key: Optional[str] = None, client_request_id: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        retry_budget: Optional[float] = None,
        danger_zone: Optional[Literal["accept", "long_context_64k", "long_context_128k", "long_context_1m"]] = None,
    ) -> ResponseResult[DecideReply]:
        if danger_zone == "long_context_1m":
            if not isinstance(request.get("state"), str):
                _long_context_unavailable("state")
            if not isinstance(request.get("questions"), Mapping) or len(request["questions"]) != 1:
                raise InvalidRequestError("The 1M class requires exactly one question.", status=413,
                    type="invalid_request_error", code="questions_too_large", param="questions")
            for name in ("policy", "mode", "route", "session_id", "prompt_cache_key"):
                if name in request:
                    _long_context_unavailable(name)
        validate_request_policy(request)
        outgoing = httpx.Headers(headers)
        if "SZ-Danger-Zone" in outgoing or (danger_zone is not None and danger_zone not in ("accept", "long_context_64k", "long_context_128k", "long_context_1m")):
            raise InvalidRequestError("Use danger_zone with accept, long_context_64k, long_context_128k or long_context_1m; do not supply its header.",
                                      status=400, type="invalid_request_error", code="invalid_request", param="SZ-Danger-Zone")
        return cast(ResponseResult[DecideReply], self._request(
            "POST", "/system-zero/decide", body=request,
            idempotency_key=idempotency_key, client_request_id=client_request_id,
            headers=outgoing, retry_budget=retry_budget, _danger_zone=danger_zone,
        ))

    _request = _request
