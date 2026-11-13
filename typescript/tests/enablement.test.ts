import ts from 'typescript';
import { gunzipSync } from 'node:zlib';
import { fileURLToPath } from 'node:url';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { expect, it } from 'vitest';
import { SystemZeroClient, InvalidRequestError, LongContextNotAvailableError, VideoNotAvailableError, videoAttachment, runBatch, runFlow } from '../src/index.js';
import type { RequestOptions } from '../src/client.js';
import oneMillionCases from '../../fixtures/long-context/1m.json';
import { SystemZeroStreamClient } from '../src/index.js';
import type { EnabledDecideRequest, VideoRequest } from '../src/index.js';

const request = { model: 'rightnow-decision', state: 'sample', questions: { q: { type: 'boolean' as const, instructions: 'Is it set?' } } };
const options = (value: unknown): RequestOptions => value as RequestOptions;
it('sends the explicit option on every retry and replay', async () => {
  const seen: Headers[] = [];
  const client = new SystemZeroClient({ apiKey: 'synthetic', sleep: async () => {}, random: () => 0, fetch: async (_url, init) => {
    seen.push(new Headers(init?.headers));
    return seen.length === 1 ? Response.json({ error: { code: 'rate_limit_exceeded', type: 'rate_limit_error', message: 'Retry.', retryable: true } }, { status: 429 }) : Response.json({ answers: {} }, { headers: { 'x-rightnow-idempotent-replay': 'true' } });
  } });
  await client.decide(request, options({ dangerZone: 'accept', idempotencyKey: 'same' }));
  expect(seen).toHaveLength(2);
  for (const headers of seen) { expect(headers.get('RightNow-Danger-Zone')).toBe('accept'); expect(headers.get('idempotency-key')).toBe('same'); expect(headers.has('x-system-zero-long-context')).toBe(false); }
});
it.each(['Accept', '', null, true, 1])('refuses an invalid option before transport: %s', async value => {
  let calls = 0;
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch: async () => { calls++; return Response.json({}); } });
  await expect(Promise.resolve().then(() => client.decide(request, options({ dangerZone: value })))).rejects.toBeInstanceOf(InvalidRequestError);
  expect(calls).toBe(0);
});
it.each(['RightNow-Danger-Zone', 'rightnow-danger-zone', 'sZ-dAnGeR-zOnE'])('refuses caller header %s with and without the option', async name => {
  let calls = 0;
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch: async () => { calls++; return Response.json({}); } });
  for (const extra of [{}, { dangerZone: 'accept' }]) await expect(Promise.resolve().then(() => client.decide(request, options({ ...extra, headers: { [name]: 'accept' } })))).rejects.toBeInstanceOf(InvalidRequestError);
  expect(calls).toBe(0);
});
it('ordinary helper requests and large states add no opt-in', async () => {
  const seen: Headers[] = [];
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch: async (_url, init) => { seen.push(new Headers(init?.headers)); return Response.json({ answers: {} }); } });
  const bytes = new Uint8Array(24); bytes.set([0, 0, 0, 24, 102, 116, 121, 112, 105, 115, 111, 109]);
  await client.decide({ ...request, attachments: [videoAttachment({ name: 'clip', data: bytes })] });
  await client.decide({ ...request, state: 'x'.repeat(140000) });
  expect(seen.every(headers => !headers.has('RightNow-Danger-Zone'))).toBe(true);
});
it.each(['billing', 'class', 'deny_list', 'global_switch', 'account_deleted', 'future_rule'])('keeps typed eligibility %s and the refusal envelope', async eligibility => {
  let calls = 0;
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch: async () => { calls++; return Response.json({ request_id: 'req_held', error: { code: 'long_context_not_available', type: 'not_found_error', message: 'This route is not available.', details: { eligibility } } }, { status: 404 }); } });
  try { await client.decide(request); throw new Error('Expected refusal.'); } catch (error) {
    expect(error).toBeInstanceOf(LongContextNotAvailableError);
    expect(error).toMatchObject({ status: 404, code: 'long_context_not_available', message: 'This route is not available.', request_id: 'req_held', eligibility, retryable: false });
  }
  expect(calls).toBe(1);
});
it('keeps the typed input refusal', async () => {
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch: async () => Response.json({ request_id: 'req_held', error: { code: 'video_not_available', type: 'not_found_error', message: 'This input type is not supported.' } }, { status: 404 }) });
  await expect(client.decide(request)).rejects.toBeInstanceOf(VideoNotAvailableError);
});

