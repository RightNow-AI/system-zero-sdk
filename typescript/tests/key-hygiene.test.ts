import { randomBytes } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { inspect } from 'node:util';
import { spawnSync } from 'node:child_process';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SystemZeroClient } from '../src/client.js';
import { SystemZeroClient as AcceptedClient } from '../src/client.js';
import { SystemZeroStreamClient } from '../src/stream.js';
import { TEST_STREAM_LIMITS } from './stream-limit-fixture';

const loopback = [127, 0, 0, 1].join('.');
const key = 'sk-sentinel-' + randomBytes(16).toString('hex');
const statuses = [401, 403, 404, 409, 413, 422, 429, ...Array.from({ length: 100 }, (_, n) => 500 + n)];
const logs: string[] = [];
beforeEach(() => {
  logs.length = 0;
  for (const name of ['log', 'debug', 'info', 'warn', 'error', 'trace'] as const)
    vi.spyOn(console, name).mockImplementation((...args) => { logs.push(inspect(args, { depth: 12 })); });
});
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

function surfaces(value: unknown): string {
  const seen = new Set<unknown>();
  const scan = (v: unknown): unknown => {
    if (v === null || typeof v !== 'object') return typeof v === 'function' ? '[function]' : v;
    if (seen.has(v)) return '[cycle]';
    seen.add(v);
    if (ArrayBuffer.isView(v)) return new TextDecoder().decode(v as Uint8Array);
    if (v instanceof Headers) return Object.fromEntries(v);
    return Object.fromEntries(Reflect.ownKeys(v).map(k => [String(k), scan(Reflect.get(v, k))]));
  };
  return [String(value), inspect(value, { depth: 20, showHidden: true }), JSON.stringify(value), JSON.stringify(scan(value))].join('\n');
}
function clean(error: unknown, client: unknown) {
  expect(error).toBeInstanceOf(Error);
  const text = surfaces(error) + surfaces(client) + logs.join('\n');
  expect(text.includes(key), 'sentinel must be absent from all diagnostic surfaces').toBe(false);
  const e = error as { headers?: Headers; details?: unknown };
  expect(e.headers?.has('authorization') ?? false).toBe(false);
  expect(e.headers?.has('proxy-authorization') ?? false).toBe(false);
  expect(JSON.stringify(e.details ?? {}).toLowerCase().includes('authorization')).toBe(false);
}
const echo = () => ({ request_id: key, error: { type: 'api_error', code: key, param: key,
  message: `refused ${key}`, retryable: false,
  details: { nested: [{ Authorization: `Bearer ${key}`, note: key }], [key]: key } } });
const headers = () => ({ Authorization: `Bearer ${key}`, 'Proxy-Authorization': `Basic ${key}`, 'x-request-id': key, 'x-debug': key });

