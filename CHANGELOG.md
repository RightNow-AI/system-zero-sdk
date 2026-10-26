# Changelog

## 0.4.0

Batch receipts and flow traces retain well-formed server error codes, including new codes. Batch resume accepts these error records. Corrected the TypeScript video documentation.

Added a typed, opt-in long-context surface for use only after API availability for the workspace and workspace opt-in in console Settings. Before availability, expect HTTP 404 long_context_not_available as LongContextNotAvailableError, including eligibility when supplied. Once available, dangerZone in TypeScript and danger_zone in Python provide explicit per-request consent; neither is sent by default. Until availability, the API ignores or refuses that option.

PDF and video are supported through ordinary inline attachments. Video request types, attachment types and helpers are exported. Refusal responses keep their typed VideoNotAvailableError and LongContextNotAvailableError; the latter exposes details.eligibility as eligibility, including future rule strings. Existing status, code, message and request IDs are preserved.

## 0.3.0

Breaking: the three function-tool lists were renamed by their JSON shape: NESTED_FUNCTION_TOOLS (nested function objects), FLAT_FUNCTION_TOOLS (flat function objects), INPUT_SCHEMA_TOOLS (input_schema objects). Update imports; the payloads are unchanged.

Function tool lists are named by format: NESTED_FUNCTION_TOOLS, FLAT_FUNCTION_TOOLS and INPUT_SCHEMA_TOOLS. Public wording describes function formats and stable availability. Both packages include the authenticated stream v1 client with state replay and decision events. Python package metadata uses the production/stable classifier. Release checks refuse prohibited wording in exported declarations and prose.

## 0.2.0

Images and 16,000 Hz integer PCM or IEEE float WAV audio attachments on decide, including function tools and the MCP adapter. Attachments remain optional and null remains valid. Static PNG and JPEG only; at most 4 images and 1 audio, 2,621,440 decoded bytes total and 3,665,920 bytes for the entire request. Images are limited to 16,777,216 pixels and 50:1 aspect ratio. Audio allows 1-64 channels and at most 300 seconds; the byte limit can be reached earlier. GIF, WebP, animated PNG, non-PDF document formats, other audio formats/rates and upload references remain refused. PDF and video support is described in 0.4.0 above. Explicit media helpers use the API refusal codes. The 71-code catalog and ordinary HTTP retry and call-count behavior are unchanged.

## 0.1.1

Exact retry and call-count rules, installation commands and runtime requirements, and the CHANGELOG link; the error catalogs now list every refusal code the API can return on these routes. Request, response and retry behavior are unchanged.

## 0.1.0

Text decisions over HTTP, typed responses, retries, workflows, batch evaluation and function tools. The npm package includes an MCP server adapter.
