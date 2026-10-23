import { TEST_STREAM_LIMITS } from './stream-limit-fixture';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SystemZeroStreamClient } from '../src/stream.js';
import { RateLimitError, responseError } from '../src/errors.js';

const request = { model: 'system-zero', state: 'SYNTHETIC', questions: { ready: { type: 'boolean' as const, instructions: 'Ready?' } } };
const protocol = 'system-zero.stream.v1';
const reply = { id: 'szd_synthetic', answers: {} };
const closeFixtures: { code: number; reconnect: string; goodbye: { reason: string | null; resume: { retry_after_ms: number }; [key: string]: unknown } }[] = JSON.parse(readFileSync(new URL('../../fixtures/stream/close-codes.json', import.meta.url), 'utf8'));

class Socket extends EventEmitter {
  static connections: Socket[] = [];
  static sendFailure = false;
  static failOpening = false;
  static upgradeRefusal: { status: number; headers: Record<string, string>; body: unknown } | undefined;
  readyState = 0;
  protocol = protocol;
  frames: Record<string, unknown>[] = [];
  seq = 0;
  id: string;
  constructor(readonly url: string, readonly protocols: string[], readonly options: { headers: Record<string, string> }) {
    super();
    this.id = `szws_${String(Socket.connections.length + 1).padStart(32, '0')}`;
    Socket.connections.push(this);
    queueMicrotask(() => {
      if (Socket.upgradeRefusal) {
        const refusal = Socket.upgradeRefusal; Socket.upgradeRefusal = undefined;
        const response = Object.assign(new EventEmitter(), {
          statusCode: refusal.status, headers: refusal.headers,
          resume() { queueMicrotask(() => { this.emit('data', Buffer.from(JSON.stringify(refusal.body))); this.emit('end'); }); },
        });
        this.emit('unexpected-response', {}, response); return;
      }
      if (Socket.failOpening) { Socket.failOpening = false; this.close(1006); return; }
      this.readyState = 1;
      this.emit('open');
      this.server({ type: 'hello', protocol, request_id: 'req_open', rate_limit: null, limits: { ...TEST_STREAM_LIMITS, heartbeat_interval_ms: 30_000, max_duration_ms: 3_600_000 } });
    });
  }
  send(text: string) {
    if (Socket.sendFailure) { Socket.sendFailure = false; throw new Error('SYNTHETIC send fault'); }
    this.frames.push(JSON.parse(text) as Record<string, unknown>);
  }
  close(code = 1000) { this.readyState = 3; this.emit('close', code, Buffer.from('SYNTHETIC')); }
  terminate() { this.close(1006); }
  server(frame: Record<string, unknown>) { this.emit('message', Buffer.from(JSON.stringify({ stream_id: this.id, seq: ++this.seq, ...frame })), false); }
  ack(version: number, scheduled = true) { this.server({ type: 'ack', event_id: `evt_${version}`, state_version: version, state_bytes: 11, synthesized_body_bytes: 100, scheduled }); }
  decision(version: number, ids: string[] = []) { this.server({ type: 'decision', state_version: version, covered_event_ids: ids, reply, request_id: 'req_decision', http_status: 200, server_timing: null, latency_ms: null, rate_limit: null, queue_ms: 0, decide_ms: 5 }); }
}

const clients: SystemZeroStreamClient[] = [];
const make = (options = {}) => {
  const client = new SystemZeroStreamClient({ apiKey: 'rnsk_synthetic', streamURL: 'wss://sdk.example.com/v1/system-zero/stream', WebSocket: Socket, random: () => 0, ...options });
  clients.push(client);
  return client;
};
const flush = async () => { for (let count = 0; count < 12; count++) await Promise.resolve(); };
const first = () => Socket.connections[0]!;
const start = async (client: SystemZeroStreamClient, mode: 'on_change' | 'on_request' = 'on_change') => {
  await client.connect();
  const pending = client.start(request, { decisionMode: mode });
  await flush();
  first().ack(1, mode === 'on_change');
  return pending;
};

function livenessSocket(receiptTimeout = 240_000) {
  return class extends Socket {
    override server(frame: Record<string, unknown>) {
      super.server(frame.type === 'hello' ? { ...frame, limits: { ...(frame.limits as Record<string, number>),
        native_heartbeat_interval_ms: 10_000, receipt_timeout_ms: receiptTimeout } } : frame);
    }
  };
}
const LivenessSocket = livenessSocket();

function eventTargetSocket(ServerSocket: typeof Socket) {
  return class {
    readonly transport: Socket;
    constructor(url: string, protocols: string[], options: { headers: Record<string, string> }) {
      this.transport = new ServerSocket(url, protocols, options);
    }
    get readyState() { return this.transport.readyState; }
    get protocol() { return this.transport.protocol; }
    send(text: string) { this.transport.send(text); }
    close(code = 1000) { this.transport.close(code); }
    terminate() { this.transport.terminate(); }
    addEventListener(type: string, listener: (event: { data?: string; code?: number }) => void) {
      if (type === 'message') this.transport.on(type, (data: Buffer) => listener({ data: data.toString('utf8') }));
      else if (type === 'close') this.transport.on(type, (code: number) => listener({ code }));
      else this.transport.on(type, () => listener({}));
    }
  };
}

