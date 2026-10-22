import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import * as sdk from '../src/index.js';
import { createToolHandler, validateToolArguments } from '../src/integrations/function-tools/index.js';
import { createMcpServer, LEGACY_PROTOCOL_VERSION } from '../src/integrations/mcp-server/server.js';

const request = { model: 'system-zero', state: 'Synthetic media', questions: { ready: { type: 'boolean' as const, instructions: 'Is it ready?' } } };
const png = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 1, 0, 0, 0, 1, 8, 2, 0, 0, 0, 144, 119, 83, 222, 0, 0, 0, 12, 73, 68, 65, 84, 120, 156, 99, 96, 96, 96, 0, 0, 0, 4, 0, 1, 246, 23, 56, 85, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130]);
function wav(rate = 16000, codec = 1, channels = 1, bits = 16): Buffer {
  const align = channels * Math.ceil(bits / 8), bytes = Buffer.alloc(44 + align + align % 2);
  bytes.write('RIFF'); bytes.writeUInt32LE(bytes.length - 8, 4); bytes.write('WAVEfmt ', 8);
  bytes.writeUInt32LE(16, 16); bytes.writeUInt16LE(codec, 20); bytes.writeUInt16LE(channels, 22);
  bytes.writeUInt32LE(rate, 24); bytes.writeUInt32LE(rate * align, 28); bytes.writeUInt16LE(align, 32);
  bytes.writeUInt16LE(bits, 34); bytes.write('data', 36); bytes.writeUInt32LE(align, 40);
  return bytes;
}
const dataUrl = (mime: string, bytes: Uint8Array) => `data:${mime};base64,${Buffer.from(bytes).toString('base64')}`;
const image = { name: 'picture', type: 'image' as const, data: dataUrl('image/png', png) };
const audio = { name: 'recording', type: 'audio' as const, data: dataUrl('audio/wav', wav()) };

