import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SystemZeroClient, type ClientOptions } from '../src/client.js';
import { retryWaitMs, shouldRetry } from '../src/retry.js';

interface PolicyFixture {
  name: string; status: number; code: string | null; retryable: boolean | null;
  headers: Record<string, string>; retry: boolean;
}
interface WaitFixture {
  name: string; headers: Record<string, string>; retryIndex: number;
  random: number; nowMs: number; waitMs: number;
}
const policies: PolicyFixture[] = JSON.parse(readFileSync(new URL('../../fixtures/retry/policy.json', import.meta.url), 'utf8'));
const waits: WaitFixture[] = JSON.parse(readFileSync(new URL('../../fixtures/retry/waits.json', import.meta.url), 'utf8'));
const request = { model: 'system-zero', state: 'SYNTHETIC', questions: { ready: { type: 'boolean' as const, instructions: 'Ready?' } } };
const reply = { id: 'szd_synthetic', answers: {} };
const ok = () => Response.json(reply);
const capacity = (headers: HeadersInit = {}) => Response.json({ error: { code: 'capacity_exhausted', retryable: true } }, { status: 429, headers });
const makeClient = (fetch: typeof globalThis.fetch, options: Partial<ClientOptions> = {}) => new SystemZeroClient({
  apiKey: 'SYNTHETIC', fetch, random: () => 0.5, sleep: async () => {}, ...options,
});

afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

describe('shared SDK capacity retry fixtures', () => {
  it.each(policies)('policy: $name', fixture => {
    expect(shouldRetry(fixture.status, fixture.code, new Headers(fixture.headers), fixture.retryable ?? undefined, 0)).toBe(fixture.retry);
  });

  it.each(waits)('wait: $name', fixture => {
    expect(retryWaitMs(new Headers(fixture.headers), fixture.retryIndex, () => fixture.random, fixture.nowMs)).toBe(fixture.waitMs);
  });

  it.each(policies)('HTTP outcome: $name', async fixture => {
    const error: Record<string, unknown> = { code: fixture.code, message: 'SYNTHETIC refusal' };
    if (fixture.retryable !== null) error.retryable = fixture.retryable;
    const fetch = vi.fn().mockResolvedValueOnce(Response.json({ error }, { status: fixture.status, headers: fixture.headers })).mockImplementationOnce(ok);
    const call = makeClient(fetch).decide(request);
    if (fixture.retry) await expect(call).resolves.toEqual(reply);
    else await expect(call).rejects.toMatchObject({ status: fixture.status, code: fixture.code });
    expect(fetch).toHaveBeenCalledTimes(fixture.retry ? 2 : 1);
  });
});

describe('HTTP capacity attempt and key bounds', () => {
  it('defaults to five retries and a sixty-second call budget', () => {
    expect(makeClient(async () => ok()).options).toMatchObject({ maxRetries: 5, retryBudget: 60_000 });
  });

  it.each([undefined, 99])('never exceeds six attempts when maxRetries is %s', async maxRetries => {
    const fetch = vi.fn(async () => capacity());
    await expect(makeClient(fetch, { maxRetries }).decide(request)).rejects.toMatchObject({ code: 'capacity_exhausted' });
    expect(fetch).toHaveBeenCalledTimes(6);
  });

  it('keeps exactly one key and serialized body through five capacity refusals', async () => {
    const keys: (string | null)[] = [];
    const bodies: unknown[] = [];
    const fetch = vi.fn(async (_url, init) => {
      keys.push(new Headers(init?.headers).get('idempotency-key'));
      bodies.push(init?.body);
      return keys.length < 6 ? capacity() : ok();
    });
    await expect(makeClient(fetch).decide(request)).resolves.toEqual(reply);
    expect(fetch).toHaveBeenCalledTimes(6);
    expect(keys[0]).toMatch(/^sz-sdk-/u);
    expect(new Set(keys).size).toBe(1);
    expect(new Set(bodies)).toEqual(new Set([JSON.stringify(request)]));
  });

  it.each(['', '   '])('does not retry a decision with an empty caller key %j', async idempotencyKey => {
    const fetch = vi.fn(async () => capacity());
    await expect(makeClient(fetch).decide(request, { idempotencyKey })).rejects.toMatchObject({ code: 'capacity_exhausted' });
    expect(fetch).toHaveBeenCalledOnce();
  });

  it('retains a caller-supplied header key across capacity retries', async () => {
    const keys: (string | null)[] = [];
    const fetch = vi.fn(async (_url, init) => {
      keys.push(new Headers(init?.headers).get('idempotency-key'));
      return keys.length === 1 ? capacity() : ok();
    });
    await makeClient(fetch).decide(request, { headers: { 'Idempotency-Key': 'given-key' } });
    expect(keys).toEqual(['given-key', 'given-key']);
  });

  it('permits a read-only models retry without an idempotency key', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(capacity()).mockResolvedValueOnce(Response.json({ object: 'list', data: [] }));
    await expect(makeClient(fetch).models.list()).resolves.toEqual({ object: 'list', data: [] });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(new Headers(fetch.mock.calls[0]![1]?.headers).has('idempotency-key')).toBe(false);
  });

  it.each(['authentication_error', 'permission_error'])('never hides an envelope %s behind a contradictory capacity status', async type => {
    const fetch = vi.fn(async () => Response.json({ error: { type, code: 'service_unavailable', retryable: true } }, { status: 503 }));
    await expect(makeClient(fetch).decide(request)).rejects.toMatchObject({ type, status: 503 });
    expect(fetch).toHaveBeenCalledOnce();
  });
});

