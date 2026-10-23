# System Zero Python SDK

Synchronous server-side Python client for the System Zero API.

Requires Python 3.10 or later.

Install with `pip install rightnow-system-zero`; import it as `system_zero`.

Set `SYSTEM_ZERO_API_KEY` in the server environment, then call the API:

```python
from system_zero import SystemZero

with SystemZero() as client:
    reply = client.decide({
        "model": "system-zero",
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

The client calls `POST /system-zero/decide` and authenticated `GET /models`.
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
Errors of authentication and permission types never retry. Python also refuses retry for
codes containing `spend`, `budget`, `auth` or `api_key`.

`Retry-After-Ms` takes precedence; only when it is absent does `Retry-After`
provide seconds or an HTTP date. The wait is never below the full-jitter floor
`min(8 seconds, 0.5 seconds * 2^attempt) * random`, with attempt zero for the first
retry and random drawn from zero to one. A malformed millisecond header does not
fall back to the seconds header.

## Workflow recovery and call limits

`run_batch` and `run_flow` use ordinary `POST /system-zero/decide` calls with a stricter
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

`system_zero.tools` supplies function tool definitions and the
`ToolHandler`, `create_tool_handler` and `handle_tool_call` helpers.
They share the API request
validation and error vocabulary. Tool catalog messages are SDK summaries;
HTTP refusal messages are preserved with credential redaction.

## Response metadata

SDK errors and response metadata expose the request's idempotency key as
`idempotency_key` (`None` when no key was sent). `RateLimitError` and
`OverloadedError` expose the response's retry delay as `retry_after_ms` in
milliseconds, or `None` when no valid retry delay is available.

Every HTTP attempt sends `X-SDK-Retry-Count`, starting at `0`, and a
`User-Agent` containing only the SDK name, SDK version and runtime version.
A blank or whitespace-only `client_request_id` omits the
`X-Client-Request-Id` header.

Keep API keys on trusted servers. See [the security policy](https://github.com/RightNow-AI/system-zero-sdk/blob/main/SECURITY.md)
for private vulnerability reporting.

## Function tool formats

Breaking: the three function-tool lists were renamed by their JSON shape: NESTED_FUNCTION_TOOLS (nested function objects), FLAT_FUNCTION_TOOLS (flat function objects), INPUT_SCHEMA_TOOLS (input_schema objects). Update imports; the payloads are unchanged.

The three lists describe the same functions. Choose the shape your tool runner accepts:

- `NESTED_FUNCTION_TOOLS`: `{"type":"function","function":{"name":"...","description":"...","parameters":{},"strict":true}}`.
- `FLAT_FUNCTION_TOOLS`: `{"type":"function","name":"...","description":"...","parameters":{},"strict":true}`.
- `INPUT_SCHEMA_TOOLS`: `{"name":"...","description":"...","input_schema":{}}`.

Each list owns its schema objects. Pass the function name and its JSON arguments to the tool handler.

## Streaming decisions

Supply `SYSTEM_ZERO_API_KEY` in the server environment before running this example.

The package root and `system_zero.stream` export `SystemZeroStreamClient`;
`system_zero.stream_interfaces` supplies its frame types. The installed runtime
dependency is `websockets>=15.0.1,<16`. Compression, automatic proxy selection,
and automatic transport pings are disabled; the client manages the gateway's
heartbeat and reconnect contract explicitly.

```python
import asyncio
from system_zero import SystemZeroStreamClient

async def main():
    async with SystemZeroStreamClient() as stream:
        events = stream.events()
        await stream.connect()
        await stream.start({
            "model": "system-zero", "state": "Order received.",
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
handshake and selects only `system-zero.stream.v1`. Keep the key on a trusted
server. The connection uses `wss://api.rightnowai.co/v1/system-zero/stream`.

Start with state and questions, then append text, append JSON items, append
attachments, or replace the state. A mutation resolves when its acknowledgement
arrives. In `on_change` mode, accepted mutations schedule decisions; in
`on_request` mode, call `decide` to schedule one explicitly and read its decision
event. One decision can cover several accepted mutations.

Reconnect restores the latest accepted state using stable idempotency keys;
it never resends old wire frames. The default `latest` policy resumes the selected
decision mode. The `never` policy restores state in `on_request` mode, leaving
the next decision to the caller. Neither policy retries authentication or protocol
failures. Idle closure reopens lazily when new work arrives. Listen for
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
