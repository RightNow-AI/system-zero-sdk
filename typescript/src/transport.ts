import { assertSafeNativeDebug } from './redaction.js';
import { Agent as HttpAgent, request as requestHttp } from 'node:http';
import { Agent as HttpsAgent, request as requestHttps } from 'node:https';
import { Readable } from 'node:stream';
import { APIError } from './errors.js';

/**
 * How long an idle connection stays open for reuse, in milliseconds. Node's process-wide agents drop an idle connection
 * after 5 s whatever the server allows, so a caller that paused longer paid a new TCP and TLS handshake on its next
 * call. The service closes a caller's idle connection at 300 s, so 240 s lets go first. A shorter Keep-Alive hint from
 * the server still wins: Node lowers a connection's idle time to the hint.
 */
const IDLE_CONNECTION_MS = 240_000;
const AGENT_OPTIONS = { keepAlive: true, scheduling: 'lifo', timeout: IDLE_CONNECTION_MS } as const;
/**
 * One pair of agents for the process, not one per client, so a client created per call reuses connections too. Node
 * unreferences an idle socket and sends TCP keepalive probes on it: it never holds the process open.
 */
const connectionAgents = { http: new HttpAgent(AGENT_OPTIONS), https: new HttpsAgent(AGENT_OPTIONS) } as const;

const started = new WeakSet<AbortSignal>();
const sent = new WeakMap<AbortSignal, boolean | null>();

/** Custom transports can report a response byte before their fetch Promise resolves. */
export function markResponseStarted(signal: AbortSignal): void { started.add(signal); sent.set(signal, true); }
export function hasResponseStarted(signal: AbortSignal): boolean { return started.has(signal); }
export function requestSent(signal: AbortSignal): boolean | null { return sent.get(signal) ?? null; }

/** Node's public HTTP APIs expose bytes before complete headers, unlike global fetch. */
export const nodeFetch: typeof globalThis.fetch = (input, init = {}) => new Promise((resolve, reject) => {
  try { assertSafeNativeDebug(); }
  catch { reject(new APIError('Native HTTP debug logging cannot safely carry credentials. Disable NODE_DEBUG for http, https, net, tls, stream and websocket.')); return; }
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url);
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    reject(new TypeError('Only HTTP and HTTPS base URLs are supported.'));
    return;
  }
  const headers = Object.fromEntries(new Headers(init.headers));
  const body = init.body;
  if (body !== undefined && body !== null) {
    if (typeof body !== 'string' && !(body instanceof Uint8Array)) {
      reject(new TypeError('The HTTP transport expects serialized JSON text or bytes.'));
      return;
    }
    headers['content-length'] = String(typeof body === 'string' ? Buffer.byteLength(body) : body.byteLength);
    delete headers['transfer-encoding'];
  }
  if (init.signal) sent.set(init.signal, false);
  const request = url.protocol === 'https:'
    ? requestHttps(url, { method: init.method ?? 'GET', headers, agent: connectionAgents.https })
    : requestHttp(url, { method: init.method ?? 'GET', headers, agent: connectionAgents.http });
  let stopWatching: (() => void) | undefined;
  let cleanAbort: (() => void) | undefined;

  request.once('socket', socket => {
    const mark = () => { if (init.signal) markResponseStarted(init.signal); };
    const connected = () => {
      if (init.signal && sent.get(init.signal) === false) sent.set(init.signal, null);
    };
    // Buffered writes cannot reach an HTTP peer before connect/TLS completion.
    // After connection, only an observed flush or response proves sending.
    const event = url.protocol === 'https:' ? 'secureConnect' : 'connect';
    if (request.reusedSocket || url.protocol === 'http:' && !socket.connecting) connected();
    else socket.once(event, connected);
    // Run before the HTTP parser, whose error event can fire synchronously on bad bytes.
    socket.prependListener('data', mark);
    stopWatching = () => { socket.removeListener('data', mark); socket.removeListener(event, connected); };
  });
  request.once('finish', () => { if (init.signal) sent.set(init.signal, true); });
  request.once('error', error => {
    stopWatching?.();
    cleanAbort?.();
    reject(error);
  });
  request.once('response', incoming => {
    if (init.signal) markResponseStarted(init.signal);
    stopWatching?.();
    incoming.once('end', () => cleanAbort?.());
    incoming.once('close', () => cleanAbort?.());
    try {
      const responseHeaders = new Headers();
      for (let index = 0; index < incoming.rawHeaders.length; index += 2) responseHeaders.append(incoming.rawHeaders[index]!, incoming.rawHeaders[index + 1]!);
      const status = incoming.statusCode ?? 500;
      if (status < 200 || status > 599) throw new APIError('RightNow returned an invalid HTTP status.', {
        status, type: 'api_error', retryable: false, headers: responseHeaders,
        request_id: responseHeaders.get('x-request-id'),
      });
      const noBody = init.method === 'HEAD' || [204, 205, 304].includes(status);
      const responseBody = noBody ? null : Readable.toWeb(incoming) as ReadableStream<Uint8Array>;
      if (noBody) incoming.resume();
      const reason = incoming.statusMessage ?? '';
      const statusText = /^[\x09\x20-\x7e\x80-\xff]*$/u.test(reason) ? reason : '';
      resolve(new Response(responseBody, { status, statusText, headers: responseHeaders }));
    } catch (error) {
      cleanAbort?.();
      incoming.destroy();
      request.destroy();
      reject(error instanceof APIError ? error : new APIError('RightNow returned an invalid HTTP response.', {
        status: incoming.statusCode ?? null, type: 'api_error', retryable: false,
      }));
    }
  });
  if (init.signal) {
    const abort = () => request.destroy(new Error('RightNow HTTP transport aborted.'));
    init.signal.addEventListener('abort', abort, { once: true });
    cleanAbort = () => init.signal?.removeEventListener('abort', abort);
    if (init.signal.aborted) { abort(); return; }
  }
  if (typeof body === 'string' || body instanceof Uint8Array) request.write(body);
  request.end();
});
