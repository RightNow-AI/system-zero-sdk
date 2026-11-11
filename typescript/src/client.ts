import { redact } from './redaction.js';
import { randomUUID } from 'node:crypto';
import { isIP } from 'node:net';
import { promisify } from 'node:util';
import { gzip } from 'node:zlib';
import type { DecideRequest, DecideReply, ModelList } from './api-types.js';
import type { EnabledDecideRequest, VideoRequest } from './enabled-types.js';
import { validateRequestPolicy, type Policy, type PolicyReply } from './policy.js';
import { createDecisionRecords, type DecisionRecords } from './decision-records.js';
import { APIConnectionError, APIError, APITimeoutError, InvalidRequestError, LongContextNotAvailableError, responseError, SystemZeroError } from './errors.js';
import { retryWaitMs, shouldRecoverWorkflow, shouldRetry } from './retry.js';
import { hasResponseStarted, nodeFetch, requestSent } from './transport.js';
import { Webhooks } from './webhooks.js';

export const VERSION = '0.4.7';
export const DEFAULT_BASE_URL = 'https://api.rightnowai.co/v1';
export type Fetch = typeof globalThis.fetch;

export interface ClientOptions {
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
  for (const key of [client && clientSecrets.get(client), process.env.SYSTEM_ZERO_API_KEY]) {
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

function metadata(headers: Headers, idempotencyKey: string | null): ResponseMetadata {
  return {
    idempotencyKey, requestId: headers.get('x-request-id'), clientRequestId: headers.get('x-client-request-id'),
    rateLimit: {
      limit: headerNumber(headers, 'x-ratelimit-limit', 'x-ratelimit-limit-requests'),
      remaining: headerNumber(headers, 'x-ratelimit-remaining', 'x-ratelimit-remaining-requests'),
      reset: headerNumber(headers, 'x-ratelimit-reset', 'x-ratelimit-reset-requests'),
    },
    latencyMs: headerNumber(headers, 'x-latency-ms'),
    replayed: headers.get('x-rightnow-idempotent-replay') === 'true',
    dangerZone: (headers.get('sz-danger-zone') ?? '').split(',').map(value => value.trim()).filter(Boolean),
  };
}

interface AttemptResult { response: Response; body: unknown; json: boolean }

export class SystemZeroClient {
  readonly options: Readonly<{ baseURL: string; timeout: number; maxRetries: number; maxRetryWait: number; retryBudget: number }>;
  readonly getDecision: DecisionRecords['getDecision'];
  readonly getDecisionTrace: DecisionRecords['getDecisionTrace'];
  readonly models: { list: (options?: RequestOptions) => APIPromise<ModelList> };
  readonly webhooks: Webhooks;
  #apiKey: string;
  #fetch: Fetch;
  #compressRequests: boolean;
  #now: () => number;
  #monotonic: () => number;
  #random: () => number;
  #sleep: (milliseconds: number) => Promise<void>;

  constructor(options: ClientOptions = {}) {
    const apiKey = options.apiKey ?? process.env.SYSTEM_ZERO_API_KEY;
    if (!apiKey) throw new Error('Supply an apiKey or set SYSTEM_ZERO_API_KEY in the server environment.');
    if (typeof apiKey !== 'string' || !/^[\x21-\x7e]+$/u.test(apiKey)) throw new TypeError('apiKey must contain only visible ASCII characters.');
    const baseURL = checkedBaseURL(options.baseURL ?? DEFAULT_BASE_URL);
    this.#apiKey = apiKey;
    clientSecrets.set(this, apiKey);
    const maxRetries = options.maxRetries ?? 5;
    if (!(Number.isSafeInteger(maxRetries) && maxRetries >= 0)) throw new RangeError('maxRetries must be a nonnegative safe integer.');
    this.options = Object.freeze({
      baseURL,
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
    this.models = Object.freeze({ list: (requestOptions: RequestOptions = {}) => this.#request<ModelList>('GET', '/models', undefined, requestOptions) });
    this.webhooks = new Webhooks((method, path, body, requestOptions, behavior) =>
      this.#request(method, path, body, requestOptions, behavior));
    const records = createDecisionRecords(<T>(method: 'GET', path: string,
      body: undefined, requestOptions: RequestOptions) =>
      this.#request<T>(method, path, body, requestOptions));
    this.getDecision = records.getDecision;
    this.getDecisionTrace = records.getDecisionTrace;
  }

  decide(request: DecideRequest | EnabledDecideRequest | VideoRequest, options: RequestOptions = {}): APIPromise<PolicyReply> {
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
    return this.#request<PolicyReply>('POST', '/system-zero/decide', JSON.stringify(request), options);
  }

  #request<T>(method: 'POST' | 'GET' | 'DELETE', path: string, body: string | undefined, options: RequestOptions, behavior: { idempotency?: boolean; maxRetries?: number } = {}): APIPromise<T> {
    const baseURL = checkedBaseURL(this.options.baseURL);
    const retryBudget = options.retryBudget ?? this.options.retryBudget;
    this.#checkRetryBudget(retryBudget);
    const context = retryContexts.get(options);
    const maxRetries = Math.min(behavior.maxRetries ?? this.options.maxRetries, context?.maxRetries ?? this.options.maxRetries);
    const headers = requestHeaders(options.headers);
    if (headers.has('SZ-Danger-Zone') || (options.dangerZone !== undefined && !['accept', 'long_context_64k', 'long_context_128k', 'long_context_1m'].includes(options.dangerZone))) {
      throw new InvalidRequestError('Use dangerZone with accept, long_context_64k, long_context_128k or long_context_1m; do not supply its header.', {
        status: 400, type: 'invalid_request_error', code: 'invalid_request', param: 'SZ-Danger-Zone',
      });
    }
    if (options.dangerZone !== undefined && method === 'POST' && path === '/system-zero/decide') {
      if (options.dangerZone !== 'accept' && body !== undefined) {
        const attachments: unknown = JSON.parse(body).attachments;
        if (attachments != null && (Array.isArray(attachments) ? attachments.length > 0 : options.dangerZone === 'long_context_1m')) longContextUnavailable('attachments');
      }
      headers.set('SZ-Danger-Zone', options.dangerZone);
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
      headers.set('user-agent', `system-zero-typescript/${VERSION} node/${process.versions.node}`);
      headers.set('accept', 'application/json');
      if (body !== undefined) headers.set('content-type', 'application/json');
      if (options.clientRequestId !== undefined) headers.set('x-client-request-id', options.clientRequestId);
      if (options.idempotencyKey !== undefined) headers.set('idempotency-key', options.idempotencyKey);
      const blankKey = headers.has('idempotency-key') && !headers.get('idempotency-key')!.trim();
      if (blankKey) headers.delete('idempotency-key');
      if (headers.has('x-client-request-id') && !headers.get('x-client-request-id')!.trim()) headers.delete('x-client-request-id');
      if (method === 'POST' && behavior.idempotency !== false && !blankKey && (oneMillion || maxRetries > 0 && retryBudget > 0) && !headers.has('idempotency-key')) headers.set('idempotency-key', `sz-sdk-${randomUUID()}`);
    } catch { throw new TypeError('Request headers are invalid.'); }
    const init: RequestInit = { method, headers, redirect: 'error' };
    if (body !== undefined) init.body = body;
    const prepare = this.#compressRequests && method === 'POST' && path === '/system-zero/decide' && body !== undefined && Buffer.byteLength(body, 'utf8') >= 32_768
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
    return new APIPromise(this.#send<T>(`${baseURL}${path}`, init, maxRetries, retryBudget, context, prepare).catch((error: unknown) => {
      if (error instanceof SystemZeroError) error.idempotencyKey = headers.get('idempotency-key');
      throw redact(error, this.#apiKey);
    }));
  }

  #checkRetryBudget(retryBudget: number): void {
    if (!(retryBudget >= 0 && retryBudget <= 2_147_483_647 && Number.isFinite(retryBudget))) throw new RangeError('retryBudget must be between 0 and 2147483647 milliseconds.');
  }

  async #send<T>(url: string, init: RequestInit, maxRetries: number, retryBudget: number, context?: RetryContext, prepare?: (signal: AbortSignal) => Promise<void>): Promise<ResponseResult<T>> {
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
      if (!safeToRetry || attempt >= maxRetries || retryBudget === 0) throw error;
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
        throw previous ?? new APITimeoutError('RightNow request timed out.', { code: 'system_zero_timeout' });
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
        if (context?.workflowRecovery || !(error instanceof APIConnectionError) || !error.retryable) throw error;
        await waitToRetry(error, new Headers(), attempt);
        continue;
      }
      const { response, body, json } = result;
      if (response.ok) {
        if (!json) throw new APIError('RightNow returned invalid JSON.', { status: response.status, type: 'api_error', headers: response.headers, request_id: response.headers.get('x-request-id') });
        return { data: body as T, response, metadata: redact(metadata(response.headers, new Headers(init.headers).get('idempotency-key')), this.#apiKey) };
      }
      let error = responseError(response.status, response.headers, body, false);
      const retryable = body !== null && typeof body === 'object' && 'error' in body && body.error !== null && typeof body.error === 'object' && 'retryable' in body.error && typeof body.error.retryable === 'boolean' ? body.error.retryable : undefined;
      const coldPool = new Headers(init.headers).get('sz-danger-zone') === 'long_context_1m' && response.status === 503 && error.code === 'service_unavailable';
      const retry = error.type !== 'authentication_error' && error.type !== 'permission_error' && (coldPool || (context?.workflowRecovery
        ? shouldRecoverWorkflow(response.status, error.code, response.headers, retryable, error.details)
        : shouldRetry(response.status, error.code, response.headers, retryable, this.#now())));
      if (retryable === undefined && retry) error = responseError(response.status, response.headers, body, true);
      if (externalRemaining() <= 0) { context?.onDeadline?.(); throw error; }
      if (!retry) throw error;
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
      retryable: !preparing && safeToRetry && response === undefined && !hasResponseStarted(controller.signal),
      status: response?.status ?? null,
      request_id: response?.headers.get('x-request-id') ?? null,
      request_sent: preparing ? false : response ? true : requestSent(controller.signal),
      ...(response ? { headers: response.headers } : {}),
    });
    const deadline = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        reject(new APIConnectionError('RightNow request timed out.', { ...fields(), type: 'timeout_error' }));
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
