# GrokBot ↔ Hermes Bridge

A self-hosted MCP gateway and Agent Plugin template that lets Grok Bot talk to
your existing Hermes Agent. Grok Bot remains the chat interface; Hermes runs
on your machine and produces the answer.

The repository is operator-agnostic: it contains no live endpoint, token,
hostname, private address, user path, account handle, or conversation.

![GrokBot to Hermes bridge overview](assets/bridge-overview.jpg)

## Quick start

Linux or macOS, in one auditable command:

```bash
git clone https://github.com/iamsupersocks/grokbot-hermes-bridge.git && cd grokbot-hermes-bridge && ./scripts/install.sh
```

Already cloned:

```bash
./scripts/install.sh
python3 scripts/doctor.py
```

The installer is local and auditable. It does not download a remote shell,
does not use `sudo`, does not start the gateway, and does not write a real
public hostname unless you pass one. Non-interactive and dry-run modes are
available for review and tests:

```bash
./scripts/install.sh --dry-run --non-interactive
./scripts/install.sh --non-interactive --endpoint https://mcp.example.com/mcp
```

Then fill the remaining local paths in `.env.local` (mode 600, never commit
it). The owner code is generated on disk; do not paste it into chat. See
[`docs/TUTORIAL.md`](docs/TUTORIAL.md) for the illustrated walkthrough.
Doctor deliberately stays red while the example hostname or Hermes paths are
still placeholders.

## How it works

1. The gateway runs beside your Hermes installation and exposes exactly two
   MCP tools: `hermes_ask` and `hermes_status`.
2. An HTTPS reverse proxy or tunnel makes `/mcp` reachable from Grok Bot.
3. Grok Bot discovers the gateway's OAuth flow. You approve the connection in
   a browser with a private owner code stored only in the server environment.
4. The plugin sends user requests to Hermes and returns the bounded reply.
5. By default `hermes_ask` talks to a **bridge-owned warm Hermes worker**
   (plugins loaded once) so answers fit under Grok Bot's ~60s MCP timeout.
   If the worker is unavailable, it falls back to `hermes --oneshot`
   (optionally `--safe-mode`). See `ARCHITECTURE.md`.

There is no SSH endpoint, generic shell tool, environment dump, or committed
credential. The owner code is not accepted as an MCP bearer token.

## Requirements

- Python 3.11+
- A working `hermes` CLI and Hermes home directory on the gateway machine
- A public HTTPS hostname or tunnel that forwards to `127.0.0.1:8099`
- Grok Bot, Codex, Cursor, or another Streamable HTTP MCP client with OAuth

## Run the gateway

After the installer (or the equivalent local `venv` + `.env.local` setup):

```bash
. .venv/bin/activate
set -a
. ./.env.local
set +a
python -m hermes_gateway.mcp --host 127.0.0.1 --port 8099
```

At minimum `.env.local` must contain:

```bash
HERMES_BRIDGE_SECRET=<64-random-hex-characters>
HERMES_BRIDGE_PUBLIC_BASE_URL=https://mcp.example.com
HERMES_BRIDGE_ALLOWED_HOSTS=localhost,127.0.0.1,mcp.example.com
HERMES_BRIDGE_HERMES_BIN=/absolute/path/to/hermes
HERMES_BRIDGE_HERMES_HOME=/absolute/path/to/hermes-home
# Optional ask-path knobs (defaults shown):
# HERMES_BRIDGE_ASK_MODE=auto
# HERMES_BRIDGE_ASK_TIMEOUT_SECONDS=55
# HERMES_BRIDGE_ONESHOT_SAFE_MODE=1
# HERMES_BRIDGE_WORKER_SOCKET=/absolute/path/to/worker.sock
```

`GET /health` should return `{"status":"ok"}`. Put HTTPS in front of the
service; do not expose port 8099 directly. See `deploy/` for generic examples.

## Configure the plugin

The installer calls `scripts/configure_plugin.py` for you. To repeat it:

```bash
python scripts/configure_plugin.py https://mcp.example.com/mcp
```

The generated config contains only the URL. Do **not** add an `Authorization`
header: Grok Bot uses OAuth discovery and PKCE to obtain its own access token.

Install or package this folder as a plugin, restart Grok Bot, then enable the
connector. The first connection opens the approval page. Enter the same owner
code stored in `HERMES_BRIDGE_SECRET`.

## Add Composio beside this bridge

[Corey Ganim's Grok Bot tip](https://x.com/coreyganim/status/2092559429275447742)
uses [Composio Connect](https://docs.composio.dev/docs/composio-connect) to go
beyond a client's native connector catalog. Composio describes Connect as one
hosted MCP endpoint for 1000+ apps through 7 meta-tools. It is not literally
every MCP server, and it is not bundled with this repository.

Keep the two connections separate:

| Client | This repository | Optional Composio connection |
| --- | --- | --- |
| Grok Bot | Install the Grok plugin and connect `hermes-bridge` | Add Composio Connect as a second MCP connector |
| Cursor | Install the Cursor plugin or add this MCP URL | Add Composio Connect as a second MCP server |
| Codex | Install the Codex plugin or add this MCP URL | Prefer Composio's native Codex plugin, or choose Connect MCP explicitly |

The `.grok-plugin`, `.cursor-plugin`, and `.codex-plugin` manifests here install
only the Hermes bridge. Composio traffic does not pass through Hermes and
Composio does not replace this bridge. See the
[tutorial](docs/TUTORIAL.md#optional-add-composio-as-a-second-connector) for
the complete setup model and safety boundaries.

## Grok Bot's visible-message limitation

Grok Bot currently emits a short visible message before every tool call, such
as “I’ll pass that to Hermes.” The plugin cannot hide or remove that host-level
step. The bundled skill keeps it brief and prevents Grok Bot from impersonating
Hermes.

## Security model

- fixed Hermes executable; no shell invocation
- bounded prompt, output, turns, timeout, payload, rate, and concurrency
- minimal child-process environment
- OAuth discovery, dynamic client registration, authorization code + PKCE,
  signed access tokens, and refresh tokens
- owner approval required; owner code never works as a bearer token
- host/origin checks, DNS-rebinding protection, localhost-only bind, log
  redaction, and filtered status output
- fail-closed startup if the endpoint, owner code, Hermes binary, or Hermes
  home is invalid

The OAuth provider is intentionally single-owner. Registered clients persist
locally in a mode-0600 JSON file; pending approvals and authorization codes are
in memory. Rotating `HERMES_BRIDGE_SECRET` invalidates issued tokens.

## Test and audit

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
python src/privacy_scan.py --root .
python scripts/audit_git_history.py
python3 scripts/doctor.py
```

Also read `SECURITY.md` before exposing the endpoint. This repository started
with a fresh public Git history; it does not inherit the private implementation
history.

## License

MIT. See `LICENSE`.
