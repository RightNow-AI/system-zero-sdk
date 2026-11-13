# Package moved

The System Zero SDK moved to `rightnow-sdk`, imported as `rightnow`. Install `pip install rightnow-sdk`. This distribution forwards to exactly version 0.5.0 and warns once when `system_zero` is imported. Existing imports keep working.

Clients keep the legacy routes, headers, user agent and error codes.
