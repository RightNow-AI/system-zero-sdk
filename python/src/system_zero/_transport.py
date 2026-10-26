"""HTTPX transport with HTTP-byte tracking and an absolute attempt deadline.

Only public httpcore 1.x interfaces are used. A per-attempt ContextVar associates
pooled network reads with the current call, including when a connection is reused
on another thread. TLS handshake bytes do not count as HTTP response bytes.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time
from typing import Any, Iterator, Optional

import httpcore
import httpx


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


class DeadlineTransport(httpx.BaseTransport):
    """Default synchronous transport; injected HTTPX transports also work.

    HTTP/1.1 uses validated TLS, keepalive pooling, and no hidden retries. Proxy
    routing is explicit through an injected transport rather than environment
    configuration. No private HTTPX/httpcore attributes are accessed.
    """

    def __init__(self):
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpcore.default_ssl_context(), network_backend=_TrackedBackend(),
            max_connections=100, max_keepalive_connections=20, keepalive_expiry=5,
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
