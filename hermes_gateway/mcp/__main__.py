"""Hermes MCP gateway — command-line entrypoint.

Run:

    HERMES_BRIDGE_SECRET='<a-long-random-secret>' python -m hermes_gateway.mcp [--port 8000]

The gateway binds to 127.0.0.1 only. TLS is meant to be terminated by a
reverse proxy (see hermes/mcp/deploy/) — this process never holds or terminates
TLS itself, and never prints the secret.

Fails closed: refuses to serve when ``HERMES_BRIDGE_SECRET`` is missing or too
weak (see :func:`hermes_gateway.mcp.config.load_config`).
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..agent import HermesUnavailableError
from .config import DEFAULT_SECRET_VAR, load_config
from .gateway import build_app
from .security import RedactingFilter

logger = logging.getLogger("hermes_gateway.mcp")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-bridge", description="Hermes MCP Streamable HTTP gateway")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1; localhost only)")
    parser.add_argument("--port", type=int, default=8099, help="bind port (default 8099)")
    parser.add_argument("--log-level", default="INFO", help="logging level (default INFO)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    if not config.ready:
        logger.error(
            "Refusing to start: configure a strong %s and a valid "
            "HERMES_BRIDGE_PUBLIC_BASE_URL (HTTPS).",
            DEFAULT_SECRET_VAR,
        )
        return 2

    # Redact the configured secret AND generic bearer/token values from logs.
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter([config.secret] if config.secret else []))

    try:
        app = build_app(config)
    except HermesUnavailableError as e:
        # Fail-closed: no usable local Hermes binary / HERMES_HOME.
        logger.error("Refusing to start: %s", e)
        return 2
    try:
        import uvicorn
    except ImportError as e:  # pragma: no cover
        logger.error("uvicorn is required to run the gateway: %s", e)
        return 2

    logger.info("Hermes MCP gateway starting on http://%s:%s%s (TLS terminated by proxy)", args.host, args.port, config.mcp_path)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