beforeEach(() => { Socket.connections = []; Socket.sendFailure = false; Socket.failOpening = false; Socket.upgradeRefusal = undefined; });
afterEach(async () => { for (const client of clients.splice(0)) await client.close(); vi.restoreAllMocks(); vi.useRealTimers(); });

it('keeps attachment-shaped append items as JSON state rather than top-level media', async () => {
  const client = make();
  await client.connect();
  const initial = client.start({ ...request, state: [] }, { decisionMode: 'on_request' });
  await flush(); first().ack(1, false); await initial;
  const item = { name: 'picture', type: 'image', data: 'not-a-media-carrier' };
  const appended = client.appendItems([item]);
  await flush();
  expect(first().frames[1]).toMatchObject({ type: 'append_items', items: [item] });
  expect(first().frames[1]).not.toHaveProperty('attachments');
  expect(first().frames[0]).not.toHaveProperty('request.attachments');
  first().ack(2, false); await appended;
});

describe('fragment liveness deadlines', () => {
  const receiptBound = 3_818_000;
  const makeLive = (WebSocket = LivenessSocket) => {
    vi.useFakeTimers(); vi.setSystemTime(1_700_000_000_000);
    return make({ WebSocket, now: () => Date.now() });
  };

  it('uses public native pings for heartbeat liveness and still expires a dead link', async () => {
    const client = makeLive(); await start(client);
    for (let index = 0; index < 7; index++) {
      await vi.advanceTimersByTimeAsync(10_000);
      first().emit('ping', Buffer.from('live'));
    }
    expect(first().readyState).toBe(1);
    await vi.advanceTimersByTimeAsync(59_999); expect(first().readyState).toBe(1);
    await vi.advanceTimersByTimeAsync(1); expect(first().readyState).toBe(3);
  });

  it.each([
    ['mutation', 240_000], ['decide', 240_000],
    ['mutation', Number.MAX_SAFE_INTEGER], ['decide', Number.MAX_SAFE_INTEGER],
  ] as const)('keeps the original finite %s receipt bound despite endless native pings and hello hint %s', async (kind, advertised) => {
    const client = makeLive(livenessSocket(advertised)); await start(client);
    void (kind === 'mutation' ? client.appendText('waiting') : client.decide()).catch(() => {}); await flush();
    let elapsed = 0;
    while (elapsed + 10_000 < receiptBound) {
      await vi.advanceTimersByTimeAsync(10_000);
      elapsed += 10_000;
      first().emit('ping', Buffer.from('live'));
      expect(first().readyState).toBe(1);
    }
    await vi.advanceTimersByTimeAsync(receiptBound - elapsed - 1); expect(first().readyState).toBe(1);
    await vi.advanceTimersByTimeAsync(1); expect(first().readyState).toBe(3);
  });

  it('keeps the original receipt timeout when no liveness arrives', async () => {
    const client = makeLive(); await start(client);
    void client.appendText('waiting').catch(() => {}); await flush();
    await vi.advanceTimersByTimeAsync(29_999); expect(first().readyState).toBe(1);
    await vi.advanceTimersByTimeAsync(1); expect(first().readyState).toBe(3);
  });

  it('never extends the opening hello watchdog with unauthenticated native pings', async () => {
    class NoHelloSocket extends Socket {
      override server(frame: Record<string, unknown>) { if (frame.type !== 'hello') super.server(frame); }
    }
    const client = makeLive(NoHelloSocket);
    void client.connect().catch(() => {}); await flush();
    for (let index = 0; index < 2; index++) {
      await vi.advanceTimersByTimeAsync(10_000); first().emit('ping', Buffer.from('early'));
    }
    await vi.advanceTimersByTimeAsync(9_999); expect(first().readyState).toBe(1);
    await vi.advanceTimersByTimeAsync(1); expect(first().readyState).toBe(3);
  });

  it.each([
    [false, 240_000], [true, 240_000],
    [false, Number.MAX_SAFE_INTEGER], [true, Number.MAX_SAFE_INTEGER],
  ] as const)('uses a finite EventTarget fallback, receipt=%s, hello hint=%s', async (pendingReceipt, advertised) => {
    vi.useFakeTimers(); vi.setSystemTime(1_700_000_000_000);
    const client = make({ WebSocket: eventTargetSocket(livenessSocket(advertised)), now: () => Date.now() });
    await start(client);
    if (pendingReceipt) { void client.appendText('waiting').catch(() => {}); await flush(); }
    const bound = pendingReceipt ? receiptBound : receiptBound + 30_000;
    await vi.advanceTimersByTimeAsync(bound - 1); expect(first().readyState).toBe(1);
    await vi.advanceTimersByTimeAsync(1); expect(first().readyState).toBe(3);
  });

  it('keeps legacy EventTarget receipt timing when hello does not advertise the bounded fallback', async () => {
    vi.useFakeTimers(); vi.setSystemTime(1_700_000_000_000);
    const client = make({ WebSocket: eventTargetSocket(Socket), now: () => Date.now() });
    await start(client);
    void client.appendText('waiting').catch(() => {}); await flush();
    await vi.advanceTimersByTimeAsync(29_999); expect(first().readyState).toBe(1);
    await vi.advanceTimersByTimeAsync(1); expect(first().readyState).toBe(3);
  });

  it('does not let stale native events extend a new connection receipt', async () => {
    const client = makeLive(); await start(client);
    const previous = first(); previous.close(1006);
    await vi.advanceTimersByTimeAsync(0); await flush();
    const current = Socket.connections[1]!; current.ack(1); await flush();
    void client.appendText('waiting').catch(() => {}); await flush();
    for (let index = 0; index < 2; index++) {
      await vi.advanceTimersByTimeAsync(10_000); previous.emit('ping', Buffer.from('stale'));
    }
    await vi.advanceTimersByTimeAsync(9_999); expect(current.readyState).toBe(1);
    await vi.advanceTimersByTimeAsync(1); expect(current.readyState).toBe(3);
  });
});

