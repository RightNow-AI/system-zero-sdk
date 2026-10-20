import { describe, expect, it } from 'vitest';
import { APIConnectionError, APITimeoutError, ServiceUnavailableError, SystemZeroClient,
  markResponseStarted, type Fetch } from '../src/index.js';

const request = { model: 'system-zero', state: 'SYNTHETIC HTTP retry parity',
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
