import { DANGER_ZONE_HEADERS, DEFAULT_BASE_URL, setWire, wireFor, type WireName } from './wire.js';
export { DEFAULT_BASE_URL, VERSION } from './wire.js';
import { redact } from './redaction.js';
import { randomUUID } from 'node:crypto';
import { isIP } from 'node:net';
import { promisify } from 'node:util';
import { gzip } from 'node:zlib';
import type { DecideRequest, DecideReply, ModelList } from './api-types.js';
import type { EnabledDecideRequest, VideoRequest } from './enabled-types.js';
import { validateRequestPolicy, type Policy, type PolicyReply } from './policy.js';
import { createDecisionRecords, type DecisionRecords } from './decision-records.js';
import { APIConnectionError, APIError, APITimeoutError, InvalidRequestError, KeyNotSupportedError, LongContextNotAvailableError, responseError, SystemZeroError } from './errors.js';
import { idempotencyMode, type Idempotency, retryWaitMs, shouldRecoverWorkflow, shouldRetry } from './retry.js';
import { hasResponseStarted, nodeFetch, requestSent } from './transport.js';
import { Webhooks } from './webhooks.js';

export type Fetch = typeof globalThis.fetch;

export interface ClientOptions {
  /** Generate replay keys only with auto; off by default. */
  idempotency?: Idempotency;
  apiKey?: string;
  baseURL?: string;
  /** Milliseconds per attempt, including the entire response body. */
  timeout?: number;
  maxRetries?: number;
  /** Milliseconds; longer server waits raise the original HTTP error. */
  maxRetryWait?: number;
  /** Milliseconds from the first attempt; zero disables automatic retries. */
  retryBudget?: number;
  /** Compress large HTTP decide bodies when gzip saves at least 10%. */
  compressRequests?: boolean;
  /** Receives RequestInit.body as JSON text or a Buffer (binary BodyInit) for compressed requests. */
  fetch?: Fetch;
  /** Clock, randomness and sleep injection keep retry tests deterministic. */
  now?: () => number;
  monotonic?: () => number;
  random?: () => number;
  sleep?: (milliseconds: number) => Promise<void>;
}

export interface RequestOptions {
  /** Generate replay keys only with auto; off by default. */
  idempotency?: Idempotency;
  /** Select the state token class explicitly. Native long classes accept no attachments. */
  dangerZone?: 'accept' | 'long_context_64k' | 'long_context_128k' | 'long_context_1m';
  idempotencyKey?: string;
  clientRequestId?: string;
  /** Extra headers, including x-mock-scenario in local tests. */
  headers?: HeadersInit;
  /** Override the client's retry budget for this call, in milliseconds. */
  retryBudget?: number;
}

interface RetryContext {
  workflowRecovery?: boolean;
  maxRetries?: number;
  random?: () => number;
  sleep?: (milliseconds: number) => Promise<void>;
  remaining?: () => number;
  onAttempt?: () => void;
  onDeadline?: () => void;
}
const retryContexts = new WeakMap<RequestOptions, RetryContext>();
const clientSecrets = new WeakMap<object, string>();

/** Redact tool-visible text without exposing a client's credential. */
export function redactClientText(client: object | undefined, text: string): string {
  for (const key of [client && clientSecrets.get(client), process.env.RIGHTNOW_API_KEY, process.env.SYSTEM_ZERO_API_KEY]) {
    if (key) text = text.split(key).join('[redacted]');
  }
  return text.replace(/\brnsk_[A-Za-z0-9_-]{20,}\b/gu, '[redacted]')
    .replace(/\bBearer[ \t]+[A-Za-z0-9._~+/-]+=*/giu, 'Bearer [redacted]');
}

/** Sanitize diagnostics produced after a request, including workflow traces. */
export function redactClientDiagnostics<T>(client: object, value: T): T {
  return redact(value, clientSecrets.get(client));
}

export function checkedBaseURL(value: string): string {
  let url: URL;
  try { url = new URL(value); } catch { throw new TypeError('baseURL must be an HTTPS or loopback HTTP URL.'); }
  const host = url.hostname;
  const local = host === ['local', 'host'].join('') || host === '[' + ':' + ':1]' || (isIP(host) === 4 && host.startsWith('127.'));
  if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && local))
    || url.username || url.password || url.search || url.hash || value.includes('?') || value.includes('#')) {
    throw new TypeError('baseURL must be an HTTPS or loopback HTTP URL without credentials, query or fragment.');
  }
  return url.href.replace(/\/+$/u, '');
}

