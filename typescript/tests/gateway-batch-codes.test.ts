import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { expect, it } from 'vitest';
import { AuthenticationError, InvalidRequestError, NotFoundError, PermissionError, ServerError,
  SystemZeroClient, runBatch, type DecideRequest } from '../src/index.js';

const request: DecideRequest = {
  model: 'system-zero', state: 'synthetic',
  questions: { q: { type: 'boolean', instructions: 'Is it set?' } },
};

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
