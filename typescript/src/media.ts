import type { Attachment } from './api-types.js';
import { responseError, type SystemZeroError } from './errors.js';

export type MediaKind = Attachment['type'];
export type InlineAttachment = Attachment;
export interface MediaInput { name: string; type: MediaKind; mime: string; data: Uint8Array }

export const MEDIA_LIMITS = Object.freeze({
  "maxAttachments": 16,
  "maxImages": 4,
  "maxAudio": 1,
  "maxInlineBytes": 2621440,
  "maxBodyBytes": 3665920,
  "audioSampleRate": 16000,
  "maxAudioDurationMs": 300000,
  "maxAudioChannels": 64,
  "maxImagePixels": 16777216,
  "maxImageAspectRatio": 50
});
export const MEDIA_MIMES = Object.freeze({
  image: Object.freeze(["image/png", "image/jpeg"]),
  audio: Object.freeze(["audio/wav"]),
});

const errors = {
  "invalid_request": [
    400,
    "The RightNow System Zero request is invalid."
  ],
  "media_unsupported": [
    400,
    "The media kind or count is not supported. At most 4 images and 1 audio are allowed."
  ],
  "media_too_large": [
    413,
    "The media exceeds the decoded byte limit."
  ],
  "media_decode_failed": [
    422,
    "The media bytes do not match the declared format."
  ],
  "media_unpriced": [
    403,
    "This input type is not supported."
  ]
} as const;
function refusal(code: keyof typeof errors, param: string): SystemZeroError {
  const [status, message] = errors[code];
  return responseError(status, new Headers(), { error: { code, message, param, type: 'invalid_request_error', retryable: false } }, false);
}
const namePattern = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/u;
const fields = new Set(['name', 'type', 'data']);
function identity(name: string, type: MediaKind): void {
  if (typeof name !== 'string' || !namePattern.test(name) || !Object.hasOwn(MEDIA_MIMES, type)) throw refusal('invalid_request', 'attachments');
}

function signatureMatches(mime: string, data: Uint8Array): boolean {
  const bytes = Buffer.from(data.buffer, data.byteOffset, data.byteLength);
  const starts = (hex: string) => bytes.subarray(0, hex.length / 2).equals(Buffer.from(hex, 'hex'));
  const ascii = (start: number, length: number) => bytes.subarray(start, start + length).toString('latin1');
  switch (mime) {
    case 'image/png': return starts('89504e470d0a1a0a');
    case 'image/jpeg': return starts('ffd8ff');
    case 'image/gif': return ['GIF87a', 'GIF89a'].includes(ascii(0, 6));
    case 'image/webp': return ascii(0, 4) === 'RIFF' && ascii(8, 4) === 'WEBP';
    case 'audio/wav': return ascii(0, 4) === 'RIFF' && ascii(8, 4) === 'WAVE';
    default: return false;
  }
}

/** Signature checks are a screen; the API validates complete image contents. */
export function attachmentMagicMatches(mime: string, data: Uint8Array): boolean {
  return Object.values(MEDIA_MIMES).some(mimes => (mimes as readonly string[]).includes(mime)) && signatureMatches(mime, data);
}

function need(value: unknown): asserts value {
  if (!value) throw refusal('media_unsupported', 'attachments');
}

function audioFrames(b: Buffer): number {
  need(b.length >= 12 && b.toString("latin1", 0, 4) === "RIFF" && b.readUInt32LE(4) === b.length - 8 && b.toString("latin1", 8, 12) === "WAVE");
  let pos = 12, align = 0, rate = 0, data = -1, format = false;
  while (pos < b.length) {
    need(pos + 8 <= b.length);
    const kind = b.toString("latin1", pos, pos + 4), size = b.readUInt32LE(pos + 4), start = pos + 8;
    need(start + size + (size % 2) <= b.length);
    if (kind === "fmt ") {
      need(!format && size >= 16); format = true;
      let codec = b.readUInt16LE(start);
      const channels = b.readUInt16LE(start + 2), bits = b.readUInt16LE(start + 14);
      rate = b.readUInt32LE(start + 4); align = b.readUInt16LE(start + 12);
      need(channels > 0 && channels <= 64 && rate > 0 && rate <= 768000 && bits > 0 && bits <= 64);
      if (codec === 0xfffe) {
        need(size >= 40 && b.readUInt16LE(start + 16) >= 22 && 18 + b.readUInt16LE(start + 16) <= size);
        const valid = b.readUInt16LE(start + 18); need(valid > 0 && valid <= bits);
        need(b.subarray(start + 28, start + 40).equals(Buffer.from("00001000800000aa00389b71", "hex")));
        codec = b.readUInt32LE(start + 24);
      } else if (size > 16) need(size >= 18 && 18 + b.readUInt16LE(start + 16) <= size);
      need(codec === 1 || codec === 3 && [32, 64].includes(bits));
      need(align === channels * Math.ceil(bits / 8) && b.readUInt32LE(start + 8) === rate * align);
    } else if (kind === "data") { need(data === -1); data = size; }
    pos = start + size + (size % 2);
  }
  need(format && data > 0 && data % align === 0);
  // Mixing channels preserves frames. Native 16 kHz needs neither decoding nor resampling to count them.
  need(rate === MEDIA_LIMITS.audioSampleRate); return data / align;
}

