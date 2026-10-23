"""Public stream v1 frames. Wire names stay snake_case, as in the HTTP types."""

from dataclasses import dataclass
from typing import Any, Literal, Optional, Protocol, TypedDict, Union

from .types import Attachment, DecideReply, DecideRequest


DecisionMode = Literal["on_change", "on_request"]
ReconnectPolicy = Literal["latest", "never"]


class StreamHello(TypedDict):
    type: Literal["hello"]
    stream_id: str
    seq: int
    protocol: Literal["system-zero.stream.v1"]
    request_id: str
    limits: dict[str, Any]
    rate_limit: Optional[dict[str, int]]


class StreamAck(TypedDict):
    type: Literal["ack"]
    stream_id: str
    seq: int
    event_id: str
    state_version: int
    state_bytes: int
    synthesized_body_bytes: int
    scheduled: bool


class StreamScheduled(TypedDict):
    type: Literal["scheduled"]
    stream_id: str
    seq: int
    state_version: int
    in_flight: bool


class StreamDecision(TypedDict):
    type: Literal["decision"]
    stream_id: str
    seq: int
    state_version: int
    covered_event_ids: list[str]
    reply: DecideReply
    request_id: str
    http_status: Literal[200]
    server_timing: Optional[str]
    latency_ms: Optional[float]
    rate_limit: Optional[dict[str, int]]
    queue_ms: float
    decide_ms: float


class StreamError(TypedDict):
    type: Literal["error"]
    stream_id: str
    seq: int
    phase: Literal["protocol", "decide", "lifecycle"]
    state_version: int
    event_id: Optional[str]
    covered_event_ids: list[str]
    http_status: int
    envelope: dict[str, Any]
    retry: dict[str, Any]
    rate_limit: Optional[dict[str, int]]
    server_timing: Optional[str]
    fatal: bool


class StreamGoodbye(TypedDict):
    type: Literal["goodbye"]
    stream_id: str
    seq: int
    reason: Optional[str]
    state_version: int
    last_decision_state_version: int
    resume: dict[str, Any]


class PossiblyDecided(TypedDict):
    """Unobserved, possibly dispatched work; excludes proven unscheduled state."""

    type: Literal["possibly_decided"]
    stream_id: str
    state_versions: list[int]
    event_ids: list[str]


StreamEvent = Union[StreamAck, StreamScheduled, StreamDecision, StreamError, StreamGoodbye, PossiblyDecided]


@dataclass(frozen=True)
class EventLatency:
    event_id: str
    state_version: int
    latency_ms: float
    decision: StreamDecision


class StreamClient(Protocol):
    async def connect(self) -> StreamHello:
        ...

    async def start(self, request: DecideRequest, *, decision_mode: DecisionMode = "on_change",
                    min_interval_ms: int = 0) -> StreamAck:
        ...

    async def append_text(self, text: str) -> StreamAck:
        ...

    async def append_items(self, items: list[Any]) -> StreamAck:
        ...

    async def append_attachments(self, attachments: list[Attachment]) -> StreamAck:
        ...

    async def replace(self, state: Any, *, attachments: Optional[list[Attachment]] = None) -> StreamAck:
        ...

    async def decide(self) -> StreamScheduled:
        ...

    async def wait_for_decision(self, ack: StreamAck) -> EventLatency:
        ...

    def events(self) -> "StreamEvents":
        ...

    async def close(self) -> None:
        ...


class StreamEvents(Protocol):
    def __aiter__(self) -> "StreamEvents":
        ...

    async def __anext__(self) -> StreamEvent:
        ...

    async def aclose(self) -> None:
        ...

    async def athrow(self, error: BaseException) -> None:
        ...
