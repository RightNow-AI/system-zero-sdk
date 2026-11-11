"""Content-free webhook types, receiver verification and workspace management."""

from hashlib import sha256
import hmac
import re
import time
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional, TypeAlias, TypedDict, Union, cast

from .errors import SystemZeroError
from .response import ResponseResult
from .webhook_errors import WebhookInvalidError, map_webhook_error

if TYPE_CHECKING:
    from .client import SystemZero


WEBHOOK_MAX_BODY_BYTES = 16384
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300
WebhookEventType = Literal["decision.completed", "decision.refused", "stream.closed"]
WebhookStreamCloseReason = Literal[
    "client_close", "shutdown", "max_lifetime", "idle", "maintenance", "lease_lost",
    "slow_reader", "overload", "auth", "policy", "protocol", "store_unavailable",
]
WebhookBody = Union[str, bytes, bytearray, memoryview]
WebhookEndpointId: TypeAlias = str
WebhookSigningSecret: TypeAlias = str
WebhookSignatureHeader: TypeAlias = str
WebhookManagementErrorCode: TypeAlias = Literal[
    "webhook_invalid", "webhook_not_found", "webhook_limit_exceeded", "webhook_unavailable",
]


class WebhookEmptyRequest(TypedDict):
    pass


class WebhookEndpoint(TypedDict):
    id: str
    object: Literal["webhook_endpoint"]
    url: str
    events: list[WebhookEventType]
    created: int


class WebhookEndpointWithSecret(WebhookEndpoint):
    secret: str


class WebhookCreateRequest(TypedDict):
    url: str
    events: list[WebhookEventType]


class WebhookEndpointList(TypedDict):
    object: Literal["list"]
    data: list[WebhookEndpoint]


class WebhookDeleted(TypedDict):
    id: str
    deleted: Literal[True]


class WebhookDelivery(TypedDict):
    event_id: str
    type: WebhookEventType
    attempt: int
    status_code: Optional[int]
    latency_ms: int
    outcome: Literal["delivered", "http_error", "blocked", "timeout", "network_error", "dead_letter"]
    created: int


class WebhookDeliveryList(TypedDict):
    object: Literal["list"]
    data: list[WebhookDelivery]


class WebhookUsageUnits(TypedDict):
    input_tokens: int
    cached_input_tokens: int
    media_tokens: int
    cached_media_tokens: int
    long_input_tokens: int
    media_items: int
    thought_tokens: int


class WebhookInputClass(TypedDict):
    name: Literal["text", "document", "image", "audio", "video"]
    status: Literal["served", "held"]


class WebhookDecisionData(TypedDict):
    decision_id: str
    request_id: str
    created: int
    model: str
    route: Optional[Literal["direct", "long"]]
    classes: list[WebhookInputClass]
    status: int
    refusal_code: Optional[str]
    usage_units: WebhookUsageUnits


class WebhookStreamClosedData(TypedDict):
    stream_id: str
    close_code: int
    reason: WebhookStreamCloseReason


class _WebhookEventFields(TypedDict):
    id: str
    object: Literal["webhook_event"]
    created: int


class WebhookDecisionEvent(_WebhookEventFields):
    type: Literal["decision.completed", "decision.refused"]
    data: WebhookDecisionData


class WebhookStreamClosedEvent(_WebhookEventFields):
    type: Literal["stream.closed"]
    data: WebhookStreamClosedData


WebhookEvent = Union[WebhookDecisionEvent, WebhookStreamClosedEvent]
# OpenAPI component names share the existing SDK definitions.
WebhookDeleteReply = WebhookDeleted
WebhookDecisionCompletedData = WebhookDecisionData
WebhookDecisionRefusedData = WebhookDecisionData
_SIGNATURE = re.compile(r"t=(0|[1-9][0-9]{0,15}),v1=([a-f0-9]{64})")
_SECRET = re.compile(r"whsec_[a-f0-9]{64}")
_ENDPOINT_ID = re.compile(r"wh_[a-f0-9]{32}")


