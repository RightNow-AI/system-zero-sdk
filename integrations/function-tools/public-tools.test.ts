import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import { SystemZeroClient } from '../../typescript/src/client.js';
import {
  createToolHandler, OPENAI_TOOLS, TOOL_DEFINITIONS, validateToolArguments,
} from '../../typescript/src/integrations/function-tools/index.js';
import limits from '../../typescript/src/integrations/function-tools/argument-limits.js';
import { createMcpServer, LEGACY_PROTOCOL_VERSION } from '../../typescript/src/integrations/mcp-server/server.js';

const argumentsValue = {
  state: { json: '{"status":"ready"}' },
  questions: [{ name: 'ready', type: 'boolean', instructions: 'Is the status ready?' }],
};

describe('public function integrations', () => {
  it('normalizes provider arguments and rejects unknown fields before dispatch', () => {
    expect(validateToolArguments('system_zero_decide', argumentsValue)).toEqual({
      model: 'system-zero', state: { status: 'ready' },
      questions: { ready: { type: 'boolean', instructions: 'Is the status ready?' } },
    });
    expect(validateToolArguments('system_zero_list_models', '{}')).toEqual({});
    expect(() => validateToolArguments('system_zero_list_models', { extra: true })).toThrow();
    expect(() => validateToolArguments('unknown_tool', {})).toThrow();
    expect(OPENAI_TOOLS.map(tool => tool.function.name)).toEqual(TOOL_DEFINITIONS.map(tool => tool.name));
    expect(OPENAI_TOOLS.every(tool => tool.function.strict)).toBe(true);
  });

  it('shares packaged argument limits and enforces the embedded document depth', () => {
    const pythonLimits = JSON.parse(readFileSync(new URL('../../python/src/system_zero/argument-limits.json', import.meta.url), 'utf8'));
    expect(limits).toEqual({ maxBytes: 24 * 1024 * 1024, maxDepth: 256, maxValues: 2_000_000 });
    expect(pythonLimits).toEqual(limits);
    const document = '['.repeat(limits.maxDepth) + '"x"' + ']'.repeat(limits.maxDepth);
    expect(() => validateToolArguments('system_zero_decide', { state: { json: document }, questions: [] })).not.toThrow();
    expect(() => validateToolArguments('system_zero_decide', { state: { json: '[' + document + ']' }, questions: [] })).toThrow();
  });

  it('runs model discovery through an injected transport without spending a decide budget', async () => {
    const fetch = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      expect(init?.method).toBe('GET');
      expect(init?.body).toBeUndefined();
      return new Response('{"object":"list","data":[]}', { headers: {
        'content-type': 'application/json', 'x-request-id': 'req_public',
      } });
    });
    const client = new SystemZeroClient({ apiKey: 'public-tools', maxRetries: 0, fetch });
    const handler = createToolHandler({ client });
    expect(await handler('system_zero_list_models', {})).toEqual({
      object: 'list', data: [], status: 'ok', request_id: 'req_public',
    });
    expect(await handler('system_zero_list_models', { extra: true })).toMatchObject({ error: { code: 'invalid_request' } });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(handler.budget).toEqual({ decideCalls: 0 });
  });

  it('initializes MCP and lists tool schemas without invoking a handler', async () => {
    const handleToolCall = vi.fn(async () => ({ status: 'ok' as const, request_id: null }));
    const server = createMcpServer({ handleToolCall });
    expect(await server.handleMessage({
      jsonrpc: '2.0', id: 1, method: 'initialize', params: {
        protocolVersion: LEGACY_PROTOCOL_VERSION, capabilities: {},
        clientInfo: { name: 'public-sdk-test', version: '1' },
      },
    })).toMatchObject({ jsonrpc: '2.0', id: 1, result: { protocolVersion: LEGACY_PROTOCOL_VERSION } });
    await server.handleMessage({ jsonrpc: '2.0', method: 'notifications/initialized' });
    expect(await server.handleMessage({ jsonrpc: '2.0', id: 2, method: 'tools/list' }))
      .toMatchObject({ id: 2, result: { tools: TOOL_DEFINITIONS } });
    expect(handleToolCall).not.toHaveBeenCalled();
  });
});