function longContextUnavailable(param: string): never {
  throw new LongContextNotAvailableError('This request shape is not available for the selected long context class.', {
    status: 404, type: 'not_found_error', code: 'long_context_not_available', param,
  });
}

/**
 * A key that starts with rp_ is served by POST /decisions alone, and only for decide(). That route takes the decision in its
 * own request shape and answers in its own shape; the client writes the one and reads the other into the decide types, so a
 * caller's code is the same for both kinds of key. Everything it does not serve is refused here, before any network call.
 */
const RP_PREFIX = 'rp_';
const RP_DECIDE_PATH = '/decisions';
/** The most questions that route takes in one call. */
const RP_MAX_QUESTIONS = 32;
/** The request members that route serves. prompt_cache_key is sent so one program works with both kinds of key; it is not used there yet. */
const RP_MEMBERS = new Set(['model', 'state', 'questions', 'attachments', 'route', 'mode', 'prompt_cache_key']);
const MILLION = 1_000_000;
const RP_METHOD_MESSAGE = 'A key that starts with rp_ calls decide() only. Streaming, uploads, webhooks, decision lookups and the model list are not available with it yet.';

function plainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function rpRefusal(message: string, param: string | null): never {
  throw new KeyNotSupportedError(message, { status: 400, type: 'invalid_request_error', code: 'key_not_supported', param, retryable: false });
}

function rpMember(name: string): never {
  rpRefusal(`${name} is not available with a key that starts with rp_ yet. Send decide() without it.`, name);
}

/** The decide request in that route's shape: a boolean question is its noul question, and the model ids are its ids. */
function rpBody(request: object, options: RequestOptions): string {
  const value = request as Record<string, unknown>;
  // That route never receives the class header: the call would be answered without it.
  if (options.dangerZone !== undefined) rpMember('dangerZone');
  // An undefined member is never sent (JSON drops it), so every check below reads undefined as absent; any defined value,
  // null included, is checked.
  // It refuses session_id and drops these members unread, so the call would be answered and charged without them.
  for (const name of ['session_id', 'policy']) if (value[name] !== undefined) rpMember(name);
  // Any other member would be answered and charged without it, so it is refused too.
  for (const name of Object.keys(value)) if (!RP_MEMBERS.has(name) && value[name] !== undefined) rpMember(name);
  const attachments = value.attachments;
  if (attachments !== undefined && attachments !== null && !(Array.isArray(attachments) && attachments.length === 0)) rpMember('attachments');
  for (const name of ['route', 'mode']) if (value[name] !== undefined && value[name] !== 'standard') rpMember(name);
  const questions = value.questions;
  if (plainObject(questions) && Object.keys(questions).length > RP_MAX_QUESTIONS) {
    rpRefusal('A key that starts with rp_ takes at most 32 questions in one decide() call. Send the rest in another call.', 'questions');
  }
  const body: Record<string, unknown> = { ...value };
  // That route refuses the attachments member whenever it is present; null or empty means none, so it is not sent.
  delete body.attachments;
  if (typeof value.model === 'string') body.model = value.model.replace(/^rightnow-decision(?=$|-\d+\.\d+\.\d+$)/u, 'system-zero');
  if (plainObject(questions)) {
    body.questions = Object.fromEntries(Object.entries(questions).map(([name, question]) =>
      [name, plainObject(question) && question.type === 'boolean' ? { ...question, type: 'noul' } : question]));
  }
  return JSON.stringify(body);
}

function millionths(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? Math.round(value * MILLION) : null;
}

/** Outcomes by probability, highest first; a tie goes to the lower level of a score and to the lower key bytes otherwise. */
function rankedOutcomes(units: Map<string, number>, score: boolean): string[] {
  return [...units.keys()].sort((a, b) => units.get(b)! - units.get(a)!
    || (score ? Number(a) - Number(b) : Buffer.compare(Buffer.from(a), Buffer.from(b))));
}

/**
 * One answer of that route read as the decide answer. It names no calibration, so calibration_version is "unpublished" and
 * calibrated is false. A refused question names no code, so the refusal's code is decision_refused. Any other shape is kept.
 */
