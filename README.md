# RightNow System Zero SDKs

TypeScript and Python clients for the RightNow System Zero API.

RightNow is the model name; System Zero is its category. The full name is RightNow System Zero.

- [TypeScript](typescript/README.md): `npm install @rightnowai/system-zero`
- [Python](python/README.md): `pip install rightnow-system-zero`
- Function tools ship in both packages; the MCP server adapter ships in the npm package.

See [CHANGELOG.md](CHANGELOG.md) for package versions and changes.
See [SECURITY.md](SECURITY.md) for private vulnerability reporting and [LICENSE](LICENSE) for Apache 2.0 terms.

## Media on decide

`attachments` is optional; `null` remains valid. Each inline attachment has a unique
`name`, a `type` of `image`, `audio`, `document` or `video`, and a canonical base64 data URL in `data`.
Names start with an ASCII letter and contain at most 64 letters, digits, `_` or `-`.
Function tools and the MCP adapter carry the image and audio forms.
Send PDF and video through the ordinary client request form shown below.

- Images: static PNG (`image/png`) and JPEG (`image/jpeg`), at most 4 images,
  16,777,216 pixels each, with an aspect ratio no greater than 50:1.
- Audio: at most 1 audio attachment, inline RIFF WAV (`audio/wav`) at exactly
  16,000 Hz. Integer PCM with 1-64 bits per sample or IEEE float with 32 or 64 bits
  is supported, including the extensible WAV form; 1-64 channels are allowed.
  The duration limit is 300 seconds (4,800,000 frames); the separate long-recording
  cutoff is 300.5 seconds. The byte limit can be reached earlier.
- All decoded inline bytes together: at most 2,621,440 bytes. The entire JSON
  request, including base64, is at most 3,665,920 bytes. The schema has a 16-entry
  attachment guard; the served image and audio counts are stricter.

The helpers check MIME types, signatures, canonical encoding, counts, byte limits,
and WAV structure/rate. Preflight is explicit; complete image decoding, dimensions,
availability and other request checks remain with the API. Signatures alone do not
prove a complete image is valid.

GIF (`image/gif`), WebP (`image/webp`), animated PNG and images outside the decoded
profile are refused with `media_unpriced`. A mismatched image signature uses
`media_decode_failed`; other unsupported image MIME types use `media_unsupported`.
Other audio formats, rates, malformed WAV and audio upload
references use `media_unsupported`. Image upload references are unavailable and
return `service_unavailable` in the listed service. Send bytes inline.

PDF is supported through ordinary inline attachments.
Text PDFs and readable scans are read as text.
Only PDF is supported; other document formats are unavailable.
Use `type: "document"` and a canonical `data:application/pdf;base64,...` URL.
The image and audio helpers do not encode PDF; construct its inline attachment directly.


Video is supported through ordinary inline attachments. Send one silent H.264 MP4 video as an inline base64 data URL. Maximum duration: 10 seconds. Maximum dimensions: 1920 x 1080. Maximum decoded file size: 2,621,440 bytes (2.5 MiB). The complete JSON request must fit 3,665,920 bytes, including base64 and questions. Rotated videos, soundtracks, separate attachments and video upload references are not supported.

Use `videoAttachment({ name: 'clip', data: bytes })` in TypeScript or `video_attachment(name='clip', data=bytes)` in Python. Video helpers encode the bytes; the API performs complete-file validation. A `video_not_available` response becomes `VideoNotAvailableError`, preserving status, code, message and request ID.
```ts
import { readFileSync } from 'node:fs';
import { SystemZeroClient, inlineAttachment, validateAttachments } from '@rightnowai/system-zero';

const client = new SystemZeroClient();
const attachments = validateAttachments([
  inlineAttachment({ name: 'picture', type: 'image', mime: 'image/png', data: readFileSync('picture.png') }),
  inlineAttachment({ name: 'speech', type: 'audio', mime: 'audio/wav', data: readFileSync('speech-16khz.wav') }),
]);
const reply = await client.decide({
  model: 'system-zero', state: 'Use the picture and recording.', attachments,
  questions: { ready: { type: 'boolean', instructions: 'Is the item ready?' } },
});
console.log(reply.answers);

// PDF uses the ordinary inline request form.
await client.decide({
  model: 'system-zero', state: 'Read the PDF.',
  questions: { ready: { type: 'boolean', instructions: 'Is the item ready?' } },
  attachments: [{ name: 'document', type: 'document',
    data: 'data:application/pdf;base64,' + readFileSync('document.pdf').toString('base64') }],
});
```

`MEDIA_MIMES`, `MEDIA_LIMITS` and `attachmentMagicMatches` expose the served
registry, limits and signature screen. `inlineAttachment` encodes one input;
`validateAttachments` also checks aggregate counts and decoded bytes.

```python
from pathlib import Path
from base64 import b64encode
from system_zero import SystemZero, inline_attachment, validate_attachments

attachments = validate_attachments([
    inline_attachment(name="picture", type="image", mime="image/png", data=Path("picture.png").read_bytes()),
    inline_attachment(name="speech", type="audio", mime="audio/wav", data=Path("speech-16khz.wav").read_bytes()),
])
with SystemZero() as client:
    reply = client.decide({
        "model": "system-zero", "state": "Use the picture and recording.", "attachments": attachments,
        "questions": {"ready": {"type": "boolean", "instructions": "Is the item ready?"}},
    })
    print(reply["answers"])
    # PDF uses the ordinary inline request form.
    client.decide({
        "model": "system-zero", "state": "Read the PDF.",
        "questions": {"ready": {"type": "boolean", "instructions": "Is the item ready?"}},
        "attachments": [{"name": "document", "type": "document",
            "data": "data:application/pdf;base64," + b64encode(Path("document.pdf").read_bytes()).decode("ascii")}],
    })
```

`MIMES`, `MEDIA_LIMITS` and `attachment_magic_matches` expose the served registry,
limits and signature screen. `inline_attachment` encodes one input;
`validate_attachments` also checks aggregate counts and decoded bytes.


## LongContextRequest opt-in

The SDK publishes the `LongContextRequest` type as an opt-in surface. It is unavailable until the API enables it for the workspace; workspace opt-in through the console's Settings is also required. Once available, give explicit per-request consent with `dangerZone: 'accept'` in TypeScript or `danger_zone="accept"` in Python. The option sends exactly `SZ-Danger-Zone: accept`; it is never sent by default or inferred from state size. Until availability, the API ignores or refuses this option. Do not supply this header through custom headers. Retries retain the option. The same explicit option is supported by batch and flow helpers.

Until workspace enablement, the expected result is HTTP 404 `long_context_not_available`, surfaced as `LongContextNotAvailableError`. The same refusal applies without workspace opt-in. When the API supplies `details.eligibility`, `LongContextNotAvailableError.eligibility` exposes the rule: `billing`, `opt_in`, `class`, `deny_list`, `global_switch` or `account_deleted`, with a string fallback for future rules. A generic held-route refusal can omit the rule. Status, code, message and request ID remain intact. Held routes are never retried.

`LongContextRequest`, `EnabledDecideRequest`, `OrdinaryRequest`, `MediaRequest` and their question types describe the separate enabled request contract in `enabled.openapi.json`. `LongContextLimits` describes its discovery limits. `openapi.json` remains the served contract. The API selects the request band and checks bounds: a `LongContextRequest` takes at most 32 questions, choices with 2 to 25 options, and scores with 2 to 4 levels. Only inline document attachments fit its document branch. The API validates document admission separately.

```typescript
await client.decide(request, { dangerZone: 'accept' });
```

```python
client.decide(request, danger_zone="accept")
```
