"""Read retained decisions and content-free traces through the existing client."""

from enum import Enum
import re
from ._wire import _wire_for, _DecisionTraceObject
from types import MappingProxyType
from typing import Any, Dict, List, Literal, Mapping, Optional, TYPE_CHECKING, TypeAlias, TypedDict, cast

from .errors import NotFoundError, SystemZeroError
from .response import ResponseResult

if TYPE_CHECKING:
    from .client import SystemZero


DecisionId: TypeAlias = str


class DecisionRecordErrorCode(str, Enum):
    DECISION_NOT_FOUND = "decision_not_found"


DECISION_RECORD_ERROR_CODES = MappingProxyType({
    "decision_not_found": MappingProxyType({"status": 404, "type": "not_found_error", "retryable": False}),
})


class DecisionNotFoundError(NotFoundError):
    """The caller's account and API key have no retained answer for this ID."""

    def __init__(self, message: str, **fields: Any):
        super().__init__(message, **{
            **fields, **DECISION_RECORD_ERROR_CODES["decision_not_found"], "code": "decision_not_found",
        })


DecisionTraceAttachment = TypedDict("DecisionTraceAttachment", {
    "class": Literal["document", "image", "audio", "video"], "count": int,
})


class DecisionTraceTimings(TypedDict):
    queue_ms: Optional[float]
    node_ms: Optional[float]
    settle_ms: Optional[float]


class DecisionTraceUsage(TypedDict):
    input_tokens: Optional[int]
    cached_input_tokens: Optional[int]
    media_tokens: Optional[int]
    cached_media_tokens: Optional[int]
    long_input_tokens: Optional[int]
    media_items: Optional[int]
    thought_tokens: Optional[int]


class DecisionTrace(TypedDict):
    """None means the existing records do not retain a value; no content is inferred."""

    object: _DecisionTraceObject
    id: DecisionId
    request_id: Optional[str]
    created: int
    model: str
    model_version: str
    render_id: Optional[str]
    route: Literal["direct", "long"]
    attachments: Optional[List[DecisionTraceAttachment]]
    timings: DecisionTraceTimings
    usage_units: DecisionTraceUsage
    refusal_code: Optional[str]
    refusal_codes: List[str]
    idempotency: Optional[Literal["fresh", "replay"]]
    stream_id: Optional[str]
    state_version: Optional[int]


# Define record types before types.py re-exports them during either import order.
from .types import DecideReply


def _path(id: str, suffix: str, wire=None) -> str:
    if not isinstance(id, str) or re.fullmatch(r"szd_[a-f0-9]{32}", id) is None:
        raise ValueError("The decision ID is invalid.")
    return (wire or _wire_for()).decisions + "/" + id + suffix


class DecisionRecords:
    """Same-key lookup limited to the replay store's existing retention window.

    This resource adds no retries, content cache, or idempotency key. The client's
    ordinary GET request machinery owns auth, timeouts, retries, and metadata.
    """

    def __init__(self, client: "SystemZero"):
        self._client = client

    def get_decision(
        self, id: str, *, client_request_id: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        retry_budget: Optional[float] = None,
    ) -> DecideReply:
        return self.get_decision_with_response(id, client_request_id=client_request_id,
                                               headers=headers, retry_budget=retry_budget).data

    def get_decision_with_response(
        self, id: str, *, client_request_id: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        retry_budget: Optional[float] = None,
    ) -> ResponseResult[DecideReply]:
        return cast(ResponseResult[DecideReply], self._get(id, "", client_request_id, headers, retry_budget))

    def get_decision_trace(
        self, id: str, *, client_request_id: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        retry_budget: Optional[float] = None,
    ) -> DecisionTrace:
        return self.get_decision_trace_with_response(id, client_request_id=client_request_id,
                                                     headers=headers, retry_budget=retry_budget).data

    def get_decision_trace_with_response(
        self, id: str, *, client_request_id: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        retry_budget: Optional[float] = None,
    ) -> ResponseResult[DecisionTrace]:
        return cast(ResponseResult[DecisionTrace], self._get(id, self._client._wire.trace, client_request_id, headers, retry_budget))

    getDecision = get_decision
    getDecisionTrace = get_decision_trace

    def _get(
        self, id: str, suffix: str, client_request_id: Optional[str], headers: Optional[Mapping[str, str]],
        retry_budget: Optional[float],
    ) -> ResponseResult[Any]:
        path = _path(id, suffix, self._client._wire)
        try:
            result = self._client._request("GET", path, client_request_id=client_request_id,
                                           headers=headers, retry_budget=retry_budget)
            data = result.data
            if data is result.data:
                return result
            return ResponseResult(data, result.metadata, result.status, result.headers)
        except SystemZeroError as error:
            if error.status != 404 or error.code != "decision_not_found" or isinstance(error, DecisionNotFoundError):
                raise
            fields: Dict[str, Any] = {
                "param": error.param, "details": error.details,
                "request_id": error.request_id, "headers": error.headers,
            }
            raise DecisionNotFoundError(error.message, **fields) from error