function rpAnswer(answer: unknown): unknown {
  if (!plainObject(answer)) return answer;
  if (answer.type === 'refusal') return { error: { code: 'decision_refused', retryable: false } };
  if (answer.type === 'noul') {
    const units = millionths(answer.noul);
    if (units === null) return answer;
    const yes = units >= MILLION / 2;
    return { type: 'boolean', probability: units / MILLION, confidence: Math.max(units, MILLION - units) / MILLION,
      calibration_version: 'unpublished', decision: yes,
      alternatives: [{ decision: !yes, confidence: Math.min(units, MILLION - units) / MILLION }], calibrated: false };
  }
  if ((answer.type === 'choice' || answer.type === 'score') && plainObject(answer.probabilities)) {
    const units = new Map<string, number>();
    for (const [key, probability] of Object.entries(answer.probabilities)) {
      const amount = millionths(probability);
      if (amount === null) return answer;
      units.set(key, amount);
    }
    const score = answer.type === 'score';
    const order = rankedOutcomes(units, score);
    const selected = score ? order[0] : answer.choice;
    if (typeof selected !== 'string' || !units.has(selected)) return answer;
    const probabilities = Object.fromEntries([...units].map(([key, amount]) => [key, amount / MILLION]));
    const fields = { confidence: units.get(selected)! / MILLION, calibration_version: 'unpublished', decision: selected,
      alternatives: order.filter(key => key !== selected).map(key => ({ decision: key, confidence: units.get(key)! / MILLION })), calibrated: false };
    return score ? { type: 'score', score: answer.score, legend: answer.legend, probabilities, ...fields }
      : { type: 'choice', choice: selected, probabilities, ...fields };
  }
  return answer;
}

function tokenCount(value: unknown): number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

/**
 * That route's answer read as the decide reply. It sends no long input, no media items and no thought tokens: those
 * counts are 0. context.tokens is its input token count, and created is its Date header, or this client's clock without
 * one. usage.cost (USD) and usage.cost_nanocents are the call's charge as it states it.
 */
function rpReply(body: unknown, model: unknown, headers: Headers, now: () => number): PolicyReply {
  if (!plainObject(body) || !plainObject(body.answers) || !plainObject(body.usage)) {
    throw new APIError('RightNow returned invalid JSON.', { status: 200, type: 'api_error', headers, request_id: headers.get('x-request-id') });
  }
  const usage = body.usage;
  const details = plainObject(usage.input_tokens_details) ? usage.input_tokens_details : {};
  // It names the served version by the old ids; a caller who sent the decision model's ids reads those ids back.
  const served = typeof body.model === 'string' && typeof model === 'string' && model.startsWith('rightnow-decision')
    ? body.model.replace(/^system-zero(?=$|-)/u, 'rightnow-decision') : body.model;
  const date = Date.parse(headers.get('date') ?? '');
  return {
    id: body.id, model: served, served_by: served,
    created: Math.floor((Number.isFinite(date) ? date : now()) / 1000),
    context: { route: 'direct', tokens: tokenCount(usage.input_tokens) },
    answers: Object.fromEntries(Object.entries(body.answers).map(([name, answer]) => [name, rpAnswer(answer)])),
    usage: {
      input_tokens: tokenCount(usage.input_tokens), cached_input_tokens: tokenCount(details.cached_tokens),
      media_tokens: tokenCount(usage.media_tokens), long_input_tokens: 0, media_items: 0, thought_tokens: 0,
      ...(typeof usage.cost === 'number' ? { cost: usage.cost } : {}),
      ...(typeof usage.cost_nanocents === 'string' ? { cost_nanocents: usage.cost_nanocents } : {}),
    },
  } as PolicyReply;
}

function requestHeaders(value?: HeadersInit): Headers {
  try { return new Headers(value); }
  catch { throw new TypeError('Request headers are invalid.'); }
}

/** Internal workflow policy and admission hooks for the shared transport. */
export function withRetryContext(options: RequestOptions, context: RetryContext): RequestOptions {
  retryContexts.set(options, context);
  return options;
}

export interface ResponseMetadata {
  requestId: string | null;
  clientRequestId: string | null;
  idempotencyKey: string | null;
  rateLimit: { limit: number | null; remaining: number | null; reset: number | null };
  latencyMs: number | null;
  replayed: boolean;
  dangerZone: string[];
}

export interface ResponseResult<T> {
  data: T;
  /** The original Response remains available with an unread body. */
  response: Response;
  metadata: ResponseMetadata;
}