it.each(statuses)('HTTP %i redacts envelope and echoed headers', async status => {
  const client = new SystemZeroClient({ apiKey: key, maxRetries: 0, fetch: async (_url, init) => {
    expect(new Headers(init?.headers).get('authorization')).toBe(`Bearer ${key}`);
    return new Response(JSON.stringify(echo()), { status, headers: headers() });
  } });
  clean(await client.models.list().catch(e => e), client);
});
it.each(['network', 'dns', 'timeout', 'wrapped', 'truncated', 'malformed', 'deadline'])('%s failure has no credential', async kind => {
  const fetch = vi.fn(async () => {
    const native = Object.assign(new Error(`transport ${key}`), { code: kind === 'dns' ? 'ENOTFOUND' : 'ECONNRESET',
      request: { headers: headers() }, details: { Authorization: key } });
    if (kind === 'deadline') return new Promise<Response>(() => {});
    if (kind === 'timeout') native.name = 'TimeoutError';
    if (kind === 'wrapped') throw new Error(`outer ${key}`, { cause: native });
    if (kind === 'malformed') return new Response(`{"${key}":`, { headers: headers() });
    if (kind === 'truncated') return new Response(new ReadableStream({ start(c) { c.error(native); } }), { headers: headers() });
    throw native;
  });
  const client = new SystemZeroClient({ apiKey: key, fetch, maxRetries: 0, timeout: 10 });
  clean(await client.models.list().catch(e => e), client);
});
it('retries preserve count and redact final diagnostic metadata', async () => {
  const counts: string[] = [], waits: number[] = [];
  const client = new SystemZeroClient({ apiKey: key, maxRetries: 2, random: () => 0, sleep: async n => { waits.push(n); },
    fetch: async (_url, init) => {
      counts.push(new Headers(init?.headers).get('x-sdk-retry-count')!);
      return new Response(JSON.stringify({ ...echo(), error: { ...echo().error, code: 'service_unavailable', retryable: true } }), { status: 503, headers: { ...headers(), 'retry-after-ms': '1' } });
    } });
  clean(await client.models.list().catch(e => e), client);
  expect(counts).toEqual(['0', '1', '2']); expect(waits).toHaveLength(2);
});
it('client and options printing do not reveal apiKey', () => {
  const client = new SystemZeroClient({ apiKey: key });
  expect((surfaces(client) + surfaces(client.options)).includes(key)).toBe(false);
});
it('native serialization hooks cannot recover the key from a cause', async () => {
  class EchoError extends Error {
    toJSON() { return { Authorization: key }; }
    toString() { return key; }
    [inspect.custom]() { return key; }
  }
  const client = new SystemZeroClient({ apiKey: key, maxRetries: 0, fetch: async () => { throw new EchoError('synthetic'); } });
  const error = await client.models.list().catch(e => e);
  clean(error, client);
  expect(String(error.cause).includes(key)).toBe(false);
});
it.each(['buffer', 'map', 'url'])('opaque native %s diagnostics do not retain credentials', async kind => {
  const data = kind === 'buffer' ? Buffer.from(key) : kind === 'map' ? new Map([['apiKey', key]]) : new URL('https://synthetic.example.com/' + key);
  const client = new SystemZeroClient({ apiKey: key, maxRetries: 0, fetch: async () => { throw Object.assign(new Error('synthetic'), { data }); } });
  clean(await client.models.list().catch(e => e), client);
});
it('DOM timeout causes remain printable without their credential', async () => {
  const client = new SystemZeroClient({ apiKey: key, maxRetries: 0, fetch: async () => { throw new DOMException(key, 'TimeoutError'); } });
  const error = await client.models.list().catch(e => e);
  clean(error, client);
  expect(error.type).toBe('timeout_error');
});
it('stream opening disconnect strips native causes and request headers', async () => {
  class Socket extends EventEmitter {
    readyState = 0; protocol = 'system-zero.stream.v1';
    constructor(_url: string, _protocol: string, options: { headers: Record<string, string> }) {
      super(); expect(options.headers.Authorization).toBe(`Bearer ${key}`);
      queueMicrotask(() => this.emit('error', Object.assign(new Error(key), { code: 'ECONNRESET', request: { headers: options.headers } })));
    }
    close() {} terminate() {} send() {}
  }
  const client = new SystemZeroStreamClient({ apiKey: key, WebSocket: Socket as never, maxReconnectAttempts: 1 });
  try { clean(await client.connect().catch(e => e), client); } finally { await client.close(); }
});

it.each(statuses)('stream upgrade %i redacts diagnostics', async status => {
  class Socket extends EventEmitter {
    readyState = 0; protocol = 'system-zero.stream.v1';
    constructor() {
      super();
      queueMicrotask(() => {
        const response = Object.assign(new EventEmitter(), { statusCode: status, headers: headers(),
          resume() { this.emit('data', JSON.stringify(echo())); this.emit('end'); } });
        this.emit('unexpected-response', {}, response);
      });
    }
    close() {} terminate() {} send() {}
  }
  const client = new SystemZeroStreamClient({ apiKey: key, WebSocket: Socket as never, maxReconnectAttempts: 1 });
  try { clean(await client.connect().catch(e => e), client); } finally { await client.close(); }
});

