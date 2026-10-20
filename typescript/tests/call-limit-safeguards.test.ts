import { mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { InvalidRequestError, SystemZeroClient, SystemZeroError,
  runBatch, runFlow, type FlowTrace } from '../src/index.js';

const request = { model: 'system-zero', state: 'SYNTHETIC state',
  questions: { ready: { type: 'boolean' as const, instructions: 'Ready?' } } };
const answer = { type: 'boolean', probability: 0.8, decision: true, confidence: 0.8, calibrated: false,
  alternatives: [{ decision: false, confidence: 0.2 }], calibration_version: 'synthetic' };
const reply = { id: 'szd_synthetic', model: 'system-zero', served_by: 'synthetic', created: 1,
  context: { route: 'direct', tokens: 1 }, answers: { ready: answer },
  usage: { input_tokens: 1, cached_input_tokens: 0, media_tokens: 0,
    long_input_tokens: 0, media_items: 0, thought_tokens: 0 } };
const items = ['one', 'two', 'three'].map(id => ({ id, request, expected: { ready: true } }));
const flow = { version: 1, request: { model: request.model, state: request.state },
  steps: ['one', 'two'].map((id, index) => ({ id, questions: request.questions,
    next: { rules: [], default: index === 0 ? 'two' : null } })) };

function client() {
  const fetch = vi.fn(async () => Response.json(reply));
  return { fetch, sdk: new SystemZeroClient({ apiKey: 'SYNTHETIC', fetch, timeout: 25, maxRetries: 0 }) };
}

async function withReceipts(run: (resultsPath: string) => Promise<void>) {
  const scratch = join(process.cwd(), 'tmp');
  mkdirSync(scratch, { recursive: true });
  const directory = mkdtempSync(join(scratch, 'call-limit-'));
  try { await run(join(directory, 'receipts.jsonl')); }
  finally {
    if (dirname(directory) !== scratch) throw new Error('Scratch directory escaped its root.');
    rmSync(directory, { recursive: true, force: true });
  }
}

describe('SDK count limits', () => {
  it('stops a zero-count batch before dispatch despite an available cost budget', async () => {
    const { sdk, fetch } = client();
    await withReceipts(async resultsPath => {
      const result = await runBatch(sdk, items, { runId: 'zero-calls', resultsPath,
         maxDecideCalls: 0, deadlineMs: 10000, concurrency: 2 });
      expect(result).toEqual({ records: [], stopped: 'call_limit', decide_calls: 0 });
      expect(fetch).not.toHaveBeenCalled();
    });
  });

  it('shares the count across workers and durable admissions on resume', async () => {
    const { sdk, fetch } = client();
    await withReceipts(async resultsPath => {
      const options = { runId: 'count-resume', resultsPath,
        maxDecideCalls: 1, deadlineMs: 10000, concurrency: 2 };
      const first = await runBatch(sdk, items, options);
      expect(first.stopped).toBe('call_limit');
      expect(first.decide_calls).toBe(1);
      expect(first.records.map(row => row.id)).toEqual(['one']);
      expect(first.records.map(row => row.outcome)).toEqual(['completed']);
      expect(fetch).toHaveBeenCalledTimes(1);
      await expect(runBatch(sdk, items, { ...options, maxDecideCalls: 0 }))
        .rejects.toMatchObject({ code: 'invalid_request', param: 'max_decide_calls' });
      expect(fetch).toHaveBeenCalledTimes(1);
      const resumed = await runBatch(sdk, items, { ...options, maxDecideCalls: 3 });
      expect(resumed.stopped).toBeNull();
      expect(resumed.decide_calls).toBe(3);
      expect(resumed.records.map(row => row.id)).toEqual(['one', 'two', 'three']);
      expect(resumed.records.every(row => row.outcome === 'completed')).toBe(true);
      expect(fetch).toHaveBeenCalledTimes(3);
    });
  });

  it.each([true, -1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1])('validates the optional batch count: %s', async value => {
    const { sdk, fetch } = client();
    await withReceipts(async resultsPath => {
      await expect(runBatch(sdk, [], { runId: 'invalid-count', resultsPath,
        maxDecideCalls: value as number, deadlineMs: 10000 })).rejects.toMatchObject({
        code: 'invalid_request', param: 'max_decide_calls', message: 'Invalid batch input or checkpoint.',
      });
      expect(fetch).not.toHaveBeenCalled();
    });
  });

  it.each([0, 1])('admits at most %s flow decisions and reports the local count', async maxDecideCalls => {
    const { sdk, fetch } = client();
    const error = await runFlow(sdk, flow, { run_id: 'counted-flow',
       max_decide_calls: maxDecideCalls,
      deadline_ms: 10000, now: () => 0 }).catch((failure: unknown) => failure);
    expect(error).toBeInstanceOf(SystemZeroError);
    expect(error).toMatchObject({ code: 'call_limit_reached', retryable: false });
    const trace = (error as SystemZeroError & { trace: FlowTrace }).trace;
    expect(trace.decide_calls).toBe(maxDecideCalls);
    expect(trace).not.toHaveProperty('reserved_cost_microcents');
    expect(trace.steps.map(step => step.decide_calls)).toEqual(maxDecideCalls === 0 ? [0] : [1, 0]);
    expect(trace.steps.at(-1)?.error?.code).toBe('call_limit_reached');
    expect(fetch).toHaveBeenCalledTimes(maxDecideCalls);
  });

  it.each([true, -1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, 33])('validates the optional flow count: %s', async value => {
    const { sdk, fetch } = client();
    const result = runFlow(sdk, flow, { run_id: 'invalid-flow-count',
       max_decide_calls: value as number,
      deadline_ms: 10000, now: () => 0 });
    await expect(result).rejects.toBeInstanceOf(InvalidRequestError);
    if (value === 33) await expect(result).rejects.toMatchObject({ message: expect.stringMatching(/max_decide_calls.*32/) });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('defaults a batch to 1000 durable admissions and refuses the 1001st', async () => {
    const { sdk, fetch } = client();
    const many = Array.from({ length: 1001 }, (_, index) => ({ ...items[0]!, id: `item-${index}` }));
    await withReceipts(async resultsPath => {
      const options = { runId: 'default-batch', resultsPath,  deadlineMs: 120000, concurrency: 2 };
      const result = await runBatch(sdk, many, options);
      expect(result.stopped).toBe('call_limit');
      expect(result.decide_calls).toBe(1000);
      expect(result.records).toHaveLength(1000);
      expect(fetch).toHaveBeenCalledTimes(1000);
      const resumed = await runBatch(sdk, many, options);
      expect(resumed.decide_calls).toBe(1000);
      expect(resumed.stopped).toBe('call_limit');
      expect(fetch).toHaveBeenCalledTimes(1000);
      const extended = await runBatch(sdk, many, { ...options, maxDecideCalls: 1001 });
      expect(extended.stopped).toBeNull();
      expect(extended.decide_calls).toBe(1001);
      expect(fetch).toHaveBeenCalledTimes(1001);
    });
  }, 120000);

  it('always traces omitted flow counts and admits all 32 steps', async () => {
    const { sdk, fetch } = client();
    const steps = Array.from({ length: 32 }, (_, index) => ({ id: `s${index}`, questions: request.questions,
      next: { rules: [], default: index < 31 ? `s${index + 1}` : null } }));
    const result = await runFlow(sdk, { ...flow, steps }, { run_id: 'default-flow',
       deadline_ms: 10000, now: () => 0 });
    expect(result.decide_calls).toBe(32);
    expect(result.steps.map(step => step.decide_calls)).toEqual(Array(32).fill(1));
    expect(fetch).toHaveBeenCalledTimes(32);
  });

  it('refuses a 33-step flow before it can dispatch', async () => {
    const { sdk, fetch } = client();
    const steps = Array.from({ length: 33 }, (_, index) => ({ id: `s${index}`, questions: request.questions,
      next: { rules: [], default: index < 32 ? `s${index + 1}` : null } }));
    await expect(runFlow(sdk, { ...flow, steps }, { run_id: 'too-many',
       deadline_ms: 10000 }))
      .rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetch).not.toHaveBeenCalled();
  });
});