describe('served media profile', () => {
  it('exposes only the served MIME and byte limits at version 0.2.0', () => {
    expect(sdk.VERSION).toBe('0.2.0');
    expect(sdk.MEDIA_MIMES).toEqual({ image: ['image/png', 'image/jpeg'], audio: ['audio/wav'] });
    expect(sdk.MEDIA_LIMITS).toEqual({ maxAttachments: 16, maxImages: 4, maxAudio: 1, maxInlineBytes: 2621440, maxBodyBytes: 3665920, audioSampleRate: 16000, maxAudioDurationMs: 300000, maxAudioChannels: 64, maxImagePixels: 16777216, maxImageAspectRatio: 50 });
    const spec = JSON.parse(readFileSync(new URL('../../openapi.json', import.meta.url), 'utf8'));
    expect(spec.components.schemas.Attachment.properties.type.enum).toEqual(['image', 'audio']);
    expect(spec.components.schemas.DecideRequest.properties.attachments.anyOf).toContainEqual({ type: 'null' });
  });

  it.each([undefined, null, [image], [audio], [image, audio]])('preserves text, null and inline media through transport (%j)', async attachments => {
    const body = { ...request, ...(attachments === undefined ? {} : { attachments }) };
    const transport = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      expect(JSON.parse(String(init?.body))).toEqual(body);
      return Response.json({ answers: {} });
    });
    await new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport, maxRetries: 0 }).decide(body);
    expect(transport).toHaveBeenCalledTimes(1);
  });

  it.each([[16000, 1, 1, 8], [16000, 1, 2, 16], [16000, 3, 2, 32], [16000, 3, 64, 64]])('encodes valid WAV frames (%j)', (rate, codec, channels, bits) => {
    expect(sdk.inlineAttachment({ name: 'sound', type: 'audio', mime: 'audio/wav', data: wav(rate, codec, channels, bits) }))
      .toEqual({ name: 'sound', type: 'audio', data: dataUrl('audio/wav', wav(rate, codec, channels, bits)) });
  });

  it.each([
    ['image/png', png], ['image/jpeg', Buffer.from('ffd8ff', 'hex')],
  ])('reuses signature checks for %s', (mime, bytes) => {
    expect(sdk.attachmentMagicMatches(mime, bytes)).toBe(true);
    expect(sdk.inlineAttachment({ name: 'picture', type: 'image', mime, data: bytes }).data).toBe(dataUrl(mime, bytes));
    expect(sdk.attachmentMagicMatches('application/pdf', Buffer.from('%PDF-'))).toBe(false);
  });

  it.each([
    ['invalid_request', () => sdk.inlineAttachment({ name: '9bad', type: 'image', mime: 'image/png', data: png })],
    ['media_unsupported', () => sdk.inlineAttachment({ name: 'x', type: 'image', mime: 'image/bmp', data: png })],
    ['media_decode_failed', () => sdk.inlineAttachment({ name: 'x', type: 'image', mime: 'image/png', data: Buffer.from('bad') })],
    ['media_unpriced', () => sdk.inlineAttachment({ name: 'x', type: 'image', mime: 'image/gif', data: Buffer.from('GIF89a') })],
    ['media_unpriced', () => sdk.inlineAttachment({ name: 'x', type: 'image', mime: 'image/webp', data: Buffer.from('RIFF0000WEBP') })],
    ['media_too_large', () => sdk.inlineAttachment({ name: 'x', type: 'image', mime: 'image/png', data: Buffer.alloc(2621441) })],
    ...[8000, 22050, 44100, 48000].map(rate => ['media_unsupported', () => sdk.inlineAttachment({ name: 'x', type: 'audio', mime: 'audio/wav', data: wav(rate) })] as const),
    ...['audio/mpeg', 'audio/flac', 'audio/ogg', 'audio/mp4', 'audio/x-wav'].map(mime => ['media_unsupported', () => sdk.inlineAttachment({ name: 'x', type: 'audio', mime, data: wav() })] as const),
    ['media_unsupported', () => sdk.inlineAttachment({ name: 'x', type: 'audio', mime: 'audio/wav', data: Buffer.from('bad') })],
    ['media_unsupported', () => sdk.inlineAttachment({ name: 'x', type: 'audio', mime: 'audio/wav', data: wav(16000, 6) })],
    ['media_unsupported', () => sdk.inlineAttachment({ name: 'x', type: 'audio', mime: 'audio/wav', data: wav(16000, 3, 1, 16) })],
    ['invalid_request', () => sdk.validateAttachments([image, image])],
    ['invalid_request', () => sdk.validateAttachments([{ ...image, data: 'data:image/png;base64,AAAA=' }])],
    ['invalid_request', () => sdk.validateAttachments(Array.from({ length: 17 }, (_, i) => ({ ...image, name: `a${i}` })))],
    ['media_unsupported', () => sdk.validateAttachments(Array.from({ length: 5 }, (_, i) => ({ ...image, name: `a${i}` })))],
    ['media_unsupported', () => sdk.validateAttachments([audio, { ...audio, name: 'second' }])],
    ['media_too_large', () => sdk.validateAttachments([image, { ...image, name: 'second', data: dataUrl('image/png', Buffer.concat([png, Buffer.alloc(2621440 - png.length)])) }])],
  ] as const)('uses gateway code %s for local preflight', (code, action) => {
    const transport = vi.fn();
    new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport });
    expect(action).toThrow(expect.objectContaining({ code }));
    expect(transport).not.toHaveBeenCalled();
  });

  it.each([['document', 'document_not_available', 404], ['video', 'video_not_available', 404], ['image', 'service_unavailable', 503], ['audio', 'media_unsupported', 400]] as const)
    ('preserves gateway refusal for %s references', async (type, code, status) => {
      const transport = vi.fn(async () => Response.json({ error: { code, message: 'Synthetic refusal.', type: 'invalid_request_error', retryable: false } }, { status }));
      const client = new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport, maxRetries: 0 });
      const body = { ...request, attachments: [{ name: 'input', type, upload: 'szu_' + 'a'.repeat(32), mime: type === 'image' ? 'image/png' : 'audio/wav' }] };
      await expect(client.decide(body as unknown as sdk.DecideRequest)).rejects.toMatchObject({ code, status });
      expect(transport).toHaveBeenCalledTimes(1);
    });

  it('carries served media through the existing function tool schema', async () => {
    const args = { state: 'Synthetic', questions: [{ name: 'ready', type: 'boolean', instructions: 'Ready?', criteria: null }], model: null, attachments: [image, audio] };
    expect(() => validateToolArguments('system_zero_decide', args)).not.toThrow();
    const transport = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      expect(JSON.parse(String(init?.body)).attachments).toEqual([image, audio]);
      return Response.json({ answers: {} });
    });
    const handler = createToolHandler({ client: new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport, maxRetries: 0 }) });
    expect((await handler('system_zero_decide', args)).status).toBe('ok');
    expect(transport).toHaveBeenCalledTimes(1);
  });

  it('carries served media through the MCP adapter', async () => {
    const transport = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      expect(JSON.parse(String(init?.body)).attachments).toEqual([image, audio]);
      return Response.json({ answers: {} });
    });
    const mcp = createMcpServer({ handleToolCall: createToolHandler({ client: new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport, maxRetries: 0 }) }) });
    await mcp.handleMessage({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: LEGACY_PROTOCOL_VERSION, capabilities: {}, clientInfo: { name: 'synthetic', version: '1' } } });
    await mcp.handleMessage({ jsonrpc: '2.0', method: 'notifications/initialized' });
    const result = await mcp.handleMessage({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'system_zero_decide', arguments: { state: 'Synthetic', questions: [{ name: 'ready', type: 'boolean', instructions: 'Ready?', criteria: null }], model: null, attachments: [image, audio] } } });
    expect(result).toMatchObject({ result: { isError: false, structuredContent: { status: 'ok' } } });
    expect(transport).toHaveBeenCalledTimes(1);
  });
});
