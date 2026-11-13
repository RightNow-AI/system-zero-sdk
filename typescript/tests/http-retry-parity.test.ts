import { describe, expect, it } from 'vitest';
import { APIConnectionError, APITimeoutError, ServiceUnavailableError, SystemZeroClient,
  markResponseStarted, type Fetch } from '../src/index.js';

const request = { model: 'rightnow-decision', state: 'SYNTHETIC HTTP retry parity',
  questions: { ready: { type: 'boolean' as const, instructions: 'Ready?' } } };
const key = 'SYNTHETIC';
const refusal = { request_id: 'req_http_parity', error: {
  type: 'api_error', code: 'service_unavailable', message: 'SYNTHETIC refusal', retryable: true,
} };

 
// The paired Python file pins the same terminal and retryable cases.
for (const method of ['GET', 'POST'] as const) {
  describe(`${method} ordinary HTTP retry parity`, () => {
    async function observe(transport: Fetch, keyed = true) {
      const attempts: Array<{ method: string | undefined; key: string | null }> = [];
      const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 2,
        timeout: 1000, retryBudget: 10_000, random: () => 0, sleep: async () => {},
        fetch: async (url, init) => {
          attempts.push({ method: init?.method, key: new Headers(init?.headers).get('idempotency-key') });
          return transport(url, init);
        },
      });
      const failure = await (method === 'GET' ? client.models.list()
        : client.decide(request, { idempotencyKey: keyed ? key : '' })).catch(error => error);
      return { attempts, failure };
    }

    function expectAttempts(attempts: Array<{ method: string | undefined; key: string | null }>, count: number) {
      expect(attempts).toEqual(Array.from({ length: count }, () => ({ method, key: method === 'GET' ? null : key })));
    }

    it('never replays an explicitly terminal SDK connection error', async () => {
      const original = new APIConnectionError('SYNTHETIC terminal transport failure', {
        type: 'connection_error', retryable: false, transport_code: 'ECONNREFUSED', request_sent: false,
      });
      const { attempts, failure } = await observe(async () => { throw original; });
      expectAttempts(attempts, 1);
      expect(failure.constructor).toBe(APIConnectionError);
      expect(failure).toMatchObject({ retryable: false, type: 'connection_error',
        transport_code: 'ECONNREFUSED', request_sent: false });
    });

    it('normalizes a terminal SDK timeout without making it retryable', async () => {
      const original = new APITimeoutError('SYNTHETIC terminal timeout', {
        type: 'timeout_error', retryable: false, transport_code: 'ETIMEDOUT', request_sent: false,
      });
      const { attempts, failure } = await observe(async () => { throw original; });
      expectAttempts(attempts, 1);
      expect(failure.constructor).toBe(APIConnectionError);
      expect(failure).toMatchObject({ retryable: false, type: 'timeout_error',
        transport_code: 'ETIMEDOUT', request_sent: false });
    });

    it('keeps the base transport retries before any response with one stable key', async () => {
      const { attempts, failure } = await observe(async () => {
        throw Object.assign(new Error('SYNTHETIC reset'), { code: 'ECONNRESET' });
      });
      expectAttempts(attempts, 3);
      expect(failure.constructor).toBe(APIConnectionError);
      expect(failure).toMatchObject({ retryable: true, transport_code: 'ECONNRESET' });
    });

    it('never replays a native failure after a response byte', async () => {
      const { attempts, failure } = await observe(async (_url, init) => {
        markResponseStarted(init!.signal!);
        throw Object.assign(new Error('SYNTHETIC reset after response'), { code: 'ECONNRESET' });
      });
      expectAttempts(attempts, 1);
      expect(failure.constructor).toBe(APIConnectionError);
      expect(failure).toMatchObject({ retryable: false, request_sent: true, transport_code: 'ECONNRESET' });
    });

    it('never replays a response stream fault after complete refusal bytes', async () => {
      const { attempts, failure } = await observe(async () => {
        let emitted = false;
        return new Response(new ReadableStream<Uint8Array>({
          pull(controller) {
            if (!emitted) {
              emitted = true;
              controller.enqueue(new TextEncoder().encode(JSON.stringify(refusal)));
            } else controller.error(Object.assign(new Error('SYNTHETIC body close fault'), { code: 'ECONNRESET' }));
          },
        }), { status: 503, headers: { 'x-request-id': 'req_http_parity' } });
      });
      expectAttempts(attempts, 1);
      expect(failure.constructor).toBe(APIConnectionError);
      expect(failure).toMatchObject({ retryable: false, request_sent: true, status: 503,
        request_id: 'req_http_parity', transport_code: 'ECONNRESET' });
    });

    it('keeps the base complete refusal retries with one stable key', async () => {
      const { attempts, failure } = await observe(async () => new Response(JSON.stringify(refusal), { status: 503 }));
      expectAttempts(attempts, 3);
      expect(failure).toBeInstanceOf(ServiceUnavailableError);
      expect(failure).toMatchObject({ retryable: true, status: 503, code: 'service_unavailable' });
    });

    if (method === 'POST') {
      it('keeps an explicitly unkeyed request terminal before any response', async () => {
        const { attempts, failure } = await observe(async () => {
          throw Object.assign(new Error('SYNTHETIC before-response reset'), { code: 'ECONNRESET' });
        }, false);
        expect(attempts).toEqual([{ method: 'POST', key: null }]);
        expect(failure.constructor).toBe(APIConnectionError);
        expect(failure.retryable).toBe(false);
      });
    }
  });
}

