"""HTTP retry behavior, with stricter workflow recovery."""

from contextvars import ContextVar
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import re
import time
from typing import Callable, Mapping, Optional, TypedDict


_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_HTTP_DATE = re.compile(r"^(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?),?\s", re.IGNORECASE)


class _RetryContext(TypedDict, total=False):
    workflow_recovery: bool
    max_retries: int
    random: Callable[[], float]
    sleep: Callable[[float], None]
    remaining: Callable[[], float]
    on_attempt: Callable[[], None]
    on_deadline: Callable[[], None]


# Batch workers count attempts and apply a tighter deadline to the same HTTP loop.
_retry_context: ContextVar[Optional[_RetryContext]] = ContextVar("system_zero_retry_context", default=None)


def _finite_nonnegative(value: Optional[str]) -> Optional[float]:
    if value is None or _DECIMAL.fullmatch(value.strip()) is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _retry_after_seconds(headers: Mapping[str, str], now: Callable[[], float] = time.time) -> Optional[float]:
    normalized = {key.lower(): value.strip() for key, value in headers.items()}
    milliseconds = _finite_nonnegative(normalized.get("retry-after-ms"))
    waits = [] if milliseconds is None else [milliseconds / 1000]
    raw = normalized.get("retry-after")
    seconds = _finite_nonnegative(raw)
    if seconds is not None:
        waits.append(seconds)
    elif raw and _HTTP_DATE.match(raw.strip()):
        try:
            date = parsedate_to_datetime(raw)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            waits.append(max(0, date.timestamp() - now()))
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return max(waits) if waits else None


def _retry_response(status: int, code: Optional[str], headers: Mapping[str, str],
                    retryable: Optional[bool] = None) -> bool:
    normalized = {key.lower(): value.strip() for key, value in headers.items()}
    hint = normalized.get("x-should-retry")
    if retryable is False or hint == "false":
        return False
    if retryable is True or hint == "true":
        if (status == 429 and code in {"capacity_exhausted", "rate_limit", "rate_limit_exceeded"} or
                status == 503 and code == "service_unavailable"):
            return True
    return False


def _recover_workflow(status: int, code: Optional[str], headers: Mapping[str, str],
                      retryable: Optional[bool] = None, details: Optional[Mapping[str, object]] = None) -> bool:
    """Workflow recovery accepts explicit admission code/cause pairs."""
    normalized = {key.lower(): value.strip() for key, value in headers.items()}
    hint = normalized.get("x-should-retry")
    if status != 429 or hint == "false" or retryable is False or not (hint == "true" or retryable is True):
        return False
    cause = details.get("cause") if isinstance(details, Mapping) else None
    # Only these code and cause pairs establish refusal before work started.
    return isinstance(cause, str) and (code, cause) in {
        ("rate_limit_exceeded", "key_rate"), ("rate_limit_exceeded", "preauth"),
        ("concurrency_limit_exceeded", "account_concurrency"),
    }


def _retry_wait(headers: Mapping[str, str], attempt: int,
                now: Callable[[], float], draw: Callable[[], float]) -> float:
    jitter = min(8.0, 0.5 * 2 ** min(attempt, 4)) * draw()
    return max(jitter, _retry_after_seconds(headers, now) or 0)
