# RightNow System Zero SDKs

TypeScript and Python clients for the RightNow System Zero API.

RightNow is the model name; System Zero is its category. The full name is RightNow System Zero.

The API has no tool calling: its request schema is strict and rejects tool-calling fields. The SDK provides "RightNow as a tool" for other models through the function-tool helpers and the MCP server adapter (the server ships in the TypeScript package).

- [TypeScript](typescript/README.md): `npm install @rightnowai/system-zero`
- [Python](python/README.md): `pip install rightnow-system-zero`
- Function tools ship in both packages; the MCP server adapter ships in the npm package.

See [CHANGELOG.md](CHANGELOG.md) for package versions and changes.
See [SECURITY.md](SECURITY.md) for private vulnerability reporting and [LICENSE](LICENSE) for Apache 2.0 terms.

## Tool selection

Tool selection: RightNow System Zero picks which of your tools to call next, or none, with a probability for each. It does not write arguments or messages, and it does not run tools.

See [Tool selection](TOOL-SELECTION.md) for helpers and limits.

## Media on decide

Check `models.list()` or the documented models endpoint for current input
availability and limits before sending an attachment. SDK types and encoders do
not enable a held API class. Use the public `system-zero` model ID; an unavailable
explicit model ID returns `model_not_found`.

Audio is served like every other attachment kind. For any kind, a 503 with
`Retry-After` is retried after that delay with the same `Idempotency-Key`;
the SDK does this within its retry limit. If the delay exceeds that limit,
wait for the reported delay, then retry the same request with the same key,
exposed on the error.

`attachments` is optional; `null` remains valid. Each inline attachment has a unique
`name`, a `type` and a canonical base64 data URL in `data`. Names start with an
ASCII letter and contain at most 64 letters, digits, `_` or `-`.
Function tools and the MCP adapter carry the inline forms listed by their schema,
including PDF and MP4. They use the same attachment kinds as the published request;
check model discovery for current API availability.

- Images: static PNG (`image/png`) and JPEG (`image/jpeg`), at most 4 images,
  16,777,216 pixels each, with an aspect ratio no greater than 50:1.
- Audio: one inline WAV (`audio/wav`) per request, with integer PCM or IEEE float
  samples at exactly 16,000 Hz, mono or multichannel. The decoded-duration limit
  is 60 seconds inclusive (60,000 ms), one pass per request; byte limits can be reached earlier.
  Audio must be the only attachment. A mixed request is refused locally with HTTP 404
  `audio_not_available`, param `attachments`.
  Audio answers currently report `calibrated: false`. Read `models.list()` for
  current availability and limits. Other audio formats, sample rates and upload
  references are not supported by this SDK's public audio preflight.
- All decoded inline bytes together: at most 2,621,440 bytes. The entire JSON
  request, including base64, is at most 3,665,920 bytes. The schema has a 16-entry
  attachment guard; individual kind counts can be stricter.

The helpers check supported MIME types, signatures, canonical encoding, counts,
byte limits and WAV structure, sample rate and decoded duration. Preflight is explicit; complete decoding, dimensions,
availability and other request checks remain with the API. Signatures alone do
not prove a complete file is valid. PDF and video validation remains with the API.

GIF (`image/gif`), WebP (`image/webp`), animated PNG and images outside the decoded
profile are not yet supported by this SDK version. The image preflight uses
`media_unpriced`, `media_decode_failed` or `media_unsupported` for unsupported or
mismatched inputs. Upload references are not served: a request naming one answers
HTTP 400 `media_unsupported` at `attachments[i].upload`. Send bytes inline.

PDF is supported through ordinary inline attachments.
Text PDFs and readable scans are read as text.
Only PDF is supported by this SDK version; consult discovery for document formats.
Send an unencrypted PDF of at most 500 pages within the common 2,621,440-byte
decoded limit. Other document formats answer HTTP 415 `document_unsupported`.
Put plain text in `state`.
Use `type: "document"` and a canonical `data:application/pdf;base64,...` URL.
`inlineAttachment` and `inline_attachment` can encode a PDF, or construct its
ordinary inline request directly as shown below.

Video is supported through ordinary inline attachments. Send one silent H.264 MP4 video as an inline base64 data URL. Maximum duration: 10 seconds. Maximum dimensions: 1920 x 1080. Maximum decoded file size: 2,621,440 bytes (2.5 MiB). The complete JSON request must fit 3,665,920 bytes, including base64 and questions. Rotated videos, soundtracks, separate attachments and video upload references are not supported.

Use `videoAttachment({ name: 'clip', data: bytes })` in TypeScript or `video_attachment(name='clip', data=bytes)` in Python. Video helpers encode the bytes; the API performs complete-file validation. A `video_not_available` response becomes `VideoNotAvailableError`, preserving status, code, message and request ID.
```ts
import { readFileSync } from 'node:fs';
import { SystemZeroClient, inlineAttachment, validateAttachments } from '@rightnowai/system-zero';

const client = new SystemZeroClient(); // Create once and reuse for the process lifetime.
const attachments = validateAttachments([
  inlineAttachment({ name: 'picture', type: 'image', mime: 'image/png', data: readFileSync('picture.png') }),
]);
const reply = await client.decide({
  model: 'system-zero', state: 'Use the picture.', attachments,
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

`MEDIA_MIMES`, `MEDIA_LIMITS` and `attachmentMagicMatches` expose the SDK encoding
registry, limits and signature screen. `inlineAttachment` encodes one input;
`validateAttachments` also checks aggregate counts and decoded bytes.

```python
from pathlib import Path
from base64 import b64encode
from system_zero import SystemZero, inline_attachment, validate_attachments

