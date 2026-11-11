import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import * as sdk from '../src/index.js';
import { createMcpServer } from '../src/integrations/mcp-server/server.js';
import { createToolHandler, validateToolArguments } from '../src/integrations/function-tools/index.js';

const request = { model: 'system-zero', state: 'Synthetic media', questions: { ready: { type: 'boolean' as const, instructions: 'Is it ready?' } } };
const png = Buffer.from('89504e470d0a1a0a', 'hex');
const dataUrl = (mime: string, bytes: Uint8Array) => `data:${mime};base64,${Buffer.from(bytes).toString('base64')}`;
const image = { name: 'picture', type: 'image' as const, data: dataUrl('image/png', png) };
function wav(codec = 1, channels = 1, rate = 16000, bits = 16, frames = 160): Buffer {
  const align = channels * Math.ceil(bits / 8), size = frames * align;
  const raw = Buffer.alloc(44 + size + size % 2);
  raw.write('RIFF'); raw.writeUInt32LE(raw.length - 8, 4); raw.write('WAVEfmt ', 8);
  raw.writeUInt32LE(16, 16); raw.writeUInt16LE(codec, 20); raw.writeUInt16LE(channels, 22);
  raw.writeUInt32LE(rate, 24); raw.writeUInt32LE(rate * align, 28);
  raw.writeUInt16LE(align, 32); raw.writeUInt16LE(bits, 34);
  raw.write('data', 36); raw.writeUInt32LE(size, 40); return raw;
}
const audio = { name: 'recording', type: 'audio' as const, data: dataUrl('audio/wav', wav()) };

