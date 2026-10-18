"""Typed public failures with open code strings and unmodified envelope details."""
from types import MappingProxyType
from typing import Any, Literal, Mapping, Optional

class SystemZeroError(Exception):

    def __init__(self, message: str, *, status: Optional[int]=None, type: str='api_error', code: Optional[str]=None, param: Optional[str]=None, retryable: bool=False, details: Optional[Mapping[str, Any]]=None, request_id: Optional[str]=None, headers: Optional[Mapping[str, str]]=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.type = type
        self.code = code
        self.param = param
        self.retryable = retryable
        self.details = details
        self.request_id = request_id
        self.headers = MappingProxyType(dict(headers or {}))

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

class OverloadedError(SystemZeroError):
    """The platform has no available capacity."""

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
    """The API refused this route or input class."""
RouteNotAvailableError = CapabilityNotAvailableError

class LongContextNotAvailableError(RouteNotAvailableError):
    """The API refused long context."""

class LongRecordingNotAvailableError(RouteNotAvailableError):
    """The API refused a long recording."""

class VideoNotAvailableError(RouteNotAvailableError):
    """The API refused video input."""

class ImageNotAvailableError(RouteNotAvailableError):
    """The API refused image input."""

class AudioNotAvailableError(RouteNotAvailableError):
    """The API refused audio input."""

class DocumentNotAvailableError(RouteNotAvailableError):
    """The API refused document input."""

class CommitteeNotAvailableError(RouteNotAvailableError):
    """The API refused the committee route."""

class DeliberateNotAvailableError(RouteNotAvailableError):
    """The API refused deliberate mode."""
MEDIA_ERROR_CLASSES = {'payload_too_large': PayloadTooLargeError, 'invalid_criteria': InvalidCriteriaError, 'service_unavailable': ServiceUnavailableError, 'long_context_not_available': LongContextNotAvailableError, 'long_recording_not_available': LongRecordingNotAvailableError, 'video_not_available': VideoNotAvailableError, 'image_not_available': ImageNotAvailableError, 'audio_not_available': AudioNotAvailableError, 'document_not_available': DocumentNotAvailableError, 'committee_not_available': CommitteeNotAvailableError, 'deliberate_not_available': DeliberateNotAvailableError}
ERROR_CLASSES = {'invalid_request_error': InvalidRequestError, 'authentication_error': AuthenticationError, 'permission_error': PermissionError, 'not_found_error': NotFoundError, 'rate_limit_error': RateLimitError, 'overloaded_error': OverloadedError, 'api_error': APIError, 'server_error': ServerError}

def error_class(kind: str, code: Optional[str]):
    """Retain envelope type compatibility while specializing current API codes."""
    base = ERROR_CLASSES.get(kind, APIError) if isinstance(kind, str) else APIError
    if isinstance(code, str):
        return MEDIA_ERROR_CLASSES.get(code, base)
    return base
