# RightNow decision model Python SDK


The API has no tool calling: its request schema is strict and rejects tool-calling fields. The SDK provides "RightNow as a tool" for other models through the function-tool helpers and the MCP server adapter (the server ships in the TypeScript package).

Synchronous server-side Python client for the RightNow decision model API.

Requires Python 3.10 or later.

Install with `pip install rightnow-sdk`; import it as `rightnow`.

Set `RIGHTNOW_API_KEY` in the server environment, then call the API:

```python
from rightnow import RightNow

client = RightNow()  # Reuse for the process lifetime; close at shutdown.
reply = client.decide({
    "model": "rightnow-decision",
    "state": {"order_total": 42},
    "questions": {
        "eligible": {
            "type": "boolean",
            "instructions": "Is the order total at least 25?",
        },
    },
})
print(reply["answers"])
print(client.models.list()["data"])
```

The client calls `POST /decision-model/decide` and authenticated `GET /decision-model/models`.
Boolean, choice and score questions use the same request. `decision_for` and
`decisions_for` read typed answers and per-question refusals. Responses preserve
the service's wire fields.

Use `decide_with_response` or `models.list_with_response` for response metadata,
including request IDs, rate limits, latency and replay status. Decide options
accept `idempotency_key`. Both calls accept `client_request_id`, extra headers
and `retry_budget`; model listing has no idempotency-key option. Client timeouts
and retry waits are seconds: `timeout=150.0`, `max_retries=5`,
`retry_budget=60.0` and `max_retry_wait=60.0`. Close the client or use its context
manager to release the HTTP transport.

Request bodies of 32 KB or more are sent gzip-compressed when that makes them at least 10 percent smaller; pass `compress_requests=False` to turn this off.

## Media on decide

Check `models.list()` or the documented models endpoint for current input
availability and limits before sending an attachment. SDK types and encoders do
not enable a held API class. Use the public `rightnow-decision` model ID. A model id of a retired version is
answered by the current version; `served_by` names the version that answered
(`model` in compatible formats). An id that was never served returns `model_not_found`.

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
```python
from pathlib import Path
from base64 import b64encode
from rightnow import RightNow, inline_attachment, validate_attachments

attachments = validate_attachments([
    inline_attachment(name="picture", type="image", mime="image/png", data=Path("picture.png").read_bytes()),
])
client = RightNow()  # Create once; reuse across calls and close at process shutdown.
reply = client.decide({
    "model": "rightnow-decision", "state": "Use the picture.", "attachments": attachments,
    "questions": {"ready": {"type": "boolean", "instructions": "Is the item ready?"}},
})
print(reply["answers"])
# PDF uses the ordinary inline request form.
client.decide({
    "model": "rightnow-decision", "state": "Read the PDF.",
    "questions": {"ready": {"type": "boolean", "instructions": "Is the item ready?"}},
    "attachments": [{"name": "document", "type": "document",
        "data": "data:application/pdf;base64," + b64encode(Path("document.pdf").read_bytes()).decode("ascii")}],
})
```

`MIMES`, `MEDIA_LIMITS` and `attachment_magic_matches` expose the SDK encoding registry,
limits and signature screen. `inline_attachment` encodes one input;
`validate_attachments` also checks aggregate counts and decoded bytes.



## Ordinary HTTP retries

`idempotency="auto"` generates keys for safe automatic retries and keeps the answer for 24 hours for replay and `GET /v1/decision-model/decisions/{id}`. The default `idempotency="off"` stores nothing unless you supply a key; a 1M request always sends a key.

The defaults are five retries, 150 seconds per attempt, a 60-second total retry
budget and a 60-second maximum wait. The retry count is capped at five, so one
call makes at most six attempts. The recovery budget starts at the first failure and
includes retry requests and waits. The first attempt keeps its normal timeout; later
attempts are limited by the remaining budget. A wait above the maximum wait, or
one that exhausts the remaining budget, raises the last error without waiting.
A retry needs at least one millisecond remaining.

GETs and keyed POSTs retain their existing retry policy.
Unkeyed decisions retry only a proven before-send failure, HTTP 429, or HTTP 503 with `error.retryable: true`.
Outside the 1M class, an explicit blank key omits the header.
The 1M class rejects blank keys and generates an omitted key even with retries disabled. Each retry
retains the same key and serialized body. Setting the retry count or budget to
zero disables retries.

GETs and keyed HTTP calls retry these marked responses:

- HTTP 429: `capacity_exhausted`, `rate_limit_exceeded`,
  `concurrency_limit_exceeded` or `workspace_limit_exceeded`.
- HTTP 5xx except 504 and `maintenance`, including `service_unavailable` and `model_unavailable`.
- HTTP 409: `idempotency_conflict`.

Marked means `error.retryable` is `true` or the `x-should-retry` header is `true`.
An explicit `false` in either marker vetoes retry. A cold pool answers marked
503 `model_unavailable` with its wait in `Retry-After`.

