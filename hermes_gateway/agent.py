"""Hermes — adapter to the real, local Hermes agent.

This module is the *only* place the Hermes gateway pulls business logic from.
It routes ``hermes_ask`` to the live local Hermes agent, and ``hermes_status``
to ``hermes status``.

Ask path (preferred): a **bridge-owned warm worker** subprocess that preloads
Hermes plugins/MCP once, then answers each ask over a local Unix-domain
JSON-line socket. Cold ``hermes --oneshot`` with full plugins exceeds Grok
Bot's ~60s MCP client timeout; the warm worker keeps asks in the few-second
range after preload.

Fallback: bounded ``hermes [--safe-mode] --oneshot=<prompt>`` when the worker
is unavailable or ``HERMES_BRIDGE_ASK_MODE=oneshot``. Safe-mode is optional
(default on for oneshot fallback) so trivial pings stay under the client
budget; it disables plugins/MCP (breaks iMessage OTP etc.).

Shared safety invariants:

  * only a known local Hermes binary is ever executed (fail-closed when it is
    missing or not executable);
  * no ``shell=True`` anywhere; oneshot carries the question as a single
    ``--oneshot=<question>`` argv element;
  * the child environment is built explicitly (never inherited wholesale);
  * hard timeout on every ask; kill/reap on timeout or cancellation;
  * output is cleaned (ANSI/control stripped) and bounded;
  * no SSH, no shell tool, no generic Hermes API is reachable from here.

The **serving path is async and cancellable**: the MCP server calls
:meth:`HermesAgent.ask_async` / :meth:`HermesAgent.status_async`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

__all__ = [
    "HermesAgent",
    "HermesResult",
    "HermesUnavailableError",
    "resolve_hermes_binary",
    "resolve_hermes_home",
    "resolve_hermes_python",
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
    "ASK_MODES",
]

logger = logging.getLogger("hermes_gateway.agent")

SESSION_SOURCE = "grokbot"

# Default ask budget fits under Grok Bot's ~60s MCP client timeout after warm
# preload. Operators can raise via HERMES_BRIDGE_ASK_TIMEOUT_SECONDS (max 180).
ASK_TIMEOUT_SECONDS = 55.0
STATUS_TIMEOUT_SECONDS = 30.0
KILL_GRACE_SECONDS = 5.0
MAX_CAPTURE_BYTES = 262_144

MIN_TURNS = 1
MAX_TURNS = 30
DEFAULT_MAX_TURNS = 10

MAX_QUESTION_LEN = 4000
MAX_CONTEXT_LEN = 8000
MAX_ANSWER_LEN = 8000

ASK_MODES = frozenset({"auto", "worker", "oneshot"})
DEFAULT_ASK_MODE = "auto"
WORKER_START_TIMEOUT_SECONDS = 90.0
WORKER_PING_TIMEOUT_SECONDS = 5.0

_HERMES_BASENAME = "hermes"

_KNOWN_HERMES_LOCATIONS = (
    "/usr/local/bin/hermes",
    "/usr/bin/hermes",
    "/opt/hermes/bin/hermes",
)

# Minimal PATH for oneshot children; worker gets a richer PATH (see below).
_SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-_]"
)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_SENSITIVE_LINE_RE = re.compile(
    r"(?i)(key|token|secret|password|passwd|bearer|authorization|credential"
    r"|cookie|api[_-]?key|https?://|ssh|@|/home/|/root/|/etc/|hermes_home)"
)
_MAX_STATUS_LINES = 40
_MAX_STATUS_LINE_LEN = 200

_EXEC_PYTHON_RE = re.compile(
    r'exec\s+"([^"]+/python[^"]*)"\s+"([^"]+)"',
    re.IGNORECASE,
)


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
    """Resolve the known local Hermes binary, fail-closed."""
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


def resolve_hermes_python(hermes_bin: Path) -> Tuple[Path, Path]:
    """Resolve ``(python, hermes_agent_root)`` from a Hermes CLI launcher.

    Supports:

    * bash wrappers that ``exec "/path/to/python" "/path/to/hermes-agent/hermes"``;
    * setuptools console scripts under ``.../venv/bin/hermes`` (shebang + import);
    * optional override ``HERMES_BRIDGE_HERMES_PYTHON``.
    """
    override = (os.environ.get("HERMES_BRIDGE_HERMES_PYTHON") or "").strip()
    try:
        text = hermes_bin.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HermesUnavailableError(f"cannot read hermes launcher: {e}") from e

    match = _EXEC_PYTHON_RE.search(text)
    if match:
        py = Path(match.group(1))
        entry = Path(match.group(2))
        if py.is_file() and os.access(py, os.X_OK) and entry.is_file():
            return py, entry.parent

    # Console-script shebang: #!/path/to/venv/bin/python3
    shebang_py: Optional[Path] = None
    first = text.splitlines()[0] if text else ""
    if first.startswith("#!"):
        cand = Path(first[2:].strip().split()[0])
        if cand.is_file() and os.access(cand, os.X_OK):
            shebang_py = cand

    py = Path(override) if override else shebang_py
    if py is None:
        py = Path(sys_executable())
    if not py.is_file() or not os.access(py, os.X_OK):
        raise HermesUnavailableError(f"Hermes Python is not executable: {py}")

    # Prefer the checkout/install root that contains hermes_cli (editable or src).
    candidates = [
        hermes_bin.parent,  # .../hermes-agent if binary sits at repo root
        hermes_bin.parent.parent.parent,  # .../venv/bin/hermes → hermes-agent
        Path(os.environ.get("HERMES_BRIDGE_HERMES_AGENT_ROOT") or ""),
    ]
    for root in candidates:
        if root and (root / "hermes_cli").is_dir():
            return py, root

    # Last resort: ask that interpreter where hermes_cli lives.
    try:
        probe = subprocess.run(
            [str(py), "-c",
             "import hermes_cli, pathlib; print(pathlib.Path(hermes_cli.__file__).resolve().parent.parent)"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if probe.returncode == 0:
            root = Path(probe.stdout.strip())
            if root.is_dir() and (root / "hermes_cli").is_dir():
                return py, root
    except (OSError, subprocess.TimeoutExpired):
        pass

    raise HermesUnavailableError(
        "cannot resolve Hermes agent root from launcher "
        f"(no hermes_cli beside {hermes_bin} or importable via {py})"
    )


def sys_executable() -> str:
    import sys
    return sys.executable


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


def _normalize_ask_mode(value: object) -> str:
    if not isinstance(value, str):
        return DEFAULT_ASK_MODE
    mode = value.strip().lower()
    return mode if mode in ASK_MODES else DEFAULT_ASK_MODE


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


class _WarmWorker:
    """Lifecycle + IPC for one bridge-owned warm Hermes worker process."""

    def __init__(
        self,
        *,
        hermes_bin: Path,
        hermes_home: Path,
        socket_path: Path,
        worker_script: Path,
        max_turns: int,
        start_timeout: float,
        ask_timeout: float,
        kill_grace: float,
        spawner: Optional[Callable] = None,
    ):
        self.hermes_bin = hermes_bin
        self.hermes_home = hermes_home
        self.socket_path = socket_path
        self.worker_script = worker_script
        self.max_turns = max_turns
        self.start_timeout = start_timeout
        self.ask_timeout = ask_timeout
        self.kill_grace = kill_grace
        self._spawner = spawner
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._ready = False
        self._req_counter = 0
        self._sync_lock = threading.Lock()

    def _worker_env(self) -> Dict[str, str]:
        home = os.environ.get("HOME", str(Path.home()))
        local_bin = str(Path(home) / ".local" / "bin")
        hermes_node = str(self.hermes_home / "node" / "bin")
        path_parts = [
            hermes_node,
            local_bin,
            "/usr/local/bin",
            "/opt/homebrew/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        env: Dict[str, str] = {
            "PATH": ":".join(path_parts),
            "HOME": home,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": "dumb",
            "NO_COLOR": "1",
            "HERMES_HOME": str(self.hermes_home),
            "HERMES_SESSION_SOURCE": SESSION_SOURCE,
            "HERMES_MAX_ITERATIONS": str(self.max_turns),
            "HERMES_YOLO_MODE": "1",
            "HERMES_ACCEPT_HOOKS": "1",
        }
        # Let Hermes resolve provider credentials the same way an interactive
        # session would, without dumping the whole parent environment.
        for key in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "TMPDIR",
            "USER",
            "LOGNAME",
        ):
            val = os.environ.get(key)
            if val:
                env[key] = val
        return env

    async def ensure_started(self) -> bool:
        async with self._lock:
            if self._ready and self._proc is not None and self._proc.returncode is None:
                if await self._ping_unlocked():
                    return True
                await self._stop_unlocked()
            return await self._start_unlocked()

    async def _start_unlocked(self) -> bool:
        try:
            hermes_python, agent_root = resolve_hermes_python(self.hermes_bin)
        except HermesUnavailableError as e:
            logger.warning("warm worker: cannot resolve hermes python: %s", e)
            return False

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        ready_file = self.socket_path.with_suffix(self.socket_path.suffix + ".ready")
        if ready_file.exists():
            try:
                ready_file.unlink()
            except OSError:
                pass

        bridge_root = Path(__file__).resolve().parent.parent
        argv = [
            str(hermes_python),
            "-m",
            "hermes_gateway.warm_worker",
            "--socket",
            str(self.socket_path),
            "--ready-file",
            str(ready_file),
        ]
        env = self._worker_env()
        # Bridge root first so ``-m hermes_gateway.warm_worker`` resolves; hermes-agent
        # next so its top-level ``agent`` / ``tools`` packages win over any shadowing.
        env["PYTHONPATH"] = os.pathsep.join(
            [str(bridge_root), str(agent_root)]
            + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )

        spawner = self._spawner or asyncio.create_subprocess_exec
        try:
            self._proc = await spawner(
                *argv,
                env=env,
                cwd=str(bridge_root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            logger.warning("warm worker spawn failed: %s", e)
            self._proc = None
            self._ready = False
            return False

        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if self._proc.returncode is not None:
                logger.warning(
                    "warm worker exited during start (code=%s)", self._proc.returncode
                )
                self._ready = False
                return False
            if ready_file.is_file() and self.socket_path.exists():
                if await self._ping_unlocked():
                    self._ready = True
                    logger.info("warm worker ready on %s", self.socket_path)
                    return True
            await asyncio.sleep(0.2)

        logger.warning("warm worker start timed out after %.0fs", self.start_timeout)
        await self._stop_unlocked()
        return False

    async def _ping_unlocked(self) -> bool:
        try:
            resp = await self._transact_unlocked(
                {"op": "ping"}, timeout=WORKER_PING_TIMEOUT_SECONDS
            )
        except Exception:
            return False
        return bool(resp.get("ok") and resp.get("answer") == "pong")

    async def ask(self, prompt: str, timeout: float) -> HermesResult:
        start = time.monotonic()
        async with self._lock:
            if not (self._ready and self._proc and self._proc.returncode is None):
                return HermesResult(
                    "", False, (time.monotonic() - start) * 1000.0,
                    error="worker_unavailable",
                )
            try:
                resp = await self._transact_unlocked(
                    {"op": "ask", "prompt": prompt}, timeout=timeout
                )
            except asyncio.TimeoutError:
                # Worker may be wedged on a long tool call — restart next time.
                self._ready = False
                await self._stop_unlocked()
                return HermesResult(
                    "", False, (time.monotonic() - start) * 1000.0, error="timeout"
                )
            except OSError:
                self._ready = False
                return HermesResult(
                    "", False, (time.monotonic() - start) * 1000.0,
                    error="worker_ipc_failed",
                )

        elapsed_ms = float(resp.get("elapsed_ms") or (time.monotonic() - start) * 1000.0)
        if not resp.get("ok"):
            return HermesResult(
                "", False, elapsed_ms, error=str(resp.get("error") or "worker_error")
            )
        answer = _clean_output(str(resp.get("answer") or ""))
        if not answer:
            return HermesResult("", False, elapsed_ms, error="empty_response")
        truncated = bool(resp.get("truncated")) or len(answer) > MAX_ANSWER_LEN
        if len(answer) > MAX_ANSWER_LEN:
            answer = answer[:MAX_ANSWER_LEN]
        return HermesResult(answer, True, elapsed_ms, truncated=truncated)

    async def _transact_unlocked(
        self, payload: Dict[str, object], *, timeout: float
    ) -> Dict[str, object]:
        self._req_counter += 1
        req = {"v": 1, "id": str(self._req_counter), **payload}
        line = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")

        def _sync_roundtrip() -> Dict[str, object]:
            with self._sync_lock:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.settimeout(timeout)
                    sock.connect(str(self.socket_path))
                    sock.sendall(line)
                    buf = b""
                    while b"\n" not in buf:
                        chunk = sock.recv(65536)
                        if not chunk:
                            raise OSError("worker closed connection")
                        buf += chunk
                        if len(buf) > MAX_CAPTURE_BYTES:
                            raise OSError("worker response too large")
                    raw = buf.split(b"\n", 1)[0]
                    data = json.loads(raw.decode("utf-8"))
                    if not isinstance(data, dict):
                        raise OSError("worker response not an object")
                    return data
                finally:
                    try:
                        sock.close()
                    except OSError:
                        pass

        return await asyncio.wait_for(asyncio.to_thread(_sync_roundtrip), timeout=timeout)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        self._ready = False
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.send_signal(signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.kill_grace)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.kill()
                    except (ProcessLookupError, OSError):
                        pass
                await proc.wait()
        for path in (self.socket_path, self.socket_path.with_suffix(self.socket_path.suffix + ".ready")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


class HermesAgent:
    """Adapter from the two Hermes tools to the live local Hermes agent.

    Construction is fail-closed: it raises :class:`HermesUnavailableError`
    when the Hermes binary is missing/non-executable or HERMES_HOME is
    invalid, so a misconfigured gateway never starts serving.

    ``runner`` is the synchronous process boundary (``subprocess.run``-
    compatible; CLI/test convenience only). ``spawner`` is the async process
    boundary used by the serving path (``asyncio.create_subprocess_exec``-
    compatible); both are injectable for tests. ``worker`` may be injected to
    mock the warm-worker boundary.
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
        ask_mode: object = DEFAULT_ASK_MODE,
        oneshot_safe_mode: object = True,
        worker_socket: Optional[str] = None,
        worker_start_timeout_seconds: object = WORKER_START_TIMEOUT_SECONDS,
        runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
        spawner: Optional[Callable] = None,
        worker: Optional[_WarmWorker] = None,
    ):
        self.name = name
        self.hermes_bin = resolve_hermes_binary(hermes_bin)
        self.hermes_home = resolve_hermes_home(hermes_home)
        self.max_turns = clamp_turns(max_turns)
        self.ask_timeout_seconds = _clamp_timeout(
            ask_timeout_seconds, ASK_TIMEOUT_SECONDS, 180.0)
        self.status_timeout_seconds = _clamp_timeout(
            status_timeout_seconds, STATUS_TIMEOUT_SECONDS, 180.0)
        self.kill_grace_seconds = _clamp_timeout(
            kill_grace_seconds, KILL_GRACE_SECONDS, 30.0)
        self.ask_mode = _normalize_ask_mode(ask_mode)
        if isinstance(oneshot_safe_mode, str):
            self.oneshot_safe_mode = oneshot_safe_mode.strip().lower() not in (
                "0", "false", "no", "off",
            )
        else:
            self.oneshot_safe_mode = bool(oneshot_safe_mode)
        self._runner = runner
        self._spawner = spawner
        self._call_count = 0

        default_sock = self.hermes_home / "run" / "grokbot-hermes-worker.sock"
        sock = Path(worker_socket) if worker_socket else default_sock
        worker_script = Path(__file__).resolve().parent / "warm_worker.py"
        self._worker = worker or _WarmWorker(
            hermes_bin=self.hermes_bin,
            hermes_home=self.hermes_home,
            socket_path=sock,
            worker_script=worker_script,
            max_turns=self.max_turns,
            start_timeout=_clamp_timeout(
                worker_start_timeout_seconds, WORKER_START_TIMEOUT_SECONDS, 300.0),
            ask_timeout=self.ask_timeout_seconds,
            kill_grace=self.kill_grace_seconds,
            spawner=spawner,
        )

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
        """Normalise and bound the (question, context) pair into one prompt."""
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

    def _ask_argv(self, prompt: str, *, safe_mode: Optional[bool] = None) -> List[str]:
        # Fixed argv: binary + optional --safe-mode + one --oneshot=<prompt>.
        # The prompt is a single argv element; it can never become a flag,
        # an extra argument, or shell input.
        use_safe = self.oneshot_safe_mode if safe_mode is None else safe_mode
        argv = [str(self.hermes_bin)]
        if use_safe:
            argv.append("--safe-mode")
        argv.append("--oneshot=" + prompt)
        return argv

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
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _signal_group(proc, sig: int) -> None:
        """Signal the child's whole process group (it leads its own session)."""
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    async def _kill_and_reap(self, proc) -> None:
        """Terminate the child process group and always reap the child."""
        if proc.returncode is None:
            self._signal_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.kill_grace_seconds)
            except asyncio.TimeoutError:
                self._signal_group(proc, signal.SIGKILL)
        await proc.wait()

    @staticmethod
    async def _collect(proc, cap: int = MAX_CAPTURE_BYTES) -> Tuple[bytes, int]:
        """Drain the child's stdout with a hard byte cap, then reap it."""
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
        """Run one bounded, cancellable child process to completion."""
        proc = await self._spawn(argv, include_turns=include_turns)
        try:
            return await asyncio.wait_for(self._collect(proc), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.shield(self._kill_and_reap(proc))
            raise
        except BaseException:
            await asyncio.shield(self._kill_and_reap(proc))
            raise

    async def _ask_via_oneshot(self, prompt: str) -> HermesResult:
        start = time.monotonic()
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
            return HermesResult("", False, elapsed_ms,
                               error=f"agent_exit_{int(returncode)}")

        answer = _clean_output(stdout.decode("utf-8", errors="replace"))
        if not answer:
            return HermesResult("", False, elapsed_ms, error="empty_response")

        truncated = len(answer) > MAX_ANSWER_LEN
        if truncated:
            answer = answer[:MAX_ANSWER_LEN]
        return HermesResult(answer, True, elapsed_ms, truncated=truncated)

    async def _ask_via_worker_or_fallback(self, prompt: str) -> HermesResult:
        mode = self.ask_mode
        if mode == "oneshot":
            return await self._ask_via_oneshot(prompt)

        started = await self._worker.ensure_started()
        if started:
            result = await self._worker.ask(prompt, self.ask_timeout_seconds)
            if result.ok or result.error not in (
                "worker_unavailable", "worker_ipc_failed",
            ):
                return result
            logger.warning("warm worker ask failed (%s); falling back", result.error)
        elif mode == "worker":
            return HermesResult("", False, 0.0, error="worker_unavailable")
        else:
            logger.info("warm worker unavailable; falling back to oneshot")

        if mode == "worker":
            return HermesResult("", False, 0.0, error="worker_unavailable")
        return await self._ask_via_oneshot(prompt)

    # -- public tools ---------------------------------------------------------

    async def ask_async(self, question: str, context: Optional[str] = None) -> HermesResult:
        """Ask the live local Hermes agent one bounded question (cancellable)."""
        start = time.monotonic()
        self._call_count += 1

        prompt = self._build_prompt(question, context)
        if prompt is None:
            return HermesResult("", False, 0.0, error="empty_question")

        result = await self._ask_via_worker_or_fallback(prompt)
        # Preserve outer elapsed if the inner path reported 0 on early failure.
        if result.elapsed_ms <= 0 and result.error and result.error != "empty_question":
            result.elapsed_ms = (time.monotonic() - start) * 1000.0
        return result

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
        :meth:`ask_async`. Sync path always uses oneshot (no worker loop).
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
        """Non-sensitive service status backed by ``hermes status`` only (sync)."""
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
        backend = "hermes-worker" if self.ask_mode != "oneshot" else "hermes-cli"
        return {
            "service": self.name,
            "ok": ok,
            "error": error,
            "backend": backend,
            "ask_mode": self.ask_mode,
            "calls": self._call_count,
            "tools": ["hermes_ask", "hermes_status"],
            "exec": False,
            "api": False,
            "status_lines": lines,
        }

    async def aclose(self) -> None:
        """Stop the warm worker if this agent owns one."""
        await self._worker.stop()
