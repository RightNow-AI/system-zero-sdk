# Tool selection

Tool selection: RightNow System Zero picks which of your tools to call next, or none, with a probability for each. It does not write arguments or messages, and it does not run tools.

`toolQuestions` / `tool_questions` builds one ordinary choice question named
`sz_tool`. Pass tools in the order you want, with a name and optional description.
Nested and flat function definitions are also accepted; only their names and
descriptions are read. Names must match `^[A-Za-z0-9_-]{1,64}$`, be unique, and
must not be `none` or all digits. Descriptions have at most 1,024 characters.
The default mode is `auto`, which adds `none` last. `required` omits `none`.

In TypeScript:

```ts
import { selectTool, toolQuestions } from '@rightnowai/system-zero';

const tools = [
  { name: 'lookup_order', description: 'Read order status.' },
  { name: 'issue_refund', description: 'Refund an order.' },
];
const questions = toolQuestions(tools, 'auto');
const selection = await selectTool(client,
  { model: 'system-zero', state: 'The order arrived damaged.' },
  tools, 'auto', { idempotencyKey: 'order-selection-1' });
```

In Python:

```python
from system_zero import select_tool, tool_questions

tools = [
    {"name": "lookup_order", "description": "Read order status."},
    {"name": "issue_refund", "description": "Refund an order."},
]
questions = tool_questions(tools, "auto")
selection = select_tool(client,
    {"model": "system-zero", "state": "The order arrived damaged."},
    tools, "auto", idempotency_key="order-selection-1")
```

The helper passes your request and decide options through the existing client,
including attachments, the idempotency key, retry budget and explicit context
class. Existing request validation and class limits still apply. Its result has
`decision` (`call` or `none`), `tool` (the name or null), `confidence`,
`alternatives` (tool names and confidence), and the untouched `reply`.
An alternative named `none` keeps that name. Required with exactly one tool
returns it with `forced: true`, null confidence and reply, and no request.
Other results have `forced: false`. Legacy replies retain null confidence.

If no selection is available, the helper raises `APIError` with
“No decision, possibly refused.” A single-question refusal can currently arrive
as a retryable HTTP 503. The helper lets the client's normal retry budget finish
and adds no retry loop; that outcome does not confirm a model refusal.

On text, PDF and video, use at most 9 tools plus `none` in auto mode, or 10 tools
when required. With an image or audio, use at most 25 tools plus `none`, or 26
when required. At least one tool is needed; a single required tool is forced
locally. One question must fit 32,768 bytes and 4,096 tokens. The API checks the
served option and size limits. Filter a larger tool list before selection.

Each selection sent to the API is an ordinary answered question on the current
prices. Tool selection has not been measured. The caller runs the selected tool
and sends its result back in the state of the next request. Each request carries
the context it needs.
