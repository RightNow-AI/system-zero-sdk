"""Async server-side stream v1 client. Reconnect replays state, never old frames."""

from ._redaction import Secret, DiagnosticLogger, protect_exception, protected, protected_async, redact, reveal

import asyncio
from collections import deque
from contextlib import nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import ipaddress
import json
import math
import os
import random as random_module
import re
import sys
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Optional, cast
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.protocol import State
from websockets.frames import OP_PONG

from .errors import APIConnectionError, APIError, APITimeoutError, StreamLimitError, SystemZeroError, error_class
from ._connection import connection_error, is_transport_error
from ._retry import _retry_after_seconds as _capacity_retry_after_seconds, _retry_response
from .stream_interfaces import (
    DecisionMode, EventLatency, ReconnectPolicy, StreamAck, StreamDecision,
    StreamEvent, StreamHello, StreamScheduled,
)
from .types import Attachment, DecideRequest


STREAM_PROTOCOL = "system-zero.stream.v1"
DEFAULT_STREAM_URL = "wss://api.rightnowai.co/v1/system-zero/stream"
_RETRYABLE_CLOSE_CODES = frozenset({1001, 1006, 1011, 1012, 1013})
_TIMER_MAX_DELAY_SECONDS = 2_147_483_647 / 1000
_NATIVE_PONG_TIMEOUT_SECONDS = 10.0
# Gateway lifetime 3600s + maximum flush/drain 203s + RFC close 5s + delivery margin 10s.
# The older 240s hello hint cannot bound a progressing ordinary reply; gateway-limit tests pin this ceiling.
_RECEIPT_TIMEOUT_SECONDS = 3818.0
_STREAM_ID = re.compile(r"szws_[a-f0-9]{32}\Z")


def _checked_stream_key(value: str) -> str:
    if not isinstance(value, str) or not value or any(not 33 <= ord(character) <= 126 for character in value):
        raise ValueError("api_key must contain only visible ASCII characters.")
    return value


def _checked_stream_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        local = host == "".join(("local", "host"))
        if host and not local:
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (parsed.scheme != "wss" and not (parsed.scheme == "ws" and local)
                or not host or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "?" in value or "#" in value):
            raise ValueError()
        parsed.port
    except (AttributeError, TypeError, ValueError):
        raise ValueError("stream_url must be WSS or loopback WS without credentials, query or fragment.") from None
    return value


class StreamDisconnectedError(APIConnectionError):
    """A connection ended; an unseen reply may have completed and been counted."""

    def __init__(self, message: str = "Stream disconnected; outstanding work may have been decided.",
                 *, close_code: Optional[int] = None):
        super().__init__(message, retryable=False)
        self.close_code = close_code


class StreamProtocolError(APIError):
    """Invalid server protocol data is terminal, rather than retried indefinitely."""

    def __init__(self, message: str, *, close_code: int = 1002):
        super().__init__(message, retryable=False)
        self.close_code = close_code


def reconnect_delay(attempt: int, draw: float) -> float:
    """Full jitter, in seconds; the first ceiling is 0.5 and the cap is 30."""
    return min(30.0, 0.5 * 2 ** min(max(0, attempt), 6)) * min(1.0, max(0.0, draw))


