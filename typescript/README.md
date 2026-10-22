# System Zero TypeScript SDK

Server-side TypeScript client for the System Zero API.

Requires Node 22 or later and ES modules (ESM).

Install with `npm install @rightnowai/system-zero`.

Set `SYSTEM_ZERO_API_KEY` in the server environment, then call the API:

```ts
import { SystemZeroClient } from '@rightnowai/system-zero';

const client = new SystemZeroClient();
const reply = await client.decide({
  model: 'system-zero',
  state: { order_total: 42 },
  questions: {
    eligible: { type: 'boolean', instructions: 'Is the order total at least 25?' },
  },
});
console.log(reply.answers);

const models = await client.models.list();
console.log(models.data);
```

The client calls `POST /system-zero/decide` and authenticated `GET /models`.
Boolean, choice and score questions use the same request. `decisionFor` and
`decisionsFor` read typed answers and per-question refusals. Responses preserve
the service's wire fields.

Use `withResponse()` on either call to access response metadata, including
request IDs, rate limits, latency and replay status. Decide options accept
`idempotencyKey`. Both calls accept `clientRequestId`, extra headers and
`retryBudget`; model listing does not need an idempotency key.
Client timeouts and retry waits are milliseconds: `timeout: 150000`,
`maxRetries: 5`, `retryBudget: 60000` and `maxRetryWait: 60000`.

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



## Ordinary HTTP retries

The defaults are five retries, 150 seconds per attempt, a 60-second total retry
budget and a 60-second maximum wait. The retry count is capped at five, so one
call makes at most six attempts. The budget starts with the first attempt and
includes requests and waits. The first attempt keeps its normal timeout; later
attempts are limited by the remaining budget. A wait above the maximum wait, or
one that exhausts the remaining budget, raises the last error without waiting.
A retry needs at least one millisecond remaining.

Only GET requests or requests carrying a nonempty idempotency key can retry.
With retries enabled, decide POSTs receive a generated key when none is supplied.
An explicit blank key omits the header and disables decision retries. Each retry
retains the same key and serialized body. Setting the retry count or budget to
zero disables retries.

Ordinary HTTP calls retry these marked responses:

- HTTP 429: `capacity_exhausted`, `rate_limit`, `rate_limit_exceeded`,
  `concurrency_limit_exceeded` or `workspace_limit_exceeded`.
- HTTP 503: `service_unavailable` or `system_zero_unavailable`.
- HTTP 409: `idempotency_conflict`.

Marked means `error.retryable` is `true` or the `x-should-retry` header is `true`.
An explicit `false` in either marker vetoes retry. A 503 warm-up response also
qualifies when its code is `starting`, or when the service's warm-up header is
`true` or `1` with `service_unavailable` or no code, and a valid retry delay is
present. That warm-up case does not require a true retry marker; a false marker
still vetoes it.

A qualifying connection failure or timeout before any response byte can also
retry, even if the request was sent. Failures after a response byte never retry.
Errors of authentication and permission types never retry.

`Retry-After-Ms` takes precedence; only when it is absent does `Retry-After`
provide seconds or an HTTP date. The wait is never below the full-jitter floor
`min(8 seconds, 0.5 seconds * 2^attempt) * random`, with attempt zero for the first
retry and random drawn from zero to one. A malformed millisecond header does not
fall back to the seconds header.

## Workflow recovery and call limits

`runBatch` and `runFlow` use ordinary `POST /system-zero/decide` calls with a stricter
recovery policy. Function tools and the MCP adapter use that policy for decide and model
listing. Only a marked HTTP 429 refusal with one of these code/`details.cause`
pairs proves that no work ran and permits recovery:

- `rate_limit_exceeded` with `key_rate` or `preauth`.
- `concurrency_limit_exceeded` with `account_concurrency`.

The marker is `error.retryable=true` or `x-should-retry: true`; an explicit
`false` in either vetoes recovery. All connection failures and timeouts, HTTP 503
(including warm-up), HTTP 409 and every other code/cause pair are terminal.
The ordinary HTTP retry allowlist above does not expand this workflow policy.
Retries retain one key and one logical call admission and use the client's
retry count, wait and time bounds. Batch retry options can only lower the
client's retry count; they cannot enable retries disabled on the client.

| Helper | Call-cap option | Default | Maximum |
| --- | --- | --- | --- |
| `runBatch` | `maxDecideCalls` | 1,000 | Nonnegative safe integer |
| `runFlow` | `max_decide_calls` | 32 | 32 |
| `createToolHandler` | `maxDecideCalls` | 100 | Nonnegative safe integer |

The flow option is spelled `max_decide_calls` even in TypeScript. The MCP
adapter uses `createToolHandler` by default and inherits its 100-call cap.
It permits two concurrent tool calls; extra concurrent calls receive local
`call_limit_reached` without dispatch.

Flow and batch reports record admitted decide calls; retries do not consume
additional logical admissions. Batch receipts also record actual HTTP attempts.
Flows admit a step only when its bounded call fits the remaining deadline;
batches also respect their deadline and the original key's replay lifetime.
Evaluation compares caller-supplied labels locally and adds no network calls.
Explicit call caps must be nonnegative safe integers; zero is allowed. A tool
handler retains its count across calls, so reuse one handler for the tool loop.
Model listing does not consume a decide count. The module-level tool helper has
a zero decide-call limit. A depleted tool limit returns local `call_limit_reached`
without dispatch; this is separate from API errors.

Function tools are exported from
`@rightnowai/system-zero/integrations/function-tools`; the MCP adapter is exported
from `@rightnowai/system-zero/integrations/mcp-server`. Both share the API request
validation and error vocabulary. Tool catalog messages are SDK summaries;
HTTP refusal messages are preserved with credential redaction.

## Response metadata

SDK errors and response metadata expose the request's idempotency key as
`idempotencyKey` (`null` when no key was sent). `RateLimitError` and
`OverloadedError` expose the response's retry delay as `retryAfterMs` in
milliseconds, or `null` when no valid retry delay is available.

Every HTTP attempt sends `X-SDK-Retry-Count`, starting at `0`, and a
`User-Agent` containing only the SDK name, SDK version and runtime version.
A blank or whitespace-only `clientRequestId` omits the
`X-Client-Request-Id` header.

Keep API keys on trusted servers. See [the security policy](https://github.com/RightNow-AI/system-zero-sdk/blob/main/SECURITY.md)
for private vulnerability reporting.
