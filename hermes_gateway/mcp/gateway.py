"""Hermes MCP gateway: hardened Streamable HTTP application.

``build_app(config)`` returns a Starlette ASGI app that:

* serves the MCP Streamable HTTP endpoint (json/stateless) on ``config.mcp_path``;
* serves a non-sensitive ``/health`` endpoint (no auth);
* wraps the MCP endpoint in a pure-ASGI hardening layer enforcing:

  - fail-closed startup (no valid runtime secret => 503, never serve);
  - mandatory Bearer auth (constant-time compare, identical failure body);
  - Host / Origin / Content-Type validation and closed CORS;
  - payload-size, per-client rate, concurrency and request-timeout limits;
  - secret-free, redacted logging and non-sensitive health output.

This module has no dependency on shell/exec or any generic Hermes API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

from .config import GatewayConfig
from .limits import ConcurrencyGate, RateLimiter, read_body_capped
from .security import (
    AUTH_FAIL_REASON,
    RedactingFilter,
    SecretStore,
    is_health_path,
)
from .server import build_hermes_server
from .oauth import HermesOAuthProvider
from mcp.server.auth.provider import ProviderTokenVerifier

logger = logging.getLogger("hermes_gateway.mcp.gateway")

__all__ = ["build_app", "HardeningMiddleware"]

_APPLICATION_JSON = "application/json"


# ---------------------------------------------------------------------------
# ASGI helpers
# ---------------------------------------------------------------------------

async def _asgi_json(scope: dict, receive: Any, send: Any, status: int, payload: dict, extra_headers: Optional[list] = None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii")), (b"cache-control", b"no-store")]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _header(scope: dict, name: str) -> Optional[str]:
    target = name.lower().encode("ascii")
    for k, v in scope.get("headers", []):
        if k.lower() == target:
            return v.decode("latin-1")
    return None


def _client_ip(scope: dict) -> str:
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])
    return "unknown"


def _make_replay_receive(body: bytes):
    """Build an ASGI ``receive`` that replays an already-buffered request body.

    The gateway reads/caps the body itself (for the size limit), then re-injects
    it here so the inner MCP app still sees a normal request body stream.
    ``body`` may be empty for bodyless requests.
    """
    sent = False

    async def _replay():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        # one final empty message to signal end-of-stream
        return {"type": "http.request", "body": b"", "more_body": False}

    return _replay


def _hostname(host: str) -> str:
    if ":" in host and not host.startswith("["):
        # ip:port
        return host.rsplit(":", 1)[0]
    if host.startswith("["):
        # [v6]:port
        return host[1:].split("]")[0]
    return host


# ---------------------------------------------------------------------------
# Hardening middleware (pure ASGI)
# ---------------------------------------------------------------------------

class HardeningMiddleware:
    """Pure-ASGI security layer around the inner MCP Starlette app."""

    def __init__(
        self,
        app: Any,
        config: GatewayConfig,
        *,
        rate_limiter: Optional[RateLimiter] = None,
        concurrency: Optional[ConcurrencyGate] = None,
        secret_store: Optional[SecretStore] = None,
        token_verifier: Optional[Any] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.app = app
        self.config = config
        self.secret_store = secret_store or SecretStore(config.secret)
        self.rate_limiter = rate_limiter or RateLimiter(config.rate_max_requests, config.rate_window_seconds)
        self.concurrency = concurrency or ConcurrencyGate(config.max_concurrent)
        self._clock = clock or time.monotonic
        self._redact = RedactingFilter([config.secret] if config.secret else [])
        self.token_verifier = token_verifier

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        method = scope.get("method", "GET")

        # Minimal liveness endpoint — no auth, no config/readiness leak.
        if is_health_path(path):
            if method in ("GET", "HEAD"):
                await _asgi_json(scope, receive, send, 200, {"status": "ok"})
                return
            await _asgi_json(scope, receive, send, 405, {"error": "method not allowed"})
            return

        # Fail-closed: without a valid runtime secret we never serve MCP.
        if not self.config.ready:
            logger.warning("hermes gateway not configured (missing/weak HERMES_BRIDGE_SECRET) — refusing service")
            await _asgi_json(scope, receive, send, 503, {"error": "service not configured"})
            return

        # Host validation (defense in depth on top of MCP SDK check).
        host = _header(scope, "host")
        if host:
            hostname = _hostname(host).lower()
            allowed = {h.lower() for h in self.config.allowed_hosts}
            if hostname not in allowed:
                logger.warning("hermes gateway rejected host %r", host)
                await _asgi_json(scope, receive, send, 403, {"error": "forbidden"})
                return

        # Origin validation / closed CORS.
        origin = _header(scope, "origin")
        if origin:
            allowed_origins = set(self.config.allowed_origins)
            same_host = False
            if host:
                same_host = _origin_matches_host(origin, host)
            if not (same_host or origin in allowed_origins):
                logger.warning("hermes gateway rejected origin %r", origin)
                await _asgi_json(scope, receive, send, 403, {"error": "forbidden"})
                return

        oauth_public = path.startswith("/.well-known/") or path in {
            "/authorize", "/token", "/register", "/oauth/approve"
        }

        # Closed CORS on the MCP surface; OAuth discovery/token endpoints own
        # their narrowly-scoped CORS policy in the MCP SDK.
        if method == "OPTIONS" and not oauth_public:
            await _asgi_json(scope, receive, send, 403, {"error": "forbidden"})
            return

        # Content-Type gate (json/stateless MCP mode requires application/json).
        if path == self.config.mcp_path and method in ("POST", "PUT", "PATCH"):
            ctype = (_header(scope, "content-type") or "").split(";")[0].strip().lower()
            if ctype != _APPLICATION_JSON:
                await _asgi_json(scope, receive, send, 415, {"error": "unsupported media type"})
                return

        # Authentication (applies to the whole MCP surface).
        if path == self.config.mcp_path:
            authorization = _header(scope, "authorization") or ""
            token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
            valid = bool(token) and self.token_verifier is not None and await self.token_verifier.verify_token(token)
            if not valid:
                logger.info(
                    "hermes gateway auth failed (reason=%s, bearer=%s)",
                    AUTH_FAIL_REASON,
                    "present-invalid" if token else "missing",
                )
                resource_metadata = f'{self.config.public_base_url}/.well-known/oauth-protected-resource{self.config.mcp_path}'
                await _asgi_json(
                    scope, receive, send, 401, {"error": AUTH_FAIL_REASON},
                    extra_headers=[(b"www-authenticate", f'Bearer resource_metadata="{resource_metadata}"'.encode("ascii"))],
                )
                return

        # Rate limiting (per client IP).
        if not self.rate_limiter.allow(_client_ip(scope)):
            logger.warning("hermes gateway rate limit exceeded for %s", _client_ip(scope))
            await _asgi_json(scope, receive, send, 429, {"error": "rate limit exceeded"})
            return

        # Payload size limit: read the body capped, reject oversized before it
        # occupies a concurrency slot or reaches the MCP handler.
        effective_receive = receive
        if path == self.config.mcp_path and method in ("POST", "PUT", "PATCH"):
            body, too_large = await read_body_capped(receive, self.config.max_payload_bytes)
            if too_large:
                logger.warning("hermes gateway rejected oversized payload (>%d bytes)", self.config.max_payload_bytes)
                await _asgi_json(scope, receive, send, 413, {"error": "payload too large"})
                return
            effective_receive = _make_replay_receive(body)

        # Concurrency gate (fast-fail).
        if not await self.concurrency.try_acquire():
            logger.warning("hermes gateway at concurrency limit")
            await _asgi_json(scope, receive, send, 503, {"error": "concurrency limit exceeded"})
            return

        # Track whether the inner app already started the response, so the
        # error/timeout paths below never double-send ``http.response.start``.
        response_started = False

        async def guarded_send(message: dict) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            ok = await asyncio.wait_for(
                self._run_inner(scope, effective_receive, guarded_send),
                timeout=self.config.request_timeout_seconds,
            )
            if not ok:
                logger.warning("hermes gateway request handler completed with uncaught error")
                if not response_started:
                    await _asgi_json(scope, receive, send, 500, {"error": "internal error"})
        except asyncio.TimeoutError:
            logger.warning("hermes gateway request timed out after %.1fs", self.config.request_timeout_seconds)
            if not response_started:
                await _asgi_json(scope, receive, send, 504, {"error": "gateway timeout"})
        finally:
            self.concurrency.release()

    async def _run_inner(self, scope: dict, receive: Any, send: Any) -> bool:
        """Run the inner ASGI app, returning False if it raised.

        The caller decides whether an error response can still be sent (it
        must not if the inner app already emitted ``http.response.start``).
        """
        try:
            await self.app(scope, receive, send)
            return True
        except Exception as e:  # defensive; inner app owns its error handling
            logger.warning("hermes gateway inner app error: %s", type(e).__name__)
            return False

def _origin_matches_host(origin: str, host: str) -> bool:
    """True when ``origin`` (scheme://host[:port]) matches the request Host.

    Default ports are normalised (http/80, https/443) so ``https://localhost``
    matches ``localhost:443``, while genuinely different hosts or ports do not.
    """
    try:
        oauth = origin.split("://", 1)[1].rstrip("/") if "://" in origin else origin
        oh, op = _split_authority(oauth)
        hh, hp = _split_authority(host)
        return oh.lower() == hh.lower() and _norm_port(op) == _norm_port(hp)
    except Exception:
        return False


def _split_authority(authority: str):
    authority = (authority or "").strip()
    if authority.startswith("["):
        idx = authority.index("]")
        return authority[1:idx], authority[idx + 1:].lstrip(":")
    if ":" in authority:
        h, p = authority.rsplit(":", 1)
        return h, p
    return authority, ""


def _norm_port(port: str) -> str:
    p = (port or "").lower()
    return "" if p in ("", "80", "443") else p


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------

def build_app(
    config: GatewayConfig,
    *,
    server: Any = None,
    rate_limiter: Optional[RateLimiter] = None,
    concurrency: Optional[ConcurrencyGate] = None,
    secret_store: Optional[SecretStore] = None,
) -> Any:
    """Build the full hardened ASGI application (Starlette).

    ``server`` / ``rate_limiter`` / ``concurrency`` / ``secret_store`` are
    injectable for tests.
    """
    auth_provider = HermesOAuthProvider(config)
    server = server or build_hermes_server(config, auth_provider=auth_provider)
    inner = server.streamable_http_app()
    return HardeningMiddleware(
        inner,
        config,
        rate_limiter=rate_limiter,
        concurrency=concurrency,
        secret_store=secret_store,
        token_verifier=ProviderTokenVerifier(auth_provider),
    )