/** A Promise-compatible result with opt-in response metadata. No reply fields are added. */
export class APIPromise<T> implements Promise<T> {
  readonly [Symbol.toStringTag] = 'Promise';
  constructor(private readonly result: Promise<ResponseResult<T>>) {}

  then<TResult1 = T, TResult2 = never>(onfulfilled?: ((value: T) => TResult1 | PromiseLike<TResult1>) | null, onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null): Promise<TResult1 | TResult2> {
    return this.result.then(value => onfulfilled ? onfulfilled(value.data) : value.data as unknown as TResult1, onrejected);
  }

  catch<TResult = never>(onrejected?: ((reason: unknown) => TResult | PromiseLike<TResult>) | null): Promise<T | TResult> {
    return this.then(undefined, onrejected);
  }

  finally(onfinally?: (() => void) | null): Promise<T> {
    return this.result.finally(onfinally).then(value => value.data);
  }

  withResponse(): Promise<ResponseResult<T>> { return this.result; }
}

function headerNumber(headers: Headers, primary: string, alias?: string): number | null {
  for (const name of alias ? [primary, alias] : [primary]) {
    const raw = headers.get(name);
    if (raw === null || raw.trim() === '') continue;
    const number = Number(raw);
    if (Number.isFinite(number) && number >= 0 && (!alias || Number.isInteger(number))) return number;
  }
  return null;
}

function metadata(headers: Headers, idempotencyKey: string | null, client: object): ResponseMetadata {
  return {
    idempotencyKey, requestId: headers.get('x-request-id'), clientRequestId: headers.get('x-client-request-id'),
    rateLimit: {
      limit: headerNumber(headers, 'x-ratelimit-limit', 'x-ratelimit-limit-requests'),
      remaining: headerNumber(headers, 'x-ratelimit-remaining', 'x-ratelimit-remaining-requests'),
      reset: headerNumber(headers, 'x-ratelimit-reset', 'x-ratelimit-reset-requests'),
    },
    latencyMs: headerNumber(headers, 'x-latency-ms'),
    replayed: headers.get('x-rightnow-idempotent-replay') === 'true',
    // The wire's name first; the old name too, which a 1M answer keeps on every route.
    dangerZone: (headers.get(wireFor(client).dangerZoneResponseHeader) ?? headers.get('sz-danger-zone') ?? '').split(',').map(value => value.trim()).filter(Boolean),
  };
}

interface AttemptResult { response: Response; body: unknown; json: boolean }

export class RightNowClient {
  readonly options: Readonly<{ baseURL: string; timeout: number; idempotency: Idempotency; maxRetries: number; maxRetryWait: number; retryBudget: number }>;
  readonly getDecision: DecisionRecords['getDecision'];
  readonly getDecisionTrace: DecisionRecords['getDecisionTrace'];
  readonly models: { list: (options?: RequestOptions) => APIPromise<ModelList> };
  readonly webhooks: Webhooks;
  #apiKey: string;
  /** The key starts with rp_: decide() goes to POST /decisions and every other method is refused. */
  #rpOnly: boolean;
  #fetch: Fetch;
  #compressRequests: boolean;
  #now: () => number;
  #monotonic: () => number;
  #random: () => number;
  #sleep: (milliseconds: number) => Promise<void>;

