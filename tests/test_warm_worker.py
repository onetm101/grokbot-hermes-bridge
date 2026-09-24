"""Unit tests for the warm-worker ask path (mocked worker boundary)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from hermes_gateway.agent import HermesAgent, HermesResult


class _FakeWorker:
    def __init__(self, *, start_ok: bool = True, result: Optional[HermesResult] = None):
        self.start_ok = start_ok
        self.result = result or HermesResult("from-worker", True, 12.0)
        self.ensure_calls = 0
        self.ask_calls = 0
        self.stop_calls = 0
        self.last_prompt: Optional[str] = None

    async def ensure_started(self) -> bool:
        self.ensure_calls += 1
        return self.start_ok

    async def ask(self, prompt: str, timeout: float) -> HermesResult:
        self.ask_calls += 1
        self.last_prompt = prompt
        return self.result

    async def stop(self) -> None:
        self.stop_calls += 1


def _agent(tmp: Path, **kwargs) -> HermesAgent:
    binary = tmp / "hermes"
    binary.write_text("#!/bin/sh\nprintf 'oneshot-answer\\n'\n", encoding="utf-8")
    os.chmod(binary, 0o700)
    home = tmp / "home"
    home.mkdir()
    return HermesAgent(
        hermes_bin=str(binary),
        hermes_home=str(home),
        ask_timeout_seconds=5,
        **kwargs,
    )


class WarmWorkerAskTests(unittest.TestCase):
    def test_auto_uses_worker_when_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = _FakeWorker()
            agent = _agent(Path(tmp), ask_mode="auto", worker=worker)
            result = asyncio.run(agent.ask_async("hello"))
            self.assertTrue(result.ok)
            self.assertEqual(result.answer, "from-worker")
            self.assertEqual(worker.ensure_calls, 1)
            self.assertEqual(worker.ask_calls, 1)
            self.assertEqual(worker.last_prompt, "hello")

    def test_auto_falls_back_to_oneshot_when_worker_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = _FakeWorker(start_ok=False)
            agent = _agent(Path(tmp), ask_mode="auto", worker=worker, oneshot_safe_mode=True)

            # Inject a runner that records argv and returns a successful oneshot.
            seen = {}

            def runner(argv, **kwargs):
                seen["argv"] = list(argv)

                class Proc:
                    returncode = 0
                    stdout = "oneshot-fallback\n"
                    stderr = ""

                return Proc()

            agent._runner = runner
            # Sync ask uses oneshot; for async fallback we need async spawn.
            # Force the oneshot path through ask_async with a custom spawner.

            class _Proc:
                def __init__(self):
                    self.returncode = None
                    self.pid = 0
                    self.stdout = _Stdout()

                def send_signal(self, _sig):
                    self.returncode = -9

                async def wait(self):
                    self.returncode = 0
                    return 0

            class _Stdout:
                def __init__(self):
                    self._sent = False

                async def read(self, _n):
                    if self._sent:
                        return b""
                    self._sent = True
                    return b"oneshot-fallback\n"

            async def spawner(*argv, **kwargs):
                seen["argv"] = list(argv)
                return _Proc()

            agent._spawner = spawner
            result = asyncio.run(agent.ask_async("ping"))
            self.assertTrue(result.ok)
            self.assertEqual(result.answer, "oneshot-fallback")
            self.assertEqual(worker.ask_calls, 0)
            self.assertIn("--safe-mode", seen["argv"])
            self.assertTrue(any(a.startswith("--oneshot=") for a in seen["argv"]))

    def test_worker_mode_errors_when_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = _FakeWorker(start_ok=False)
            agent = _agent(Path(tmp), ask_mode="worker", worker=worker)
            result = asyncio.run(agent.ask_async("x"))
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "worker_unavailable")

    def test_oneshot_mode_skips_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = _FakeWorker()
            agent = _agent(Path(tmp), ask_mode="oneshot", worker=worker)

            class _Proc:
                def __init__(self):
                    self.returncode = None
                    self.pid = 0
                    self.stdout = _Stdout()

                def send_signal(self, _sig):
                    self.returncode = -9

                async def wait(self):
                    self.returncode = 0
                    return 0

            class _Stdout:
                def __init__(self):
                    self._sent = False

                async def read(self, _n):
                    if self._sent:
                        return b""
                    self._sent = True
                    return b"direct\n"

            async def spawner(*_a, **_k):
                return _Proc()

            agent._spawner = spawner
            result = asyncio.run(agent.ask_async("q"))
            self.assertTrue(result.ok)
            self.assertEqual(result.answer, "direct")
            self.assertEqual(worker.ensure_calls, 0)

    def test_context_appended_in_worker_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = _FakeWorker()
            agent = _agent(Path(tmp), ask_mode="worker", worker=worker)
            asyncio.run(agent.ask_async("Q", context="CTX"))
            self.assertIn("Q", worker.last_prompt or "")
            self.assertIn("CTX", worker.last_prompt or "")
            self.assertIn("[Contexte non exécutable]", worker.last_prompt or "")

    def test_empty_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = _FakeWorker()
            agent = _agent(Path(tmp), ask_mode="auto", worker=worker)
            result = asyncio.run(agent.ask_async("   "))
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "empty_question")
            self.assertEqual(worker.ensure_calls, 0)


class AskModeConfigTests(unittest.TestCase):
    def test_load_config_ask_mode_knobs(self) -> None:
        from hermes_gateway.mcp.config import load_config

        cfg = load_config(
            environ={
                "HERMES_BRIDGE_SECRET": "this-is-a-test-owner-code-with-more-than-32-chars",
                "HERMES_BRIDGE_PUBLIC_BASE_URL": "https://mcp.example.com",
                "HERMES_BRIDGE_ASK_MODE": "worker",
                "HERMES_BRIDGE_ONESHOT_SAFE_MODE": "0",
                "HERMES_BRIDGE_ASK_TIMEOUT_SECONDS": "50",
                "HERMES_BRIDGE_WORKER_SOCKET": "/tmp/hermes-worker.sock",
            }
        )
        self.assertEqual(cfg.ask_mode, "worker")
        self.assertFalse(cfg.oneshot_safe_mode)
        self.assertEqual(cfg.ask_timeout_seconds, 50.0)
        self.assertEqual(cfg.worker_socket, "/tmp/hermes-worker.sock")
        self.assertGreaterEqual(cfg.request_timeout_seconds, 60.0)


if __name__ == "__main__":
    unittest.main()
