import { randomUUID } from 'node:crypto';
import { isIP } from 'node:net';
import type { DecideRequest, DecideReply, ModelList } from './api-types.js';
import { APIConnectionError, APIError, APITimeoutError, responseError, SystemZeroError } from './errors.js';
import { retryWaitMs, shouldRecoverWorkflow, shouldRetry } from './retry.js';
import { hasResponseStarted, nodeFetch, requestSent } from './transport.js';

export const VERSION = '0.3.0';
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
  fetch?: Fetch;
  /** Clock, randomness and sleep injection keep retry tests deterministic. */
  now?: () => number;
  monotonic?: () => number;
  random?: () => number;
  sleep?: (milliseconds: number) => Promise<void>;
}

export interface RequestOptions {
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
  };
}

interface AttemptResult { response: Response; body: unknown; json: boolean }

export class SystemZeroClient {
  readonly options: Readonly<{ baseURL: string; timeout: number; maxRetries: number; maxRetryWait: number; retryBudget: number }>;
  readonly models: { list: (options?: RequestOptions) => APIPromise<ModelList> };
  #apiKey: string;
  #fetch: Fetch;
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
    this.#now = options.now ?? Date.now;
    this.#monotonic = options.monotonic ?? (() => performance.now());
    this.#random = options.random ?? Math.random;
    this.#sleep = options.sleep ?? (milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)));
    this.models = Object.freeze({ list: (requestOptions: RequestOptions = {}) => this.#request<ModelList>('GET', '/models', undefined, requestOptions) });
  }

  decide(request: DecideRequest, options: RequestOptions = {}): APIPromise<DecideReply> {
    return this.#request<DecideReply>('POST', '/system-zero/decide', JSON.stringify(request), options);
  }

  #request<T>(method: 'POST' | 'GET', path: string, body: string | undefined, options: RequestOptions): APIPromise<T> {
    const baseURL = checkedBaseURL(this.options.baseURL);
    const retryBudget = options.retryBudget ?? this.options.retryBudget;
    this.#checkRetryBudget(retryBudget);
    const context = retryContexts.get(options);
    const maxRetries = Math.min(this.options.maxRetries, context?.maxRetries ?? this.options.maxRetries);
    const headers = requestHeaders(options.headers);
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
      if (method === 'POST' && !blankKey && maxRetries > 0 && retryBudget > 0 && !headers.has('idempotency-key')) headers.set('idempotency-key', `sz-sdk-${randomUUID()}`);
    } catch { throw new TypeError('Request headers are invalid.'); }
    const init: RequestInit = { method, headers, redirect: 'error' };
    if (body !== undefined) init.body = body;
    return new APIPromise(this.#send<T>(`${baseURL}${path}`, init, maxRetries, retryBudget, context).catch((error: unknown) => {
      if (error instanceof SystemZeroError) error.idempotencyKey = headers.get('idempotency-key');
      throw error;
    }));
  }

  #checkRetryBudget(retryBudget: number): void {
    if (!(retryBudget >= 0 && retryBudget <= 2_147_483_647 && Number.isFinite(retryBudget))) throw new RangeError('retryBudget must be between 0 and 2147483647 milliseconds.');
  }

  async #send<T>(url: string, init: RequestInit, maxRetries: number, retryBudget: number, context?: RetryContext): Promise<ResponseResult<T>> {
    const started = this.#monotonic();
    const initialExternalRemaining = context?.remaining?.() ?? Infinity;
    const elapsed = () => Math.max(0, this.#monotonic() - started);
    // Pin the admission lifetime to monotonic time. A wall clock rollback
    // cannot extend either the batch deadline or the original key's lifetime.
    const externalRemaining = () => Math.min(initialExternalRemaining - elapsed(), context?.remaining?.() ?? Infinity);
    const remaining = () => Math.min(retryBudget - elapsed(), externalRemaining());
    const random = context?.random ?? this.#random;
    const sleep = context?.sleep ?? this.#sleep;
    const safeToRetry = init.method === 'GET' || Boolean(new Headers(init.headers).get('idempotency-key')?.trim());
    let previous: SystemZeroError | undefined;
    const waitToRetry = async (error: SystemZeroError, headers: Headers, attempt: number): Promise<void> => {
      if (externalRemaining() <= 0) { context?.onDeadline?.(); throw error; }
      if (!safeToRetry || attempt >= maxRetries || retryBudget === 0) throw error;
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
        throw previous ?? new APITimeoutError('System Zero request timed out.', { code: 'system_zero_timeout' });
      }
      // One millisecond is the minimum useful retry timeout on supported runtimes.
      (init.headers as Headers).set('x-sdk-retry-count', String(attempt));
      context?.onAttempt?.();
      let result: AttemptResult;
      // The first request retains its normal timeout, subject to any batch deadline.
      // Only retry attempts use the remaining recovery budget.
      try { result = await this.#attempt(url, init, Math.min(this.options.timeout, admittedTimeLeft, attempt === 0 ? Infinity : timeLeft)); }
      catch (error) {
        if (externalRemaining() <= 0) { context?.onDeadline?.(); throw error; }
        if (context?.workflowRecovery || !(error instanceof APIConnectionError) || !error.retryable) throw error;
        await waitToRetry(error, new Headers(), attempt);
        continue;
      }
      const { response, body, json } = result;
      if (response.ok) {
        if (!json) throw new APIError('System Zero returned invalid JSON.', { status: response.status, type: 'api_error', headers: response.headers, request_id: response.headers.get('x-request-id') });
        return { data: body as T, response, metadata: metadata(response.headers, new Headers(init.headers).get('idempotency-key')) };
      }
      let error = responseError(response.status, response.headers, body, false);
      const retryable = body !== null && typeof body === 'object' && 'error' in body && body.error !== null && typeof body.error === 'object' && 'retryable' in body.error && typeof body.error.retryable === 'boolean' ? body.error.retryable : undefined;
      const retry = error.type !== 'authentication_error' && error.type !== 'permission_error' && (context?.workflowRecovery
        ? shouldRecoverWorkflow(response.status, error.code, response.headers, retryable, error.details)
        : shouldRetry(response.status, error.code, response.headers, retryable, this.#now()));
      if (retryable === undefined && retry) error = responseError(response.status, response.headers, body, true);
      if (externalRemaining() <= 0) { context?.onDeadline?.(); throw error; }
      if (!retry) throw error;
      await waitToRetry(error, response.headers, attempt);
    }
  }

  async #attempt(url: string, init: RequestInit, timeout: number): Promise<AttemptResult> {
    const controller = new AbortController();
    const safeToRetry = init.method === 'GET' || Boolean(new Headers(init.headers).get('idempotency-key')?.trim());
    let response: Response | undefined;
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Keyed decisions and safe GETs retain their before-response retry policy.
    // request_sent is diagnostic; sending alone does not end keyed eligibility.
    const fields = () => ({
      retryable: safeToRetry && response === undefined && !hasResponseStarted(controller.signal),
      status: response?.status ?? null,
      request_id: response?.headers.get('x-request-id') ?? null,
      request_sent: response ? true : requestSent(controller.signal),
      ...(response ? { headers: response.headers } : {}),
    });
    const operation = (async (): Promise<AttemptResult> => {
      try {
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
        throw new APIConnectionError('System Zero connection failed.', { ...transportFields, cause });
      } finally {
        reader?.releaseLock();
      }
    })();
    const deadline = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        reject(new APIConnectionError('System Zero request timed out.', { ...fields(), type: 'timeout_error' }));
        controller.abort();
        // Injected transports may ignore abort. Cancel both tee branches without awaiting them.
        void reader?.cancel().catch(() => {});
        void response?.body?.cancel().catch(() => {});
      }, timeout);
    });
    try { return await Promise.race([operation, deadline]); }
    finally { clearTimeout(timer); }
  }
}
