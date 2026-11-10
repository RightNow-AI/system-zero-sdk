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
`questions_too_large`. Enable access per workspace in the Console: add funds
with a saved card, then open Settings, Workspace, Context access. Before access,
HTTP 404 `long_context_not_available` is uncharged; `details.eligibility` names
the first unmet step and may contain new strings.

The maximum charge is held before work and settled to exact usage. Insufficient
credit returns HTTP 402 `insufficient_credits` before work. A daily class limit
returns HTTP 429 `long_context_daily_budget_exhausted` until the next UTC day;
neither error is retried automatically. A cold pool returns HTTP 503
`service_unavailable` before any hold or charge; the SDK retries within its
normal budget using the same key. One reply runs at a time per pool; a concurrent
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
