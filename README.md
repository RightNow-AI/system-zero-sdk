# System Zero SDKs

TypeScript and Python clients for the System Zero API.

- [TypeScript](typescript/README.md): `npm install @rightnowai/system-zero`
- [Python](python/README.md): `pip install rightnow-system-zero`
- Function tools ship in both packages; the MCP server adapter ships in the npm package.

See [CHANGELOG.md](CHANGELOG.md) for package versions and changes.
See [SECURITY.md](SECURITY.md) for private vulnerability reporting and [LICENSE](LICENSE) for Apache 2.0 terms.

## Media on decide

`attachments` is optional; `null` remains valid. Each inline attachment has a unique
`name`, a `type` of `image` or `audio`, and a canonical base64 data URL in `data`.
Names start with an ASCII letter and contain at most 64 letters, digits, `_` or `-`.
Function tools and the MCP adapter carry the same inline forms.

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
Documents remain refused with `document_not_available`, and video with
`video_not_available`. Other audio formats, rates, malformed WAV and audio upload
references use `media_unsupported`. Image upload references are unavailable and
return `service_unavailable` in the listed service. Send bytes inline.

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
```

`MEDIA_MIMES`, `MEDIA_LIMITS` and `attachmentMagicMatches` expose the served
registry, limits and signature screen. `inlineAttachment` encodes one input;
`validateAttachments` also checks aggregate counts and decoded bytes.

```python
from pathlib import Path
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
```

`MIMES`, `MEDIA_LIMITS` and `attachment_magic_matches` expose the served registry,
limits and signature screen. `inline_attachment` encodes one input;
`validate_attachments` also checks aggregate counts and decoded bytes.