describe('stream protocol', () => {
  it('authenticates only on upgrade, negotiates v1, and defaults min_interval_ms to zero', async () => {
    const client = make();
    await start(client);
    expect(first().options.headers.Authorization).toBe('Bearer rnsk_synthetic');
    expect(first().protocols).toEqual([protocol]);
    expect(first().url).not.toContain('rnsk_');
    expect(first().frames).toEqual([{ type: 'start', event_id: 'evt_1', idempotency_key: expect.any(String), request: { ...request, prompt_cache_key: expect.any(String) }, decision_mode: 'on_change', min_interval_ms: 0 }]);
    expect(JSON.stringify(first().frames)).not.toContain('rnsk_');
  });
  it('waits for ack before resolving and serializes mutations using accepted versions', async () => {
    const client = make();
    await start(client);
    let resolved = false;
    const append = client.appendText(' one').then(ack => { resolved = true; return ack; });
    const replace = client.replace(['new']);
    const items = client.appendItems(['item']);
    await flush();
    expect(first().frames.at(-1)).toEqual({ type: 'append_text', event_id: 'evt_2', idempotency_key: expect.any(String), text: ' one' });
    expect(resolved).toBe(false);
    first().ack(2);
    expect((await append).state_version).toBe(2);
    await flush();
    expect(first().frames.at(-1)).toEqual({ type: 'replace', event_id: 'evt_3', idempotency_key: expect.any(String), state: ['new'] });
    first().ack(3); await replace; await flush();
    expect(first().frames.at(-1)).toEqual({ type: 'append_items', event_id: 'evt_4', idempotency_key: expect.any(String), items: ['item'] });
    first().ack(4); await items;
  });
  it('emits ack, scheduled and decisions through iterator and callbacks', async () => {
    const client = make();
    const events = client.events();
    const callback = vi.fn(); client.on('decision', callback);
    await start(client);
    expect((await events.next()).value.type).toBe('ack');
    const pending = client.decide(); await flush();
    expect(first().frames.at(-1)).toEqual({ type: 'decide', state_version: 1 });
    first().server({ type: 'scheduled', state_version: 1, in_flight: false });
    expect((await pending).state_version).toBe(1);
    expect((await events.next()).value.type).toBe('scheduled');
    first().decision(1, ['evt_1']);
    expect((await events.next()).value.reply).toEqual(reply);
    expect(callback).toHaveBeenCalledOnce();
  });
  it('answers JSON ping automatically without exposing it as a decision event', async () => {
    const client = make(); await start(client);
    first().server({ type: 'ping', nonce: 17 }); await flush();
    expect(first().frames.at(-1)).toEqual({ type: 'pong', nonce: 17 });
  });
  it('uses the successful watermark for coalesced latency timers and ignores errors', async () => {
    let now = 10;
    const client = make({ now: () => now }); await start(client);
    now = 20; const one = client.appendText(' one'); await flush(); first().ack(2); const ack1 = await one;
    const latency1 = client.waitForDecision(ack1);
    now = 30; const two = client.appendText(' two'); await flush(); first().ack(3); const ack2 = await two;
    const latency2 = client.waitForDecision(ack2);
    first().decision(1, ['evt_1']);
    first().server({ type: 'error', phase: 'decide', state_version: 3, event_id: null, covered_event_ids: ['evt_2', 'evt_3'], http_status: 503, envelope: { error: { message: 'SYNTHETIC unavailable', type: 'api_error', code: 'service_unavailable', param: null, retryable: true }, request_id: 'req_failed' }, retry: { should_retry: true, retry_after_ms: 0, retry_after: null }, rate_limit: null, server_timing: null, fatal: false });
    let resolved = false; void latency1.then(() => { resolved = true; }); await flush(); expect(resolved).toBe(false);
    now = 55; first().decision(3, ['evt_2', 'evt_3']);
    expect(await latency1).toMatchObject({ eventId: 'evt_2', stateVersion: 2, latencyMs: 35 });
    expect(await latency2).toMatchObject({ eventId: 'evt_3', stateVersion: 3, latencyMs: 25 });
  });
  it('rejects a malformed ack rather than depending on an unaccepted version', async () => {
    const client = make(); await start(client);
    const pending = client.appendText(' bad'); await flush();
    const assertion = expect(pending).rejects.toMatchObject({ name: 'StreamProtocolError' });
    first().ack(4); await assertion; await flush();
    expect(Socket.connections).toHaveLength(1);
  });
  it('reuses the next event id after a rejected mutation and removes its desired state', async () => {
    const client = make(); await start(client);
    const pending = client.appendText(' rejected'); await flush();
    const assertion = expect(pending).rejects.toMatchObject({ code: 'invalid_request' });
    first().server({ type: 'error', phase: 'protocol', state_version: 1, event_id: 'evt_2', covered_event_ids: [], http_status: 400, envelope: { error: { message: 'SYNTHETIC invalid', type: 'invalid_request_error', code: 'invalid_request', param: null, retryable: false }, request_id: 'req_error' }, retry: { should_retry: false, retry_after_ms: null, retry_after: null }, rate_limit: null, server_timing: null, fatal: false });
    await assertion;
    const valid = client.appendText(' accepted'); await flush(); expect(first().frames.at(-1)?.event_id).toBe('evt_2');
    first().ack(2); await valid;
    first().close(1006); await flush(); await vi.waitFor(() => expect(Socket.connections).toHaveLength(2));
    const replay = Socket.connections[1]!; await flush();
    expect((replay.frames[0]?.request as typeof request).state).toBe('SYNTHETIC accepted'); replay.ack(1);
  });
  it('rejects a recoverable decide error immediately without reconnecting or re-deciding', async () => {
    const client = make(); await start(client, 'on_request');
    const pending = client.decide(); await flush();
    const assertion = expect(pending).rejects.toMatchObject({ code: 'stream_version_mismatch' });
    first().server({ type: 'error', phase: 'protocol', state_version: 1, event_id: null, covered_event_ids: [], http_status: 409, envelope: { error: { message: 'SYNTHETIC version', type: 'invalid_request_error', code: 'stream_version_mismatch', param: null, retryable: false }, request_id: 'req_version' }, retry: { should_retry: false, retry_after_ms: null, retry_after: null }, rate_limit: null, server_timing: null, fatal: false });
    await assertion; expect(Socket.connections).toHaveLength(1);
    expect(first().frames.filter(frame => frame.type === 'decide')).toHaveLength(1);
  });
  it('lets ack callbacks use the latency helper on their exact ack object', async () => {
    const client = make(); let latency: ReturnType<typeof client.waitForDecision> | undefined;
    client.on('ack', ack => { latency = client.waitForDecision(ack); });
    await start(client); first().decision(1, ['evt_1']);
    expect((await latency!).stateVersion).toBe(1);
  });
  it('treats malformed retry fields as a protocol error without throwing in the socket handler', async () => {
    const client = make(); await start(client);
    expect(() => first().server({ type: 'error', phase: 'decide', state_version: 1, event_id: null, covered_event_ids: [], http_status: 429, envelope: { error: {} }, retry: {}, fatal: false })).not.toThrow();
    await expect(client.connect()).rejects.toMatchObject({ name: 'StreamProtocolError' });
  });
  it('generates one visible ASCII cache key per stream without mutating the request', async () => {
    const client = make(); await start(client);
    const generated = (first().frames[0]!.request as { prompt_cache_key?: string }).prompt_cache_key;
    expect(generated).toMatch(/^[!-~]{1,256}$/);
    expect(request).not.toHaveProperty('prompt_cache_key');
    const other = make(); await other.connect(); const pending = other.start(request); await flush();
    const socket = Socket.connections[1]!;
    expect((socket.frames[0]!.request as { prompt_cache_key?: string }).prompt_cache_key).not.toBe(generated);
    socket.ack(1); await pending;
  });
  it('keeps an explicit prompt_cache_key unchanged', async () => {
    const client = make(); await client.connect();
    const pending = client.start({ ...request, prompt_cache_key: 'cache-example' }); await flush();
    expect(first().frames[0]!.request).toMatchObject({ prompt_cache_key: 'cache-example' });
    first().ack(1); await pending;
  });
  it('maps stream_limit_exceeded to a typed rate limit error with retry fields', () => {
    const failure = responseError(429, new Headers({ 'retry-after': '2', 'retry-after-ms': '2100', 'x-should-retry': 'true' }), {
      error: { message: 'SYNTHETIC concurrent stream', type: 'rate_limit_error', code: 'stream_limit_exceeded', param: null, retryable: true, details: { cause: 'account_concurrency', limit: 1 } },
      request_id: 'req_stream_limit',
    }, false);
    expect(failure).toBeInstanceOf(RateLimitError);
    expect(failure).toMatchObject({ name: 'StreamLimitError', status: 429, code: 'stream_limit_exceeded', limit: 1, retry_after: '2', retry_after_ms: 2100, should_retry: true });
    expect(failure.headers.get('x-should-retry')).toBe('true');
  });
  it('surfaces a stream-limit upgrade refusal without retrying before or after Retry-After', async () => {
    vi.useFakeTimers();
    Socket.upgradeRefusal = { status: 429, headers: { 'retry-after': '2', 'x-should-retry': 'true' }, body: {
      error: { message: 'SYNTHETIC concurrent stream', type: 'rate_limit_error', code: 'stream_limit_exceeded', param: null, retryable: true, details: { cause: 'account_concurrency', limit: 1 } }, request_id: 'req_stream_limit',
    } };
    const client = make(); let refusal: unknown; void client.connect().catch(error => { refusal = error; });
    await flush();
    expect(refusal).toMatchObject({ name: 'StreamLimitError', limit: 1, retry_after: '2', retry_after_ms: 2000, should_retry: true });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(Socket.connections).toHaveLength(1);
    await expect(client.start(request)).rejects.toMatchObject({ name: 'StreamLimitError' });
  });
});

