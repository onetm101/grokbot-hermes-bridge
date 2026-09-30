"""Hermes — a safe, dedicated MCP Streamable HTTP gateway.

Exposes four tools backed by the live local Hermes agent: short sync
``hermes_ask`` and ``hermes_status``, plus ``hermes_ask_async`` /
``hermes_job_status`` for long Mac/browser work that must not sit inside one
MCP call. The adapter (:mod:`hermes_gateway.agent`) is strictly bounded and
async-cancellable (on timeout or cancellation the child process group is
killed and reaped), wrapped in a hardened HTTP layer
(:mod:`hermes_gateway.mcp.gateway`). No shell/exec, no SSH and no generic Hermes API
are reachable through the gateway; it fails closed when the local Hermes
binary is missing or not executable.
"""

__version__ = "0.3.0"