it.each(['accept', 'long_context_64k', 'long_context_128k', 'long_context_1m'] as const)('batch forwards the option and refuses an invalid option before admission (%s)', async selected => {
  const folder = mkdtempSync(join(tmpdir(), 'sdk-enablement-'));
  const seen: Headers[] = [];
  const client = new SystemZeroClient({ apiKey: 'synthetic', timeout: 1000, maxRetries: 0, fetch: async (_url, init) => {
    seen.push(new Headers(init?.headers));
    return Response.json({ error: { code: 'long_context_not_available', type: 'not_found_error', message: 'This route is not available.' } }, { status: 404 });
  } });
  const items = [{ id: 'one', request, expected: { q: true } }];
  try {
    const common = { runId: 'enabled', resultsPath: join(folder, 'rows.jsonl'), deadlineMs: 10000, concurrency: 1 as const };
    const result = await runBatch(client, items, { ...common, dangerZone: selected });
    expect(result.records[0]?.outcome).toBe('held');
    expect(seen[0]?.get('RightNow-Danger-Zone')).toBe(selected);
    await expect(runBatch(client, items, { ...common, dangerZone: 'wrong' as 'accept' })).rejects.toBeInstanceOf(InvalidRequestError);
    expect(seen).toHaveLength(1);
    if (selected === 'long_context_1m') {
      // Simulate a lost result row while retaining the admission journal.
      writeFileSync(common.resultsPath, '');
      await runBatch(client, items, { ...common, dangerZone: selected });
      expect(seen).toHaveLength(2);
      expect(seen[1]?.get('rightnow-danger-zone')).toBe(selected);
      expect(seen[1]?.get('idempotency-key')).toBe(seen[0]?.get('idempotency-key'));
      await expect(runBatch(client, items, { ...common, dangerZone: 'long_context_64k' })).rejects.toBeInstanceOf(InvalidRequestError);
      expect(seen).toHaveLength(2);
    }
  } finally { rmSync(folder, { recursive: true, force: true }); }
});
it.each(['accept', 'long_context_64k', 'long_context_128k', 'long_context_1m'] as const)('flow forwards its explicit request option (%s)', async selected => {
  const seen: Headers[] = [];
  const client = new SystemZeroClient({ apiKey: 'synthetic', timeout: 1000, maxRetries: 0, fetch: async (_url, init) => {
    seen.push(new Headers(init?.headers));
    return Response.json({ error: { code: 'long_context_not_available', type: 'not_found_error', message: 'This route is not available.' } }, { status: 404 });
  } });
  const flow = { version: 1, request: { model: request.model, state: request.state }, steps: [{ id: 'one', questions: request.questions, next: { rules: [], default: null } }] };
  await expect(runFlow(client, flow, { run_id: 'enabled', deadline_ms: 10000, dangerZone: selected })).rejects.toBeInstanceOf(LongContextNotAvailableError);
  expect(seen[0]?.get('RightNow-Danger-Zone')).toBe(selected);
});