it.each(['disconnect', 'error-frame'])('connected stream %s redacts errors and events', async kind => {
  let socket: Socket;
  class Socket extends EventEmitter {
    readyState = 1; protocol = 'system-zero.stream.v1'; seq = 0;
    constructor() {
      super(); socket = this;
      queueMicrotask(() => this.server({ type: 'hello', protocol: this.protocol, limits: { ...TEST_STREAM_LIMITS, heartbeat_interval_ms: 1000 } }));
    }
    server(frame: Record<string, unknown>) { this.emit('message', JSON.stringify({ stream_id: `szws_${'a'.repeat(32)}`, seq: ++this.seq, ...frame }), false); }
    send(text: string) {
      const frame = JSON.parse(text);
      if (frame.type === 'start') this.server({ type: 'ack', event_id: frame.event_id, state_version: 1, scheduled: true });
    }
    close() {} terminate() {}
  }
  const client = new SystemZeroStreamClient({ apiKey: key, WebSocket: Socket as never, maxReconnectAttempts: 1, reconnectPolicy: 'never' });
  const events: unknown[] = [];
  client.on('error', event => { events.push(event); });
  try {
    await client.connect();
    const ack = await client.start({ model: 'system-zero', state: 'synthetic', questions: { ok: { type: 'boolean', instructions: 'Ready?' } } });
    const pending = client.waitForDecision(ack).catch(e => e);
    if (kind === 'disconnect') socket!.emit('error', Object.assign(new Error(key), { code: 'ECONNRESET', request: { headers: headers() } }));
    else socket!.server({ type: 'error', phase: 'decide', state_version: 1, event_id: null, fatal: true, http_status: 503,
      envelope: echo(), retry: { should_retry: false, retry_after: key, retry_after_ms: null } });
    clean(await pending, client);
    if (kind === 'error-frame') {
      expect(events).toHaveLength(1);
      expect(surfaces(events).includes(key)).toBe(false);
    }
  } finally { await client.close(); }
});

