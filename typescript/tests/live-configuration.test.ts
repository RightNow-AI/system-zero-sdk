import { afterEach, describe, expect, it, vi } from 'vitest';
import { SystemZeroClient, type RequestOptions } from '../src/client.js';
import { shouldRecoverWorkflow, shouldRetry } from '../src/retry.js';
import { createToolHandler, handleToolCall } from '../src/integrations/function-tools/index.js';
import { createMcpServer, CURRENT_PROTOCOL_VERSION } from '../src/integrations/mcp-server/server.js';

afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); });

const toolArgs = { state: { json: '{"ready":true}' }, questions: [
  { name: 'ready', type: 'boolean', instructions: 'Is this ready?' },
] };
const configurationError = 'apiKey must contain only visible ASCII characters.';
const loopbackHosts = [[127, 0, 0, 1].join('.'), '[' + ':' + ':1]', ['local', 'host'].join('')];
const userinfoURL = new URL('https://sdk.example.com/v1');
userinfoURL.username = 'user';
userinfoURL.password = 'pass';
const loopbackURLs = loopbackHosts.map(host => {
  const url = new URL('http://sdk.example.com/v1');
  url.hostname = host;
  url.port = '8000';
  return url.href;
});

function thrown(work: () => unknown): Error {
  try { work(); } catch (error) { expect(error).toBeInstanceOf(Error); return error as Error; }
  throw new Error('Expected configuration refusal.');
}

describe('client configuration and request boundaries', () => {
  it.each(['\n', '\r', '\0', '\t', ' ', '\u007f', '\u00e9'])('refuses a malformed key before transport: %j', character => {
    const key = 'SYNTHETIC' + character + 'VALUE';
    const fetch = vi.fn(async () => Response.json({ object: 'list', data: [] }));
    const error = thrown(() => new SystemZeroClient({ apiKey: key, fetch }));
    expect(error.message).toBe(configurationError);
    expect(error.cause).toBeUndefined();
    expect(String(error.stack)).not.toContain(key);
    expect(fetch).not.toHaveBeenCalled();
  });

  it('validates keys from the environment with the same fixed refusal', () => {
    vi.stubEnv('SYSTEM_ZERO_API_KEY', 'SYNTHETIC\nVALUE');
    expect(thrown(() => new SystemZeroClient()).message).toBe(configurationError);
  });

  it.each(['headers', 'clientRequestId', 'idempotencyKey'] as const)('does not echo malformed request header values: %s', async field => {
    const value = 'SYNTHETIC\nVALUE';
    const fetch = vi.fn(async () => Response.json({ object: 'list', data: [] }));
    const client = new SystemZeroClient({ apiKey: 'SYNTHETIC', fetch });
    const options: RequestOptions = field === 'headers' ? { headers: { 'x-example': value } } : { [field]: value };
    let failure: unknown;
    try { await client.models.list(options); } catch (error) { failure = error; }
    expect(failure).toBeInstanceOf(TypeError);
    expect((failure as Error).message).toBe('Request headers are invalid.');
    expect((failure as Error).cause).toBeUndefined();
    expect(String((failure as Error).stack)).not.toContain(value);
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each(['https://sdk.example.com/v1?', 'https://sdk.example.com/v1#',
    'https://sdk.example.com/v1?x=1', 'https://sdk.example.com/v1#part',
    userinfoURL.href, 'http://sdk.example.com/v1'])('rejects an invalid URL at client and tool-factory construction: %s', baseURL => {
    expect(() => new SystemZeroClient({ apiKey: 'SYNTHETIC', baseURL })).toThrow();
    expect(() => createToolHandler({ baseURL })).toThrow();
  });

  it.each(['https://sdk.example.com/v1', ...loopbackURLs])('accepts a secure or loopback base without dispatch: %s', baseURL => {
    const fetch = vi.fn(async () => Response.json({ object: 'list', data: [] }));
    expect(() => new SystemZeroClient({ apiKey: 'SYNTHETIC', baseURL, fetch })).not.toThrow();
    expect(() => createToolHandler({ baseURL })).not.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('does not count a call when lazy client configuration fails', async () => {
    vi.stubEnv('SYSTEM_ZERO_API_KEY', undefined);
    const handler = createToolHandler({ maxDecideCalls: 2 });
    for (let index = 0; index < 2; index++) {
      expect(await handler('system_zero_decide', toolArgs)).toMatchObject({ status: 'error' });
      expect(handler.budget.decideCalls).toBe(0);
    }
  });

  it('keeps the module-level tool helper at zero decide calls', async () => {
    vi.stubEnv('SYSTEM_ZERO_API_KEY', undefined);
    expect(await handleToolCall('system_zero_decide', toolArgs)).toEqual({
      status: 'error', request_id: null,
      error: { code: 'call_limit_reached', message: 'The local call limit has been reached.' },
    });
  });
});

describe('bounded retry and local adapter admission', () => {
  it('retains explicit refusal-before-work recovery and terminal ambiguous replies', () => {
    const headers = new Headers({ 'x-should-retry': 'true' });
    for (const [code, cause, expected] of [
      ['rate_limit_exceeded', 'key_rate', true], ['rate_limit_exceeded', 'preauth', true],
      ['concurrency_limit_exceeded', 'account_concurrency', true],
      ['rate_limit_exceeded', 'capacity', false], ['capacity_exhausted', 'capacity', false],
      ['invalid_api_key', 'key_rate', false],
    ] as const) {
      expect(shouldRecoverWorkflow(429, code, headers, true, { cause })).toBe(expected);
      expect(shouldRecoverWorkflow(429, code, headers, false, { cause })).toBe(false);
    }
    expect(shouldRetry(503, 'service_unavailable', headers, true)).toBe(true);
    expect(shouldRetry(429, 'rate_limit_exceeded', headers, true)).toBe(true);
    expect(shouldRetry(401, 'invalid_api_key', headers, true)).toBe(false);
  });

  it('reports the two-call MCP concurrency ceiling as a local refusal and releases capacity', async () => {
    let release: (() => void) | undefined;
    const pending = new Promise<void>(resolve => { release = resolve; });
    const handle = vi.fn(async () => { await pending; return { status: 'ok' as const, request_id: null }; });
    const server = createMcpServer({ handleToolCall: handle });
    const message = (id: number) => ({ jsonrpc: '2.0', id, method: 'tools/call', params: {
      name: 'system_zero_decide', arguments: toolArgs,
      _meta: { 'io.modelcontextprotocol/protocolVersion': CURRENT_PROTOCOL_VERSION,
        'io.modelcontextprotocol/clientCapabilities': {} },
    } });
    const first = server.handleMessage(message(1));
    const second = server.handleMessage(message(2));
    try {
      const third = await server.handleMessage(message(3));
      expect(third).toMatchObject({ result: { isError: true, structuredContent: {
        status: 'error', request_id: null,
        error: { code: 'call_limit_reached', message: 'The local concurrent call limit has been reached.' },
      } } });
      expect(handle).toHaveBeenCalledTimes(2);
    } finally { release?.(); await Promise.all([first, second]); }
    expect(await server.handleMessage(message(4))).toMatchObject({ result: { isError: false } });
    expect(handle).toHaveBeenCalledTimes(3);
  });
});
