import { afterEach, describe, expect, it, vi } from 'vitest';
import { StreamProtocolError, SystemZeroStreamClient, type StreamClientOptions } from '../src/stream.js';

const configurationError = 'apiKey must contain only visible ASCII characters.';
const urlError = 'streamURL must be WSS or loopback WS without credentials, query or fragment.';
const loopbackHosts = [
  [127, 0, 0, 1].join('.'), [127, 2, 3, 4].join('.'), '[' + ':' + ':1]', ['local', 'host'].join(''),
];
const withHost = (host: string) => {
  const url = new URL('ws://sdk.example.com/v1/system-zero/stream');
  url.hostname = host;
  return url.href;
};
const userinfo = new URL('wss://sdk.example.com/v1/system-zero/stream');
userinfo.username = 'synthetic';
userinfo.password = 'synthetic';

function thrown(work: () => unknown): Error {
  try { work(); } catch (error) { expect(error).toBeInstanceOf(Error); return error as Error; }
  throw new Error('Expected stream configuration refusal.');
}

afterEach(() => { vi.unstubAllEnvs(); });

describe('stream credential and destination safeguards', () => {
  it.each(['\n', '\r', '\0', '\t', ' ', '\u007f', '\u00e9'])('rejects a malformed key before opening a socket: %j', character => {
    const key = 'SYNTHETIC' + character + 'VALUE';
    const Socket = vi.fn();
    const error = thrown(() => new SystemZeroStreamClient({
      apiKey: key, WebSocket: Socket as unknown as NonNullable<StreamClientOptions['WebSocket']>,
    }));
    expect(error.message).toBe(configurationError);
    expect(error.cause).toBeUndefined();
    expect(String(error.stack)).not.toContain(key);
    expect(Socket).not.toHaveBeenCalled();
  });

  it('validates an environment key with the same fixed refusal', () => {
    vi.stubEnv('SYSTEM_ZERO_API_KEY', 'SYNTHETIC\nVALUE');
    expect(thrown(() => new SystemZeroStreamClient()).message).toBe(configurationError);
  });

  it.each([
    'ws://sdk.example.com/v1/system-zero/stream',
    withHost(loopbackHosts[0] + '.example.com'),
    withHost(loopbackHosts[3] + '.example.com'),
    withHost([10, 0, 0, 1].join('.')),
    'wss://sdk.example.com/v1/system-zero/stream?',
    'wss://sdk.example.com/v1/system-zero/stream#',
    'wss://sdk.example.com/v1/system-zero/stream?x=1',
    'wss://sdk.example.com/v1/system-zero/stream#fragment',
    userinfo.href,
    'SYNTHETIC_INVALID_STREAM_URL',
  ])('rejects an insecure or invalid destination without a socket: %s', streamURL => {
    const Socket = vi.fn();
    const error = thrown(() => new SystemZeroStreamClient({ apiKey: 'SYNTHETIC', streamURL,
      WebSocket: Socket as unknown as NonNullable<StreamClientOptions['WebSocket']> }));
    expect(error.message).toBe(urlError);
    expect(error.cause).toBeUndefined();
    expect(Socket).not.toHaveBeenCalled();
  });

  it.each(['wss://sdk.example.com/v1/system-zero/stream', ...loopbackHosts.map(withHost)])('accepts WSS or loopback WS lazily: %s', streamURL => {
    const Socket = vi.fn();
    const client = new SystemZeroStreamClient({ apiKey: 'SYNTHETIC', streamURL,
      WebSocket: Socket as unknown as NonNullable<StreamClientOptions['WebSocket']> });
    expect(client.options.streamURL).toBe(new URL(streamURL).href);
    expect(Socket).not.toHaveBeenCalled();
  });

  it('revalidates a replaced public destination before passing credentials to a socket', async () => {
    const Socket = vi.fn(function () { throw new StreamProtocolError('SYNTHETIC fixture stopped.'); });
    const client = new SystemZeroStreamClient({ apiKey: 'SYNTHETIC', maxReconnectAttempts: 1,
      WebSocket: Socket as unknown as NonNullable<StreamClientOptions['WebSocket']> });
    Object.defineProperty(client, 'options', { value: Object.freeze({ ...client.options,
      streamURL: 'ws://sdk.example.com/v1/system-zero/stream',
    }) });
    try {
      const error = await client.connect().catch(error => error as Error);
      expect(Socket).not.toHaveBeenCalled();
      expect(error).toMatchObject({ message: urlError });
    } finally {
      await client.close();
    }
  });
});