A keyed POST or GET can also retry a qualifying connection failure or timeout
before any response byte, even if the request was sent. Failures after a response byte never retry.
Errors of authentication and permission types never retry. Python also refuses retry for
codes containing `spend`, `budget`, `auth` or `api_key`.

`Retry-After-Ms` takes precedence; only when it is absent does `Retry-After`
provide seconds or an HTTP date. The wait is never below the full-jitter floor
`min(8 seconds, 0.5 seconds * 2^attempt) * random`, with attempt zero for the first
retry and random drawn from zero to one. A malformed millisecond header does not
fall back to the seconds header.

## Workflow recovery and call limits

`run_batch` and `run_flow` use ordinary `POST /decision-model/decide` calls with a stricter
recovery policy. Function tools use that policy for decide and model
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
| `run_batch` | `max_decide_calls` | 1,000 | Nonnegative safe integer |
| `run_flow` | `max_decide_calls` | 32 | 32 |
| `create_tool_handler` / `ToolHandler` | `max_decide_calls` | 100 | Nonnegative safe integer |

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

`rightnow.tools` supplies function tool definitions and the
`ToolHandler`, `create_tool_handler` and `handle_tool_call` helpers.
They share the API request
validation and error vocabulary. Tool catalog messages are SDK summaries;
HTTP refusal messages are preserved with credential redaction.

## Response metadata

SDK errors and response metadata expose the request's idempotency key as
`idempotency_key` (`None` when no key was sent). All HTTP errors expose the response's retry delay as `retry_after_ms` in
milliseconds, or `None` when no valid retry delay is available.

Every HTTP attempt sends `X-SDK-Retry-Count`, starting at `0`, and a
`User-Agent` containing only the SDK name, SDK version and runtime version.
A blank or whitespace-only `client_request_id` omits the
`X-Client-Request-Id` header.

Keep API keys on trusted servers. Don't enable DEBUG logging for the HTTP or WebSocket libraries in production; it can record request headers, including your API key. See [the security policy](https://github.com/RightNow-AI/system-zero-sdk/blob/main/SECURITY.md)
for private vulnerability reporting.

## Latency

Create one client per process and reuse it across calls. Do not create a client per call.
Close it at process shutdown. Default clients share a process-wide connection
pool; closing one client does not close the pool. The transport keeps idle connections
for up to 240 seconds, subject to the server closing sooner, so calls after a pause
can skip connection setup. The first call after a long pause opens a new connection.
An open stream skips connection setup for subsequent decisions; reuse it while
sending state changes. A context manager should span the application lifetime.

## Function tool formats

Breaking: the three function-tool lists were renamed by their JSON shape: NESTED_FUNCTION_TOOLS (nested function objects), FLAT_FUNCTION_TOOLS (flat function objects), INPUT_SCHEMA_TOOLS (input_schema objects). Update imports; the payloads are unchanged.

The three lists describe the same functions. Choose the shape your tool runner accepts:

- `NESTED_FUNCTION_TOOLS`: `{"type":"function","function":{"name":"...","description":"...","parameters":{},"strict":true}}`.
- `FLAT_FUNCTION_TOOLS`: `{"type":"function","name":"...","description":"...","parameters":{},"strict":true}`.
- `INPUT_SCHEMA_TOOLS`: `{"name":"...","description":"...","input_schema":{}}`.

Each list owns its schema objects. Pass the function name and its JSON arguments to the tool handler.

## Streaming decisions

Supply `RIGHTNOW_API_KEY` in the server environment before running this example.

The package root and `rightnow.stream` export `RightNowStreamClient`;
`rightnow.stream_interfaces` supplies its frame types. The installed runtime
dependency is `websockets>=15.0.1,<18`. Compression, automatic proxy selection,
and automatic transport pings are disabled; the client manages the gateway's
heartbeat and reconnect contract explicitly.

```python
import asyncio
from rightnow import RightNowStreamClient

async def main():
    async with RightNowStreamClient() as stream:
        events = stream.events()
        await stream.connect()
        await stream.start({
            "model": "rightnow-decision", "state": "Order received.",
            "questions": {"ready": {"type": "boolean", "instructions": "Is the order ready?"}},
        }, decision_mode="on_request", min_interval_ms=100)
        await stream.append_text(" Payment received.")
        await stream.decide()
        async for event in events:
            if event["type"] == "decision":
                print(event["reply"], event["request_id"])
                break
            if event["type"] == "error":
                print(event["envelope"])

asyncio.run(main())
```

The client sends `Authorization: Bearer <key>` during the RFC 6455 version 13
handshake and selects only `rightnow-decision.stream.v1`. Keep the key on a trusted
server. The connection uses `wss://api.rightnowai.co/v1/decision-model/stream`.

Start with state and questions, then append text, append JSON items, append
attachments, or replace the state. A mutation resolves when its acknowledgement
arrives. In `on_change` mode, accepted mutations schedule decisions; in
`on_request` mode, call `decide` to schedule one explicitly and read its decision
event. One decision can cover several accepted mutations.

