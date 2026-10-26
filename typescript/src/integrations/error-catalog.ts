export const SYSTEM_ZERO_ERRORS = {
  "account_suspended": [
    403,
    "This account does not currently have API access."
  ],
  "attachment_too_large": [
    413,
    "This input exceeds the supported limits."
  ],
  "audio_not_available": [
    404,
    "Audio is unavailable. Send the request without the audio attachment."
  ],
  "auth_error": [
    403,
    "API key is revoked"
  ],
  "capacity_exhausted": [
    429,
    "RightNow is at capacity. Retry after the Retry-After delay."
  ],
  "committee_not_available": [
    404,
    "This route is not available."
  ],
  "concurrency_limit_exceeded": [
    429,
    "This account has reached its concurrent call limit. Retry after the Retry-After delay."
  ],
  "daily_dispatch_limit": [
    429,
    "This account reached today's request limit. It resets at 00:00 UTC."
  ],
  "deliberate_not_available": [
    404,
    "This mode is not available."
  ],
  "document_controls_not_available": [
    404,
    "Documents with control characters are unavailable. Remove the control characters and send it again."
  ],
  "document_decode_failed": [
    422,
    "The document could not be decoded."
  ],
  "document_not_available": [
    404,
    "Documents are unavailable. Send the request without the document attachment."
  ],
  "document_pictures_not_available": [
    404,
    "Pictures inside documents are unavailable. Send the document without pictures, or as text."
  ],
  "document_too_large": [
    413,
    "The document exceeds the decoded byte limit."
  ],
  "document_unsupported": [
    415,
    "The document format is not supported."
  ],
  "https_required": [
    403,
    "This API answers only over HTTPS. Send the request to the https address."
  ],
  "idempotency_conflict": [
    409,
    "A request with this Idempotency-Key is still running. Retry with the same key shortly."
  ],
  "idempotency_key_reuse": [
    400,
    "This idempotency key already settled another request."
  ],
  "idempotency_replay_unavailable": [
    409,
    "The answer to this request is no longer stored. Send it again with a new Idempotency-Key."
  ],
  "idempotency_replayed_error": [
    500,
    "The original request failed. Its stored error response is replayed."
  ],
  "idempotency_unavailable": [
    503,
    "Replay protection is temporarily unavailable. Retry this request with the same Idempotency-Key."
  ],
  "image_not_available": [
    404,
    "Images are unavailable. Send the request without the image attachment."
  ],
  "injection_suspected": [
    422,
    "The request was refused because its content reads as instructions to the model. Remove the embedded instructions and send it again."
  ],
  "input_quarantined": [
    422,
    "This exact input caused repeated serving failures, so it is refused for up to 24 hours. Change the input or contact support."
  ],
  "insufficient_credits": [
    402,
    "The workspace's balance does not cover this call."
  ],
  "internal_error": [
    500,
    "The request failed unexpectedly. Retry the request. Keep the request id if the failure persists."
  ],
  "invalid_api_key": [
    401,
    "Invalid API key."
  ],
  "invalid_criteria": [
    422,
    "The question criteria are invalid."
  ],
  "invalid_idempotency_key": [
    400,
    "Idempotency-Key must be nonblank ASCII and 255 characters or less."
  ],
  "invalid_request": [
    400,
    "The request is invalid."
  ],
  "invalid_trace_header": [
    400,
    "X-Client-Request-Id must be nonblank ASCII and 512 characters or less."
  ],
  "limiter_unavailable": [
    503,
    "API rate limiting is temporarily unavailable. Please retry shortly."
  ],
  "long_context_daily_budget_exhausted": [
    429,
    "The daily cap for this decision class is reached. New work of this class resumes on the next UTC day."
  ],
  "long_context_not_available": [
    404,
    "This route is not available."
  ],
  "long_duration_unsupported": [
    413,
    "This input exceeds the supported limits."
  ],
  "long_evidence_incomplete": [
    422,
    "This input could not be read completely."
  ],
  "long_media_guard_refused": [
    422,
    "This input was refused."
  ],
  "long_no_readable_window": [
    422,
    "This input could not be read."
  ],
  "long_options_unsupported": [
    422,
    "These options cannot be evaluated for this input."
  ],
  "long_phrase_invalid": [
    422,
    "This phrase cannot be evaluated for this input."
  ],
  "long_question_type_unsupported": [
    422,
    "This question type cannot be answered for this input."
  ],
  "long_question_unsupported": [
    422,
    "This question cannot be answered for this input."
  ],
  "long_recording_not_available": [
    404,
    "This input type is not supported."
  ],
  "long_state_unsupported": [
    400,
    "This input combination is not supported."
  ],
  "long_too_many_phrases": [
    413,
    "This request exceeds the supported limits."
  ],
  "maintenance": [
    503,
    "The API is down for maintenance. Retry after the Retry-After delay."
  ],
  "media_decode_failed": [
    422,
    "The media bytes do not match the declared format."
  ],
  "media_too_large": [
    413,
    "The media exceeds the decoded byte limit."
  ],
  "media_unpriced": [
    403,
    "This input type is not supported."
  ],
  "media_unsupported": [
    400,
    "This input kind or count is not supported."
  ],
  "method_not_allowed": [
    405,
    "The HTTP method is not supported by this endpoint."
  ],
  "model_not_found": [
    404,
    "The requested model is not available."
  ],
  "monthly_cap_reached": [
    429,
    "This workspace reached its monthly decision cap."
  ],
  "output_limit_exceeded": [
    422,
    "The decision exceeded its output token limit."
  ],
  "payload_too_large": [
    413,
    "The request exceeds the body limit. Send fewer or smaller attachments."
  ],
  "question_not_grounded": [
    422,
    "The question cannot be answered from this state."
  ],
  "question_too_large": [
    422,
    "The question exceeds the size limit."
  ],
  "questions_too_large": [
    413,
    "The questions exceed the reply or token limit. Send fewer or shorter questions."
  ],
  "rate_limit_exceeded": [
    429,
    "Too many requests from this key or address. Retry after the Retry-After delay."
  ],
  "request_refused": [
    422,
    "RightNow refused this request. Change it before sending it again."
  ],
  "service_unavailable": [
    503,
    "RightNow cannot serve this request right now. Retry after the Retry-After delay."
  ],
  "staging_attempt_ceiling": [
    429,
    "The request ceiling is reached. Retry after the Retry-After delay."
  ],
  "state_too_large": [
    413,
    "The state exceeds the size or nesting limit."
  ],
  "state_too_large_unguarded": [
    413,
    "This release does not answer states of this length. Shorten or split the state."
  ],
  "stream_limit_exceeded": [
    429,
    "This account has reached its stream limit. Retry after the Retry-After delay."
  ],
  "system_zero_timeout": [
    504,
    "RightNow exceeded the response deadline."
  ],
  "system_zero_unavailable": [
    503,
    "RightNow is temporarily unavailable."
  ],
  "unknown_endpoint": [
    404,
    "The requested API endpoint was not found."
  ],
  "upload_in_use": [
    409,
    "The requested item is in use by another request."
  ],
  "upload_not_found": [
    404,
    "The requested item is not available to this account."
  ],
  "upload_not_ready": [
    409,
    "The requested item is not ready."
  ],
  "video_not_available": [
    404,
    "This input type is not supported."
  ],
  "workspace_limit_exceeded": [
    429,
    "This workspace is over one of its own allowances, such as calls in flight or bytes being read. Retry after the Retry-After delay."
  ],
  "workspace_quarantined": [
    422,
    "This account's requests are paused for up to an hour after repeated serving failures. Contact support."
  ]
} as const;
export type SystemZeroErrorCode = keyof typeof SYSTEM_ZERO_ERRORS;