describe('HTTP per-call capacity budget', () => {
  it.each(['client', 'request'] as const)('zero %s budget disables retries and automatic keys', async scope => {
    const fetch = vi.fn(async () => capacity());
    const client = makeClient(fetch, scope === 'client' ? { retryBudget: 0 } : {});
    await expect(client.decide(request, scope === 'request' ? { retryBudget: 0 } : {})).rejects.toMatchObject({ code: 'capacity_exhausted' });
    expect(fetch).toHaveBeenCalledOnce();
    expect(new Headers(fetch.mock.calls[0]![1]?.headers).has('idempotency-key')).toBe(false);
  });

  it('can enable retries for one request when the client budget is zero', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(capacity()).mockImplementationOnce(ok);
    await expect(makeClient(fetch, { retryBudget: 0 }).decide(request, { retryBudget: 1000 })).resolves.toEqual(reply);
    expect(fetch).toHaveBeenCalledTimes(2);
    const first = new Headers(fetch.mock.calls[0]![1]?.headers).get('idempotency-key');
    expect(first).toMatch(/^sz-sdk-/u);
    expect(new Headers(fetch.mock.calls[1]![1]?.headers).get('idempotency-key')).toBe(first);
  });

  it('counts elapsed attempts and backoff against one request budget', async () => {
    let now = 0;
    const sleep = vi.fn(async (wait: number) => { now += wait; });
    const fetch = vi.fn(async () => { now += 200; return capacity(); });
    await expect(makeClient(fetch, { monotonic: () => now, sleep }).decide(request, { retryBudget: 600 })).rejects.toMatchObject({ code: 'capacity_exhausted' });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(sleep.mock.calls).toEqual([[250]]);
    expect(now).toBe(650);
  });

  it('does not sleep or dispatch when Retry-After consumes all remaining budget', async () => {
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn(async () => capacity({ 'Retry-After': '1' }));
    await expect(makeClient(fetch, { monotonic: () => 0, sleep, retryBudget: 1000 }).decide(request)).rejects.toMatchObject({ status: 429, code: 'capacity_exhausted' });
    expect(fetch).toHaveBeenCalledOnce();
    expect(sleep).not.toHaveBeenCalled();
  });

  it('does not retry an initial answer that exhausted the recovery budget', async () => {
    let now = 0;
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn(async () => { now += 1000; return capacity(); });
    await expect(makeClient(fetch, { monotonic: () => now, sleep, retryBudget: 500 }).decide(request)).rejects.toMatchObject({ code: 'capacity_exhausted' });
    expect(fetch).toHaveBeenCalledOnce();
    expect(sleep).not.toHaveBeenCalled();
  });

  it('checks the budget again after an oversleep before dispatching', async () => {
    let now = 0;
    const fetch = vi.fn(async () => capacity());
    const sleep = vi.fn(async () => { now = 1000; });
    await expect(makeClient(fetch, { monotonic: () => now, sleep, retryBudget: 500 }).decide(request)).rejects.toMatchObject({ code: 'capacity_exhausted' });
    expect(fetch).toHaveBeenCalledOnce();
    expect(sleep).toHaveBeenCalledOnce();
  });

  it('uses the remaining budget as the next attempt deadline', async () => {
    vi.useFakeTimers(); vi.setSystemTime(0);
    const fetch = vi.fn().mockResolvedValueOnce(capacity()).mockImplementationOnce(() => new Promise<Response>(() => {}));
    const call = makeClient(fetch, {
      timeout: 10_000, retryBudget: 500, monotonic: Date.now,
      sleep: wait => new Promise(resolve => setTimeout(resolve, wait)),
    }).decide(request).catch(error => error);
    await vi.advanceTimersByTimeAsync(501);
    expect(await call).toMatchObject({ name: 'APIConnectionError' });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it.each([0, 5])('retains the original timeout with a %i ms recovery budget', async retryBudget => {
    vi.useFakeTimers(); vi.setSystemTime(0);
    const fetch = vi.fn(() => new Promise<Response>(() => {}));
    let settled = false;
    const call = makeClient(fetch, { timeout: 20, retryBudget }).decide(request).catch(error => { settled = true; return error; });
    await vi.advanceTimersByTimeAsync(19);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(await call).toMatchObject({ name: 'APIConnectionError' });
    expect(fetch).toHaveBeenCalledOnce();
  });

  it.each([-1, NaN, Infinity, 2_147_483_648])('rejects invalid client and per-call budgets %s', retryBudget => {
    expect(() => makeClient(async () => ok(), { retryBudget })).toThrow(RangeError);
    expect(() => makeClient(async () => ok()).decide(request, { retryBudget })).toThrow(RangeError);
  });
});
