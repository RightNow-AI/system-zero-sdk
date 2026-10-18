import { Buffer } from 'node:buffer';
import type { Readable, Writable } from 'node:stream';
import { createToolHandler, TOOL_DEFINITIONS } from '../function-tools/index.js';

export const CURRENT_PROTOCOL_VERSION = '2026-07-28';
export const LEGACY_PROTOCOL_VERSION = '2025-11-25';
export const SUPPORTED_PROTOCOL_VERSIONS = [CURRENT_PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION] as const;
// The HTTP caller cap is 3,665,920 bytes. JSON tool wrapping can escape that
// content again; this transport bound leaves room for the complete caller body.
export const MAX_FRAME_BYTES = 24 * 1024 * 1024;
const SERVER_INFO = { name: 'system-zero', version: '0.1.0' } as const;
const CAPABILITIES = { tools: { listChanged: false } } as const;
const CACHE = { ttlMs: 3_600_000, cacheScope: 'public' } as const;
const VERSION_FIELD = 'io.modelcontextprotocol/protocolVersion';
const CAPABILITIES_FIELD = 'io.modelcontextprotocol/clientCapabilities';
const CLIENT_FIELD = 'io.modelcontextprotocol/clientInfo';
type RequestId = string | number;
type ToolResult = Awaited<ReturnType<ReturnType<typeof createToolHandler>>>;
type ToolHandler = (name: string, args: unknown) => Promise<ToolResult>;

export interface RpcReply {
  jsonrpc: '2.0';
  id: RequestId | null;
  result?: Record<string, unknown>;
  error?: { code: number; message: string; data?: unknown };
}

export interface McpServerOptions {
  handleToolCall?: ToolHandler;
}