With auto idempotency enabled or a caller-supplied state key, reconnect restores the latest accepted state using stable idempotency keys;
it never resends old wire frames. The default `latest` policy resumes the selected
decision mode. The `never` policy restores state in `on_request` mode, leaving
the next decision to the caller. Neither policy retries authentication or protocol
failures; an unkeyed stream refuses replay after a lost connection. Idle closure reopens lazily when new work arrives. Listen for
`possibly_decided`: a lost connection can leave accepted work whose outcome is
unknown, and reopening does not prove that it was uncounted. Opening attempts
are bounded by the retry count and elapsed-time options.

Limits are server-advertised in the hello frame. The SDK paces outgoing state
events using `max_events_per_second` and the advertised `event_burst` (or `burst`)
capacity; when no burst is supplied it conservatively allows one event at a time.
Await each mutation to observe queue backpressure. Waiting for a token does not
start its receipt timeout or block heartbeat replies. The SDK rejects messages
above `max_message_bytes` before sending. Read hello for session lifetime, idle,
heartbeat, accumulated request and decision interval limits; these can change.
The SDK answers heartbeats automatically. A `stream_limit_exceeded` refusal
supplies the effective account limit in its details. Respect retry hints.

Billing is per decision, not per connection, heartbeat, acknowledgement, or
mutation. Several mutations can be covered by one decision. A decision may be
counted even if the connection closes before its event arrives; use the event's
usage and request identifier when reconciling completed work.

## Tool selection

Tool selection: RightNow decision model picks which of your tools to call next, or none, with a probability for each. It does not write arguments or messages, and it does not run tools.

Use `toolQuestions` / `tool_questions` and `selectTool` / `select_tool`. See [Tool selection](../TOOL-SELECTION.md) for examples and results.

Text, PDF and video fit 9 tools plus none, or 10 when required. Image or audio
fits 25 tools plus none, or 26 when required. One question fits 32,768 bytes
and 4,096 tokens. A single required tool is forced locally without a request.
Each selection sent is an ordinary answered question on the current prices.
Tool selection has not been measured. The caller runs the selected tool and
sends its result back in the next request.

## Naming compatibility

Import `RightNow`, `RightNowStreamClient` and `RightNowError` from `rightnow`. Their `SystemZero*` spellings remain identical aliases. An explicit `api_key` wins over `RIGHTNOW_API_KEY`, which wins over the `SYSTEM_ZERO_API_KEY` fallback. The stream client is asynchronous; there is no separate asynchronous HTTP client.

## LongContextRequest opt-in

Select the class for the state size explicitly. In TypeScript:

```ts
await client.decide(request, { dangerZone: 'long_context_64k' });
await client.decide(request, { dangerZone: 'long_context_128k' });
```

In Python:

```python
client.decide(request, danger_zone="long_context_64k")
client.decide(request, danger_zone="long_context_128k")
```

`long_context_64k` covers 38,001 to 65,536 state tokens;
`long_context_128k` covers 65,537 to 131,072. The option sends
`RightNow-Danger-Zone` with that exact value. State size never selects a class by itself.
Send the text in `state` with no attachments. A nonempty attachment list with
either class is refused locally as HTTP 404 `long_context_not_available`.
`LongContextRequest` excludes attachments. For the 64k and 128k classes, at most 32 questions are allowed,
with 2 to 25 choice options or 2 to 4 score levels. The API checks token bounds.

Use the exact class values above. With `accept`, text answers HTTP 400
`invalid_request`, param `RightNow-Danger-Zone`, when more than one native class is served.

With `accept`, PDF answers HTTP 404 `long_context_not_available`; image, audio or
video drops the long grants and runs as an ordinary request.

1M is not available. Requests selecting `long_context_1m` receive HTTP 404
`long_context_not_available` without a charge.

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
`rightnow-danger-zone` values as `dangerZone` in TypeScript and `danger_zone` in Python.
When only `sz-danger-zone` is present, including on 1M answers, the SDK reads that header.
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

```python
from rightnow import RightNow, decisions_for

client = RightNow()  # Reuse this client; close it at shutdown.
reply = client.decide({
    "model": "rightnow-decision", "state": "The shipment arrived.",
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

```python
from rightnow import RightNow

client = RightNow()  # Reuse this client; close it at shutdown.
reply = client.decide({
    "model": "rightnow-decision", "state": "The shipment arrived.",
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

```python
if reply.get("provenance"):
    print(reply["provenance"]["resolved_release"])
```

## Webhooks

Create an endpoint for `decision.completed`, `decision.refused` or `stream.closed`.
`decision.completed` events are sent when the decision's usage is recorded, usually within about a minute of the answer, and can include decisions answered a few minutes before the endpoint was created.
A `decision.completed` event can take up to about six minutes when its usage record is written late.
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

```python
import json
import os
from rightnow import RightNow, verify_webhook_signature

with RightNow(api_key=os.environ["RIGHTNOW_API_KEY"]) as client:
    # POST /v1/decision-model/webhooks
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
