import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import { APIPromise, type RequestOptions } from '../src/client.js';
import { responseError, SystemZeroError } from '../src/errors.js';
import { Webhooks, verifyWebhookSignature, type WebhookRequest, type WebhookEndpoint, type WebhookDelivery, type WebhookList } from '../src/webhooks.js';
import { WEBHOOK_ERROR_CODES, WebhookInvalidError } from '../src/webhook-errors.js';

interface SignatureCase {
  name: string; valid: boolean; timestamp?: number; now?: number; body?: string; append?: string;
  repeat?: string; count?: number; secret?: string; signature?: string;
}
const fixtures = JSON.parse(readFileSync(new URL('../../fixtures/webhooks/signatures.json', import.meta.url), 'utf8'), (key, value) => ['secret', 'signature'].includes(key) && Array.isArray(value) ? String.fromCharCode(...value) : value) as {
  secret: string; timestamp: number; body: string; signature: string; cases: SignatureCase[];
  errors: { code: keyof typeof WEBHOOK_ERROR_CODES; status: number; type: string; retryable: boolean; class_name: string }[];
};
const management = JSON.parse(readFileSync(new URL('../../fixtures/webhooks/management.json', import.meta.url), 'utf8'), (key, value) => ['secret', 'signature'].includes(key) && Array.isArray(value) ? String.fromCharCode(...value) : value) as {
  endpoint: WebhookEndpoint; deliveries: WebhookList<WebhookDelivery>;
};
const { endpoint } = management;
const id = endpoint.id;

function resolved<T>(data: T): APIPromise<T> {
  return new APIPromise(Promise.resolve({
    data, response: new Response(JSON.stringify(data), { headers: { 'x-request-id': 'req_webhook' } }),
    metadata: { requestId: 'req_webhook', clientRequestId: null, rateLimit: { limit: null, remaining: null, reset: null }, latencyMs: null, replayed: false },
  }));
}

describe('shared webhook signature contract', () => {
  it.each(fixtures.cases)('$name', entry => {
    const body = entry.repeat === undefined ? (entry.body ?? fixtures.body) + (entry.append ?? '') : entry.repeat.repeat(entry.count!);
    const signature = entry.signature ?? fixtures.signature;
    const secret = entry.secret ?? fixtures.secret;
    const options = { now: entry.now ?? entry.timestamp ?? fixtures.timestamp };
    expect(verifyWebhookSignature(body, signature, secret, options)).toBe(entry.valid);
    const bytes = Buffer.from(body, 'utf8');
    // Invalid UTF-16 has no equivalent UTF-8 byte body. The replacement-character
    // vector separately proves that actual U+FFFD bytes remain valid.
    if (bytes.toString('utf8') === body) expect(verifyWebhookSignature(bytes, signature, secret, options)).toBe(entry.valid);
  });

  it('checks raw bytes without parsing, normalizing or retaining the payload', () => {
    expect(verifyWebhookSignature(JSON.stringify(JSON.parse(fixtures.body), null, 2), fixtures.signature, fixtures.secret, { now: fixtures.timestamp })).toBe(false);
    expect(verifyWebhookSignature(new Uint8Array([0xff]), fixtures.signature, fixtures.secret, { now: fixtures.timestamp })).toBe(false);
    expect(verifyWebhookSignature('\ud800', fixtures.signature, fixtures.secret, { now: fixtures.timestamp })).toBe(false);
  });

  it('verifies only the visible bytes of a body view', () => {
    const storage = Buffer.from(`x${fixtures.body}y`, 'utf8');
    expect(verifyWebhookSignature(storage.subarray(1, storage.length - 1), fixtures.signature, fixtures.secret, { now: fixtures.timestamp })).toBe(true);
  });

  it.each([NaN, Infinity, -1, 1770000000.5, Number.MAX_SAFE_INTEGER + 1])('rejects invalid local clock %s', now => {
    expect(verifyWebhookSignature(fixtures.body, fixtures.signature, fixtures.secret, { now })).toBe(false);
  });

  it('uses wall time when no test clock is supplied', () => {
    vi.spyOn(Date, 'now').mockReturnValue(fixtures.timestamp * 1000);
    try { expect(verifyWebhookSignature(fixtures.body, fixtures.signature, fixtures.secret)).toBe(true); }
    finally { vi.restoreAllMocks(); }
  });
});

