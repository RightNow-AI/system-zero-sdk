import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { expect, it } from 'vitest';
import { AuthenticationError, InvalidRequestError, NotFoundError, PermissionError, ServerError,
  SystemZeroClient, SystemZeroError, SYSTEM_ZERO_ERROR_CODES, GATEWAY_ERROR_CODES,
  evaluate, runBatch, runFlow, type DecideRequest, type FlowError } from '../src/index.js';

const request: DecideRequest = {
  model: 'system-zero', state: 'synthetic',
  questions: { q: { type: 'boolean', instructions: 'Is it set?' } },
};

const heldCodes = new Set([
  'long_context_not_available', 'long_recording_not_available', 'video_not_available',
  'image_not_available', 'audio_not_available', 'document_not_available',
  'committee_not_available', 'deliberate_not_available',
]);
const refusedCodes = new Set([
  'injection_suspected', 'request_refused', 'question_not_grounded',
  ]);
const openCodes = ['future_code_x', 'a', 'a' + '0_'.repeat(31) + '0'];
const malformedCodes: unknown[] = [
  'FUTURE_CODE_X', 'future code', 'a'.repeat(65), '', '_future', '1future',
  'future-code', 'future.code', 'future_code_x\n', 'future_code_x\r',
  'futur\u00e9', 123, false, {}, [], null, undefined,
];
const workflowCodeCases = [
  ...openCodes.map(code => ({ code, savedCode: code, status: 409 })),
  ...malformedCodes.map(code => ({ code, savedCode: 'service_unavailable', status: 503 })),
  ...[...new Set([...SYSTEM_ZERO_ERROR_CODES, ...GATEWAY_ERROR_CODES])].flatMap(code =>
    [404, 409, 422, 503].map(status => ({ code, savedCode: code, status }))),
];
const flow = {
  version: 1, request: { model: request.model, state: request.state },
  steps: [{ id: 'first', questions: request.questions, next: { rules: [], default: null } }],
};

it('batch preserves open codes, falls back for unusable codes, and keeps known outcomes on resume', async () => {
  const scratch = join(process.cwd(), 'tmp');
  mkdirSync(scratch, { recursive: true });
  const folder = mkdtempSync(join(scratch, 'batch-open-codes-'));
  try {
    for (const [index, { code, savedCode, status }] of workflowCodeCases.entries()) {
      let calls = 0;
      const client = new SystemZeroClient({
        apiKey: 'synthetic', timeout: 1000, maxRetries: 0,
        fetch: async () => {
          calls++;
          return Response.json({ error: {
            message: 'Synthetic response.', type: 'api_error', code, retryable: false,
          } }, { status });
        },
      });
      const items = [{ id: 'one', request, expected: { q: true } }];
      const options = { runId: 'open-codes', resultsPath: join(folder, `${index}.jsonl`),
        deadlineMs: 10000, concurrency: 1, now: () => 1000 };
      const result = await runBatch(client, items, options);
      const saved = JSON.parse(readFileSync(options.resultsPath, 'utf8'));
      const outcome = status === 404 && heldCodes.has(savedCode) ? 'held'
        : status === 422 && refusedCodes.has(savedCode) ? 'refused' : 'error';
      expect(saved).toMatchObject({ code: savedCode, outcome, attempts: 1, usage: null });
      expect(saved.questions).toEqual(outcome === 'refused' ? [{
        question_id: 'q', type: 'boolean', outcome: 'refused', expected_refusal: false,
        correct: false, confidence: null, choice_expected: null, choice_actual: null,
        choice_schema: null, code: savedCode,
      }] : []);
      expect(result.records).toEqual([saved]);
      expect(evaluate(result.records).records[0]!.code).toBe(savedCode);
      expect((await runBatch(client, items, options)).records).toEqual([saved]);
      expect(calls).toBe(1);
    }
  } finally {
    rmSync(folder, { recursive: true, force: true });
  }
}, 30000);

it('flow preserves open codes, falls back for unusable codes, and keeps known outcomes', async () => {
  for (const { code, savedCode, status } of workflowCodeCases) {
    let calls = 0;
    const client = new SystemZeroClient({
      apiKey: 'synthetic', timeout: 1000, maxRetries: 0,
      fetch: async () => {
        calls++;
        return Response.json({ error: {
          message: 'Synthetic response.', type: 'api_error', code, retryable: false,
        } }, { status });
      },
    });
    const error = await runFlow(client, flow, { run_id: 'open-codes', deadline_ms: 10000, now: () => 0 })
      .catch((failure: unknown) => failure) as FlowError;
    expect(error).toBeInstanceOf(SystemZeroError);
    const outcome = heldCodes.has(savedCode) ? 'held' : status < 500 ? 'refused' : 'failed';
    expect(error.trace.status).toBe(outcome);
    expect(error.trace.decide_calls).toBe(1);
    expect(error.trace.steps[0]).toMatchObject({ status: outcome, error: {
      code: savedCode, type: 'api_error', status, retryable: typeof code !== 'string',
    } });
    expect(calls).toBe(1);
  }
}, 30000);

it('batch handles open and malformed codes on directly raised SDK errors', async () => {
  const scratch = join(process.cwd(), 'tmp');
  mkdirSync(scratch, { recursive: true });
  const folder = mkdtempSync(join(scratch, 'batch-raised-codes-'));
  try {
    for (const [index, code] of ['future_code_x', ...malformedCodes].entries()) {
      class FailingClient extends SystemZeroClient {
        override decide(): never {
          throw new SystemZeroError('Synthetic response.', { code: code as string, status: 503 });
        }
      }
      const client = new FailingClient({ apiKey: 'synthetic', timeout: 1000, maxRetries: 0 });
      const result = await runBatch(client, [{ id: 'one', request, expected: { q: true } }], {
        runId: 'raised-codes', resultsPath: join(folder, `${index}.jsonl`), deadlineMs: 10000, concurrency: 1,
      });
      expect(result.records[0]).toMatchObject({
        code: index === 0 ? 'future_code_x' : 'service_unavailable', outcome: 'error', questions: [],
      });
    }
  } finally {
    rmSync(folder, { recursive: true, force: true });
  }
});

