import { randomUUID } from 'node:crypto';
import type { IncomingMessage } from 'node:http';
import { isIP } from 'node:net';
import type { Attachment, JsonValue, StructuredJson } from './api-types.js';
import { APIConnectionError, responseError, SystemZeroError } from './errors.js';
import { validateAttachments } from './media.js';
import { retryAfterMs, shouldRetry } from './retry.js';
import type {
  ClientMutation, EventLatency, PossiblyDecided, StreamAck, StreamClientOptions,
  StreamDecision, StreamError, StreamEvent, StreamHello, StreamRequest,
  StreamScheduled, StreamStartOptions, StreamWebSocket,
} from './stream-interface.js';

export * from './stream-interface.js';
export const STREAM_PROTOCOL = 'system-zero.stream.v1' as const;
export const STREAM_CLIENT_IMPLEMENTED = true as const;
const retryableCodes = new Set([1001, 1006, 1011, 1012, 1013]);
const STREAM_TIMER_MAX_DELAY_MS = 2_147_483_647;
// Gateway maximum lifetime (3600000) + max(close flush, deploy drain) (203000)
// + RFC close handshake (5000) + native pong/delivery margin (10000). A receipt
// can wait behind output already completed before admission closes. Keep this
// SDK bound fixed: an older 240000 hello hint is too short for that valid flush,
// while a larger peer-supplied hint must not extend receipt waiting indefinitely.
export const STREAM_RECEIPT_TIMEOUT_MS = 3_818_000;
const STREAM_FALLBACK_HEARTBEAT_GRACE_MS = 30_000;

export class StreamProtocolError extends Error {
  override name = 'StreamProtocolError';
  constructor(message: string, readonly code: string | null = null) { super(message); }
}
export class StreamDisconnectedError extends Error {
  override name = 'StreamDisconnectedError';
  constructor(message?: string, readonly closeCode?: number) { super(message); }
}
class ConnectionLost extends Error {}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  let settled = false;
  const promise = new Promise<T>((yes, no) => {
    resolve = value => { settled = true; yes(value); };
    reject = error => { settled = true; no(error); };
  });
  // A caller may register its latency wait after the connection has already closed.
  void promise.catch(() => {});
  return { promise, resolve, reject, get settled() { return settled; } };
}
type Deferred<T> = ReturnType<typeof deferred<T>>;
function copy<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T; }
function integer(value: unknown, minimum = 0): value is number { return Number.isSafeInteger(value) && (value as number) >= minimum; }
function object(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === 'object' && !Array.isArray(value); }

function checkedStreamURL(value: string): URL {
  const message = 'streamURL must be WSS or loopback WS without credentials, query or fragment.';
  if (typeof value !== 'string') throw new TypeError(message);
  let url: URL;
  try { url = new URL(value); } catch { throw new TypeError(message); }
  const host = url.hostname;
  const local = host === ['local', 'host'].join('') || host === '[' + ':' + ':1]' || (isIP(host) === 4 && host.startsWith('127.'));
  if ((url.protocol !== 'wss:' && !(url.protocol === 'ws:' && local))
    || url.username || url.password || url.search || url.hash || value.includes('?') || value.includes('#')) throw new TypeError(message);
  return url;
}

function streamShouldRetry(status: number, error: Record<string, unknown> | undefined, headers: Headers, everConnected: boolean): boolean {
  if (error?.type === 'authentication_error' || error?.type === 'permission_error') return false;
  const code = typeof error?.code === 'string' ? error.code : null;
  const retryable = typeof error?.retryable === 'boolean' ? error.retryable : undefined;
  const hint = headers.get('x-should-retry');
   
  const recovery = status === 503 && code === 'limiter_unavailable'
    || status === 429 && code === 'stream_limit_exceeded' && everConnected;
  return shouldRetry(status, code, headers, retryable)
    || recovery && retryable !== false && hint !== 'false' && (retryable === true || hint === 'true');
}

function validateStreamAttachments(attachments: readonly Attachment[]): void {
  validateAttachments(attachments);
}

type SocketListener = (...args: any[]) => void;
function forwardSocketEvent(cell: { callback: SocketListener | undefined }): SocketListener {
  return (...args) => cell.callback?.(...args);
}
function socketHandlers(socket: StreamWebSocket) {
  const remove = socket.on ? (socket.off ?? socket.removeListener)?.bind(socket) : socket.removeEventListener?.bind(socket);
  const entries: Array<{ type: string; listener: SocketListener; cell: { callback: SocketListener | undefined } }> = [];
  let disposed = false;
  const detach = (type: string, listener: SocketListener) => { try { remove?.(type, listener); } catch {} };
  return {
    on(type: string, callback: SocketListener) {
      if (disposed) return;
      const cell = { callback: callback as SocketListener | undefined }, listener = forwardSocketEvent(cell);
      entries.push({ type, listener, cell });
      if (socket.on) socket.on(type, listener);
      else socket.addEventListener!(type, listener);
      // An adapter can publish buffered data before its registration returns.
      if (disposed) detach(type, listener);
    },
    dispose(peerClosed = false) {
      if (disposed) return;
      disposed = true;
      const pending = entries.splice(0);
      // Adapter registration/removal can throw or reenter. Release SDK callbacks
      // before calling that foreign code so cleanup cannot strand client work.
      for (const { cell } of pending) cell.callback = undefined;
      // Node can emit error while close/terminate completes. These temporary
      // handlers retain only the transport, never the SDK or application callbacks.
      if (socket.on && remove && !peerClosed && socket.readyState !== 3) {
        const ignoreError = () => {};
        const closed = () => { detach('error', ignoreError); detach('close', closed); };
        try { socket.on('error', ignoreError); socket.on('close', closed); }
        catch { detach('error', ignoreError); detach('close', closed); }
      }
      for (const { type, listener } of pending) detach(type, listener);
    },
  };
}

