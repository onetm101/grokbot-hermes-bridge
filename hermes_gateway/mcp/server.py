"""Hermes MCP server.

Builds the MCP ``FastMCP`` server exposing *exactly* two tools backed by the
live local Hermes agent through the bounded :mod:`hermes_gateway.agent` adapter:

  * ``hermes_ask``  — bounded question -> answer from the real Hermes agent
    (single subprocess call, hard timeout, clamped turn budget);
  * ``hermes_status`` — non-sensitive service status (``hermes status`` only,
    strictly filtered).

No shell/exec tool and no generic Hermes API are exposed here or anywhere in
the gateway. Building the server is fail-closed: a missing or non-executable
Hermes binary raises :class:`hermes_gateway.agent.HermesUnavailableError` so the
gateway never starts serving without a working backend. DNS-rebinding
protection (Host/Origin validation) is enabled via ``transport_security``;
the outer :mod:`hermes_gateway.mcp.gateway` hardening layer adds auth,
rate/concurrency/payload/timeout limits and redaction.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.transport_security import TransportSecuritySettings

from ..agent import HermesAgent
from .config import GatewayConfig

logger = logging.getLogger("hermes_gateway.mcp.server")

__all__ = ["build_hermes_server"]

_TOOL_NAMES = ("hermes_ask", "hermes_status")


def build_hermes_server(
    config: GatewayConfig,
    agent: Optional[HermesAgent] = None,
    auth_provider: Optional[object] = None,
) -> FastMCP:
    """Build the FastMCP Hermes server.

    ``agent`` is injectable for tests; defaults to a :class:`HermesAgent`
    wired to the local Hermes runtime described by ``config`` (fail-closed).
    """
    agent = agent or HermesAgent(
        name=config.service_name,
        hermes_bin=config.hermes_bin,
        hermes_home=config.hermes_home,
        max_turns=config.max_turns,
        ask_timeout_seconds=config.ask_timeout_seconds,
    )

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        # Accept both the bare hostname used by TLS reverse proxies and the
        # host:port form used by direct/local clients. The outer gateway
        # middleware still validates the same configured hostname allowlist.
        allowed_hosts=[
            candidate
            for host in config.allowed_hosts
            for candidate in (host, f"{host}:*")
        ],
        allowed_origins=list(config.allowed_origins),
    )

    auth_settings = None
    if auth_provider is not None and config.public_base_url:
        auth_settings = AuthSettings(
            issuer_url=config.public_base_url,
            resource_server_url=f"{config.public_base_url}{config.mcp_path}",
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["hermes"],
                default_scopes=["hermes"],
            ),
            required_scopes=["hermes"],
        )

    server = FastMCP(
        name=config.service_name,
        instructions=(
            "Hermes gateway. Exposes exactly hermes_ask and hermes_status. "
            "No execution, no generic Hermes API."
        ),
        streamable_http_path=config.mcp_path,
        json_response=True,
        stateless_http=True,
        auth_server_provider=auth_provider,
        auth=auth_settings,
        transport_security=security,
        debug=False,
    )

    @server.tool()
    async def hermes_ask(question: str, context: Optional[str] = None) -> str:
        """Ask the live Hermes agent one question and return the exact bounded answer.

        Args:
            question: The question or request to route.
            context: Optional non-sensitive context (treated as non-executable).
        """
        # Async + cancellable: if the request is cancelled (e.g. transport
        # timeout), the adapter kills and reaps the child process group
        # before the cancellation propagates.
        result = await agent.ask_async(question=question, context=context)
        return json.dumps(result.to_dict(), ensure_ascii=False)

    @server.tool()
    async def hermes_status() -> str:
        """Return non-sensitive Hermes service status (hermes status, filtered)."""
        return json.dumps(await agent.status_async(), ensure_ascii=False)

    if auth_provider is not None:
        server.custom_route("/oauth/approve", methods=["GET", "POST"])(auth_provider.approval)

    return server
