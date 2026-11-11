import { afterEach, expect, it, vi } from 'vitest';
import { SystemZeroClient, SystemZeroStreamClient, decisionsFor, InvalidRequestError, PolicyMaxCostExceededError } from '../src/index.js';
import { validatePolicy } from '../src/policy.js';

const request = { model: 'system-zero', state: 'SYNTHETIC shipment.',
  questions: { ready: { type: 'boolean' as const, instructions: 'Has it arrived?' } } };
const policy = { min_confidence: 0.8, max_cost_usd: 0.01 };
afterEach(() => vi.restoreAllMocks());

it('sends policy unchanged and reads a charged abstention without a decision', async () => {
  const answer = { type: 'boolean' as const, abstained: true as const, abstention_reason: 'low_confidence' as const,
    probability: 0.6, confidence: 0.6, alternatives: [{ decision: false, confidence: 0.4 }],
    calibrated: false, calibration_version: 'synthetic' };
  const fetch = vi.fn(async (_input: string | URL | Request, _init?: RequestInit) => Response.json({ answers: { ready: answer }, policy }));
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', fetch, maxRetries: 0 });
  const reply = await client.decide({ ...request, policy });
  expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body)).policy).toEqual(policy);
  expect(reply.policy).toEqual(policy);
  expect(decisionsFor(reply).ready).toMatchObject({ kind: 'abstained', confidence: 0.6 });
  expect(decisionsFor(reply).ready).not.toHaveProperty('decision');
});

it('refuses held members before any HTTP attempt', () => {
  const fetch = vi.fn();
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', fetch });
  for (const [policy, param] of [
    [{ on_low_confidence: 'error' }, 'policy.on_low_confidence'],
    [{ require_calibrated: false }, 'policy.require_calibrated'],
    [{ min_confidence: 0 }, 'policy.min_confidence'],
  ] as const) {
    expect(() => validatePolicy(policy)).toThrow(expect.objectContaining({ code: 'invalid_request', param }));
    expect(() => client.decide({ ...request, policy } as never)).toThrow(InvalidRequestError);
  }
  expect(fetch).not.toHaveBeenCalled();
});

it('preserves the uncharged over-budget refusal', async () => {
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 0, fetch: async () => Response.json({
    error: { code: 'policy_max_cost_exceeded', type: 'invalid_request_error', message: 'SYNTHETIC cap.',
      param: 'policy.max_cost_usd', retryable: false, details: { counted: false } },
  }, { status: 400 }) });
  await expect(client.decide({ ...request, policy })).rejects.toMatchObject({
    status: 400, code: 'policy_max_cost_exceeded', param: 'policy.max_cost_usd', counted: false,
  });
  await expect(client.decide({ ...request, policy })).rejects.toBeInstanceOf(PolicyMaxCostExceededError);
});

it('validates stream policy before opening a connection', async () => {
  const client = new SystemZeroStreamClient({ apiKey: 'SYNTHETIC' });
  const connect = vi.spyOn(client, 'connect');
  try {
    await expect(client.start({ ...request, policy: { on_low_confidence: 'error' } } as never)).rejects.toMatchObject({
      code: 'invalid_request', param: 'policy.on_low_confidence',
    });
    expect(connect).not.toHaveBeenCalled();
  } finally { await client.close(); }
});