it('types the required request fields, state-only long classes and bounded score levels', () => {
  const filename = fileURLToPath(new URL('enabled-typecheck.ts', import.meta.url)).replaceAll('\\', '/');
  const source = `import type { LongContextRequest, EnabledDecideRequest, VideoRequest, Model } from '../src/index.js';
import { videoAttachment } from '../src/index.js';
const request: LongContextRequest = { model: 'rightnow-decision', state: 'sample', questions: { q: { type: 'score', instructions: 'Rate it.', criteria: ['low', 'high'] } } };
const enabled: EnabledDecideRequest = request;
const pdf: EnabledDecideRequest = { model: 'rightnow-decision', state: 'Read the PDF.', questions: { q: { type: 'boolean', instructions: 'Ready?' } }, attachments: [{ name: 'document', type: 'document', data: 'data:application/pdf;base64,JVBERi0xLjcK' }] };
declare const client: import('../src/index.js').SystemZeroClient;
client.decide(pdf);
// @ts-expect-error model, state and questions are required
const missing: LongContextRequest = {};
// @ts-expect-error native long classes exclude all attachments
request.attachments = [{ name: 'paper', type: 'document', data: 'data:application/pdf;base64,JVBERi0=' }];
// @ts-expect-error long context scores have at most four levels
request.questions.q = { type: 'score', instructions: 'Rate it.', criteria: ['a','b','c','d','e'] };
const video: VideoRequest = { model: 'rightnow-decision', state: 'sample', questions: { q: { type: 'boolean', instructions: 'Ready?' } }, attachments: [videoAttachment({ name: 'clip', data: new Uint8Array() })] };
// @ts-expect-error the video request requires model, state and questions
const incomplete: VideoRequest = { attachments: [] };
declare const model: Model;
const bound: number | undefined = model.limits.choice_options.long_context?.max;
const bands: Model['limits']['long_context'] = [{ header_value: 'long_context_64k', min_state_tokens: 38001, state_tokens: 65536, rendered_tokens: 69632, attachments: 0 }];
const header: string | undefined = model.limits.long_context?.[0]?.header_value;
const minimum: number | undefined = model.limits.long_context?.[0]?.min_state_tokens;
const state: number | undefined = model.limits.long_context?.[0]?.state_tokens;
const rendered: number | undefined = model.limits.long_context?.[0]?.rendered_tokens;
const attachments: 0 | undefined = model.limits.long_context?.[0]?.attachments;
// @ts-expect-error every long band requires its lower bound
const incompleteBand: NonNullable<Model['limits']['long_context']>[number] = { header_value: 'long_context_64k', state_tokens: 65536, rendered_tokens: 69632, attachments: 0 };
// @ts-expect-error long bands exclude attachments
const mediaBand: NonNullable<Model['limits']['long_context']>[number] = { ...bands[0], attachments: 1 };
void bands; void header; void minimum; void state; void rendered; void attachments; void incompleteBand; void mediaBand;
void bound; void enabled; void missing; void video; void incomplete;
`;
  const options: ts.CompilerOptions = { strict: true, skipLibCheck: true, noEmit: true, types: ['node'], typeRoots: [fileURLToPath(new URL('../node_modules/@types/', import.meta.url)), join(process.cwd(), 'node_modules/@types')], target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.NodeNext, moduleResolution: ts.ModuleResolutionKind.NodeNext };
  const host = ts.createCompilerHost(options);
  const read = host.readFile, exists = host.fileExists, get = host.getSourceFile;
  host.readFile = name => name === filename ? source : read(name);
  host.fileExists = name => name === filename || exists(name);
  host.getSourceFile = (name, version, onError, create) => name === filename ? ts.createSourceFile(name, source, version, true) : get(name, version, onError, create);
  const program = ts.createProgram([filename], options, host);
  expect(ts.getPreEmitDiagnostics(program).map(diagnostic => ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'))).toEqual([]);
});

it('refuses a reserved header on model discovery before transport', async () => {
  let calls = 0;
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch: async () => { calls++; return Response.json({ data: [] }); } });
  await expect(Promise.resolve().then(() => client.models.list({ headers: { 'sZ-Danger-Zone': 'accept' } }))).rejects.toBeInstanceOf(InvalidRequestError);
  expect(calls).toBe(0);
});

it('sends ordinary inline PDF and video through exported request types without opt-in', async () => {
  const seen: { body: unknown; headers: Headers }[] = [];
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch: async (_url, init) => {
    seen.push({ body: JSON.parse(String(init?.body)), headers: new Headers(init?.headers) });
    return Response.json({ answers: {} });
  } });
  const pdf: EnabledDecideRequest = { ...request, attachments: [{ name: 'document', type: 'document', data: 'data:application/pdf;base64,JVBERi0xLjcK' }] };
  const bytes = new Uint8Array(24); bytes.set([0, 0, 0, 24, 102, 116, 121, 112, 105, 115, 111, 109]);
  const video: VideoRequest = { ...request, attachments: [videoAttachment({ name: 'clip', data: bytes })] };
  await client.decide(pdf); await client.decide(video);
  expect(seen.map(entry => entry.body)).toEqual([pdf, video]);
  expect(seen.every(entry => !entry.headers.has('RightNow-Danger-Zone'))).toBe(true);
});