attachments = validate_attachments([
    inline_attachment(name="picture", type="image", mime="image/png", data=Path("picture.png").read_bytes()),
])
client = SystemZero()  # Create once; reuse across calls and close at process shutdown.
reply = client.decide({
    "model": "system-zero", "state": "Use the picture.", "attachments": attachments,
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

`MIMES`, `MEDIA_LIMITS` and `attachment_magic_matches` expose the SDK encoding registry,
limits and signature screen. `inline_attachment` encodes one input;
`validate_attachments` also checks aggregate counts and decoded bytes.


## LongContextRequest opt-in

Select the class for the state size explicitly. In TypeScript:

```ts
await client.decide(request, { dangerZone: 'long_context_64k' });
await client.decide(request, { dangerZone: 'long_context_128k' });
await client.decide(request, { dangerZone: 'long_context_1m' });
```

In Python:

```python
client.decide(request, danger_zone="long_context_64k")
client.decide(request, danger_zone="long_context_128k")
client.decide(request, danger_zone="long_context_1m")
```

`long_context_64k` covers 38,001 to 65,536 state tokens;
`long_context_128k` covers 65,537 to 131,072. The option sends
`SZ-Danger-Zone` with that exact value. State size never selects a class by itself.
Send the text in `state` with no attachments. A nonempty attachment list with
either class is refused locally as HTTP 404 `long_context_not_available`.
`LongContextRequest` excludes attachments. For the 64k and 128k classes, at most 32 questions are allowed,
with 2 to 25 choice options or 2 to 4 score levels. The API checks token bounds.

Use the exact class values above. With `accept`, text answers HTTP 400
`invalid_request`, param `SZ-Danger-Zone`, when more than one native class is served.

With `accept`, PDF answers HTTP 404 `long_context_not_available`; image, audio or
video drops the long grants and runs as an ordinary request.

Select `long_context_1m` explicitly with `dangerZone` / `danger_zone` for a
state above 131,072 through 1,048,576 tokens. `accept` and body size never select
it. Use one string state and exactly one question, with 2 to 25 choice options
or 2 to 4 score levels. Send no attachments, `policy`, `mode`, `route`,
`session_id` or `prompt_cache_key`. Empty or null attachments are allowed.
The rendered request is at most 1,048,575 tokens and the body at most 8,388,608
bytes. Use ordinary API-key HTTP calls; this class has no stream or sessions.
Every request carries its full context. Stored answers replay for 24 hours.

The SDK sends your `Idempotency-Key` or generates one even when retries are
disabled; a blank key is refused locally as HTTP 400 `invalid_request`, param
`Idempotency-Key`. Invalid shapes are refused locally as HTTP 404
`long_context_not_available`; a question count other than one uses
`questions_too_large`. Enable access per workspace in the Console: make one
completed payment, then open Settings, Workspace, Context access. Before access,
HTTP 404 `long_context_not_available` is uncharged; `details.eligibility` names
the first unmet step and may contain new strings.

The maximum charge is held before work and settled to exact usage. Insufficient
credit returns HTTP 402 `insufficient_credits` before work. A daily class limit
returns HTTP 429 `long_context_daily_budget_exhausted` until the next UTC day;
neither error is retried automatically. A cold pool returns HTTP 503
`service_unavailable` before any hold or charge; the SDK retries within its
normal budget using the same key. The first call
after an idle period can take several minutes to become available: the answer
names the wait in `Retry-After`; keep retrying for up to ten minutes. One reply runs at a time per pool; a concurrent
second call returns HTTP 429 `capacity_exhausted` with `Retry-After`.

No selector is sent by default. Do not supply the header
through custom SDK headers. Retries, batch, flow and their resume paths retain
the exact selected class.

Check `models.list()` and the documented models endpoint for current classes and
workspace requirements. Ordinary `limits.state_tokens` / `rendered_tokens` are
the direct window (38,000 / 40,960). Optional `limits.long_context` lists each
served band with `header_value`, `min_state_tokens`, `state_tokens`,
`rendered_tokens` and `attachments`. An ordinary over-length refusal names the
direct window and a selector that covers the state, if available. A selected
class with state outside its band returns non-retryable HTTP 404
`long_context_not_available`, param `state`, naming that band; it is not charged.
A held or ineligible class, or missing workspace opt-in,
can return HTTP 404 `long_context_not_available`, raised as
`LongContextNotAvailableError`. Its `eligibility` preserves `details.eligibility`
when supplied, including future rule strings. Held routes are never retried.

Replies type `usage.long_input_tokens`. Response metadata exposes the server's
`sz-danger-zone` values as `dangerZone` in TypeScript and `danger_zone` in Python.
`LongContextLimits` describes discovery limits; `openapi.json` describes the
served contract and `enabled.openapi.json` contains the additional request shapes.
