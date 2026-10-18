import { describe, expect, it } from 'vitest';
import * as sdk from '../src/index.js';
import type { BatchItem, EvaluationReport, FlowDefinition } from '../src/index.js';
import { runFlow } from '../src/flows.js';
import { runBatch } from '../src/batch.js';

const request = {
  model: 'system-zero',
  state: 'SYNTHETIC: The review is complete.',
  questions: { ready: { type: 'boolean' as const, instructions: 'Is the review complete?' } },
};

describe('public workflow entrypoints', () => {
  it('exposes flows and accepts a typed definition through the package entrypoint', () => {
    const definition: FlowDefinition = {
      version: 1,
      request: { model: request.model, state: request.state },
      steps: [{ id: 'review', questions: request.questions, next: { rules: [], default: null } }],
    };
    expect(sdk.runFlow).toBe(runFlow);
    expect(sdk.validateFlow(definition)).toEqual(definition);
    expect(sdk.flowIdempotencyKey('public-flow', 'review', request)).toBe(
      sdk.flowIdempotencyKey('public-flow', 'review', structuredClone(request)),
    );
    expect(sdk.flowRequestHash(request)).toMatch(/^[a-f0-9]{64}$/u);
  });

  it('exposes batch execution and deterministic identity helpers', () => {
    const item: BatchItem = { id: 'review', request, expected: { ready: true } };
    expect(sdk.runBatch).toBe(runBatch);
    const hash = sdk.requestHash(item.request);
    expect(sdk.idempotencyKey('public-batch', item.id, hash)).toBe(
      sdk.idempotencyKey('public-batch', item.id, sdk.requestHash(structuredClone(item.request))),
    );
  });

  it('exposes evaluation and report helpers without a client or a network call', () => {
    const report: EvaluationReport = sdk.evaluate([]);
    expect(report.items.total).toBe(0);
    expect(JSON.parse(sdk.reportJson(report))).toEqual(report);
    expect(sdk.reportMarkdown(report)).toContain('0');
    expect(() => sdk.validateExpected(request, { ready: true })).not.toThrow();
    const refusal = sdk.scoreRefusal(request, { ready: true }, 'question_not_grounded');
    expect(refusal[0]?.outcome).toBe('refused');
    expect(sdk.scoreReply).toBeTypeOf('function');
  });
});
