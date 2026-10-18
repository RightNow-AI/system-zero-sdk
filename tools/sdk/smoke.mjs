import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(process.argv[2] ?? fileURLToPath(new URL('../../typescript/', import.meta.url)));
const pkg = JSON.parse(await readFile(resolve(root, 'package.json'), 'utf8'));
const load = file => import(pathToFileURL(resolve(root, 'dist', file)).href);
const core = await load('index.js');
const tools = await load('integrations/function-tools/index.js');
const mcp = await load('integrations/mcp-server/server.js');

assert.equal(core.VERSION, pkg.version);
assert.deepEqual(tools.validateToolArguments('system_zero_list_models', {}), {});
const request = {
  model: 'system-zero', state: 'SYNTHETIC package smoke.',
  questions: { ready: { type: 'boolean', instructions: 'Is this a package smoke?' } },
};
const reply = { id: 'szd_' + 'a'.repeat(32), answers: {} };
let calls = 0;
const client = new core.SystemZeroClient({
  apiKey: 'public-smoke', maxRetries: 0,
  fetch: async (_url, init) => {
    calls += 1;
    assert.equal(init.method, 'POST');
    assert.deepEqual(JSON.parse(init.body), request);
    assert.equal(new Headers(init.headers).get('idempotency-key'), 'smoke-request');
    return new Response(JSON.stringify(reply), { headers: { 'content-type': 'application/json' } });
  },
});
assert.deepEqual(await client.decide(request, { idempotencyKey: 'smoke-request' }), reply);
assert.equal(calls, 1);
const server = mcp.createMcpServer();
const initialized = await server.handleMessage({
  jsonrpc: '2.0', id: 1, method: 'initialize', params: {
    protocolVersion: mcp.LEGACY_PROTOCOL_VERSION, capabilities: {},
    clientInfo: { name: 'sdk-smoke', version: '1' },
  },
});
assert.equal(initialized.result.serverInfo.version, pkg.version);
console.log('TypeScript SDK public entrypoints and injected transport passed.');
