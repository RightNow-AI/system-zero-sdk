# System Zero Python SDK

Synchronous server-side Python client for the System Zero API.

The distribution is `rightnow-system-zero`; import it as `system_zero`.

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
including request IDs, rate limits, latency and replay status. Call options
accept `idempotency_key`, `client_request_id`, extra headers and a retry budget.
Client timeouts and retry waits are seconds. Retries are bounded and retain the
serialized request and idempotency key across attempts. Close the client or use
its context manager to release the HTTP transport.

Requests use text or structured JSON state. The attachments field is omitted or `null`.

`run_batch`, `run_flow` and the evaluation helpers compose ordinary decide
calls. They support local call limits, bounded retries, stable request fingerprints
and typed result reports. A local flow call limit can stop a workflow before dispatch.

`system_zero.tools` supplies OpenAI and Anthropic function definitions and the
`ToolHandler`, `create_tool_handler` and `handle_tool_call` helpers. These wrappers
call decide or list models and use the same request validation and API errors.

Keep API keys on trusted servers. See [the security policy](https://github.com/RightNow-AI/system-zero-sdk/blob/main/SECURITY.md) for private
vulnerability reporting.

Tool factories accept a maximum decide-call count. Local admission failures use
`call_limit_reached`; this code is separate from API errors. The tool catalog
includes API and gateway refusals. Its fixed catalog messages are SDK
summaries; HTTP refusal messages are preserved with credential redaction.

The module-level tool helper has a zero decide-call limit. `create_tool_handler`
defaults to 100 decide calls; set `max_decide_calls` to choose the limit for your
tool loop. Model listing does not consume that count.

## Retries and response metadata

Ordinary HTTP calls retry marked 429 capacity_exhausted, rate_limit,
rate_limit_exceeded, concurrency_limit_exceeded and workspace_limit_exceeded;
marked 503 service_unavailable and system_zero_unavailable; and marked 409
idempotency_conflict. Marked means error.retryable=true or x-should-retry=true.
An explicit false vetoes retry. Each retry keeps the same key and request body.

Defaults remain five retries, 150 seconds per attempt, a 60-second retry budget
and a 60-second maximum wait. Longer waits surface the last error.
Retry-After-Ms takes precedence; Retry-After is used only when milliseconds are
absent. Full jitter remains a minimum wait. A retry requires at least one
millisecond remaining, or the previous error is raised. Explicit blank keys
disable retries and omit the header; blank client request IDs omit the header.
Errors and response metadata expose idempotencyKey / idempotency_key.
Rate-limit and overloaded errors expose retryAfterMs / retry_after_ms.
Every attempt sends X-SDK-Retry-Count and a User-Agent with SDK name, version
and runtime version only.
