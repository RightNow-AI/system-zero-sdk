## LongContextRequest opt-in

The SDK publishes the `LongContextRequest` type as an opt-in surface. It is unavailable until the API enables it for the workspace; workspace opt-in through the console's Settings is also required. Once available, give explicit per-request consent with `dangerZone: 'accept'` in TypeScript or `danger_zone="accept"` in Python. The option sends exactly `SZ-Danger-Zone: accept`; it is never sent by default or inferred from state size. Until availability, the API ignores or refuses this option. Do not supply this header through custom headers. Retries retain the option. The same explicit option is supported by batch and flow helpers.

Until workspace enablement, the expected result is HTTP 404 `long_context_not_available`, surfaced as `LongContextNotAvailableError`. The same refusal applies without workspace opt-in. When the API supplies `details.eligibility`, `LongContextNotAvailableError.eligibility` exposes the rule: `billing`, `opt_in`, `class`, `deny_list`, `global_switch` or `account_deleted`, with a string fallback for future rules. A generic held-route refusal can omit the rule. Status, code, message and request ID remain intact. Held routes are never retried.

`LongContextRequest`, `EnabledDecideRequest`, `OrdinaryRequest`, `MediaRequest` and their question types describe the separate enabled request contract in `enabled.openapi.json`. `LongContextLimits` describes its discovery limits. `openapi.json` remains the served contract. The API selects the request band and checks bounds: a `LongContextRequest` takes at most 32 questions, choices with 2 to 25 options, and scores with 2 to 4 levels. Only inline document attachments fit its document branch. The API validates document admission separately.

```typescript
await client.decide(request, { dangerZone: 'accept' });
```

```python
client.decide(request, danger_zone="accept")
```
