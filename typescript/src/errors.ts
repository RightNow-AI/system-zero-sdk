import { redact, redactText, registerErrorPrototype } from './redaction.js';
import { retryAfterMs } from './retry.js';
import type { ErrorEnvelope } from './api-types.js';

export interface ErrorFields {
  status?: number | null;
  type?: string | null;
  code?: string | null;
  param?: string | null;
  retryable?: boolean;
  details?: Record<string, unknown>;
  limit_cents?: number;
  spent_cents?: number;
  resets_at?: string;
  request_id?: string | null;
  idempotencyKey?: string | null;
  headers?: Headers;
  cause?: unknown;
}

/** All HTTP, connection and timeout errors inherit this base. Codes are open strings. */
export class RightNowError extends Error {
  readonly status: number | null;
  readonly type: string | null;
  readonly code: string | null;
  readonly param: string | null;
  readonly retryable: boolean;
  readonly details: Record<string, unknown> | undefined;
  readonly limit_cents: number | undefined;
  readonly spent_cents: number | undefined;
  readonly resets_at: string | undefined;
  readonly request_id: string | null;
  idempotencyKey: string | null;
  readonly headers: Headers;
  readonly retryAfterMs: number | null;

  constructor(message: string, fields: ErrorFields = {}) {
    message = redactText(message); fields = redact(fields);
    super(message, fields.cause === undefined ? undefined : { cause: fields.cause });
    this.name = new.target.name;
    this.status = fields.status ?? null;
    this.type = fields.type ?? null;
    this.code = fields.code ?? null;
    this.param = fields.param ?? null;
    this.retryable = fields.retryable ?? false;
    this.details = fields.details;
    this.limit_cents = fields.limit_cents;
    this.spent_cents = fields.spent_cents;
    this.resets_at = fields.resets_at;
    this.request_id = fields.request_id ?? null;
    this.idempotencyKey = fields.idempotencyKey ?? null;
    this.headers = new Headers(fields.headers);
    this.retryAfterMs = retryAfterMs(this.headers);
  }
}

export class InvalidRequestError extends RightNowError {}
/** Admission estimate exceeded the cap before any node work or usage counting. */
export class PolicyMaxCostExceededError extends InvalidRequestError { readonly counted = false; }
/** Completed work counts toward usage and computed cost despite the confidence refusal. */
export class PolicyConfidenceFailedError extends InvalidRequestError { readonly counted = true; }
export class AuthenticationError extends RightNowError {}
export class PermissionError extends RightNowError {}
export class NotFoundError extends RightNowError {}
export class RateLimitError extends RightNowError {}
export class StreamLimitError extends RateLimitError {
  readonly limit: number | null;
  readonly retry_after: string | null;
  readonly retry_after_ms: number | null;
  readonly should_retry: boolean;

  constructor(message: string, fields: ErrorFields = {}) {
    super(message, fields);
    const limit = this.details?.limit;
    this.limit = typeof limit === 'number' && Number.isSafeInteger(limit) && limit >= 0 ? limit : null;
    this.retry_after = this.headers.get('retry-after');
    const milliseconds = this.headers.get('retry-after-ms');
    const numeric = (value: string | null): number | null => {
      if (value === null || !value.trim()) return null;
      const number = Number(value); return Number.isFinite(number) && number >= 0 ? number : null;
    };
    const seconds = numeric(this.retry_after);
    const date = this.retry_after === null ? NaN : Date.parse(this.retry_after);
    this.retry_after_ms = numeric(milliseconds) ?? (seconds === null ? (Number.isFinite(date) ? Math.max(0, date - Date.now()) : null) : seconds * 1000);
    const hint = this.headers.get('x-should-retry');
    this.should_retry = hint === 'true' || (hint !== 'false' && this.retryable);
  }
}
export class OverloadedError extends RightNowError {}
export class APIError extends RightNowError {}
export class ServerError extends RightNowError {}
export interface ConnectionErrorFields extends ErrorFields {
  transport_code?: string | null;
  request_sent?: boolean | null;
}

function transportFailure(cause: unknown): { code: string | null; timeout: boolean } {
  const pending = [cause], seen = new Set<unknown>();
  let code: string | null = null, timeout = false;
  for (let index = 0; index < pending.length && index < 32; index++) {
    const value = pending[index];
    if (value === null || typeof value !== 'object' || seen.has(value)) continue;
    seen.add(value);
    // An SDK error's API code is not a native transport code.
    if (value instanceof APIConnectionError) { code ??= value.transport_code; timeout ||= value.type === 'timeout_error'; continue; }
    const error = value as { code?: unknown; name?: unknown; cause?: unknown; error?: unknown; errors?: unknown };
    if (typeof error.code === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/u.test(error.code)) code ??= error.code;
    timeout ||= error.name === 'TimeoutError';
    pending.push(error.cause, error.error);
    if (Array.isArray(error.errors)) pending.push(...error.errors.slice(0, 32));
  }
  timeout ||= code !== null && ['ETIMEDOUT', 'ESOCKETTIMEDOUT', 'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT', 'ERR_SOCKET_CONNECTION_TIMEOUT'].includes(code);
  return { code, timeout };
}

