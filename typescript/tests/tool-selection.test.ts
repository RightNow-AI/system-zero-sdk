import { readFileSync } from 'node:fs';
import { expect, it, vi } from 'vitest';
import { APIError, InvalidRequestError, SystemZeroClient, selectTool, toolQuestions } from '../src/index.js';
import type { ToolDefinition, ToolSelectionMode, ToolSelectionResult } from '../src/index.js';

const fixture = JSON.parse(readFileSync(new URL('../../fixtures/tool-selection/sz-tools-v1.json', import.meta.url), 'utf8')) as {
  format: string; cases: Array<{ name: string; tools: ToolDefinition[]; mode: ToolSelectionMode; question?: unknown; error?: string }>;
};
it.each(fixture.cases)('sz-tools-v1: $name', item => {
  expect(fixture.format).toBe('sz-tools-v1');
  if (item.error) {
    expect(() => toolQuestions(item.tools, item.mode)).toThrow(InvalidRequestError);
  } else {
    const compiled = toolQuestions(item.tools, item.mode);
    expect(compiled).toEqual(item.question);
    expect(JSON.stringify(compiled)).toBe(JSON.stringify(item.question));
  }
});
const tools = [{ name: 'lookup' }, { name: 'refund' }];
const request = { model: 'rightnow-decision', state: 'The order is damaged.' };
function reply(winner: string) {
  const probabilities = Object.fromEntries(['lookup', 'refund', 'none'].map(name => [name, name === winner ? 0.8 : 0.1]));
  return { marker: 'untouched', answers: { sz_tool: {
    type: 'choice', choice: winner, probabilities, decision: winner, confidence: 0.8,
    alternatives: Object.keys(probabilities).filter(name => name !== winner).sort().map(decision => ({ decision, confidence: 0.1 })),
    calibrated: false, calibration_version: 'unpublished',
  } } };
}
it.each(['lookup', 'none'])('returns %s and the untouched reply', async winner => {
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch: async () => Response.json(reply(winner)) });
  const decide = client.decide.bind(client);
  let original: ToolSelectionResult['reply'];
  vi.spyOn(client, 'decide').mockImplementation((...args) => {
    const result = decide(...args);
    void result.then(value => { original = value; });
    return result;
  });
  const result = await selectTool(client, request, tools);
  expect(result).toMatchObject({ decision: winner === 'none' ? 'none' : 'call', tool: winner === 'none' ? null : winner, confidence: 0.8, forced: false });
  expect(result.reply).toBe(original!);
  expect(result.reply).toEqual(reply(winner));
  expect(result.alternatives).toEqual(reply(winner).answers.sz_tool.alternatives.map(item => ({ tool: item.decision, confidence: item.confidence })));
});
it('forces a required single tool without transport or confidence', async () => {
  const fetch = vi.fn();
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch });
  expect(await selectTool(client, request, [tools[0]!], 'required')).toEqual({ decision: 'call', tool: 'lookup', confidence: null, alternatives: [], reply: null, forced: true });
  expect(fetch).not.toHaveBeenCalled();
});
it('sends required with two tools and keeps legacy confidence unavailable', async () => {
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch: async (_url, init) => {
    expect(JSON.parse(String(init?.body)).questions).toEqual(toolQuestions(tools, 'required'));
    return Response.json({ answers: { sz_tool: { type: 'choice', choice: 'lookup', probabilities: { lookup: 0.8, refund: 0.2 }, confidence: 0.6, calibration_version: 'unpublished' } } });
  } });
  expect(await selectTool(client, request, tools, 'required')).toMatchObject({ decision: 'call', tool: 'lookup', confidence: null, alternatives: [], forced: false });
});
it('refuses invalid definitions before transport', async () => {
  const fetch = vi.fn();
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch });
  await expect(selectTool(client, request, [{ name: 'none' }])).rejects.toBeInstanceOf(InvalidRequestError);
  expect(fetch).not.toHaveBeenCalled();
});
it('passes the request, attachments and all options through existing decide', async () => {
  const seen: Array<{ headers: Headers; body: unknown }> = [];
  const client = new SystemZeroClient({ apiKey: 'synthetic', maxRetries: 1, sleep: async () => {}, random: () => 0, fetch: async (_url, init) => {
    seen.push({ headers: new Headers(init?.headers), body: JSON.parse(String(init?.body)) });
    return seen.length === 1 ? Response.json({ error: { code: 'service_unavailable', message: 'Retry.', retryable: true } }, { status: 503 }) : Response.json(reply('refund'));
  } });
  const options = { dangerZone: 'long_context_1m' as const, idempotencyKey: 'selection-key', clientRequestId: 'selection-client', retryBudget: 1000, headers: { 'x-example': 'kept' } };
  const decide = vi.spyOn(client, 'decide');
  await selectTool(client, request, tools, 'auto', options);
  expect(decide.mock.calls[0]?.[1]).toBe(options);
  expect(seen).toHaveLength(2);
  for (const item of seen) {
    expect(item.body).toEqual({ ...request, questions: toolQuestions(tools) });
    expect(item.headers.get('rightnow-danger-zone')).toBe('long_context_1m');
    expect(item.headers.get('idempotency-key')).toBe('selection-key');
    expect(item.headers.get('x-example')).toBe('kept');
  }
  const attachments = [{ name: 'paper', type: 'document' as const, data: 'data:application/pdf;base64,JVBERi0xLjcK' }];
  const ordinary = new SystemZeroClient({ apiKey: 'synthetic', fetch: async (_url, init) => {
    expect(JSON.parse(String(init?.body)).attachments).toEqual(attachments);
    return Response.json(reply('lookup'));
  } });
  await selectTool(ordinary, { ...request, attachments }, tools);
});
it('ends after the normal retry budget for a possibly refused question', async () => {
  const fetch = vi.fn(async () => Response.json({ error: { code: 'service_unavailable', message: 'Retry.', retryable: true } }, { status: 503 }));
  const client = new SystemZeroClient({ apiKey: 'synthetic', maxRetries: 2, sleep: async () => {}, random: () => 0, fetch });
  await expect(selectTool(client, request, tools)).rejects.toMatchObject({ message: 'No decision, possibly refused.', code: 'service_unavailable', retryable: false });
  expect(fetch).toHaveBeenCalledTimes(3);
});
it.each([{}, { sz_tool: { error: { code: 'question_not_grounded', retryable: false } } }])('reports no decision without another request', async answers => {
  const fetch = vi.fn(async () => Response.json({ answers }));
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch });
  await expect(selectTool(client, request, tools)).rejects.toThrow('No decision, possibly refused.');
  expect(fetch).toHaveBeenCalledTimes(1);
});
it('preserves ordinary errors', async () => {
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch: async () => Response.json({ error: { code: 'insufficient_credits', message: 'Add credit.' } }, { status: 402 }) });
  await expect(selectTool(client, request, tools)).rejects.toBeInstanceOf(APIError);
});
