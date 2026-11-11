"""HTTPX transport with HTTP-byte tracking and an absolute attempt deadline.

Only public httpcore 1.x interfaces are used. A per-attempt ContextVar associates
pooled network reads with the current call, including when a connection is reused
on another thread. TLS handshake bytes do not count as HTTP response bytes.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import gzip
import json
import sys
import time
import atexit
from threading import Lock
from types import MappingProxyType
from typing import Any, Iterator, Literal, Mapping, Optional, cast
from uuid import uuid4

import httpcore
import httpx

from ._redaction import protected, redact, reveal
from ._connection import ResponseCloseGuard, connection_error, is_transport_error
from ._retry import _recover_workflow, _retry_context, _retry_response, _retry_wait
from .errors import APIError, APITimeoutError, InvalidRequestError, SystemZeroError
from .response import ResponseResult, response_metadata


@dataclass
class AttemptState:
    deadline: float
    response_started: bool = False
    request_sent: Optional[bool] = None


attempt_state: ContextVar[Optional[AttemptState]] = ContextVar("system_zero_attempt", default=None)


def _timeout(timeout: Optional[float], error_type: type[Exception]) -> Optional[float]:
    state = attempt_state.get()
    if state is None:
        return timeout
    remaining = state.deadline - time.monotonic()
    if remaining <= 0:
        raise error_type("RightNow attempt deadline exceeded")
    return min(timeout, remaining) if timeout is not None else remaining


class _TrackedStream(httpcore.NetworkStream):
    def __init__(self, stream: httpcore.NetworkStream):
        self._stream = stream

    def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
        data = self._stream.read(max_bytes, timeout=_timeout(timeout, httpcore.ReadTimeout))
        if data:
            state = attempt_state.get()
            if state is not None:
                state.response_started = True
                state.request_sent = True
        return data

    def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
        # Refresh the absolute budget between bounded writes, as well as reads.
        for offset in range(0, len(buffer), 65536):
            remaining = _timeout(timeout, httpcore.WriteTimeout)
            state = attempt_state.get()
            if state is not None:
                # Once write begins an exception cannot prove that zero bytes left.
                state.request_sent = True
            self._stream.write(buffer[offset:offset + 65536], timeout=remaining)

    def close(self) -> None:
        self._stream.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _TrackedStream(self._stream.start_tls(
            ssl_context=ssl_context, server_hostname=server_hostname,
            timeout=_timeout(timeout, httpcore.ConnectTimeout),
        ))

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _TrackedBackend(httpcore.NetworkBackend):
    def __init__(self):
        self._backend = httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return _TrackedStream(self._backend.connect_tcp(
            host=host, port=port, timeout=_timeout(timeout, httpcore.ConnectTimeout),
            local_address=local_address, socket_options=socket_options,
        ))

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return _TrackedStream(self._backend.connect_unix_socket(
            path=path, timeout=_timeout(timeout, httpcore.ConnectTimeout), socket_options=socket_options,
        ))

    def sleep(self, seconds):
        self._backend.sleep(seconds)


_CORE_ERRORS = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.ProxyError, httpx.ProxyError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
)


@contextmanager
def _map_errors():
    try:
        yield
    except Exception as exc:
        for source, target in _CORE_ERRORS:
            if isinstance(exc, source):
                raise target("RightNow HTTP transport failed") from exc
        raise


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        with _map_errors():
            yield from self._stream

    def close(self) -> None:
        self._stream.close()


# Seconds an idle connection stays in the pool. The service keeps a caller's idle connection open for minutes, so a
# caller that pauses between calls reuses it instead of paying a new TCP and TLS handshake. It was 5.
KEEPALIVE_EXPIRY_SECONDS = 240


class DeadlineTransport(httpx.BaseTransport):
    """Default synchronous transport; injected HTTPX transports also work.

    HTTP/1.1 uses validated TLS, keepalive pooling, and no hidden retries. Proxy
    routing is explicit through an injected transport rather than environment
    configuration. No private HTTPX/httpcore attributes are accessed.
    """

    def __init__(self):
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpcore.default_ssl_context(), network_backend=_TrackedBackend(),
            max_connections=100, max_keepalive_connections=20, keepalive_expiry=KEEPALIVE_EXPIRY_SECONDS,
            http1=True, http2=False, retries=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        state = attempt_state.get()
        if state is not None and state.request_sent is None:
            state.request_sent = False
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme, host=request.url.raw_host,
                port=request.url.port, target=request.url.raw_path,
            ),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions,
        )
        with _map_errors():
            response = self._pool.handle_request(core_request)
        return httpx.Response(
            status_code=response.status, headers=response.headers,
            stream=_ResponseStream(response.stream), extensions=response.extensions,
        )

    def close(self) -> None:
        self._pool.close()


_shared_transport: Optional[DeadlineTransport] = None
_shared_lock = Lock()


class _SharedTransport(httpx.BaseTransport):
    def __init__(self, transport: DeadlineTransport):
        self._transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._transport.handle_request(request)

    def close(self) -> None:
        # A client's lifetime must not close another client's connections.
        pass


def default_transport() -> httpx.BaseTransport:
    global _shared_transport
    with _shared_lock:
        if _shared_transport is None:
            _shared_transport = DeadlineTransport()
            atexit.register(_shared_transport.close)
        return _SharedTransport(_shared_transport)


# Prepare the logical HTTP request before the transport attempt loop.
@protected
def _request(
    self, method: str, path: str, *, body: Optional[Mapping[str, Any]] = None,
    idempotency_key: Optional[str] = None, client_request_id: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    generate_idempotency: bool = True, retries: Optional[int] = None,
    _danger_zone: Optional[Literal["accept", "long_context_64k", "long_context_128k", "long_context_1m"]] = None,
    retry_budget: Optional[float] = None,
) -> ResponseResult[Any]:
    # The client imports this module to bind _request, so these helpers are deferred.
    from .client import VERSION, _api_error, _checked_base_url, _long_context_unavailable, _validate_retry_budget

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
    if "SZ-Danger-Zone" in outgoing or (_danger_zone is not None and _danger_zone not in ("accept", "long_context_64k", "long_context_128k", "long_context_1m")):
        raise InvalidRequestError("Use danger_zone with accept, long_context_64k, long_context_128k or long_context_1m; do not supply its header.",
                                  status=400, type="invalid_request_error", code="invalid_request", param="SZ-Danger-Zone")
    if _danger_zone is not None and method == "POST" and path == "/system-zero/decide":
        if _danger_zone != "accept" and body is not None and (body.get("attachments") or
                _danger_zone == "long_context_1m" and body.get("attachments") is not None and body.get("attachments") != []):
            _long_context_unavailable("attachments")
        outgoing["SZ-Danger-Zone"] = _danger_zone
    outgoing["Authorization"] = "Bearer " + reveal(self._api_key)
    outgoing["User-Agent"] = "system-zero-python/" + VERSION + " python/" + ".".join(map(str, sys.version_info[:3]))
    outgoing["Accept"] = "application/json"
    if body is not None:
        outgoing["Content-Type"] = "application/json"
    if client_request_id is not None:
        outgoing["X-Client-Request-Id"] = client_request_id
    one_million = _danger_zone == "long_context_1m"
    if one_million:
        key = idempotency_key if idempotency_key is not None else outgoing.get("Idempotency-Key")
        if key is not None and not key.strip():
            raise InvalidRequestError("The 1M class requires a nonblank Idempotency-Key.", status=400,
                type="invalid_request_error", code="invalid_request", param="Idempotency-Key")
    if method == "POST" and generate_idempotency:
        if idempotency_key is None:
            idempotency_key = outgoing.get("Idempotency-Key")
        if idempotency_key is None and (one_million or max_retries > 0):
            idempotency_key = "sz-sdk-" + str(uuid4())
        if idempotency_key is not None:
            outgoing["Idempotency-Key"] = idempotency_key
    if "Idempotency-Key" in outgoing and not outgoing["Idempotency-Key"].strip():
        del outgoing["Idempotency-Key"]
    if "X-Client-Request-Id" in outgoing and not outgoing["X-Client-Request-Id"].strip():
        del outgoing["X-Client-Request-Id"]
    used_key = outgoing.get("Idempotency-Key")
    # Python dict insertion order is preserved. Serialize once for all attempts.
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if body is not None else None
    if one_million and payload is not None and len(payload) > 8_388_608:
        raise InvalidRequestError("The request body exceeds 8,388,608 bytes.", status=413,
            type="invalid_request_error", code="payload_too_large", param="body")
    # Prepare once; retries reuse the same payload, key, and compressed length.
    if self._compress_requests and method == "POST" and path == "/system-zero/decide" and payload is not None and len(payload) >= 32_768:
        compressed = gzip.compress(payload, compresslevel=4, mtime=0)
        if len(compressed) * 10 <= len(payload) * 9:
            payload = compressed
            outgoing["Content-Encoding"] = "gzip"
            outgoing["Content-Length"] = str(len(payload))
    safe_replay = method in {"GET", "HEAD", "OPTIONS"} or bool(outgoing.get("Idempotency-Key", "").strip())
    started = self._monotonic()
    retry_deadline = started + budget if context.get("workflow_recovery") else float("inf")
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
                cold_pool = one_million and response.status_code == 503 and error.code == "service_unavailable"
                should_retry = (safe_replay and not response.is_success and
                                error.type not in {"authentication_error", "permission_error"} and
                                (cold_pool or (_recover_workflow(response.status_code, error.code, response.headers, workflow_marker, error.details)
                                 if context.get("workflow_recovery") else
                                 _retry_response(response.status_code, error.code, response.headers, workflow_marker))))
                if workflow_marker is None and should_retry:
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
        if attempt == 0 and not context.get("workflow_recovery"):
            retry_deadline = self._monotonic() + budget
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
