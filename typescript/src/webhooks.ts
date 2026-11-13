import { WIRES, type Wire } from './wire.js';
import { createHmac, timingSafeEqual } from 'node:crypto';
import { APIPromise, type RequestOptions } from './client.js';
import { mapWebhookError, WEBHOOK_ERROR_CODES, WebhookInvalidError } from './webhook-errors.js';

export const WEBHOOK_MAX_BODY_BYTES = 16_384;
export const WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300;
export type WebhookEventType = 'decision.completed' | 'decision.refused' | 'stream.closed';
export type WebhookStreamCloseReason = 'client_close' | 'shutdown' | 'max_lifetime' | 'idle' | 'maintenance' |
  'lease_lost' | 'slow_reader' | 'overload' | 'auth' | 'policy' | 'protocol' | 'store_unavailable';

export interface WebhookEndpoint {
  id: string;
  object: 'webhook_endpoint';
  url: string;
  events: WebhookEventType[];
  created: number;
}
export interface WebhookEndpointWithSecret extends WebhookEndpoint {
  /** Returned only by this create or rotation. Store it in a server secret manager. */
  secret: string;
}
export interface WebhookCreateRequest { url: string; events: readonly WebhookEventType[] }
export interface WebhookList<T> { object: 'list'; data: T[] }
export interface WebhookDeleted { id: string; deleted: true }
export interface WebhookDelivery {
  event_id: string;
  type: WebhookEventType;
  attempt: number;
  status_code: number | null;
  latency_ms: number;
  outcome: 'delivered' | 'http_error' | 'blocked' | 'timeout' | 'network_error' | 'dead_letter';
  created: number;
}
export interface WebhookUsageUnits {
  input_tokens: number;
  cached_input_tokens: number;
  media_tokens: number;
  cached_media_tokens: number;
  long_input_tokens: number;
  media_items: number;
  thought_tokens: number;
}
export interface WebhookInputClass {
  name: 'text' | 'document' | 'image' | 'audio' | 'video';
  status: 'served' | 'held';
}
export interface WebhookDecisionData {
  decision_id: string;
  request_id: string;
  created: number;
  /** Validated served version on success, offered alias/version before execution, or 'unknown'. */
  model: string;
  route: 'direct' | 'long' | null;
  classes: WebhookInputClass[];
  status: number;
  refusal_code: string | null;
  usage_units: WebhookUsageUnits;
}
export interface WebhookStreamClosedData {
  stream_id: string;
  close_code: number;
  reason: WebhookStreamCloseReason;
}
interface WebhookEventFields { id: string; object: 'webhook_event'; created: number }
export type WebhookEvent = WebhookEventFields & (
  { type: 'decision.completed' | 'decision.refused'; data: WebhookDecisionData } |
  { type: 'stream.closed'; data: WebhookStreamClosedData }
);

export interface WebhookSignatureOptions {
  /** Integer Unix seconds; omitted uses the receiver's wall clock. */
  now?: number;
}

/** Verify the original bytes before parsing JSON; deduplicate event ids separately. */
export function verifyWebhookSignature(
  rawBody: string | Uint8Array, signatureHeader: string, secret: string,
  options: WebhookSignatureOptions = {},
): boolean {
  if (typeof secret !== 'string' || secret.length !== 70 || !/^whsec_[a-f0-9]{64}$/u.test(secret)) return false;
  if (typeof signatureHeader !== 'string' || signatureHeader.length > 86) return false;
  const match = /^t=(0|[1-9][0-9]{0,15}),v1=([a-f0-9]{64})$/u.exec(signatureHeader);
  // JavaScript's end anchor can match before a final newline.
  if (!match || match[0] !== signatureHeader) return false;
  const timestamp = Number(match[1]);
  const now = options.now ?? Math.floor(Date.now() / 1000);
  if (!Number.isSafeInteger(timestamp) || !Number.isSafeInteger(now) || now < 0 ||
      Math.abs(now - timestamp) > WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS) return false;

  let body: Buffer;
  if (typeof rawBody === 'string') {
    if (rawBody.length > WEBHOOK_MAX_BODY_BYTES) return false;
    body = Buffer.from(rawBody, 'utf8');
    // Do not silently replace unpaired UTF-16 surrogates with different bytes.
    if (body.toString('utf8') !== rawBody) return false;
  } else if (rawBody instanceof Uint8Array) {
    if (rawBody.byteLength > WEBHOOK_MAX_BODY_BYTES) return false;
    body = Buffer.from(rawBody);
  } else return false;
  if (body.byteLength > WEBHOOK_MAX_BODY_BYTES) return false;
  const expected = createHmac('sha256', Buffer.from(secret.slice(6), 'hex'))
    .update(`${match[1]}.`, 'utf8').update(body).digest();
  return timingSafeEqual(expected, Buffer.from(match[2]!, 'hex'));
}

