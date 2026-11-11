import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import * as sdk from '../src/index.js';
import { Webhooks, verifyWebhookSignature } from '../src/webhooks.js';
import { WEBHOOK_ERROR_CODES, WebhookInvalidError, WebhookLimitExceededError, WebhookNotFoundError, WebhookUnavailableError } from '../src/webhook-errors.js';

const management = JSON.parse(readFileSync(new URL('../../fixtures/webhooks/management.json', import.meta.url), 'utf8'), (key, value) => ['secret', 'signature'].includes(key) && Array.isArray(value) ? String.fromCharCode(...value) : value);
const signatures = JSON.parse(readFileSync(new URL('../../fixtures/webhooks/signatures.json', import.meta.url), 'utf8'), (key, value) => ['secret', 'signature'].includes(key) && Array.isArray(value) ? String.fromCharCode(...value) : value);
const endpoint = management.endpoint;
const request = { url: endpoint.url as string, events: ['decision.completed'] as const };
const createRequest = () => ({ ...request, events: [...request.events] });
const response = (body: unknown, status = 200, headers: Record<string, string> = {}) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json', ...headers },
});
const unavailable = () => response({ error: { message: 'SYNTHETIC unavailable', type: 'overloaded_error', code: 'webhook_unavailable', retryable: true } },
  503, { 'x-should-retry': 'true', 'retry-after-ms': '0' });
const capacityUnavailable = () => response({ error: { message: 'SYNTHETIC capacity', type: 'server_error', code: 'service_unavailable', retryable: true } },
  503, { 'retry-after-ms': '5' });