describe('stream reconnect', () => {
  it.each(['goodbye', 'decide_error', 'lazy_connect'] as const)('chunks a capacity wait without early reconnect after %s', async source => {
    vi.useFakeTimers();
    const client = make(); await start(client, 'on_request');
    const delay = 2_419_200_000, maximumTimer = 2_147_483_647;
    const hints = vi.fn(); client.on(source === 'decide_error' ? 'error' : 'goodbye', hints);
    const timers = vi.spyOn(globalThis, 'setTimeout');
    if (source === 'decide_error') {
      first().server({ type: 'error', phase: 'decide', state_version: 1, event_id: null, covered_event_ids: [], http_status: 429,
        envelope: { error: { message: 'SYNTHETIC capacity', type: 'rate_limit_error', code: 'capacity_exhausted', param: null, retryable: true }, request_id: 'req_capacity' },
        retry: { should_retry: true, retry_after_ms: delay, retry_after: '2419200' }, rate_limit: null, server_timing: null, fatal: true });
    } else first().server({ type: 'goodbye', reason: 'overload', state_version: 1, last_decision_state_version: 0,
      resume: { supported: false, action: 'reconnect_and_replay', retry_after_ms: delay } });
    expect(hints).toHaveBeenCalledWith(expect.objectContaining(source === 'decide_error'
      ? { retry: { should_retry: true, retry_after_ms: delay, retry_after: '2419200' } }
      : { resume: { supported: false, action: 'reconnect_and_replay', retry_after_ms: delay } }));
    first().close(source === 'lazy_connect' ? 1000 : 1013);
    if (source === 'lazy_connect') void client.connect();
    expect(timers.mock.calls.at(-1)?.[1]).toBe(maximumTimer);
    expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(maximumTimer);
    expect(Socket.connections).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(1);
    expect(timers.mock.calls.at(-1)?.[1]).toBe(delay - maximumTimer);
    await vi.advanceTimersByTimeAsync(delay - maximumTimer - 1);
    expect(Socket.connections).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(Socket.connections).toHaveLength(2);
    Socket.connections[1]!.ack(1, false);
    expect(timers.mock.calls.every(call => typeof call[1] === 'number' && call[1] <= maximumTimer)).toBe(true);
  });

  it('cancels a chunked reconnect wait when the client closes', async () => {
    vi.useFakeTimers(); const client = make(); await start(client, 'on_request');
    first().server({ type: 'goodbye', reason: 'overload', state_version: 1, last_decision_state_version: 0,
      resume: { supported: false, action: 'reconnect_and_replay', retry_after_ms: 2_419_200_000 } });
    first().close(1013);
    await client.close();
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(2_419_200_000);
    expect(Socket.connections).toHaveLength(1);
  });

  it.each(closeFixtures)('preserves the shared goodbye and reconnects according to $code/$goodbye.reason', async ({ code, reconnect, goodbye }) => {
    vi.useFakeTimers();
    const client = make({ random: () => 0.5 });
    const received = vi.fn(); client.on('goodbye', received);
    await start(client, 'on_request');
    first().server({ ...goodbye, stream_id: first().id });
    expect(received).toHaveBeenCalledWith({ ...goodbye, stream_id: first().id });
    first().close(code);
    if (reconnect === 'never' || reconnect === 'lazy') {
      await vi.advanceTimersByTimeAsync(1000);
      expect(Socket.connections).toHaveLength(1);
      if (reconnect === 'never') {
        await expect(client.appendText(' later')).rejects.toBeInstanceOf(Error);
      } else {
        const pending = client.appendText(' later'); await flush();
        Socket.connections[1]!.ack(1, false); await pending;
        expect(Socket.connections[1]!.frames[0]!.request).toMatchObject({ state: 'SYNTHETIC later' });
      }
    } else {
      const delay = Math.max(goodbye.resume.retry_after_ms, reconnect === 'immediate' ? 0 : 250);
      if (delay > 0) {
        await vi.advanceTimersByTimeAsync(delay - 1);
        expect(Socket.connections).toHaveLength(1);
        await vi.advanceTimersByTimeAsync(1);
      } else await vi.advanceTimersByTimeAsync(0);
      expect(Socket.connections).toHaveLength(2);
      expect(Socket.connections[1]!.frames[0]!.request).toEqual(first().frames[0]!.request);
      Socket.connections[1]!.ack(1, false);
    }
  });

  it.each([false, true])('preserves a fresh explicit intent when the old decision precedes scheduled (receipt=%s)', async receipt => {
    const client = make(); await start(client, 'on_request');
    const possible = vi.fn(); client.on('possibly_decided', possible);
    const old = client.decide(); await flush(); first().server({ type: 'scheduled', state_version: 1, in_flight: false }); await old;
    const repeat = client.decide(); void repeat.catch(() => {}); await flush();
    first().decision(1, ['evt_1']);
    if (receipt) { first().server({ type: 'scheduled', state_version: 1, in_flight: false }); await repeat; }
    first().close(1006);
    expect(possible).toHaveBeenCalledWith(expect.objectContaining({ state_versions: [1], event_ids: ['evt_1'] }));
  });
  it('reports later on_request mutations that an unresolved explicit intent may dispatch', async () => {
    const client = make(); await start(client, 'on_request');
    const possible = vi.fn(); client.on('possibly_decided', possible);
    const decide = client.decide(); await flush(); first().server({ type: 'scheduled', state_version: 1, in_flight: false }); await decide;
    const appended = client.appendText(' possibly included'); await flush(); first().ack(2, false); await appended;
    first().close(1006);
    expect(possible).toHaveBeenCalledWith(expect.objectContaining({ state_versions: [1, 2], event_ids: ['evt_1', 'evt_2'] }));
  });
  it('stops with the typed server error when replay start rejects the desired state', async () => {
    vi.useFakeTimers(); const client = make(); await start(client);
    const pending = client.appendText(' invalid desired state'); await flush();
    let rejection: unknown; void pending.catch(error => { rejection = error; });
    first().close(1006); await vi.advanceTimersByTimeAsync(0); await flush();
    expect(Socket.connections).toHaveLength(2);
    const replay = Socket.connections[1]!;
    replay.server({ type: 'error', phase: 'protocol', state_version: 0, event_id: 'evt_1', covered_event_ids: [], http_status: 413, envelope: { error: { message: 'SYNTHETIC state too large', type: 'invalid_request_error', code: 'state_too_large', param: 'state', retryable: false }, request_id: 'req_replay_invalid' }, retry: { should_retry: false, retry_after_ms: null, retry_after: null }, rate_limit: null, server_timing: null, fatal: false });
    await flush();
    expect(rejection).toMatchObject({ name: 'InvalidRequestError', code: 'state_too_large', status: 413 });
    await expect(client.connect()).rejects.toMatchObject({ code: 'state_too_large' });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(Socket.connections).toHaveLength(2);
  }, 2000);
  it('replays complete desired state once and never appends an unacked mutation twice', async () => {
    const client = make(); await start(client);
    const pending = client.appendText(' pending'); await flush();
    first().close(1006); await vi.waitFor(() => expect(Socket.connections).toHaveLength(2)); await flush();
    const replay = Socket.connections[1]!;
    expect(replay.frames).toEqual([{ type: 'start', event_id: 'evt_1', idempotency_key: expect.any(String), request: { ...request, state: 'SYNTHETIC pending', prompt_cache_key: expect.any(String) }, decision_mode: 'on_change', min_interval_ms: 0 }]);
    replay.ack(1); expect((await pending).state_version).toBe(1); await flush(); expect(replay.frames).toHaveLength(1);
  });
  it('keeps connect callers pending through a transient opening failure', async () => {
    Socket.failOpening = true; const client = make();
    const pending = client.connect();
    await vi.waitFor(() => expect(Socket.connections).toHaveLength(2));
    expect((await pending).protocol).toBe(protocol);
  });
  it('reports on_change ambiguity before never policy has actually replayed a connection', async () => {
    Socket.failOpening = true; const client = make({ reconnectPolicy: 'never' });
    const possible = vi.fn(); client.on('possibly_decided', possible);
    await client.connect(); const initial = client.start(request); await flush();
    const socket = Socket.connections[1]!; socket.ack(1); await initial;
    const append = client.appendText(' retained'); await flush(); socket.ack(2); await append;
    socket.close(1006);
    expect(possible).toHaveBeenCalledWith(expect.objectContaining({ state_versions: [1, 2] }));
  });
  it('replays the whole queued desired state after a synchronous send failure', async () => {
    const client = make(); await start(client); Socket.sendFailure = true;
    const one = client.appendText(' one'); const two = client.appendText(' two');
    await vi.waitFor(() => expect(Socket.connections).toHaveLength(2)); await flush();
    const replay = Socket.connections[1]!;
    expect((replay.frames[0]?.request as typeof request).state).toBe('SYNTHETIC one two');
    replay.ack(1); expect((await one).state_version).toBe(1); expect((await two).state_version).toBe(1);
    expect(replay.frames).toHaveLength(1);
  });
  it.each([1002, 1003, 1007, 1008, 1009])('never reconnects after permanent close %i', async code => {
    vi.useFakeTimers(); const client = make(); await start(client); first().close(code); await vi.advanceTimersByTimeAsync(0);
    expect(Socket.connections).toHaveLength(1);
    await expect(client.appendText(' later')).rejects.toBeInstanceOf(Error);
  });
  it('leaves normal close idle until the next send and then replays with the same cache key', async () => {
    vi.useFakeTimers(); const client = make(); await start(client, 'on_request');
    const key = (first().frames[0]!.request as { prompt_cache_key?: string }).prompt_cache_key;
    expect(key).toMatch(/^[!-~]{1,256}$/);
    first().close(1000); await vi.advanceTimersByTimeAsync(1000);
    expect(Socket.connections).toHaveLength(1);
    const pending = client.appendText(' after idle'); await flush();
    const replay = Socket.connections[1]!;
    expect(replay.frames).toHaveLength(1);
    expect(replay.frames[0]!.request).toMatchObject({ state: 'SYNTHETIC after idle', prompt_cache_key: key });
    replay.ack(1, false); await pending;
  });
  it.each([1001, 1006, 1011, 1012, 1013])('reconnects and replays after retryable close %i while honoring goodbye delay', async code => {
    vi.useFakeTimers(); const client = make(); await start(client, 'on_request');
    first().server({ type: 'goodbye', reason: 'shutdown', state_version: 1, last_decision_state_version: 0, resume: { supported: false, action: 'reconnect_and_replay', retry_after_ms: 500 } });
    first().close(code); await vi.advanceTimersByTimeAsync(499); expect(Socket.connections).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1); expect(Socket.connections).toHaveLength(2);
    const replay = Socket.connections[1]!;
    expect(replay.frames[0]!.request).toEqual(first().frames[0]!.request);
    replay.ack(1, false);
  });
  it('reconnects immediately on maximum lifetime even when jitter is nonzero', async () => {
    vi.useFakeTimers(); const client = make({ random: () => 1 }); await start(client, 'on_request');
    first().close(1001); await vi.advanceTimersByTimeAsync(0);
    expect(Socket.connections).toHaveLength(2); Socket.connections[1]!.ack(1, false);
  });
  it('keeps a retryable failed version possibly_decided until a later same-version success', async () => {
    vi.useFakeTimers(); const client = make(); const possible = vi.fn(); client.on('possibly_decided', possible);
    const ack = await start(client), latency = client.waitForDecision(ack);
    first().server({ type: 'error', phase: 'decide', state_version: 1, event_id: null, covered_event_ids: ['evt_1'], http_status: 503, envelope: { error: { message: 'SYNTHETIC retry', type: 'api_error', code: 'service_unavailable', param: null, retryable: true }, request_id: 'req_retry' }, retry: { should_retry: true, retry_after_ms: 0, retry_after: null }, rate_limit: null, server_timing: null, fatal: false });
    let settled = false; void latency.then(() => { settled = true; }); await flush(); expect(settled).toBe(false);
    first().decision(1, ['evt_1']); expect((await latency).stateVersion).toBe(1);
    first().close(1000); await flush();
    expect(possible).not.toHaveBeenCalled();
  });
  it('reports possible billing if disconnected during a server-side retry of the same version', async () => {
    vi.useFakeTimers(); const client = make(); const possible = vi.fn(); client.on('possibly_decided', possible);
    await start(client);
    first().server({ type: 'error', phase: 'decide', state_version: 1, event_id: null, covered_event_ids: ['evt_1'], http_status: 503, envelope: { error: { message: 'SYNTHETIC retry', type: 'api_error', code: 'service_unavailable', param: null, retryable: true }, request_id: 'req_retry' }, retry: { should_retry: true, retry_after_ms: 0, retry_after: null }, rate_limit: null, server_timing: null, fatal: false });
    first().close(1000);
    expect(possible).toHaveBeenCalledWith(expect.objectContaining({ state_versions: [1], event_ids: ['evt_1'] }));
  });
  it('tracks a retryable failed version again after an earlier same-version success', async () => {
    vi.useFakeTimers(); const client = make(); const possible = vi.fn(); client.on('possibly_decided', possible);
    await start(client); first().decision(1, ['evt_1']);
    first().server({ type: 'error', phase: 'decide', state_version: 1, event_id: null, covered_event_ids: ['evt_1'], http_status: 503, envelope: { error: { message: 'SYNTHETIC repeated retry', type: 'api_error', code: 'service_unavailable', param: null, retryable: true }, request_id: 'req_retry' }, retry: { should_retry: true, retry_after_ms: 0, retry_after: null }, rate_limit: null, server_timing: null, fatal: false });
    first().close(1000);
    expect(possible).toHaveBeenCalledWith(expect.objectContaining({ state_versions: [1], event_ids: ['evt_1'] }));
  });
  it('reports ambiguous versions before replay and rejects their old latency wait', async () => {
    const client = make(); const possible = vi.fn(); client.on('possibly_decided', possible);
    const ack = await start(client); const timer = client.waitForDecision(ack);
    const assertion = expect(timer).rejects.toMatchObject({ name: 'APIConnectionError' });
    first().close(1006); await assertion;
    expect(possible).toHaveBeenCalledWith(expect.objectContaining({ type: 'possibly_decided', stream_id: first().id, state_versions: [1], event_ids: ['evt_1'] }));
    await vi.waitFor(() => expect(Socket.connections).toHaveLength(2)); Socket.connections[1]!.ack(1);
  });
  it('measures the replay start ack even when there are no pending logical mutations', async () => {
    const client = make(); await start(client);
    let latency: ReturnType<typeof client.waitForDecision> | undefined;
    client.on('ack', ack => { latency = client.waitForDecision(ack); });
    first().close(1006); await vi.waitFor(() => expect(Socket.connections).toHaveLength(2)); await flush();
    const replay = Socket.connections[1]!; replay.ack(1); replay.decision(1, ['evt_1']);
    expect((await latency!).decision.stream_id).toBe(replay.id);
  });
  it('never policy restores state on_request and leaves re-deciding to the caller', async () => {
    const client = make({ reconnectPolicy: 'never' }); await start(client);
    first().close(1006); await vi.waitFor(() => expect(Socket.connections).toHaveLength(2)); await flush();
    expect(Socket.connections[1]!.frames[0]?.decision_mode).toBe('on_request');
    Socket.connections[1]!.ack(1, false); await flush(); expect(Socket.connections[1]!.frames).toHaveLength(1);
  });
  it('honors a longer goodbye wait rather than shortening it to jitter', async () => {
    vi.useFakeTimers(); const client = make({ random: () => 0.5 }); await start(client);
    first().server({ type: 'goodbye', reason: 'maintenance', state_version: 1, last_decision_state_version: 0, resume: { supported: false, action: 'reconnect_and_replay', retry_after_ms: 300_000 } });
    first().close(1012); await vi.advanceTimersByTimeAsync(299_999); expect(Socket.connections).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1); expect(Socket.connections).toHaveLength(2); Socket.connections[1]!.ack(1);
  });
  it('reconnects after two missing heartbeat intervals even while other frames arrive', async () => {
    vi.useFakeTimers(); const client = make(); await start(client);
    await vi.advanceTimersByTimeAsync(30_000); first().decision(1, ['evt_1']);
    await vi.advanceTimersByTimeAsync(30_001); expect(Socket.connections).toHaveLength(2); Socket.connections[1]!.ack(1);
  });
  it('retains a 429 Retry-After for reconnect without automatically repeating decide', async () => {
    vi.useFakeTimers(); const client = make(); await start(client, 'on_request');
    const scheduled = client.decide(); await flush(); first().server({ type: 'scheduled', state_version: 1, in_flight: false }); await scheduled;
    first().server({ type: 'error', phase: 'decide', state_version: 1, event_id: null, covered_event_ids: ['evt_1'], http_status: 429, envelope: { error: { message: 'SYNTHETIC rate', type: 'rate_limit_error', code: 'rate_limit_exceeded', param: null, retryable: true }, request_id: 'req_rate' }, retry: { should_retry: true, retry_after_ms: 2000, retry_after: '2' }, rate_limit: null, server_timing: null, fatal: false });
    first().close(1006); await vi.advanceTimersByTimeAsync(1999); expect(Socket.connections).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1); expect(Socket.connections).toHaveLength(2); Socket.connections[1]!.ack(1, false); await flush(); expect(Socket.connections[1]!.frames).toHaveLength(1);
  });
});
