import ts from 'typescript';
import { fileURLToPath } from 'node:url';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { expect, it } from 'vitest';
import { SystemZeroClient, InvalidRequestError, LongContextNotAvailableError, VideoNotAvailableError, videoAttachment, runBatch, runFlow } from '../src/index.js';
import type { RequestOptions } from '../src/client.js';
import type { EnabledDecideRequest, VideoRequest } from '../src/index.js';

const request = { model: 'system-zero', state: 'sample', questions: { q: { type: 'boolean' as const, instructions: 'Is it set?' } } };
const options = (value: unknown): RequestOptions => value as RequestOptions;
it('sends the explicit option on every retry and replay', async () => {
  const seen: Headers[] = [];
  const client = new SystemZeroClient({ apiKey: 'synthetic', sleep: async () => {}, random: () => 0, fetch: async (_url, init) => {
    seen.push(new Headers(init?.headers));
    return seen.length === 1 ? Response.json({ error: { code: 'rate_limit_exceeded', type: 'rate_limit_error', message: 'Retry.', retryable: true } }, { status: 429 }) : Response.json({ answers: {} }, { headers: { 'x-rightnow-idempotent-replay': 'true' } });
  } });
  await client.decide(request, options({ dangerZone: 'accept', idempotencyKey: 'same' }));
  expect(seen).toHaveLength(2);
  for (const headers of seen) { expect(headers.get('SZ-Danger-Zone')).toBe('accept'); expect(headers.get('idempotency-key')).toBe('same'); expect(headers.has('x-system-zero-long-context')).toBe(false); }
});
it.each(['Accept', '', null, true, 1])('refuses an invalid option before transport: %s', async value => {
  let calls = 0;
  const client = new SystemZeroClient({ apiKey: 'synthetic', fetch: async () => { calls++; return Response.json({}); } });
  await expect(Promise.resolve().then(() => client.decide(request, options({ dangerZone: value })))).rejects.toBeInstanceOf(InvalidRequestError);
  expect(calls).toBe(0);
});
it.each(['SZ-Danger-Zone', 'sz-danger-zone', 'sZ-dAnGeR-zOnE'])('refuses caller header %s with and without the option', async name => {
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
  expect(seen.every(headers => !headers.has('SZ-Danger-Zone'))).toBe(true);
});
it.each(['billing', 'opt_in', 'class', 'deny_list', 'global_switch', 'account_deleted', 'future_rule'])('keeps typed eligibility %s and the refusal envelope', async eligibility => {
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

it('batch forwards the option and refuses an invalid option before admission', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sdk-enablement-'));
  const seen: Headers[] = [];
  const client = new SystemZeroClient({ apiKey: 'synthetic', timeout: 1000, maxRetries: 0, fetch: async (_url, init) => {
    seen.push(new Headers(init?.headers));
    return Response.json({ error: { code: 'long_context_not_available', type: 'not_found_error', message: 'This route is not available.' } }, { status: 404 });
  } });
  const items = [{ id: 'one', request, expected: { q: true } }];
  try {
    const common = { runId: 'enabled', resultsPath: join(folder, 'rows.jsonl'), deadlineMs: 10000, concurrency: 1 as const };
    const result = await runBatch(client, items, { ...common, dangerZone: 'accept' });
    expect(result.records[0]?.outcome).toBe('held');
    expect(seen[0]?.get('SZ-Danger-Zone')).toBe('accept');
    await expect(runBatch(client, items, { ...common, dangerZone: 'wrong' as 'accept' })).rejects.toBeInstanceOf(InvalidRequestError);
    expect(seen).toHaveLength(1);
  } finally { rmSync(folder, { recursive: true, force: true }); }
});
it('flow forwards its explicit request option', async () => {
  const seen: Headers[] = [];
  const client = new SystemZeroClient({ apiKey: 'synthetic', timeout: 1000, maxRetries: 0, fetch: async (_url, init) => {
    seen.push(new Headers(init?.headers));
    return Response.json({ error: { code: 'long_context_not_available', type: 'not_found_error', message: 'This route is not available.' } }, { status: 404 });
  } });
  const flow = { version: 1, request: { model: request.model, state: request.state }, steps: [{ id: 'one', questions: request.questions, next: { rules: [], default: null } }] };
  await expect(runFlow(client, flow, { run_id: 'enabled', deadline_ms: 10000, dangerZone: 'accept' })).rejects.toBeInstanceOf(LongContextNotAvailableError);
  expect(seen[0]?.get('SZ-Danger-Zone')).toBe('accept');
});

it('types the required request fields, document branch and bounded score levels', () => {
  const filename = fileURLToPath(new URL('enabled-typecheck.ts', import.meta.url));
  const source = `import type { LongContextRequest, EnabledDecideRequest, VideoRequest, Model } from '../src/index.js';
import { videoAttachment } from '../src/index.js';
const request: LongContextRequest = { model: 'system-zero', state: 'sample', questions: { q: { type: 'score', instructions: 'Rate it.', criteria: ['low', 'high'] } } };
const enabled: EnabledDecideRequest = request;
const pdf: EnabledDecideRequest = { model: 'system-zero', state: 'Read the PDF.', questions: { q: { type: 'boolean', instructions: 'Ready?' } }, attachments: [{ name: 'document', type: 'document', data: 'data:application/pdf;base64,JVBERi0xLjcK' }] };
declare const client: import('../src/index.js').SystemZeroClient;
client.decide(pdf);
// @ts-expect-error model, state and questions are required
const missing: LongContextRequest = {};
// @ts-expect-error the long context document branch excludes image inputs
request.attachments = [{ name: 'picture', type: 'image', data: 'data:image/png;base64,AA==' }];
// @ts-expect-error long context scores have at most four levels
request.questions.q = { type: 'score', instructions: 'Rate it.', criteria: ['a','b','c','d','e'] };
const video: VideoRequest = { model: 'system-zero', state: 'sample', questions: { q: { type: 'boolean', instructions: 'Ready?' } }, attachments: [videoAttachment({ name: 'clip', data: new Uint8Array() })] };
// @ts-expect-error the video request requires model, state and questions
const incomplete: VideoRequest = { attachments: [] };
declare const model: Model;
const bound: number | undefined = model.limits.choice_options.long_context?.max;
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
  expect(seen.every(entry => !entry.headers.has('SZ-Danger-Zone'))).toBe(true);
});