for (const selected of ['long_context_64k', 'long_context_128k'] as const) {
  it(`retains ${selected} through retries and exposes the response class`, async () => {
    const seen: Headers[] = [];
    const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', random: () => 0, sleep: async () => {},
      fetch: async (_url, init) => {
        seen.push(new Headers(init?.headers));
        return seen.length === 1 ? Response.json({ error: { code: 'model_unavailable', retryable: true } }, { status: 503, headers: { 'Retry-After-Ms': '1' } })
          : Response.json({ answers: {}, usage: { long_input_tokens: 48000 } }, { headers: { 'rightnow-danger-zone': selected } });
      } });
    const result = await client.decide(request, { dangerZone: selected }).withResponse();
    expect(seen.map(headers => headers.get('rightnow-danger-zone'))).toEqual([selected, selected]);
    expect(seen[0]?.get('idempotency-key')).toBe(seen[1]?.get('idempotency-key'));
    expect(result.data.usage.long_input_tokens).toBe(48000);
    expect(result.metadata.dangerZone).toEqual([selected]);
  });
  it(`refuses attachments with ${selected} before transport`, () => {
    let calls = 0;
    const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: async () => { calls++; return Response.json({}); } });
    expect(() => client.decide({ ...request, attachments: [{ name: 'paper', type: 'document', data: 'data:application/pdf;base64,JVBERi0=' }] }, { dangerZone: selected }))
      .toThrow(expect.objectContaining({ status: 404, code: 'long_context_not_available' }));
    expect(calls).toBe(0);
  });
}

it('preserves direct model limits and optional long class bands through discovery', async () => {
  const band = { header_value: 'long_context_64k', min_state_tokens: 38001, state_tokens: 65536, rendered_tokens: 69632, attachments: 0 };
  const direct = { id: 'rightnow-decision', limits: { state_tokens: 38000, rendered_tokens: 40960 } };
  const payload = { object: 'list', data: [direct, { ...direct, limits: { ...direct.limits, long_context: [band] } }] };
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 0, fetch: async (url, init) => {
    expect(new URL(String(url)).pathname).toBe('/v1/decision-model/models');
    expect(new Headers(init?.headers).has('rightnow-danger-zone')).toBe(false);
    return Response.json(payload);
  } });
  const result = await client.models.list();
  expect(result).toEqual(payload);
  expect(result.data[0]!.limits.long_context).toBeUndefined();
  expect(result.data[1]!.limits.long_context).toEqual([band]);
});
it('1M generates a key even with retries disabled', async () => {
  const seen: Headers[] = [];
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 0, retryBudget: 0,
    fetch: async (_url, init) => { seen.push(new Headers(init?.headers)); return Response.json({ answers: {} }); } });
  await client.decide(request, options({ dangerZone: 'long_context_1m' }));
  expect(seen[0]?.get('idempotency-key')).toBeTruthy();
});
it('1M requires exactly one question before transport', () => {
  let calls = 0;
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: async () => { calls++; return Response.json({}); } });
  expect(() => client.decide({ ...request, questions: {} }, options({ dangerZone: 'long_context_1m' })))
    .toThrow(expect.objectContaining({ code: 'questions_too_large', param: 'questions' }));
  expect(calls).toBe(0);
});

