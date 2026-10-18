import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { SystemZeroClient } from '../src/client.js';
import { decisionFor, decisionsFor, type AnswerOrRefusal, type SystemZeroReply } from '../src/decisions.js';
import { SystemZeroError } from '../src/errors.js';
import { flowIdempotencyKey, flowRequestHash, runFlow, validateFlow, type FlowDefinition, type FlowTrace } from '../src/flows.js';
import { idempotencyKey, requestHash, runBatch, type BatchItem } from '../src/batch.js';
import { evaluate, reportJson, reportMarkdown, scoreReply, type EvaluationRecord, type EvaluationReport, type ExpectedAnswers } from '../src/evaluate.js';
import type { DecideRequest } from '../src/api-types.js';

function fixture<T>(name: string): T {
  return JSON.parse(readFileSync(new URL('../../fixtures/' + name, import.meta.url), 'utf8')) as T;
}
interface ReaderCase {
  name: string; accepts: boolean; answer: AnswerOrRefusal; kind?: string;
  metadata?: { preview?: true; calibration_version: string };
}
const readers = ['boolean', 'choice', 'legacy', 'metadata', 'score', 'score-work-bound']
  .flatMap(name => fixture<ReaderCase[]>('decision-reader/' + name + '.json'));

describe('shared decision-reader behavior', () => {
  it.each(readers)('$name', row => {
    const answer = structuredClone(row.answer);
    const reply = { answers: { sample: answer } } as SystemZeroReply;
    const before = structuredClone(reply);
    if (!row.accepts) {
      expect(() => decisionFor(answer)).toThrow(TypeError);
      expect(() => decisionsFor(reply)).toThrow(TypeError);
    } else {
      const direct = decisionFor(answer);
      expect(decisionsFor(reply)).toEqual({ sample: direct });
      expect(direct.kind).toBe(row.kind ?? 'v1');
      if (direct.kind === 'v1') {
        const expected = answer as { decision: boolean | string; confidence: number; alternatives: unknown; calibrated: boolean };
        expect(direct.decision).toBe(expected.decision);
        expect(direct.confidence).toBe(expected.confidence);
        expect(direct.probability).toBe(expected.confidence);
        expect(direct.alternatives).toBe(expected.alternatives);
        expect(direct.calibrated).toBe(expected.calibrated);
        if (row.metadata) {
          expect(direct.preview).toBe(row.metadata.preview);
          expect(direct.calibration_version).toBe(row.metadata.calibration_version);
        }
      } else if (direct.kind === 'legacy') {
        expect(direct.raw).toBe(answer);
        expect(direct.confidence).toBeNull();
        expect(direct.probability).toBeNull();
        expect(direct.alternatives).toEqual([]);
      } else {
        expect(direct.code).toBe((answer as { error: { code: string } }).error.code);
        expect(direct.retryable).toBe(false);
      }
    }
    expect(reply).toEqual(before);
  });
});

interface FlowCase {
  name: string; definition: FlowDefinition; validate_only?: boolean; client_timeout_ms?: number;
  options?: { max_decide_calls?: number; deadline_ms?: number };
  responses?: Array<{ status: number; body: unknown; headers?: Record<string, string> }>;
  expect: { status: string; step_indexes: number[]; error_code?: string; requests?: unknown[];
    decide_calls?: number; step_statuses?: string[]; branch_rule_indexes?: Array<number | null> };
}
const flowCases = fixture<{ cases: FlowCase[] }>('flows/conformance.json').cases;
const flowVectors = fixture<{ hash_vectors: Array<{ value: unknown; sha256: string; idempotency_key: string }> }>('flows/boundaries.json').hash_vectors;

describe('shared flow behavior', () => {
  it.each(flowCases)('$name', async row => {
    if (row.validate_only) {
      expect(() => validateFlow(row.definition)).toThrowError(expect.objectContaining({ code: row.expect.error_code }));
      return;
    }
    const requests: unknown[] = [];
    const responses = [...(row.responses ?? [])];
    const sdk = new SystemZeroClient({ apiKey: 'SYNTHETIC', timeout: row.client_timeout_ms ?? 50, maxRetries: 0,
      fetch: async (_url, init) => {
        requests.push(JSON.parse(String(init?.body)));
        expect(new Headers(init?.headers).get('idempotency-key')).toMatch(/^sz-flow-v1-[a-f0-9]{64}$/u);
        const response = responses.shift();
        if (!response) throw new Error('Unexpected fixture request.');
        return new Response(JSON.stringify(response.body), { status: response.status, headers: response.headers ?? {} });
      } });
    let trace: FlowTrace;
    try {
      trace = await runFlow(sdk, row.definition, { run_id: 'fixture-run', max_decide_calls: 32,
        deadline_ms: 10000, include_content: true, now: () => 0, ...row.options });
      expect(row.expect.error_code).toBeUndefined();
    } catch (error) {
      expect(error).toBeInstanceOf(SystemZeroError);
      expect(error).toMatchObject({ code: row.expect.error_code });
      trace = (error as SystemZeroError & { trace: FlowTrace }).trace;
    }
    expect(trace.status).toBe(row.expect.status);
    expect(trace.steps.map(step => step.step_index)).toEqual(row.expect.step_indexes);
    if (row.expect.step_statuses) expect(trace.steps.map(step => step.status)).toEqual(row.expect.step_statuses);
    if (row.expect.branch_rule_indexes) expect(trace.steps.map(step => step.branch?.rule_index ?? null)).toEqual(row.expect.branch_rule_indexes);
    if (row.expect.requests) expect(requests).toEqual(row.expect.requests);
    if (row.expect.decide_calls !== undefined) expect(trace.decide_calls).toBe(row.expect.decide_calls);
    expect(responses).toHaveLength(0);
  });

  it.each(flowVectors)('pins ordered request identity $sha256', row => {
    expect(flowRequestHash(row.value)).toBe(row.sha256);
    expect(flowIdempotencyKey('fixture-run', 'start', row.value)).toBe(row.idempotency_key);
  });
});

