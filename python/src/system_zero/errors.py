"""Typed public failures with open code strings and sanitized envelope details."""
from ._redaction import redact, redact_text
from ._retry import _retry_after_seconds
from types import MappingProxyType
from typing import Any, Literal, Mapping, Optional, Union

class SystemZeroError(Exception):

    def __init__(self, message: str, *, status: Optional[int]=None, type: str='api_error', code: Optional[str]=None, param: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None, request_id: Optional[str]=None, headers: Optional[Mapping[str, str]]=None, idempotency_key: Optional[str]=None):
        message = redact_text(message)
        type, code, param = (redact(type), redact(code), redact(param))
        request_id, idempotency_key = (redact(request_id), redact(idempotency_key))
        details, headers = (redact(details), redact(headers))
        super().__init__(message)
        self.message = message
        self.status = status
        self.type = type
        self.code = code
        self.param = param
        self.retryable = retryable
        self.details = details
        self.request_id = request_id
        self.idempotency_key = idempotency_key
        self.headers = MappingProxyType(dict(headers or {}))

    def __reduce__(self):
        state = dict(redact(vars(self)))
        state['headers'] = dict(self.headers)
        return (type(self), (self.message,), state)

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.headers = MappingProxyType(dict(self.headers))

class InvalidRequestError(SystemZeroError):
    """The server refused the request shape or content."""

class AuthenticationError(SystemZeroError):
    """API-key authentication failed."""

class PermissionError(SystemZeroError):
    """The account cannot use this resource."""

class NotFoundError(SystemZeroError):
    """The endpoint or resource is unavailable."""

class RateLimitError(SystemZeroError):
    """The server refused a rate or allowance limit."""

    def __init__(self, message: str, **fields: Any):
        super().__init__(message, **fields)
        seconds = _retry_after_seconds(self.headers)
        self.retry_after_ms = None if seconds is None else seconds * 1000

class StreamLimitError(RateLimitError):
    """An account stream refusal; automatic recovery may retry a stale lease."""

    def __init__(self, message: str, *, retry_after_ms: Optional[float]=None, retry_after: Optional[str]=None, should_retry: Optional[bool]=None, **fields: Any):
        super().__init__(message, **fields)
        limit = self.details.get('limit') if self.details is not None else None
        self.limit = limit if type(limit) is int and 0 <= limit <= 9007199254740991 else None
        self.retry_after_ms = retry_after_ms
        self.retry_after = retry_after
        self.should_retry = self.retryable if should_retry is None else should_retry

class OverloadedError(SystemZeroError):
    """The platform has no available capacity."""

    def __init__(self, message: str, **fields: Any):
        super().__init__(message, **fields)
        seconds = _retry_after_seconds(self.headers)
        self.retry_after_ms = None if seconds is None else seconds * 1000

class APIError(SystemZeroError):
    """An API or proxy failure, including a non-JSON response."""

class ServerError(SystemZeroError):
    """A server_error envelope."""

class APIConnectionError(SystemZeroError):
    """A transport failed; evidence never infers delivery from a missing response."""

    def __init__(self, message: str, *, transport_code: Optional[str]=None, request_sent: Optional[bool]=None, **fields: Any):
        super().__init__(message, **fields)
        self.transport_code = transport_code
        self.request_sent = request_sent

class APITimeoutError(APIConnectionError):
    """Compatibility export for local workflow deadlines; transport uses APIConnectionError."""

class PayloadTooLargeError(InvalidRequestError):
    """The complete caller JSON exceeds the body limit."""

class InvalidCriteriaError(InvalidRequestError):
    """The question criteria are outside the applicable request contract."""

class ServiceUnavailableError(APIError):
    """The gateway cannot serve the request; preserve its retryability hint."""

class CapabilityNotAvailableError(NotFoundError):
    """An independently held route or input class is unavailable."""
RouteNotAvailableError = CapabilityNotAvailableError

class LongContextNotAvailableError(RouteNotAvailableError):
    """Expected HTTP 404 until workspace availability. Workspace opt-in is also required.

    eligibility exposes details.eligibility, including future rule strings.
    """

    @property
    def eligibility(self) -> Optional[Union[Literal['billing', 'opt_in', 'class', 'deny_list', 'global_switch', 'account_deleted'], str]]:
        value = self.details.get('eligibility') if self.details is not None else None
        return value if isinstance(value, str) else None

class LongRecordingNotAvailableError(RouteNotAvailableError):
    """Long recordings are held independently of short audio."""

class VideoNotAvailableError(RouteNotAvailableError):
    """Video is held."""

class ImageNotAvailableError(RouteNotAvailableError):
    """Images are held."""

class AudioNotAvailableError(RouteNotAvailableError):
    """Short audio is held."""

class DocumentNotAvailableError(RouteNotAvailableError):
    """Documents are held."""

class CommitteeNotAvailableError(RouteNotAvailableError):
    """The committee route is held."""

class DeliberateNotAvailableError(RouteNotAvailableError):
    """The deliberate mode is held."""
MEDIA_ERROR_CLASSES = {'payload_too_large': PayloadTooLargeError, 'invalid_criteria': InvalidCriteriaError, 'service_unavailable': ServiceUnavailableError, 'long_context_not_available': LongContextNotAvailableError, 'long_recording_not_available': LongRecordingNotAvailableError, 'video_not_available': VideoNotAvailableError, 'image_not_available': ImageNotAvailableError, 'audio_not_available': AudioNotAvailableError, 'document_not_available': DocumentNotAvailableError, 'committee_not_available': CommitteeNotAvailableError, 'deliberate_not_available': DeliberateNotAvailableError}
ERROR_CLASSES = {'invalid_request_error': InvalidRequestError, 'authentication_error': AuthenticationError, 'permission_error': PermissionError, 'not_found_error': NotFoundError, 'rate_limit_error': RateLimitError, 'overloaded_error': OverloadedError, 'api_error': APIError, 'server_error': ServerError}

def error_class(kind: str, code: Optional[str]):
    """Retain envelope type compatibility while specializing current API codes."""
    base = ERROR_CLASSES.get(kind, APIError) if isinstance(kind, str) else APIError
    if isinstance(code, str):
        return MEDIA_ERROR_CLASSES.get(code, base)
    return base
