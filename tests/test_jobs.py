"""Async Hermes jobs: enqueue returns before the ask finishes."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hermes_gateway.agent import MAX_ANSWER_LEN, HermesAgent, HermesResult
from hermes_gateway.jobs import JobRunner, redact_sensitive
from hermes_gateway.mcp.config import load_config
from hermes_gateway.mcp.server import _TOOL_NAMES, build_hermes_server


OWNER_CODE = "this-is-a-test-owner-code-with-more-than-32-chars"


class _ScriptedAgent:
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[dict] = []

    async def ask_async(self, question: str, context: Optional[str] = None, timeout: Optional[float] = None):
        self.calls.append({"question": question, "context": context, "timeout": timeout})
        return await self._handler(question, context, timeout)


def _runner(tmp: Path, handler, **kwargs) -> JobRunner:
    return JobRunner(_ScriptedAgent(handler), tmp / "jobs", **kwargs)


async def _until(runner: JobRunner, job_id: str, status: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = await runner.status(job_id)
        if last.get("status") == status:
            return last
        await asyncio.sleep(0.01)
    return last


def _tool_json(result) -> dict:
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict):
        text = result.get("result", result)
        return text if isinstance(text, dict) else json.loads(text)
    if isinstance(result, str):
        return json.loads(result)
    texts = []
    for block in result:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return json.loads("".join(texts))


class AsyncJobTests(unittest.TestCase):
    def test_enqueue_returns_before_slow_ask_then_reaches_done(self) -> None:
        async def scenario() -> None:
            release = asyncio.Event()
            entered = asyncio.Event()

            async def slow(question, context, timeout):
                entered.set()
                await release.wait()
                return HermesResult("browser-ok", True, 1500.0)

            with tempfile.TemporaryDirectory() as tmp:
                runner = _runner(Path(tmp), slow, job_timeout_seconds=1800)
                started = time.monotonic()
                payload = await asyncio.wait_for(
                    runner.enqueue("open the store in chrome"),
                    timeout=1.0,
                )
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 0.5)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["status"], "queued")
                self.assertTrue(payload["job_id"])
                self.assertTrue(str(payload["result_path"]).endswith(".json"))

                running = await _until(runner, payload["job_id"], "running")
                self.assertEqual(running["status"], "running")
                self.assertNotIn("answer", running)
                self.assertTrue(entered.is_set())
                self.assertEqual(runner._agent.calls[0]["timeout"], 1800)

                release.set()
                done = await _until(runner, payload["job_id"], "done")
                self.assertTrue(done["ok"])
                self.assertEqual(done["answer"], "browser-ok")
                self.assertIsNone(done["error"])
                self.assertIn("started_at", done)
                self.assertIn("finished_at", done)
                self.assertEqual(done["elapsed_ms"], 1500.0)

                path = Path(done["result_path"])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["status"], "done")
                self.assertEqual(saved["answer"], "browser-ok")
                self.assertIn("open the store", saved["question_summary"])
                self.assertNotIn("question", saved)

        asyncio.run(scenario())

    def test_jobs_are_serial(self) -> None:
        async def scenario() -> None:
            release = asyncio.Event()
            order: list[str] = []

            async def slow(question, context, timeout):
                order.append(question)
                if question == "first":
                    await release.wait()
                return HermesResult(question + "-ok", True, 1.0)

            with tempfile.TemporaryDirectory() as tmp:
                runner = _runner(Path(tmp), slow)
                first = await runner.enqueue("first")
                second = await runner.enqueue("second")
                running = await _until(runner, first["job_id"], "running")
                self.assertEqual(running["status"], "running")
                queued = await runner.status(second["job_id"])
                self.assertEqual(queued["status"], "queued")
                self.assertEqual(order, ["first"])
                release.set()
                done_first = await _until(runner, first["job_id"], "done")
                done_second = await _until(runner, second["job_id"], "done")
                self.assertEqual(done_first["answer"], "first-ok")
                self.assertEqual(done_second["answer"], "second-ok")
                self.assertEqual(order, ["first", "second"])

        asyncio.run(scenario())

    def test_failed_ask_is_recorded(self) -> None:
        async def scenario() -> None:
            async def fail(question, context, timeout):
                return HermesResult("", False, 9.0, error="timeout")

            with tempfile.TemporaryDirectory() as tmp:
                runner = _runner(Path(tmp), fail)
                payload = await runner.enqueue("long")
                done = await _until(runner, payload["job_id"], "failed")
                self.assertFalse(done["ok"])
                self.assertEqual(done["error"], "timeout")
                self.assertNotIn("answer", done)

        asyncio.run(scenario())

    def test_unsafe_error_text_is_not_stored(self) -> None:
        async def scenario() -> None:
            leaked = "hun" + "ter2"
            async def fail(question, context, timeout):
                return HermesResult("", False, 1.0, error=f"pass word={leaked} leaked")

            with tempfile.TemporaryDirectory() as tmp:
                runner = _runner(Path(tmp), fail)
                payload = await runner.enqueue("long")
                done = await _until(runner, payload["job_id"], "failed")
                self.assertEqual(done["error"], "job_failed")
                blob = Path(done["result_path"]).read_text(encoding="utf-8")
                self.assertNotIn(leaked, blob)

        asyncio.run(scenario())

    def test_secrets_sms_and_context_are_not_persisted(self) -> None:
        password = "pass" + "word"
        token_name = "tok" + "en"
        sms = "S" + "MS"
        secret = "hun" + "ter2"
        token = "abcdEFGH" + "ijkl"
        otp = "445566"
        context_secret = "context" + "secret99"
        body = (
            f"Your Costco verification code is {otp}. "
            "Do not share this code with anyone."
        )
        question = (
            f"Open the site using {password}={secret} and {token_name}={token} "
            f"and include this {sms}: {body}"
        )
        context = f"{password}={context_secret}"

        async def scenario() -> None:
            async def echo(question, context, timeout):
                return HermesResult(f"{question}\n{context or ''}", True, 2.0)

            with tempfile.TemporaryDirectory() as tmp:
                runner = _runner(Path(tmp), echo)
                payload = await runner.enqueue(question, context=context, label="costco run")
                done = await _until(runner, payload["job_id"], "done")
                blob = Path(done["result_path"]).read_text(encoding="utf-8")
                response = json.dumps(done)
                for forbidden in (secret, token, otp, "Do not share", context_secret):
                    self.assertNotIn(forbidden, blob)
                    self.assertNotIn(forbidden, response)
                self.assertIn("costco run", blob)
                self.assertNotIn(context_secret, blob)
                self.assertIn("<redacted>", done["answer"])

        asyncio.run(scenario())

    def test_answer_is_bounded_like_sync_ask(self) -> None:
        async def scenario() -> None:
            async def huge(question, context, timeout):
                return HermesResult("y" * (MAX_ANSWER_LEN + 500), True, 1.0)

            with tempfile.TemporaryDirectory() as tmp:
                runner = _runner(Path(tmp), huge)
                payload = await runner.enqueue("count")
                done = await _until(runner, payload["job_id"], "done")
                self.assertTrue(done["truncated"])
                self.assertEqual(len(done["answer"]), MAX_ANSWER_LEN)

        asyncio.run(scenario())

    def test_empty_and_invalid_inputs(self) -> None:
        async def scenario() -> None:
            async def unused(question, context, timeout):
                raise AssertionError("ask should not run")

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runner = _runner(root, unused)
                empty = await runner.enqueue("   ")
                self.assertFalse(empty["ok"])
                self.assertEqual(empty["error"], "empty_question")
                self.assertEqual(list((root / "jobs").glob("*.json")), [])

                bad = await runner.enqueue("hello", job_id="../outside")
                self.assertEqual(bad["error"], "invalid_job_id")
                missing = await runner.status("missingjob")
                self.assertEqual(missing["error"], "not_found")
                self.assertFalse(missing["ok"])

        asyncio.run(scenario())

    def test_duplicate_job_id_does_not_run_twice(self) -> None:
        async def scenario() -> None:
            release = asyncio.Event()

            async def slow(question, context, timeout):
                await release.wait()
                return HermesResult("once", True, 1.0)

            with tempfile.TemporaryDirectory() as tmp:
                runner = _runner(Path(tmp), slow)
                first = await runner.enqueue("first question", job_id="jobonce01")
                await _until(runner, first["job_id"], "running")
                second = await runner.enqueue("second question", job_id="jobonce01")
                self.assertEqual(second["job_id"], "jobonce01")
                self.assertEqual(second["status"], "running")
                self.assertEqual(len(runner._agent.calls), 1)
                release.set()
                done = await _until(runner, "jobonce01", "done")
                self.assertEqual(done["answer"], "once")
                self.assertEqual(len(runner._agent.calls), 1)
                replay = await runner.enqueue("third", job_id="jobonce01")
                self.assertEqual(replay["status"], "done")
                self.assertEqual(len(runner._agent.calls), 1)

        asyncio.run(scenario())

    def test_queue_full(self) -> None:
        async def scenario() -> None:
            release = asyncio.Event()

            async def slow(question, context, timeout):
                await release.wait()
                return HermesResult("ok", True, 1.0)

            with tempfile.TemporaryDirectory() as tmp:
                runner = _runner(Path(tmp), slow, max_active=1)
                first = await runner.enqueue("only")
                self.assertTrue(first["ok"])
                blocked = await runner.enqueue("another")
                self.assertEqual(blocked["error"], "queue_full")
                release.set()
                await _until(runner, first["job_id"], "done")

        asyncio.run(scenario())

    def test_reap_expires_old_jobs_and_protects_inflight(self) -> None:
        async def scenario() -> None:
            async def quick(question, context, timeout):
                return HermesResult("kept", True, 1.0)

            with tempfile.TemporaryDirectory() as tmp:
                runner = _runner(Path(tmp), quick, ttl_seconds=24 * 60 * 60)
                old_at = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                assert runner.store is not None
                runner.store.create({
                    "v": 1,
                    "id": "oldjob0001",
                    "status": "done",
                    "question_summary": "stale",
                    "created_at": old_at,
                    "answer": "gone",
                    "error": None,
                })
                fresh = await runner.enqueue("fresh", job_id="freshjob01")
                done = await _until(runner, fresh["job_id"], "done")
                self.assertEqual(done["answer"], "kept")
                self.assertFalse((runner.store.root / "oldjob0001.json").exists())
                self.assertTrue((runner.store.root / "freshjob01.json").exists())

                runner.store.create({
                    "v": 1,
                    "id": "oldrunning1",
                    "status": "running",
                    "question_summary": "busy",
                    "created_at": old_at,
                })
                removed = runner.store.reap(protect={"oldrunning1"})
                self.assertEqual(removed, 0)
                self.assertTrue((runner.store.root / "oldrunning1.json").exists())

        asyncio.run(scenario())

    def test_restart_marks_orphaned_jobs_interrupted(self) -> None:
        async def scenario() -> None:
            async def quick(question, context, timeout):
                return HermesResult("new", True, 1.0)

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "jobs"
                first = JobRunner(_ScriptedAgent(quick), root)
                assert first.store is not None
                first.store.create({
                    "v": 1,
                    "id": "orphanjob1",
                    "status": "running",
                    "question_summary": "previous process",
                    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                second = JobRunner(_ScriptedAgent(quick), root)
                payload = await second.enqueue("after restart", job_id="afterrestart")
                orphan = await second.status("orphanjob1")
                self.assertEqual(orphan["status"], "failed")
                self.assertEqual(orphan["error"], "interrupted")
                done = await _until(second, payload["job_id"], "done")
                self.assertEqual(done["answer"], "new")

        asyncio.run(scenario())

    def test_sync_ask_keeps_short_timeout(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                binary = root / "hermes"
                binary.write_text("#!/bin/sh\nprintf 'oneshot\\n'\n", encoding="utf-8")
                os.chmod(binary, 0o700)
                home = root / "home"
                home.mkdir()

                class _Worker:
                    def __init__(self) -> None:
                        self.timeouts: list[float] = []

                    async def ensure_started(self) -> bool:
                        return True

                    async def ask(self, prompt: str, timeout: float) -> HermesResult:
                        self.timeouts.append(timeout)
                        return HermesResult("from-worker", True, 3.0)

                    async def stop(self) -> None:
                        return None

                worker = _Worker()
                agent = HermesAgent(
                    hermes_bin=str(binary),
                    hermes_home=str(home),
                    ask_mode="worker",
                    ask_timeout_seconds=55,
                    worker=worker,
                )
                sync = await agent.ask_async("short")
                self.assertTrue(sync.ok)
                self.assertEqual(sync.answer, "from-worker")
                self.assertEqual(worker.timeouts, [55])

                runner = JobRunner(agent, home / "run" / "jobs", job_timeout_seconds=1800)
                payload = await runner.enqueue("long browser job")
                done = await _until(runner, payload["job_id"], "done")
                self.assertEqual(done["answer"], "from-worker")
                self.assertEqual(worker.timeouts, [55, 1800])

        asyncio.run(scenario())

    def test_job_timeout_does_not_extend_http_timeout(self) -> None:
        cfg = load_config(
            environ={
                "HERMES_BRIDGE_SECRET": OWNER_CODE,
                "HERMES_BRIDGE_PUBLIC_BASE_URL": "https://mcp.example.com",
                "HERMES_BRIDGE_ASK_TIMEOUT_SECONDS": "55",
                "HERMES_BRIDGE_JOB_TIMEOUT_SECONDS": "1800",
            }
        )
        self.assertEqual(cfg.ask_timeout_seconds, 55.0)
        self.assertEqual(cfg.job_timeout_seconds, 1800.0)
        self.assertLess(cfg.request_timeout_seconds, 120.0)

        clamped = load_config(
            environ={
                "HERMES_BRIDGE_SECRET": OWNER_CODE,
                "HERMES_BRIDGE_PUBLIC_BASE_URL": "https://mcp.example.com",
                "HERMES_BRIDGE_JOB_TIMEOUT_SECONDS": "99999",
            }
        )
        self.assertEqual(clamped.job_timeout_seconds, 7200.0)
        floored = load_config(
            environ={
                "HERMES_BRIDGE_SECRET": OWNER_CODE,
                "HERMES_BRIDGE_PUBLIC_BASE_URL": "https://mcp.example.com",
                "HERMES_BRIDGE_JOB_TIMEOUT_SECONDS": "1",
            }
        )
        self.assertEqual(floored.job_timeout_seconds, 30.0)

    def test_redact_helper_keeps_ordinary_answer_text(self) -> None:
        self.assertIn("42.50", redact_sensitive("The basket total is $42.50."))
        self.assertNotIn("445566", redact_sensitive("verification code is 445566"))


class McpAsyncToolTests(unittest.TestCase):
    def test_server_registers_async_tools_and_keeps_sync_ask(self) -> None:
        async def scenario() -> None:
            release = asyncio.Event()

            class Agent:
                def __init__(self, home: Path) -> None:
                    self.hermes_home = home
                    self.sync_timeouts: list[object] = []
                    self.async_seen = asyncio.Event()

                async def ask_async(self, question: str, context: Optional[str] = None, timeout: Optional[float] = None):
                    if timeout is None:
                        self.sync_timeouts.append(timeout)
                        return HermesResult("short-answer", True, 4.0)
                    self.async_seen.set()
                    await release.wait()
                    return HermesResult("long-answer", True, 80.0)

                async def status_async(self) -> dict:
                    return {"ok": True}

            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "home"
                home.mkdir()
                cfg = load_config(
                    environ={
                        "HERMES_BRIDGE_SECRET": OWNER_CODE,
                        "HERMES_BRIDGE_PUBLIC_BASE_URL": "https://mcp.example.com",
                        "HERMES_BRIDGE_JOB_TIMEOUT_SECONDS": "1800",
                        "HERMES_BRIDGE_OAUTH_CLIENTS_FILE": str(Path(tmp) / "clients.json"),
                    }
                )
                agent = Agent(home)
                server = build_hermes_server(cfg, agent=agent)
                names = sorted(tool.name for tool in await server.list_tools())
                self.assertEqual(names, sorted(_TOOL_NAMES))

                sync = _tool_json(await server.call_tool("hermes_ask", {"question": "hi"}))
                self.assertEqual(sync["answer"], "short-answer")
                self.assertTrue(sync["ok"])
                self.assertNotIn("job_id", sync)
                self.assertEqual(agent.sync_timeouts, [None])

                started = time.monotonic()
                queued = _tool_json(await asyncio.wait_for(
                    server.call_tool(
                        "hermes_ask_async",
                        {"question": "drive chrome through the long checkout", "label": "checkout"},
                    ),
                    timeout=1.0,
                ))
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(queued["ok"])
                self.assertEqual(queued["status"], "queued")
                # The enqueue snapshot is queued even if the runner has already
                # entered the ask. The ask must still be blocked.
                status = _tool_json(await server.call_tool(
                    "hermes_job_status", {"job_id": queued["job_id"]},
                ))
                if status["status"] == "queued":
                    status = _tool_json(await server.call_tool(
                        "hermes_job_status", {"job_id": queued["job_id"]},
                    ))
                deadline = time.monotonic() + 2.0
                while status["status"] != "running" and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                    status = _tool_json(await server.call_tool(
                        "hermes_job_status", {"job_id": queued["job_id"]},
                    ))
                self.assertEqual(status["status"], "running")
                release.set()
                deadline = time.monotonic() + 2.0
                while status["status"] != "done" and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                    status = _tool_json(await server.call_tool(
                        "hermes_job_status", {"job_id": queued["job_id"]},
                    ))
                self.assertEqual(status["status"], "done")
                self.assertEqual(status["answer"], "long-answer")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
