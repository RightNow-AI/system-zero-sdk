export default {
  "account_suspended": {
    "status": 403,
    "message": "This account does not currently have API access."
  },
  "auth_error": {
    "status": 403,
    "message": "API key is revoked"
  },
  "concurrency_limit_exceeded": {
    "status": 429,
    "message": "This account has reached its concurrent call limit. Retry after the Retry-After delay."
  },
  "idempotency_conflict": {
    "status": 409,
    "message": "A request with this Idempotency-Key is still running. Retry with the same key shortly."
  },
  "idempotency_replay_unavailable": {
    "status": 409,
    "message": "The answer to this request is no longer stored. Send it again with a new Idempotency-Key."
  },
  "idempotency_replayed_error": {
    "status": 500,
    "message": "The original request failed. Its stored error response is replayed."
  },
  "idempotency_unavailable": {
    "status": 503,
    "message": "Replay protection is temporarily unavailable. Retry this request with the same Idempotency-Key."
  },
  "internal_error": {
    "status": 500,
    "message": "The request failed unexpectedly. Retry the request. Keep the request id if the failure persists."
  },
  "invalid_api_key": {
    "status": 401,
    "message": "Invalid API key."
  },
  "invalid_idempotency_key": {
    "status": 400,
    "message": "Idempotency-Key must be nonblank ASCII and 255 characters or less."
  },
  "invalid_trace_header": {
    "status": 400,
    "message": "X-Client-Request-Id must be nonblank ASCII and 512 characters or less."
  },
  "limiter_unavailable": {
    "status": 503,
    "message": "API rate limiting is temporarily unavailable. Please retry shortly."
  },
  "maintenance": {
    "status": 503,
    "message": "The API is down for maintenance. Retry after the Retry-After delay."
  },
  "method_not_allowed": {
    "status": 405,
    "message": "The HTTP method is not supported by this endpoint."
  },
  "model_not_found": {
    "status": 404,
    "message": "The requested model is not available."
  },
  "monthly_cap_reached": {
    "status": 429,
    "message": "This workspace reached its monthly decision cap."
  },
  "unknown_endpoint": {
    "status": 404,
    "message": "The requested API endpoint was not found."
  }
};
