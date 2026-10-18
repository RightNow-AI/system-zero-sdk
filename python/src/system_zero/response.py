"""Response metadata is separate from parsed JSON, including on errors."""

from dataclasses import dataclass
import math
import re
from typing import Generic, Mapping, Optional, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class RateLimitInfo:
    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset: Optional[int] = None
    limit_requests: Optional[int] = None
    remaining_requests: Optional[int] = None
    reset_requests: Optional[int] = None


@dataclass(frozen=True)
class ResponseMetadata:
    request_id: Optional[str]
    client_request_id: Optional[str]
    rate_limit: RateLimitInfo
    latency_ms: Optional[float]
    replayed: bool
    replay_header: Optional[str]


@dataclass(frozen=True)
class ResponseResult(Generic[T]):
    data: T
    metadata: ResponseMetadata
    status: int
    headers: Mapping[str, str]


def _integer(raw: Optional[str]) -> Optional[int]:
    return int(raw) if raw is not None and re.fullmatch(r"[0-9]+", raw) else None


def _number(raw: Optional[str]) -> Optional[float]:
    try:
        value = float(raw) if raw else float("nan")
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def response_metadata(headers: Mapping[str, str]) -> ResponseMetadata:
    primary = [_integer(headers.get("x-ratelimit-" + name)) for name in ("limit", "remaining", "reset")]
    requests = [_integer(headers.get("x-ratelimit-" + name + "-requests")) for name in ("limit", "remaining", "reset")]
    rates = RateLimitInfo(*[first if first is not None else second for first, second in zip(primary, requests)], *requests)
    replay = headers.get("x-rightnow-idempotent-replay")
    return ResponseMetadata(
        request_id=headers.get("x-request-id"),
        client_request_id=headers.get("x-client-request-id"),
        rate_limit=rates, latency_ms=_number(headers.get("x-latency-ms")),
        replayed=replay == "true", replay_header=replay,
    )
