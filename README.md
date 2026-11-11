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

1M needs nothing switched on. It is experimental, not stable.
1M is not guaranteed to be available for every request.

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
`questions_too_large`. Nothing needs to be switched on in the Console. A
workspace that has made one completed payment can use the 1M class; the request
option `dangerZone` / `danger_zone` (`SZ-Danger-Zone: long_context_1m`) is still
required. An ineligible workspace gets HTTP 404 `long_context_not_available`
without a charge; `details.eligibility` names the first unmet step and may
contain new strings. A state too short for this class is refused with HTTP 404
`long_context_not_available`, param `state`, without a charge.

The maximum charge is held before work and settled to exact usage. Insufficient
credit returns HTTP 402 `insufficient_credits` before work and is not retried
automatically. A workspace that reaches its daily bound for this class gets
HTTP 429 `long_context_daily_budget_exhausted` with `details.scope` equal to
`"workspace"` or `"workspace_attempts"`. The day-wide limit returns the same code
without `details.scope`. None of these daily refusals is retried automatically;
all clear at the next UTC day. A cold pool returns HTTP 503
`service_unavailable` before any hold or charge; the SDK retries within its
normal budget using the same key. The first call after an idle period can take several minutes to become available: the answer names the wait in `Retry-After`; send the same request again with the same Idempotency-Key until it answers. One reply runs at a time per pool; a concurrent
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
A held or ineligible class can return HTTP 404
`long_context_not_available`, raised as
`LongContextNotAvailableError`. Its `eligibility` preserves `details.eligibility`
when supplied, including future rule strings. Held routes are never retried.

Replies type `usage.long_input_tokens`. Response metadata exposes the server's
`sz-danger-zone` values as `dangerZone` in TypeScript and `danger_zone` in Python.
`LongContextLimits` describes discovery limits; `openapi.json` describes the
served contract and `enabled.openapi.json` contains the additional request shapes.

## Request policy

Every workspace can send `policy` on ordinary decide calls and stream starts.
`min_confidence` sets a minimum answer confidence greater than zero and at most
one. Below it, the answer keeps `confidence`, adds `abstained: true` and
`abstention_reason: "low_confidence"`, and has no `decision` member. The reply
echoes `policy`. An abstained answer is charged exactly like any answered question.
The decision readers return the `abstained` kind without a decision.

`max_cost_usd` caps the request's maximum possible cost. If that maximum exceeds
the cap, the API refuses the request with HTTP 400 `policy_max_cost_exceeded`,
parameter `policy.max_cost_usd`, without charging it.

`on_low_confidence` defaults to `abstain`, its only served value.
`on_low_confidence: "error"` and `require_calibrated` are not served: they are
refused with HTTP 400 `invalid_request` and their respective
`policy.on_low_confidence` or `policy.require_calibrated` parameter.
The 1M class does not accept policy.

```ts
import { SystemZeroClient, decisionsFor } from '@rightnowai/system-zero';

const client = new SystemZeroClient();
const reply = await client.decide({
  model: 'system-zero', state: 'The shipment arrived.',
  questions: { arrived: { type: 'boolean', instructions: 'Has it arrived?' } },
  policy: { min_confidence: 0.8 },
});
for (const result of Object.values(decisionsFor(reply))) {
  if (result.kind === 'abstained') console.log(result.abstention_reason);
  else if (result.kind === 'v1') console.log(result.decision);
}
```

```python
from system_zero import SystemZero, decisions_for

client = SystemZero()  # Reuse this client; close it at shutdown.
reply = client.decide({
    "model": "system-zero", "state": "The shipment arrived.",
    "questions": {"arrived": {"type": "boolean", "instructions": "Has it arrived?"}},
    "policy": {"min_confidence": 0.8},
})
for result in decisions_for(reply).values():
    if result.kind == "abstained":
        print(result.abstention_reason)
    elif result.kind == "v1":
        print(result.decision)
```

## Decision records

Read a decision again by its id with the same API key and workspace. Only decides
sent with an `Idempotency-Key` are kept, for 24 hours. The stored reply is returned
unchanged. Its trace carries no content; some members are null in this version.

An unknown id, an unkeyed decision, another key or workspace, or an expired record
returns HTTP 404 `decision_not_found`. Reads use the key's request limit, create
no usage and are not charged.

```ts
import { SystemZeroClient } from '@rightnowai/system-zero';

const client = new SystemZeroClient();
const reply = await client.decide({
  model: 'system-zero', state: 'The shipment arrived.',
  questions: { arrived: { type: 'boolean', instructions: 'Has it arrived?' } },
}, { idempotencyKey: 'shipment-check' });
const stored = await client.getDecision(reply.id);
const trace = await client.getDecisionTrace(reply.id);
```

