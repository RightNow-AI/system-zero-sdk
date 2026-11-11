import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import { APIPromise, type RequestOptions, type ResponseMetadata } from '../src/client.js';
import { AuthenticationError, NotFoundError, responseError } from '../src/errors.js';
import {
  createDecisionRecords, DECISION_RECORD_ERROR_CODES, DecisionNotFoundError,
  type DecisionRecordsRequest, type DecisionTrace,
} from '../src/decision-records.js';

const fixtures = new URL('../../fixtures/', import.meta.url);
const contract = JSON.parse(readFileSync(new URL('decision-records/contract.json', fixtures), 'utf8'));
const metadata: ResponseMetadata = {
  requestId: 'req_synthetic_lookup', clientRequestId: 'synthetic-client', idempotencyKey: null,
  rateLimit: { limit: null, remaining: null, reset: null }, latencyMs: null, replayed: false, dangerZone: [],
};

function result<T>(data: T): APIPromise<T> {
  return new APIPromise(Promise.resolve({
    data, metadata, response: new Response(JSON.stringify(data), { headers: contract.response_headers }),
  }));
}

describe('decision record SDK contract (shared fixtures)', () => {
  it.each(contract.decision_fixtures as string[])('preserves retained %s without normalization', async name => {
    const decision = JSON.parse(readFileSync(new URL(name, fixtures), 'utf8'));
    const request = vi.fn(() => result(decision));
    const records = createDecisionRecords(request as DecisionRecordsRequest);
    const options: RequestOptions = { clientRequestId: 'synthetic-client', headers: { 'x-synthetic': 'test' } };
    const call = records.getDecision(contract.id, options);
    expect(await call).toEqual(decision);
    expect(request).toHaveBeenCalledExactlyOnceWith('GET', contract.decision_path, undefined, options);
    const detailed = await call.withResponse();
    expect(detailed.data).toBe(decision);
    expect(detailed.metadata).toBe(metadata);
    expect(await detailed.response.json()).toEqual(decision);
    expect(options).toEqual({ clientRequestId: 'synthetic-client', headers: { 'x-synthetic': 'test' } });
  });

  it('uses the same lookup for a stream decision ID', async () => {
    const decision = JSON.parse(readFileSync(new URL('decide-v1-boolean.json', fixtures), 'utf8'));
    const frame = { type: 'decision', state_version: 7, reply: decision };
    const request = vi.fn(() => result(decision));
    expect(await createDecisionRecords(request as DecisionRecordsRequest).getDecision(frame.reply.id)).toEqual(decision);
    expect(request).toHaveBeenCalledExactlyOnceWith('GET', contract.decision_path, undefined, {});
  });

  it.each([{}, ...contract.trace_variants])('preserves content-free trace and OPEN null fields %j', async variant => {
    const trace: DecisionTrace = { ...contract.trace, ...variant };
    const request = vi.fn(() => result(trace));
    const call = createDecisionRecords(request as DecisionRecordsRequest).getDecisionTrace(contract.id);
    expect(await call).toEqual(trace);
    expect(request).toHaveBeenCalledExactlyOnceWith('GET', contract.trace_path, undefined, {});
    expect((await call.withResponse()).data).toBe(trace);
    expect(trace.usage_units.cached_media_tokens).toBeNull();
    expect(trace.timings).toEqual({ queue_ms: null, node_ms: null, settle_ms: null });
    expect(trace.attachments).toBeNull();
    expect(Object.keys(trace)).not.toContain('answers');
  });

  it.each(['getDecision', 'getDecisionTrace'] as const)('maps only lookup absence to a typed 404 for %s', async method => {
    const original = responseError(404, new Headers(contract.not_found_headers), contract.not_found, false);
    const request = vi.fn(() => new APIPromise(Promise.reject(original)));
    const error = await createDecisionRecords(request as DecisionRecordsRequest)[method](contract.id).catch(error => error);
    expect(error).toBeInstanceOf(DecisionNotFoundError);
    if (!(error instanceof DecisionNotFoundError)) throw error;
    expect(error).toBeInstanceOf(NotFoundError);
    expect(error).toMatchObject({ status: 404, type: 'not_found_error', code: 'decision_not_found', retryable: false, request_id: contract.not_found.request_id });
    expect(error.headers.get('x-request-id')).toBe('req_synthetic_lookup');
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('keeps other typed failures and their metadata unchanged', async () => {
    const original = new AuthenticationError('Synthetic auth error', { status: 401, code: 'invalid_api_key' });
    const request = vi.fn(() => new APIPromise(Promise.reject(original)));
    await expect(createDecisionRecords(request as DecisionRecordsRequest).getDecision(contract.id)).rejects.toBe(original);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it.each(contract.invalid_ids as string[])('rejects an invalid ID before request dispatch: %j', id => {
    const request = vi.fn();
    const records = createDecisionRecords(request as DecisionRecordsRequest);
    expect(() => records.getDecision(id)).toThrow(TypeError);
    expect(() => records.getDecisionTrace(id)).toThrow(TypeError);
    expect(request).not.toHaveBeenCalled();
  });

  it('publishes the new typed code without changing existing SDK registries', () => {
    expect(DECISION_RECORD_ERROR_CODES.decision_not_found).toEqual({ status: 404, type: 'not_found_error', retryable: false });
  });
});
