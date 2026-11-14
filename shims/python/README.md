# Package moved

The System Zero SDK moved to `rightnow-sdk`, imported as `rightnow`. Install `pip install rightnow-sdk`. This distribution forwards to exactly version 0.5.1 and warns once when `system_zero` is imported. Existing imports keep working.

Clients keep the legacy routes, headers, user agent and error codes; with a key from the API keys page (it starts with `rp_`), `decide` calls `POST /v1/decisions`.
