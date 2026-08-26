#!/usr/bin/env python3
"""Write the public MCP endpoint into both vendor config files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit


def validate_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("endpoint must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("endpoint must not contain credentials, a query, or a fragment")
    if parsed.path != "/mcp":
        raise ValueError("endpoint path must be exactly /mcp")
    return endpoint


def configure(root: Path, endpoint: str) -> None:
    payload = {
        "mcpServers": {
            "hermes-bridge": {
                "type": "http",
                "url": validate_endpoint(endpoint),
                "tool_timeout_sec": 240,
            }
        }
    }
    rendered = json.dumps(payload, indent=2) + "\n"
    for name in ("mcp.json", ".mcp.json"):
        (root / name).write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", help="Public endpoint, for example https://mcp.example.com/mcp")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        endpoint = validate_endpoint(args.endpoint)
    except ValueError as exc:
        parser.error(str(exc))
    configure(args.root.resolve(), endpoint)
    print(f"configured MCP endpoint: {endpoint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
