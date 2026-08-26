# Architecture

```text
Grok Bot / MCP client
  │  HTTPS + OAuth 2.1 discovery, DCR and PKCE
  ▼
reverse proxy or tunnel
  │  forwards to localhost only
  ▼
HardeningMiddleware ── health, host/origin, auth, size, rate, concurrency
  │
  ▼
FastMCP Streamable HTTP ── hermes_ask / hermes_status only
  │
  ▼
HermesAgent ── fixed argv, minimal env, timeout + kill/reap, bounded output
  │
  ▼
local Hermes CLI and its existing configuration
```

The plugin files contain only the public MCP URL and skill instructions. OAuth
client registrations are created by the client and stored on the server. The
owner code and Hermes paths live only in the gateway process environment.

`hermes_ask` passes one bounded `--oneshot=<prompt>` argument to Hermes.
`hermes_status` calls only `hermes status` and removes lines that look like
credentials, URLs, handles, or filesystem details.

## Deliberate limits

- two tools only; no generic command runner
- single-owner authorization model
- localhost bind; TLS is terminated outside Python
- no deployment automation for a particular hosting provider
- Grok Bot's visible pre-tool message remains visible
