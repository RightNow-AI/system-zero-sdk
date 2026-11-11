# Changelog

## 0.4.7

Decision provenance: the decide reply's optional `provenance` member is typed. Check for it before reading it.

Webhooks: create, list, delete and rotate endpoints, read their deliveries, and verify a delivery with `verifyWebhookSignature` / `verify_webhook_signature`.

Both SDKs now gzip HTTP decide request bodies of 32 KB or more when compression saves at least 10 percent; a client option (`compressRequests` / `compress_requests`) turns it off. An injected fetch receives a Buffer body for a compressed request.

The request policy text states that an abstained answer is charged exactly like any answered question.

## 0.4.6

Request policy supports min_confidence with abstained answers and max_cost_usd.

Decision records let you read a decision and its content-free trace by id.

The 1M class needs nothing switched on in the Console; the request option is still required.

The first call after an idle period can take several minutes to become available: the answer names the wait in `Retry-After`; send the same request again with the same Idempotency-Key until it answers.

## 0.4.5

Audio is served like every other attachment kind; the on-demand note is removed.

To access the largest context class, make one completed payment, then open Settings, Workspace, Context access.

The first 1M call after an idle period can take several minutes to become available: the answer names the wait in `Retry-After`; keep retrying for up to ten minutes.

## 0.4.4

Select which tool to call next with toolQuestions / tool_questions and selectTool / select_tool. The helpers return the choice and probabilities through ordinary decide; the caller runs the tool. Tool selection has not been measured.

Select the 1M class explicitly with long_context_1m. Both SDKs keep its idempotency key across retries, generate a key even without retries, and check its request shape before sending. Workspace access is enabled in the Console.

Legacy answers whose probabilities sum exactly at the tolerance edge are now read the same way by both SDKs and by every supported Python version.

Reported and fixed by an outside contributor in pull request 5 of the SDK repository.

## 0.4.3

Model listing now types optional `limits.long_context` entries with `header_value`, `min_state_tokens`, `state_tokens`, `rendered_tokens` and `attachments`; ordinary token limits describe the direct window. A cold audio pool can return HTTP 503 `system_zero_unavailable` with `Retry-After` up to 240 seconds. The SDK preserves the delay and idempotency key; a wait beyond the retry budget raises immediately. Wait for that delay and retry the same request and key, or explicitly raise both the retry budget and maximum wait to allow automatic recovery.

Explicit class selection uses dangerZone / danger_zone with long_context_64k or long_context_128k. Audio is one WAV at 16,000 Hz, alone, at most 60 seconds. Every HTTP error exposes retryAfterMs / retry_after_ms; over-budget waits raise immediately. Retained stream tokens are redeemed before state restoration.

Calls after a pause can reuse an open connection instead of paying for a new connection: Python shares a process-wide pool and keeps idle HTTP connections for up to 240 seconds, and TypeScript uses its own keep-alive agents for up to 240 seconds, subject to the server closing sooner. Create one client per process and reuse it.

Updated media and long-context guidance to follow `models.list()` for availability. PDF and MP4 use ordinary inline attachments; SDK encoding support does not enable a held API class. Function-tool and MCP attachment validation now follows the published request schema, including PDF and MP4.

## 0.4.2

0.4.1 was tagged but never published to PyPI or npm. This release includes its key-hygiene fixes and corrects stream cleanup on Python 3.10 and 3.11 when a receipt notification races with cancellation.

Python streams redact WebSocket handshake headers before DEBUG logging. Python clients conceal stored API keys in vars(client) and repr; pickling a client is refused without exposing the key. Stream clients redact echoed credentials in retained failures, including pending SDK tasks waiting on native transport failures.

Node native HTTP debug channels are refused when they could print credentials. SDK error diagnostics and function-tool refusal payloads redact echoed credentials. Unsafe native attachments and hooks may be omitted.

## 0.4.1

Python streams redact WebSocket handshake headers before DEBUG logging. Python clients conceal stored API keys in vars(client) and repr; pickling a client is refused without exposing the key. Stream clients redact echoed credentials in retained failures, including pending SDK tasks waiting on native transport failures.

Node native HTTP debug channels are refused when they could print credentials. SDK error diagnostics redact echoed credentials. Unsafe native attachments and hooks may be omitted.

## 0.4.0

Batch receipts and flow traces retain well-formed server error codes, including new codes. Batch resume accepts these error records. Corrected the TypeScript video documentation.

Added typed long-context requests and per-request consent through dangerZone in TypeScript and danger_zone in Python. Check models.list() and the documented API contract for available classes and workspace opt-in. The API serves 64k and 128k only with its explicit class selector; explicit class selection is not yet supported by this SDK version. The 1M class is closed. A held or ineligible class returns LongContextNotAvailableError, including eligibility when supplied.

PDF and video are supported through ordinary inline attachments. Video request types, attachment types and helpers are exported. Refusal responses keep their typed VideoNotAvailableError and LongContextNotAvailableError; the latter exposes details.eligibility as eligibility, including future rule strings. Existing status, code, message and request IDs are preserved.

## 0.3.0

Breaking: the three function-tool lists were renamed by their JSON shape: NESTED_FUNCTION_TOOLS (nested function objects), FLAT_FUNCTION_TOOLS (flat function objects), INPUT_SCHEMA_TOOLS (input_schema objects). Update imports; the payloads are unchanged.

Function tool lists are named by format: NESTED_FUNCTION_TOOLS, FLAT_FUNCTION_TOOLS and INPUT_SCHEMA_TOOLS. Public wording describes function formats and stable availability. Both packages include the authenticated stream v1 client with state replay and decision events. Python package metadata uses the production/stable classifier. Release checks refuse prohibited wording in exported declarations and prose.

## 0.2.0

Added SDK encoding for images and 16,000 Hz integer PCM or IEEE float WAV audio attachments, including function tools and the MCP adapter. Encoding does not establish API availability; check models.list() before sending a kind. Attachments remain optional and null remains valid. Static PNG and JPEG only; at most 4 images and 1 audio, 2,621,440 decoded bytes total and 3,665,920 bytes for the entire request. Images are limited to 16,777,216 pixels and 50:1 aspect ratio. Read models.list() for audio availability. Inline audio/wav uses integer PCM or IEEE float at 16,000 Hz, mono or multichannel, with one audio per request and a decoded-duration limit of 60 seconds inclusive (60,000 ms), one pass per request. Audio answers currently report calibrated: false; the byte limit can be reached earlier. GIF, WebP, animated PNG, non-PDF document formats, other audio formats/rates and upload references are not yet supported by this SDK version. PDF and video support is described in 0.4.0 above. Explicit media helpers use the API refusal codes. The 71-code catalog and ordinary HTTP retry and call-count behavior are unchanged.

## 0.1.1

Exact retry and call-count rules, installation commands and runtime requirements, and the CHANGELOG link; the error catalogs now list every refusal code the API can return on these routes. Request, response and retry behavior are unchanged.

## 0.1.0

Text decisions over HTTP, typed responses, retries, workflows, batch evaluation and function tools. The npm package includes an MCP server adapter.
