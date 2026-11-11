// SYNTHETIC regression for W2C-COUNTED-NOT-BILLED.
import { describe, expect, expectTypeOf, it } from 'vitest';
import * as sdk from '../src/index.js';

const request = {
  model: 'system-zero', state: 'SYNTHETIC policy count.',
  questions: { ready: { type: 'boolean' as const, instructions: 'Is it ready?' } },
};
const cases = [
  { code: 'policy_max_cost_exceeded', status: 400, counted: false, ErrorClass: sdk.PolicyMaxCostExceededError },
  { code: 'policy_confidence_failed', status: 422, counted: true, ErrorClass: sdk.PolicyConfidenceFailedError },
] as const;

describe.each(cases)('$code counted contract', sample => {
   
  // are absent, malformed, or contradictory; it is not inferred from details.
  const detailsCases: { name: string; details: Record<string, unknown> | undefined }[] = [
    { name: 'matching details', details: { counted: sample.counted } },
    { name: 'missing details', details: undefined },
    { name: 'missing member', details: {} },
    { name: 'malformed member', details: { counted: 'unknown' } },
    { name: 'null member', details: { counted: null } },
    { name: 'contradictory member', details: { counted: !sample.counted } },
  ];

  it.each(detailsCases)('public HTTP error keeps counted with $name and no billed alias', async ({ details }) => {
    const body = { error: {
      message: 'SYNTHETIC policy refusal.', type: 'invalid_request_error',
      code: sample.code, retryable: false, ...(details === undefined ? {} : { details }),
    } };
    const client = new sdk.SystemZeroClient({
      apiKey: 'SYNTHETIC', maxRetries: 0,
      fetch: async () => Response.json(body, { status: sample.status }),
    });
    const error: unknown = await client.decide(request).then(() => undefined, failure => failure);
    expect(error).toBeInstanceOf(sample.ErrorClass);
    expect(error).toHaveProperty('counted', sample.counted);
    expect(error).not.toHaveProperty('billed');
    expect(error).toHaveProperty('details', details);
  });

  it.each(detailsCases)('public specialization keeps counted with $name and no billed alias', ({ details }) => {
    const source = new sdk.SystemZeroError('SYNTHETIC policy refusal.', {
      status: sample.status, type: 'invalid_request_error', code: sample.code,
      ...(details === undefined ? {} : { details }),
    });
    const error = sdk.asPolicyError(source);
    expect(error).toBeInstanceOf(sample.ErrorClass);
    expect(error).toHaveProperty('counted', sample.counted);
    expect(error).not.toHaveProperty('billed');
    expect(error?.details).toBe(details);
    expect(sdk.asPolicyError(error!)).toBe(error);
  });
});

it('keeps the public counted types as the existing per-code boolean literals', () => {
  expectTypeOf<sdk.PolicyMaxCostExceededError['counted']>().toEqualTypeOf<false>();
  expectTypeOf<sdk.PolicyConfidenceFailedError['counted']>().toEqualTypeOf<true>();
});
