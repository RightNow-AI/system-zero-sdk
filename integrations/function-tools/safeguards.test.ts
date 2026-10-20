import { afterEach, describe, expect, it, vi } from 'vitest';
import { SystemZeroClient } from '../../typescript/src/client.js';
import { createToolHandler } from '../../typescript/src/integrations/function-tools/index.js';
import catalog from '../../typescript/src/integrations/function-tools/gateway-errors.js';

const args = { state: 'SYNTHETIC', questions: [{ name: 'ready', type: 'boolean', instructions: 'Ready?' }] };
const limits = { maxDecideCalls: 10, };
const gatewayCodes = [
  ['invalid_api_key', 401], ['monthly_cap_reached', 429], ['idempotency_conflict', 409],
  ['auth_error', 403], ['account_suspended', 403], ['model_not_found', 404],
  ['idempotency_replayed_error', 500],
] as const;

afterEach(() => { vi.unstubAllEnvs(); });

describe('SDK tool safeguards', () => {
  it.each(['system_zero_decide', 'system_zero_list_models'])('redacts every field and object key returned by %s', async tool => {
    const apiKey = 'SYNTHETIC';
    const environmentKey = 'ENV_KEY';
    const shaped = 'rnsk_' + 'Q'.repeat(40);
    const bearer = 'TOKEN';
    vi.stubEnv('SYSTEM_ZERO_API_KEY', environmentKey);
    const echo = `${apiKey} ${environmentKey} ${shaped} Bearer ${bearer}`;
    const clean = '[redacted] [redacted] [redacted] Bearer [redacted]';
    const body = { id: echo, answers: {}, data: [{ [echo]: { nested: [echo] } }] };
    const client = new SystemZeroClient({ apiKey, maxRetries: 0, fetch: async () => Response.json(body, { headers: { 'x-request-id': echo } }) });
    const result = await createToolHandler({ ...limits, client })(tool, tool === 'system_zero_decide' ? args : {});
    expect(result).toEqual({ id: clean, answers: {}, data: [{ [clean]: { nested: [clean] } }], status: 'ok', request_id: clean });
    for (const secret of [apiKey, environmentKey, shaped, bearer]) expect(JSON.stringify(result)).not.toContain(secret);
    expect(body.id).toBe(echo);
  });

  it('redacts client credentials from typed errors and all-question refusal payloads', async () => {
    const apiKey = 'SYNTHETIC';
    const shaped = 'rnsk_' + 'Z'.repeat(32);
    const echo = `${apiKey} ${shaped} Bearer TOKEN`;
    for (const status of [400, 200]) {
      const refusal = { code: 'invalid_request', message: echo, type: 'invalid_request_error', retryable: false };
      const body = status === 400 ? { request_id: echo, error: refusal } : { id: echo, answers: { [echo]: { error: refusal, extra: [echo] } } };
      const client = new SystemZeroClient({ apiKey, maxRetries: 0, fetch: async () => Response.json(body, { status }) });
      const result = await createToolHandler({ ...limits, client })('system_zero_decide', args);
      expect(result).toMatchObject({ status: 'error', error: { code: 'invalid_request' } });
      for (const secret of [apiKey, shaped, 'TOKEN']) expect(JSON.stringify(result)).not.toContain(secret);
    }
  });

  it.each(gatewayCodes)('publishes and retains the gateway catalog code %s', async (code, status) => {
    expect(catalog).toHaveProperty(code, expect.objectContaining({ status }));
    for (const tool of ['system_zero_decide', 'system_zero_list_models']) {
      const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 0, fetch: async () => Response.json({
        request_id: 'req_synthetic', error: { code, type: 'api_error', message: 'Synthetic refusal.', retryable: false },
      }, { status }) });
      expect(await createToolHandler({ ...limits, client })(tool, tool === 'system_zero_decide' ? args : {})).toEqual({
        status: 'error', request_id: 'req_synthetic', error: { code, message: 'Synthetic refusal.' },
      });
    }
  });

  it.each(gatewayCodes.slice(3))('keeps %s when every answer is refused', async code => {
    const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 0, fetch: async () => Response.json({
      answers: { ready: { error: { code, message: 'Synthetic refusal.', retryable: false } } },
    }, { headers: { 'x-request-id': 'req_synthetic' } }) });
    expect(await createToolHandler({ ...limits, client })('system_zero_decide', args)).toMatchObject({
      status: 'error', request_id: 'req_synthetic', error: { code },
      answers: { ready: { error: { code } } },
    });
  });
});
