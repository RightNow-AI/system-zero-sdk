import {
  InvalidRequestError, NotFoundError, OverloadedError, RateLimitError,
  SystemZeroError, type ErrorFields,
} from './errors.js';

/** Public webhook metadata; the generic generated catalogs retain their historical contract pin. */
export const WEBHOOK_ERROR_CODES = Object.freeze({
  webhook_invalid: Object.freeze({ status: 400, type: 'invalid_request_error', retryable: false }),
  webhook_not_found: Object.freeze({ status: 404, type: 'not_found_error', retryable: false }),
  webhook_limit_exceeded: Object.freeze({ status: 429, type: 'rate_limit_error', retryable: false }),
  webhook_unavailable: Object.freeze({ status: 503, type: 'overloaded_error', retryable: true }),
});
export type WebhookErrorCode = keyof typeof WEBHOOK_ERROR_CODES;

export class WebhookInvalidError extends InvalidRequestError {}
export class WebhookNotFoundError extends NotFoundError {}
export class WebhookLimitExceededError extends RateLimitError {}
export class WebhookUnavailableError extends OverloadedError {}

const classes = {
  webhook_invalid: WebhookInvalidError,
  webhook_not_found: WebhookNotFoundError,
  webhook_limit_exceeded: WebhookLimitExceededError,
  webhook_unavailable: WebhookUnavailableError,
} as const;

/** Specialize only webhook failures, retaining the existing transport envelope. */
export function mapWebhookError(error: unknown): unknown {
  if (!(error instanceof SystemZeroError) || error.code === null || !Object.hasOwn(classes, error.code)) return error;
  const ErrorClass = classes[error.code as WebhookErrorCode];
  if (error instanceof ErrorClass) return error;
  const fields: ErrorFields = {
    status: error.status, type: error.type, code: error.code, param: error.param,
    retryable: error.retryable, request_id: error.request_id, headers: error.headers,
  };
  if (error.details !== undefined) fields.details = error.details;
  if (error.cause !== undefined) fields.cause = error.cause;
  return new ErrorClass(error.message, fields);
}
