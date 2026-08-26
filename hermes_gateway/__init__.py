"""Hermes — a safe, dedicated MCP Streamable HTTP gateway.

Exposes exactly two tools (``hermes_ask``, ``hermes_status``) backed by the
live local Hermes agent through a strictly bounded, async-cancellable
subprocess adapter (:mod:`hermes_gateway.agent` — on timeout or cancellation the
child process group is killed and reaped), wrapped in a hardened HTTP layer
(:mod:`hermes_gateway.mcp.gateway`). No shell/exec, no SSH and no generic Hermes API
are reachable through the gateway; it fails closed when the local Hermes
binary is missing or not executable.
"""

__version__ = "0.3.0"
