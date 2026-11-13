import { expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import * as sdk from '../src/index.js';
import type { Fetch } from '../src/client.js';

const request = { model: 'rightnow-decision', state: 'ready', questions: { q: { type: 'boolean' as const, instructions: 'Ready?' } } };

it('offers the package stream URL and protocol while the shim offers the legacy pair', async () => {
  const { SystemZeroStreamClient } = await import('../../shims/typescript/stream.js');
  for (const [Client, path, protocol] of [
    [sdk.RightNowStreamClient, 'decision-model', 'rightnow-decision.stream.v1'],
    [SystemZeroStreamClient, 'system-zero', 'system-zero.stream.v1'],
  ] as const) {
    const offered: unknown[] = [];
    class Socket extends EventEmitter {
      readyState = 0; protocol = '';
      constructor(url: string, protocols: string[]) {
        super(); offered.push(url, protocols);
        queueMicrotask(() => this.emit('error', new Error('synthetic disconnect')));
      }
      close() {} terminate() {} send() {}
    }
    const client = new Client({ apiKey: 'test-key', WebSocket: Socket, maxReconnectAttempts: 1 });
    try {
      await expect(client.connect()).rejects.toBeInstanceOf(sdk.APIConnectionError);
      expect(offered).toEqual([`wss://api.rightnowai.co/v1/${path}/stream`, [protocol]]);
    } finally { await client.close(); }
  }
});

it('sends the new decision path, model and explicit class header', async () => {
  const fetch = vi.fn<Fetch>(async (url, init) => {
    expect(url).toBe('https://api.rightnowai.co/v1/decision-model/decide');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init!.body as string).model).toBe('rightnow-decision');
    expect(new Headers(init?.headers).get('RightNow-Danger-Zone')).toBe('long_context_64k');
    expect(new Headers(init?.headers).has('SZ-Danger-Zone')).toBe(false);
    return Response.json({});
  });
  await new sdk.RightNowClient({ apiKey: 'test-key', fetch }).decide(request, { dangerZone: 'long_context_64k' });
  expect(fetch).toHaveBeenCalledOnce();
});

it('reads the new class answer first and the old answer when it is the only one', async () => {
  for (const headers of [
    { 'rightnow-danger-zone': 'long_context_64k', 'sz-danger-zone': 'accept' },
    { 'sz-danger-zone': 'long_context_1m' },
  ]) {
    const client = new sdk.RightNowClient({ apiKey: 'test-key', fetch: async () => Response.json({}, { headers }) });
    expect((await client.decide(request).withResponse()).metadata.dangerZone).toEqual([
      headers['rightnow-danger-zone'] ?? headers['sz-danger-zone'],
    ]);
  }
});

it('keeps the shim decide path, model and class header', async () => {
  const { SystemZeroClient } = await import('../../shims/typescript/index.js');
  const fetch = vi.fn<Fetch>(async (url, init) => {
    expect(url).toBe('https://api.rightnowai.co/v1/system-zero/decide');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init!.body as string).model).toBe('system-zero');
    expect(new Headers(init?.headers).get('SZ-Danger-Zone')).toBe('long_context_64k');
    expect(new Headers(init?.headers).has('RightNow-Danger-Zone')).toBe(false);
    return Response.json({});
  });
  await new SystemZeroClient({ apiKey: 'test-key', fetch }).decide({ ...request, model: 'system-zero' }, { dangerZone: 'long_context_64k' });
  expect(fetch).toHaveBeenCalledOnce();
});

it('exports RightNow classes and identical deprecated aliases', () => {
  expect(sdk.RightNowClient).toBeTypeOf('function');
  expect(sdk.SystemZeroClient).toBe(sdk.RightNowClient);
  expect(sdk.SystemZeroStreamClient).toBe(sdk.RightNowStreamClient);
  expect(sdk.SystemZeroError).toBe(sdk.RightNowError);
});

it.each([
  [undefined, undefined, 'legacy-key'],
  [undefined, 'new-key', 'new-key'],
  ['explicit-key', 'new-key', 'explicit-key'],
] as const)('prefers explicit and new environment keys (%s, %s)', async (explicit, current, expected) => {
  vi.stubEnv('SYSTEM_ZERO_API_KEY', 'legacy-key');
  vi.stubEnv('RIGHTNOW_API_KEY', current);
  const fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    expect(new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined)).get('authorization')).toBe(`Bearer ${expected}`);
    return new Response(JSON.stringify({ object: 'list', data: [] }), { status: 200 });
  });
  const client = new sdk.RightNowClient({ ...(explicit === undefined ? {} : { apiKey: explicit }), fetch });
  await client.models.list();
  expect(fetch).toHaveBeenCalledOnce();
  class Socket extends EventEmitter {
    readyState = 0;
    protocol = 'system-zero.stream.v1';
    constructor(_url: string, _protocol: string, options: { headers: Record<string, string> }) {
      super();
      expect(options.headers.Authorization).toBe(`Bearer ${expected}`);
      queueMicrotask(() => this.emit('error', new Error('synthetic disconnect')));
    }
    close() {} terminate() {} send() {}
  }
  const stream = new sdk.RightNowStreamClient({ ...(explicit === undefined ? {} : { apiKey: explicit }), WebSocket: Socket as never, maxReconnectAttempts: 1 });
  try { await expect(stream.connect()).rejects.toBeInstanceOf(sdk.APIConnectionError); }
  finally { await stream.close(); }
});
