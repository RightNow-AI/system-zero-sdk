import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import * as sdk from '../src/index.js';
import { SYSTEM_ZERO_ERRORS } from '../src/integrations/error-catalog.js';
import { createToolHandler } from '../src/integrations/function-tools/index.js';

const request = {
  model: 'system-zero', state: 'SYNTHETIC state',
  questions: {
    zebra: { type: 'boolean' as const, instructions: 'Is this synthetic?' },
    alpha: { type: 'boolean' as const, instructions: 'Is this a test?' },
  },
};
const toolArguments = {
  state: { json: '{"status":"ready"}' },
  questions: [{ name: 'ready', type: 'boolean', instructions: 'Is the status ready?' }],
};
const callLimitRefusal = {
  status: 'error', request_id: null,
  error: { code: 'call_limit_reached', message: 'The local call limit has been reached.' },
};

describe('live HTTP and local helper behavior', () => {
  it('preserves request order, identity headers and separate response metadata', async () => {
    const reply = { id: 'szd_00000000000000000000000000000000', answers: {} };
    const response = Response.json(reply, { headers: {
      'X-Request-Id': 'req_synthetic', 'X-Client-Request-Id': 'client-synthetic',
      'X-RateLimit-Limit': '100', 'X-RateLimit-Remaining': '99', 'X-RateLimit-Reset': '1800000000',
      'X-Latency-Ms': '12.5', 'X-RightNow-Idempotent-Replay': 'true',
    } });
    const transport = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      expect(new URL(String(url)).pathname).toBe('/v1/system-zero/decide');
      expect(init?.method).toBe('POST');
      expect(init?.body).toBe(JSON.stringify(request));
      const headers = new Headers(init?.headers);
      expect(headers.get('authorization')).toBe('Bearer SYNTHETIC');
      expect(headers.get('idempotency-key')).toBe('stable-synthetic-key');
      expect(headers.get('x-client-request-id')).toBe('client-synthetic');
      expect(headers.get('x-synthetic')).toBe('preserved');
      return response;
    });
    const client = new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport, maxRetries: 0 });
    const call = client.decide(request, {
      idempotencyKey: 'stable-synthetic-key', clientRequestId: 'client-synthetic', headers: { 'x-synthetic': 'preserved' },
    });
    const result = await call.withResponse();
    expect(result.data).toEqual(reply);
    expect(Object.keys(result.data)).toEqual(Object.keys(reply));
    expect(result.metadata).toEqual({
      requestId: 'req_synthetic', clientRequestId: 'client-synthetic', latencyMs: 12.5, replayed: true, idempotencyKey: 'stable-synthetic-key',
      rateLimit: { limit: 100, remaining: 99, reset: 1800000000 },
    });
    expect(result.response).toBe(response);
    expect(await result.response.json()).toEqual(reply);
    expect(await call).toEqual(reply);
    expect(transport).toHaveBeenCalledOnce();
  });

  it('stops a zero-count batch before transport', async () => {
    const scratchRoot = join(process.cwd(), 'tmp');
    mkdirSync(scratchRoot, { recursive: true });
    const directory = mkdtempSync(join(scratchRoot, 'live-batch-'));
    try {
      const transport = vi.fn(async () => Response.json({ answers: {} }));
      const client = new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport, maxRetries: 0 });
      const result = await sdk.runBatch(client, [{ id: 'one', request, expected: { zebra: true, alpha: true } }], {
        runId: 'synthetic-count', resultsPath: join(directory, 'results.jsonl'), maxDecideCalls: 0,
        deadlineMs: 1000, concurrency: 1,
      });
      expect(result).toEqual({ records: [], stopped: 'call_limit', decide_calls: 0 });
      expect(transport).not.toHaveBeenCalled();
    } finally {
      if (dirname(directory) !== scratchRoot) throw new Error('Scratch directory escaped its root.');
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it('refuses local call admission before transport without an API error code', async () => {
    expect(Object.keys(SYSTEM_ZERO_ERRORS)).toHaveLength(46);
    expect(Object.hasOwn(SYSTEM_ZERO_ERRORS, 'call_limit_reached')).toBe(false);
    const transport = vi.fn(async () => Response.json({ answers: {} }));
    const client = new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport, maxRetries: 0 });
    const handler = createToolHandler({ client, maxDecideCalls: 0 });
    expect(handler.budget).toEqual({ decideCalls: 0 });
    expect(await handler('system_zero_decide', toolArguments)).toEqual(callLimitRefusal);
    expect(transport).not.toHaveBeenCalled();
    expect(handler.budget).toEqual({ decideCalls: 0 });
  });

  it('retains the call count after the limit is exhausted', async () => {
    const transport = vi.fn(async () => Response.json({ answers: {} }, {
      headers: { 'x-request-id': 'req_synthetic_count' },
    }));
    const client = new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport, maxRetries: 0 });
    const handler = createToolHandler({ client, maxDecideCalls: 1 });
    expect(await handler('system_zero_decide', toolArguments)).toEqual({
      answers: {}, status: 'ok', request_id: 'req_synthetic_count',
    });
    for (let refused = 0; refused < 2; refused++) {
      expect(await handler('system_zero_decide', toolArguments)).toEqual(callLimitRefusal);
      expect(transport).toHaveBeenCalledOnce();
      expect(handler.budget).toEqual({ decideCalls: 1 });
    }
  });

  it('keeps local admission results outside remote refusal catalogs', async () => {
    const remoteError = {
      code: 'call_limit_reached', type: 'permission_error', message: 'SYNTHETIC remote refusal.', retryable: false,
    };
    for (const [status, body] of [[403, { error: remoteError }], [200, { answers: { ready: { error: remoteError } } }]] as const) {
      const transport = vi.fn(async () => Response.json(body, { status }));
      const client = new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport, maxRetries: 0 });
      const handler = createToolHandler({ client, maxDecideCalls: 1 });
      expect(await handler('system_zero_decide', toolArguments)).toMatchObject({
        status: 'error', request_id: null,
        error: { code: 'system_zero_unavailable', message: SYSTEM_ZERO_ERRORS.system_zero_unavailable[1] },
      });
      expect(transport).toHaveBeenCalledOnce();
      expect(handler.budget).toEqual({ decideCalls: 1 });
    }
  });

  it.each([
    ['invalid_api_key', 401], ['monthly_cap_reached', 429], ['idempotency_conflict', 409],
    ['auth_error', 403], ['account_suspended', 403], ['model_not_found', 404],
  ] as const)('preserves the live gateway refusal %s', async (code, status) => {
    const client = new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 0, fetch: async () => Response.json({
      request_id: 'synthetic-request', error: { code, type: 'api_error', message: 'Synthetic refusal.', retryable: false },
    }, { status }) });
    expect(await createToolHandler({ client })('system_zero_list_models', {})).toEqual({
      status: 'error', request_id: 'synthetic-request', error: { code, message: 'Synthetic refusal.' },
    });
  });

  it('rejects insecure remote bases before transport and accepts HTTPS', () => {
    const transport = vi.fn(async () => Response.json({ data: [] }));
    expect(() => new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', baseURL: 'http://sdk.example.com/v1', fetch: transport })).toThrow();
    expect(() => new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', baseURL: 'https://sdk.example.com/v1', fetch: transport })).not.toThrow();
    expect(transport).not.toHaveBeenCalled();
  });

  it('redacts injected credentials and bearer values from tool output', async () => {
    const apiKey = 'SYNTHETIC_KEY';
    const shaped = 'rnsk_' + 'Q'.repeat(40);
    const echo = `${apiKey} Bearer SYNTHETIC_BEARER ${shaped}`;
    const client = new sdk.SystemZeroClient({ apiKey, maxRetries: 0, fetch: async () => Response.json({
      request_id: echo, error: { code: 'invalid_request', type: 'invalid_request_error', message: echo, retryable: false },
    }, { status: 400 }) });
    const result = await createToolHandler({ client })('system_zero_list_models', {});
    expect(result.error?.code).toBe('invalid_request');
    for (const secret of [apiKey, 'SYNTHETIC_BEARER', shaped]) expect(JSON.stringify(result)).not.toContain(secret);
  });
});