describe('published media encoding and API availability', () => {
  it('exposes the selected MIME registry at version 0.4.7', () => {
    expect(sdk.VERSION).toBe('0.4.7');
    expect(sdk.MEDIA_MIMES).toEqual({ image: ['image/jpeg', 'image/png'], audio: ['audio/wav'], document: ['application/pdf'], video: ['video/mp4'] });
    expect(sdk.MEDIA_LIMITS.maxAudioDurationMs).toBe(60_000);
    const spec = JSON.parse(readFileSync(new URL('../../openapi.json', import.meta.url), 'utf8'));
    expect(spec.components.schemas.Attachment.properties.type.enum).toEqual(['image', 'audio', 'document', 'video']);
    expect(spec.components.schemas.DecideRequest.properties.attachments.anyOf).toContainEqual({ type: 'null' });
  });

  it.each([
    ['audio', 'audio/wav', wav()],
    ['audio', 'audio/wav', wav(3, 2, 16000, 32)],
    ['document', 'application/pdf', Buffer.from('%PDF-1.7\nSynthetic signature fixture')],
    ['video', 'video/mp4', Buffer.from('000000186674797069736f6d', 'hex')],
  ] as const)('carries %s through helper, preflight and ordinary HTTP without a local refusal', async (type, mime, bytes) => {
    const attachment = sdk.inlineAttachment({ name: 'input', type, mime, data: bytes });
    expect(attachment).toEqual({ name: 'input', type, data: dataUrl(mime, bytes) });
    expect(sdk.validateAttachments([attachment])).toEqual([attachment]);
    const transport = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      expect(JSON.parse(String(init?.body)).attachments).toEqual([attachment]);
      return Response.json({ answers: {} });
    });
    await new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport }).decide({ ...request, attachments: [attachment] });
    expect(transport).toHaveBeenCalledTimes(1);
    expect(sdk.attachmentMagicMatches(mime, bytes)).toBe(true);
  });

  it.each([undefined, null, [image]])('preserves optional attachments through transport (%j)', async attachments => {
    const body = { ...request, ...(attachments === undefined ? {} : { attachments }) };
    const transport = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      expect(JSON.parse(String(init?.body))).toEqual(body);
      return Response.json({ answers: {} });
    });
    await new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport, maxRetries: 0 }).decide(body);
    expect(transport).toHaveBeenCalledTimes(1);
  });

  it('keeps PDF format restrictions and image count checks', () => {
    expect(() => sdk.inlineAttachment({ name: 'paper', type: 'document', mime: 'text/plain', data: Buffer.from('text') })).toThrow();
    expect(() => sdk.validateAttachments(Array.from({ length: 5 }, (_, i) => ({ ...image, name: `image${i}` })))).toThrow(expect.objectContaining({ code: 'media_unsupported' }));
  });

  it('checks WAV rate, structure, one-audio count and the independent byte bound', () => {
    for (const raw of [wav(1, 1, 44100), Buffer.from('RIFF malformed')]) {
      expect(() => sdk.inlineAttachment({ name: 'recording', type: 'audio', mime: 'audio/wav', data: raw })).toThrow(expect.objectContaining({ code: 'media_unsupported' }));
    }
    expect(() => sdk.validateAttachments([audio, { ...audio, name: 'second' }])).toThrow(expect.objectContaining({ code: 'media_unsupported' }));
    expect(() => sdk.inlineAttachment({ name: 'recording', type: 'audio', mime: 'audio/wav', data: wav(1, 4, 16000, 16, 400_000) })).toThrow(expect.objectContaining({ code: 'media_too_large' }));
  });

  it.each([1, 2])('accepts exactly 60 seconds with %i channels through both preflight helpers', channels => {
    const raw = wav(1, channels, 16000, 8, 960_000);
    const attachment = sdk.inlineAttachment({ name: 'recording', type: 'audio', mime: 'audio/wav', data: raw });
    expect(attachment.data).toBe(dataUrl('audio/wav', raw));
    expect(sdk.validateAttachments([attachment])).toEqual([attachment]);
  });

  it.each([960_001, 976_000])('refuses %i decoded audio frames with the API duration error', frames => {
    const raw = wav(1, 1, 16000, 8, frames);
    const error = expect.objectContaining({ code: 'media_too_large', status: 413, param: 'attachments' });
    expect(() => sdk.inlineAttachment({ name: 'recording', type: 'audio', mime: 'audio/wav', data: raw })).toThrow(error);
    expect(() => sdk.validateAttachments([{ name: 'recording', type: 'audio', data: dataUrl('audio/wav', raw) }])).toThrow(error);
  });

  it('preserves an uncalibrated audio answer through HTTP', async () => {
    const reply = { answers: { ready: { type: 'boolean', decision: true, calibrated: false } } };
    const transport = vi.fn(async () => Response.json(reply));
    expect(await new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport }).decide({ ...request, attachments: [audio] })).toEqual(reply);
  });

  it.each(['audio_not_available', 'video_not_available', 'document_not_available'])('preserves an API hold (%s)', async code => {
    const transport = vi.fn(async () => Response.json({ error: { code, message: 'Synthetic hold.', type: 'not_found_error', retryable: false } }, { status: 404 }));
    await expect(new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport }).decide(request)).rejects.toMatchObject({ code, status: 404 });
    expect(transport).toHaveBeenCalledTimes(1);
  });

  it.each([
    image,
    audio,
    { name: 'paper', type: 'document', data: dataUrl('application/pdf', Buffer.from('%PDF-1.7\nSynthetic')) },
    { name: 'clip', type: 'video', data: dataUrl('video/mp4', Buffer.from('000000186674797069736f6d', 'hex')) },
  ])('carries $type through function tools and MCP without a local refusal', async attachment => {
    const args = { state: 'Synthetic', questions: [{ name: 'ready', type: 'boolean', instructions: 'Ready?', criteria: null }], model: null, attachments: [attachment] };
    expect(() => validateToolArguments('system_zero_decide', args)).not.toThrow();
    const transport = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      expect(JSON.parse(String(init?.body)).attachments).toEqual([attachment]);
      return Response.json({ answers: {} });
    });
    const handler = createToolHandler({ client: new sdk.SystemZeroClient({ apiKey: 'SYNTHETIC', fetch: transport }) });
    expect((await handler('system_zero_decide', args)).status).toBe('ok');
    const mcp = createMcpServer({ handleToolCall: handler });
    await mcp.handleMessage({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'synthetic', version: '1' } } });
    await mcp.handleMessage({ jsonrpc: '2.0', method: 'notifications/initialized' });
    const result = await mcp.handleMessage({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'system_zero_decide', arguments: args } });
    expect(result).toMatchObject({ result: { isError: false } });
    expect(transport).toHaveBeenCalledTimes(2);
  });
});

it.each(['image', 'document', 'video'] as const)('refuses audio beside %s with the API error', type => {
  const other = { name: 'other', type, data: dataUrl(type === 'document' ? 'application/pdf' : type === 'video' ? 'video/mp4' : 'image/png', png) };
  for (const attachments of [[audio, other], [other, audio]]) {
    expect(() => sdk.validateAttachments(attachments)).toThrow(expect.objectContaining({ status: 404, code: 'audio_not_available', param: 'attachments' }));
  }
});
