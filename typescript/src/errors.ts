import type { ErrorEnvelope } from './api-types.js';

export interface ErrorFields {
  status?: number | null;
  type?: string | null;
  code?: string | null;
  param?: string | null;
  retryable?: boolean;
  details?: Record<string, unknown>;
  request_id?: string | null;
  headers?: Headers;
  cause?: unknown;
}

/** All HTTP, connection and timeout errors inherit this base. Codes are open strings. */
export class SystemZeroError extends Error {
  readonly status: number | null;
  readonly type: string | null;
  readonly code: string | null;
  readonly param: string | null;
  readonly retryable: boolean;
  readonly details: Record<string, unknown> | undefined;
  readonly request_id: string | null;
  readonly headers: Headers;

  constructor(message: string, fields: ErrorFields = {}) {
    super(message, fields.cause === undefined ? undefined : { cause: fields.cause });
    this.name = new.target.name;
    this.status = fields.status ?? null;
    this.type = fields.type ?? null;
    this.code = fields.code ?? null;
    this.param = fields.param ?? null;
    this.retryable = fields.retryable ?? false;
    this.details = fields.details;
    this.request_id = fields.request_id ?? null;
    this.headers = new Headers(fields.headers);
  }
}

export class InvalidRequestError extends SystemZeroError {}
export class AuthenticationError extends SystemZeroError {}
export class PermissionError extends SystemZeroError {}
export class NotFoundError extends SystemZeroError {}
export class RateLimitError extends SystemZeroError {}
export class OverloadedError extends SystemZeroError {}
export class APIError extends SystemZeroError {}
export class ServerError extends SystemZeroError {}
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
    if (typeof error.code === 'string' && error.code) code ??= error.code;
    timeout ||= error.name === 'TimeoutError';
    pending.push(error.cause, error.error);
    if (Array.isArray(error.errors)) pending.push(...error.errors.slice(0, 32));
  }
  timeout ||= code !== null && ['ETIMEDOUT', 'ESOCKETTIMEDOUT', 'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT', 'ERR_SOCKET_CONNECTION_TIMEOUT'].includes(code);
  return { code, timeout };
}

export class APIConnectionError extends SystemZeroError {
  readonly transport_code: string | null;
  readonly request_sent: boolean | null;

  constructor(message: string, fields: ConnectionErrorFields = {}) {
    const native = transportFailure(fields.cause);
    super(message, { ...fields, type: fields.type ?? (native.timeout || new.target === APITimeoutError ? 'timeout_error' : 'connection_error') });
    this.transport_code = fields.transport_code === undefined ? native.code : fields.transport_code;
    this.request_sent = fields.request_sent ?? null;
  }
}
/** Retained for compatibility and local workflow deadlines; transport failures use APIConnectionError. */
export class APITimeoutError extends APIConnectionError {}

// Specialized exceptions retain the API's code and envelope fields.
export class PayloadTooLargeError extends InvalidRequestError {}
export class InvalidCriteriaError extends InvalidRequestError {}
export class ServiceUnavailableError extends APIError {}
export class CapabilityNotAvailableError extends NotFoundError {}
export { CapabilityNotAvailableError as RouteNotAvailableError };
export class ImageNotAvailableError extends CapabilityNotAvailableError {}
export class AudioNotAvailableError extends CapabilityNotAvailableError {}
export class DocumentNotAvailableError extends CapabilityNotAvailableError {}
export class VideoNotAvailableError extends CapabilityNotAvailableError {}
export class LongContextNotAvailableError extends CapabilityNotAvailableError {}
export class LongRecordingNotAvailableError extends CapabilityNotAvailableError {}
export class CommitteeNotAvailableError extends CapabilityNotAvailableError {}
export class DeliberateNotAvailableError extends CapabilityNotAvailableError {}

const codeClasses = {
  payload_too_large: PayloadTooLargeError, invalid_criteria: InvalidCriteriaError,
  service_unavailable: ServiceUnavailableError,
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

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** A response is parsed defensively; proxy bodies and future envelope types stay usable. */
export function responseError(status: number, headers: Headers, body: unknown, fallbackRetryable: boolean): SystemZeroError {
  const envelope = record(body) && record(body.error) ? body : null;
  const error = envelope?.error as Partial<ErrorEnvelope['error']> | undefined;
  const type = typeof error?.type === 'string' ? error.type : 'api_error';
  const ErrorClass = typeof error?.code === 'string' && Object.hasOwn(codeClasses, error.code) ? codeClasses[error.code as keyof typeof codeClasses]
    : Object.hasOwn(classes, type) ? classes[type as keyof typeof classes] : APIError;
  const fields: ErrorFields = {
    status, headers, type,
    code: typeof error?.code === 'string' ? error.code : null,
    param: typeof error?.param === 'string' ? error.param : null,
    retryable: typeof error?.retryable === 'boolean' ? error.retryable : fallbackRetryable,
    request_id: typeof envelope?.request_id === 'string' ? envelope.request_id : headers.get('x-request-id'),
  };
  if (record(error?.details)) fields.details = error.details;
  return new ErrorClass(typeof error?.message === 'string' ? error.message : `System Zero HTTP request failed with status ${status}.`, fields);
}
