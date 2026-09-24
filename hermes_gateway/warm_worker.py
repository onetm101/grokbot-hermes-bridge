"""Bridge-owned Hermes warm worker (JSON-line Unix-domain socket).

Runs under the *Hermes* interpreter (not the bridge venv) so it can import
``hermes_cli`` / plugins. The bridge gateway spawns this once, preloads
plugins + MCP discovery, then multiplexes ``hermes_ask`` prompts over a
local socket — avoiding a cold ``hermes --oneshot`` per request (which
exceeds Grok Bot's ~60s MCP client timeout when plugins load).

Protocol (one JSON object per line, UTF-8):

  request:  {"v":1,"id":"<str>","op":"ask"|"ping"|"shutdown","prompt":"..."}
  response: {"v":1,"id":"<str>","ok":bool,"answer":str|null,"error":str|null,
             "elapsed_ms":float}

``ask`` uses Hermes oneshot internals (``hermes_cli.oneshot._run_agent``) with
YOLO / accept-hooks set, same contract as ``hermes -z``. No official
persistent-oneshot CLI exists; this worker is the bridge-owned substitute.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

PROTOCOL_VERSION = 1

def _scrub_sys_path() -> None:
    """Running as a file puts ``hermes_gateway/`` on ``sys.path[0]``, which
    shadows Hermes's top-level ``agent`` package with this bridge's
    ``hermes_gateway.agent`` module. Prefer ``python -m hermes_gateway.warm_worker``;
    still defend if invoked as a script.
    """
    script_dir = str(Path(__file__).resolve().parent)
    while script_dir in sys.path:
        sys.path.remove(script_dir)
    # Drop empty '' entry that also means cwd.
    while "" in sys.path:
        sys.path.remove("")


MAX_LINE_BYTES = 1_048_576
MAX_ANSWER_CHARS = 8000

_log = logging.getLogger("hermes_gateway.warm_worker")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _preload() -> None:
    _scrub_sys_path()
    """Load plugins + MCP once so subsequent asks stay warm."""
    os.environ.setdefault("HERMES_YOLO_MODE", "1")
    os.environ.setdefault("HERMES_ACCEPT_HOOKS", "1")
    from hermes_cli.plugins import discover_plugins
    from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build

    t0 = time.monotonic()
    discover_plugins()
    ensure_mcp_discovery_before_agent_build(
        logger=_log, single_query=True,
    )
    _log.info("warm_worker preload done in %.1fs", time.monotonic() - t0)


def _run_ask(prompt: str) -> Dict[str, Any]:
    """Execute one oneshot-equivalent turn; return answer/error fields."""
    from hermes_cli.oneshot import _run_agent

    os.environ["HERMES_YOLO_MODE"] = "1"
    os.environ["HERMES_ACCEPT_HOOKS"] = "1"
    t0 = time.monotonic()
    try:
        response, result = _run_agent(prompt)
    except BaseException as exc:  # noqa: BLE001 — surface as bounded error
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return {
            "ok": False,
            "answer": None,
            "error": "agent_failed",
            "elapsed_ms": elapsed_ms,
            "detail": type(exc).__name__,
        }
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    answer = (response or "").strip()
    if not answer:
        err = "empty_response"
        if result.get("failed") or result.get("partial"):
            err = "agent_failed"
        return {
            "ok": False,
            "answer": None,
            "error": err,
            "elapsed_ms": elapsed_ms,
        }
    truncated = len(answer) > MAX_ANSWER_CHARS
    if truncated:
        answer = answer[:MAX_ANSWER_CHARS]
    return {
        "ok": True,
        "answer": answer,
        "error": None,
        "elapsed_ms": elapsed_ms,
        "truncated": truncated,
    }


def _handle(req: Dict[str, Any]) -> Dict[str, Any]:
    rid = str(req.get("id") or "")
    op = str(req.get("op") or "")
    base: Dict[str, Any] = {"v": PROTOCOL_VERSION, "id": rid}

    if op == "ping":
        base.update(ok=True, answer="pong", error=None, elapsed_ms=0.0)
        return base
    if op == "shutdown":
        base.update(ok=True, answer="bye", error=None, elapsed_ms=0.0)
        return base
    if op != "ask":
        base.update(ok=False, answer=None, error="unknown_op", elapsed_ms=0.0)
        return base

    prompt = req.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        base.update(ok=False, answer=None, error="empty_question", elapsed_ms=0.0)
        return base

    outcome = _run_ask(prompt.strip())
    base.update(outcome)
    return base


def _serve_client(conn: socket.socket, stop: threading.Event) -> None:
    buf = b""
    try:
        conn.settimeout(1.0)
        while not stop.is_set():
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_LINE_BYTES:
                _log.warning("client line exceeded cap; closing")
                break
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                req: Optional[Dict[str, Any]] = None
                try:
                    parsed = json.loads(line.decode("utf-8"))
                    if not isinstance(parsed, dict):
                        raise ValueError("not an object")
                    req = parsed
                    resp = _handle(req)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    resp = {
                        "v": PROTOCOL_VERSION,
                        "id": "",
                        "ok": False,
                        "answer": None,
                        "error": "bad_request",
                        "elapsed_ms": 0.0,
                    }
                try:
                    conn.sendall(
                        (json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")
                    )
                except OSError:
                    return
                if req is not None and str(req.get("op") or "") == "shutdown":
                    stop.set()
                    return
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    _scrub_sys_path()
    parser = argparse.ArgumentParser(description="GrokBot Hermes warm worker")
    parser.add_argument("--socket", required=True, help="Unix domain socket path")
    parser.add_argument(
        "--ready-file",
        default="",
        help="Optional path touched after preload + bind (for the bridge)",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    sock_path = Path(args.socket)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass

    try:
        _preload()
    except Exception:
        _log.error("preload failed:\n%s", traceback.format_exc())
        return 2

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    try:
        os.chmod(sock_path, 0o600)
    except OSError:
        pass
    server.listen(8)
    server.settimeout(1.0)

    stop = threading.Event()

    def _on_signal(signum, _frame):
        _log.info("signal %s; shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if args.ready_file:
        ready = Path(args.ready_file)
        ready.parent.mkdir(parents=True, exist_ok=True)
        ready.write_text(str(os.getpid()), encoding="utf-8")

    # Announce readiness on stdout for bridge waiters that prefer a pipe signal.
    sys.stdout.write(f"ready pid={os.getpid()} socket={sock_path}\n")
    sys.stdout.flush()
    _log.info("listening on %s", sock_path)

    # Serialize asks: Hermes agent construction is not assumed thread-safe.
    client_lock = threading.Lock()

    try:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if stop.is_set():
                    break
                raise

            def _run(c=conn):
                with client_lock:
                    _serve_client(c, stop)

            threading.Thread(target=_run, daemon=True).start()
    finally:
        try:
            server.close()
        except OSError:
            pass
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass
        if args.ready_file:
            try:
                Path(args.ready_file).unlink(missing_ok=True)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
