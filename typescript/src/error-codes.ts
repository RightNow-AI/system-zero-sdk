// API error codes returned by System Zero.
export const SYSTEM_ZERO_ERROR_CODES = [
  "audio_not_available",
  "capacity_exhausted",
  "committee_not_available",
  "daily_dispatch_limit",
  "deliberate_not_available",
  "document_not_available",
  "https_required",
  "idempotency_key_reuse",
  "image_not_available",
  "injection_suspected",
  "input_quarantined",
  "invalid_criteria",
  "invalid_request",
  "long_context_not_available",
  "long_recording_not_available",
  "payload_too_large",
  "question_not_grounded",
  "question_too_large",
  "questions_too_large",
  "rate_limit_exceeded",
  "request_refused",
  "service_unavailable",
  "state_too_large",
  "state_too_large_unguarded",
  "system_zero_timeout",
  "system_zero_unavailable",
  "video_not_available",
  "workspace_limit_exceeded",
  "workspace_quarantined"
] as const;

export const GATEWAY_ERROR_CODES = [
  "account_suspended",
  "auth_error",
  "concurrency_limit_exceeded",
  "idempotency_conflict",
  "idempotency_replay_unavailable",
  "idempotency_unavailable",
  "internal_error",
  "invalid_api_key",
  "invalid_idempotency_key",
  "invalid_trace_header",
  "limiter_unavailable",
  "maintenance",
  "method_not_allowed",
  "model_not_found",
  "monthly_cap_reached",
  "unknown_endpoint"
] as const;

export type KnownErrorCode = typeof SYSTEM_ZERO_ERROR_CODES[number] | typeof GATEWAY_ERROR_CODES[number];
export const ERROR_CODES = Object.freeze(Object.fromEntries([...SYSTEM_ZERO_ERROR_CODES, ...GATEWAY_ERROR_CODES].map(code => [code.toUpperCase(), code]))) as Readonly<Record<Uppercase<KnownErrorCode>, KnownErrorCode>>;
