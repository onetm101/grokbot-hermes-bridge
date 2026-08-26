"""Hermes — adapter to the real, local Hermes agent.

This module is the *only* place the Hermes gateway pulls business logic from.
It routes ``hermes_ask`` to the live local Hermes agent through a strictly
bounded subprocess call, and ``hermes_status`` to ``hermes status``:

  * only a known local Hermes binary is ever executed (fail-closed when it is
    missing or not executable);
  * no ``shell=True`` anywhere, fixed argv shape — the question is carried as
    a single ``--oneshot=<question>`` argv element, so no flag or shell
    injection is possible;
  * the child environment is built explicitly (never inherited wholesale):
    ``HERMES_HOME`` is explicit, sessions are tagged ``HERMES_SESSION_SOURCE=
    grokbot``, and the turn budget is clamped to 1..30 via
    ``HERMES_MAX_ITERATIONS``. Provider/model resolution stays with the Hermes
    runtime config — nothing is hardcoded here;
  * hard timeout (180 s max) on every call; non-zero exit, empty output and
    timeout all produce bounded, non-leaking error results;
  * output is cleaned (ANSI/control stripped) and the exact final answer is
    returned;
  * no SSH, no shell tool, no generic Hermes API is reachable from here.

The **serving path is async and cancellable**: the MCP server calls
:meth:`HermesAgent.ask_async` / :meth:`HermesAgent.status_async`, which use
``asyncio.create_subprocess_exec`` (``start_new_session=True``, output
collection capped in bytes). On timeout *or task cancellation* (e.g. the
gateway's transport timeout firing) the whole child process group receives
SIGTERM, then SIGKILL after a bounded grace period, and the child is always
awaited/reaped — no runaway Hermes run survives a 504, and no zombie is left
behind. The synchronous :meth:`HermesAgent.ask` / :meth:`HermesAgent.status`
remain only as a CLI/test convenience boundary; the server never uses them.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

__all__ = [
    "HermesAgent",
    "HermesResult",
    "HermesUnavailableError",
    "resolve_hermes_binary",
    "resolve_hermes_home",
    "clamp_turns",
    "SESSION_SOURCE",
    "ASK_TIMEOUT_SECONDS",
    "STATUS_TIMEOUT_SECONDS",
    "KILL_GRACE_SECONDS",
    "MAX_CAPTURE_BYTES",
    "MIN_TURNS",
    "MAX_TURNS",
    "MAX_QUESTION_LEN",
    "MAX_CONTEXT_LEN",
    "MAX_ANSWER_LEN",
]

SESSION_SOURCE = "grokbot"

ASK_TIMEOUT_SECONDS = 180.0     # hard upper bound per task spec
STATUS_TIMEOUT_SECONDS = 30.0
KILL_GRACE_SECONDS = 5.0        # SIGTERM -> SIGKILL escalation grace
MAX_CAPTURE_BYTES = 262_144     # hard cap on bytes collected from the child

MIN_TURNS = 1
MAX_TURNS = 30
DEFAULT_MAX_TURNS = 10

MAX_QUESTION_LEN = 4000
MAX_CONTEXT_LEN = 8000
MAX_ANSWER_LEN = 8000

_HERMES_BASENAME = "hermes"

# Known local install locations probed when no explicit binary is configured.
_KNOWN_HERMES_LOCATIONS = (
    "/usr/local/bin/hermes",
    "/usr/bin/hermes",
    "/opt/hermes/bin/hermes",
)

# Minimal, fixed PATH for the child — never the parent's PATH.
_SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"          # CSI sequences
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences
    r"|\x1b[@-_]"                          # other escapes
)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Strict non-sensitive filter for status lines: any line that even *looks*
# like it carries credentials, endpoints or filesystem detail is dropped.
_SENSITIVE_LINE_RE = re.compile(
    r"(?i)(key|token|secret|password|passwd|bearer|authorization|credential"
    r"|cookie|api[_-]?key|https?://|ssh|@|/home/|/root/|/etc/|hermes_home)"
)
_MAX_STATUS_LINES = 40
_MAX_STATUS_LINE_LEN = 200


class HermesUnavailableError(RuntimeError):
    """Raised (fail-closed) when the local Hermes runtime cannot be used."""


def clamp_turns(value: object) -> int:
    try:
        turns = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MAX_TURNS
    return max(MIN_TURNS, min(MAX_TURNS, turns))


def _clamp_timeout(value: object, default: float, maximum: float) -> float:
    try:
        t = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if t <= 0:
        return default
    return min(t, maximum)


def resolve_hermes_binary(candidate: Optional[str] = None) -> Path:
    """Resolve the known local Hermes binary, fail-closed.

    * With ``candidate``: must be an absolute path to an existing, executable
      regular file whose basename is ``hermes``.
    * Without: probe the known local install locations.

    Raises :class:`HermesUnavailableError` when nothing valid is found.
    """
    candidates: List[str]
    if candidate:
        candidates = [candidate]
    else:
        candidates = list(_KNOWN_HERMES_LOCATIONS)

    for raw in candidates:
        path = Path(raw)
        if not path.is_absolute():
            continue
        if path.name != _HERMES_BASENAME:
            continue
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if not os.access(path, os.X_OK):
            continue
        return path

    raise HermesUnavailableError(
        "no usable local Hermes binary (must be an absolute path to an "
        "executable file named 'hermes')"
    )


def resolve_hermes_home(candidate: Optional[str] = None) -> Path:
    """Resolve the explicit HERMES_HOME directory, fail-closed."""
    raw = candidate or str(Path.home() / ".hermes")
    path = Path(raw)
    if not path.is_absolute():
        raise HermesUnavailableError("HERMES_HOME must be an absolute path")
    try:
        if not path.is_dir():
            raise HermesUnavailableError(f"HERMES_HOME directory does not exist: {path}")
    except OSError as e:
        raise HermesUnavailableError(f"HERMES_HOME is not accessible: {e}") from e
    return path


def _clean_output(text: str) -> str:
    text = _ANSI_RE.sub("", text or "")
    text = _CTRL_RE.sub("", text)
    return text.strip()


def _filter_status_lines(text: str) -> List[str]:
    """Strictly filter ``hermes status`` output down to non-sensitive lines."""
    lines: List[str] = []
    for raw in _clean_output(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        if _SENSITIVE_LINE_RE.search(line):
            continue
        lines.append(line[:_MAX_STATUS_LINE_LEN])
        if len(lines) >= _MAX_STATUS_LINES:
            break
    return lines


class HermesResult:
    """A bounded, serialisable answer produced by the Hermes adapter."""

    __slots__ = ("answer", "ok", "error", "elapsed_ms", "truncated")

    def __init__(self, answer: str, ok: bool, elapsed_ms: float,
                 error: Optional[str] = None, truncated: bool = False):
        self.answer = answer
        self.ok = ok
        self.error = error
        self.elapsed_ms = elapsed_ms
        self.truncated = truncated

    def to_dict(self) -> Dict[str, object]:
        return {
            "answer": self.answer,
            "ok": self.ok,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "truncated": self.truncated,
        }


class HermesAgent:
    """Adapter from the two Hermes tools to the live local Hermes agent.

    Construction is fail-closed: it raises :class:`HermesUnavailableError`
    when the Hermes binary is missing/non-executable or HERMES_HOME is
    invalid, so a misconfigured gateway never starts serving.

    ``runner`` is the synchronous process boundary (``subprocess.run``-
    compatible; CLI/test convenience only). ``spawner`` is the async process
    boundary used by the serving path (``asyncio.create_subprocess_exec``-
    compatible); both are injectable for tests.
    """

    def __init__(
        self,
        name: str = "hermes",
        *,
        hermes_bin: Optional[str] = None,
        hermes_home: Optional[str] = None,
        max_turns: object = DEFAULT_MAX_TURNS,
        ask_timeout_seconds: object = ASK_TIMEOUT_SECONDS,
        status_timeout_seconds: object = STATUS_TIMEOUT_SECONDS,
        kill_grace_seconds: object = KILL_GRACE_SECONDS,
        runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
        spawner: Optional[Callable] = None,
    ):
        self.name = name
        self.hermes_bin = resolve_hermes_binary(hermes_bin)
        self.hermes_home = resolve_hermes_home(hermes_home)
        self.max_turns = clamp_turns(max_turns)
        self.ask_timeout_seconds = _clamp_timeout(
            ask_timeout_seconds, ASK_TIMEOUT_SECONDS, ASK_TIMEOUT_SECONDS)
        self.status_timeout_seconds = _clamp_timeout(
            status_timeout_seconds, STATUS_TIMEOUT_SECONDS, ASK_TIMEOUT_SECONDS)
        self.kill_grace_seconds = _clamp_timeout(
            kill_grace_seconds, KILL_GRACE_SECONDS, 30.0)
        self._runner = runner
        self._spawner = spawner
        self._call_count = 0

    # -- internals -----------------------------------------------------------

    def _child_env(self, *, include_turns: bool) -> Dict[str, str]:
        """Explicit, minimal child environment — never the parent's env."""
        env: Dict[str, str] = {
            "PATH": _SAFE_PATH,
            "HOME": os.environ.get("HOME", str(Path.home())),
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "HERMES_HOME": str(self.hermes_home),
            "HERMES_SESSION_SOURCE": SESSION_SOURCE,
        }
        if include_turns:
            env["HERMES_MAX_ITERATIONS"] = str(self.max_turns)
        return env

    def _run(self, argv: List[str], timeout: float, *, include_turns: bool):
        return self._runner(
            argv,
            env=self._child_env(include_turns=include_turns),
            cwd=str(self.hermes_home),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            start_new_session=True,
        )

    @staticmethod
    def _build_prompt(question: str, context: Optional[str]) -> Optional[str]:
        """Normalise and bound the (question, context) pair into one prompt.

        Returns ``None`` when the question is empty after normalisation.
        """
        if not isinstance(question, str):
            question = str(question)
        question = question.strip()[:MAX_QUESTION_LEN]
        if not question:
            return None
        prompt = question
        if context:
            if not isinstance(context, str):
                context = str(context)
            context = context.strip()[:MAX_CONTEXT_LEN]
            if context:
                prompt += "\n\n[Contexte non exécutable]\n" + context
        return prompt

    def _ask_argv(self, prompt: str) -> List[str]:
        # Fixed argv: binary + one self-contained --oneshot=<prompt> token.
        # The prompt is a single argv element; it can never become a flag,
        # an extra argument, or shell input.
        return [str(self.hermes_bin), "--oneshot=" + prompt]

    # -- async process boundary (serving path) --------------------------------

    async def _spawn(self, argv: List[str], *, include_turns: bool):
        """Spawn the child in its own session with a minimal explicit env."""
        spawner = self._spawner or asyncio.create_subprocess_exec
        return await spawner(
            *argv,
            env=self._child_env(include_turns=include_turns),
            cwd=str(self.hermes_home),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            # stderr is never surfaced (it may carry paths/config detail) —
            # drop it at the kernel boundary instead of buffering it.
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _signal_group(proc, sig: int) -> None:
        """Signal the child's whole process group (it leads its own session)."""
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            # Group already gone (or unsupported) — fall back to the child.
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    async def _kill_and_reap(self, proc) -> None:
        """Terminate the child process group and always reap the child.

        SIGTERM the group, wait a bounded grace period, escalate to SIGKILL,
        then ``await proc.wait()`` unconditionally so the child can never be
        left running or as a zombie.
        """
        if proc.returncode is None:
            self._signal_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.kill_grace_seconds)
            except asyncio.TimeoutError:
                self._signal_group(proc, signal.SIGKILL)
        await proc.wait()

    @staticmethod
    async def _collect(proc, cap: int = MAX_CAPTURE_BYTES) -> Tuple[bytes, int]:
        """Drain the child's stdout with a hard byte cap, then reap it.

        Bytes beyond ``cap`` are discarded (bounded memory) while the stream
        keeps draining so the child never blocks on a full pipe. Returns
        ``(captured_bytes, returncode)``.
        """
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            if total < cap:
                chunks.append(chunk[: cap - total])
            total += len(chunk)
        returncode = await proc.wait()
        return b"".join(chunks), returncode

    async def _run_async(self, argv: List[str], timeout: float, *,
                         include_turns: bool) -> Tuple[bytes, int]:
        """Run one bounded, cancellable child process to completion.

        On timeout or cancellation the process group is SIGTERM'd, escalated
        to SIGKILL after a bounded grace, and the child is reaped before the
        error propagates — the caller (and the gateway's concurrency slot)
        only moves on once the child is fully cleaned up. The reap is
        shielded so a second cancellation cannot orphan it.

        Raises :class:`asyncio.TimeoutError` / :class:`asyncio.CancelledError`
        after cleanup; ``OSError`` if the spawn itself fails.
        """
        proc = await self._spawn(argv, include_turns=include_turns)
        try:
            return await asyncio.wait_for(self._collect(proc), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.shield(self._kill_and_reap(proc))
            raise
        except BaseException:
            await asyncio.shield(self._kill_and_reap(proc))
            raise

    # -- public tools ---------------------------------------------------------

    async def ask_async(self, question: str, context: Optional[str] = None) -> HermesResult:
        """Ask the live local Hermes agent one bounded question (cancellable).

        This is the serving path used by the MCP server: fully async, hard
        timeout, and on timeout/cancel the child process group is killed and
        reaped before returning/propagating.
        """
        start = time.monotonic()
        self._call_count += 1

        prompt = self._build_prompt(question, context)
        if prompt is None:
            return HermesResult("", False, 0.0, error="empty_question")

        try:
            stdout, returncode = await self._run_async(
                self._ask_argv(prompt), self.ask_timeout_seconds, include_turns=True)
        except asyncio.TimeoutError:
            return HermesResult("", False, (time.monotonic() - start) * 1000.0,
                               error="timeout")
        except OSError:
            return HermesResult("", False, (time.monotonic() - start) * 1000.0,
                               error="spawn_failed")

        elapsed_ms = (time.monotonic() - start) * 1000.0
        if returncode != 0:
            # Never surface stderr — it may carry paths/config detail.
            return HermesResult("", False, elapsed_ms,
                               error=f"agent_exit_{int(returncode)}")

        answer = _clean_output(stdout.decode("utf-8", errors="replace"))
        if not answer:
            return HermesResult("", False, elapsed_ms, error="empty_response")

        truncated = len(answer) > MAX_ANSWER_LEN
        if truncated:
            answer = answer[:MAX_ANSWER_LEN]
        return HermesResult(answer, True, elapsed_ms, truncated=truncated)

    async def status_async(self) -> Dict[str, object]:
        """Non-sensitive service status (cancellable serving path)."""
        self._call_count += 1
        argv = [str(self.hermes_bin), "status"]
        ok = False
        lines: List[str] = []
        error: Optional[str] = None
        try:
            stdout, returncode = await self._run_async(
                argv, self.status_timeout_seconds, include_turns=False)
            ok = returncode == 0
            if not ok:
                error = f"status_exit_{int(returncode)}"
            lines = _filter_status_lines(stdout.decode("utf-8", errors="replace"))
        except asyncio.TimeoutError:
            error = "timeout"
        except OSError:
            error = "spawn_failed"
        return self._status_payload(ok, error, lines)

    def ask(self, question: str, context: Optional[str] = None) -> HermesResult:
        """Ask the live local Hermes agent one bounded question (sync).

        CLI/test convenience boundary only — the MCP server uses
        :meth:`ask_async`, which is cancellable and kills+reaps the child
        process group on timeout/cancel.
        """
        start = time.monotonic()
        self._call_count += 1

        prompt = self._build_prompt(question, context)
        if prompt is None:
            return HermesResult("", False, 0.0, error="empty_question")

        argv = self._ask_argv(prompt)

        try:
            proc = self._run(argv, self.ask_timeout_seconds, include_turns=True)
        except subprocess.TimeoutExpired:
            return HermesResult("", False, (time.monotonic() - start) * 1000.0,
                               error="timeout")
        except OSError:
            return HermesResult("", False, (time.monotonic() - start) * 1000.0,
                               error="spawn_failed")

        elapsed_ms = (time.monotonic() - start) * 1000.0
        if proc.returncode != 0:
            # Never surface stderr — it may carry paths/config detail.
            return HermesResult("", False, elapsed_ms,
                               error=f"agent_exit_{int(proc.returncode)}")

        answer = _clean_output(proc.stdout or "")
        if not answer:
            return HermesResult("", False, elapsed_ms, error="empty_response")

        truncated = len(answer) > MAX_ANSWER_LEN
        if truncated:
            answer = answer[:MAX_ANSWER_LEN]
        return HermesResult(answer, True, elapsed_ms, truncated=truncated)

    def status(self) -> Dict[str, object]:
        """Non-sensitive service status backed by ``hermes status`` only (sync).

        CLI/test convenience boundary only — the MCP server uses
        :meth:`status_async`.
        """
        self._call_count += 1
        argv = [str(self.hermes_bin), "status"]
        ok = False
        lines: List[str] = []
        error: Optional[str] = None
        try:
            proc = self._run(argv, self.status_timeout_seconds, include_turns=False)
            ok = proc.returncode == 0
            if not ok:
                error = f"status_exit_{int(proc.returncode)}"
            lines = _filter_status_lines(proc.stdout or "")
        except subprocess.TimeoutExpired:
            error = "timeout"
        except OSError:
            error = "spawn_failed"
        return self._status_payload(ok, error, lines)

    def _status_payload(self, ok: bool, error: Optional[str],
                        lines: List[str]) -> Dict[str, object]:
        return {
            "service": self.name,
            "ok": ok,
            "error": error,
            "backend": "hermes-cli",
            "calls": self._call_count,
            "tools": ["hermes_ask", "hermes_status"],
            "exec": False,  # explicitly advertise: no execution surface
            "api": False,   # explicitly advertise: no generic Hermes API surface
            "status_lines": lines,
        }