def verify_webhook_signature(raw_body: WebhookBody, signature_header: str, secret: str, *, now: Optional[int] = None) -> bool:
    """Verify original bytes before parsing JSON; deduplicate event ids separately.

    now is integer Unix seconds, defaulting to the receiver wall clock. The
    fixed window is 300 seconds in either direction. No payload is retained.
    """
    if not isinstance(secret, str) or len(secret) != 70 or _SECRET.fullmatch(secret) is None:
        return False
    if not isinstance(signature_header, str) or len(signature_header) > 86:
        return False
    match = _SIGNATURE.fullmatch(signature_header)
    if match is None:
        return False
    timestamp = int(match.group(1))
    current = int(time.time()) if now is None else now
    if (type(current) is not int or not 0 <= current <= 9007199254740991 or timestamp > 9007199254740991 or
            abs(current - timestamp) > WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS):
        return False
    if isinstance(raw_body, str):
        if len(raw_body) > WEBHOOK_MAX_BODY_BYTES:
            return False
        try:
            body = raw_body.encode("utf-8")
        except UnicodeEncodeError:
            return False
    elif isinstance(raw_body, (bytes, bytearray)):
        if len(raw_body) > WEBHOOK_MAX_BODY_BYTES:
            return False
        body = bytes(raw_body)
    elif isinstance(raw_body, memoryview):
        try:
            if raw_body.nbytes > WEBHOOK_MAX_BODY_BYTES:
                return False
            body = raw_body.tobytes()
        except ValueError:
            return False
    else:
        return False
    if len(body) > WEBHOOK_MAX_BODY_BYTES:
        return False
    expected = hmac.new(bytes.fromhex(secret[6:]), match.group(1).encode("ascii") + b"." + body, sha256).digest()
    return hmac.compare_digest(expected, bytes.fromhex(match.group(2)))


def _invalid(message: str, param: str) -> WebhookInvalidError:
    return WebhookInvalidError(message, status=400, type="invalid_request_error", code="webhook_invalid", param=param, retryable=False)


def _endpoint_path(endpoint_id: str) -> str:
    if not isinstance(endpoint_id, str) or _ENDPOINT_ID.fullmatch(endpoint_id) is None:
        raise _invalid("The webhook endpoint ID is invalid.", "id")
    return "/system-zero/webhooks/" + endpoint_id


