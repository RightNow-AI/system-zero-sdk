import { TEST_STREAM_LIMITS } from './stream-limit-fixture';
import { EventEmitter } from 'node:events';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SystemZeroClient } from '../src/client.js';
import { SystemZeroStreamClient } from '../src/stream.js';

const request = { model: 'system-zero', state: 'SYNTHETIC', questions: { q: { type: 'boolean' as const, instructions: 'Ready?' } } };
const protocol = 'system-zero.stream.v1';
const clients: SystemZeroStreamClient[] = [];
const turns = async () => { for (let count = 0; count < 30; count++) await Promise.resolve(); };
type Refusal = { status: number; code: string; retry: boolean; wait?: number };
type Opening = 'hello' | 'loss' | 'silent' | Refusal;
const unavailable: Refusal = { status: 503, code: 'limiter_unavailable', retry: true };
const limit: Refusal = { status: 429, code: 'stream_limit_exceeded', retry: true, wait: 60_000 };

function fixture(steps: Opening[] = ['hello'], options: Record<string, unknown> = {}) {
  const sockets: Socket[] = [];
  class Socket extends EventEmitter {
    readyState = 0;
    readonly protocol = protocol;
    readonly id: string;
    seq = 0;
    mode = 'on_change';
    autoAck = true;
    frames: Record<string, unknown>[] = [];
    constructor() {
      super();
      const opening = steps[Math.min(sockets.length, steps.length - 1)];
      this.id = `szws_${String(sockets.length + 1).padStart(32, '0')}`;
      sockets.push(this);
      queueMicrotask(() => {
        if (opening === 'loss') { this.close(1006); return; }
        if (opening === 'silent') return;
        if (opening === 'hello') {
          this.readyState = 1;
          this.server({ type: 'hello', protocol, request_id: 'req_open', limits: { ...TEST_STREAM_LIMITS, heartbeat_interval_ms: 30_000 }, rate_limit: null });
          return;
        }
        const response = Object.assign(new EventEmitter(), {
          statusCode: opening.status,
          headers: { 'x-should-retry': String(opening.retry), ...(opening.wait === undefined ? {} : { 'retry-after-ms': String(opening.wait) }) },
          resume() {
            this.emit('data', Buffer.from(JSON.stringify({ error: { type: opening.status === 429 ? 'rate_limit_error' : 'api_error',
              code: opening.code, message: 'SYNTHETIC opening refusal', param: null, retryable: opening.retry,
              ...(opening.code === 'stream_limit_exceeded' ? { details: { cause: 'account_concurrency', limit: 1 } } : {}) }, request_id: `req_${sockets.length}` })));
            this.emit('end');
          },
        });
        this.emit('unexpected-response', {}, response);
      });
    }
    send(text: string) {
      const frame = JSON.parse(text) as Record<string, unknown>; this.frames.push(frame);
      if (frame.type === 'start') this.mode = String(frame.decision_mode);
      if (this.autoAck && typeof frame.event_id === 'string') queueMicrotask(() => this.ack(frame.event_id as string));
    }
    ack(eventId: string, scheduled = this.mode === 'on_change') {
      this.server({ type: 'ack', event_id: eventId, state_version: Number(eventId.slice(4)), state_bytes: 10, synthesized_body_bytes: 100, scheduled });
    }
    server(frame: Record<string, unknown>) { this.emit('message', Buffer.from(JSON.stringify({ stream_id: this.id, seq: ++this.seq, ...frame })), false); }
    close(code = 1000) { if (this.readyState === 3) return; this.readyState = 3; this.emit('close', code); }
    terminate() { this.close(1006); }
  }
  const client = new SystemZeroStreamClient({ apiKey: 'rnsk_synthetic', streamURL: 'wss://sdk.example.com/v1/system-zero/stream',
    WebSocket: Socket, now: () => Date.now(), random: () => 1, ...options });
  clients.push(client);
  return { client, sockets };
}

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(1_700_000_000_000); });
afterEach(async () => { for (const client of clients.splice(0)) await client.close(); vi.clearAllTimers(); vi.useRealTimers(); });