describe('public webhook SDK surface through the existing HTTP transport', () => {
  it('exports the resource, receiver helper, limits and all typed webhook errors', () => {
    expect(sdk.Webhooks).toBe(Webhooks);
    expect(sdk.verifyWebhookSignature).toBe(verifyWebhookSignature);
    expect(sdk.WEBHOOK_ERROR_CODES).toBe(WEBHOOK_ERROR_CODES);
    expect(sdk.WebhookInvalidError).toBe(WebhookInvalidError);
    expect(sdk.WebhookNotFoundError).toBe(WebhookNotFoundError);
    expect(sdk.WebhookLimitExceededError).toBe(WebhookLimitExceededError);
    expect(sdk.WebhookUnavailableError).toBe(WebhookUnavailableError);
    expect(sdk.WEBHOOK_MAX_BODY_BYTES).toBe(16384);
    expect(sdk.WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS).toBe(300);
    expect(sdk.verifyWebhookSignature(signatures.body, signatures.signature, signatures.secret, { now: signatures.timestamp })).toBe(true);
  });

  it('uses public client.webhooks for all five operations, authentication and response metadata', async () => {
    const payloads = [
      { ...endpoint, secret: (signatures.secret) }, { object: 'list', data: [endpoint] },
      { id: endpoint.id, deleted: true }, { ...endpoint, secret: (signatures.secret) }, management.deliveries,
    ];
    const sent: { url: string; init: RequestInit }[] = [];
    const fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      sent.push({ url: String(url), init: init! });
      return response(payloads[sent.length - 1], sent.length === 1 ? 201 : 200, {
        'x-request-id': 'req_webhook', 'x-client-request-id': 'client_webhook',
        'x-ratelimit-limit': '100', 'x-ratelimit-remaining': '99', 'x-ratelimit-reset': '1770000060', 'x-latency-ms': '7',
      });
    });
    const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', baseURL: 'https://gateway.example/v1', fetch });
    expect(client.webhooks).toBeInstanceOf(sdk.Webhooks);
    const created = await client.webhooks.create(createRequest(), { clientRequestId: 'client_webhook', headers: { 'x-custom': 'synthetic' } }).withResponse();
    expect(created.data).toEqual(payloads[0]);
    expect(created.response.status).toBe(201);
    expect(await created.response.json()).toEqual(payloads[0]);
    expect(created.metadata).toEqual({ dangerZone: [], idempotencyKey: null, requestId: 'req_webhook', clientRequestId: 'client_webhook',
      rateLimit: { limit: 100, remaining: 99, reset: 1770000060 }, latencyMs: 7, replayed: false });
    await expect(client.webhooks.list()).resolves.toEqual(payloads[1]);
    await expect(client.webhooks.delete(endpoint.id)).resolves.toEqual(payloads[2]);
    await expect(client.webhooks.rotateSecret(endpoint.id)).resolves.toEqual(payloads[3]);
    await expect(client.webhooks.deliveries(endpoint.id, { limit: 1 })).resolves.toEqual(payloads[4]);
    expect(sent.map(({ url, init }) => [init.method, url])).toEqual([
      ['POST', 'https://gateway.example/v1/system-zero/webhooks'], ['GET', 'https://gateway.example/v1/system-zero/webhooks'],
      ['DELETE', `https://gateway.example/v1/system-zero/webhooks/${endpoint.id}`],
      ['POST', `https://gateway.example/v1/system-zero/webhooks/${endpoint.id}/rotate-secret`],
      ['GET', `https://gateway.example/v1/system-zero/webhooks/${endpoint.id}/deliveries?limit=1`],
    ]);
    for (const { init } of sent) {
      const headers = new Headers(init.headers);
      expect(headers.get('authorization')).toBe('Bearer rnsk_synthetic');
      expect(headers.get('user-agent')).toBe(`system-zero-typescript/${sdk.VERSION} node/${process.versions.node}`);
      expect(headers.has('idempotency-key')).toBe(false);
      expect(init.redirect).toBe('error');
    }
    expect(new Headers(sent[0].init.headers).get('x-custom')).toBe('synthetic');
    expect(new Headers(sent[0].init.headers).get('x-client-request-id')).toBe('client_webhook');
    expect(sent[0].init.body).toBe(JSON.stringify(createRequest()));
    expect(sent[3].init.body).toBe('{}');
  });

  it.each(['create', 'rotateSecret'] as const)('never retries public %s after a retryable HTTP error or connection failure', async method => {
    for (const connection of [false, true]) {
      const sleep = vi.fn(async () => {});
      const fetch = vi.fn(async () => { if (connection) throw new Error('SYNTHETIC ambiguous response'); return unavailable(); });
      const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', baseURL: 'https://gateway.example/v1', maxRetries: 3, fetch, sleep });
      const result = method === 'create' ? client.webhooks.create(createRequest()) : client.webhooks.rotateSecret(endpoint.id);
      await expect(result).rejects.toBeInstanceOf(connection ? sdk.APIConnectionError : sdk.WebhookUnavailableError);
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(sleep).not.toHaveBeenCalled();
    }
  });

  it('retains bounded retries for ordinary webhook reads', async () => {
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn().mockResolvedValueOnce(capacityUnavailable()).mockResolvedValueOnce(response({ object: 'list', data: [endpoint] }));
    const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', baseURL: 'https://gateway.example/v1', maxRetries: 2, fetch, sleep, random: () => 0 });
    await expect(client.webhooks.list()).resolves.toEqual({ object: 'list', data: [endpoint] });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(sleep).toHaveBeenCalledTimes(1);
    expect(sleep).toHaveBeenCalledWith(5);
  });

  it.each(signatures.errors)('classifies public $code without losing the HTTP envelope or retry metadata', async entry => {
    const fetch = vi.fn(async () => response({ request_id: 'req_body', error: {
      message: 'SYNTHETIC refusal', code: entry.code, type: entry.type, retryable: entry.retryable, param: 'url', details: { limit: 5 },
    } }, entry.status, { 'x-request-id': 'req_header', 'x-should-retry': 'false', 'retry-after-ms': '17' }));
    const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', baseURL: 'https://gateway.example/v1', maxRetries: 3, fetch });
    const error = await client.webhooks.list().catch((caught: unknown) => caught) as sdk.SystemZeroError;
    expect(error).toBeInstanceOf(sdk.SystemZeroError);
    expect(error).toMatchObject({ name: entry.class_name, code: entry.code, status: entry.status, type: entry.type,
      retryable: entry.retryable, param: 'url', details: { limit: 5 }, request_id: 'req_body' });
    expect(error.headers.get('x-request-id')).toBe('req_header');
    expect(error.headers.get('retry-after-ms')).toBe('17');
    expect(error.headers.get('x-should-retry')).toBe('false');
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('refuses caller idempotency headers before using the public transport', () => {
    const fetch = vi.fn();
    const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', baseURL: 'https://gateway.example/v1', fetch });
    expect(() => client.webhooks.list({ headers: { 'Idempotency-Key': 'forbidden' } })).toThrow(sdk.WebhookInvalidError);
    expect(fetch).not.toHaveBeenCalled();
  });

  it('preserves ordinary decide retry bytes and its generated idempotency key', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(capacityUnavailable()).mockResolvedValueOnce(response({ object: 'synthetic_decision' }));
    const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', baseURL: 'https://gateway.example/v1', maxRetries: 2,
      fetch, sleep: async () => {}, random: () => 0 });
    const body = { state: 'SYNTHETIC state', questions: [] } as unknown as sdk.DecideRequest;
    await client.decide(body);
    expect(fetch).toHaveBeenCalledTimes(2);
    const first = fetch.mock.calls[0][1] as RequestInit;
    const second = fetch.mock.calls[1][1] as RequestInit;
    expect(first.body).toBe(JSON.stringify(body));
    expect(second.body).toBe(first.body);
    const key = new Headers(first.headers).get('idempotency-key');
    expect(key).toMatch(/^sz-sdk-/);
    expect(new Headers(second.headers).get('idempotency-key')).toBe(key);
    expect(fetch.mock.calls[0][0]).toBe('https://gateway.example/v1/system-zero/decide');
  });
});