describe('webhook management resource', () => {
  it('uses existing authenticated request plumbing with once-only create and rotate', async () => {
    const replies = [
      { ...endpoint, secret: fixtures.secret }, { object: 'list', data: [endpoint] }, { id, deleted: true },
      { ...endpoint, secret: fixtures.secret }, management.deliveries,
    ];
    const request = vi.fn(() => resolved(replies.shift()));
    const resource = new Webhooks(request as WebhookRequest);
    const options = { clientRequestId: 'client_webhook', headers: { 'x-custom': 'synthetic' } };
    const created = await resource.create({ url: endpoint.url, events: ['decision.completed'] }, options).withResponse();
    expect(created.data).toEqual({ ...endpoint, secret: fixtures.secret });
    expect(created.metadata.requestId).toBe('req_webhook');
    await expect(resource.list()).resolves.toEqual({ object: 'list', data: [endpoint] });
    await expect(resource.delete(id)).resolves.toEqual({ id, deleted: true });
    await expect(resource.rotateSecret(id)).resolves.toEqual({ ...endpoint, secret: fixtures.secret });
    const deliveries = await resource.deliveries(id);
    expect(deliveries).toEqual(management.deliveries);
    for (const delivery of deliveries.data) {
      expect(Object.keys(delivery).sort()).toEqual(['attempt', 'created', 'event_id', 'latency_ms', 'outcome', 'status_code', 'type']);
    }
    expect(request.mock.calls).toEqual([
      ['POST', '/decision-model/webhooks', JSON.stringify({ url: endpoint.url, events: ['decision.completed'] }), options, { idempotency: false, maxRetries: 0 }],
      ['GET', '/decision-model/webhooks', undefined, {}, { idempotency: false }],
      ['DELETE', `/decision-model/webhooks/${id}`, undefined, {}, { idempotency: false }],
      ['POST', `/decision-model/webhooks/${id}/rotate-secret`, '{}', {}, { idempotency: false, maxRetries: 0 }],
      ['GET', `/decision-model/webhooks/${id}/deliveries`, undefined, {}, { idempotency: false }],
    ]);
  });

  it.each(['', '../other', 'wh_ABC', `wh_${'1'.repeat(32)}/rotate-secret`, `${id}\n`])('rejects unsafe endpoint id %j before transport', badId => {
    const request = vi.fn();
    const resource = new Webhooks(request as WebhookRequest);
    for (const action of [() => resource.delete(badId), () => resource.rotateSecret(badId), () => resource.deliveries(badId)]) {
      expect(action).toThrow(WebhookInvalidError);
    }
    expect(request).not.toHaveBeenCalled();
  });

  it.each([1, 100])('supports the bounded delivery log limit %s', async limit => {
    const request = vi.fn(() => resolved({ object: 'list', data: [] }));
    const options = { limit, clientRequestId: 'req_log' };
    await new Webhooks(request as WebhookRequest).deliveries(id, options);
    expect(request.mock.calls).toEqual([
      ['GET', `/decision-model/webhooks/${id}/deliveries?limit=${limit}`, undefined, options, { idempotency: false }],
    ]);
    expect((request.mock.calls as unknown as unknown[][])[0]![3]).toBe(options);
  });

  it.each([0, 101, -1, 1.5, NaN, Infinity])('refuses invalid delivery log limit %s', limit => {
    const request = vi.fn();
    expect(() => new Webhooks(request as WebhookRequest).deliveries(id, { limit })).toThrow(WebhookInvalidError);
    expect(request).not.toHaveBeenCalled();
  });

  it.each([{ headers: { 'Idempotency-Key': 'forbidden' } }, { idempotencyKey: 'forbidden' }])('refuses an idempotency key on management', supplied => {
    const request = vi.fn();
    const resource = new Webhooks(request as WebhookRequest);
    const options = supplied as RequestOptions;
    for (const action of [
      () => resource.create({ url: endpoint.url, events: ['stream.closed'] }, options),
      () => resource.list(options), () => resource.delete(id, options),
      () => resource.rotateSecret(id, options), () => resource.deliveries(id, options),
    ]) expect(action).toThrow(WebhookInvalidError);
    expect(request).not.toHaveBeenCalled();
  });

  it.each(fixtures.errors)('preserves the typed $code envelope', async entry => {
    const original = responseError(entry.status, new Headers({ 'x-request-id': 'req_header' }), {
      error: { message: 'SYNTHETIC webhook failure', code: entry.code, type: entry.type, retryable: entry.retryable, param: 'url', details: { limit: 5 } },
      request_id: 'req_body',
    }, false);
    const request = vi.fn(() => new APIPromise(Promise.reject(original)));
    const resource = new Webhooks(request as WebhookRequest);
    const error = await resource.list().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(SystemZeroError);
    expect(error).toMatchObject({ name: entry.class_name, code: entry.code, type: entry.type, status: entry.status, retryable: entry.retryable, param: 'url', details: { limit: 5 }, request_id: 'req_body' });
    expect((error as SystemZeroError).headers.get('x-request-id')).toBe('req_header');
    expect(WEBHOOK_ERROR_CODES[entry.code]).toEqual({ status: entry.status, type: entry.type, retryable: entry.retryable });
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('preserves unknown errors without losing their identity', async () => {
    const original = new SystemZeroError('SYNTHETIC unrelated', { code: 'future_code', status: 400 });
    const request = vi.fn(() => new APIPromise(Promise.reject(original)));
    await expect(new Webhooks(request as WebhookRequest).list()).rejects.toBe(original);
  });
});