describe('bounded stream recovery', () => {
  it('B3 rejects every pending caller with the last typed error after the caller attempt limit', async () => {
    const h = fixture([unavailable], { maxReconnectAttempts: 3 });
    const errors: unknown[] = [];
    const outcomes = [h.client.connect(), h.client.start(request), h.client.decide()].map((promise, index) => promise.catch(error => { errors[index] = error; }));
    await vi.advanceTimersByTimeAsync(2_000);
    expect(h.sockets).toHaveLength(3);
    expect(errors).toHaveLength(3); await Promise.all(outcomes);
    expect(errors[0]).toMatchObject({ status: 503, code: 'limiter_unavailable', retryable: true });
    expect(errors[1]).toBe(errors[0]); expect(errors[2]).toBe(errors[0]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('B3 bounds a hung opening by total elapsed time as well as attempt count', async () => {
    const h = fixture(['silent'], { reconnectTimeout: 50, timeout: 1_000 });
    let settled = false;
    void h.client.connect().catch(() => { settled = true; });
    await vi.advanceTimersByTimeAsync(49); expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1); expect(settled).toBe(true);
    expect(h.sockets).toHaveLength(1); expect(h.sockets[0].readyState).toBe(3);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('B3 immediately returns the refusal when Retry-After exceeds the remaining time budget', async () => {
    const h = fixture([{ ...unavailable, wait: 2_000 }], { reconnectTimeout: 1_000 });
    let failure: unknown;
    void h.client.connect().catch(error => { failure = error; }); await turns();
    expect(failure).toMatchObject({ status: 503, code: 'limiter_unavailable' });
    expect(h.sockets).toHaveLength(1); expect(vi.getTimerCount()).toBe(0);
  });

  it('B3 resets both consecutive-opening budgets after an accepted hello', async () => {
    const h = fixture(['loss', 'loss', 'hello', 'loss', 'loss', 'hello'], { maxReconnectAttempts: 3, reconnectTimeout: 4_000 });
    const first = h.client.connect(); void first.catch(() => {});
    await vi.advanceTimersByTimeAsync(1_500); await first;
    expect(h.sockets).toHaveLength(3);
    h.sockets[2].close(1006);
    const second = h.client.connect(); void second.catch(() => {});
    await vi.advanceTimersByTimeAsync(3_500);
    expect(h.sockets).toHaveLength(6); await second;
  });

  it('X5 retries an automatic stale-lease refusal after its wait and restores state', async () => {
    const h = fixture(['hello', limit, 'hello']); await h.client.start(request);
    h.sockets[0].server({ type: 'goodbye', reason: 'store_unavailable', state_version: 1, last_decision_state_version: 0,
      resume: { supported: false, action: 'reconnect_and_replay', retry_after_ms: 500 } });
    h.sockets[0].close(1013);
    const recovery = h.client.connect(); void recovery.catch(() => {});
    await vi.advanceTimersByTimeAsync(500); expect(h.sockets).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(59_999); expect(h.sockets).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1); await recovery;
    expect(h.sockets).toHaveLength(3);
    expect(h.sockets[2].frames[0]).toMatchObject({ type: 'start', request: { state: 'SYNTHETIC' } });
  });

  it('X5 keeps a first-connect stream limit terminal', async () => {
    const h = fixture([limit]);
    await expect(h.client.connect()).rejects.toMatchObject({ name: 'StreamLimitError', retry_after_ms: 60_000 });
    await vi.advanceTimersByTimeAsync(180_000);
    expect(h.sockets).toHaveLength(1); expect(vi.getTimerCount()).toBe(0);
  });
});

describe('state replay idempotency', () => {
  it('B4 preserves the accepted state key and request bytes on reconnect, then gives new state a new key', async () => {
    const h = fixture(); await h.client.start(request);
    const first = h.sockets[0].frames[0];
    expect(first.idempotency_key).toEqual(expect.stringMatching(/^[!-~]{32,255}$/));
    h.sockets[0].close(1006); await vi.advanceTimersByTimeAsync(500);
    const replay = h.sockets[1].frames[0];
    expect(replay.idempotency_key).toBe(first.idempotency_key);
    expect(JSON.stringify(replay.request)).toBe(JSON.stringify(first.request));
    await h.client.appendText(' new');
    const mutation = h.sockets[1].frames[1];
    expect(mutation.idempotency_key).not.toBe(first.idempotency_key);
    expect(mutation.idempotency_key).toEqual(expect.stringMatching(/^[!-~]{32,255}$/));
  });

  it('B4 reuses a lost-ACK candidate key and preserves accepted ownership after a rejected candidate', async () => {
    const h = fixture(); await h.client.start(request);
    h.sockets[0].autoAck = false;
    const append = h.client.appendText(' kept'); void append.catch(() => {}); await turns();
    const candidate = h.sockets[0].frames[1].idempotency_key;
    expect(candidate).toEqual(expect.any(String));
    h.sockets[0].close(1006); await vi.advanceTimersByTimeAsync(500); await append;
    expect(h.sockets[1].frames[0]).toMatchObject({ idempotency_key: candidate, request: { state: 'SYNTHETIC kept' } });
    h.sockets[1].autoAck = false;
    const rejected = h.client.appendText(' rejected'); const refusal = rejected.catch(error => error); await turns();
    h.sockets[1].server({ type: 'error', phase: 'protocol', state_version: 1, event_id: 'evt_2', covered_event_ids: [], http_status: 400,
      envelope: { error: { message: 'SYNTHETIC invalid', type: 'invalid_request_error', code: 'invalid_request', param: null, retryable: false }, request_id: 'req_invalid' },
      retry: { should_retry: false, retry_after_ms: null, retry_after: null }, fatal: false });
    expect(await refusal).toMatchObject({ code: 'invalid_request' });
    h.sockets[1].close(1006); await vi.advanceTimersByTimeAsync(500);
    expect(h.sockets[2].frames[0]).toMatchObject({ idempotency_key: candidate, request: { state: 'SYNTHETIC kept' } });
  });
});

describe('SDK timing option bounds', () => {
  it.each([0, -1, NaN, Infinity, 2_147_483_648, 2_500_000_000, Number.MAX_SAFE_INTEGER])('X9 rejects invalid stream timeout %s', timeout => {
    expect(() => fixture(['hello'], { timeout })).toThrow(RangeError);
  });
  it.each([0, -1, NaN, Infinity, 2_147_483_648])('X9 rejects invalid reconnect timeout %s', reconnectTimeout => {
    expect(() => fixture(['hello'], { reconnectTimeout })).toThrow(RangeError);
  });
  it.each([0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1])('B3 rejects invalid opening attempt count %s', maxReconnectAttempts => {
    expect(() => fixture(['hello'], { maxReconnectAttempts })).toThrow(RangeError);
  });
  it.each(['timeout', 'maxRetryWait'] as const)('X9 rejects an overflowing HTTP %s option', option => {
    expect(() => new SystemZeroClient({ apiKey: 'rnsk_synthetic', [option]: 2_500_000_000 })).toThrow(RangeError);
  });
  it('X9 accepts the maximum representable timer delay', () => {
    expect(() => fixture(['hello'], { timeout: 2_147_483_647, reconnectTimeout: 2_147_483_647 })).not.toThrow();
    expect(() => new SystemZeroClient({ apiKey: 'rnsk_synthetic', timeout: 2_147_483_647, maxRetryWait: 2_147_483_647 })).not.toThrow();
  });
});