```python
from system_zero import SystemZero

client = SystemZero()  # Reuse this client; close it at shutdown.
reply = client.decide({
    "model": "system-zero", "state": "The shipment arrived.",
    "questions": {"arrived": {"type": "boolean", "instructions": "Has it arrived?"}},
}, idempotency_key="shipment-check")
stored = client.get_decision(reply["id"])
trace = client.get_decision_trace(reply["id"])
```

## Decision provenance

Every ordinary decide reply carries `provenance`, including replies delivered by
stream. It adds no cost. The largest context class does not carry it, a reply
too large to carry it omits it, and a decision stored before 2026-11-11 is
returned without it. Check for the member before reading it.

- `decision_id`: use this id for a later decision lookup.
- `request_id`: match the original decision request, including after a replay.
- `created`: read when the decision was created, as Unix seconds.
- `model`: see the model name you requested.
- `resolved_release`: see the release that answered.
- `render_id`: an opaque identifier of the input rendering.
- `numeric_fingerprint`: compare the numeric configuration used for replies.
- `calibration_version`: the calibration version for each answered question, as
  opaque strings.
- `route`: see the route that handled the decision.
- `served_by`: read the serving identifier returned for the decision.
- `attachment_classes`: see the kinds of attachments used, without their content.
- `keyed_replay`: `true` only when the reply is a keyed replay (the same
  `Idempotency-Key` sent again); `false` on a fresh reply and on a decision lookup.

```ts
if (reply.provenance) {
  console.log(reply.provenance.resolved_release);
}
```

```python
if reply.get("provenance"):
    print(reply["provenance"]["resolved_release"])
```

## Webhooks

Create an endpoint for `decision.completed`, `decision.refused` or `stream.closed`.
`decision.completed` events are sent when the decision's usage is recorded, usually within about five minutes of the answer, and can include decisions answered a few minutes before the endpoint was created.
`decision.refused` and `stream.closed` events are sent within about a minute; a new endpoint starts receiving them within 30 seconds.
A decision of the largest context class produces no event in this release. A keyed replay produces no event.

Each delivery carries one signature header: `Webhook-Signature: t=<unix seconds>,v1=<64 hex>`.
It is HMAC-SHA256 over the ASCII timestamp, a period and the original body bytes.
Verify the raw bytes before parsing JSON. The signature helper accepts timestamps within 300 seconds of the receiver's clock.
Deduplicate deliveries by event id.

The endpoint secret (`whsec_...`) is shown once when you create the endpoint and once when you rotate its secret. Store it securely then.
Payloads contain no request state, question text, answers' free text or attachments.
An event has an id, type and creation time. Decision data contains decision and request ids, creation time, model, route, input classes and their status, response status, refusal code and usage counters.
Stream-close data contains the stream id, close code and reason.

```ts
import { SystemZeroClient, verifyWebhookSignature } from '@rightnowai/system-zero';

const client = new SystemZeroClient({ apiKey: (process.env.SYSTEM_ZERO_API_KEY ?? '') });
const endpoint = await client.webhooks.create({
  url: 'https://example.com/events', events: ['decision.completed'],
});
// Store endpoint.secret securely now; later reads do not include it.

export async function handleDelivery(request: Request, storedSecret: string): Promise<Response> {
  const rawBody = new Uint8Array(await request.arrayBuffer());
  const signature = request.headers.get('Webhook-Signature') ?? '';
  if (!verifyWebhookSignature(rawBody, signature, storedSecret)) {
    return new Response('Invalid signature', { status: 400 });
  }
  const event = JSON.parse(new TextDecoder().decode(rawBody));
  // Deduplicate by event.id before processing event.data.
  return new Response(null, { status: 204 });
}
```

```python
import json
import os
from system_zero import SystemZero, verify_webhook_signature

with SystemZero(api_key=os.environ["SYSTEM_ZERO_API_KEY"]) as client:
    endpoint = client.webhooks.create({
        "url": "https://example.com/events", "events": ["decision.completed"],
    })
    # Store endpoint["secret"] securely now; later reads do not include it.

def handle_delivery(raw_body: bytes, signature_header: str, stored_secret: str):
    if not verify_webhook_signature(raw_body, signature_header, stored_secret):
        raise ValueError("Invalid signature")
    event = json.loads(raw_body)
    # Deduplicate by event["id"] before processing event["data"].
    return event
```
