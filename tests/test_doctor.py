from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.configure_plugin import configure
from scripts.doctor import Check, main, run_checks

ROOT = Path(__file__).resolve().parents[1]
OWNER = "this-is-a-test-owner-code-with-more-than-32-chars"


def _write_env(root: Path, *, owner: str = OWNER, public_base: str = "https://gateway.public.test") -> Path:
    hermes_bin = root / "bin" / "hermes"
    hermes_bin.parent.mkdir(parents=True, exist_ok=True)
    hermes_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hermes_bin, 0o700)
    hermes_home = root / "hermes-home"
    hermes_home.mkdir(exist_ok=True)
    path = root / ".env.local"
    path.write_text(
        "\n".join(
            [
                f"HERMES_BRIDGE_SECRET={owner}",
                f"HERMES_BRIDGE_PUBLIC_BASE_URL={public_base}",
                "HERMES_BRIDGE_ALLOWED_HOSTS=localhost,127.0.0.1,gateway.public.test",
                f"HERMES_BRIDGE_HERMES_BIN={hermes_bin}",
                f"HERMES_BRIDGE_HERMES_HOME={hermes_home}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def _write_venv(root: Path) -> None:
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    os.chmod(python, 0o700)


def _by_name(checks: list[Check]) -> dict[str, Check]:
    return {item.name: item for item in checks}


class DoctorTests(unittest.TestCase):
    def test_ready_fixture_passes_local_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_venv(root)
            _write_env(root)
            configure(root, "https://gateway.public.test/mcp")
            names = _by_name(run_checks(root))
            self.assertTrue(names["venv"].ok)
            self.assertTrue(names["env-mode"].ok)
            self.assertTrue(names["owner-code"].ok)
            self.assertTrue(names["public-base"].ok)
            self.assertTrue(names["hermes-bin"].ok)
            self.assertTrue(names["hermes-home"].ok)
            self.assertTrue(names["mcp.json-url"].ok)
            self.assertTrue(names["mcp.json-headers"].ok)

    def test_missing_env_and_headers_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure(root, "https://gateway.public.test/mcp")
            data = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
            data["mcpServers"]["hermes-bridge"]["headers"] = {"Authorization": "Bearer unused"}
            (root / "mcp.json").write_text(json.dumps(data), encoding="utf-8")
            names = _by_name(run_checks(root))
            self.assertFalse(names["venv"].ok)
            self.assertFalse(names["env-file"].ok)
            self.assertFalse(names["mcp.json-headers"].ok)

    def test_placeholder_owner_code_and_open_mode_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_venv(root)
            path = _write_env(root, owner="<generate-a-unique-random-owner-code>")
            os.chmod(path, 0o644)
            configure(root, "https://gateway.public.test/mcp")
            names = _by_name(run_checks(root))
            self.assertFalse(names["env-mode"].ok)
            self.assertFalse(names["owner-code"].ok)

    def test_example_endpoint_is_not_reported_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_venv(root)
            _write_env(root, public_base="https://mcp.example.com")
            configure(root, "https://mcp.example.com/mcp")
            names = _by_name(run_checks(root))
            self.assertFalse(names["public-base"].ok)
            self.assertFalse(names["mcp.json-url"].ok)

    def test_main_does_not_print_owner_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_venv(root)
            _write_env(root)
            configure(root, "https://gateway.public.test/mcp")
            from io import StringIO
            from contextlib import redirect_stdout

            buffer = StringIO()
            with redirect_stdout(buffer):
                code = main(["--root", str(root)])
            output = buffer.getvalue()
            self.assertEqual(code, 0, output)
            self.assertNotIn(OWNER, output)
            self.assertIn("doctor: ok", output)

    def test_docs_mention_pkce_and_no_static_header(self) -> None:
        tutorial = (ROOT / "docs" / "TUTORIAL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertLess(readme.index("## Quick start"), readme.index("## How it works"))
        self.assertIn("assets/bridge-overview.jpg", tutorial)
        self.assertIn("ImageGen", tutorial)
        for text in (tutorial, readme):
            self.assertIn("PKCE", text)
            self.assertIn("Authorization", text)
            self.assertIn("scripts/install.sh", text)
            self.assertIn("scripts/doctor.py", text)
            self.assertIn("visible", text.lower())
            self.assertIn("tool", text.lower())


if __name__ == "__main__":
    unittest.main()
