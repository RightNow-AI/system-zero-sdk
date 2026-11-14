import { expect, it, vi } from 'vitest';
import { RightNowClient, SystemZeroClient, RightNowStreamClient } from '../src/index.js';
import { RightNowClient as InternalClient, type Fetch } from '../src/client.js';
import { wireFor } from '../src/wire.js';

const request = { model: 'system-zero', state: 'ready', questions: { q: { type: 'boolean' as const, instructions: 'Ready?' } } };

it.each([
  ['system-zero', '/system-zero', 'system-zero', 'SZ-Danger-Zone', 'system_zero'],
  ['rightnow-decision', '/decision-model', 'rightnow-decision', 'RightNow-Danger-Zone', 'model'],
] as const)('selects the package HTTP wire for %s', async (name, path, model, header, codePrefix) => {
  const fetch = vi.fn<Fetch>(async (url, init) => {
    expect(url).toBe(`https://api.rightnowai.co/v1${path}/decide`);
    expect(JSON.parse(init!.body as string).model).toBe(model);
    const headers = new Headers(init!.headers);
    expect(headers.get(header)).toBe('accept');
    expect(headers.has(name === 'system-zero' ? 'RightNow-Danger-Zone' : 'SZ-Danger-Zone')).toBe(false);
    expect(headers.get('user-agent')).toMatch(new RegExp(`^${name === 'system-zero' ? 'system-zero' : 'rightnow'}-typescript/0.5.1 node/`));
    return Response.json({});
  });
  const client = new InternalClient({ apiKey: 'test-key', fetch, maxRetries: 0 }, name);
  await client.decide({ ...request, model }, { dangerZone: 'accept' });
  expect(wireFor(client).model).toBe(model);
  expect(wireFor(client).codes).toEqual({ timeout: `${codePrefix}_timeout`, unavailable: `${codePrefix}_unavailable`, servingUnattested: `${codePrefix}_serving_unattested` });
  const timeout = new InternalClient({ apiKey: 'test-key', timeout: 1, maxRetries: 0, fetch: () => new Promise(() => {}) }, name);
  await expect(timeout.decide({ ...request, model })).rejects.toMatchObject({ code: `${codePrefix}_timeout` });
});

it.each([RightNowClient, SystemZeroClient])('selects the new package wire for every HTTP alias', Client => {
  expect(wireFor(new Client({ apiKey: 'test-key' })).name).toBe('rightnow-decision');
});

it.each(['SZ-Danger-Zone', 'RightNow-Danger-Zone', 'sZ-dAnGeR-zOnE', 'rIgHtNoW-dAnGeR-zOnE'])('rejects caller header %s on both wires', header => {
  for (const name of ['system-zero', 'rightnow-decision'] as const) {
    const fetch = vi.fn();
    const client = new InternalClient({ apiKey: 'test-key', fetch }, name);
    expect(() => client.decide(request, { headers: { [header]: 'accept' } })).toThrow('do not supply its header');
    expect(fetch).not.toHaveBeenCalled();
  }
});

it.each(['system-zero', 'rightnow-decision'] as const)('selects package stream and webhook routes on %s', async name => {
  const stream = new RightNowStreamClient({ apiKey: 'test-key' }, name);
  expect(stream.options.streamURL).toBe(`wss://api.rightnowai.co/v1/${name === 'system-zero' ? 'system-zero' : 'decision-model'}/stream`);
  const client = new InternalClient({ apiKey: 'test-key', fetch: async url => {
    expect(url).toBe(`https://api.rightnowai.co/v1/${name === 'system-zero' ? 'system-zero' : 'decision-model'}/webhooks`);
    return Response.json({ object: 'list', data: [] });
  } }, name);
  await client.webhooks.list();
});