def _nonnegative(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _retry_after_seconds(headers: Mapping[str, str], now: float) -> Optional[float]:
    return _capacity_retry_after_seconds(headers, lambda: now)


def retry_after_delay(headers: Mapping[str, str], now: float) -> float:
    """Honor every valid server minimum without capping it at jitter."""
    return _retry_after_seconds(headers, now) or 0.0


def _stream_retryable(status: int, body: Mapping[str, Any], headers: Mapping[str, str], ever_opened: bool) -> bool:
    if body.get("type") in {"authentication_error", "permission_error"}:
        return False
    code = body.get("code") if isinstance(body.get("code"), str) else None
    retryable = body.get("retryable") if type(body.get("retryable")) is bool else None
    hint = headers.get("x-should-retry", "").strip()
    
    recovery = (status == 503 and code == "limiter_unavailable" or
                status == 429 and code == "stream_limit_exceeded" and ever_opened)
    return (_retry_response(status, code, headers, retryable) or
            recovery and retryable is not False and hint != "false" and (retryable is True or hint == "true"))


def _upgrade_error(error: InvalidStatus, now: float, ever_opened: bool) -> tuple[SystemZeroError, bool]:
    response = error.response
    headers = {key.lower(): value for key, value in response.headers.items()}
    try:
        envelope = json.loads(response.body)
    except (ValueError, TypeError, UnicodeError):
        envelope = None
    body = envelope.get("error") if isinstance(envelope, dict) else None
    hint = headers.get("x-should-retry", "").strip()
    retryable = ((hint == "true") if hint in {"true", "false"} else
                 body["retryable"] if isinstance(body, dict) and type(body.get("retryable")) is bool else
                 response.status_code in {429, 500, 502, 503, 504, 524})
    envelope_retryable = body["retryable"] if isinstance(body, dict) and type(body.get("retryable")) is bool else retryable
    should_retry = _stream_retryable(response.status_code, body if isinstance(body, dict) else {}, headers, ever_opened)
    if response.status_code == 429 and isinstance(body, dict) and body.get("code") == "stream_limit_exceeded":
        # This diagnostic field retains the raw milliseconds header when present;
        # actual scheduling below honors both server minima.
        milliseconds = _nonnegative(headers.get("retry-after-ms"))
        delay = milliseconds / 1000 if milliseconds is not None else _retry_after_seconds(headers, now)
        return StreamLimitError(
            body.get("message", "Account stream limit exceeded."), status=429,
            type=body.get("type", "rate_limit_error"), code="stream_limit_exceeded",
            param=body.get("param"), retryable=envelope_retryable,
            details=body.get("details") if isinstance(body.get("details"), dict) else None,
            request_id=envelope.get("request_id", headers.get("x-request-id")), headers=headers,
            retry_after_ms=None if delay is None else delay * 1000,
            retry_after=headers.get("retry-after"),
            should_retry=retryable,
        ), should_retry
    details = body if isinstance(body, dict) else {}
    kind = details.get("type", "api_error")
    cls = error_class(kind, details.get("code")) if isinstance(body, dict) else APIConnectionError
    return cls(details.get("message", "Stream upgrade failed with HTTP %d." % response.status_code),
                              status=response.status_code, type=details.get("type", "api_error"),
                              code=details.get("code"), param=details.get("param"), retryable=envelope_retryable,
                              details=details.get("details") if isinstance(details.get("details"), dict) else None,
                              request_id=envelope.get("request_id", headers.get("x-request-id"))
                              if isinstance(envelope, dict) else headers.get("x-request-id"), headers=headers), should_retry


def _integer(value: Any, minimum: int = 0, maximum: int = 9007199254740991) -> bool:
    return type(value) is int and minimum <= value <= maximum


_task_owner = ContextVar("system_zero_stream_task_owner", default=None)


def _current_owner():
    current = asyncio.current_task()
    owner = _task_owner.get()
    # A transport may re-enter close(). Preserve the logical caller without
    # treating independently created tasks inheriting this context as owners.
    return owner[1] if owner is not None and owner[0] is current else current


def _future() -> asyncio.Future:
    future = asyncio.get_running_loop().create_future()
    # A latency is optional to await. Retrieve failures even when nobody does.
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return future


@dataclass
class _Timing:
    sent: float
    future: asyncio.Future
    version: Optional[int] = None
    scheduled: Optional[bool] = None


@dataclass
class _Mutation:
    future: asyncio.Future
    event_id: str
    version: int
    kind: str
    fields: dict[str, Any]
    idempotency_key: str = field(default_factory=lambda: "szsdk_" + uuid4().hex)
    own_key: str = field(init=False)

    def __post_init__(self) -> None:
        self.own_key = self.idempotency_key


class _Ack(dict):
    """Retain this event's optional timer with the caller's ack, not SDK history."""

    def __init__(self, frame: Mapping[str, Any], timing: _Timing, owner: object):
        super().__init__(frame)
        self.timing = timing
        self.owner = owner


def stream_error(frame: Mapping[str, Any]) -> SystemZeroError:
    """Read an error event as a typed exception without altering the event.

    Decide failures remain events; callers choose whether to raise the returned
    exception and retain its status, details and response headers.
    """
    envelope = frame["envelope"]
    error = envelope["error"]
    kind = error.get("type", "api_error")
    cls = error_class(kind, error.get("code"))
    retry = frame.get("retry", {})
    headers = {}
    if "should_retry" in retry:
        headers["x-should-retry"] = str(retry["should_retry"]).lower()
    if retry.get("retry_after_ms") is not None:
        headers["retry-after-ms"] = str(retry["retry_after_ms"])
    if retry.get("retry_after") is not None:
        headers["retry-after"] = str(retry["retry_after"])
    return cls(error.get("message", "Stream request failed."), status=frame["http_status"],
               type=kind, code=error.get("code"), param=error.get("param"),
               retryable=error.get("retryable", False), details=error.get("details"),
               request_id=envelope.get("request_id"), headers=headers)


_remote_error = stream_error


class _EventSubscriber(AsyncIterator[StreamEvent]):
    """One bounded subscription; terminal state never consumes a queue slot."""

    def __init__(self, owner: "SystemZeroStreamClient"):
        self._owner: Optional[SystemZeroStreamClient] = owner
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._changed = asyncio.Event()
        self._done = False
        self._error: Optional[BaseException] = None

    def __aiter__(self) -> "_EventSubscriber":
        return self

    async def __anext__(self) -> StreamEvent:
        while True:
            if not self._queue.empty():
                return cast(StreamEvent, self._queue.get_nowait())
            if self._done:
                self._detach()
                error, self._error = self._error, None
                if error is not None:
                    raise error
                raise StopAsyncIteration
            self._changed.clear()
            await self._changed.wait()

    def _detach(self) -> None:
        if self._owner is not None:
            self._owner._subscribers.discard(self)
            self._owner = None

    def _finish(self, error: Optional[BaseException] = None, *, discard: bool = False) -> None:
        self._done = True
        if discard:
            while not self._queue.empty():
                self._queue.get_nowait()
            self._error = None
            self._detach()
        elif error is not None:
            self._error = error
        self._changed.set()

    async def aclose(self) -> None:
        self._finish(discard=True)

    async def athrow(self, error: BaseException) -> None:
        await self.aclose()
        raise error


class SystemZeroStreamClient(AsyncIterator[StreamEvent]):
    """One logical state across ephemeral WebSocket connections.

    ``latest`` permits the reconnect start to decide the latest state once when
    the original mode is on_change. ``never`` replays in on_request, requiring an
    explicit decide after reconnect. Neither policy resends old decide commands.
    Time hooks and waits use seconds. Event latencies use monotonic milliseconds.
    """

    def __init__(self, api_key: Optional[str] = None, *, stream_url: str = DEFAULT_STREAM_URL,
                 reconnect_policy: ReconnectPolicy = "latest",
                 max_reconnect_attempts: int = 8, reconnect_timeout: float = 120.0,
                 receipt_timeout: float = 30.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 random: Callable[[], float] = random_module.random,
                 monotonic: Callable[[], float] = time.monotonic,
                 now: Callable[[], float] = time.time):
        key = api_key if api_key is not None else os.environ.get("SYSTEM_ZERO_API_KEY")
        if key is None:
            raise ValueError("Set api_key or SYSTEM_ZERO_API_KEY.")
        key = _checked_stream_key(key)
        stream_url = _checked_stream_url(stream_url)
        if reconnect_policy not in {"latest", "never"}:
            raise ValueError("reconnect_policy must be latest or never.")
        if not _integer(max_reconnect_attempts, 1):
            raise ValueError("max_reconnect_attempts must be a positive finite integer.")
        for name, value in (("reconnect_timeout", reconnect_timeout), ("receipt_timeout", receipt_timeout)):
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not 0 < value <= _TIMER_MAX_DELAY_SECONDS):
                raise ValueError(name + " must be positive, finite and at most 2147483.647 seconds.")
        self._api_key = Secret(key)
        self.stream_url = stream_url
        self.reconnect_policy = reconnect_policy
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_timeout, self.receipt_timeout = reconnect_timeout, receipt_timeout
        self._sleep, self._random = sleep, random
        self._monotonic, self._now = monotonic, now
        self._socket: Optional[ClientConnection] = None
        self._runner: Optional[asyncio.Task] = None
        self._reader: Optional[asyncio.Task] = None
        self._replayer: Optional[asyncio.Task] = None
        self._drainer: Optional[asyncio.Task] = None
        self._decision_task: Optional[asyncio.Task] = None
        self._ready: Optional[asyncio.Future] = None
        self._reconnect_requested = asyncio.Event()
        self._idle = False
        self._event_rate = 0.0
        self._event_burst = self._event_tokens = 1.0
        self._event_at = 0.0
        self._native_buckets = []
        self._native_tasks: set[asyncio.Task] = set()
        self._native_queue_limit = 2
        self._native_restore: Optional[Callable[[], None]] = None
        self._message_rate = 0.0
        self._message_burst = self._message_tokens = 1.0
        self._message_at = 0.0
        self._message_bytes = 4194304
        self._pacing_wake = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._native_write_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._queue: Optional[asyncio.Queue] = None
        self._subscribers: set[_EventSubscriber] = set()
        self._default_subscriber: Optional[_EventSubscriber] = None
        self._events_ended = False
        self._overflow_error: Optional[StreamDisconnectedError] = None
        self._closed = False
        self._cleaning_connection = False
        self._disconnecting = False
        self._terminal_error: Optional[BaseException] = None
        self._connection_error: Optional[SystemZeroError] = None
        self._transport_failure: Optional[asyncio.Future] = None
        self._request_sent: Optional[bool] = None
        self._request: Optional[dict[str, Any]] = None
        self._prompt_cache_key = "szsdk_" + uuid4().hex
        self._desired_state: Any = None
        self._accepted_state: Any = None
        self._desired_attachments: Any = None
        self._accepted_attachments: Any = None
        self._accepted_idempotency_key: Optional[str] = None
        self._refused_idempotency_key: Optional[str] = None
        self._replay_on_request = False
        self._decision_mode: DecisionMode = "on_change"
        self._effective_mode: DecisionMode = "on_change"
        self._min_interval_ms = 0
        self._hello: Optional[StreamHello] = None
        self._stream_id: Optional[str] = None
        self._seq = 0
        self._version = 0
        self._last_decision_version = 0
        self._last_decision: Optional[StreamDecision] = None
        self._last_ping = 0.0
        self._heartbeat_interval = 30.0
        self._native_heartbeat_interval: Optional[float] = None
        self._retry_not_before = 0.0
        self._attempt = 0
        self._opening_started: Optional[float] = None
        self._opening_attempts = 0
        self._opening_failure: Optional[BaseException] = None
        self._ever_opened = False
        self._receipt_changed = asyncio.Event()
        self._receipt_future: Optional[asyncio.Future] = None
        self._receipt_deadline = self._receipt_inactivity = 0.0
        self._active_mutation: Optional[_Mutation] = None
        self._queued_mutations: list[_Mutation] = []
        self._replay_included: list[_Mutation] = []
        self._replay_state: Any = None
        self._replay_attachments: Any = None
        self._pending_schedule: Optional[asyncio.Future] = None
        self._pending_schedule_version = 0
        self._pending_schedule_prior_work = False
        self._explicit_intent = False
        self._outstanding: set[int] = set()
        self._event_versions: dict[str, int] = {}
        self._timings: dict[tuple[str, str], _Timing] = {}
        self._latencies: deque[EventLatency] = deque(maxlen=16)
        self._ack_owner = object()

    @property
    def desired_state(self) -> Any:
        return deepcopy(self._desired_state)

    @property
    def state_version(self) -> int:
        return self._version

    @property
    def last_decision(self) -> Optional[StreamDecision]:
        return self._last_decision

    @property
    def last_decision_stale(self) -> bool:
        return (self._last_decision is None or self._last_decision.get("stream_id") != self._stream_id
                or self._last_decision_version < self._version or self._active_mutation is not None)

    @property
    def event_latencies(self) -> tuple[EventLatency, ...]:
        return tuple(self._latencies)

    async def __aenter__(self) -> "SystemZeroStreamClient":
        try:
            await self.connect()
        except BaseException:
            # Failed entry does not call __aexit__. Finish owned cleanup even if
            # the caller cancels again, then preserve the original entry failure.
            cleanup = asyncio.create_task(self.close())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if not cleanup.cancelled():
                cleanup.exception()
            raise
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()

    def __aiter__(self) -> "SystemZeroStreamClient":
        self._subscribe()
        return self

    async def __anext__(self) -> StreamEvent:
        self._subscribe()
        return await cast(_EventSubscriber, self._default_subscriber).__anext__()

    def events(self) -> _EventSubscriber:
        """Create an independent 64-event subscriber; await aclose() on early exit."""
        subscriber = _EventSubscriber(self)
        self._subscribers.add(subscriber)
        if self._default_subscriber is None:
            self._default_subscriber = subscriber
            self._queue = subscriber._queue
        if self._closed or self._events_ended:
            subscriber._finish(discard=True)
        return subscriber

    def _subscribe(self) -> None:
        if self._default_subscriber is None:
            self.events()

    def _emit(self, event: Any, *, closing: bool = False) -> None:
        if isinstance(event, dict) and event.get("type") == "error":
            event = redact(event)
        if self._closed or self._events_ended or self._overflow_error is not None:
            return
        subscribers = tuple(self._subscribers)
        if not closing and any(subscriber._queue.full() for subscriber in subscribers):
            self._overflow_error = StreamDisconnectedError("Stream event consumer fell behind.", close_code=1008)
            self._terminal_error = self._overflow_error
            raise self._overflow_error
        for subscriber in subscribers:
            if not subscriber._done and not subscriber._queue.full():
                subscriber._queue.put_nowait(event)
                subscriber._changed.set()

    def _end_events(self, *, discard: bool = False) -> None:
        self._events_ended = True
        for subscriber in tuple(self._subscribers):
            subscriber._finish(self._overflow_error, discard=discard)

    @protected_async
    async def connect(self) -> StreamHello:
        self._check_open()
        if self._runner is None:
            self._ready = _future()
            self._runner = asyncio.create_task(self._safe_task(self._run))
        elif self._idle:
            self._reconnect_requested.set()
        hello = cast(StreamHello, await asyncio.shield(cast(asyncio.Future, self._ready)))
        self._check_open()
        return hello

    def _check_open(self) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._closed:
            raise StreamDisconnectedError("Stream client is closed.")

    async def _available(self) -> None:
        await self.connect()
        self._check_open()

    async def start(self, request: DecideRequest, *, decision_mode: DecisionMode = "on_change",
                    min_interval_ms: int = 0) -> StreamAck:
        if decision_mode not in {"on_change", "on_request"}:
            raise ValueError("decision_mode must be on_change or on_request.")
        if not _integer(min_interval_ms, 0, 60000):
            raise ValueError("min_interval_ms must be an integer from 0 to 60000.")
        await self._available()
        if self._request is not None:
            raise ValueError("start is allowed once per logical stream client.")
        copied = deepcopy(dict(request))
        if copied.get("prompt_cache_key") is None:
            copied["prompt_cache_key"] = self._prompt_cache_key
        self._request = copied
        self._desired_state = deepcopy(copied["state"])
        self._desired_attachments = deepcopy(copied.get("attachments"))
        self._decision_mode = self._effective_mode = decision_mode
        self._min_interval_ms = min_interval_ms
        return await self._submit("start", {"request": copied, "decision_mode": decision_mode,
                                            "min_interval_ms": min_interval_ms})

    async def append_text(self, text: str) -> StreamAck:
        self._check_open()
        self._require_started()
        if not isinstance(text, str) or not text or not isinstance(self._desired_state, str):
            raise ValueError("append_text requires non-empty text and string state.")
        self._desired_state += text
        return await self._submit("append_text", {"text": text})

    async def append_items(self, items: list[Any]) -> StreamAck:
        self._check_open()
        self._require_started()
        if not isinstance(items, list) or not 1 <= len(items) <= 4096 or not isinstance(self._desired_state, list):
            raise ValueError("append_items requires 1 to 4096 items and array state.")
        copied = deepcopy(items)
        self._desired_state = self._desired_state + copied
        return await self._submit("append_items", {"items": copied})

    async def append_attachments(self, attachments: list[Attachment]) -> StreamAck:
        self._check_open()
        self._require_started()
        if not isinstance(attachments, list) or not attachments:
            raise ValueError("append_attachments requires a non-empty attachment list.")
        return await self._submit("append_attachments", {"attachments": deepcopy(attachments)})

    async def replace(self, state: Any, *, attachments: Optional[list[Attachment]] = None) -> StreamAck:
        self._check_open()
        self._require_started()
        fields = {"state": deepcopy(state)}
        if attachments is not None:
            fields["attachments"] = deepcopy(attachments)
        return await self._submit("replace", fields)

    def _require_started(self) -> None:
        if self._request is None:
            raise ValueError("Call start before changing or deciding state.")

    async def _submit(self, kind: str, fields: dict[str, Any]) -> StreamAck:
        mutation = _Mutation(_future(), "", 0, kind, fields)
        self._queued_mutations.append(mutation)
        self._rebuild_desired()
        if self._drainer is None or self._drainer.done():
            self._drainer = asyncio.create_task(self._drain_mutations())
        return cast(StreamAck, await asyncio.shield(mutation.future))

    async def _drain_mutations(self) -> None:
        # Queue desired changes immediately, but send each only after the prior ack.
        try:
            while self._queued_mutations and not self._closed:
                async with self._operation_lock:
                    await self._available()
                    if not self._queued_mutations:
                        continue
                    mutation = self._queued_mutations[0]
                    if self._replay_on_request and mutation.idempotency_key != self._refused_idempotency_key:
                        # Resume on_change only with the changed state on a new
                        # connection, never by deciding the refused replay.
                        self._ready = _future()
                        # A deliberate connection handover retains queued work;
                        # ordinary 1000 closure intentionally rejects that queue.
                        await self._safe_task(lambda: cast(ClientConnection, self._socket).close(code=1001))
                        await self._available()
                        continue
                    try:
                        await self._mutate(mutation)
                    except (SystemZeroError, ValueError, TypeError, UnicodeError):
                        # The rejected caller owns its error; later valid events continue.
                        pass
        except asyncio.CancelledError:
            pass
        except BaseException as error:
            self._reject_pending(error)

    async def _mutate(self, mutation: _Mutation) -> StreamAck:
        mutation.version = self._version + 1
        mutation.event_id = "evt_%d" % mutation.version
        self._active_mutation = mutation
        frame = {"type": mutation.kind, "event_id": mutation.event_id, **mutation.fields}
        try:
            await self._send_mutation(frame, mutation)
        except (ConnectionClosed, OSError, APIConnectionError) as error:
            if isinstance(error, ConnectionClosed) and error.rcvd is not None and error.rcvd.code == 1000:
                self._reconnect_requested.set()
            # The reader owns reconnect. Desired state already includes this event.
            pass
        except BaseException as error:
            if not mutation.future.done():
                self._reject_mutation(mutation, error)
        return cast(StreamAck, await asyncio.shield(mutation.future))

    @staticmethod
    def _apply(state: Any, mutation: _Mutation) -> Any:
        fields = mutation.fields
        if mutation.kind == "start":
            return deepcopy(fields["request"]["state"])
        if mutation.kind == "replace":
            return deepcopy(fields["state"])
        if mutation.kind == "append_text" and isinstance(state, str):
            return state + fields["text"]
        if mutation.kind == "append_items" and isinstance(state, list):
            return state + deepcopy(fields["items"])
        if mutation.kind == "append_attachments":
            return state
        raise ValueError("Queued append no longer matches the accepted state kind.")

    @staticmethod
    def _apply_attachments(attachments: Any, mutation: _Mutation) -> Any:
        if mutation.kind == "start":
            return deepcopy(mutation.fields["request"].get("attachments"))
        if mutation.kind == "replace" and "attachments" in mutation.fields:
            return deepcopy(mutation.fields["attachments"])
        if mutation.kind == "append_attachments":
            return (attachments or []) + deepcopy(mutation.fields["attachments"])
        return attachments

    def _rebuild_desired(self) -> None:
        state = deepcopy(self._accepted_state)
        attachments = deepcopy(self._accepted_attachments)
        key = self._accepted_idempotency_key
        retained = []
        for mutation in self._queued_mutations:
            try:
                next_state = self._apply(state, mutation)
                next_attachments = self._apply_attachments(attachments, mutation)
                # Compare wire content, including order and JSON value types.
                before = json.dumps([state, attachments], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                after = json.dumps([next_state, next_attachments], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                # A refusal can turn a queued no-op into a change. Retain its
                # own key so two different rebased bodies never share a key.
                mutation.idempotency_key = key if key is not None and before == after else mutation.own_key
                state, attachments, key = next_state, next_attachments, mutation.idempotency_key
                retained.append(mutation)
            except (ValueError, TypeError, UnicodeError) as error:
                if mutation.kind == "start":
                    self._request = None
                if not mutation.future.done():
                    mutation.future.set_exception(error)
        self._queued_mutations = retained
        self._desired_state = state
        self._desired_attachments = attachments

    def _reject_mutation(self, mutation: _Mutation, error: BaseException) -> None:
        self._clear_receipt(mutation.future)
        if mutation in self._queued_mutations:
            self._queued_mutations.remove(mutation)
        if mutation.kind == "start":
            self._request = None
        self._active_mutation = None
        timing = self._timings.get((cast(str, self._stream_id), mutation.event_id))
        if timing is not None and not timing.future.done():
            timing.future.set_exception(error)
        self._timings.pop((cast(str, self._stream_id), mutation.event_id), None)
        self._outstanding.discard(mutation.version)
        self._event_versions.pop(mutation.event_id, None)
        if not mutation.future.done():
            mutation.future.set_exception(error)
        self._rebuild_desired()

    def _configure_limits(self, limits: Mapping[str, Any]) -> None:
        for key in (
            'max_duration_ms',
            'idle_timeout_ms',
            'start_timeout_ms',
            'heartbeat_interval_ms',
            'native_heartbeat_interval_ms',
            'max_control_queue_frames',
            'pong_timeout_ms',
            'max_message_bytes',
            'max_decide_body_bytes',
            'max_pending_events',
            'max_events',
            'max_events_per_second',
            'event_burst',
            'max_messages_per_second',
            'message_burst',
            'max_streams_per_process',
            'max_send_queue_bytes',
            'max_send_queue_frames',
            'min_interval_ms',
            'max_native_control_frames_per_second',
            'native_control_frame_burst',
            'max_native_control_bytes_per_second',
            'native_control_byte_burst',
        ):
            value = limits.get(key)
            if not isinstance(value, (int, float)) or _nonnegative(value) is None or (key != "min_interval_ms" and value == 0):
                raise StreamProtocolError(f"Server hello requires a valid {key} limit.")
        rate, burst, size = limits["max_events_per_second"], limits["event_burst"], limits["max_message_bytes"]
        if not _integer(burst, 1) or not _integer(size, 1):
            raise StreamProtocolError("Server send limits are invalid.")
        self._event_rate, self._event_burst = float(rate), float(burst)
        self._event_tokens, self._event_at = float(burst), self._monotonic()
        self._message_bytes = min(size, 4194304)
        message_rate, message_burst = limits["max_messages_per_second"], limits["message_burst"]
        if not _integer(message_burst, 1):
            raise StreamProtocolError("Server message limits are invalid.")
        self._message_rate, self._message_burst = float(message_rate), float(message_burst)
        self._message_tokens, self._message_at = float(message_burst), self._monotonic()
        self._pacing_wake = asyncio.Event()
        self._native_buckets = []
        for rate_key, burst_key in [
            ("max_native_control_frames_per_second", "native_control_frame_burst"),
            ("max_native_control_bytes_per_second", "native_control_byte_burst"),
        ]:
            rate, burst = limits[rate_key], limits[burst_key]
            if not _integer(burst, 1):
                raise StreamProtocolError("Server native control limits are invalid.")
            self._native_buckets.append([float(rate), float(burst), float(burst), self._monotonic()])
        queue = limits["max_control_queue_frames"]
        if not _integer(queue, 1):
            raise StreamProtocolError("Server native control queue limit is invalid.")
        self._native_queue_limit = min(queue, 2)

    @protected
    def _safe_failure(self, error: BaseException) -> BaseException:
        # Retained Futures and Tasks are ordinary client diagnostics. Establish
        # the owning key even when a protocol callback runs in another context.
        return protect_exception(error) if isinstance(error, Exception) else error

    async def _await_native(self, awaitable: Awaitable) -> Any:
        # Drive the native await iterator without making it another Task. Each
        # yielded Future is observed by a callback; this Task awaits only a
        # neutral signal, including the turn before that callback can run.
        iterator = awaitable.__await__()
        value = failure = None
        try:
            while True:
                try:
                    if failure is None:
                        waiting = iterator.send(value)
                    else:
                        error, failure = failure, None
                        waiting = iterator.throw(error)
                except StopIteration as finished:
                    return finished.value
                try:
                    if waiting is None:
                        await asyncio.sleep(0)
                        value = None
                        continue
                    if not asyncio.isfuture(waiting) or not waiting._asyncio_future_blocking:
                        raise RuntimeError("Native awaitable yielded an invalid wait.")
                    if waiting is asyncio.current_task() or waiting.get_loop() is not asyncio.get_running_loop():
                        raise RuntimeError("Native awaitable yielded an incompatible wait.")
                    # This is the Future await protocol consumed by Task too.
                    # Reset its handoff flag, but never yield it to our Task.
                    waiting._asyncio_future_blocking = False
                    ready = asyncio.get_running_loop().create_future()
                    def completed(_done, signal=ready):
                        if not signal.done():
                            signal.set_result(None)
                    waiting.add_done_callback(completed)
                    try:
                        try:
                            await asyncio.shield(ready)
                        except asyncio.CancelledError:
                            if not waiting.cancel():
                                if not waiting.cancelled():
                                    waiting.exception()
                                raise
                            while not ready.done():
                                try:
                                    await asyncio.shield(ready)
                                except asyncio.CancelledError:
                                    pass
                        waiting.result()
                        value = None
                    finally:
                        waiting.remove_done_callback(completed)
                except BaseException as error:
                    # Deliver the native type/cause to the existing transport
                    # handler without publishing it on a Task waiter. The
                    # protected producer sanitizes before completing outwardly.
                    failure = error
        finally:
            iterator.close()

    def _safe_pong(self, native: asyncio.Future) -> asyncio.Future:
        # ping() returns a Future as a value. Copy its outcome before any Task
        # can retain/await it, rather than returning the raw Future in a Task.
        safe = _future()
        def completed(done):
            if safe.done():
                if not done.cancelled():
                    done.exception()
            elif done.cancelled():
                safe.cancel()
            else:
                try:
                    value = done.result()
                except BaseException as error:
                    safe.set_exception(self._safe_failure(error))
                else:
                    safe.set_result(value)
        native.add_done_callback(completed)
        safe.add_done_callback(lambda done: native.cancel() if done.cancelled() else None)
        return safe

    @protected_async
    async def _safe_task(self, operation: Callable, *args: Any) -> Any:
        # A retained Task must never expose a native waiter's exception in repr
        # before its coroutine resumes. Only this neutral bridge is awaited by
        # the retained owner; the producer and its outcome stay in local state.
        complete = asyncio.get_running_loop().create_future()
        result = failure = None
        owner = _current_owner()

        async def produce() -> None:
            nonlocal result, failure
            token = _task_owner.set((asyncio.current_task(), owner))
            try:
                try:
                    # Lazy construction also handles cancellation before startup.
                    result = await self._await_native(operation(*args))
                except BaseException as error:
                    failure = self._safe_failure(error)
            finally:
                _task_owner.reset(token)

        producer = asyncio.create_task(produce())
        producer.add_done_callback(lambda _done: complete.set_result(None))
        try:
            await asyncio.shield(complete)
        except asyncio.CancelledError:
            if not producer.cancel():
                # Completion may precede delivery through the bridge. A new
                # caller cancellation still wins if no producer can receive it.
                if not producer.cancelled():
                    producer.exception()
                raise
            # Keep a strong reference and join owned cleanup without ever
            # awaiting the producer Task itself (its repr can nest raw waits).
            while not complete.done():
                try:
                    await asyncio.shield(complete)
                except asyncio.CancelledError:
                    pass
        if producer.cancelled():
            raise asyncio.CancelledError
        # A failure of the sanitizer itself must also be retrieved.
        producer.result()
        if failure is not None:
            # Outside the except suite: preserve the sanitized native cause
            # chain needed by transport classification.
            raise failure
        return result

    @protected_async
    async def _write_native(self, socket: ClientConnection, size: int, write: Callable,
                            before: Optional[Callable[[], None]] = None) -> Any:
        while True:
            # Serialize controls independently so a blocked application write
            # cannot stop native liveness. Context entry may itself wait.
            async with self._native_write_lock:
                self._check_open()
                if socket is not self._socket:
                    raise APIConnectionError("Stream disconnected before control send.", request_sent=False)
                async with (socket.send_context() if hasattr(socket, "send_context") else nullcontext()):
                    self._check_open()
                    if socket is not self._socket:
                        raise APIConnectionError("Stream disconnected before control send.", request_sent=False)
                    now, delay = self._monotonic(), 0.0
                    for bucket, amount in zip(self._native_buckets, (1, size + 6)):
                        rate, burst, tokens, at = bucket
                        if amount > burst:
                            raise StreamProtocolError("Native control exceeds the advertised limit.")
                        bucket[2] = min(burst, tokens + max(0, now - at) * rate)
                        bucket[3] = max(at, now)
                        delay = max(delay, (amount - bucket[2]) / rate)
                    if delay <= 0:
                        for bucket, amount in zip(self._native_buckets, (1, size + 6)):
                            bucket[2] -= amount
                        if before is not None:
                            before()
                        return await write()
            await self._sleep(min(_TIMER_MAX_DELAY_SECONDS, max(0.001, delay)))

    def _install_native_pacing(self, socket: ClientConnection) -> None:
        # The protocol generates automatic pongs synchronously. Queue them before
        # they enter its writer so probes and replies share both native budgets.
        protocol = getattr(socket, "protocol", None)
        if protocol is None:
            return
        send_frame = protocol.send_frame
        async def pong(frame) -> None:
            async def write() -> None:
                send_frame(frame)
            await self._write_native(socket, len(frame.data), write)
        def completed(task: asyncio.Task) -> None:
            self._native_tasks.discard(task)
            if not task.cancelled():
                error = task.exception()
                if error is not None and self._transport_failure is not None and not self._transport_failure.done():
                    self._transport_failure.set_exception(self._safe_failure(error))
        def paced_frame(frame) -> None:
            if frame.opcode != OP_PONG:
                send_frame(frame)
                return
            if len(self._native_tasks) >= self._native_queue_limit:
                if self._transport_failure is not None and not self._transport_failure.done():
                    self._transport_failure.set_exception(StreamProtocolError("Native control queue exceeds the advertised limit."))
                return
            task = asyncio.create_task(self._safe_task(pong, frame))
            self._native_tasks.add(task)
            task.add_done_callback(completed)
        protocol.send_frame = paced_frame
        self._native_restore = lambda: setattr(protocol, "send_frame", send_frame)

    def _message_delay(self, event: bool) -> float:
        now = self._monotonic()
        self._event_tokens = min(self._event_burst, self._event_tokens + max(0, now - self._event_at) * self._event_rate)
        self._event_at = max(now, self._event_at)
        self._message_tokens = min(self._message_burst, self._message_tokens + max(0, now - self._message_at) * self._message_rate)
        self._message_at = max(now, self._message_at)
        delay = max(0, (1 - self._event_tokens) / self._event_rate if event else 0,
                    (1 - self._message_tokens) / self._message_rate)
        if delay == 0:
            if event:
                self._event_tokens -= 1
            self._message_tokens -= 1
        return delay

    async def _admitted_write(self, socket: ClientConnection, raw: str, *, event: bool = False,
                              before: Optional[Callable[[], None]] = None) -> bool:
        wake = self._pacing_wake
        while True:
            async with self._write_lock:
                self._check_open()
                if socket is not self._socket or wake.is_set() or getattr(socket, "state", State.OPEN) != State.OPEN:
                    return False
                delay = self._message_delay(event)
                if delay == 0:
                    if before is not None:
                        before()
                    await self._safe_task(self._send_raw, socket, raw)
                    return True
            # Release the writer during token waits so receipts and heartbeats progress.
            sleep = asyncio.ensure_future(self._sleep(min(_TIMER_MAX_DELAY_SECONDS, max(0.001, delay))))
            disconnected = asyncio.create_task(wake.wait())
            try:
                await asyncio.wait({sleep, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                sleep.cancel()
                disconnected.cancel()
                await asyncio.gather(sleep, disconnected, return_exceptions=True)

    async def _send_mutation(self, frame: dict[str, Any], mutation: _Mutation) -> None:
        raw = self._serialize({**frame, "idempotency_key": mutation.idempotency_key})
        socket = self._socket
        if socket is None:
            return
        def before() -> None:
            stream_id = cast(str, self._stream_id)
            timing = _Timing(self._monotonic(), _future())
            self._timings[(stream_id, mutation.event_id)] = timing
            self._event_versions[mutation.event_id] = mutation.version
            if self._effective_mode == "on_change" or self._explicit_intent:
                self._outstanding.add(mutation.version)
            self._arm_receipt(mutation.future)
        await self._admitted_write(socket, raw, event=True, before=before)

    def _serialize(self, frame: Mapping[str, Any]) -> str:
        raw = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > self._message_bytes:
            raise ValueError("Stream message exceeds the server-advertised byte limit.")
        return raw

    async def _send(self, frame: dict[str, Any], *, before: Optional[Callable[[], None]] = None) -> None:
        raw = self._serialize(frame)
        socket = self._socket
        if socket is None or not await self._admitted_write(socket, raw, before=before):
            raise APIConnectionError("Stream disconnected before send.", request_sent=False)

    def _transport_error(self, error: BaseException, message: str = "Stream connection failed.", *,
                         context_boundary: Optional[BaseException] = None) -> APIConnectionError:
        return connection_error(error, message, request_sent=self._request_sent,
                                websocket_opening=self._socket is None, context_boundary=context_boundary)

    @protected_async
    async def _send_raw(self, socket: ClientConnection, raw: str) -> None:
        context_boundary = sys.exc_info()[1]
        self._request_sent = True
        try:
            await socket.send(raw)
        except Exception as cause:
            if isinstance(cause, SystemZeroError):
                error = (self._transport_error(cause, context_boundary=context_boundary)
                         if isinstance(cause, APITimeoutError) else cause)
                if isinstance(error, StreamProtocolError):
                    self._terminal_error = self._terminal_error or error
                else:
                    self._connection_error = self._connection_error or error
            elif is_transport_error(cause, context_boundary=context_boundary):
                error = self._transport_error(cause, context_boundary=context_boundary)
                # Keep an earlier fatal API frame as the authoritative cause.
                self._connection_error = self._connection_error or error
            elif isinstance(cause, ConnectionClosed):
                error = cause
            else:
                raise
            # A failed write need not wake recv. Give the connection owner the
            # same failure immediately instead of waiting for a receipt timeout.
            if self._transport_failure is not None and not self._transport_failure.done():
                self._transport_failure.set_exception(self._safe_failure(error))
            raise error from None

    async def _wait_until(self, deadline: float) -> None:
        # Keep the real server deadline while limiting each timer to the shared SDK bound.
        while True:
            remaining = max(0.0, max(deadline, self._retry_not_before) - self._monotonic())
            await self._sleep(min(_TIMER_MAX_DELAY_SECONDS, remaining))
            if max(deadline, self._retry_not_before) <= self._monotonic():
                return

    async def decide(self) -> StreamScheduled:
        if self._queued_mutations:
            await asyncio.shield(self._queued_mutations[-1].future)
        # Cancellation before admission never creates a command. Once admitted,
        # an internal owner keeps both send flow control and receipt serialization.
        await self._operation_lock.acquire()
        try:
            await self._available()
            self._require_started()
            wait = max(0.0, self._retry_not_before - self._monotonic())
            if wait:
                await self._wait_until(self._retry_not_before)
            await self._available()
            command = asyncio.create_task(self._decide_owned())
            self._decision_task = command
            command.add_done_callback(self._decide_finished)
        except BaseException:
            self._operation_lock.release()
            raise
        return cast(StreamScheduled, await asyncio.shield(command))

    def _decide_finished(self, command: asyncio.Task) -> None:
        # A task cancelled before its first step never reaches a coroutine finally.
        self._operation_lock.release()
        if self._decision_task is command:
            self._decision_task = None
        # A cancelled caller no longer observes the owned command's outcome.
        if not command.cancelled():
            command.exception()

    async def _decide_owned(self) -> StreamScheduled:
        # close() can run after admission but before this task's first step.
        self._check_open()
        scheduled = _future()
        def before() -> None:
            self._pending_schedule = scheduled
            self._pending_schedule_version = self._version
            self._pending_schedule_prior_work = self._version in self._outstanding
            self._explicit_intent = True
            self._outstanding.add(self._version)
            self._arm_receipt(scheduled)
        try:
            await self._send({"type": "decide", "state_version": self._version}, before=before)
        except (ConnectionClosed, OSError, APIConnectionError) as error:
            if self._pending_schedule is not scheduled:
                raise  # No write was admitted, so no ambiguous receipt exists.
            if isinstance(error, ConnectionClosed) and error.rcvd is not None and error.rcvd.code == 1000:
                self._reconnect_requested.set()
            # Explicit commands are never resent; the outcome is ambiguous.
            pass
        return cast(StreamScheduled, await asyncio.shield(scheduled))

    async def wait_for_decision(self, ack: StreamAck) -> EventLatency:
        """First successful decision covering this ack, including coalesced events."""
        timing = ack.timing if isinstance(ack, _Ack) and ack.owner is self._ack_owner else None
        if timing is None or timing.version != ack["state_version"]:
            raise ValueError("ack does not identify an accepted event from this client.")
        return cast(EventLatency, await asyncio.shield(timing.future))

    async def _replay(self) -> None:
        request = {**cast(dict, self._request), "state": deepcopy(self._desired_state)}
        if self._desired_attachments is not None:
            request["attachments"] = deepcopy(self._desired_attachments)
        else:
            request.pop("attachments", None)
        self._replay_included = list(self._queued_mutations)
        self._replay_state = deepcopy(self._desired_state)
        self._replay_attachments = deepcopy(self._desired_attachments)
        key = (self._replay_included[-1].idempotency_key if self._replay_included else
               self._accepted_idempotency_key)
        if not isinstance(key, str) or not key.strip():
            raise StreamProtocolError("Cannot replay stream work without its original idempotency key.")
        self._replay_on_request = (key == self._refused_idempotency_key and self._decision_mode == "on_change"
                                   and self.reconnect_policy == "latest")
        self._effective_mode = (self._decision_mode if self.reconnect_policy == "latest" and not self._replay_on_request
                                else "on_request")
        mutation = _Mutation(_future(), "evt_1", 1, "replay", {}, key)
        self._active_mutation = mutation
        await self._send_mutation({"type": "start", "event_id": "evt_1", "request": request,
                                   "decision_mode": self._effective_mode,
                                   "min_interval_ms": self._min_interval_ms}, mutation)
        await asyncio.shield(mutation.future)

    def _opening_remaining(self) -> float:
        if self._opening_started is None:
            self._opening_started = self._monotonic()
        return self.reconnect_timeout - (self._monotonic() - self._opening_started)

    @protected_async
    async def _run(self) -> None:
        failure: Optional[BaseException] = None
        try:
            while not self._closed:
                remaining = self._opening_remaining()
                if self._opening_attempts >= self.max_reconnect_attempts or remaining <= 0:
                    failure = self._opening_failure or self._transport_error(TimeoutError(), "Stream reconnect budget exhausted.")
                    break
                self._opening_attempts += 1
                self._idle = False
                self._reconnect_requested.clear()
                self._stream_id = None
                self._connection_error = None
                self._transport_failure = _future()
                self._request_sent = None
                self._seq = 0
                self._version = self._last_decision_version = 0
                self._outstanding.clear()
                self._event_versions.clear()
                self._explicit_intent = False
                failure = None
                terminal = False
                try:
                    try:
                        stream_url = _checked_stream_url(self.stream_url)
                        key = _checked_stream_key(reveal(self._api_key))
                    except ValueError as error:
                        raise StreamProtocolError(str(error)) from None
                    # Native probes are enabled only by hello; JSON heartbeat remains compatible with older servers.
                    headers = {"Authorization": "Bearer " + key}
                    self._socket = await asyncio.wait_for(self._await_native(connect(stream_url,
                                                 subprotocols=[STREAM_PROTOCOL],
                                                 additional_headers=headers, logger=DiagnosticLogger(self._api_key),
                                                 compression=None, ping_interval=None, proxy=None,
                                                 open_timeout=min(10, remaining), close_timeout=1, max_size=4194304,
                                                 max_queue=64)), min(10, remaining))
                    # connect returns only after a valid HTTP Upgrade response.
                    self._request_sent = True
                    if self._socket.subprotocol != STREAM_PROTOCOL:
                        raise StreamProtocolError("Server did not select system-zero.stream.v1.")
                    remaining = self._opening_remaining()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    hello = self._decode(await asyncio.wait_for(self._await_native(self._socket.recv()), min(10, remaining)), first=True)
                    # An accepted hello ends only the failed-opening episode, before replay can wait for an ack.
                    self._opening_attempts = self._attempt = 0
                    self._opening_started = self._opening_failure = None
                    self._ever_opened = True
                    self._configure_limits(hello["limits"])
                    self._install_native_pacing(self._socket)
                    self._hello = cast(StreamHello, hello)
                    self._heartbeat_interval = float(hello["limits"]["heartbeat_interval_ms"]) / 1000
                    native_interval = hello["limits"].get("native_heartbeat_interval_ms")
                    self._native_heartbeat_interval = None if native_interval is None else float(native_interval) / 1000
                    self._last_ping = self._monotonic()
                    self._reader = asyncio.create_task(self._receive())
                    if self._request is not None:
                        replay = asyncio.create_task(self._replay())
                        self._replayer = replay
                        done, _ = await asyncio.wait({self._reader, replay}, return_when=asyncio.FIRST_COMPLETED)
                        if self._reader in done:
                            replay.cancel()
                            await asyncio.gather(replay, return_exceptions=True)
                            await self._reader
                        await replay
                        self._replayer = None
                    if not cast(asyncio.Future, self._ready).done():
                        cast(asyncio.Future, self._ready).set_result(hello)
                    await self._reader
                    failure = (APIConnectionError("Stream connection failed.", type="connection_error",
                                                   request_sent=self._request_sent)
                               if self._socket.close_code == 1006 else
                               StreamDisconnectedError(close_code=self._socket.close_code))
                    self._idle = self._socket.close_code == 1000
                    terminal = not self._idle and self._socket.close_code not in _RETRYABLE_CLOSE_CODES
                except asyncio.CancelledError:
                    raise
                except StreamProtocolError as error:
                    failure, terminal = error, True
                    # Reserve validation ownership before disconnect can publish callbacks.
                    self._terminal_error = self._terminal_error or error
                except StreamDisconnectedError as error:
                    failure = error
                    self._idle = error.close_code == 1000
                    terminal = error.close_code is not None and not self._idle and error.close_code not in _RETRYABLE_CLOSE_CODES
                except ConnectionClosed as error:
                    code = error.rcvd.code if error.rcvd is not None else 1006
                    failure = (self._transport_error(error) if code == 1006 else
                               StreamDisconnectedError(close_code=code))
                    self._idle = code == 1000
                    terminal = not self._idle and code not in _RETRYABLE_CLOSE_CODES
                except InvalidStatus as error:
                    delay = retry_after_delay(dict(error.response.headers), self._now())
                    self._retry_not_before = max(self._retry_not_before, self._monotonic() + delay)
                    failure, should_retry = _upgrade_error(error, self._now(), self._ever_opened)
                    self._opening_failure = failure
                    terminal = not should_retry
                except InvalidHandshake as error:
                    if is_transport_error(error):
                        failure = self._transport_error(error)
                    else:
                        failure, terminal = StreamProtocolError("Invalid WebSocket handshake."), True
                except APIConnectionError as error:
                    failure = self._transport_error(error)
                except SystemZeroError as error:
                    failure, terminal = error, True
                except Exception as error:
                    if is_transport_error(error):
                        failure = self._transport_error(error)
                    else:
                        failure, terminal = StreamProtocolError("Invalid stream server data."), True
                finally:
                    self._cleaning_connection = True
                    ready = cast(asyncio.Future, self._ready)
                    if self._idle and not ready.done():
                        ready.set_exception(failure or StreamDisconnectedError(close_code=1000))
                    if ready.done():
                        self._ready = _future()
                    if self._idle:
                        # Reject old commands before cleanup yields to the next caller.
                        if self._stream_id is not None:
                            self._disconnected(self._terminal_error or failure or StreamDisconnectedError(close_code=1000))
                        if self._active_mutation is not None and self._active_mutation.kind == "start":
                            self._request = None
                        self._reject_pending(failure or StreamDisconnectedError(close_code=1000))
                        self._active_mutation = None
                        self._replay_included = []
                        self._rebuild_desired()
                    if self._replayer is not None:
                        self._replayer.cancel()
                        await asyncio.gather(self._replayer, return_exceptions=True)
                        self._replayer = None
                    if self._reader is not None:
                        self._reader.cancel()
                        await asyncio.gather(self._reader, return_exceptions=True)
                        self._reader = None
                    if self._stream_id is not None and not self._idle:
                        self._disconnected(self._terminal_error or failure or StreamDisconnectedError())
                    for task in self._native_tasks:
                        task.cancel()
                    await asyncio.gather(*self._native_tasks, return_exceptions=True)
                    self._native_tasks.clear()
                    if self._native_restore is not None:
                        self._native_restore()
                        self._native_restore = None
                    if self._socket is not None:
                        code = (1008 if self._overflow_error is not None else
                                failure.close_code if isinstance(failure, StreamProtocolError) else 1000)
                        try:
                            await self._socket.close(code=code)
                        except Exception as error:
                            if not is_transport_error(error):
                                raise
                            failure = failure or self._transport_error(error)
                        finally:
                            self._socket = None
                    if self._transport_failure is not None:
                        self._transport_failure.cancel()
                        self._transport_failure = None
                    self._cleaning_connection = False
                if terminal or self._closed:
                    break
                if self._idle:
                    # Code 1000 remains dormant until a caller needs another send.
                    await self._reconnect_requested.wait()
                immediate = isinstance(failure, StreamDisconnectedError) and failure.close_code == 1001
                delay = max(0.0 if immediate or self._idle else reconnect_delay(self._attempt, self._random()),
                            self._retry_not_before - self._monotonic(), 0.0)
                self._attempt += 1
                if self._opening_started is None:
                    # A healthy stream's resume hint precedes the next failed-opening episode.
                    await self._wait_until(self._monotonic() + delay)
                    continue
                remaining = self._opening_remaining()
                self._opening_failure = self._opening_failure or failure
                if self._opening_attempts >= self.max_reconnect_attempts or remaining <= 0 or delay >= remaining:
                    failure = self._opening_failure
                    break
                try:
                    await asyncio.wait_for(self._wait_until(self._monotonic() + delay), remaining)
                except asyncio.TimeoutError:
                    failure = self._opening_failure
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._terminal_error = self._terminal_error or failure or StreamDisconnectedError("Stream client is closed.")
            self._reject_pending(self._terminal_error)
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(self._terminal_error)
            self._end_events(discard=self._closed)

    def _decode(self, raw: Any, *, first: bool = False) -> dict[str, Any]:
        if not isinstance(raw, str):
            raise StreamProtocolError("Binary stream application data is unsupported.", close_code=1003)
        try:
            def reject_constant(value):
                raise ValueError("Non-JSON number")
            frame = json.loads(raw, parse_constant=reject_constant)
        except (ValueError, TypeError):
            raise StreamProtocolError("Malformed JSON stream message.", close_code=1007) from None
        if not isinstance(frame, dict) or not _integer(frame.get("seq"), 1) or not isinstance(frame.get("stream_id"), str) or not _STREAM_ID.fullmatch(frame["stream_id"]):
            raise StreamProtocolError("Invalid stream envelope.")
        if first:
            interval = _nonnegative(frame.get("limits", {}).get("heartbeat_interval_ms")) if isinstance(frame.get("limits"), dict) else None
            if frame.get("type") != "hello" or frame["seq"] != 1 or frame.get("protocol") != STREAM_PROTOCOL or interval is None or interval <= 0:
                raise StreamProtocolError("Invalid stream hello.")
            native = frame["limits"].get("native_heartbeat_interval_ms")
            if native is not None and (_nonnegative(native) is None or float(native) <= 0):
                raise StreamProtocolError("Invalid native heartbeat interval.")
            self._stream_id = frame["stream_id"]
        elif frame["stream_id"] != self._stream_id or frame["seq"] != self._seq + 1 or frame.get("type") not in {"ack", "scheduled", "decision", "error", "ping", "goodbye"}:
            raise StreamProtocolError("Invalid stream message ordering or type.")
        self._seq = frame["seq"]
        return redact(frame) if frame.get("type") == "error" else frame

    @protected_async
    async def _native_heartbeat(self, socket: ClientConnection, interval: float) -> None:
        while True:
            await self._sleep(interval)

            # The transport's default probe payload is four bytes. Admission is
            # outside its pong deadline and shares tokens with automatic replies.
            admitted = _future()
            async def send_probe():
                try:
                    pong = await self._write_native(socket, 4, socket.ping,
                        before=lambda: None if admitted.done() else admitted.set_result(None))
                    return self._safe_pong(pong)
                except BaseException as error:
                    if not admitted.done():
                        admitted.set_exception(self._safe_failure(error))
                    raise
            sending = asyncio.create_task(self._safe_task(send_probe))
            async def probe() -> None:
                pong = await sending
                await pong
            try:
                # Token/context waits are outside the deadline, but once admitted
                # both a blocked native write and its pong wait remain bounded.
                await admitted
                await asyncio.wait_for(probe(), _NATIVE_PONG_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as error:
                raise self._transport_error(error, "Server native heartbeat stopped.") from None
            finally:
                sending.cancel()
                await asyncio.gather(sending, return_exceptions=True)
                if not sending.cancelled() and sending.exception() is None:
                    pong = sending.result()
                    if not pong.done():
                        pong.cancel()
            self._liveness()

    def _liveness(self) -> None:
        self._last_ping = self._monotonic()
        if self._receipt_future is not None:
            self._receipt_inactivity = self._last_ping + self.receipt_timeout
            self._receipt_changed.set()

    def _arm_receipt(self, future: asyncio.Future) -> None:
        now = self._monotonic()
        self._receipt_future = future
        self._receipt_deadline, self._receipt_inactivity = now + _RECEIPT_TIMEOUT_SECONDS, now + self.receipt_timeout
        self._receipt_changed.set()

    def _clear_receipt(self, future: Optional[asyncio.Future] = None) -> None:
        if future is None or self._receipt_future is future:
            self._receipt_future = None
            self._receipt_changed.set()

    async def _watch_receipt(self) -> None:
        while True:
            self._receipt_changed.clear()
            if self._receipt_future is None:
                await self._receipt_changed.wait()
                continue
            remaining = min(self._receipt_deadline, self._receipt_inactivity) - self._monotonic()
            if remaining <= 0:
                raise self._transport_error(TimeoutError(),
                    "Stream command receipt timed out; outstanding work may have been decided.")
            # Isolate wait_for's Python 3.10/3.11 cancellation/result race:
            # the owner waits on a shield, so a completed receipt cannot consume
            # its cancellation. The inner wait only handles a neutral SDK event.
            waiting = asyncio.create_task(asyncio.wait_for(self._receipt_changed.wait(), remaining))
            try:
                await asyncio.shield(waiting)
            except asyncio.TimeoutError:
                # Recheck after a same-deadline pong or command receipt updates ownership.
                continue
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    @protected_async
    async def _receive(self) -> None:
        socket = cast(ClientConnection, self._socket)
        reading = asyncio.create_task(self._receive_frames(socket))
        native = (asyncio.create_task(self._native_heartbeat(socket, self._native_heartbeat_interval))
                  if self._native_heartbeat_interval is not None else None)
        receipt = asyncio.create_task(self._watch_receipt())
        tasks = {reading, receipt} if native is None else {reading, receipt, native}
        transport_failure = self._transport_failure
        try:
            done, _ = await asyncio.wait(tasks | {cast(asyncio.Future, transport_failure)},
                                         return_when=asyncio.FIRST_COMPLETED)
            # Either watchdog also interrupts a JSON-pong send blocked in the reader.
            await (reading if reading in done else cast(asyncio.Future, transport_failure)
                   if transport_failure in done else receipt if receipt in done else cast(asyncio.Task, native))
        finally:
            self._clear_receipt()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @protected_async
    async def _receive_frames(self, socket: ClientConnection) -> None:
        received = asyncio.create_task(self._safe_task(socket.recv))
        pongs: set[asyncio.Task] = set()
        def pong_done(task: asyncio.Task) -> None:
            pongs.discard(task)
            if not task.cancelled():
                error = task.exception()
                if error is not None and self._transport_failure is not None and not self._transport_failure.done():
                    self._transport_failure.set_exception(self._safe_failure(error))
        try:
            while True:
                remaining = self._last_ping + 2 * self._heartbeat_interval - self._monotonic()
                if remaining <= 0:
                    raise self._transport_error(TimeoutError(), "Server stream heartbeat stopped.")
                try:
                    raw = await asyncio.wait_for(asyncio.shield(received), remaining)
                except asyncio.TimeoutError:
                    if received.done():
                        # A native recv timeout is not the heartbeat polling timer.
                        await received
                    # A native pong can refresh liveness while recv assembles one fragmented JSON message.
                    # Cancel only the wait; the same recv keeps its partially assembled output.
                    continue
                frame = self._decode(raw)
                received = asyncio.create_task(self._safe_task(socket.recv))
                kind = frame["type"]
                if kind == "ping":
                    if not _integer(frame.get("nonce"), 1):
                        raise StreamProtocolError("Invalid stream ping.")
                    self._liveness()
                    # A token wait must not hide an ack already queued behind
                    # this ping. The connection owner still joins every write.
                    if len(pongs) >= 2:
                        raise StreamProtocolError("Too many pending stream heartbeat replies.")
                    pong = asyncio.create_task(self._safe_task(self._send, {"type": "pong", "nonce": frame["nonce"]}))
                    pongs.add(pong)
                    pong.add_done_callback(pong_done)
                    continue
                if kind == "ack":
                    frame = self._ack(frame)
                elif kind == "scheduled":
                    self._message_at = self._monotonic()
                    if not _integer(frame.get("state_version"), 1, self._version) or type(frame.get("in_flight")) is not bool or self._pending_schedule is None:
                        raise StreamProtocolError("Unexpected scheduled frame.")
                    self._clear_receipt(self._pending_schedule)
                    self._pending_schedule.set_result(frame)
                    self._pending_schedule = None
                    self._pending_schedule_prior_work = False
                    self._outstanding.add(frame["state_version"])
                elif kind == "decision":
                    self._decision(frame)
                elif kind == "error":
                    self._error(frame)
                elif kind == "goodbye":
                    resume = frame.get("resume")
                    if not isinstance(resume, dict) or not _integer(resume.get("retry_after_ms")):
                        raise StreamProtocolError("Invalid goodbye retry hint.")
                    self._retry_not_before = max(self._retry_not_before,
                                                 self._monotonic() + resume["retry_after_ms"] / 1000)
                fatal_error = None
                if kind == "error":
                    body = frame["envelope"]["error"]
                    permanent = (frame["http_status"] in {401, 402, 403} or
                                 body.get("type") in {"authentication_error", "permission_error"} or
                                 any(token in str(body.get("code", "")) for token in ("spend", "budget", "monthly_cap", "payment", "credit", "auth", "api_key")))
                    retry_headers = {"x-should-retry": str(frame["retry"].get("should_retry")).lower()}
                    if frame["retry"].get("retry_after_ms") is not None:
                        retry_headers["retry-after-ms"] = str(frame["retry"]["retry_after_ms"])
                    if frame["retry"].get("retry_after") is not None:
                        retry_headers["retry-after"] = str(frame["retry"]["retry_after"])
                    # Fatal ends this connection, but its close can still permit reconnect.
                    if frame.get("fatal"):
                        self._connection_error = _remote_error(frame)
                    if permanent or frame.get("fatal") and not _stream_retryable(frame["http_status"], body, retry_headers, self._ever_opened):
                        fatal_error = self._connection_error or _remote_error(frame)
                if fatal_error is not None:
                    # Reserve fatal ownership before waking application code.
                    # _emit may still replace it with the priority overflow error.
                    self._terminal_error = fatal_error
                self._emit(frame)
                if fatal_error is not None:
                    raise fatal_error
        finally:
            received.cancel()
            for pong in pongs:
                pong.cancel()
            await asyncio.gather(received, *pongs, return_exceptions=True)

    def _ack(self, frame: dict[str, Any]) -> _Ack:
        mutation = self._active_mutation
        if mutation is None or frame.get("event_id") != mutation.event_id or frame.get("state_version") != mutation.version or not _integer(frame.get("state_version"), 1, 100000) or type(frame.get("scheduled")) is not bool or not _integer(frame.get("state_bytes")) or not _integer(frame.get("synthesized_body_bytes"), 0, 3665920):
            raise StreamProtocolError("Ack does not match the pending mutation.")
        # Receipt time is a conservative refill origin across transport latency.
        self._event_at = self._monotonic()
        self._message_at = self._event_at
        self._version = frame["state_version"]
        timing = self._timings[(cast(str, self._stream_id), mutation.event_id)]
        timing.version = self._version
        timing.scheduled = frame["scheduled"]
        ack = _Ack(frame, timing, self._ack_owner)
        if frame["scheduled"]:
            self._outstanding.add(self._version)
        elif self._effective_mode == "on_change" or not self._explicit_intent:
            self._outstanding.discard(self._version)
        self._accepted_idempotency_key = mutation.idempotency_key
        self._clear_receipt(mutation.future)
        self._active_mutation = None
        if mutation.kind == "replay":
            self._accepted_state = deepcopy(self._replay_state)
            self._accepted_attachments = deepcopy(self._replay_attachments)
            for included in self._replay_included:
                if included in self._queued_mutations:
                    self._queued_mutations.remove(included)
                if not included.future.done():
                    included.future.set_result(ack)
            self._replay_included = []
        else:
            self._accepted_state = self._apply(self._accepted_state, mutation)
            self._accepted_attachments = self._apply_attachments(self._accepted_attachments, mutation)
            if mutation in self._queued_mutations:
                self._queued_mutations.remove(mutation)
        self._rebuild_desired()
        mutation.future.set_result(ack)
        return ack

    def _decision(self, frame: dict[str, Any]) -> None:
        version = frame.get("state_version")
        if not _integer(version, max(1, self._last_decision_version), self._version) or not isinstance(frame.get("reply"), dict) or frame.get("http_status") != 200:
            raise StreamProtocolError("Invalid decision coverage.")
        self._last_decision_version = version
        self._last_decision = cast(StreamDecision, frame)
        self._attempt = 0
        if self._pending_schedule is not None and version >= self._pending_schedule_version:
            # This settles earlier work, while the separate command still needs its receipt.
            self._pending_schedule_prior_work = False
        self._outstanding.difference_update(value for value in list(self._outstanding)
                                            if value <= version and not (self._pending_schedule is not None
                                                                        and value == self._pending_schedule_version))
        if self._pending_schedule is None:
            self._explicit_intent = bool(self._outstanding)
        received = self._monotonic()
        for (stream_id, event_id), timing in list(self._timings.items()):
            if stream_id == self._stream_id and timing.version is not None and timing.version <= version and not timing.future.done():
                latency = EventLatency(event_id, timing.version, max(0.0, received - timing.sent) * 1000,
                                       cast(StreamDecision, frame))
                self._latencies.append(latency)
                timing.future.set_result(latency)
                self._timings.pop((stream_id, event_id), None)

    def _error(self, frame: dict[str, Any]) -> None:
        if frame.get("phase") not in {"protocol", "decide", "lifecycle"} or not isinstance(frame.get("envelope"), dict) or not isinstance(frame["envelope"].get("error"), dict) or not isinstance(frame.get("retry"), dict) or not _integer(frame.get("http_status"), 400, 599):
            raise StreamProtocolError("Invalid error frame.")
        retry = frame["retry"]
        headers = {}
        if retry.get("retry_after_ms") is not None:
            headers["retry-after-ms"] = str(retry["retry_after_ms"])
        if retry.get("retry_after") is not None:
            headers["retry-after"] = str(retry["retry_after"])
        delay = retry_after_delay(headers, self._now())
        self._retry_not_before = max(self._retry_not_before, self._monotonic() + delay)
        headers["x-should-retry"] = str(retry.get("should_retry")).lower()
        if (frame["phase"] == "decide" and frame.get("state_version") == self._version and
                not _stream_retryable(frame["http_status"], frame["envelope"]["error"], headers, self._ever_opened)):
            self._refused_idempotency_key = self._accepted_idempotency_key
        error = _remote_error(frame)
        mutation = self._active_mutation
        if frame["phase"] == "protocol" and mutation is not None and frame.get("event_id") == mutation.event_id:
            self._reject_mutation(mutation, error)
        if frame["phase"] == "protocol" and self._pending_schedule is not None and frame.get("event_id") is None:
            self._clear_receipt(self._pending_schedule)
            self._pending_schedule.set_exception(error)
            self._pending_schedule = None
            # Refusing a new command cannot erase a previous decide for the same version.
            if not self._pending_schedule_prior_work:
                self._outstanding.discard(self._pending_schedule_version)
            self._pending_schedule_prior_work = False
            self._explicit_intent = bool(self._outstanding)
        if frame["phase"] == "decide" and _integer(frame.get("state_version"), 1):
            version = frame["state_version"]
            if self._effective_mode == "on_change" and retry.get("should_retry") is True and not frame.get("fatal"):
                # The server may re-dispatch this version after emitting the error.
                self._outstanding.add(version)
            else:
                if self._pending_schedule is not None and version >= self._pending_schedule_version:
                    self._pending_schedule_prior_work = False
                self._outstanding.difference_update(
                    value for value in list(self._outstanding)
                    if value <= version and not (self._pending_schedule is not None
                                                 and value == self._pending_schedule_version))
            self._explicit_intent = bool(self._outstanding)

    def _disconnected(self, error: BaseException) -> None:
        protect_exception(error)
        self._pacing_wake.set()
        if self._disconnecting:
            return
        self._disconnecting = True
        error = self._terminal_error or self._connection_error or error
        outstanding = self._outstanding.copy()
        self._outstanding.clear()
        try:
            if outstanding:
                self._emit({"type": "possibly_decided", "stream_id": self._stream_id,
                            "state_versions": sorted(outstanding),
                            "event_ids": [key for key, version in self._event_versions.items()
                                          if version in outstanding]}, closing=True)
            for (stream_id, event_id), timing in list(self._timings.items()):
                if stream_id == self._stream_id and not timing.future.done():
                    timing.future.set_exception(StreamDisconnectedError("State was not scheduled on this connection.")
                        if isinstance(error, StreamDisconnectedError) and timing.scheduled is False
                        and timing.version not in outstanding else error)
                    self._timings.pop((stream_id, event_id), None)
            if self._pending_schedule is not None and not self._pending_schedule.done():
                self._pending_schedule.set_exception(error)
            self._pending_schedule = None
            self._pending_schedule_prior_work = False
        finally:
            self._connection_error = None
            self._disconnecting = False

    def _reject_pending(self, error: BaseException) -> None:
        for mutation in self._queued_mutations:
            if not mutation.future.done():
                mutation.future.set_exception(error)
        self._queued_mutations.clear()
        if self._active_mutation is not None and not self._active_mutation.future.done():
            self._active_mutation.future.set_exception(error)
        if self._pending_schedule is not None and not self._pending_schedule.done():
            self._pending_schedule.set_exception(error)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._terminal_error = self._terminal_error or self._connection_error or StreamDisconnectedError("Stream client is closed.")
            self._end_events(discard=True)
            self._clear_receipt()
            self._reject_pending(self._terminal_error)
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(self._terminal_error)
            # Do not cancel an already-running close handshake or sibling task
            # cleanup. Repeated callers join it instead of interrupting it.
            if self._runner is not None and not self._cleaning_connection:
                self._runner.cancel()
        current = _current_owner()
        if self._runner is not None and self._runner is not current:
            await asyncio.shield(asyncio.gather(self._runner, return_exceptions=True))
        if self._decision_task is not None and self._decision_task is not current:
            self._decision_task.cancel()
            await asyncio.gather(self._decision_task, return_exceptions=True)
        if self._drainer is not None and self._drainer is not current:
            self._drainer.cancel()
            await asyncio.gather(self._drainer, return_exceptions=True)


def stream_client(api_key: Optional[str] = None, **options: Any) -> SystemZeroStreamClient:
    return SystemZeroStreamClient(api_key=api_key, **options)
