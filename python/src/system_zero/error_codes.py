"""API error codes returned by System Zero."""
from enum import Enum

class ErrorCode(str, Enum):
    ACCOUNT_SUSPENDED = 'account_suspended'
    AUDIO_NOT_AVAILABLE = 'audio_not_available'
    AUTH_ERROR = 'auth_error'
    CAPACITY_EXHAUSTED = 'capacity_exhausted'
    COMMITTEE_NOT_AVAILABLE = 'committee_not_available'
    CONCURRENCY_LIMIT_EXCEEDED = 'concurrency_limit_exceeded'
    DAILY_DISPATCH_LIMIT = 'daily_dispatch_limit'
    DELIBERATE_NOT_AVAILABLE = 'deliberate_not_available'
    DOCUMENT_NOT_AVAILABLE = 'document_not_available'
    HTTPS_REQUIRED = 'https_required'
    IDEMPOTENCY_CONFLICT = 'idempotency_conflict'
    IDEMPOTENCY_KEY_REUSE = 'idempotency_key_reuse'
    IDEMPOTENCY_REPLAY_UNAVAILABLE = 'idempotency_replay_unavailable'
    IDEMPOTENCY_UNAVAILABLE = 'idempotency_unavailable'
    IMAGE_NOT_AVAILABLE = 'image_not_available'
    INJECTION_SUSPECTED = 'injection_suspected'
    INPUT_QUARANTINED = 'input_quarantined'
    INTERNAL_ERROR = 'internal_error'
    INVALID_API_KEY = 'invalid_api_key'
    INVALID_CRITERIA = 'invalid_criteria'
    INVALID_IDEMPOTENCY_KEY = 'invalid_idempotency_key'
    INVALID_REQUEST = 'invalid_request'
    INVALID_TRACE_HEADER = 'invalid_trace_header'
    LIMITER_UNAVAILABLE = 'limiter_unavailable'
    LONG_CONTEXT_NOT_AVAILABLE = 'long_context_not_available'
    LONG_RECORDING_NOT_AVAILABLE = 'long_recording_not_available'
    MAINTENANCE = 'maintenance'
    METHOD_NOT_ALLOWED = 'method_not_allowed'
    MODEL_NOT_FOUND = 'model_not_found'
    MONTHLY_CAP_REACHED = 'monthly_cap_reached'
    PAYLOAD_TOO_LARGE = 'payload_too_large'
    QUESTION_NOT_GROUNDED = 'question_not_grounded'
    QUESTION_TOO_LARGE = 'question_too_large'
    QUESTIONS_TOO_LARGE = 'questions_too_large'
    RATE_LIMIT_EXCEEDED = 'rate_limit_exceeded'
    REQUEST_REFUSED = 'request_refused'
    SERVICE_UNAVAILABLE = 'service_unavailable'
    STATE_TOO_LARGE = 'state_too_large'
    STATE_TOO_LARGE_UNGUARDED = 'state_too_large_unguarded'
    SYSTEM_ZERO_TIMEOUT = 'system_zero_timeout'
    SYSTEM_ZERO_UNAVAILABLE = 'system_zero_unavailable'
    UNKNOWN_ENDPOINT = 'unknown_endpoint'
    VIDEO_NOT_AVAILABLE = 'video_not_available'
    WORKSPACE_LIMIT_EXCEEDED = 'workspace_limit_exceeded'
    WORKSPACE_QUARANTINED = 'workspace_quarantined'

SYSTEM_ZERO_ERROR_CODES = (
    'audio_not_available',
    'capacity_exhausted',
    'committee_not_available',
    'daily_dispatch_limit',
    'deliberate_not_available',
    'document_not_available',
    'https_required',
    'idempotency_key_reuse',
    'image_not_available',
    'injection_suspected',
    'input_quarantined',
    'invalid_criteria',
    'invalid_request',
    'long_context_not_available',
    'long_recording_not_available',
    'payload_too_large',
    'question_not_grounded',
    'question_too_large',
    'questions_too_large',
    'rate_limit_exceeded',
    'request_refused',
    'service_unavailable',
    'state_too_large',
    'state_too_large_unguarded',
    'system_zero_timeout',
    'system_zero_unavailable',
    'video_not_available',
    'workspace_limit_exceeded',
    'workspace_quarantined',
)

GATEWAY_ERROR_CODES = (
    'account_suspended',
    'auth_error',
    'concurrency_limit_exceeded',
    'idempotency_conflict',
    'idempotency_replay_unavailable',
    'idempotency_unavailable',
    'internal_error',
    'invalid_api_key',
    'invalid_idempotency_key',
    'invalid_trace_header',
    'limiter_unavailable',
    'maintenance',
    'method_not_allowed',
    'model_not_found',
    'monthly_cap_reached',
    'unknown_endpoint',
)

KNOWN_ERROR_CODES = frozenset(SYSTEM_ZERO_ERROR_CODES + GATEWAY_ERROR_CODES)
