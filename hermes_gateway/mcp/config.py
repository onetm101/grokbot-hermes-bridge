"""Gateway configuration, loaded from the runtime environment.

Security posture is *fail-closed*: the gateway refuses to start a serving
entrypoint when the dedicated runtime secret ``HERMES_BRIDGE_SECRET`` is absent,
too short, or equal to a known-insecure placeholder. All limits have safe
defaults, are validated (type, range, path shape, allowlist entry shape) and
clamped to sane bounds. When ``environ`` is injected, *every* read goes
through it — ``os.environ`` is never consulted behind the caller's back.

This file never prints, logs or writes the secret value.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Mapping, Optional

from .security import MIN_SECRET_LEN

__all__ = [
    "GatewayConfig",
    "load_config",
    "DEFAULT_SECRET_VAR",
    "REQUEST_TIMEOUT_MARGIN_SECONDS",
]

DEFAULT_SECRET_VAR = "HERMES_BRIDGE_SECRET"  # pragma: allowlist secret

# The transport (ASGI request) timeout must always cover the backend ask
# timeout plus this margin, so the gateway never 504s a request whose backend
# subprocess was still within budget. load_config() enforces the invariant:
#   request_timeout_seconds >= ask_timeout_seconds + REQUEST_TIMEOUT_MARGIN_SECONDS
REQUEST_TIMEOUT_MARGIN_SECONDS = 10.0

# Barely-secret placeholders that must never be accepted as a real secret.
_INSECURE_PLACEHOLDERS = {
    "changeme",
    "change-me",
    "changeme123",
    "replaceme",
    "your_secret_here",
    "secret",
    "hermes",
    "hermes-bridge-secret",
}

# Validation shapes for allowlists / path / service name.
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,253})$")
_ORIGIN_RE = re.compile(r"^https?://[a-z0-9]([a-z0-9.-]{0,253})(:\d{1,5})?$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9/_-]{0,127}$")
_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PUBLIC_BASE_RE = re.compile(r"^https://[a-z0-9]([a-z0-9.-]{0,253})(:\d{1,5})?$")

# Range clamps for numeric limits (floor, ceiling).
_INT_BOUNDS = {
    "max_payload_bytes": (1024, 16_777_216),        # 1 KiB .. 16 MiB
    "rate_max_requests": (1, 100_000),
    "max_concurrent": (1, 1024),
    "max_turns": (1, 30),
}
_FLOAT_BOUNDS = {
    "rate_window_seconds": (0.1, 3600.0),
    # ceiling must fit ask ceiling + margin (180 + 10)
    "request_timeout_seconds": (1.0, 300.0),
    "ask_timeout_seconds": (1.0, 180.0),
    "worker_start_timeout_seconds": (5.0, 300.0),
}


def _int_env(env: Mapping[str, str], name: str, default: int, bounds: tuple) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    lo, hi = bounds
    return max(lo, min(hi, val))


def _float_env(env: Mapping[str, str], name: str, default: float, bounds: tuple) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = float(raw)
    except (ValueError, OverflowError):
        return default
    if val != val or val in (float("inf"), float("-inf")):
        return default
    lo, hi = bounds
    return max(lo, min(hi, val))


def _hosts_env(env: Mapping[str, str], name: str, default: List[str]) -> List[str]:
    raw = env.get(name)
    if not raw:
        return list(default)
    out = []
    for h in raw.split(","):
        h = h.strip().lower()
        if h and _HOST_RE.match(h):
            out.append(h)
    return out or list(default)


def _origins_env(env: Mapping[str, str], name: str) -> List[str]:
    raw = env.get(name)
    if not raw:
        return []
    out = []
    for o in raw.split(","):
        o = o.strip().lower().rstrip("/")
        if o and _ORIGIN_RE.match(o):
            out.append(o)
    return out


@dataclass
class GatewayConfig:
    secret: Optional[str] = None
    secret_configured: bool = False

    # host / origin allowlists (defense in depth; the MCP SDK also validates)
    allowed_hosts: List[str] = field(default_factory=lambda: ["localhost", "127.0.0.1"])
    allowed_origins: List[str] = field(default_factory=list)  # empty => same-origin only

    # limits
    max_payload_bytes: int = 1_048_576            # 1 MiB
    rate_max_requests: int = 60                   # per window per client
    rate_window_seconds: float = 60.0
    max_concurrent: int = 8
    # Transport timeout. Must cover ask_timeout_seconds + margin so the ASGI
    # layer never times out a backend call that is still within its own
    # budget (load_config enforces this; see REQUEST_TIMEOUT_MARGIN_SECONDS).
    request_timeout_seconds: float = 65.0

    # behaviour
    require_tls_hint: bool = False                # informational; TLS is terminated at proxy
    mcp_path: str = "/mcp"
    service_name: str = "hermes"
    public_base_url: Optional[str] = None
    oauth_clients_file: str = "~/.config/hermes-bridge/oauth-clients.json"

    # Hermes adapter (hermes_ask / hermes_status backend). ``None`` means
    # "probe known local install locations" / "default ~/.hermes" — actual
    # fail-closed validation happens in hermes_gateway.agent at server build time.
    hermes_bin: Optional[str] = None
    hermes_home: Optional[str] = None
    max_turns: int = 10
    # Default fits under Grok Bot's ~60s MCP client timeout with the warm worker.
    ask_timeout_seconds: float = 55.0
    # ask path: auto (worker then oneshot fallback) | worker | oneshot
    ask_mode: str = "auto"
    # oneshot fallback uses --safe-mode by default (fast, no plugins/MCP)
    oneshot_safe_mode: bool = True
    worker_socket: Optional[str] = None
    worker_start_timeout_seconds: float = 90.0

    @property
    def ready(self) -> bool:
        # OAuth discovery needs the canonical HTTPS issuer URL. Refuse to
        # serve if either half of the remote configuration is missing.
        return self.secret_configured and self.public_base_url is not None


def _is_placeholder(secret: str) -> bool:
    return secret.strip().lower() in _INSECURE_PLACEHOLDERS


def load_config(
    *,
    secret: Optional[str] = None,
    secret_var: str = DEFAULT_SECRET_VAR,
    environ: Optional[Mapping[str, str]] = None,
) -> GatewayConfig:
    """Build a :class:`GatewayConfig` from the environment (or an explicit dict).

    ``secret`` takes precedence over ``environ[secret_var]``. The secret is
    validated (non-empty, min length, not a placeholder) — a rejected secret
    leaves ``secret_configured=False`` so callers stay fail-closed. When
    ``environ`` is supplied, no value is read from ``os.environ``.
    """
    env: Mapping[str, str] = environ if environ is not None else os.environ

    cfg = GatewayConfig()

    # secret resolution: explicit arg > env var
    raw_secret = secret if secret is not None else env.get(secret_var)
    if raw_secret is None or raw_secret.strip() == "":
        cfg.secret = None
        cfg.secret_configured = False
    else:
        stripped = raw_secret.strip()
        if _is_placeholder(stripped) or len(stripped) < MIN_SECRET_LEN:
            cfg.secret = None
            cfg.secret_configured = False
        else:
            cfg.secret = stripped
            cfg.secret_configured = True

    # allowlists — validated entry-by-entry; invalid entries are dropped and
    # an entirely-invalid host list falls back to the safe default.
    cfg.allowed_hosts = _hosts_env(env, "HERMES_BRIDGE_ALLOWED_HOSTS", cfg.allowed_hosts)
    cfg.allowed_origins = _origins_env(env, "HERMES_BRIDGE_ALLOWED_ORIGINS")

    cfg.max_payload_bytes = _int_env(env, "HERMES_BRIDGE_MAX_PAYLOAD_BYTES",
                                     cfg.max_payload_bytes, _INT_BOUNDS["max_payload_bytes"])
    cfg.rate_max_requests = _int_env(env, "HERMES_BRIDGE_MAX_REQUESTS",
                                     cfg.rate_max_requests, _INT_BOUNDS["rate_max_requests"])
    cfg.rate_window_seconds = _float_env(env, "HERMES_BRIDGE_RATE_WINDOW_SECONDS",
                                         cfg.rate_window_seconds, _FLOAT_BOUNDS["rate_window_seconds"])
    cfg.max_concurrent = _int_env(env, "HERMES_BRIDGE_MAX_CONCURRENT",
                                  cfg.max_concurrent, _INT_BOUNDS["max_concurrent"])
    cfg.request_timeout_seconds = _float_env(env, "HERMES_BRIDGE_REQUEST_TIMEOUT_SECONDS",
                                             cfg.request_timeout_seconds,
                                             _FLOAT_BOUNDS["request_timeout_seconds"])

    raw_path = env.get("HERMES_BRIDGE_PATH", cfg.mcp_path)
    cfg.mcp_path = raw_path if _PATH_RE.match(raw_path or "") else "/mcp"

    raw_service = env.get("HERMES_BRIDGE_SERVICE_NAME", cfg.service_name)
    cfg.service_name = raw_service if _SERVICE_RE.match(raw_service or "") else "hermes"

    raw_public_base = (env.get("HERMES_BRIDGE_PUBLIC_BASE_URL") or "").strip().lower().rstrip("/")
    cfg.public_base_url = raw_public_base if _PUBLIC_BASE_RE.match(raw_public_base) else None
    raw_clients_file = (env.get("HERMES_BRIDGE_OAUTH_CLIENTS_FILE") or cfg.oauth_clients_file).strip()
    cfg.oauth_clients_file = raw_clients_file if raw_clients_file else cfg.oauth_clients_file

    # Hermes adapter settings. The binary/home paths get their hard
    # fail-closed validation in hermes_gateway.agent; here we only accept absolute
    # paths so a relative/malformed value never reaches the resolver.
    raw_bin = (env.get("HERMES_BRIDGE_HERMES_BIN") or "").strip()
    cfg.hermes_bin = raw_bin if raw_bin.startswith("/") else None
    # HERMES_BRIDGE_HERMES_HOME wins; plain HERMES_HOME is honoured as a fallback so
    # a standard Hermes environment needs no duplicate variable. No production
    # path is ever hardcoded here.
    raw_home = (env.get("HERMES_BRIDGE_HERMES_HOME") or env.get("HERMES_HOME") or "").strip()
    cfg.hermes_home = raw_home if raw_home.startswith("/") else None
    cfg.max_turns = _int_env(env, "HERMES_BRIDGE_MAX_TURNS", cfg.max_turns, _INT_BOUNDS["max_turns"])
    cfg.ask_timeout_seconds = _float_env(env, "HERMES_BRIDGE_ASK_TIMEOUT_SECONDS",
                                         cfg.ask_timeout_seconds, _FLOAT_BOUNDS["ask_timeout_seconds"])

    raw_mode = (env.get("HERMES_BRIDGE_ASK_MODE") or cfg.ask_mode).strip().lower()
    cfg.ask_mode = raw_mode if raw_mode in ("auto", "worker", "oneshot") else "auto"
    raw_safe = (env.get("HERMES_BRIDGE_ONESHOT_SAFE_MODE") or "1").strip().lower()
    cfg.oneshot_safe_mode = raw_safe not in ("0", "false", "no", "off")
    raw_sock = (env.get("HERMES_BRIDGE_WORKER_SOCKET") or "").strip()
    cfg.worker_socket = raw_sock if raw_sock.startswith("/") else None
    cfg.worker_start_timeout_seconds = _float_env(
        env, "HERMES_BRIDGE_WORKER_START_TIMEOUT_SECONDS",
        cfg.worker_start_timeout_seconds, _FLOAT_BOUNDS["worker_start_timeout_seconds"],
    )

    # Invariant: the transport timeout always covers the backend budget plus
    # a margin, so a still-in-budget hermes_ask can never be 504'd by our own
    # ASGI layer (and a 504 therefore always implies backend kill+reap).
    floor = cfg.ask_timeout_seconds + REQUEST_TIMEOUT_MARGIN_SECONDS
    if cfg.request_timeout_seconds < floor:
        cfg.request_timeout_seconds = floor

    return cfg
