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

Requests use text or structured JSON state. The attachments field is omitted or `null`.

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

`system_zero.tools` supplies OpenAI and Anthropic function definitions and the
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