it('native HTTP debug output cannot print credentials', () => {
  const script = `
    import { createServer } from 'node:http';
    import { randomBytes } from 'node:crypto';
    import { SystemZeroClient } from '${new URL('../src/client.js', import.meta.url).href}';
    const sentinel = 'sk-sentinel-' + randomBytes(16).toString('hex');
    const server = createServer((req, res) => { res.writeHead(401, {'content-type':'application/json'}); res.end('{"error":{"message":"synthetic"}}'); });
    await new Promise(r => server.listen(0, '${loopback}', r));
    try {
      const url = new URL('http://sdk.example.com');
      url.hostname = '${loopback}'; url.port = String(server.address().port);
      const client = new SystemZeroClient({ baseURL: url.href, maxRetries: 0, apiKey: sentinel });
      await client.models.list().catch(() => {});
    } finally { server.closeAllConnections(); await new Promise(r => server.close(r)); }
  `;
  const result = spawnSync(process.execPath, ['--import', new URL('./key-hygiene-loader.mjs', import.meta.url).href, '--input-type=module', '-e', script], {
    cwd: process.cwd(), env: { ...process.env, NODE_DEBUG: 'http,https,net,tls' }, encoding: 'utf8', timeout: 15000,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  expect(result.status, 'debug subprocess completed').toBe(0);
  expect(/sk-sentinel-[a-f0-9]{32}/u.test(result.stdout + result.stderr), 'native debug log must not contain a sentinel').toBe(false);
});


// Review regressions: these same cases run in the exported package.
it.each(['primitive', 'name', 'message'] as const)('review A-03 B-05 inherited %s diagnostic is safe', async kind => {
  class Reflected extends Error {}
  const field = kind === 'primitive' ? Symbol.toPrimitive : kind;
  Object.defineProperty(Reflected.prototype, field, kind === 'primitive'
    ? { value() { return key; } } : { get() { return key; } });
  const native = new Reflected('safe');
  if (kind === 'message') delete (native as { message?: string }).message;
  const client = new SystemZeroClient({ apiKey: key, maxRetries: 0,
    fetch: async () => { throw new Error('outer', { cause: native }); } });
  const error = await client.models.list().catch(e => e);
  expect(String(error.cause.cause)).not.toContain(key);
  expect(`${error.cause.cause}`).not.toContain(key);
  clean(error, client);
});

it('review A-03 B-05 stream inherited conversion and display hooks are safe', async () => {
  class Reflected extends Error { [Symbol.toPrimitive]() { return key; } }
  Object.defineProperty(Reflected.prototype, 'name', { get() { return key; } });
  class Socket extends EventEmitter {
    readyState = 0; protocol = 'system-zero.stream.v1';
    constructor() { super(); queueMicrotask(() => this.emit('error', new Reflected('safe'))); }
    close() {} terminate() {} send() {}
  }
  const client = new SystemZeroStreamClient({ apiKey: key, WebSocket: Socket as never, maxReconnectAttempts: 1 });
  try {
    const error = await client.connect().catch(e => e);
    expect(String(error.cause)).not.toContain(key);
    clean(error, client);
  } finally { await client.close(); }
});

it('review A-06 B-03 credential-bearing header names retain typed authentication errors', async () => {
  const client = new SystemZeroClient({ apiKey: key, maxRetries: 0, fetch: async () => Response.json({
    error: { type: 'authentication_error', code: 'auth_error', message: 'refused' },
  }, { status: 401, headers: { ['x-' + key]: 'safe', 'x-kept': 'retained' } }) });
  const error = await client.models.list().catch(e => e);
  expect(error.name).toBe('AuthenticationError');
  expect(error.status).toBe(401); expect(error.code).toBe('auth_error');
  expect(error.headers.get('x-kept')).toBe('retained');
  clean(error, client);
});

it('review A-05 malformed flow replies cannot persist echoed diagnostic metadata', async () => {
  const { runFlow } = await import('../src/flows.js');
  const client = new SystemZeroClient({ apiKey: key, maxRetries: 0, timeout: 1000,
    fetch: async () => Response.json({}, { headers: { 'x-request-id': key, 'x-client-request-id': key } }) });
  const flow = { version: 1 as const, request: { model: 'system-zero', state: 'safe' },
    steps: [{ id: 'first', questions: { ready: { type: 'boolean' as const, instructions: 'Ready?' } }, next: { rules: [], default: null } }] };
  const error = await runFlow(client, flow, { run_id: 'review-flow', deadline_ms: 10_000, now: () => 0 }).catch(e => e);
  expect(error.code).toBe('service_unavailable');
  expect(error.request_id).not.toContain(key);
  expect(JSON.stringify(error.trace)).not.toContain(key);
  clean(error, client);
});

it.each(['stream', 'websocket'])('review A-02 refuses native %s echoed-byte diagnostics', channel => {
  const script = `
    import { assertSafeNativeDebug } from '${new URL('../src/redaction.js', import.meta.url).href}';
    import { Readable } from 'node:stream';
    import { channel } from 'node:diagnostics_channel';
    import { randomBytes } from 'node:crypto';
    const marker = 'echo-' + randomBytes(12).toString('hex');
    console.log(JSON.stringify({ marker }));
    try { assertSafeNativeDebug(); }
    catch { console.log('REFUSED'); process.exit(0); }
    if (${JSON.stringify(channel)} === 'stream') new Readable({ read() {} }).push(Buffer.from(marker));
    else {
      new WebSocket('ws://' + [127,0,0,1].join('.') + ':1').addEventListener('error', () => {});
      channel('undici:websocket:close').publish({ websocket: {}, code: 1011, reason: marker });
    }
  `;
  const result = spawnSync(process.execPath, ['--import', new URL('./key-hygiene-loader.mjs', import.meta.url).href,
    '--input-type=module', '-e', script], { encoding: 'utf8', timeout: 10000, env: { ...process.env, NODE_DEBUG: channel } });
  expect(result.status).toBe(0);
  const marker = JSON.parse(result.stdout.split('\n')[0]!).marker as string;
  expect(result.stderr).not.toContain(marker);
  expect(result.stderr.toLowerCase().replace(/[^a-f0-9]/g, '')).not.toContain(Buffer.from(marker).toString('hex'));
  expect(result.stdout).toContain('REFUSED');
});


it.each(['stream', 'websocket'])('review round3 HTTP refusal names the %s remediation', channel => {
  const script = `
    import { nodeFetch } from '${new URL('../src/transport.js', import.meta.url).href}';
    import { APIError } from '${new URL('../src/errors.js', import.meta.url).href}';
    try { await nodeFetch('http://sdk.example.com'); process.exitCode = 2; }
    catch (error) { console.log(JSON.stringify({ typed: error instanceof APIError, message: error.message })); }
  `;
  const result = spawnSync(process.execPath, ['--import', new URL('./key-hygiene-loader.mjs', import.meta.url).href,
    '--input-type=module', '-e', script], { encoding: 'utf8', timeout: 10000, env: { ...process.env, NODE_DEBUG: channel } });
  expect(result.status).toBe(0);
  const diagnostic = JSON.parse(result.stdout.trim());
  expect(diagnostic.typed).toBe(true);
  expect(diagnostic.message).toContain(channel);
});
