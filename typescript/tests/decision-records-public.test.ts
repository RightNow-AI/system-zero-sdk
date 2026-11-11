import { readFileSync } from 'node:fs';
import { describe, expect, expectTypeOf, it, vi } from 'vitest';
import * as sdk from '../src/index.js';
import {
  GATEWAY_ERROR_CODES as PINNED_GATEWAY_CODES,
  SYSTEM_ZERO_ERROR_CODES as PINNED_SYSTEM_CODES,
} from '../src/error-codes.js';

const fixtures = new URL('../../fixtures/', import.meta.url);
const contract = JSON.parse(readFileSync(new URL('decision-records/contract.json', fixtures), 'utf8'));
const decision = JSON.parse(readFileSync(new URL('decide-v1-boolean.json', fixtures), 'utf8'));
const methods = ['getDecision', 'getDecisionTrace'] as const;
const headers = {
  'x-request-id': 'req_public_lookup', 'x-client-request-id': 'public-client',
  'x-ratelimit-limit': '120', 'x-ratelimit-remaining': '119', 'x-ratelimit-reset': '1800000000',
};
const payload = (method: typeof methods[number]) => method === 'getDecision' ? decision : contract.trace;
const path = (method: typeof methods[number]) => method === 'getDecision' ? contract.decision_path : contract.trace_path;