for (const status of [429, 503]) {
  for (const milliseconds of [true, false]) {
    it(`exposes and bounds server waits (${status}, milliseconds=${milliseconds})`, async () => {
      let attempts = 0;
      const sleeps: number[] = [];
      const headers = milliseconds ? { 'Retry-After-Ms': '240000', 'Retry-After': '1' } : { 'Retry-After': '180' };
      const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', random: () => 0,
        sleep: async ms => { sleeps.push(ms); }, fetch: async () => {
          attempts++;
          return Response.json({ error: { code: status === 429 ? 'rate_limit_exceeded' : 'model_unavailable', retryable: true } }, { status, headers });
        } });
      const error = await client.decide(request).catch(error => error);
      expect(error).toMatchObject({ status, retryAfterMs: milliseconds ? 240000 : 180000 });
      expect(attempts).toBe(1);
      expect(sleeps).toEqual([]);
    });
  }
  it(`honors a server wait within the remaining budget (${status})`, async () => {
    let attempts = 0, clock = 0;
    const sleeps: number[] = [];
    const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', retryBudget: 2000, monotonic: () => clock, random: () => 0,
      sleep: async ms => { sleeps.push(ms); clock += ms; }, fetch: async () => {
        attempts++;
        return attempts === 1 ? Response.json({ error: { code: status === 429 ? 'rate_limit_exceeded' : 'model_unavailable', retryable: true } }, { status, headers: { 'Retry-After-Ms': '500', 'Retry-After': '180' } }) : Response.json({ answers: {} });
      } });
    await client.decide(request);
    expect(sleeps).toEqual([500]);
    expect(attempts).toBe(2);
  });
}

it.each([
  [503, 'idempotency_unavailable'], [503, 'limiter_unavailable'],
  [500, 'internal_error'], [503, 'starter_unavailable'], [502, null], [503, null],
] as const)('recovers a safe server refusal %i/%s using the original key', async (status, code) => {
  const seen: Array<string | null> = [];
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', random: () => 0, sleep: async () => {},
    fetch: async (_url, init) => {
      seen.push(new Headers(init?.headers).get('idempotency-key'));
      return seen.length > 1 ? Response.json({ answers: {} }) : code === null ? new Response('upstream unavailable', { status })
        : Response.json({ error: { code, retryable: true } }, { status });
    } });
  await client.decide(request, { idempotencyKey: 'same-recovery-key' });
  expect(seen).toEqual(['same-recovery-key', 'same-recovery-key']);
});

it('starts the recovery budget after the first failure', async () => {
  let clock = 0, attempts = 0;
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', monotonic: () => clock,
    random: () => 0, sleep: async () => {}, fetch: async () => {
      if (++attempts === 1) {
        clock += 70000;
        return Response.json(refusal, { status: 503 });
      }
      return Response.json({ answers: {} });
    } });
  await client.decide(request);
  expect(attempts).toBe(2);
});
