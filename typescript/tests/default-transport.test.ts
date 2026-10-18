import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { existsSync, mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { APIConnectionError, APIError, SystemZeroClient } from '../src/index.js';
import { runBatch } from '../src/batch.js';

const host = [127, 0, 0, 1].join('.');
const requestBody = { model: 'system-zero', state: 'SYNTHETIC \u00e9',
  questions: { ready: { type: 'boolean' as const, instructions: 'Ready?' } } };
const models = { object: 'list', data: [] };
const decision = { id: 'szd_synthetic', model: 'system-zero', served_by: 'synthetic', created: 0,
  context: { route: 'direct', tokens: 1 }, answers: {},
  usage: { input_tokens: 1, cached_input_tokens: 0, media_tokens: 0, long_input_tokens: 0, media_items: 0, thought_tokens: 0 } };
type ObservedRequest = { method: string; headers: IncomingMessage['headers']; bytes: Buffer };

async function withServer(
  handle: (request: IncomingMessage, response: ServerResponse, count: number) => void,
  run: (baseURL: string, seen: ObservedRequest[]) => Promise<void>,
): Promise<void> {
  const seen: ObservedRequest[] = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk: Buffer) => chunks.push(chunk));
    request.on('end', () => {
      seen.push({ method: request.method ?? '', headers: request.headers, bytes: Buffer.concat(chunks) });
      handle(request, response, seen.length);
    });
  });
  await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, host, resolve); });
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Expected a TCP listening address.');
  const url = new URL('http://sdk.example.com/v1');
  url.hostname = host;
  url.port = String(address.port);
  try { await run(url.href, seen); }
  finally {
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
}

function client(baseURL: string, maxRetries = 1, timeout = 300): SystemZeroClient {
  return new SystemZeroClient({ apiKey: 'SYNTHETIC', baseURL, maxRetries, timeout,
    retryBudget: 5000, random: () => 0, sleep: async () => {} });
}

describe('default HTTP transport', () => {
  it('sends decide and models with explicit JSON body length', async () => {
    await withServer((request, response) => {
      response.setHeader('content-type', 'application/json');
      response.end(JSON.stringify(request.method === 'POST' ? decision : models));
    }, async (baseURL, seen) => {
      const sdk = client(baseURL);
      expect(await sdk.decide(requestBody, { idempotencyKey: 'SYNTHETIC' })).toEqual(decision);
      expect(await sdk.models.list()).toEqual(models);
      expect(seen.map(row => row.method)).toEqual(['POST', 'GET']);
      expect(seen[0]!.headers['content-length']).toBe(String(seen[0]!.bytes.length));
      expect(seen[0]!.headers['transfer-encoding']).toBeUndefined();
      expect(seen[0]!.bytes.toString('utf8')).toBe(JSON.stringify(requestBody));
      expect(seen[1]!.bytes.length).toBe(0);
    });
  });

  it.each(['GET', 'POST'] as const)('retries %s before any response byte with a stable key', async method => {
    await withServer((request, response, count) => {
      if (count === 1) { request.socket.destroy(); return; }
      response.end(JSON.stringify(method === 'GET' ? models : decision));
    }, async (baseURL, seen) => {
      const sdk = client(baseURL);
      await (method === 'GET' ? sdk.models.list() : sdk.decide(requestBody, { idempotencyKey: 'SYNTHETIC' }));
      expect(seen).toHaveLength(2);
      expect(seen.map(row => row.headers['idempotency-key'])).toEqual(method === 'GET' ? [undefined, undefined] : ['SYNTHETIC', 'SYNTHETIC']);
      expect(seen[0]!.bytes).toEqual(seen[1]!.bytes);
    });
  });

  it.each(['GET', 'POST'] as const)('does not replay %s after a partial response header', async method => {
    await withServer((request) => { request.socket.end('H'); }, async (baseURL, seen) => {
      const sdk = client(baseURL);
      const failure: unknown = await (method === 'GET' ? sdk.models.list() : sdk.decide(requestBody, { idempotencyKey: 'SYNTHETIC' })).catch(error => error);
      expect(failure).toBeInstanceOf(APIConnectionError);
      expect(failure).toMatchObject({ retryable: false, request_sent: true });
      expect(seen).toHaveLength(1);
    });
  });

  it('bounds an incomplete body by the attempt deadline without replay', async () => {
    await withServer((_request, response) => { response.writeHead(200); response.write('{'); }, async (baseURL, seen) => {
      const started = performance.now();
      const failure: unknown = await client(baseURL, 1, 100).models.list().catch(error => error);
      expect(failure).toBeInstanceOf(APIConnectionError);
      expect(failure).toMatchObject({ type: 'timeout_error', retryable: false, request_sent: true, status: 200 });
      expect(performance.now() - started).toBeLessThan(2000);
      expect(seen).toHaveLength(1);
    });
  });

  it.each([600, 999])('returns an API error for status %s without escaping the response callback', async status => {
    await withServer((_request, response) => { response.writeHead(status); response.end('{}'); }, async (baseURL, seen) => {
      const failure: unknown = await client(baseURL, 1, 150).models.list().catch(error => error);
      expect(failure).toBeInstanceOf(APIError);
      expect(failure).not.toBeInstanceOf(APIConnectionError);
      expect(failure).toMatchObject({ status, retryable: false });
      expect(seen).toHaveLength(1);
    });
  });
});

describe('client target revalidation', () => {
  it.each(['GET', 'POST', 'batch'] as const)('refuses a replaced unsafe target before %s dispatch or receipt creation', async method => {
    const fetch = vi.fn(async () => Response.json(models));
    const sdk = new SystemZeroClient({ apiKey: 'SYNTHETIC', fetch, maxRetries: 0 });
    Object.defineProperty(sdk, 'options', { value: { ...sdk.options, baseURL: 'http://sdk.example.com/v1' } });
    const scratch = join(process.cwd(), 'tmp');
    mkdirSync(scratch, { recursive: true });
    const temporary = mkdtempSync(join(scratch, 'target-test-'));
    const resultsPath = join(temporary, 'receipts.jsonl');
    let failure: unknown;
    try {
      try {
        if (method === 'GET') await sdk.models.list();
        else if (method === 'POST') await sdk.decide(requestBody);
        else await runBatch(sdk, [], { runId: 'synthetic', resultsPath, maxDecideCalls: 1, deadlineMs: 1000 });
      } catch (error) { failure = error; }
      expect(failure).toBeInstanceOf(Error);
      expect(fetch).not.toHaveBeenCalled();
      expect(existsSync(resultsPath)).toBe(false);
      expect(existsSync(resultsPath + '.admissions.jsonl')).toBe(false);
    } finally { rmSync(temporary, { recursive: true, force: true }); }
  });
});