class Webhooks:
    """Use an existing SystemZero client; destination admission stays on the server.

    Create and rotate never retry automatically because their response contains
    a signing secret returned once. Store that value in a server secret manager.
    """

    def __init__(self, client: "SystemZero"):
        self._client = client

    def create(self, request: WebhookCreateRequest, *, client_request_id: Optional[str] = None,
               headers: Optional[Mapping[str, str]] = None,
               retry_budget: Optional[float] = None) -> WebhookEndpointWithSecret:
        return self.create_with_response(request, client_request_id=client_request_id,
                                         headers=headers, retry_budget=retry_budget).data

    def create_with_response(self, request: WebhookCreateRequest, *, client_request_id: Optional[str] = None,
                             headers: Optional[Mapping[str, str]] = None,
                             retry_budget: Optional[float] = None) -> ResponseResult[WebhookEndpointWithSecret]:
        return cast(ResponseResult[WebhookEndpointWithSecret], self._request(
            "POST", "/system-zero/webhooks", body=request, client_request_id=client_request_id,
            headers=headers, retries=0, retry_budget=retry_budget,
        ))

    def list(self, *, client_request_id: Optional[str] = None,
             headers: Optional[Mapping[str, str]] = None,
             retry_budget: Optional[float] = None) -> WebhookEndpointList:
        return self.list_with_response(client_request_id=client_request_id, headers=headers, retry_budget=retry_budget).data

    def list_with_response(self, *, client_request_id: Optional[str] = None,
                           headers: Optional[Mapping[str, str]] = None,
                           retry_budget: Optional[float] = None) -> ResponseResult[WebhookEndpointList]:
        return cast(ResponseResult[WebhookEndpointList], self._request(
            "GET", "/system-zero/webhooks", client_request_id=client_request_id, headers=headers, retry_budget=retry_budget,
        ))

    def delete(self, endpoint_id: str, *, client_request_id: Optional[str] = None,
               headers: Optional[Mapping[str, str]] = None,
               retry_budget: Optional[float] = None) -> WebhookDeleted:
        return self.delete_with_response(endpoint_id, client_request_id=client_request_id,
                                         headers=headers, retry_budget=retry_budget).data

    def delete_with_response(self, endpoint_id: str, *, client_request_id: Optional[str] = None,
                             headers: Optional[Mapping[str, str]] = None,
                             retry_budget: Optional[float] = None) -> ResponseResult[WebhookDeleted]:
        return cast(ResponseResult[WebhookDeleted], self._request(
            "DELETE", _endpoint_path(endpoint_id), client_request_id=client_request_id, headers=headers, retry_budget=retry_budget,
        ))

    def rotate_secret(self, endpoint_id: str, *, client_request_id: Optional[str] = None,
                      headers: Optional[Mapping[str, str]] = None,
                      retry_budget: Optional[float] = None) -> WebhookEndpointWithSecret:
        return self.rotate_secret_with_response(endpoint_id, client_request_id=client_request_id,
                                                headers=headers, retry_budget=retry_budget).data

    def rotate_secret_with_response(self, endpoint_id: str, *, client_request_id: Optional[str] = None,
                                    headers: Optional[Mapping[str, str]] = None,
                                    retry_budget: Optional[float] = None) -> ResponseResult[WebhookEndpointWithSecret]:
        return cast(ResponseResult[WebhookEndpointWithSecret], self._request(
            "POST", _endpoint_path(endpoint_id) + "/rotate-secret", body={},
            client_request_id=client_request_id, headers=headers, retries=0, retry_budget=retry_budget,
        ))

    def deliveries(self, endpoint_id: str, *, limit: Optional[int] = None, client_request_id: Optional[str] = None,
                   headers: Optional[Mapping[str, str]] = None,
                   retry_budget: Optional[float] = None) -> WebhookDeliveryList:
        return self.deliveries_with_response(endpoint_id, limit=limit, client_request_id=client_request_id,
                                             headers=headers, retry_budget=retry_budget).data

    def deliveries_with_response(self, endpoint_id: str, *, limit: Optional[int] = None, client_request_id: Optional[str] = None,
                                 headers: Optional[Mapping[str, str]] = None,
                                 retry_budget: Optional[float] = None) -> ResponseResult[WebhookDeliveryList]:
        path = _endpoint_path(endpoint_id) + "/deliveries"
        if limit is not None:
            if type(limit) is not int or not 1 <= limit <= 100:
                raise _invalid("Delivery log limit must be an integer from 1 through 100.", "limit")
            path += "?limit=" + str(limit)
        return cast(ResponseResult[WebhookDeliveryList], self._request(
            "GET", path, client_request_id=client_request_id, headers=headers, retry_budget=retry_budget,
        ))

    def _request(self, method: str, path: str, *, body: Optional[Mapping[str, Any]] = None,
                 client_request_id: Optional[str] = None, headers: Optional[Mapping[str, str]] = None,
                 retries: Optional[int] = None, retry_budget: Optional[float] = None) -> ResponseResult[Any]:
        if headers is not None and any(name.lower() == "idempotency-key" for name in headers):
            raise _invalid("Webhook management does not accept an Idempotency-Key.", "headers")
        try:
            return self._client._request(method, path, body=body, client_request_id=client_request_id, headers=headers,
                                         generate_idempotency=False, retries=retries, retry_budget=retry_budget)
        except SystemZeroError as error:
            mapped = map_webhook_error(error)
            if mapped is error:
                raise
            raise mapped from None