export type WebhookRequestOptions = Omit<RequestOptions, 'idempotencyKey'>;
export interface WebhookDeliveryRequestOptions extends WebhookRequestOptions { limit?: number }
export type WebhookMethod = 'GET' | 'POST' | 'DELETE';
export interface WebhookRequestBehavior { idempotency: false; maxRetries?: number }
 
export interface WebhookRequest {
  <T>(method: WebhookMethod, path: string, body: string | undefined, options: WebhookRequestOptions,
    behavior: WebhookRequestBehavior): APIPromise<T>;
}

function invalid(message: string, param: string): WebhookInvalidError {
  return new WebhookInvalidError(message, { ...WEBHOOK_ERROR_CODES.webhook_invalid, code: 'webhook_invalid', param });
}

function endpointPath(id: string, wire: Wire): string {
  if (typeof id !== 'string' || id.length !== 35 || !/^wh_[a-f0-9]{32}$/u.test(id)) {
    throw invalid('The webhook endpoint ID is invalid.', 'id');
  }
  return `${wire.webhooksPath}/${id}`;
}

/** Authenticated endpoint management; DNS admission remains the gateway's job. */
export class Webhooks {
  readonly #request: WebhookRequest;
  constructor(request: WebhookRequest, private readonly wire: Wire = WIRES['rightnow-decision']) { this.#request = request; }

  /** A lost response may contain the only copy of the returned signing secret. */
  create(request: WebhookCreateRequest, options: WebhookRequestOptions = {}): APIPromise<WebhookEndpointWithSecret> {
    return this.#send('POST', this.wire.webhooksPath, JSON.stringify(request), options, { idempotency: false, maxRetries: 0 });
  }

  list(options: WebhookRequestOptions = {}): APIPromise<WebhookList<WebhookEndpoint>> {
    return this.#send('GET', this.wire.webhooksPath, undefined, options);
  }

  delete(id: string, options: WebhookRequestOptions = {}): APIPromise<WebhookDeleted> {
    return this.#send('DELETE', endpointPath(id, this.wire), undefined, options);
  }

  /** A deliberate retry creates another secret; automatic retries are disabled. */
  rotateSecret(id: string, options: WebhookRequestOptions = {}): APIPromise<WebhookEndpointWithSecret> {
    return this.#send('POST', `${endpointPath(id, this.wire)}/rotate-secret`, '{}', options, { idempotency: false, maxRetries: 0 });
  }

  deliveries(id: string, options: WebhookDeliveryRequestOptions = {}): APIPromise<WebhookList<WebhookDelivery>> {
    const path = `${endpointPath(id, this.wire)}/deliveries`;
    const { limit } = options;
    if (limit !== undefined && (!Number.isInteger(limit) || limit < 1 || limit > 100)) {
      throw invalid('Delivery log limit must be an integer from 1 through 100.', 'limit');
    }
    // Retain retry/admission context, which is keyed by this options object.
    return this.#send('GET', limit === undefined ? path : `${path}?limit=${limit}`, undefined, options);
  }

  #send<T>(method: WebhookMethod, path: string, body: string | undefined, options: WebhookRequestOptions,
    behavior: WebhookRequestBehavior = { idempotency: false }): APIPromise<T> {
    if ((options as RequestOptions).idempotencyKey !== undefined || new Headers(options.headers).has('idempotency-key')) {
      throw invalid('Webhook management does not accept an Idempotency-Key.', 'headers');
    }
    return new APIPromise(this.#request<T>(method, path, body, options, behavior).withResponse()
      .catch((error: unknown) => { throw mapWebhookError(error); }));
  }
}