  constructor(options: ClientOptions = {}, wire: WireName = 'rightnow-decision') {
    setWire(this, wire);
    const apiKey = options.apiKey ?? process.env.RIGHTNOW_API_KEY ?? process.env.SYSTEM_ZERO_API_KEY;
    if (!apiKey) throw new Error('Supply an apiKey or set RIGHTNOW_API_KEY in the server environment.');
    if (typeof apiKey !== 'string' || !/^[\x21-\x7e]+$/u.test(apiKey)) throw new TypeError('apiKey must contain only visible ASCII characters.');
    const baseURL = checkedBaseURL(options.baseURL ?? DEFAULT_BASE_URL);
    this.#apiKey = apiKey;
    this.#rpOnly = apiKey.startsWith(RP_PREFIX);
    clientSecrets.set(this, apiKey);
    const maxRetries = options.maxRetries ?? 5;
    if (!(Number.isSafeInteger(maxRetries) && maxRetries >= 0)) throw new RangeError('maxRetries must be a nonnegative safe integer.');
    this.options = Object.freeze({
      baseURL, idempotency: idempotencyMode(options.idempotency === undefined ? 'off' : options.idempotency),
      timeout: options.timeout ?? 150_000, maxRetries: Math.min(maxRetries, 5),
      maxRetryWait: options.maxRetryWait ?? 60_000,
      retryBudget: options.retryBudget ?? 60_000,
    });
    // These are SDK settings, not API request checks. They must bound every retry loop.
    if (!(this.options.timeout >= 1 && this.options.timeout <= 2_147_483_647 && Number.isFinite(this.options.timeout))) throw new RangeError('timeout must be between 1 and 2147483647 milliseconds.');
    if (!(this.options.maxRetryWait >= 0 && this.options.maxRetryWait <= 2_147_483_647 && Number.isFinite(this.options.maxRetryWait))) throw new RangeError('maxRetryWait must be between 0 and 2147483647 milliseconds.');
    this.#checkRetryBudget(this.options.retryBudget);
    this.#fetch = options.fetch ?? nodeFetch;
    this.#compressRequests = options.compressRequests ?? true;
    this.#now = options.now ?? Date.now;
    this.#monotonic = options.monotonic ?? (() => performance.now());
    this.#random = options.random ?? Math.random;
    this.#sleep = options.sleep ?? (milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)));
    this.models = Object.freeze({ list: (requestOptions: RequestOptions = {}) => this.#request<ModelList>('GET', wireFor(this).paths.models, undefined, requestOptions) });
    this.webhooks = new Webhooks((method, path, body, requestOptions, behavior) =>
      this.#request(method, path, body, requestOptions, behavior), wireFor(this));
    const records = createDecisionRecords(<T>(method: 'GET', path: string,
      body: undefined, requestOptions: RequestOptions) =>
      this.#request<T>(method, path, body, requestOptions), wireFor(this));
    this.getDecision = records.getDecision;
    this.getDecisionTrace = records.getDecisionTrace;
  }

  decide(request: DecideRequest | EnabledDecideRequest | VideoRequest, options: RequestOptions = {}): APIPromise<PolicyReply> {
    if (this.#rpOnly) return this.#rpDecide(request, options);
    if (options.dangerZone === 'long_context_1m') {
      if (typeof request.state !== 'string') longContextUnavailable('state');
      if (!request.questions || Object.keys(request.questions).length !== 1) throw responseError(413, new Headers(), {
        error: { code: 'questions_too_large', type: 'invalid_request_error', message: 'The 1M class requires exactly one question.', param: 'questions' },
      }, false);
      for (const name of ['policy', 'mode', 'route', 'session_id', 'prompt_cache_key']) {
        if (Object.hasOwn(request, name)) longContextUnavailable(name);
      }
    }
    validateRequestPolicy(request);
    return this.#request<PolicyReply>('POST', wireFor(this).paths.decide, JSON.stringify(request), options);
  }

  #request<T>(method: 'POST' | 'GET' | 'DELETE', path: string, body: string | undefined, options: RequestOptions, behavior: { idempotency?: boolean; maxRetries?: number } = {}): APIPromise<T> {
    if (this.#rpOnly && !(method === 'POST' && path === RP_DECIDE_PATH)) rpRefusal(RP_METHOD_MESSAGE, null);
    const baseURL = checkedBaseURL(this.options.baseURL);
    const retryBudget = options.retryBudget ?? this.options.retryBudget;
    this.#checkRetryBudget(retryBudget);
    const context = retryContexts.get(options);
    const maxRetries = Math.min(behavior.maxRetries ?? this.options.maxRetries, context?.maxRetries ?? this.options.maxRetries);
    const idempotency = idempotencyMode(options.idempotency === undefined ? this.options.idempotency : options.idempotency);
    const headers = requestHeaders(options.headers);
    if (DANGER_ZONE_HEADERS.some(name => headers.has(name)) || (options.dangerZone !== undefined && !['accept', 'long_context_64k', 'long_context_128k', 'long_context_1m'].includes(options.dangerZone))) {
      throw new InvalidRequestError('Use dangerZone with accept, long_context_64k, long_context_128k or long_context_1m; do not supply its header.', {
        status: 400, type: 'invalid_request_error', code: 'invalid_request', param: wireFor(this).dangerZoneHeader,
      });
    }
    if (options.dangerZone !== undefined && method === 'POST' && path === wireFor(this).paths.decide) {
      if (options.dangerZone !== 'accept' && body !== undefined) {
        const attachments: unknown = JSON.parse(body).attachments;
        if (attachments != null && (Array.isArray(attachments) ? attachments.length > 0 : options.dangerZone === 'long_context_1m')) longContextUnavailable('attachments');
      }
      headers.set(wireFor(this).dangerZoneHeader, options.dangerZone);
    }
    const oneMillion = options.dangerZone === 'long_context_1m';
    if (oneMillion) {
      const key = options.idempotencyKey ?? headers.get('idempotency-key');
      if (key !== null && !key.trim()) throw new InvalidRequestError('The 1M class requires a nonblank Idempotency-Key.', {
        status: 400, type: 'invalid_request_error', code: 'invalid_request', param: 'Idempotency-Key',
      });
      if (body !== undefined && Buffer.byteLength(body, 'utf8') > 8_388_608) throw responseError(413, new Headers(), {
        error: { code: 'payload_too_large', type: 'invalid_request_error', message: 'The request body exceeds 8,388,608 bytes.', param: 'body' },
      }, false);
    }
    if (behavior.idempotency === false && (options.idempotencyKey !== undefined || headers.has('idempotency-key'))) throw new TypeError('Upload routes do not accept an Idempotency-Key.');
    try {
      headers.set('authorization', `Bearer ${this.#apiKey}`);
      headers.set('user-agent', wireFor(this).userAgent);
      headers.set('accept', 'application/json');
      if (body !== undefined) headers.set('content-type', 'application/json');
      if (options.clientRequestId !== undefined) headers.set('x-client-request-id', options.clientRequestId);
      if (options.idempotencyKey !== undefined) headers.set('idempotency-key', options.idempotencyKey);
      const blankKey = headers.has('idempotency-key') && !headers.get('idempotency-key')!.trim();
      if (blankKey) headers.delete('idempotency-key');
      if (headers.has('x-client-request-id') && !headers.get('x-client-request-id')!.trim()) headers.delete('x-client-request-id');
      if (method === 'POST' && behavior.idempotency !== false && !blankKey && (oneMillion || idempotency === 'auto' && maxRetries > 0 && retryBudget > 0) && !headers.has('idempotency-key')) headers.set('idempotency-key', `${wireFor(this).idempotencyPrefix}${randomUUID()}`);
    } catch { throw new TypeError('Request headers are invalid.'); }
    const init: RequestInit = { method, headers, redirect: 'error' };
    if (body !== undefined) init.body = body;
    const prepare = this.#compressRequests && method === 'POST' && path === wireFor(this).paths.decide && body !== undefined && Buffer.byteLength(body, 'utf8') >= 32_768
      ? async (signal: AbortSignal) => {
      // Only this route is opted in. Stream starts and unrelated POSTs keep their wire format.
        const bytes = Buffer.from(body, 'utf8');
        const compressed = await promisify(gzip)(bytes, { level: 4 });
        signal.throwIfAborted();
        if (compressed.byteLength * 10 <= bytes.byteLength * 9) {
          init.body = compressed as BodyInit;
          headers.set('content-encoding', 'gzip');
          headers.set('content-length', String(compressed.byteLength));
        }
      } : undefined;
    return new APIPromise(this.#send<T>(`${baseURL}${path}`, init, maxRetries, retryBudget, method === 'POST' && (path === wireFor(this).paths.decide || path === RP_DECIDE_PATH), context, prepare, this.#rpOnly && method === 'POST' && path === RP_DECIDE_PATH).catch((error: unknown) => {
      if (error instanceof SystemZeroError) error.idempotencyKey = headers.get('idempotency-key');
      throw redact(error, this.#apiKey);
    }));
  }

  /** decide() with a key that starts with rp_: POST /decisions in that route's request shape, read back as the decide reply. */
  #rpDecide(request: object, options: RequestOptions): APIPromise<PolicyReply> {
    const body = rpBody(request, options);
    const model = (request as { model?: unknown }).model;
    return new APIPromise(this.#request<unknown>('POST', '/decisions', body, options).withResponse().then(response => ({
      ...response,
      data: rpReply(response.data, model, response.response.headers, this.#now),
      // That route marks a stored answer it sends again with its own header of the same form.
      metadata: { ...response.metadata, replayed: response.metadata.replayed
        || [...response.response.headers].some(([name, value]) => /^x-[a-z]+-idempotent-replay$/u.test(name) && value === 'true') },
    })));
  }

  #checkRetryBudget(retryBudget: number): void {
    if (!(retryBudget >= 0 && retryBudget <= 2_147_483_647 && Number.isFinite(retryBudget))) throw new RangeError('retryBudget must be between 0 and 2147483647 milliseconds.');
  }

