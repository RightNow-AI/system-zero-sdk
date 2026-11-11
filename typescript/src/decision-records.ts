import { APIPromise, type RequestOptions } from './client.js';
import type { PolicyReply } from './policy.js';
import { NotFoundError, SystemZeroError, type ErrorFields } from './errors.js';

export const DECISION_RECORD_ERROR_CODES = {
  decision_not_found: { status: 404, type: 'not_found_error', retryable: false },
} as const;
export type DecisionRecordErrorCode = keyof typeof DECISION_RECORD_ERROR_CODES;

/** The same account and API key can only read an answer the replay store still retains. */
export class DecisionNotFoundError extends NotFoundError {
  constructor(message: string, fields: ErrorFields = {}) {
    super(message, { ...fields, ...DECISION_RECORD_ERROR_CODES.decision_not_found, code: 'decision_not_found' });
  }
}

export interface DecisionTraceAttachment {
  class: 'document' | 'image' | 'audio' | 'video';
  count: number;
}

export interface DecisionTraceTimings {
  queue_ms: number | null;
  node_ms: number | null;
  settle_ms: number | null;
}

export interface DecisionTraceUsage {
  input_tokens: number | null;
  cached_input_tokens: number | null;
  media_tokens: number | null;
  cached_media_tokens: number | null;
  long_input_tokens: number | null;
  media_items: number | null;
  thought_tokens: number | null;
}

/** Null means the existing records do not retain that field; no content is inferred. */
export interface DecisionTrace {
  object: 'system_zero.decision_trace';
  id: string;
  request_id: string | null;
  created: number;
  model: string;
  model_version: string;
  render_id: string | null;
  route: 'direct' | 'long';
  attachments: DecisionTraceAttachment[] | null;
  timings: DecisionTraceTimings;
  usage_units: DecisionTraceUsage;
  refusal_code: string | null;
  refusal_codes: string[];
  idempotency: 'fresh' | 'replay' | null;
  stream_id: string | null;
  state_version: number | null;
}

export type DecisionLookupOptions = Omit<RequestOptions, 'idempotencyKey'>;

/** Integration binds this callback to the client's existing private request method. */
export type DecisionRecordsRequest = <T>(
  method: 'GET', path: string, body: undefined, options: RequestOptions,
) => APIPromise<T>;

export interface DecisionRecords {
  getDecision(id: string, options?: DecisionLookupOptions): APIPromise<PolicyReply>;
  getDecisionTrace(id: string, options?: DecisionLookupOptions): APIPromise<DecisionTrace>;
}

function lookupError(error: unknown): unknown {
  if (!(error instanceof SystemZeroError) || error.status !== 404 || error.code !== 'decision_not_found' || error instanceof DecisionNotFoundError) return error;
  return new DecisionNotFoundError(error.message, {
    param: error.param, request_id: error.request_id, headers: error.headers, cause: error,
    ...(error.details === undefined ? {} : { details: error.details }),
  });
}

/** Uses ordinary GET retries/auth, with no decide call, content cache, or new idempotency key. */
export function createDecisionRecords(request: DecisionRecordsRequest): DecisionRecords {
  function get<T>(id: string, suffix: string, options: DecisionLookupOptions): APIPromise<T> {
    if (typeof id !== 'string' || id.length !== 36 || !/^szd_[a-f0-9]{32}$/u.test(id)) throw new TypeError('The decision ID is invalid.');
    const result = request<T>('GET', `/system-zero/decisions/${id}${suffix}`, undefined, options)
      .withResponse().then(response => {
        return response;
      }).catch((error: unknown) => { throw lookupError(error); });
    return new APIPromise(result);
  }
  return Object.freeze({
    getDecision: (id: string, options: DecisionLookupOptions = {}) => get<PolicyReply>(id, '', options),
    getDecisionTrace: (id: string, options: DecisionLookupOptions = {}) => get<DecisionTrace>(id, '/trace', options),
  });
}