it('flow handles open and malformed codes on directly raised SDK errors', async () => {
  for (const [index, code] of ['future_code_x', ...malformedCodes].entries()) {
    class FailingClient extends SystemZeroClient {
      override decide(): never {
        throw new SystemZeroError('Synthetic response.', { code: code as string, status: 503 });
      }
    }
    const client = new FailingClient({ apiKey: 'synthetic', timeout: 1000, maxRetries: 0 });
    const error = await runFlow(client, flow, { run_id: 'raised-codes', deadline_ms: 10000, now: () => 0 })
      .catch((failure: unknown) => failure) as FlowError;
    expect(error).toBeInstanceOf(SystemZeroError);
    expect(error.trace.steps[0]).toMatchObject({ status: 'failed', error: {
      code: index === 0 ? 'future_code_x' : 'service_unavailable',
    } });
  }
});

it.each([
  [404, 'model_not_found', 'not_found_error', 'error'],
  [403, 'auth_error', 'authentication_error', 'error'],
  [403, 'account_suspended', 'permission_error', 'error'],
  [500, 'idempotency_replayed_error', 'server_error', 'error'],
  [422, 'request_refused', 'invalid_request_error', 'refused'],
] as const)('batch preserves %i %s and its %s classification', async (status, code, type, outcome) => {
  const scratch = join(process.cwd(), 'tmp');
  mkdirSync(scratch, { recursive: true });
  const folder = mkdtempSync(join(scratch, 'gateway-batch-'));
  const resultsPath = join(folder, 'rows.jsonl');
  let calls = 0;
  const client = new SystemZeroClient({
    apiKey: 'synthetic', baseURL: 'https://api.example.com/v1', timeout: 1000, maxRetries: 0,
    fetch: async () => {
      calls++;
      return Response.json({ error: { message: 'Synthetic gateway response.', code, type, retryable: false, param: code === 'model_not_found' ? 'model' : null } }, { status });
    },
  });
  try {
    const result = await runBatch(client, [{ id: 'one', request, expected: { q: true } }], {
      runId: 'gateway-code', resultsPath,  deadlineMs: 10000, concurrency: 1,
    });
    const saved = JSON.parse(readFileSync(resultsPath, 'utf8'));
    expect(calls).toBe(1);
    expect(saved).toMatchObject({ code, outcome, attempts: 1 });
    expect(result.records[0]).toEqual(saved);
    expect(saved.questions).toHaveLength(outcome === 'refused' ? 1 : 0);
    if (outcome === 'held') expect(saved.usage).toBeNull();
    expect(saved).not.toHaveProperty('cost_nanocents');
    expect(saved).not.toHaveProperty('accounted_nanocents');
  } finally {
    rmSync(folder, { recursive: true, force: true });
  }
});

it.each([
  [403, 'auth_error', 'authentication_error', AuthenticationError],
  [403, 'account_suspended', 'permission_error', PermissionError],
  [404, 'model_not_found', 'not_found_error', NotFoundError],
  [422, 'idempotency_replayed_error', 'invalid_request_error', InvalidRequestError],
  [503, 'idempotency_replayed_error', 'server_error', ServerError],
] as const)('enabled batch retries preserve %i %s as %s after one send', async (status, code, type, ErrorClass) => {
  const scratch = join(process.cwd(), 'tmp');
  mkdirSync(scratch, { recursive: true });
  const folder = mkdtempSync(join(scratch, 'gateway-batch-retries-'));
  const resultsPath = join(folder, 'rows.jsonl');
  const observed: unknown[] = [];
  const waits: number[] = [];
  let calls = 0;
  class RecordingClient extends SystemZeroClient {
    override decide(...parameters: Parameters<SystemZeroClient['decide']>) {
      const operation = super.decide(...parameters);
      void operation.catch(error => { observed.push(error); });
      return operation;
    }
  }
  const client = new RecordingClient({
    apiKey: 'synthetic', baseURL: 'https://api.example.com/v1', timeout: 1000, maxRetries: 3,
    sleep: async delay => { waits.push(delay); },
    fetch: async () => {
      calls++;
      // Even an erroneous retry hint cannot widen the workflow admission policy.
      return Response.json({ request_id: 'req_batch_code', error: {
        message: 'Synthetic gateway response.', code, type, retryable: true,
      } }, { status, headers: { 'x-should-retry': 'true', 'retry-after-ms': '1' } });
    },
  });
  try {
    const result = await runBatch(client, [{ id: 'one', request, expected: { q: true } }], {
      runId: 'gateway-code-retries', resultsPath,  deadlineMs: 10000, concurrency: 1,
      maxRetries: 3, sleep: async delay => { waits.push(delay); },
    });
    expect(calls).toBe(1);
    expect(waits).toEqual([]);
    expect(observed).toHaveLength(1);
    expect((observed[0] as Error).constructor).toBe(ErrorClass);
    expect(observed[0]).toMatchObject({ code, status, type, request_id: 'req_batch_code' });
    const saved = JSON.parse(readFileSync(resultsPath, 'utf8'));
    expect(saved).toMatchObject({ code, outcome: 'error', attempts: 1 });
    expect(result.records).toEqual([saved]);
  } finally {
    rmSync(folder, { recursive: true, force: true });
  }
});