describe('public decision record client', () => {
  it.each(methods)('%s uses the configured GET transport and preserves response metadata', async method => {
    const response = Response.json(payload(method), { headers });
    const fetch = vi.fn<typeof globalThis.fetch>(async () => response);
    const client = new sdk.SystemZeroClient({
      apiKey: 'test-key', baseURL: 'https://sdk.example.com/v1/', fetch,
    });
    const options = {
      clientRequestId: 'public-client', headers: { authorization: 'Bearer test', 'x-synthetic': 'public' },
    };
    const call = client[method](contract.id, options);
    expect(await call).toEqual(payload(method));
    const detailed = await call.withResponse();
    expect(detailed.data).toEqual(payload(method));
    expect(detailed.metadata).toMatchObject({
      requestId: 'req_public_lookup', clientRequestId: 'public-client', replayed: false,
      rateLimit: { limit: 120, remaining: 119, reset: 1800000000 },
    });
    expect(detailed.response).toBe(response);
    expect(detailed.response.headers.has('x-rightnow-idempotent-replay')).toBe(false);
    expect(detailed.response.headers.has('x-should-retry')).toBe(false);
    expect(await detailed.response.json()).toEqual(payload(method));
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe('https://sdk.example.com/v1' + path(method));
    expect(init?.method).toBe('GET');
    expect(init?.body).toBeUndefined();
    const outgoing = new Headers(init?.headers);
    expect(outgoing.get('authorization')).toBe('Bearer test-key');
    expect(outgoing.get('x-client-request-id')).toBe('public-client');
    expect(outgoing.get('x-synthetic')).toBe('public');
    expect(outgoing.has('idempotency-key')).toBe(false);
    expect(options.headers.authorization).toBe('Bearer test');
  });

  it.each(methods)('%s reuses ordinary GET retries and server pacing', async method => {
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json({ error: {
        type: 'api_error', code: 'service_unavailable', message: 'Synthetic capacity outage', retryable: true,
      } }, { status: 503, headers: { 'retry-after-ms': '5' } }))
      .mockResolvedValueOnce(Response.json(payload(method), { headers }));
    const client = new sdk.SystemZeroClient({ apiKey: 'test-key', fetch, sleep, random: () => 0 });
    expect(await client[method](contract.id)).toEqual(payload(method));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(sleep).toHaveBeenCalledExactlyOnceWith(5);
    for (const [url, init] of fetch.mock.calls) {
      expect(url).toBe('https://api.rightnowai.co/v1' + path(method));
      expect(init?.method).toBe('GET');
      expect(init?.body).toBeUndefined();
      expect(new Headers(init?.headers).has('idempotency-key')).toBe(false);
    }
  });

  it.each(methods)('%s retries a marked store outage inside the shared retry bounds', async method => {
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn<typeof globalThis.fetch>(async () => Response.json({ error: {
      type: 'api_error', code: 'idempotency_unavailable', message: 'Synthetic store outage', retryable: true,
    } }, { status: 503, headers: { 'retry-after-ms': '5' } }));
    const client = new sdk.SystemZeroClient({ apiKey: 'test-key', fetch, sleep });
    await expect(client[method](contract.id)).rejects.toMatchObject({
      status: 503, code: 'idempotency_unavailable', retryable: true, message: 'Synthetic store outage',
    });
    expect(fetch).toHaveBeenCalledTimes(6);
    expect(sleep).toHaveBeenCalledTimes(5);
  });

  it.each(methods)('%s accepts a per-call budget without restarting the transport policy', async method => {
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json({ error: { code: 'service_unavailable', retryable: true } },
        { status: 503, headers: { 'retry-after-ms': '5' } }))
      .mockResolvedValueOnce(Response.json(payload(method)));
    const client = new sdk.SystemZeroClient({
      apiKey: 'test-key', fetch, sleep, random: () => 0, monotonic: () => 0, retryBudget: 0,
    });
    expect(await client[method](contract.id, { retryBudget: 10 })).toEqual(payload(method));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(sleep).toHaveBeenCalledExactlyOnceWith(5);
    expect(fetch.mock.calls.every(([, init]) => !new Headers(init?.headers).has('idempotency-key'))).toBe(true);
  });

  it.each(methods.flatMap(method => [0, 5].map(retryBudget => ({ method, retryBudget }))))(
    '$method surfaces the failure when a $retryBudget ms lookup budget cannot admit the server wait', async ({ method, retryBudget }) => {
      const sleep = vi.fn(async () => {});
      const fetch = vi.fn<typeof globalThis.fetch>(async () => Response.json({ error: {
        code: 'service_unavailable', retryable: true,
      } }, { status: 503, headers: { 'retry-after-ms': '5' } }));
      const client = new sdk.SystemZeroClient({
        apiKey: 'test-key', fetch, sleep, random: () => 0, monotonic: () => 0,
      });
      await expect(client[method](contract.id, { retryBudget })).rejects.toMatchObject({ status: 503, code: 'service_unavailable' });
      expect(fetch).toHaveBeenCalledOnce();
      expect(sleep).not.toHaveBeenCalled();
    },
  );

  it.each(methods)('%s returns the public typed 404 without retrying', async method => {
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn<typeof globalThis.fetch>(async () => Response.json(contract.not_found, {
      status: 404, headers: { ...contract.not_found_headers, ...headers },
    }));
    const client = new sdk.SystemZeroClient({ apiKey: 'test-key', fetch, sleep });
    const error = await client[method](contract.id).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(sdk.DecisionNotFoundError);
    if (!(error instanceof sdk.DecisionNotFoundError)) throw error;
    expect(error).toBeInstanceOf(sdk.NotFoundError);
    expect(error).toMatchObject({
      status: 404, type: 'not_found_error', code: 'decision_not_found', retryable: false,
      request_id: contract.not_found.request_id,
    });
    expect(error.headers.get('x-request-id')).toBe('req_public_lookup');
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(sleep).not.toHaveBeenCalled();
  });

  it('preserves a revocation response instead of mapping it to lookup absence', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => Response.json({
      error: { type: 'authentication_error', code: 'auth_error', message: 'Synthetic revoked key', retryable: false },
      request_id: 'req_public_revoked',
    }, { status: 403, headers: { 'x-should-retry': 'false' } }));
    const client = new sdk.SystemZeroClient({ apiKey: 'test-key', fetch });
    const error = await client.getDecision(contract.id).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(sdk.AuthenticationError);
    expect(error).toMatchObject({
      status: 403, type: 'authentication_error', code: 'auth_error', request_id: 'req_public_revoked', retryable: false,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each(contract.invalid_ids as string[])('rejects malformed ID %j before any transport call', id => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    const client = new sdk.SystemZeroClient({ apiKey: 'test-key', fetch });
    for (const method of methods) expect(() => client[method](id)).toThrow(TypeError);
    expect(fetch).not.toHaveBeenCalled();
  });

  it('exports typed resource helpers and adds the new code without dropping pinned codes', () => {
    expect(typeof sdk.createDecisionRecords).toBe('function');
    expect(typeof sdk.DecisionNotFoundError).toBe('function');
    expect(sdk.DECISION_RECORD_ERROR_CODES.decision_not_found).toEqual({ status: 404, type: 'not_found_error', retryable: false });
    expect(sdk.SYSTEM_ZERO_ERROR_CODES).toEqual(PINNED_SYSTEM_CODES);
    expect(sdk.GATEWAY_ERROR_CODES).toEqual(PINNED_GATEWAY_CODES);
    expect(new Set(Object.values(sdk.ERROR_CODES))).toEqual(new Set([
      ...PINNED_SYSTEM_CODES, ...PINNED_GATEWAY_CODES, 'decision_not_found',
    ]));
    expect(sdk.ERROR_CODES.DECISION_NOT_FOUND).toBe('decision_not_found');
    const code: sdk.KnownErrorCode = 'decision_not_found';
    expect(code).toBe(sdk.ERROR_CODES.DECISION_NOT_FOUND);
    expectTypeOf<sdk.SystemZeroClient['getDecision']>().returns.toEqualTypeOf<sdk.APIPromise<sdk.SystemZeroReply>>();
    expectTypeOf<sdk.SystemZeroClient['getDecisionTrace']>().returns.toEqualTypeOf<sdk.APIPromise<sdk.DecisionTrace>>();
    expectTypeOf<sdk.DecisionTrace['created']>().toEqualTypeOf<number>();
    expectTypeOf<sdk.DecisionTrace['model']>().toEqualTypeOf<string>();
    expectTypeOf<sdk.DecisionTrace['model_version']>().toEqualTypeOf<string>();
    expectTypeOf<sdk.DecisionTrace['route']>().toEqualTypeOf<'direct' | 'long'>();
  });
});