export class APIConnectionError extends RightNowError {
  readonly transport_code: string | null;
  readonly request_sent: boolean | null;

  constructor(message: string, fields: ConnectionErrorFields = {}) {
    const native = transportFailure(fields.cause);
    super(message, { ...fields, type: fields.type ?? (native.timeout || new.target === APITimeoutError ? 'timeout_error' : 'connection_error') });
    this.transport_code = fields.transport_code === undefined ? native.code : redactText(fields.transport_code ?? '') || null;
    this.request_sent = fields.request_sent ?? null;
  }
}
/** Retained for compatibility and local workflow deadlines; transport failures use APIConnectionError. */
export class APITimeoutError extends APIConnectionError {}

 
// but these names do not presume a class grant or a new refusal vocabulary.
export class PayloadTooLargeError extends InvalidRequestError {}
export class InvalidCriteriaError extends InvalidRequestError {}
export class ServiceUnavailableError extends APIError {}
export class CapabilityNotAvailableError extends NotFoundError {}
export { CapabilityNotAvailableError as RouteNotAvailableError };
export class ImageNotAvailableError extends CapabilityNotAvailableError {}
export class AudioNotAvailableError extends CapabilityNotAvailableError {}
export class DocumentNotAvailableError extends CapabilityNotAvailableError {}
export class VideoNotAvailableError extends CapabilityNotAvailableError {}
/** 1M needs no Console switch; a completed payment and request option are required. details.eligibility names a refused rule. */
export class LongContextNotAvailableError extends CapabilityNotAvailableError {
  get eligibility(): 'billing' | 'opt_in' | 'class' | 'deny_list' | 'global_switch' | 'account_deleted' | (string & Record<never, never>) | null {
    const value = this.details?.eligibility;
    return typeof value === 'string' ? value : null;
  }
}
export class LongRecordingNotAvailableError extends CapabilityNotAvailableError {}
export class CommitteeNotAvailableError extends CapabilityNotAvailableError {}
export class DeliberateNotAvailableError extends CapabilityNotAvailableError {}

const codeClasses = {
  payload_too_large: PayloadTooLargeError, invalid_criteria: InvalidCriteriaError,
  service_unavailable: ServiceUnavailableError,
  policy_max_cost_exceeded: PolicyMaxCostExceededError,
  policy_confidence_failed: PolicyConfidenceFailedError,
  image_not_available: ImageNotAvailableError, audio_not_available: AudioNotAvailableError,
  document_not_available: DocumentNotAvailableError, video_not_available: VideoNotAvailableError,
  long_context_not_available: LongContextNotAvailableError, long_recording_not_available: LongRecordingNotAvailableError,
  committee_not_available: CommitteeNotAvailableError, deliberate_not_available: DeliberateNotAvailableError,
} as const;

const classes = {
  invalid_request_error: InvalidRequestError,
  authentication_error: AuthenticationError,
  permission_error: PermissionError,
  not_found_error: NotFoundError,
  rate_limit_error: RateLimitError,
  overloaded_error: OverloadedError,
  api_error: APIError,
  server_error: ServerError,
} as const;

for (const kind of [RightNowError, APIConnectionError, APITimeoutError, StreamLimitError,
  CapabilityNotAvailableError, ...Object.values(classes), ...Object.values(codeClasses)]) {
  registerErrorPrototype(kind.prototype);
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** A response is parsed defensively; proxy bodies and future envelope types stay usable. */
export function responseError(status: number, headers: Headers, body: unknown, fallbackRetryable: boolean): RightNowError {
  const envelope = record(body) && record(body.error) ? body : null;
  const error = envelope?.error as Partial<ErrorEnvelope['error']> | undefined;
  const type = typeof error?.type === 'string' ? error.type : 'api_error';
  const ErrorClass = typeof error?.code === 'string' && Object.hasOwn(codeClasses, error.code) ? codeClasses[error.code as keyof typeof codeClasses]
    : status === 429 && error?.code === 'stream_limit_exceeded' ? StreamLimitError : Object.hasOwn(classes, type) ? classes[type as keyof typeof classes] : APIError;
  const fields: ErrorFields = {
    status, headers, type,
    code: typeof error?.code === 'string' ? error.code : null,
    param: typeof error?.param === 'string' ? error.param : null,
    retryable: typeof error?.retryable === 'boolean' ? error.retryable : fallbackRetryable,
    request_id: typeof envelope?.request_id === 'string' ? envelope.request_id : headers.get('x-request-id'),
  };
  if (record(error?.details)) fields.details = error.details;
  const limit = error?.limit_cents, spent = error?.spent_cents;
  if (typeof limit === 'number' && Number.isSafeInteger(limit) && limit >= 0) fields.limit_cents = limit;
  if (typeof spent === 'number' && Number.isSafeInteger(spent) && spent >= 0) fields.spent_cents = spent;
  if (typeof error?.resets_at === 'string') fields.resets_at = error.resets_at;
  return new ErrorClass(typeof error?.message === 'string' ? error.message : `RightNow HTTP request failed with status ${status}.`, fields);
}

/** @deprecated Use RightNowError. */
export { RightNowError as SystemZeroError };