interface EvaluationCase {
  id: string; request: DecideRequest; expected: ExpectedAnswers; reply?: SystemZeroReply;
  outcome: EvaluationRecord['outcome']; code: string | null; latency_ms: number;
}
const evaluation = fixture<{ cases: EvaluationCase[]; bins: number; expected: EvaluationReport }>('batch/evaluation.json');
function evaluationRecords(): EvaluationRecord[] {
  return evaluation.cases.map(row => ({ id: row.id, outcome: row.outcome, latency_ms: row.latency_ms,
    usage: row.reply?.usage ?? null, code: row.code, questions: row.reply ? scoreReply(row.request, row.expected, row.reply) : [] }));
}

describe('shared batch and evaluation behavior', () => {
  it('matches hand-computed metrics, latency, usage and calibration', () => {
    const report = evaluate(evaluationRecords(), { bins: evaluation.bins });
    for (const key of ['items', 'metrics', 'by_type', 'latency_ms', 'usage'] as const) expect(report[key]).toEqual(evaluation.expected[key]);
    const calibration = report.calibration!;
    const expected = evaluation.expected.calibration!;
    expect(calibration.count).toBe(expected.count);
    expect(calibration.ece).toBeCloseTo(expected.ece, 12);
    expect(calibration.brier_score).toBeCloseTo(expected.brier_score, 12);
    expect(calibration.bins).toHaveLength(expected.bins.length);
    expected.bins.forEach((bin, index) => {
      expect(calibration.bins[index]!.count).toBe(bin.count);
      for (const key of ['lower', 'upper', 'accuracy', 'mean_confidence'] as const) {
        if (bin[key] === null) expect(calibration.bins[index]![key]).toBeNull();
        else expect(calibration.bins[index]![key]).toBeCloseTo(bin[key], 12);
      }
    });
    expect(JSON.parse(reportJson(report))).toEqual(report);
    expect(reportMarkdown(report)).toContain(String(report.items.total));
  });

  it('executes fixture replies, persists only receipt fields and resumes without new calls', async () => {
    const scratch = join(process.cwd(), 'tmp');
    mkdirSync(scratch, { recursive: true });
    const temporary = mkdtempSync(join(scratch, 'workflow-test-'));
    const resultsPath = join(temporary, 'receipts.jsonl');
    const rows = evaluation.cases.filter(row => row.reply !== undefined);
    let calls = 0;
    const sdk = new SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 0, timeout: 100,
      fetch: async (_url, init) => {
        const row = rows[calls++];
        if (!row) throw new Error('Unexpected fixture request.');
        expect(JSON.parse(String(init?.body))).toEqual(row.request);
        expect(new Headers(init?.headers).get('idempotency-key')).toBe(idempotencyKey('fixture-run', row.id, requestHash(row.request)));
        return Response.json(row.reply);
      } });
    const items: BatchItem[] = rows.map(row => ({ id: row.id, request: row.request, expected: row.expected }));
    const options = { runId: 'fixture-run', resultsPath, maxDecideCalls: rows.length, deadlineMs: 10000, concurrency: 1 };
    try {
      const result = await runBatch(sdk, items, options);
      expect(result.stopped).toBeNull();
      expect(result.records.map(row => row.outcome)).toEqual(rows.map(() => 'completed'));
      expect(result.records.map(row => row.questions)).toEqual(rows.map(row => scoreReply(row.request, row.expected, row.reply!)));
      expect(result.records.map(row => row.usage)).toEqual(rows.map(row => row.reply?.usage ?? null));
      for (const row of result.records) {
        expect(Object.keys(row).sort()).toEqual(['schema', 'run_id', 'id', 'request_hash', 'expected_hash', 'idempotency_key', 'admitted_at_ms', 'target', 'outcome', 'code', 'attempts', 'latency_ms', 'usage', 'questions'].sort());
      }
      expect((await runBatch(sdk, items, options)).records).toEqual(result.records);
      expect(calls).toBe(rows.length);
    } finally { rmSync(temporary, { recursive: true, force: true }); }
  });
});
