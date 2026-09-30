"""Hermes MCP server.

Builds the MCP ``FastMCP`` server backed by the live local Hermes agent
through the bounded :mod:`hermes_gateway.agent` adapter:

  * ``hermes_ask`` — short synchronous question. Waits for the answer.
    Use only when Hermes should finish in under about 30 seconds.
  * ``hermes_status`` — non-sensitive service status (``hermes status`` only,
    strictly filtered).
  * ``hermes_ask_async`` — enqueue a long Mac/browser ask and return
    immediately with a job id. Does not wait for Hermes.
  * ``hermes_job_status`` — read one async job. When done, includes the
    same bounded answer shape as ``hermes_ask``.

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
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.transport_security import TransportSecuritySettings

from ..agent import HermesAgent
from ..jobs import JobRunner
from .config import GatewayConfig

logger = logging.getLogger("hermes_gateway.mcp.server")

__all__ = ["build_hermes_server"]

_TOOL_NAMES = (
    "hermes_ask",
    "hermes_status",
    "hermes_ask_async",
    "hermes_job_status",
)


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
        ask_mode=config.ask_mode,
        oneshot_safe_mode=config.oneshot_safe_mode,
        worker_socket=config.worker_socket,
        worker_start_timeout_seconds=config.worker_start_timeout_seconds,
    )
    jobs_home = getattr(agent, "hermes_home", None) or config.hermes_home
    jobs_dir = Path(jobs_home) / "run" / "jobs" if jobs_home else None
    runner = JobRunner(
        agent,
        jobs_dir,
        job_timeout_seconds=config.job_timeout_seconds,
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
            "Hermes gateway. Short asks (under ~30s): hermes_ask. "
            "Long Mac/browser jobs: hermes_ask_async, then poll hermes_job_status "
            "every 15-30s. hermes_status for service status. "
            "No execution, no generic Hermes API. Do not repeat a long hermes_ask "
            "after a client timeout."
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
        """Ask Hermes one short question and wait for the bounded answer.

        Use only when the work should finish in under about 30 seconds.
        Long Mac, Chrome, or browser jobs must use hermes_ask_async and then
        hermes_job_status. Grok Bot's MCP client times out around 60 seconds
        (-32001); repeating this tool will not finish a long job.

        Args:
            question: The question or request to route.
            context: Optional non-sensitive context (treated as non-executable).
        """
        # Async + cancellable: if the request is cancelled (e.g. transport
        # timeout), the adapter kills and reaps the child process group
        # before the cancellation propagates. The short sync budget is unchanged.
        result = await agent.ask_async(question=question, context=context)
        return json.dumps(result.to_dict(), ensure_ascii=False)

    @server.tool()
    async def hermes_status() -> str:
        """Return non-sensitive Hermes service status (hermes status, filtered)."""
        return json.dumps(await agent.status_async(), ensure_ascii=False)

    @server.tool()
    async def hermes_ask_async(
        question: str,
        context: Optional[str] = None,
        job_id: Optional[str] = None,
        label: Optional[str] = None,
    ) -> str:
        """Enqueue a long Hermes ask and return immediately.

        Use for Mac, Chrome, or browser work that may run longer than about
        30 seconds (for example a Costco session). Returns job_id, status
        (queued or running), and result_path without waiting for Hermes.
        Poll hermes_job_status every 15-30 seconds. One long job runs at a
        time; extra jobs stay queued.

        Do not put passwords, tokens, or SMS bodies in the question. Context
        is optional and non-executable. job_id and label are optional.

        Args:
            question: The long request to hand to Hermes.
            context: Optional non-executable context. It is not stored on disk.
            job_id: Optional id ([A-Za-z0-9_-], max 64). Reuses the existing
                record when that id is already known.
            label: Optional short label stored with the job after redaction.
        """
        payload = await runner.enqueue(
            question, context=context, job_id=job_id, label=label,
        )
        return json.dumps(payload, ensure_ascii=False)

    @server.tool()
    async def hermes_job_status(job_id: str) -> str:
        """Return the status of one async Hermes job without waiting on it.

        status is queued, running, done, or failed. When done, answer is the
        bounded Hermes reply (same limits as hermes_ask). Passwords, tokens,
        and SMS bodies are removed from the stored answer.

        Args:
            job_id: The id returned by hermes_ask_async.
        """
        payload = await runner.status(job_id)
        return json.dumps(payload, ensure_ascii=False)

    if auth_provider is not None:
        server.custom_route("/oauth/approve", methods=["GET", "POST"])(auth_provider.approval)

    return server