class EventQueue implements AsyncIterableIterator<StreamEvent> {
  #items: StreamEvent[] = [];
  #waiters: Array<{ resolve: (value: IteratorResult<StreamEvent>) => void; reject: (error: unknown) => void }> = [];
  #closed = false;
  #error: Error | undefined;
  constructor(private readonly unsubscribe: () => void) {}
  push(value: StreamEvent): boolean {
    if (this.#closed) return true;
    const waiter = this.#waiters.shift();
    if (waiter) waiter.resolve({ done: false, value });
    else {
      // Local teardown notices cannot grow a full subscriber beyond the bound.
      if (this.#items.length >= 64) return value.type === 'possibly_decided';
      this.#items.push(value);
    }
    return true;
  }
  end(error?: Error, discard = false) {
    this.#closed = true; this.#error = error;
     
    if (discard) this.#items = [];
    if (!this.#items.length) this.unsubscribe();
    for (const waiter of this.#waiters.splice(0)) {
      if (error) waiter.reject(error);
      else waiter.resolve({ done: true, value: undefined });
    }
  }
  next(): Promise<IteratorResult<StreamEvent>> {
    const value = this.#items.shift();
    if (this.#closed && !this.#items.length) this.unsubscribe();
    if (value) return Promise.resolve({ done: false, value });
    if (this.#error) return Promise.reject(this.#error);
    if (this.#closed) return Promise.resolve({ done: true, value: undefined });
    return new Promise((resolve, reject) => this.#waiters.push({ resolve, reject }));
  }
  return(): Promise<IteratorResult<StreamEvent>> { this.#items = []; this.end(); return Promise.resolve({ done: true, value: undefined }); }
  throw(error?: unknown): Promise<IteratorResult<StreamEvent>> { void this.return(); return Promise.reject(error); }
  [Symbol.asyncIterator]() { return this; }
}

interface Timer {
  sent: number; streamId: string; eventId: string; version: number;
  ack?: StreamAck; result: Deferred<EventLatency>;
}
interface StateSnapshot { state: StructuredJson; attachments?: Attachment[] }
interface Mutation {
  revision: number; key: string; apply: (state: StateSnapshot | undefined) => StateSnapshot;
  frame: (eventId: string) => ClientMutation; ack: Deferred<StreamAck>; timer?: Timer;
}
interface PendingAck { eventId: string; version: number; timers: Timer[]; resolve: (ack: StreamAck) => void; reject: (error: unknown) => void }

/** Server-side protocol v1 client. Replayed state retains ordinary HTTP idempotency ownership. */
export class SystemZeroStreamClient implements AsyncIterable<StreamEvent> {
  readonly protocol = STREAM_PROTOCOL;
  readonly options: Readonly<{ streamURL: string; reconnectPolicy: 'latest' | 'never'; timeout: number; maxReconnectAttempts: number; reconnectTimeout: number }>;
  #key: string;
  #promptCacheKey = randomUUID();
  #webSocketConstructor: StreamClientOptions['WebSocket'];
  #now: () => number;
  #random: () => number;
  #socket: StreamWebSocket | undefined;
  #detachSocket: ((peerClosed: boolean) => void) | undefined;
  #ready: Deferred<StreamHello> | undefined;
  #hello: Deferred<StreamHello> | undefined;
  #helloValue: StreamHello | undefined;
  #closed = false;
  #terminal: Error | undefined;
  #connectionFailure: SystemZeroError | undefined;
  #requestSent: boolean | null = null;
  #stopped = false;
  #disconnecting = false;
  #generation = 0;
  #connecting = false;
  #everConnected = false;
  #openingAttempts = 0;
  #openingDeadline: number | undefined;
  #openingTimer: ReturnType<typeof setTimeout> | undefined;
  #lastOpeningError: Error | undefined;
  #retryAttempt = 0;
  #retryNotBefore = 0;
  #reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  #watchdog: ReturnType<typeof setTimeout> | undefined;
  #receiptDeadline: number | undefined;
  #fallbackReceiptTimeout: number | undefined;
  #heartbeat: ReturnType<typeof setTimeout> | undefined;
  #seq = 0;
  #version = 0;
  #lastDecision = 0;
  #streamId = '';
  #request: StreamRequest | undefined;
  #startOptions: Required<StreamStartOptions> = { decisionMode: 'on_change', minIntervalMs: 0 };
  #connectionMode: 'on_change' | 'on_request' = 'on_change';
  #everStarted = false;
  #accepted: StateSnapshot | undefined;
  #acceptedKey: string | undefined;
  #versionKey: string | undefined;
  #refusedKey: string | undefined;
  #replayOnRequest = false;
  #desired: StateSnapshot | undefined;
  #revision = 0;
  #mutations: Mutation[] = [];
  #writes = Promise.resolve();
  #eventRate = 0;
  #eventBurst = 1;
  #eventTokens = 1;
  #eventAt = 0;
  #messageBytes = 4_194_304;
  #nativeFrames = { rate: 0, burst: 1, tokens: 1, at: 0 };
  #nativeBytes = { rate: 0, burst: 131, tokens: 131, at: 0 };
  #nativePending = 0;
  #nativeQueueLimit = 2;
  #messageRate = 0;
  #messageBurst = 1;
  #messageTokens = 1;
  #messageAt = 0;
  #pacing = new Set<{ timer: ReturnType<typeof setTimeout>; reject: (error: Error) => void }>();
  #ack: PendingAck | undefined;
  #scheduled: Deferred<StreamScheduled> | undefined;
  #scheduledPriorWork = false;
  #explicitIntentVersion: number | undefined;
  #queues = new Set<EventQueue>();
  #listeners = new Map<string, Set<(event: any) => void>>();
  #timers = new Set<Timer>();
  #ackTimers = new WeakMap<StreamAck, Timer>();
  #ambiguous = new Map<number, string>();

  constructor(options: StreamClientOptions = {}) {
    const key = options.apiKey ?? process.env.SYSTEM_ZERO_API_KEY;
    if (!key) throw new Error('Supply an apiKey or set SYSTEM_ZERO_API_KEY in the server environment.');
    if (typeof key !== 'string' || !/^[\x21-\x7e]+$/u.test(key)) throw new TypeError('apiKey must contain only visible ASCII characters.');
    const url = checkedStreamURL(options.streamURL ?? 'wss://api.rightnowai.co/v1/system-zero/stream');
    const timeout = options.timeout ?? 30_000;
    const reconnectTimeout = options.reconnectTimeout ?? 120_000;
    const maxReconnectAttempts = options.maxReconnectAttempts ?? 8;
    for (const [name, value] of [['timeout', timeout], ['reconnectTimeout', reconnectTimeout]] as const) {
      if (!(Number.isFinite(value) && value >= 1 && value <= STREAM_TIMER_MAX_DELAY_MS)) throw new RangeError(`${name} must be between 1 and ${STREAM_TIMER_MAX_DELAY_MS} milliseconds.`);
    }
    if (!(Number.isSafeInteger(maxReconnectAttempts) && maxReconnectAttempts >= 1)) throw new RangeError('maxReconnectAttempts must be a positive safe integer.');
    if (options.reconnectPolicy !== undefined && !['latest', 'never'].includes(options.reconnectPolicy)) throw new TypeError('reconnectPolicy must be latest or never.');
    this.options = Object.freeze({ streamURL: url.href, reconnectPolicy: options.reconnectPolicy ?? 'latest', timeout, maxReconnectAttempts, reconnectTimeout });
    this.#key = key; this.#webSocketConstructor = options.WebSocket;
    this.#now = options.now ?? (() => performance.now()); this.#random = options.random ?? Math.random;
  }

  connect(): Promise<StreamHello> {
    if (this.#closed || this.#terminal) return Promise.reject(this.#terminal ?? new StreamDisconnectedError('Stream is closed.'));
    if (!this.#ready) { this.#ready = deferred(); void this.#open(); }
    else if (!this.#socket && !this.#connecting && !this.#reconnectTimer) {
      const delay = Math.max(0, this.#retryNotBefore - this.#now());
      if (delay) this.#scheduleReconnect(this.#retryNotBefore);
      else void this.#open();
    }
    return this.#ready.promise;
  }

  start(request: StreamRequest, options: StreamStartOptions = {}): Promise<StreamAck> {
    if (this.#request) return Promise.reject(new Error('start may be called only once; use a new client to change questions or model.'));
    const interval = options.minIntervalMs ?? 0;
    if (!integer(interval) || interval > 60_000) return Promise.reject(new RangeError('minIntervalMs must be an integer from 0 through 60000.'));
    const mode = options.decisionMode ?? 'on_change';
    if (!['on_change', 'on_request'].includes(mode)) return Promise.reject(new TypeError('decisionMode must be on_change or on_request.'));
    try {
      if (request.attachments != null) validateStreamAttachments(request.attachments);
      this.#request = copy(request);
    } catch (error) { return Promise.reject(error); }
    this.#request.prompt_cache_key ??= this.#promptCacheKey;
    this.#startOptions = { decisionMode: mode, minIntervalMs: interval };
    const state: StateSnapshot = { state: copy(this.#request.state), ...(this.#request.attachments == null ? {} : { attachments: copy(this.#request.attachments) }) };
    return this.#mutate(() => copy(state), eventId => ({ type: 'start', event_id: eventId, request: copy(this.#request!), decision_mode: mode, min_interval_ms: interval }));
  }

  appendText(text: string): Promise<StreamAck> {
    return this.#mutate(snapshot => {
      if (typeof snapshot?.state !== 'string' || typeof text !== 'string' || !text.length) throw new TypeError('appendText requires string state and nonempty text.');
      return { ...snapshot, state: snapshot.state + text };
    }, eventId => ({ type: 'append_text', event_id: eventId, text }));
  }

  appendItems(items: JsonValue[]): Promise<StreamAck> {
    const snapshot = copy(items);
    return this.#mutate(state => {
      if (!Array.isArray(state?.state) || !Array.isArray(snapshot) || !snapshot.length) throw new TypeError('appendItems requires array state and nonempty items.');
      return { ...state, state: [...state.state, ...snapshot] };
    }, eventId => ({ type: 'append_items', event_id: eventId, items: snapshot }));
  }

  appendAttachments(attachments: Attachment[]): Promise<StreamAck> {
    let snapshot: Attachment[];
    try {
      if (!Array.isArray(attachments) || !attachments.length) throw responseError(400, new Headers(), { error: { code: 'invalid_request', type: 'invalid_request_error', message: 'The System Zero request is invalid.', param: 'attachments', retryable: false } }, false);
      // Validate caller objects before copying, in full-list HTTP order and indices.
      validateStreamAttachments([...(this.#desired?.attachments ?? []), ...attachments]);
      snapshot = copy(attachments);
    } catch (error) { return Promise.reject(error); }
    return this.#mutate(state => ({ ...state!, attachments: [...(state?.attachments ?? []), ...snapshot] }),
      eventId => ({ type: 'append_attachments', event_id: eventId, attachments: snapshot }));
  }

  replace(state: StructuredJson, attachments?: Attachment[]): Promise<StreamAck> {
    let snapshot: StructuredJson, media: Attachment[] | undefined;
    try {
      if (attachments !== undefined) validateStreamAttachments(attachments);
      snapshot = copy(state); media = attachments === undefined ? undefined : copy(attachments);
    } catch (error) { return Promise.reject(error); }
    return this.#mutate(current => ({ ...current, state: copy(snapshot), ...(media === undefined ? {} : { attachments: copy(media) }) }),
      eventId => ({ type: 'replace', event_id: eventId, state: snapshot, ...(media === undefined ? {} : { attachments: media }) }));
  }

  decide(): Promise<StreamScheduled> {
    if (!this.#request) return Promise.reject(new Error('Call start before decide.'));
    const result = deferred<StreamScheduled>();
    this.#writes = this.#writes.then(async () => {
      try {
        await this.connect();
        // A close can run while the resolved connect promise yields control.
        if (this.#closed || this.#terminal) throw this.#terminal ?? new StreamDisconnectedError('Stream is closed.');
        if (!this.#socket || this.#socket.readyState !== 1) throw new StreamDisconnectedError('Stream is not connected.');
        if (!this.#version) throw new Error('The stream has no accepted start.');
        const socket = this.#socket;
        await this.#paceSend(false, () => {
          if (socket !== this.#socket || socket.readyState !== 1) throw new ConnectionLost('Stream disconnected before send.');
          this.#scheduled = result;
          this.#scheduledPriorWork = this.#ambiguous.has(this.#version);
          this.#explicitIntentVersion = this.#version;
          this.#ambiguous.set(this.#version, `evt_${this.#version}`);
          this.#armWatchdog(true);
          this.#send({ type: 'decide', state_version: this.#version });
        });
        await result.promise;
      } catch (error) { result.reject(error); }
    });
    return result.promise;
  }

  waitForDecision(ack: StreamAck): Promise<EventLatency> {
    const timer = this.#ackTimers.get(ack);
    return timer ? timer.result.promise : Promise.reject(new TypeError('The ack must come from this client.'));
  }

  events(): AsyncIterableIterator<StreamEvent> {
    const queue = new EventQueue(() => { this.#queues.delete(queue); });
    if (this.#closed || this.#terminal) queue.end(this.#terminal instanceof StreamDisconnectedError && this.#terminal.closeCode === 1008 ? this.#terminal : undefined);
    else this.#queues.add(queue);
    return queue;
  }
  [Symbol.asyncIterator]() { return this.events(); }
  on<T extends StreamEvent['type']>(type: T, callback: (event: Extract<StreamEvent, { type: T }>) => void): () => void {
    let callbacks = this.#listeners.get(type);
    if (!callbacks) { callbacks = new Set(); this.#listeners.set(type, callbacks); }
    callbacks.add(callback);
    return () => { callbacks.delete(callback); };
  }

  async close(): Promise<void> {
    if (this.#closed) return;
    this.#closed = true;
    // Ownership precedes callbacks; teardown may still be pending on that frame.
    if (this.#stopped) {
      const failure = this.#terminal instanceof StreamDisconnectedError && this.#terminal.closeCode === 1008 ? this.#terminal : undefined;
      for (const queue of this.#queues) queue.end(failure, true);
      this.#queues.clear(); this.#listeners.clear(); return;
    }
    clearTimeout(this.#reconnectTimer);
    const socket = this.#socket;
    const error = this.#terminal ?? this.#connectionFailure ?? new StreamDisconnectedError('Stream closed by caller.');
    // The outer teardown must retain caller ownership when nested entry is skipped.
    if (this.#disconnecting) this.#terminal ??= error;
    this.#disconnect(error, 1000);
    try { socket?.close(1000, 'client_close'); } catch { socket?.terminate?.(); }
    for (const queue of this.#queues) queue.end(undefined, true);
    this.#queues.clear(); this.#listeners.clear();
  }

  #mutate(apply: Mutation['apply'], frame: Mutation['frame']): Promise<StreamAck> {
    if (this.#closed || this.#terminal) return Promise.reject(this.#terminal ?? new StreamDisconnectedError('Stream is closed.'));
    if (!this.#request) return Promise.reject(new Error('Call start before sending state mutations.'));
    let desired: StateSnapshot;
    try {
      desired = apply(this.#desired);
      if (desired.attachments !== undefined) validateStreamAttachments(desired.attachments);
    } catch (error) { return Promise.reject(error); }
    const priorKey = this.#mutations.at(-1)?.key ?? this.#acceptedKey;
    const key = priorKey !== undefined && JSON.stringify(desired) === JSON.stringify(this.#desired) ? priorKey : 'sz-stream-' + randomUUID();
    this.#desired = desired;
    const mutation: Mutation = { revision: ++this.#revision, key, apply, frame, ack: deferred() };
    this.#mutations.push(mutation);
    this.#writes = this.#writes.then(async () => {
      for (;;) {
        try {
          await this.connect();
          if (!this.#mutations.includes(mutation)) return;
          if (this.#replayOnRequest && mutation.key !== this.#refusedKey) {
            // A changed state can resume on_change on a fresh connection; the
            // guarded replay must never make the previously refused state run.
            const socket = this.#socket;
            this.#disconnect(new ConnectionLost('Restore automatic decisions for changed state.'), 1000);
            try { socket?.close(1000, 'state_changed'); } catch { socket?.terminate?.(); }
            continue;
          }
          const eventId = `evt_${this.#version + 1}`;
          const frame = { ...mutation.frame(eventId), idempotency_key: mutation.key };
          const ack = await this.#sendMutation(frame, [mutation]);
          this.#accepted = mutation.apply(this.#accepted);
          this.#acceptedKey = mutation.key;
          this.#mutations = this.#mutations.filter(item => item !== mutation);
          mutation.ack.resolve(ack);
          return;
        } catch (error) {
          if (error instanceof ConnectionLost && !this.#closed && !this.#terminal) continue;
          this.#mutations = this.#mutations.filter(item => item !== mutation);
          this.#rebuildDesired(); mutation.ack.reject(error); return;
        }
      }
    });
    return mutation.ack.promise;
  }

  #rebuildDesired() {
    this.#desired = this.#accepted;
    let priorKey = this.#acceptedKey;
    for (const mutation of [...this.#mutations]) {
      try {
        const desired = mutation.apply(this.#desired);
        if (desired.attachments !== undefined) validateStreamAttachments(desired.attachments);
        // A refusal can make a queued replacement unchanged after rollback.
        if (priorKey !== undefined && JSON.stringify(desired) === JSON.stringify(this.#desired)) mutation.key = priorKey;
        else if (mutation.key === priorKey) mutation.key = 'sz-stream-' + randomUUID();
        this.#desired = desired;
        priorKey = mutation.key;
      }
      catch (error) { this.#mutations = this.#mutations.filter(item => item !== mutation); mutation.ack.reject(error); }
    }
  }

  async #open() {
    if (this.#connecting || this.#closed || this.#terminal) return;
    let streamURL: string;
    try { streamURL = checkedStreamURL(this.options.streamURL).href; }
    catch (error) { this.#stop(error as Error, 1008); return; }
    if (this.#openingDeadline === undefined) {
      this.#openingDeadline = this.#now() + this.options.reconnectTimeout;
      const timer = setTimeout(() => {
        if (this.#openingTimer !== timer) return;
        this.#exhaustOpening();
      }, this.options.reconnectTimeout);
      this.#openingTimer = timer; timer.unref?.();
    }
    if (this.#openingAttempts >= this.options.maxReconnectAttempts || this.#now() >= this.#openingDeadline) { this.#exhaustOpening(); return; }
    this.#openingAttempts++;
    this.#connecting = true;
    const generation = ++this.#generation;
    try {
      this.#seq = 0; this.#version = 0; this.#lastDecision = 0; this.#streamId = '';
      this.#connectionFailure = undefined;
      this.#requestSent = null;
      this.#helloValue = undefined; this.#fallbackReceiptTimeout = undefined;
      this.#hello = deferred();
      const headers: Record<string, string> = { Authorization: `Bearer ${this.#key}` };
      let socket: StreamWebSocket;
      if (this.#webSocketConstructor) socket = new this.#webSocketConstructor(streamURL, [STREAM_PROTOCOL], { headers, perMessageDeflate: false, autoPong: false });
      else {
        // Node 22 and 24 accept headers through the second-argument options object.
        // Reject runtimes whose native implementation lacks that overload.
        if (!globalThis.WebSocket?.toString().includes('WebSocketInit')) throw new StreamProtocolError('Inject a server-side WebSocket constructor such as ws; this platform has no verified Authorization-header support.');
        const Native = globalThis.WebSocket as unknown as new(url: string, options: { protocols: string[]; headers: Record<string, string> }) => StreamWebSocket;
        socket = new Native(streamURL, { protocols: [STREAM_PROTOCOL], headers });
      }
      this.#socket = socket;
      const handlers = socketHandlers(socket);
      this.#detachSocket = peerClosed => handlers.dispose(peerClosed);
      const current = () => !this.#disconnecting && this.#generation === generation && this.#socket === socket;
      const failed = (cause: unknown, opaqueOpening = false) => {
        if (!current()) return;
        if (cause instanceof StreamProtocolError || cause instanceof StreamDisconnectedError || cause instanceof SystemZeroError && !(cause instanceof APIConnectionError)) {
          this.#stop(cause, cause instanceof StreamProtocolError ? 1002 : 1008); return;
        }
        const failure = this.#transportError('Stream connection failed.', cause);
        if (opaqueOpening) this.#stop(failure, 1008);
        else this.#disconnect(failure, 1006);
        try { if (socket.terminate) socket.terminate(); else socket.close(); } catch {}
      };
      // Injected adapters can deliver a buffered hello during listener registration.
      this.#armWatchdog();
      if (socket.on) {
        handlers.on('message', (data: Buffer | string, binary: boolean) => { if (current()) this.#message(typeof data === 'string' ? data : data.toString('utf8'), binary); });
        handlers.on('ping', (data: Buffer) => {
          if (!current() || !this.#helloValue) return;
          this.#liveness();
          if (socket.pong) void this.#nativePong(socket, data).catch(error => { if (current()) this.#stop(error, 1008); });
        });
        handlers.on('close', (code: number) => { if (current()) this.#disconnect(new ConnectionLost('Stream disconnected.'), code, true); });
        handlers.on('error', failed);
        handlers.on('upgrade', () => { if (current()) this.#requestSent = true; });
        handlers.on('unexpected-response', (_request: unknown, response: IncomingMessage) => {
          if (!current()) return;
          this.#requestSent = true;
          const status = response.statusCode ?? 0;
          const headers = new Headers();
          for (const [name, value] of Object.entries(response.headers)) {
            if (typeof value === 'string') headers.set(name, value);
            else if (Array.isArray(value)) for (const item of value) headers.append(name, item);
          }
          const delay = retryAfterMs(headers);
          if (delay !== null) this.#retryNotBefore = Math.max(this.#retryNotBefore, this.#now() + delay);
          const refuse = (body?: unknown) => {
            if (!current()) return;
            const failure = responseError(status, headers, body, status === 429 || status >= 500);
            const error = object(body) && object(body.error) ? body.error : undefined;
            if (!streamShouldRetry(status, error, headers, this.#everConnected)) {
              this.#stop(failure, 1008); socket.terminate?.();
            } else { this.#disconnect(failure, 1006); socket.terminate?.(); }
          };
          const chunks: Buffer[] = []; let bytes = 0;
          response.on('data', (chunk: Buffer | string) => {
            const buffer = typeof chunk === 'string' ? Buffer.from(chunk) : chunk;
            bytes += buffer.length;
            if (bytes <= 65_536) chunks.push(buffer);
            else chunks.length = 0;
          });
          response.once('end', () => {
            let body: unknown;
            if (bytes <= 65_536) { try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch {} }
            refuse(body);
          });
          response.once('error', (cause: unknown) => {
            if (!current()) return;
            this.#stop(new APIConnectionError('Stream upgrade response failed.', { cause, status, headers, request_sent: true }), 1006);
            try { socket.terminate?.(); } catch {}
          });
          response.resume();
        });
      } else if (socket.addEventListener) {
        handlers.on('message', (event: { data: unknown }) => { if (current()) this.#message(typeof event.data === 'string' ? event.data : '', typeof event.data !== 'string'); });
        handlers.on('close', (event: { code: number }) => { if (current()) this.#disconnect(new ConnectionLost('Stream disconnected.'), event.code, true); });
        handlers.on('error', (event: unknown) => {
          // Native WebSocket hides HTTP upgrade status/headers. Fail closed before hello:
          // otherwise an authentication refusal could become an endless reconnect loop.
          const cause = object(event) && 'error' in event ? event.error : event instanceof Error ? event : undefined;
          failed(cause, this.#seq === 0);
        });
      } else throw new StreamProtocolError('Injected WebSocket must expose on or addEventListener.');
      const hello = await this.#hello.promise;
      if (!current()) return;
      if (socket.protocol !== STREAM_PROTOCOL) throw new StreamProtocolError('Server did not negotiate the stream v1 subprotocol.');
      this.#everConnected = true;
      this.#resetOpening(); this.#retryAttempt = 0;
      const receiptTimeout = hello.limits.receipt_timeout_ms;
      // Native EventTarget transports hide control pings. A valid receipt hint
      // advertises fallback support; its value does not override the SDK's bound.
      if (!socket.on && typeof receiptTimeout === 'number' && Number.isFinite(receiptTimeout) && receiptTimeout > 0) {
        this.#fallbackReceiptTimeout = STREAM_RECEIPT_TIMEOUT_MS;
      }
      this.#armHeartbeat(hello.limits.heartbeat_interval_ms!);
      if (this.#everStarted && this.#request) {
        const pending = [...this.#mutations];
        const state = copy(this.#desired!);
        const key = pending.at(-1)?.key ?? this.#acceptedKey;
        if (typeof key !== 'string' || !key.trim()) throw new StreamProtocolError('Cannot replay stream work without its original idempotency key.');
        this.#replayOnRequest = key === this.#refusedKey && this.#startOptions.decisionMode === 'on_change' && this.options.reconnectPolicy !== 'never';
        const ack = await this.#sendMutation({ type: 'start', event_id: 'evt_1', request: { ...this.#request, ...state }, idempotency_key: key, decision_mode: this.options.reconnectPolicy === 'never' || this.#replayOnRequest ? 'on_request' : this.#startOptions.decisionMode, min_interval_ms: this.#startOptions.minIntervalMs }, pending);
        this.#accepted = state;
        this.#acceptedKey = key;
        for (const mutation of pending) { this.#mutations = this.#mutations.filter(item => item !== mutation); mutation.ack.resolve(mutation.timer?.ack ?? ack); }
      }
      if (!current()) return;
      this.#connecting = false;
      this.#retryAttempt = 0;
      this.#ready!.resolve(hello);
    } catch (error) {
      if (generation !== this.#generation) return;
      if (error instanceof StreamProtocolError) this.#stop(error, 1002);
      // A replay rejected by ordinary request validation cannot heal with backoff.
      else if (error instanceof SystemZeroError && !(error instanceof APIConnectionError)) this.#stop(error, 1000);
      else this.#disconnect(error instanceof APIConnectionError && error.constructor === APIConnectionError
        ? error : this.#transportError('Stream connection failed.', error), 1006);
    }
  }

  #configureLimits(hello: StreamHello) {
    const limits = hello.limits;
    for (const key of [
      "max_duration_ms",
      "idle_timeout_ms",
      "start_timeout_ms",
      "heartbeat_interval_ms",
      "native_heartbeat_interval_ms",
      "max_control_queue_frames",
      "pong_timeout_ms",
      "max_message_bytes",
      "max_decide_body_bytes",
      "max_pending_events",
      "max_events",
      "max_events_per_second",
      "event_burst",
      "max_messages_per_second",
      "message_burst",
      "max_streams_per_process",
      "max_send_queue_bytes",
      "max_send_queue_frames",
      "min_interval_ms",
      "max_native_control_frames_per_second",
      "native_control_frame_burst",
      "max_native_control_bytes_per_second",
      "native_control_byte_burst",
    ] as const) {
      const value = limits[key];
      if (typeof value !== 'number' || !Number.isFinite(value) || value < (key === 'min_interval_ms' ? 0 : Number.MIN_VALUE)) {
        throw new StreamProtocolError(`Server hello requires a valid ${key} limit.`);
      }
    }
    const rate = limits.max_events_per_second!, burst = limits.event_burst!, bytes = limits.max_message_bytes!;
    if (!integer(burst, 1) || !integer(bytes, 1)) throw new StreamProtocolError('Server send limits are invalid.');
    this.#eventRate = rate; this.#eventBurst = burst; this.#eventTokens = burst;
    this.#eventAt = this.#now(); this.#messageBytes = Math.min(bytes, 4_194_304);
    const messageRate = limits.max_messages_per_second!, messageBurst = limits.message_burst!;
    if (!integer(messageBurst, 1)) throw new StreamProtocolError('Server message limits are invalid.');
    this.#messageRate = messageRate; this.#messageBurst = messageBurst;
    this.#messageTokens = messageBurst; this.#messageAt = this.#now();
    for (const [bucket, rateKey, burstKey] of [
      [this.#nativeFrames, 'max_native_control_frames_per_second', 'native_control_frame_burst'],
      [this.#nativeBytes, 'max_native_control_bytes_per_second', 'native_control_byte_burst'],
    ] as const) {
      const rate = limits[rateKey]!, burst = limits[burstKey]!;
      if (!integer(burst, 1)) throw new StreamProtocolError('Server native control limits are invalid.');
      Object.assign(bucket, { rate, burst, tokens: burst, at: this.#now() });
    }
    const queue = limits.max_control_queue_frames!;
    if (!integer(queue, 1)) throw new StreamProtocolError('Server native control queue limit is invalid.');
    this.#nativeQueueLimit = Math.min(queue, 2); this.#nativePending = 0;
    // EventTarget transports own automatic pongs. Verify that the advertised
    // gateway cadence fits their control budgets for the entire stream lifetime.
    if (!this.#socket?.on && limits.native_heartbeat_interval_ms !== undefined) {
      const interval = limits.native_heartbeat_interval_ms, lifetime = limits.max_duration_ms!;
      if (!integer(interval, 1) || !integer(lifetime, 1)) throw new StreamProtocolError('Server native heartbeat limits are invalid.');
      const bytes = 9 + String(Math.ceil(lifetime / interval)).length;
      if (this.#nativeFrames.rate && (this.#nativeFrames.rate * interval < 1000 || this.#nativeFrames.burst < 1) ||
          this.#nativeBytes.rate && (this.#nativeBytes.rate * interval < bytes * 1000 || this.#nativeBytes.burst < bytes)) {
        throw new StreamProtocolError('Native transport cannot satisfy the advertised control limits.');
      }
    }
  }

  async #nativePong(socket: StreamWebSocket, data: Buffer): Promise<void> {
    const bytes = data.byteLength + 6; // masked RFC control-frame header
    if (data.byteLength > 125 || this.#nativeBytes.rate && bytes > this.#nativeBytes.burst || this.#nativePending >= this.#nativeQueueLimit) throw new StreamProtocolError('Native control exceeds the advertised limit.');
    this.#nativePending++;
    try {
      for (;;) {
        if (socket !== this.#socket || socket.readyState !== 1) return;
        const now = this.#now(); let delay = 0;
        for (const [bucket, amount] of [[this.#nativeFrames, 1], [this.#nativeBytes, bytes]] as const) {
          bucket.tokens = Math.min(bucket.burst, bucket.tokens + Math.max(0, now - bucket.at) * bucket.rate / 1000);
          bucket.at = Math.max(now, bucket.at);
          if (bucket.rate) delay = Math.max(delay, (amount - bucket.tokens) * 1000 / bucket.rate);
        }
        if (delay <= 0) {
          if (this.#nativeFrames.rate) this.#nativeFrames.tokens--;
          if (this.#nativeBytes.rate) this.#nativeBytes.tokens -= bytes;
          socket.pong!(data); return;
        }
        await this.#waitForToken(Math.min(STREAM_TIMER_MAX_DELAY_MS, Math.max(1, Math.ceil(delay))));
      }
    } finally { if (socket === this.#socket) this.#nativePending--; }
  }

  #admitSend(event: boolean): number {
    const now = this.#now();
    this.#messageTokens = Math.min(this.#messageBurst, this.#messageTokens + Math.max(0, now - this.#messageAt) * this.#messageRate / 1000);
    this.#messageAt = Math.max(now, this.#messageAt);
    this.#eventTokens = Math.min(this.#eventBurst, this.#eventTokens + Math.max(0, now - this.#eventAt) * this.#eventRate / 1000);
    this.#eventAt = Math.max(now, this.#eventAt);
    const messageWait = this.#messageRate > 0 ? (1 - this.#messageTokens) * 1000 / this.#messageRate : 0;
    const eventWait = event && this.#eventRate > 0 ? (1 - this.#eventTokens) * 1000 / this.#eventRate : 0;
    const delay = Math.max(0, messageWait, eventWait);
    if (delay > 0) return Math.min(STREAM_TIMER_MAX_DELAY_MS, Math.max(1, Math.ceil(delay)));
    if (this.#messageRate > 0) this.#messageTokens -= 1;
    if (event && this.#eventRate > 0) this.#eventTokens -= 1;
    return 0;
  }

  #waitForToken(delay: number): Promise<void> {
    return new Promise((resolve, reject) => {
      const pending = { timer: setTimeout(() => { this.#pacing.delete(pending); resolve(); }, delay), reject };
      this.#pacing.add(pending);
    });
  }

  async #paceSend(event: boolean, write: () => void): Promise<void> {
    const socket = this.#socket;
    for (;;) {
      if (!socket || socket !== this.#socket || socket.readyState !== 1) throw new ConnectionLost('Stream is not connected.');
      const delay = this.#admitSend(event);
      if (!delay) { write(); return; }
      await this.#waitForToken(delay);
    }
  }

  #serialize(frame: unknown): string {
    const raw = JSON.stringify(frame);
    if (Buffer.byteLength(raw, 'utf8') > this.#messageBytes) throw new RangeError('Stream message exceeds the server-advertised byte limit.');
    return raw;
  }

  #sendMutation(frame: ClientMutation, mutations: Mutation[]): Promise<StreamAck> {
    // Idle close can race a resolved connect; retry before recording unsent work.
    if (!this.#socket) return Promise.reject(new ConnectionLost('Stream is not connected.'));
     
    // down before recording intent, then let the ordinary replay path recover.
    if (this.#socket.readyState !== 1) {
      const socket = this.#socket;
      this.#disconnect(new ConnectionLost('Stream is not connected.'), 1006);
      socket.terminate?.(); try { socket.close(); } catch {}
      return Promise.reject(new ConnectionLost('Stream is not connected.'));
    }
    // Admit both budgets atomically before receipt or billing bookkeeping.
    try { this.#serialize(frame); } catch (error) { return Promise.reject(error); }
    const delay = this.#admitSend(true);
    if (delay > 0) return this.#waitForToken(delay).then(() => this.#sendMutation(frame, mutations));
    if (frame.type === 'start') this.#everStarted = true;
    const result = deferred<StreamAck>();
    const version = this.#version + 1;
    const timers: Timer[] = [];
    this.#ack = { eventId: frame.event_id, version, timers, resolve: ack => {
      // An error can follow this ack before the mutation's await resumes.
      this.#versionKey = frame.idempotency_key;
      for (const timer of timers) { timer.ack = { ...ack }; this.#ackTimers.set(timer.ack, timer); }
      if (timers[0]) this.#ackTimers.set(ack, timers[0]);
      result.resolve(mutations.length === 1 ? mutations[0]!.timer!.ack! : ack);
    }, reject: result.reject };
    const sent = this.#now();
    for (const mutation of mutations) {
      const timer: Timer = { sent, streamId: this.#streamId, eventId: frame.event_id, version, result: deferred() };
      mutation.timer = timer; this.#timers.add(timer); timers.push(timer);
    }
    if (!timers.length) {
      const timer: Timer = { sent, streamId: this.#streamId, eventId: frame.event_id, version, result: deferred() };
      this.#timers.add(timer); timers.push(timer);
    }
    if (frame.type === 'start') this.#connectionMode = frame.decision_mode;
    if (this.#connectionMode === 'on_change' || this.#explicitIntentVersion !== undefined) this.#ambiguous.set(version, frame.event_id);
     
    try { this.#armWatchdog(true); this.#send(frame); }
    catch { const socket = this.#socket; this.#disconnect(new ConnectionLost('Stream mutation send failed.'), 1006); socket?.terminate?.(); try { socket?.close(); } catch {} }
    return result.promise;
  }

  #send(frame: unknown) {
    if (!this.#socket || this.#socket.readyState !== 1) throw new ConnectionLost('Stream is not connected.');
    const raw = this.#serialize(frame);
    try { this.#socket.send(raw); }
    catch (cause) {
      const socket = this.#socket;
      if (cause instanceof StreamProtocolError || cause instanceof StreamDisconnectedError || cause instanceof SystemZeroError && !(cause instanceof APIConnectionError)) {
        this.#stop(cause, cause instanceof StreamProtocolError ? 1002 : 1008);
      } else this.#disconnect(this.#transportError('Stream send failed.', cause), 1006);
      try { socket.terminate?.(); } catch {} try { socket.close(); } catch {}
      throw new ConnectionLost('Stream send failed.');
    }
  }

  #message(text: string, binary: boolean) {
    if (binary) { this.#stop(new StreamProtocolError('Server sent binary application data.'), 1003); return; }
    let raw: unknown;
    try { raw = JSON.parse(text); } catch { this.#stop(new StreamProtocolError('Server sent invalid JSON.'), 1007); return; }
    if (!object(raw) || typeof raw.type !== 'string' || !integer(raw.seq, 1) || raw.seq !== this.#seq + 1 || typeof raw.stream_id !== 'string' || !/^szws_[0-9a-f]{32}$/.test(raw.stream_id) || (this.#streamId && raw.stream_id !== this.#streamId)) { this.#stop(new StreamProtocolError('Server frame sequence or identity is invalid.'), 1002); return; }
    this.#seq = raw.seq; this.#streamId = raw.stream_id;
    this.#requestSent = true;
    if (raw.type === 'hello') {
      if (this.#seq !== 1 || raw.protocol !== STREAM_PROTOCOL || !object(raw.limits)) { this.#stop(new StreamProtocolError('Server hello is invalid.'), 1002); return; }
      const hello = raw as unknown as StreamHello;
      // Validate before a coalesced ping can reach the writer with unset budgets.
      try { this.#configureLimits(hello); }
      catch (error) { this.#stop(error as StreamProtocolError, 1002); return; }
      this.#helloValue = hello;
      this.#clearWatchdog(); this.#hello!.resolve(hello); return;
    }
    if (this.#seq === 1) { this.#stop(new StreamProtocolError('Server hello must be first.'), 1002); return; }
    if (raw.type === 'ping') {
      if (!integer(raw.nonce, 1)) { this.#stop(new StreamProtocolError('Server ping nonce is invalid.'), 1002); return; }
      this.#liveness();
      const socket = this.#socket;
      void this.#paceSend(false, () => {
        if (socket === this.#socket && socket?.readyState === 1) this.#send({ type: 'pong', nonce: raw.nonce });
      }).catch(() => { if (socket === this.#socket) this.#disconnect(new ConnectionLost('Heartbeat send failed.'), 1006); });
      return;
    }
    if (raw.type === 'ack') {
      const pending = this.#ack;
      if (!pending || raw.event_id !== pending.eventId || raw.state_version !== pending.version || typeof raw.scheduled !== 'boolean') { this.#stop(new StreamProtocolError('Server ack does not match the pending mutation.'), 1002); return; }
      // Refill from receipt time to avoid outrunning the server across transport latency.
      this.#eventAt = this.#now(); this.#messageAt = this.#eventAt;
      this.#version = pending.version; this.#ack = undefined; this.#clearWatchdog();
      if (!raw.scheduled && (this.#connectionMode === 'on_change' || this.#explicitIntentVersion === undefined)) this.#ambiguous.delete(pending.version);
      pending.resolve(raw as unknown as StreamAck);
    } else if (raw.type === 'scheduled') {
      this.#messageAt = this.#now();
      if (!this.#scheduled || raw.state_version !== this.#version || typeof raw.in_flight !== 'boolean') { this.#stop(new StreamProtocolError('Server scheduled frame is invalid.'), 1002); return; }
      this.#ambiguous.set(this.#version, `evt_${this.#version}`);
      this.#scheduledPriorWork = false;
      const pending = this.#scheduled; this.#scheduled = undefined; this.#clearWatchdog(); pending.resolve(raw as unknown as StreamScheduled);
    } else if (raw.type === 'decision') {
      if (!integer(raw.state_version, 1) || raw.state_version > this.#version || raw.state_version < this.#lastDecision || raw.http_status !== 200 || !object(raw.reply) || !Array.isArray(raw.covered_event_ids)) { this.#stop(new StreamProtocolError('Server decision watermark is invalid.'), 1002); return; }
      const decision = raw as unknown as StreamDecision;
      this.#lastDecision = decision.state_version;
      if (this.#scheduled && this.#explicitIntentVersion !== undefined && decision.state_version >= this.#explicitIntentVersion) {
        this.#scheduledPriorWork = false;
      }
      for (const timer of this.#timers) {
        if (timer.streamId === this.#streamId && timer.ack && timer.version <= decision.state_version) {
          timer.result.resolve({ eventId: timer.eventId, stateVersion: timer.version, latencyMs: Math.max(0, this.#now() - timer.sent), decision }); this.#timers.delete(timer);
        }
      }
      // An older snapshot may complete before a new explicit command's receipt.
      // That result does not settle the fresh command, which can still be counted.
      for (const version of this.#ambiguous.keys()) {
        if (version <= decision.state_version && !(this.#scheduled && version === this.#explicitIntentVersion)) this.#ambiguous.delete(version);
      }
      if (!this.#scheduled) {
        if (this.#explicitIntentVersion !== undefined && decision.state_version >= this.#explicitIntentVersion) this.#explicitIntentVersion = undefined;
      }
    } else if (raw.type === 'error') {
      if (!object(raw.retry) || typeof raw.retry.should_retry !== 'boolean' || !(raw.retry.retry_after_ms === null || (typeof raw.retry.retry_after_ms === 'number' && Number.isFinite(raw.retry.retry_after_ms) && raw.retry.retry_after_ms >= 0)) || !(raw.retry.retry_after === null || typeof raw.retry.retry_after === 'string') || !object(raw.envelope) || !object(raw.envelope.error) || typeof raw.envelope.error.message !== 'string' || !integer(raw.http_status, 400) || raw.http_status > 599) { this.#stop(new StreamProtocolError('Server error envelope is invalid.'), 1002); return; }
      const error = raw as unknown as StreamError;
      this.#rememberRetry(error.retry.retry_after_ms, error.retry.retry_after);
      const retryHeaders = new Headers({ 'x-should-retry': String(error.retry.should_retry) });
      if (error.retry.retry_after_ms !== null) retryHeaders.set('retry-after-ms', String(error.retry.retry_after_ms));
      if (error.retry.retry_after !== null) retryHeaders.set('retry-after', error.retry.retry_after);
      const canRetry = streamShouldRetry(error.http_status, error.envelope.error, retryHeaders, this.#everConnected);
      if (error.phase === 'decide' && error.state_version === this.#version && !canRetry) this.#refusedKey = this.#versionKey;
      if (error.phase === 'decide' && integer(error.state_version, 1) && error.state_version <= this.#version) {
        if (this.#connectionMode === 'on_change' && error.retry.should_retry && !error.fatal) {
          // The gateway may redispatch this version after the failed attempt.
          this.#ambiguous.set(error.state_version, `evt_${error.state_version}`);
        } else {
          for (const version of this.#ambiguous.keys()) {
            if (version <= error.state_version && !(this.#scheduled && version === this.#explicitIntentVersion)) this.#ambiguous.delete(version);
          }
          if (!this.#scheduled && this.#explicitIntentVersion !== undefined && error.state_version >= this.#explicitIntentVersion) this.#explicitIntentVersion = undefined;
        }
        if (this.#scheduled && this.#explicitIntentVersion !== undefined && error.state_version >= this.#explicitIntentVersion
          && !(this.#connectionMode === 'on_change' && error.retry.should_retry && !error.fatal)) this.#scheduledPriorWork = false;
      }
      if (error.phase === 'protocol' && this.#ack && error.event_id === this.#ack.eventId) {
        const pending = this.#ack; this.#ack = undefined; this.#clearWatchdog();
        this.#ambiguous.delete(pending.version);
        const failure = responseError(error.http_status, new Headers(), error.envelope, false);
        for (const timer of pending.timers) { timer.result.reject(failure); this.#timers.delete(timer); }
        pending.reject(failure);
      } else if (error.phase === 'protocol' && this.#scheduled && error.event_id === null) {
        const pending = this.#scheduled; this.#scheduled = undefined; this.#clearWatchdog();
        // A refused fresh command cannot leave ambiguity after its earlier work settled.
        if (!this.#scheduledPriorWork) this.#ambiguous.delete(this.#version);
        this.#scheduledPriorWork = false;
        this.#explicitIntentVersion = this.#ambiguous.size ? Math.max(...this.#ambiguous.keys()) : undefined;
        pending.reject(responseError(error.http_status, new Headers(), error.envelope, false));
      }
      const envelope = error.envelope.error;
      const unauthorized = [401, 403].includes(error.http_status)
        || envelope.type === 'authentication_error' || envelope.type === 'permission_error'
        || /auth|api_key/.test(envelope.code ?? '');
      const billing = error.http_status === 402 || /spend|budget|monthly_cap|payment|credit/.test(envelope.code ?? '');
      const fatal = unauthorized || billing || error.fatal && !canRetry;
      // A fatal frame ends this connection even when its close permits reconnect.
      // Retain its typed refusal before callbacks can close the transport.
      if (error.fatal) this.#connectionFailure = responseError(error.http_status, retryHeaders, error.envelope, false);
      // Own permanent refusals before application callbacks can call close().
      if (fatal) this.#terminal ??= unauthorized ? new StreamProtocolError('Stream authorization was refused.', error.envelope.error.code)
        : this.#connectionFailure ?? responseError(error.http_status, retryHeaders, error.envelope, false);
      this.#publish(error);
      if (fatal) this.#stop(this.#terminal!, 1008);
      return;
    } else if (raw.type === 'goodbye') {
      if (!object(raw.resume) || !integer(raw.resume.retry_after_ms)) { this.#stop(new StreamProtocolError('Server goodbye is invalid.'), 1002); return; }
      this.#rememberRetry(raw.resume.retry_after_ms, null);
    } else { this.#stop(new StreamProtocolError('Unknown server frame type.'), 1002); return; }
    this.#publish(raw as unknown as StreamEvent);
  }

  #publish(event: StreamEvent) {
    const overflow = this.#terminal instanceof StreamDisconnectedError && this.#terminal.closeCode === 1008;
    for (const queue of overflow ? [] : this.#queues) {
      if (!queue.push(event)) {
        this.#stop(new StreamDisconnectedError('Stream event consumer fell behind.', 1008), 1008);
        return;
      }
    }
    // Callback errors belong to application code and must not corrupt protocol state.
    // Snapshot subscriptions so removing and re-adding a callback cannot revisit it.
    for (const callback of [...(this.#listeners.get(event.type) ?? [])]) { try { callback(event); } catch {} }
  }

  #rememberRetry(milliseconds: number | null, value: string | null) {
    const headers = new Headers();
    if (value !== null) headers.set('retry-after', value);
    const delay = Math.max(typeof milliseconds === 'number' && Number.isFinite(milliseconds) && milliseconds >= 0 ? milliseconds : 0,
      retryAfterMs(headers) ?? 0);
    this.#retryNotBefore = Math.max(this.#retryNotBefore, this.#now() + delay);
  }

  #scheduleReconnect(deadline: number) {
    clearTimeout(this.#reconnectTimer);
    if (this.#closed || this.#terminal) return;
    const notBefore = Math.max(deadline, this.#retryNotBefore);
    if (this.#openingDeadline !== undefined && notBefore >= this.#openingDeadline) { this.#exhaustOpening(); return; }
    // Node reduces an overflowing timer to 1 ms. Keep the deadline and wait in bounded chunks.
    const delay = Math.min(STREAM_TIMER_MAX_DELAY_MS, Math.max(0, Math.ceil(notBefore - this.#now())));
    const timer = setTimeout(() => {
      if (this.#reconnectTimer !== timer || this.#closed || this.#terminal) return;
      this.#reconnectTimer = undefined;
      if (Math.max(notBefore, this.#retryNotBefore) > this.#now()) this.#scheduleReconnect(notBefore);
      else void this.#open();
    }, delay);
    this.#reconnectTimer = timer;
  }

  #resetOpening() {
    clearTimeout(this.#openingTimer); this.#openingTimer = undefined;
    this.#openingDeadline = undefined; this.#openingAttempts = 0; this.#lastOpeningError = undefined;
  }

  #exhaustOpening() {
    const error = this.#lastOpeningError ?? this.#transportError('Stream opening retry budget was exhausted.', undefined, true);
    const socket = this.#socket;
    this.#stop(error, 1006); socket?.terminate?.();
  }

  #clearWatchdog() {
    clearTimeout(this.#watchdog);
    this.#watchdog = undefined; this.#receiptDeadline = undefined;
  }

  #armWatchdog(receipt = false) {
    this.#clearWatchdog();
    let delay = this.options.timeout;
    if (receipt) {
      this.#receiptDeadline = this.#now() + STREAM_RECEIPT_TIMEOUT_MS;
      delay = this.#fallbackReceiptTimeout ?? Math.min(delay, STREAM_RECEIPT_TIMEOUT_MS);
    }
    this.#scheduleWatchdog(delay);
  }

  #scheduleWatchdog(delay: number) {
    clearTimeout(this.#watchdog);
    const socket = this.#socket;
    const timer = setTimeout(() => {
      if (this.#watchdog !== timer || this.#socket !== socket) return;
      this.#disconnect(this.#transportError('Stream hello or acknowledgement timed out.', undefined, true), 1006);
      socket?.terminate?.(); try { socket?.close(); } catch {}
    }, Math.max(0, delay));
    this.#watchdog = timer; timer.unref?.();
  }

  #liveness() {
    this.#armHeartbeat(this.#helloValue?.limits.heartbeat_interval_ms ?? 30_000);
    // Public control pings can arrive between fragments. They renew inactivity,
    // never the original receipt deadline; opening and hello are not extended.
    if (this.#socket?.on && this.#receiptDeadline !== undefined && (this.#ack || this.#scheduled)) {
      this.#scheduleWatchdog(Math.min(this.options.timeout, this.#receiptDeadline - this.#now()));
    }
  }
  #armHeartbeat(interval: number) {
    clearTimeout(this.#heartbeat);
    if (!(Number.isFinite(interval) && interval > 0)) { this.#stop(new StreamProtocolError('Server heartbeat interval is invalid.'), 1002); return; }
    const socket = this.#socket;
    // A fragmented message can start just before the next JSON heartbeat is due.
    // Include that phase offset for transports that cannot observe native pings.
    const delay = this.#fallbackReceiptTimeout === undefined ? interval * 2
      : this.#fallbackReceiptTimeout + Math.min(interval, STREAM_FALLBACK_HEARTBEAT_GRACE_MS);
    const timer = setTimeout(() => {
      if (this.#heartbeat !== timer || this.#socket !== socket) return;
      this.#disconnect(this.#transportError('Server heartbeat was lost.', undefined, true), 1006);
      socket?.terminate?.(); try { socket?.close(); } catch {}
    }, delay);
    this.#heartbeat = timer;
    this.#heartbeat.unref?.();
  }

  #stop(error: Error, code: number) {
    if (this.#stopped) return;
    this.#stopped = true;
    // Overflow during publish takes priority over a fatal frame's reserved owner.
    this.#terminal = error;
    const socket = this.#socket;
    this.#disconnect(error, code);
    try { socket?.close(code, 'protocol'); } catch { socket?.terminate?.(); }
  }

  #transportError(message: string, cause?: unknown, timeout = false): APIConnectionError {
    return new APIConnectionError(message, { cause, request_sent: this.#requestSent, ...(timeout ? { type: 'timeout_error' } : {}) });
  }

  #disconnect(error: Error, code: number, peerClosed = false) {
    if (this.#disconnecting) return;
    if (!this.#socket && !this.#connecting && !this.#closed && !this.#terminal) return;
    if (error instanceof ConnectionLost && code === 1006) error = this.#transportError(error.message);
    if (error instanceof APIConnectionError) this.#connectionFailure ??= error;
    const failure = this.#terminal ?? this.#connectionFailure ?? error;
    // Own teardown before callbacks, without marking retryable disconnects stopped.
    this.#disconnecting = true;
    let reconnectAt: number | undefined;
    try {
      const detach = this.#detachSocket; this.#detachSocket = undefined; detach?.(peerClosed);
      this.#clearWatchdog(); clearTimeout(this.#heartbeat); clearTimeout(this.#reconnectTimer);
      for (const pending of this.#pacing) {
        clearTimeout(pending.timer);
        pending.reject(new ConnectionLost('Stream disconnected while waiting to send.'));
      }
      this.#pacing.clear();
      this.#heartbeat = undefined;
      this.#reconnectTimer = undefined;
      if (this.#openingDeadline !== undefined) this.#lastOpeningError = failure instanceof ConnectionLost
        ? new StreamDisconnectedError(`Stream closed (${code}).`) : failure;
      const streamId = this.#streamId;
      if (this.#ambiguous.size && streamId) {
        const entries = [...this.#ambiguous].sort(([a], [b]) => a - b);
        const possible: PossiblyDecided = { type: 'possibly_decided', stream_id: streamId, state_versions: entries.map(([version]) => version), event_ids: entries.map(([, id]) => id) };
        this.#publish(possible);
      }
      // A callback may close future calls without replacing this connection's
      // ambiguity. Keep protocol/API ownership and a newly raised overflow.
      const decisionFailure = this.#terminal instanceof StreamDisconnectedError && this.#terminal.closeCode === 1008
        ? this.#terminal : failure instanceof StreamProtocolError || failure instanceof SystemZeroError ? failure : undefined;
      for (const timer of this.#timers) timer.result.reject(decisionFailure ?? new StreamDisconnectedError(timer.ack?.scheduled === false && !this.#ambiguous.has(timer.version)
        ? 'State was not scheduled for a decision on this connection.'
        : 'Connection lost before an observed decision; it may have completed and been counted.'));
      this.#connectionFailure = undefined;
      this.#ambiguous.clear();
      this.#explicitIntentVersion = undefined; this.#scheduledPriorWork = false;
      this.#timers.clear();
      this.#socket = undefined; this.#connecting = false; ++this.#generation;
      this.#hello?.reject(failure);
      const exhausted = this.#openingDeadline !== undefined && (this.#openingAttempts >= this.options.maxReconnectAttempts || this.#now() >= this.#openingDeadline);
      const fatal = exhausted || this.#closed || this.#terminal !== undefined || (code !== 1000 && !retryableCodes.has(code));
      if (fatal) {
        this.#terminal ??= failure instanceof ConnectionLost ? new StreamDisconnectedError(`Stream closed permanently (${code}).`) : failure;
        this.#stopped = true;
        this.#resetOpening();
        this.#ready?.reject(this.#terminal);
        this.#ack?.reject(this.#terminal); this.#ack = undefined;
        this.#scheduled?.reject(this.#terminal); this.#scheduled = undefined;
        for (const mutation of this.#mutations) mutation.ack.reject(this.#terminal);
        this.#mutations = [];
        const queueFailure = this.#terminal instanceof StreamDisconnectedError && this.#terminal.closeCode === 1008 ? this.#terminal : undefined;
        for (const queue of this.#queues) queue.end(queueFailure, this.#closed);
        // Peer-ended queues remain reachable for a later caller close until drained.
        if (this.#closed) this.#queues.clear();
        return;
      }
      // New commands wait for a new hello and replay ack; explicit decide commands are never replayed.
      // Initial connect callers keep waiting through transient opening failures.
      if (code === 1000) { this.#ready?.reject(new StreamDisconnectedError('Stream closed normally.')); this.#ready = deferred(); }
      else if (!this.#ready || this.#ready.settled) this.#ready = deferred();
      this.#ack?.reject(code === 1000 ? new StreamDisconnectedError('Stream closed normally before acknowledgement.') : new ConnectionLost('Stream disconnected before acknowledgement.')); this.#ack = undefined;
      this.#scheduled?.reject(failure instanceof SystemZeroError ? failure : new StreamDisconnectedError('Stream disconnected before scheduling was acknowledged.')); this.#scheduled = undefined;
      // CLOSE-CODES.md: normal idle closure reopens only for the next caller send.
      if (code === 1000) return;
      const jitter = code === 1001 ? 0 : Math.max(0, Math.min(1, this.#random())) * Math.min(30_000, 500 * 2 ** Math.min(16, this.#retryAttempt++));
      reconnectAt = Math.max(this.#now() + jitter, this.#retryNotBefore);
    } finally { this.#disconnecting = false; }
    // Scheduling can exhaust the opening budget and must be able to finish teardown.
    if (reconnectAt !== undefined) this.#scheduleReconnect(reconnectAt);
  }
}
