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
FastMCP Streamable HTTP
  ├─ hermes_ask          sync, short (<~30s), waits for the answer
  ├─ hermes_status       filtered `hermes status`
  ├─ hermes_ask_async    enqueue, return immediately (<~5s)
  └─ hermes_job_status   read one job file, return immediately
  │
  ▼
HermesAgent.ask_async
  ├─ warm worker (default) ── long-lived Hermes Python child, plugins/MCP
  │     preloaded once; each ask is one blocking JSON line on a Unix socket
  └─ oneshot fallback ── hermes [--safe-mode] --oneshot=<prompt>
  │
  ▼
local Hermes CLI / runtime and its existing configuration

Async jobs only:
  hermes_ask_async writes $HERMES_HOME/run/jobs/<job_id>.json
  one in-process runner calls the same ask path with a longer timeout
  hermes_job_status reads that file
```

The plugin files contain only the public MCP URL and skill instructions. OAuth
client registrations are created by the client and stored on the server. The
owner code and Hermes paths live only in the gateway process environment.

## Ask path (sync)

Cold `hermes --oneshot` with full plugins/MCP often exceeds Grok Bot's ~60s
MCP client timeout (`-32001`). There is no official Hermes "persistent oneshot"
CLI, so the bridge owns a small warm worker (`hermes_gateway/warm_worker.py`)
started with the same Python interpreter as the local `hermes` launcher:

1. Bridge spawns the worker once (or reuses it) without `--safe-mode`.
2. Worker preloads plugins + MCP discovery, then listens on a Unix socket
   (default `$HERMES_HOME/run/grokbot-hermes-worker.sock`).
3. Each `hermes_ask` sends one JSON line and blocks until a bounded answer
   comes back, with the same `HermesResult` shape as before.
4. If the worker is down, `HERMES_BRIDGE_ASK_MODE=auto` falls back to oneshot
   (optionally `--safe-mode` via `HERMES_BRIDGE_ONESHOT_SAFE_MODE`, default on).

The worker protocol is synchronous request/response. Asks are serialized
(`client_lock`) because Hermes agent construction is not assumed to be
thread-safe. There is no non-blocking enqueue opcode on the socket.

`hermes_ask` is the short path only. It keeps the sync timeout (default 55s,
max 180s) so a healthy short ask fits under the ~60s client. Do not use it
for Mac/browser work that may run longer than about 30 seconds, and do not
retry it hoping `-32001` will clear.

Env knobs: `HERMES_BRIDGE_ASK_MODE=auto|worker|oneshot`,
`HERMES_BRIDGE_WORKER_SOCKET`, `HERMES_BRIDGE_WORKER_START_TIMEOUT_SECONDS`,
`HERMES_BRIDGE_ONESHOT_SAFE_MODE`, `HERMES_BRIDGE_ASK_TIMEOUT_SECONDS`.

`hermes_status` still calls only `hermes status` and removes lines that look
like credentials, URLs, handles, or filesystem details.

## Ask path (async jobs)

Long Chrome / Costco-style work stays on the same Hermes ask path, but the
MCP call must not wait for it.

1. `hermes_ask_async` validates the question, writes a `queued` job file, and
   returns `{ok, job_id, status, result_path}` immediately.
2. One gateway process runs a single background job at a time. Further jobs
   stay `queued` (serial on purpose). The runner calls `ask_async` with
   `HERMES_BRIDGE_JOB_TIMEOUT_SECONDS` (default 1800, clamp 30–7200). That
   budget is not added to the HTTP request timeout.
3. `hermes_job_status` reads the job file and returns immediately. When
   `status` is `done`, `answer` uses the same bound as `hermes_ask` (max 8000
   characters). Poll every 15–30 seconds.
4. A sync `hermes_ask` issued while a long job holds the warm worker waits
   behind that job and can still hit the client timeout. Do not mix them.

Job files live in `$HERMES_HOME/run/jobs/` (directory mode `0700`, file mode
`0600`). Each file stores the id, status, a short redacted question summary,
timestamps, and the answer or error code when finished. The raw question and
the context are not written. Passwords, tokens, and SMS bodies are stripped
from the summary and from the stored answer. Files older than 24 hours are
deleted. After a gateway restart, queued or running files from the previous
process are marked `failed` / `interrupted` because the in-memory queue is
gone. Reusing a `job_id` returns the existing record; a retry needs a new id.

`HERMES_BRIDGE_JOB_TIMEOUT_SECONDS` does not change `hermes_ask`.

## Deliberate limits

- four narrow tools; no generic command runner
- one long job at a time, in a single gateway process
- single-owner authorization model
- localhost bind; TLS is terminated outside Python
- no deployment automation for a particular hosting provider
- Grok Bot's visible pre-tool message remains visible
