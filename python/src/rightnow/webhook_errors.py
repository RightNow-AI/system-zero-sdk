"""Additive webhook failures, preserving the existing API error envelope."""

from types import MappingProxyType

from .errors import InvalidRequestError, NotFoundError, OverloadedError, RateLimitError, SystemZeroError


WEBHOOK_ERROR_CODES = MappingProxyType({
    "webhook_invalid": MappingProxyType({"status": 400, "type": "invalid_request_error", "retryable": False}),
    "webhook_not_found": MappingProxyType({"status": 404, "type": "not_found_error", "retryable": False}),
    "webhook_limit_exceeded": MappingProxyType({"status": 429, "type": "rate_limit_error", "retryable": False}),
    "webhook_unavailable": MappingProxyType({"status": 503, "type": "overloaded_error", "retryable": True}),
})


class WebhookInvalidError(InvalidRequestError):
    """The endpoint declaration or management request was refused."""


class WebhookNotFoundError(NotFoundError):
    """The endpoint does not exist in this authenticated workspace."""


class WebhookLimitExceededError(RateLimitError):
    """The workspace must delete an endpoint before creating another."""


class WebhookUnavailableError(OverloadedError):
    """The webhook service could not complete the operation."""


_CLASSES = {
    "webhook_invalid": WebhookInvalidError,
    "webhook_not_found": WebhookNotFoundError,
    "webhook_limit_exceeded": WebhookLimitExceededError,
    "webhook_unavailable": WebhookUnavailableError,
}


def map_webhook_error(error: SystemZeroError) -> SystemZeroError:
    """Specialize known webhook codes without losing headers or envelope fields."""
    cls = _CLASSES.get(error.code) if isinstance(error.code, str) else None
    if cls is None or isinstance(error, cls):
        return error
    return cls(error.message, status=error.status, type=error.type, code=error.code,
               param=error.param, retryable=error.retryable, details=error.details,
               request_id=error.request_id, headers=error.headers)
