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
`SZ-Danger-Zone` with that exact value. State size never selects a class by itself.
Send the text in `state` with no attachments. A nonempty attachment list with
either class is refused locally as HTTP 404 `long_context_not_available`.
`LongContextRequest` excludes attachments. At most 32 questions are allowed,
with 2 to 25 choice options or 2 to 4 score levels. The API checks token bounds.

Use the exact class values above. With `accept`, text answers HTTP 400
`invalid_request`, param `SZ-Danger-Zone`, when more than one native class is served.

With `accept`, PDF answers HTTP 404 `long_context_not_available`; image, audio or
video drops the long grants and runs as an ordinary request.
The 1M class is not yet supported by this SDK version. No selector is sent by default. Do not supply the header
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