export interface McpServer {
  handleMessage(message: unknown): Promise<RpcReply | null>;
  handleLine(line: string): Promise<RpcReply | null>;
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requestId(value: unknown): value is RequestId {
  return typeof value === 'string' || typeof value === 'number' && Number.isInteger(value);
}

function clientInfo(value: unknown): boolean {
  return object(value) && typeof value.name === 'string' && typeof value.version === 'string';
}

function error(id: RequestId | null, code: number, message: string, data?: unknown): RpcReply {
  return { jsonrpc: '2.0', id, error: { code, message, ...(data === undefined ? {} : { data }) } };
}

function result(id: RequestId, data: Record<string, unknown>, modern: boolean): RpcReply {
  return {
    jsonrpc: '2.0', id,
    result: modern ? { ...data, resultType: 'complete', _meta: { 'io.modelcontextprotocol/serverInfo': SERVER_INFO } } : data,
  };
}

function toolResult(id: RequestId, value: ToolResult, modern: boolean): RpcReply {
  return result(id, {
    content: [{ type: 'text', text: JSON.stringify(value) }],
    structuredContent: value,
    isError: value.status === 'error',
  }, modern);
}

/**
 * See https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
 * Modern requests carry their own version and capabilities. The legacy
 * handshake remains available for clients using the previous published era.
 */
export function createMcpServer(options: McpServerOptions = {}): McpServer {
  const handleToolCall = options.handleToolCall ?? createToolHandler({});
  let initialized = false;
  let initializationSent = false;
  const active = new Map<RequestId, { cancelled: boolean }>();

  async function handleMessage(message: unknown): Promise<RpcReply | null> {
    if (!object(message) || message.jsonrpc !== '2.0' || typeof message.method !== 'string') {
      return error(object(message) && requestId(message.id) ? message.id : null, -32600, 'Invalid Request');
    }
    const method = message.method;
    if (!Object.hasOwn(message, 'id')) {
      if (method === 'notifications/initialized' && initializationSent) initialized = true;
      if (method === 'notifications/cancelled' && object(message.params) && requestId(message.params.requestId)) {
        const operation = active.get(message.params.requestId);
        if (operation) operation.cancelled = true;
      }
      // No notification can invoke a counted operation or produce a response.
      return null;
    }
    if (!requestId(message.id)) return error(null, -32600, 'Invalid Request');
    const id = message.id;
    if (Object.hasOwn(message, 'params') && !object(message.params)) return error(id, -32602, 'Invalid params');
    const params = object(message.params) ? message.params : {};
    if (Object.hasOwn(params, '_meta') && !object(params._meta)) return error(id, -32602, 'Invalid params');

    if (method === 'initialize') {
      if (initializationSent) return error(id, -32600, 'Already initialized');
      if (typeof params.protocolVersion !== 'string' || !object(params.capabilities) || !clientInfo(params.clientInfo)) return error(id, -32602, 'Invalid params');
      initializationSent = true;
      return result(id, { protocolVersion: LEGACY_PROTOCOL_VERSION, capabilities: CAPABILITIES, serverInfo: SERVER_INFO }, false);
    }

    const metadata = object(params._meta) ? params._meta : {};
    const version = metadata[VERSION_FIELD];
    let modern = false;
    if (Object.hasOwn(metadata, VERSION_FIELD)) {
      if (typeof version !== 'string') return error(id, -32602, 'Invalid params');
      if (!SUPPORTED_PROTOCOL_VERSIONS.some(supported => supported === version)) {
        return error(id, -32022, 'Unsupported protocol version', { supported: [...SUPPORTED_PROTOCOL_VERSIONS], requested: version });
      }
      modern = version === CURRENT_PROTOCOL_VERSION;
      if (modern && (!object(metadata[CAPABILITIES_FIELD]) || Object.hasOwn(metadata, CLIENT_FIELD) && !clientInfo(metadata[CLIENT_FIELD]))) return error(id, -32602, 'Invalid params');
    }

    if (method === 'ping' && !modern) return result(id, {}, false);
    if (!modern && !initialized) return error(id, -32602, 'Initialize the legacy protocol or supply current request metadata');

    if (method === 'server/discover' && modern) {
      return result(id, { supportedVersions: [...SUPPORTED_PROTOCOL_VERSIONS], capabilities: CAPABILITIES, ...CACHE }, true);
    }
    if (method === 'tools/list') {
      if (params.cursor !== undefined) return error(id, -32602, 'Invalid cursor');
      return result(id, { tools: TOOL_DEFINITIONS, ...(modern ? CACHE : {}) }, modern);
    }
    if (method !== 'tools/call') return error(id, -32601, 'Method not found');
    if (typeof params.name !== 'string' || !TOOL_DEFINITIONS.some(tool => tool.name === params.name)) return error(id, -32602, 'Unknown tool');
    if (Object.hasOwn(params, 'arguments') && !object(params.arguments)) return error(id, -32602, 'Invalid params');
    if (params.requestState !== undefined || params.inputResponses !== undefined || params.task !== undefined) return error(id, -32602, 'Unsupported tool request parameters');
    if (active.has(id)) return error(id, -32600, 'Duplicate in-flight request id');
    if (active.size >= 2) {
      return toolResult(id, { status: 'error', request_id: null, error: { code: 'call_limit_reached', message: 'The local concurrent call limit has been reached.' } }, modern);
    }
    const operation = { cancelled: false };
    active.set(id, operation);
    try {
      // The common handler owns validation, ordinary decide, typed refusals,
      // idempotency and the host's local call limit in both provider integrations.
      const value = await handleToolCall(params.name, params.arguments ?? {});
      return operation.cancelled ? null : toolResult(id, value, modern);
    } catch {
      return operation.cancelled ? null : error(id, -32603, 'Internal error');
    } finally {
      active.delete(id);
    }
  }

  return {
    handleMessage,
    async handleLine(line) {
      if (Buffer.byteLength(line, 'utf8') > MAX_FRAME_BYTES) return error(null, -32600, 'Message exceeds transport limit');
      let message: unknown;
      try { message = JSON.parse(line) as unknown; }
      catch { return error(null, -32700, 'Parse error'); }
      return handleMessage(message);
    },
  };
}

export interface StdioOptions {
  server?: McpServer;
  input?: Readable;
  output?: Writable;
}

/** Newline JSON-RPC framing, with bounded input and backpressure on stdout. */
export async function serveStdio(options: StdioOptions = {}): Promise<void> {
  const server = options.server ?? createMcpServer();
  const input = options.input ?? process.stdin;
  const output = options.output ?? process.stdout;
  const pending = new Set<Promise<void>>();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let writes = Promise.resolve();
  let failure: unknown;
  let fragments: Buffer[] = [];
  let frameBytes = 0;
  let oversized = false;

  function emit(response: RpcReply | null): Promise<void> {
    if (response === null) return Promise.resolve();
    const bytes = Buffer.from(JSON.stringify(response) + '\n', 'utf8');
    writes = writes.then(() => new Promise<void>((resolve, reject) => {
      output.write(bytes, (problem?: Error | null) => problem ? reject(problem) : resolve());
    }));
    return writes;
  }

  function dispatch(): void {
    let response: Promise<RpcReply | null>;
    if (oversized) response = Promise.resolve(error(null, -32600, 'Message exceeds transport limit'));
    else {
      try { response = server.handleLine(decoder.decode(Buffer.concat(fragments, frameBytes))); }
      catch { response = Promise.resolve(error(null, -32700, 'Parse error')); }
    }
    fragments = [];
    frameBytes = 0;
    oversized = false;
    const work = response.then(emit).catch(problem => { failure ??= problem; });
    pending.add(work);
    void work.then(() => pending.delete(work));
  }

  function append(fragment: Buffer): void {
    if (oversized) return;
    frameBytes += fragment.byteLength;
    if (frameBytes > MAX_FRAME_BYTES) {
      oversized = true;
      fragments = [];
      frameBytes = 0;
    } else if (fragment.byteLength !== 0) fragments.push(fragment);
  }

  for await (const chunk of input) {
    if (failure !== undefined) throw failure;
    const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk), 'utf8');
    let start = 0;
    for (;;) {
      const end = data.indexOf(10, start);
      if (end < 0) { append(data.subarray(start)); break; }
      append(data.subarray(start, end));
      dispatch();
      start = end + 1;
      if (pending.size >= 32) await Promise.race(pending);
    }
  }
  if (frameBytes !== 0 || oversized) dispatch();
  await Promise.all(pending);
  if (failure !== undefined) throw failure;
}