it.each(oneMillionCases)('1M locally refuses %j', test => {
  let calls = 0;
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: async () => { calls++; return Response.json({}); } });
  expect(() => client.decide({ ...request, ...test.patch } as unknown as Parameters<typeof client.decide>[0], { dangerZone: 'long_context_1m' }))
    .toThrow(expect.objectContaining({ status: test.status, code: test.code, param: test.param }));
  expect(calls).toBe(0);
});
it.each(['', ' ', '\t'])('1M refuses an explicit blank key %j from options or headers', key => {
  let calls = 0;
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 0, fetch: async () => { calls++; return Response.json({}); } });
  for (const extra of [{ idempotencyKey: key }, { headers: { 'Idempotency-Key': key } }]) {
    expect(() => client.decide(request, { dangerZone: 'long_context_1m', ...extra }))
      .toThrow(expect.objectContaining({ status: 400, code: 'invalid_request', param: 'Idempotency-Key' }));
  }
  expect(calls).toBe(0);
});
it.each([undefined, null, []])('1M accepts absent, null and empty attachments: %j', async attachments => {
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 0, fetch: async (_url, init) => {
    const headers = new Headers(init?.headers);
    expect(headers.get('rightnow-danger-zone')).toBe('long_context_1m');
    expect(headers.get('idempotency-key')).toBe('caller-key');
    return Response.json({ answers: {} }, { headers: { 'rightnow-danger-zone': 'long_context_1m' } });
  } });
  const result = await client.decide({ ...request, attachments }, { dangerZone: 'long_context_1m', idempotencyKey: 'caller-key' }).withResponse();
  expect(result.metadata.dangerZone).toEqual(['long_context_1m']);
});
it('1M measures the exact UTF-8 body boundary', async () => {
  let calls = 0;
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 0, fetch: async (_url, init) => {
    calls++;
    const bytes = Buffer.from(await new Response(init?.body).arrayBuffer());
    const plain = new Headers(init?.headers).get('content-encoding') === 'gzip' ? gunzipSync(bytes) : bytes;
    expect(plain.byteLength).toBe(8_388_608); return Response.json({ answers: {} });
  } });
  const overhead = Buffer.byteLength(JSON.stringify({ ...request, state: '' }));
  const state = '\u00e9' + 'x'.repeat(8_388_608 - overhead - 2);
  await client.decide({ ...request, state }, { dangerZone: 'long_context_1m' });
  expect(() => client.decide({ ...request, state: state + 'x' }, { dangerZone: 'long_context_1m' }))
    .toThrow(expect.objectContaining({ status: 413, code: 'payload_too_large' }));
  expect(calls).toBe(1);
});
it('1M refuses stream start and construction before connecting', async () => {
  expect(() => new SystemZeroStreamClient({ apiKey: 'SYNTHETIC', dangerZone: 'long_context_1m' }))
    .toThrow(expect.objectContaining({ status: 404, code: 'long_context_not_available' }));
  const client = new SystemZeroStreamClient({ apiKey: 'SYNTHETIC' });
  await expect(client.start(request, { dangerZone: 'long_context_1m' })).rejects.toMatchObject({ status: 404, code: 'long_context_not_available' });
});
it.each([false, true])('1M retries cold admission with the same class and key (workflow=%s)', async workflow => {
  const { withRetryContext } = await import('../src/client.js');
  const seen: Headers[] = [];
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', maxRetries: 1, sleep: async () => {}, random: () => 0,
    fetch: async (_url, init) => {
      seen.push(new Headers(init?.headers));
      return seen.length === 1 ? Response.json({ error: { code: 'service_unavailable' } }, { status: 503 }) : Response.json({ answers: {} });
    } });
  await client.decide(request, withRetryContext({ dangerZone: 'long_context_1m' }, { workflowRecovery: workflow }));
  expect(seen).toHaveLength(2);
  expect(seen[0]?.get('idempotency-key')).toBeTruthy();
  expect(seen[1]?.get('idempotency-key')).toBe(seen[0]?.get('idempotency-key'));
  expect(seen.map(headers => headers.get('rightnow-danger-zone'))).toEqual(['long_context_1m', 'long_context_1m']);
});
it.each([[402, 'insufficient_credits'], [429, 'long_context_daily_budget_exhausted']] as const)('1M never retries %s %s', async (status, code) => {
  let calls = 0;
  const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', sleep: async () => {}, fetch: async () => {
    calls++; return Response.json({ error: { code, retryable: true } }, { status, headers: { 'x-should-retry': 'true' } });
  } });
  await expect(client.decide(request, { dangerZone: 'long_context_1m' })).rejects.toMatchObject({ status, code });
  expect(calls).toBe(1);
});
