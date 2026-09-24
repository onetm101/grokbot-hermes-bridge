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
HermesAgent
  ├─ warm worker (default) ── long-lived Hermes Python child, plugins/MCP
  │     preloaded once; each ask is JSON-line IPC on a Unix socket
  └─ oneshot fallback ── hermes [--safe-mode] --oneshot=<prompt>
  │
  ▼
local Hermes CLI / runtime and its existing configuration
```

The plugin files contain only the public MCP URL and skill instructions. OAuth
client registrations are created by the client and stored on the server. The
owner code and Hermes paths live only in the gateway process environment.

## Ask path (warm worker)

Cold `hermes --oneshot` with full plugins/MCP often exceeds Grok Bot's ~60s
MCP client timeout (`-32001`). There is no official Hermes "persistent oneshot"
CLI, so the bridge owns a small warm worker (`hermes_gateway/warm_worker.py`)
started with the same Python interpreter as the local `hermes` launcher:

1. Bridge spawns the worker once (or reuses it) without `--safe-mode`.
2. Worker preloads plugins + MCP discovery, then listens on a Unix socket
   (default `$HERMES_HOME/run/grokbot-hermes-worker.sock`).
3. Each `hermes_ask` sends one JSON line and receives a bounded answer with
   the same `HermesResult` shape as before.
4. If the worker is down, `HERMES_BRIDGE_ASK_MODE=auto` falls back to oneshot
   (optionally `--safe-mode` via `HERMES_BRIDGE_ONESHOT_SAFE_MODE`, default on).

Env knobs: `HERMES_BRIDGE_ASK_MODE=auto|worker|oneshot`,
`HERMES_BRIDGE_WORKER_SOCKET`, `HERMES_BRIDGE_WORKER_START_TIMEOUT_SECONDS`,
`HERMES_BRIDGE_ONESHOT_SAFE_MODE`, `HERMES_BRIDGE_ASK_TIMEOUT_SECONDS`
(default 55s so the ask fits under a ~60s client).

`hermes_status` still calls only `hermes status` and removes lines that look
like credentials, URLs, handles, or filesystem details.

## Deliberate limits

- two tools only; no generic command runner
- single-owner authorization model
- localhost bind; TLS is terminated outside Python
- no deployment automation for a particular hosting provider
- Grok Bot's visible pre-tool message remains visible
