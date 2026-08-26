#!/usr/bin/env python3
"""Report local onboarding health without printing secrets."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.configure_plugin import validate_endpoint  # noqa: E402

MIN_OWNER_CODE_LEN = 32
PLACEHOLDER_OWNER_CODES = {
    "changeme",
    "change-me",
    "changeme123",
    "replaceme",
    "your_secret_here",
    "secret",
    "hermes",
    "hermes-bridge-secret",
    "<generate-a-unique-random-owner-code>",
    "<64-random-hex-characters>",
}
PUBLIC_BASE_PREFIX = "https://"
EXAMPLE_HOST = "mcp.example.com"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def check_python() -> Check:
    version = sys.version_info
    if version >= (3, 11):
        return Check("python", True, f"{version.major}.{version.minor} meets 3.11+")
    return Check("python", False, f"{version.major}.{version.minor} is below 3.11")


def check_venv(root: Path) -> Check:
    python = root / ".venv" / "bin" / "python"
    if python.is_file():
        return Check("venv", True, "local venv is present")
    return Check("venv", False, "local venv is missing")


def check_env_file(root: Path) -> list[Check]:
    path = root / ".env.local"
    if not path.is_file():
        return [Check("env-file", False, "local env file is missing")]

    mode = stat.S_IMODE(path.stat().st_mode)
    checks = [
        Check(
            "env-mode",
            mode == 0o600,
            "local env file mode is 600" if mode == 0o600 else "local env file mode must be 600",
        )
    ]
    try:
        values = _parse_env_file(path)
    except OSError:
        checks.append(Check("env-file", False, "local env file is unreadable"))
        return checks

    owner = values.get("HERMES_BRIDGE_SECRET", "")
    if not owner:
        checks.append(Check("owner-code", False, "owner code is missing"))
    elif owner.strip().lower() in PLACEHOLDER_OWNER_CODES or len(owner) < MIN_OWNER_CODE_LEN:
        checks.append(Check("owner-code", False, "owner code is not ready"))
    else:
        checks.append(Check("owner-code", True, "owner code is present"))

    public_base = values.get("HERMES_BRIDGE_PUBLIC_BASE_URL", "").strip().rstrip("/")
    parsed = urlsplit(public_base)
    public_ok = (
        public_base.startswith(PUBLIC_BASE_PREFIX)
        and parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.hostname != EXAMPLE_HOST
        and parsed.path in {"", "/"}
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )
    checks.append(
        Check(
            "public-base",
            public_ok,
            "public base uses https without a path" if public_ok else "public base must be an https origin",
        )
    )

    hermes_bin = Path(values.get("HERMES_BRIDGE_HERMES_BIN", "")).expanduser()
    bin_ok = hermes_bin.is_file() and bool(hermes_bin.stat().st_mode & stat.S_IXUSR)
    checks.append(
        Check(
            "hermes-bin",
            bin_ok,
            "Hermes executable is ready" if bin_ok else "set an existing executable Hermes path",
        )
    )

    hermes_home = Path(values.get("HERMES_BRIDGE_HERMES_HOME", "")).expanduser()
    home_ok = hermes_home.is_dir()
    checks.append(
        Check(
            "hermes-home",
            home_ok,
            "Hermes home is ready" if home_ok else "set an existing Hermes home directory",
        )
    )
    return checks


def _mcp_entry_checks(path: Path, label: str) -> list[Check]:
    if not path.is_file():
        return [Check(f"{label}-file", False, f"{label} is missing")]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        server = data["mcpServers"]["hermes-bridge"]
        endpoint = validate_endpoint(str(server["url"]))
        if urlsplit(endpoint).hostname == EXAMPLE_HOST:
            raise ValueError("placeholder endpoint")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return [Check(f"{label}-url", False, f"{label} endpoint is not a public https /mcp URL")]
    checks = [Check(f"{label}-url", True, f"{label} endpoint is https /mcp")]
    if "headers" in server:
        checks.append(Check(f"{label}-headers", False, f"{label} must not contain a headers block"))
    else:
        checks.append(Check(f"{label}-headers", True, f"{label} has no static headers"))
    return checks


def check_mcp_configs(root: Path) -> list[Check]:
    checks: list[Check] = []
    checks.extend(_mcp_entry_checks(root / "mcp.json", "mcp.json"))
    checks.extend(_mcp_entry_checks(root / ".mcp.json", ".mcp.json"))
    return checks


def run_checks(root: Path) -> list[Check]:
    checks = [check_python(), check_venv(root)]
    checks.extend(check_env_file(root))
    checks.extend(check_mcp_configs(root))
    return checks


def render(checks: list[Check]) -> str:
    lines = []
    for item in checks:
        mark = "ok" if item.ok else "fail"
        lines.append(f"{mark:4} {item.name}: {item.detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Checkout to inspect")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    checks = run_checks(root)
    print(render(checks))
    failed = sum(1 for item in checks if not item.ok)
    if failed:
        print(f"doctor: {failed} check(s) failed")
        return 1
    print("doctor: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
