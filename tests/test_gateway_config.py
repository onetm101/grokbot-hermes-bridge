from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from hermes_gateway.agent import HermesAgent, HermesResult, HermesUnavailableError
from hermes_gateway.mcp.config import load_config
from hermes_gateway.mcp.gateway import build_app
from hermes_gateway.mcp.oauth import HermesOAuthProvider
from hermes_gateway.mcp.server import build_hermes_server


OWNER_CODE = "this-is-a-test-owner-code-with-more-than-32-chars"


class GatewayConfigTests(unittest.TestCase):
    def test_ready_requires_secret_and_https_base_url(self) -> None:
        self.assertFalse(load_config(environ={}).ready)
        self.assertFalse(load_config(environ={"HERMES_BRIDGE_SECRET": OWNER_CODE}).ready)
        cfg = load_config(
            environ={
                "HERMES_BRIDGE_SECRET": OWNER_CODE,
                "HERMES_BRIDGE_PUBLIC_BASE_URL": "https://mcp.example.com",
            }
        )
        self.assertTrue(cfg.ready)

    def test_placeholders_and_http_fail_closed(self) -> None:
        cfg = load_config(
            environ={
                "HERMES_BRIDGE_SECRET": "hermes-bridge-secret",  # pragma: allowlist secret
                "HERMES_BRIDGE_PUBLIC_BASE_URL": "http://mcp.example.com",
            }
        )
        self.assertFalse(cfg.ready)

    def test_owner_code_is_not_a_bearer_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(
                environ={
                    "HERMES_BRIDGE_SECRET": OWNER_CODE,
                    "HERMES_BRIDGE_PUBLIC_BASE_URL": "https://mcp.example.com",
                    "HERMES_BRIDGE_OAUTH_CLIENTS_FILE": str(Path(tmp) / "clients.json"),
                }
            )
            provider = HermesOAuthProvider(cfg)
            self.assertIsNone(asyncio.run(provider.load_access_token(OWNER_CODE)))

    def test_server_and_hardened_app_build_with_current_mcp_sdk(self) -> None:
        class FakeAgent:
            async def ask_async(self, question: str, context: str | None = None) -> HermesResult:
                return HermesResult("example", True, 1.0)

            async def status_async(self) -> dict[str, object]:
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(
                environ={
                    "HERMES_BRIDGE_SECRET": OWNER_CODE,
                    "HERMES_BRIDGE_PUBLIC_BASE_URL": "https://mcp.example.com",
                    "HERMES_BRIDGE_OAUTH_CLIENTS_FILE": str(Path(tmp) / "clients.json"),
                }
            )
            provider = HermesOAuthProvider(cfg)
            server = build_hermes_server(cfg, agent=FakeAgent(), auth_provider=provider)
            app = build_app(cfg, server=server)
            self.assertTrue(callable(app))


class HermesAdapterTests(unittest.TestCase):
    def test_binary_and_home_are_fail_closed(self) -> None:
        with self.assertRaises(HermesUnavailableError):
            HermesAgent(hermes_bin="relative/hermes", hermes_home="relative/home")

    def test_question_is_one_bounded_argv_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "hermes"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(binary, 0o700)
            home = root / "home"
            home.mkdir()
            agent = HermesAgent(
                hermes_bin=str(binary),
                hermes_home=str(home),
                ask_mode="oneshot",
                oneshot_safe_mode=True,
            )
            argv = agent._ask_argv("hello --version; still data")
            self.assertEqual(argv[0], str(binary))
            self.assertEqual(argv[1], "--safe-mode")
            self.assertEqual(argv[2], "--oneshot=hello --version; still data")
            argv_full = agent._ask_argv("x", safe_mode=False)
            self.assertEqual(argv_full, [str(binary), "--oneshot=x"])


if __name__ == "__main__":
    unittest.main()