describe('webhook transport behavior', () => {
  for (const method of ['list', 'deliveries'] as const) {
    const read = (client: sdk.SystemZeroClient, options: sdk.WebhookDeliveryRequestOptions) => method === 'list'
      ? client.webhooks.list(options) : client.webhooks.deliveries(endpoint.id, options);

    it(`${method} preserves request context and caller options`, async () => {
      const fetch = vi.fn(async () => response({ object: 'list', data: [] }));
      const onAttempt = vi.fn();
      const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', fetch });
      const options = sdk.withRetryContext({ limit: 2, headers: { 'x-custom': 'SYNTHETIC' } } as sdk.WebhookDeliveryRequestOptions, { onAttempt });
      const before = JSON.stringify(options);
      await read(client, options);
      expect(onAttempt).toHaveBeenCalledTimes(1);
      expect(JSON.stringify(options)).toBe(before);
      const init = (fetch.mock.calls as unknown as [unknown, RequestInit][])[0]![1];
      expect(init.body).toBeUndefined();
      expect(new Headers(init.headers).has('limit')).toBe(false);
    });

    it(`${method} keeps workflow recovery restrictions`, async () => {
      const fetch = vi.fn().mockResolvedValueOnce(capacityUnavailable()).mockResolvedValueOnce(response({ object: 'list', data: [] }));
      const sleep = vi.fn(async () => {});
      const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', fetch, sleep, maxRetries: 2, random: () => 0 });
      await expect(read(client, sdk.withRetryContext({ limit: 2 } as sdk.WebhookDeliveryRequestOptions, { workflowRecovery: true }))).rejects.toMatchObject({ code: 'service_unavailable' });
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(sleep).not.toHaveBeenCalled();
    });

    it(`${method} respects an exhausted admission deadline before transport`, async () => {
      const fetch = vi.fn(async () => response({ object: 'list', data: [] }));
      const onDeadline = vi.fn();
      const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', fetch });
      await expect(read(client, sdk.withRetryContext({ limit: 2 } as sdk.WebhookDeliveryRequestOptions, { remaining: () => 0, onDeadline }))).rejects.toBeInstanceOf(sdk.APITimeoutError);
      expect(fetch).not.toHaveBeenCalled();
      expect(onDeadline).toHaveBeenCalledTimes(1);
    });

    it(`${method} honors the per-call retry budget`, async () => {
      const fetch = vi.fn(async () => capacityUnavailable());
      const sleep = vi.fn(async () => {});
      const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', fetch, sleep, maxRetries: 2, random: () => 0 });
      await expect(read(client, { limit: 2, retryBudget: 0 })).rejects.toMatchObject({ code: 'service_unavailable' });
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(sleep).not.toHaveBeenCalled();
    });
  }

  it.each(['create', 'rotateSecret'] as const)('%s never repeats even eligible capacity failures with context retries', async method => {
    const fetch = vi.fn(async () => capacityUnavailable());
    const sleep = vi.fn(async () => {}), onAttempt = vi.fn();
    const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', fetch, sleep, maxRetries: 5, random: () => 0 });
    const options = sdk.withRetryContext({ retryBudget: 1000 }, { maxRetries: 5, onAttempt });
    await expect(method === 'create' ? client.webhooks.create(createRequest(), options)
      : client.webhooks.rotateSecret(endpoint.id, options)).rejects.toMatchObject({ code: 'service_unavailable' });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(onAttempt).toHaveBeenCalledTimes(1);
    expect(sleep).not.toHaveBeenCalled();
  });

  it('bounds retries of a marked webhook failure without changing its retryable metadata', async () => {
    const fetch = vi.fn(async () => unavailable()), sleep = vi.fn(async () => {});
    const client = new sdk.SystemZeroClient({ apiKey: 'rnsk_synthetic', fetch, sleep, maxRetries: 3 });
    await expect(client.webhooks.list()).rejects.toMatchObject({ name: 'WebhookUnavailableError', code: 'webhook_unavailable', retryable: true });
    expect(fetch).toHaveBeenCalledTimes(4);
    expect(sleep).toHaveBeenCalledTimes(3);
  });


});
