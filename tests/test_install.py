from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"
OWNER_CODE_RE = r"^[0-9a-f]{64}$"


def _run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), *args],
        cwd=cwd or ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class InstallerTests(unittest.TestCase):
    def test_script_is_local_and_auditable(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/usr/bin/env bash"))
        self.assertIsNone(re.search(r"(?m)^\s*(curl|wget|sudo)\b", text))
        self.assertNotIn("| sh", text)
        self.assertNotIn("| bash", text)
        self.assertIn("--dry-run", text)
        self.assertIn("--non-interactive", text)

    def test_dry_run_does_not_write_or_leak_owner_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            before = {path.name for path in target.iterdir()}
            result = _run(
                [
                    "--dry-run",
                    "--non-interactive",
                    "--root",
                    str(target),
                    "--endpoint",
                    "https://mcp.example.com/mcp",
                ]
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({path.name for path in target.iterdir()}, before)
            self.assertIn("dry-run", result.stdout)
            self.assertIn("mode 600", result.stdout)
            self.assertIn("configure_plugin.py", result.stdout)
            self.assertIn("would not start the gateway", result.stdout)
            self.assertNotRegex(result.stdout, OWNER_CODE_RE)
            self.assertNotRegex(result.stderr, OWNER_CODE_RE)

    def test_dry_run_rejects_unsafe_endpoint(self) -> None:
        result = _run(
            [
                "--dry-run",
                "--non-interactive",
                "--endpoint",
                "http://mcp.example.com/mcp",
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid endpoint", result.stderr)

    def test_non_interactive_writes_env_and_mcp_without_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            result = _run(
                [
                    "--non-interactive",
                    "--skip-venv",
                    "--root",
                    str(target),
                    "--endpoint",
                    "https://mcp.example.com/mcp",
                ]
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            env_path = target / ".env.local"
            self.assertTrue(env_path.is_file())
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
            env_text = env_path.read_text(encoding="utf-8")
            owner = ""
            for line in env_text.splitlines():
                if line.startswith("HERMES_BRIDGE_SECRET="):
                    owner = line.split("=", 1)[1]
            self.assertRegex(owner, OWNER_CODE_RE)
            self.assertIn("HERMES_BRIDGE_PUBLIC_BASE_URL=https://mcp.example.com", env_text)
            self.assertNotIn(owner, result.stdout)
            self.assertNotIn(owner, result.stderr)
            for name in ("mcp.json", ".mcp.json"):
                server = json.loads((target / name).read_text())["mcpServers"]["hermes-bridge"]
                self.assertEqual(server["url"], "https://mcp.example.com/mcp")
                self.assertNotIn("headers", server)

    def test_keeps_existing_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            env_path = target / ".env.local"
            env_path.write_text(
                "HERMES_BRIDGE_SECRET=keep-this-existing-owner-code-value\n",  # pragma: allowlist secret
                encoding="utf-8",
            )
            os.chmod(env_path, 0o644)
            result = _run(
                [
                    "--non-interactive",
                    "--skip-venv",
                    "--root",
                    str(target),
                    "--endpoint",
                    "https://mcp.example.com/mcp",
                ]
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            text = env_path.read_text(encoding="utf-8")
            self.assertIn("keep-this-existing-owner-code-value", text)
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
            self.assertNotIn("keep-this-existing-owner-code-value", result.stdout)


if __name__ == "__main__":
    unittest.main()