  async #send<T>(url: string, init: RequestInit, maxRetries: number, retryBudget: number, unkeyedDecision: boolean, context?: RetryContext, prepare?: (signal: AbortSignal) => Promise<void>, rpDecide = false): Promise<ResponseResult<T>> {
    const started = this.#monotonic();
    const initialExternalRemaining = context?.remaining?.() ?? Infinity;
    const elapsed = () => Math.max(0, this.#monotonic() - started);
    // Pin the admission lifetime to monotonic time. A wall clock rollback
    // cannot extend either the batch deadline or the original key's lifetime.
    const externalRemaining = () => Math.min(initialExternalRemaining - elapsed(), context?.remaining?.() ?? Infinity);
    let recoveryStarted: number | undefined = context?.workflowRecovery ? started : undefined;
    const remaining = () => Math.min(retryBudget - (recoveryStarted === undefined ? 0 : Math.max(0, this.#monotonic() - recoveryStarted)), externalRemaining());
    const random = context?.random ?? this.#random;
    const sleep = context?.sleep ?? this.#sleep;
    const safeToRetry = init.method === 'GET' || Boolean(new Headers(init.headers).get('idempotency-key')?.trim());
    let previous: SystemZeroError | undefined;
    const waitToRetry = async (error: SystemZeroError, headers: Headers, attempt: number): Promise<void> => {
      if (externalRemaining() <= 0) { context?.onDeadline?.(); throw error; }
      if (attempt >= maxRetries || retryBudget === 0) throw error;
      recoveryStarted ??= this.#monotonic();
      const wait = retryWaitMs(headers, attempt, random, this.#now());
      if (wait > this.options.maxRetryWait) throw error;
      if (wait >= remaining()) {
        if (wait >= externalRemaining()) context?.onDeadline?.();
        throw error;
      }
      previous = error;
      await sleep(wait);
      if (remaining() < 1) {
        if (externalRemaining() <= 0) context?.onDeadline?.();
        throw error;
      }
    };
    for (let attempt = 0; ; attempt++) {
      const timeLeft = remaining();
      const admittedTimeLeft = externalRemaining();
      if (previous && timeLeft < 1 || admittedTimeLeft <= 0) {
        if (admittedTimeLeft <= 0) context?.onDeadline?.();
        throw previous ?? new APITimeoutError('RightNow request timed out.', { code: wireFor(this).codes.timeout });
      }
      // One millisecond is the minimum useful retry timeout on supported runtimes.
      (init.headers as Headers).set('x-sdk-retry-count', String(attempt));
      context?.onAttempt?.();
      let result: AttemptResult;
      // The first request retains its normal timeout, subject to any batch deadline.
      // Only retry attempts use the remaining recovery budget.
      try { result = await this.#attempt(url, init, Math.min(this.options.timeout, admittedTimeLeft, attempt === 0 ? Infinity : timeLeft), attempt === 0 ? prepare : undefined); }
      catch (error) {
        if (externalRemaining() <= 0) { context?.onDeadline?.(); throw error; }
        if (context?.workflowRecovery || !(error instanceof APIConnectionError) || !error.retryable || !safeToRetry && !(unkeyedDecision && error.request_sent === false)) throw error;
        await waitToRetry(error, new Headers(), attempt);
        continue;
      }
      const { response, body, json } = result;
      if (response.ok) {
        if (!json) throw new APIError('RightNow returned invalid JSON.', { status: response.status, type: 'api_error', headers: response.headers, request_id: response.headers.get('x-request-id') });
        return { data: body as T, response, metadata: redact(metadata(response.headers, new Headers(init.headers).get('idempotency-key'), this), this.#apiKey) };
      }
      let error = responseError(response.status, response.headers, body, false);
      const retryable = body !== null && typeof body === 'object' && 'error' in body && body.error !== null && typeof body.error === 'object' && 'retryable' in body.error && typeof body.error.retryable === 'boolean' ? body.error.retryable : undefined;
      const coldPool = new Headers(init.headers).get(wireFor(this).dangerZoneHeader) === 'long_context_1m' && response.status === 503 && error.code === 'service_unavailable';
      const retry = error.type !== 'authentication_error' && error.type !== 'permission_error' && (coldPool || (context?.workflowRecovery
        ? shouldRecoverWorkflow(response.status, error.code, response.headers, retryable, error.details)
        : shouldRetry(response.status, error.code, response.headers, retryable, this.#now())));
      if (externalRemaining() <= 0) { context?.onDeadline?.(); throw error; }
      const refusedBeforeWork = (response.status === 429 || response.status === 503 && retryable === true)
        && retryable !== false && response.headers.get('x-should-retry') !== 'false'
        && error.type !== 'authentication_error' && error.type !== 'permission_error';
      // The route for a key that starts with rp_ marks neither of these with error.retryable. Its 429 refuses before any
      // work, and its 409 idempotency_conflict says the same key is still running, so a keyed call sends the same key
      // again and reads the first answer; an explicit false still vetoes.
      const rpSameKey = rpDecide && !context?.workflowRecovery && retryable !== false
        && response.headers.get('x-should-retry') !== 'false'
        && error.type !== 'authentication_error' && error.type !== 'permission_error'
        && (response.status === 429 || response.status === 409 && error.code === 'idempotency_conflict');
      const eligible = safeToRetry ? retry || rpSameKey : unkeyedDecision && !context?.workflowRecovery && refusedBeforeWork;
      if (retryable === undefined && eligible) error = responseError(response.status, response.headers, body, true);
      if (!eligible) throw error;
      await waitToRetry(error, response.headers, attempt);
    }
  }

  async #attempt(url: string, init: RequestInit, timeout: number, prepare?: (signal: AbortSignal) => Promise<void>): Promise<AttemptResult> {
    const controller = new AbortController();
    const safeToRetry = init.method === 'GET' || Boolean(new Headers(init.headers).get('idempotency-key')?.trim());
    let response: Response | undefined;
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let preparing = prepare !== undefined;
    // Keyed decisions and safe GETs retain their before-response retry policy.
    // request_sent is diagnostic; sending alone does not end keyed eligibility.
    const fields = () => ({
      retryable: !preparing && (safeToRetry || requestSent(controller.signal) === false) && response === undefined && !hasResponseStarted(controller.signal),
      status: response?.status ?? null,
      request_id: response?.headers.get('x-request-id') ?? null,
      request_sent: preparing ? false : response ? true : requestSent(controller.signal),
      ...(response ? { headers: response.headers } : {}),
    });
    const deadline = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        reject(new APIConnectionError('RightNow request timed out.', { ...fields(), type: 'timeout_error', code: wireFor(this).codes.timeout }));
        controller.abort();
        void reader?.cancel().catch(() => {});
        void response?.body?.cancel().catch(() => {});
      }, timeout);
    });
    const operation = (async (): Promise<AttemptResult> => {
      try {
        if (prepare) {
          await new Promise<void>((resolve, reject) => {
            const abort = () => reject(controller.signal.reason);
            controller.signal.addEventListener('abort', abort, { once: true });
            void prepare(controller.signal).then(resolve, reject).finally(() => controller.signal.removeEventListener('abort', abort));
            if (controller.signal.aborted) abort();
          });
          controller.signal.throwIfAborted();
          preparing = false;
        }
        response = await this.#fetch(url, { ...init, headers: new Headers(init.headers), signal: controller.signal });
        const copy = response.clone();
        reader = copy.body?.getReader();
        const decoder = new TextDecoder();
        let text = '';
        if (reader) {
          for (;;) {
            const chunk = await reader.read();
            if (chunk.done) break;
            text += decoder.decode(chunk.value, { stream: true });
          }
          text += decoder.decode();
        }
        try { return { response, body: JSON.parse(text) as unknown, json: true }; }
        catch { return { response, body: null, json: false }; }
      } catch (cause) {
        if (cause instanceof SystemZeroError && !(cause instanceof APIConnectionError)) throw cause;
        // Typed transport failures already carry the base retry decision.
        const transportFields = cause instanceof APIConnectionError ? {
          ...cause, request_sent: fields().request_sent === true ? true : cause.request_sent,
        } : fields();
        throw new APIConnectionError('RightNow connection failed.', { ...transportFields, cause });
      } finally {
        reader?.releaseLock();
      }
    })();
    try { return await Promise.race([operation, deadline]); }
    finally { clearTimeout(timer); }
  }
}

/** @deprecated Use RightNowClient. */
export { RightNowClient as SystemZeroClient };
