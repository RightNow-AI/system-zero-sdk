import { readFileSync } from 'node:fs';
import { createHmac } from 'node:crypto';
import { expect, it, vi } from 'vitest';
import * as current from '@rightnowai/rightnow';
import * as old from '../index.js';
import * as currentStream from '@rightnowai/rightnow/stream';
import * as oldStream from '../stream.js';
import * as oldTools from '../integrations/function-tools.js';
import * as currentTools from '@rightnowai/rightnow/integrations/function-tools';
import { wireFor } from '../../../typescript/src/wire.js';

it('keeps webhook management on the legacy paths', async () => {
  const calls: string[] = [];
  const client = new old.RightNowClient({ apiKey: 'test-key', fetch: async (url, init) => {
    calls.push(`${init!.method} ${String(url)}`);
    return Response.json({});
  } });
  await client.webhooks.list();
  await client.webhooks.create({ url: 'https://example.com/events', events: ['decision.refused'] });
  await client.webhooks.rotateSecret('wh_' + '1'.repeat(32));
  expect(calls).toEqual([
    'GET https://api.rightnowai.co/v1/system-zero/webhooks',
    'POST https://api.rightnowai.co/v1/system-zero/webhooks',
    `POST https://api.rightnowai.co/v1/system-zero/webhooks/wh_${'1'.repeat(32)}/rotate-secret`,
  ]);
});

it.each([
  ['rightnow-decision-3.0.0', 'model_unavailable'], ['system-zero-3.0.0', 'system_zero_unavailable'],
])('accepts signed webhook deliveries with %s names through the shim', (model, refusal_code) => {
  const secret = 'whsec_' + 'ab'.repeat(32), timestamp = 1770000000;
  const body = JSON.stringify({ id: 'evt_names', object: 'webhook_event', type: 'decision.refused',
    created: timestamp, data: { model, refusal_code } });
  const digest = createHmac('sha256', Buffer.from(secret.slice(6), 'hex')).update(`${timestamp}.${body}`).digest('hex');
  expect(old.verifyWebhookSignature(body, `t=${timestamp},v1=${digest}`, secret, { now: timestamp })).toBe(true);
  expect(JSON.parse(body).data).toEqual({ model, refusal_code });
});

it('wraps every client and preserves other root and subpath exports', () => {
  const clients = new Set(['RightNowClient', 'SystemZeroClient', 'RightNowStreamClient', 'SystemZeroStreamClient', 'STREAM_PROTOCOL']);
  for (const name of Object.keys(current)) if (!clients.has(name)) expect(old[name as keyof typeof old]).toBe(current[name as keyof typeof current]);
  expect(old.SystemZeroClient).toBe(old.RightNowClient);
  expect(oldStream.SystemZeroStreamClient).toBe(oldStream.RightNowStreamClient);
  expect(old.STREAM_PROTOCOL).toBe('system-zero.stream.v1');
  expect(oldStream.STREAM_PROTOCOL).toBe('system-zero.stream.v1');
  expect(old.RightNowClient.prototype).toBeInstanceOf(current.RightNowClient);
  expect(oldStream.RightNowStreamClient.prototype).toBeInstanceOf(currentStream.RightNowStreamClient);
  expect(oldTools.NESTED_FUNCTION_TOOLS).toBe(currentTools.NESTED_FUNCTION_TOOLS);
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
  expect(pkg.dependencies).toEqual({ '@rightnowai/rightnow': '0.5.1' });
});

it('keeps a lazily owned tool client on the legacy wire', async () => {
  vi.stubEnv('RIGHTNOW_API_KEY', 'test-key');
  const decide = vi.spyOn(current.RightNowClient.prototype, 'decide').mockImplementation(function (this: current.RightNowClient, request) {
    expect(wireFor(this).name).toBe('system-zero');
    expect(request.model).toBe('system-zero');
    return { withResponse: async () => ({ data: { answers: {} }, metadata: { requestId: null } }) } as ReturnType<current.RightNowClient['decide']>;
  });
  const handler = oldTools.createToolHandler();
  expect(await handler('system_zero_decide', {
    state: 'ready', model: null, attachments: null,
    questions: [{ name: 'q', type: 'boolean', instructions: 'Ready?', criteria: null }],
  })).toMatchObject({ status: 'ok' });
  expect(decide).toHaveBeenCalledOnce();
});

it.each([old.RightNowClient, old.SystemZeroClient])('keeps the complete legacy HTTP wire', async Client => {
  const request = { model: 'system-zero', state: 'ready', questions: { q: { type: 'boolean' as const, instructions: 'Ready?' } } };
  const client = new Client({ apiKey: 'test-key', fetch: async (url, init) => {
    expect(url).toBe('https://api.rightnowai.co/v1/system-zero/decide');
    expect(JSON.parse(init!.body as string).model).toBe('system-zero');
    const headers = new Headers(init!.headers);
    expect(headers.get('user-agent')?.startsWith('system-zero-typescript/0.5.1 node/')).toBe(true);
    expect(headers.get('sz-danger-zone')).toBe('accept');
    expect(headers.has('rightnow-danger-zone')).toBe(false);
    expect(headers.get('idempotency-key')).toMatch(/^sz-sdk-/u);
    return Response.json({});
  } });
  expect(wireFor(client).name).toBe('system-zero');
  await client.decide(request, { dangerZone: 'accept' });
  const timeout = new Client({ apiKey: 'test-key', timeout: 1, maxRetries: 0, fetch: () => new Promise(() => {}) });
  await expect(timeout.decide(request)).rejects.toMatchObject({ code: 'system_zero_timeout' });
});

it.each([old.RightNowStreamClient, old.SystemZeroStreamClient, oldStream.RightNowStreamClient, oldStream.SystemZeroStreamClient])('fixes the legacy wire for every stream export', Client => {
  const client = new Client({ apiKey: 'test-key' });
  expect(wireFor(client).name).toBe('system-zero');
  expect(client.options.streamURL).toBe('wss://api.rightnowai.co/v1/system-zero/stream');
  expect(client.protocol).toBe('system-zero.stream.v1');
  expect(client.options.idempotency).toBe('auto');
});

it('keeps the legacy environment key working through the old package', async () => {
  vi.stubEnv('RIGHTNOW_API_KEY', undefined);
  vi.stubEnv('SYSTEM_ZERO_API_KEY', 'old-key');
  const client = new old.SystemZeroClient({ fetch: async (_url, options) => {
    expect(new Headers(options?.headers).get('authorization')).toBe('Bearer old-key');
    return new Response(JSON.stringify({ object: 'list', data: [] }), { status: 200 });
  } });
  await client.models.list();
});