function content(type: MediaKind, mime: string, data: Uint8Array, param: string): void {
  if (type === 'audio') {
    if (mime !== 'audio/wav') throw refusal('media_unsupported', param);
    audioFrames(Buffer.from(data.buffer, data.byteOffset, data.byteLength));
    return;
  }
  // These two formats pass the initial MIME screen but cannot be admitted for billing.
  if (mime === 'image/gif' || mime === 'image/webp') {
    if (!signatureMatches(mime, data)) throw refusal('media_decode_failed', param);
    throw refusal('media_unpriced', param);
  }
  if (!(MEDIA_MIMES[type] as readonly string[]).includes(mime)) throw refusal('media_unsupported', param);
  if (!signatureMatches(mime, data)) throw refusal('media_decode_failed', param);
}

/** Encode one served inline attachment with bounded, caller-known checks. */
export function inlineAttachment(input: MediaInput): InlineAttachment {
  identity(input.name, input.type);
  if (!(input.data instanceof Uint8Array)) throw refusal('invalid_request', 'attachments');
  if (input.data.byteLength > MEDIA_LIMITS.maxInlineBytes) throw refusal('media_too_large', 'attachments');
  content(input.type, input.mime, input.data, 'attachments');
  return { name: input.name, type: input.type, data: `data:${input.mime};base64,${Buffer.from(input.data).toString('base64')}` };
}

/** Explicit preflight; availability, references and complete image decoding belong to the API. */
export function validateAttachments<T extends readonly Attachment[]>(attachments: T): T {
  if (!Array.isArray(attachments)) throw refusal('invalid_request', 'attachments');
  // Preserve the API's availability/referral precedence for untyped callers.
  if ((attachments as readonly unknown[]).some(item => item !== null && typeof item === 'object'
    && ('upload' in item || 'type' in item && (item.type === 'document' || item.type === 'video')))) return attachments;
  if (attachments.length > MEDIA_LIMITS.maxAttachments) throw refusal('invalid_request', 'attachments');
  const names = new Set<string>();
  for (const [index, item] of attachments.entries()) {
    if (item === null || typeof item !== 'object' || Array.isArray(item) || Object.keys(item).some(key => !fields.has(key))) throw refusal('invalid_request', 'attachments');
    identity(item.name, item.type);
    if (typeof item.data !== 'string' || !item.data.startsWith('data:') || names.has(item.name)) throw refusal('invalid_request', `attachments[${index}]`);
    names.add(item.name);
  }
  const counts = { image: 0, audio: 0 };
  let total = 0;
  for (const [index, item] of attachments.entries()) {
    counts[item.type]++;
    if (counts.image > MEDIA_LIMITS.maxImages || counts.audio > MEDIA_LIMITS.maxAudio) throw refusal('media_unsupported', 'attachments');
    const param = `attachments[${index}].data`;
    const comma = item.data.indexOf(','), header = item.data.slice(0, comma), encoded = item.data.slice(comma + 1);
    if (comma < 0 || !/^data:[a-z0-9.+/-]+;base64$/u.test(header)) throw refusal('invalid_request', param);
    const size = encoded.length / 4 * 3 - (encoded.endsWith('==') ? 2 : encoded.endsWith('=') ? 1 : 0);
    if (size > MEDIA_LIMITS.maxInlineBytes - total) throw refusal('media_too_large', param);
    if (encoded.length % 4 !== 0) throw refusal('invalid_request', param);
    const bytes = Buffer.from(encoded, 'base64');
    if (bytes.toString('base64') !== encoded) throw refusal('invalid_request', param);
    content(item.type, header.slice(5, -7), bytes, param);
    total += bytes.length;
  }
  return attachments;
}
