# System Zero TypeScript SDK

Server-side TypeScript client for the System Zero API.

Requires Node 22 or newer and ES modules (ESM).

The npm package is `@rightnow/system-zero`.

Set `SYSTEM_ZERO_API_KEY` in the server environment, then call the API:

```ts
import { SystemZeroClient } from '@rightnow/system-zero';

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
request IDs, rate limits, latency and replay status. Request options accept
`idempotencyKey`, `clientRequestId`, extra headers and a retry budget.
Client timeouts and retry waits are milliseconds. Retries are bounded and retain
the serialized request and idempotency key across attempts.

Requests use text or structured JSON state. The attachments field is omitted or `null`.

`runBatch`, `runFlow` and the evaluation helpers compose ordinary decide calls.
They support local call limits, bounded retries, stable request fingerprints and
typed result reports. A local flow call limit can stop a workflow before dispatch.

Function tools are exported from
`@rightnow/system-zero/integrations/function-tools`; the MCP adapter is exported
from `@rightnow/system-zero/integrations/mcp-server`. Both wrap the two API calls
above and share their request validation and error vocabulary.

Keep API keys on trusted servers. See [the security policy](https://github.com/RightNow-AI/system-zero-sdk/blob/main/SECURITY.md) for private
vulnerability reporting.

Tool factories accept a maximum decide-call count. Local admission failures use
`call_limit_reached`; this code is separate from API errors. The tool catalog
includes API and gateway refusals. Its fixed catalog messages are SDK
summaries; HTTP refusal messages are preserved with credential redaction.

The module-level tool helper has a zero decide-call limit. `createToolHandler`
defaults to 100 decide calls; set `maxDecideCalls` to choose the limit for your
tool loop. Model listing does not consume that count.

The MCP adapter permits two concurrent tool calls. Additional concurrent calls
receive a local `call_limit_reached` result without dispatch.
