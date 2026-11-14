# Package moved

The System Zero SDK moved to `@rightnowai/rightnow`. Install `npm install @rightnowai/rightnow`. This package forwards every export to exactly version 0.5.1. Existing imports keep working.

Clients keep the legacy routes, headers, user agent and error codes; with a key from the API keys page (it starts with `rp_`), `decide` calls `POST /v1/decisions`.
