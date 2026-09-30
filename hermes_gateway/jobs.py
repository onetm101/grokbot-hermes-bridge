"""Async Hermes jobs for work that exceeds the MCP client timeout.

``hermes_ask`` stays synchronous and short. Long Mac/browser asks are
enqueued here and return immediately. A single in-process runner then calls
the same :meth:`hermes_gateway.agent.HermesAgent.ask_async` path (warm-worker
IPC or oneshot fallback) with a longer server-side timeout.

The warm-worker protocol is still one blocking JSON line per ask. This module
does not add a worker opcode and does not run shell commands.

One long job runs at a time. Further jobs stay ``queued``. State is one JSON
file per job under ``$HERMES_HOME/run/jobs`` (directory mode 0700, files mode
0600). Files older than 24 hours are removed. Job files and status payloads
store a short redacted question summary, never the raw question, context,
passwords, tokens, or SMS bodies.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .agent import MAX_ANSWER_LEN

__all__ = [
    "JOB_TTL_SECONDS",
    "MAX_ACTIVE_JOBS",
    "JobRunner",
    "JobStore",
    "question_summary",
    "redact_sensitive",
]

logger = logging.getLogger("hermes_gateway.jobs")

JOB_TTL_SECONDS = 24 * 60 * 60
MAX_ACTIVE_JOBS = 16
_SUMMARY_LEN = 160
_MAX_JOB_TIMEOUT_SECONDS = 7200.0

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ERROR_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Credential assignments, bearer values, and well-known token prefixes.
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|bearer)"
    r"(\s*[:=]\s*)(\S+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{8,}")
_TOKEN_PREFIX_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{8,}|AKIA[0-9A-Z]{16})\b"
)
# SMS / iMessage bodies after a label. The instruction prefix stays; the body
# does not. A blank line ends the body so a following task sentence can stay.
_SMS_BODY_RE = re.compile(
    r"(?is)(\b(?:sms|imessage|text message|message body)\b(?:\s+\w+){0,6}\s*[:=]\s*)"
    r"(.+?)(?=\n\s*\n|\Z)"
)
_SMS_BLOCK_RE = re.compile(
    r"(?is)(^[ \t]*(?:sms|imessage|text message|message body)\b[^\n]*\n)"
    r"(.+?)(?=\n\s*\n|\Z)",
    re.MULTILINE,
)
_OTP_RE = re.compile(
    r"(?i)\b((?:otp|verification code|sms code|security code|login code|"
    r"one[- ]time (?:code|password)|passcode|(?:the )?code)"
    r"\s*(?:is|was|:)?\s*)(\d{4,8})\b"
)


def redact_sensitive(text: str) -> str:
    """Remove passwords, tokens, and SMS bodies from text that may be stored."""
    if not text:
        return ""
    redacted = _SMS_BLOCK_RE.sub(lambda m: m.group(1) + "<redacted>", text)
    redacted = _SMS_BODY_RE.sub(lambda m: m.group(1) + "<redacted>", redacted)
    redacted = _OTP_RE.sub(lambda m: m.group(1) + "<redacted>", redacted)
    redacted = _ASSIGNMENT_RE.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", redacted)
    redacted = _BEARER_RE.sub(lambda m: m.group(1) + "<redacted>", redacted)
    redacted = _TOKEN_PREFIX_RE.sub("<redacted>", redacted)
    return redacted


def question_summary(question: str, limit: int = _SUMMARY_LEN) -> str:
    """Short, non-secret description of a question for the job file."""
    text = redact_sensitive(question or "").replace("\n", " ")
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text or "(empty)"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: object) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _valid_job_id(job_id: object) -> bool:
    return isinstance(job_id, str) and bool(_JOB_ID_RE.match(job_id))


def _safe_error(error: object) -> str:
    if isinstance(error, str) and _ERROR_RE.match(error):
        return error
    return "job_failed"


def _sanitize_label(label: object) -> Optional[str]:
    if not isinstance(label, str):
        return None
    text = redact_sensitive(label)
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or "<redacted>" in text:
        return None
    return text[:64]


def _bound_answer(answer: str, already_truncated: bool) -> tuple[str, bool]:
    cleaned = redact_sensitive(answer or "")
    truncated = already_truncated or len(cleaned) > MAX_ANSWER_LEN
    if len(cleaned) > MAX_ANSWER_LEN:
        cleaned = cleaned[:MAX_ANSWER_LEN]
    return cleaned, truncated


def _fail(error: str, job_id: Optional[str] = None, result_path: Optional[str] = None) -> dict[str, Any]:
    return {
        "ok": False,
        "job_id": job_id,
        "status": "failed",
        "error": error,
        "result_path": result_path,
    }


def _public_view(record: dict[str, Any], result_path: Path) -> dict[str, Any]:
    status = str(record.get("status") or "failed")
    if status not in ("queued", "running", "done", "failed"):
        status = "failed"
    view: dict[str, Any] = {
        "ok": status != "failed",
        "job_id": record.get("id"),
        "status": status,
        "result_path": str(result_path),
    }
    if record.get("started_at"):
        view["started_at"] = record["started_at"]
    if record.get("finished_at"):
        view["finished_at"] = record["finished_at"]
    if status == "done":
        view["answer"] = record.get("answer") or ""
        view["error"] = None
        view["elapsed_ms"] = record.get("elapsed_ms")
        view["truncated"] = bool(record.get("truncated"))
    elif status == "failed":
        view["error"] = _safe_error(record.get("error"))
        if record.get("elapsed_ms") is not None:
            view["elapsed_ms"] = record["elapsed_ms"]
    return view


class JobStore:
    """One JSON file per job under a private directory."""

    def __init__(self, root: Path, *, ttl_seconds: float = JOB_TTL_SECONDS):
        self.root = Path(root)
        self.ttl_seconds = float(ttl_seconds)

    def ensure_dir(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def path_for(self, job_id: str) -> Path:
        if not _valid_job_id(job_id):
            raise ValueError("invalid job id")
        return self.root / f"{job_id}.json"

    def read(self, job_id: str) -> Optional[dict[str, Any]]:
        if not _valid_job_id(job_id):
            return None
        path = self.path_for(job_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None
        if not isinstance(data, dict) or data.get("id") != job_id:
            return None
        return data

    def create(self, record: dict[str, Any]) -> None:
        self.ensure_dir()
        self._write(self.path_for(str(record["id"])), record)

    def update(self, job_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        record = self.read(job_id)
        if record is None:
            return None
        record.update(fields)
        record["id"] = job_id
        self._write(self.path_for(job_id), record)
        return record

    def iter_records(self) -> list[tuple[Path, dict[str, Any]]]:
        if not self.root.is_dir():
            return []
        found: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(self.root.glob("*.json")):
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            if isinstance(data, dict) and _valid_job_id(data.get("id")):
                found.append((path, data))
        return found

    def reap(self, *, protect: Optional[set[str]] = None, now: Optional[float] = None) -> int:
        """Delete job files older than the TTL. In-flight ids are kept."""
        if not self.root.is_dir():
            return 0
        keep = protect or set()
        moment = time_now() if now is None else now
        removed = 0
        for path, record in self.iter_records():
            job_id = str(record.get("id"))
            if job_id in keep:
                continue
            created = _parse_utc(record.get("created_at"))
            if created is None:
                try:
                    created = path.stat().st_mtime
                except OSError:
                    continue
            if moment - created > self.ttl_seconds:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def _write(self, path: Path, record: dict[str, Any]) -> None:
        payload = (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        os.chmod(path, 0o600)


def time_now() -> float:
    return datetime.now(timezone.utc).timestamp()


@dataclass
class _Pending:
    job_id: str
    question: str
    context: Optional[str]


class JobRunner:
    """Serial background runner. Enqueue and status return without waiting on Hermes."""

    def __init__(
        self,
        agent: Any,
        jobs_dir: Optional[Path],
        *,
        job_timeout_seconds: float = 1800.0,
        max_active: int = MAX_ACTIVE_JOBS,
        ttl_seconds: float = JOB_TTL_SECONDS,
    ):
        self._agent = agent
        self.store = JobStore(Path(jobs_dir), ttl_seconds=ttl_seconds) if jobs_dir is not None else None
        try:
            timeout = float(job_timeout_seconds)
        except (TypeError, ValueError):
            timeout = 1800.0
        if timeout <= 0:
            timeout = 1800.0
        self.job_timeout_seconds = min(timeout, _MAX_JOB_TIMEOUT_SECONDS)
        self._max_active = max(1, int(max_active))
        self._owned: set[str] = set()
        self._inflight: set[str] = set()
        self._recovered = False
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[_Pending] = asyncio.Queue()
        self._task: Optional[asyncio.Task[None]] = None

    async def enqueue(
        self,
        question: str,
        context: Optional[str] = None,
        job_id: Optional[str] = None,
        label: Optional[str] = None,
    ) -> dict[str, Any]:
        """Persist a queued job and return immediately."""
        if self.store is None:
            return _fail("jobs_unconfigured")
        if not isinstance(question, str) or not question.strip():
            return _fail("empty_question")
        if job_id is None:
            chosen = "job_" + secrets.token_hex(8)
        elif _valid_job_id(job_id):
            chosen = job_id
        else:
            return _fail("invalid_job_id")

        # Shield the commit: a client disconnect must not leave a queued file
        # that was never handed to the runner.
        try:
            return await asyncio.shield(
                self._commit(chosen, question, context, label)
            )
        except OSError:
            logger.warning("job enqueue failed to write state")
            return _fail("jobs_unconfigured")

    async def _commit(
        self,
        chosen: str,
        question: str,
        context: Optional[str],
        label: Optional[str],
    ) -> dict[str, Any]:
        if self.store is None:
            return _fail("jobs_unconfigured")
        async with self._lock:
            self.store.ensure_dir()
            self.store.reap(protect=self._inflight)
            self._recover_orphans()
            existing = self.store.read(chosen)
            if existing is not None:
                return _public_view(existing, self.store.path_for(chosen))
            if self._active_count() >= self._max_active:
                return _fail("queue_full")
            record: dict[str, Any] = {
                "v": 1,
                "id": chosen,
                "status": "queued",
                "question_summary": question_summary(question),
                "created_at": _utc_now(),
                "started_at": None,
                "finished_at": None,
                "answer": None,
                "error": None,
                "elapsed_ms": None,
                "truncated": False,
            }
            clean_label = _sanitize_label(label)
            if clean_label:
                record["label"] = clean_label
            self.store.create(record)
            self._owned.add(chosen)
            path = self.store.path_for(chosen)
        await self._queue.put(_Pending(job_id=chosen, question=question, context=context))
        self._ensure_task()
        logger.info("job %s queued", chosen)
        return _public_view(record, path)

    async def status(self, job_id: str) -> dict[str, Any]:
        """Read one job record. Does not start or wait for Hermes."""
        if self.store is None:
            return _fail("jobs_unconfigured")
        if not _valid_job_id(job_id):
            return _fail("invalid_job_id")
        try:
            async with self._lock:
                self.store.reap(protect=self._inflight)
                self._recover_orphans()
                record = self.store.read(job_id)
                path = self.store.path_for(job_id)
        except OSError:
            return _fail("jobs_unconfigured", job_id=job_id)
        if record is None:
            return _fail("not_found", job_id=job_id)
        return _public_view(record, path)

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="hermes-job-runner")

    def _recover_orphans(self) -> None:
        """Mark on-disk queued/running jobs from a previous process as interrupted.

        The in-memory queue does not survive a restart, so those records will
        never finish. Jobs this process owns are left alone.
        """
        if self._recovered or self.store is None:
            return
        self._recovered = True
        finished = _utc_now()
        for _path, record in self.store.iter_records():
            job_id = str(record.get("id"))
            if job_id in self._owned or job_id in self._inflight:
                continue
            if record.get("status") in ("queued", "running"):
                self.store.update(
                    job_id,
                    status="failed",
                    error="interrupted",
                    finished_at=record.get("finished_at") or finished,
                )
                logger.info("job %s marked interrupted after restart", job_id)

    def _active_count(self) -> int:
        if self.store is None:
            return 0
        count = 0
        for _path, record in self.store.iter_records():
            if record.get("id") in self._owned and record.get("status") in ("queued", "running"):
                count += 1
        return count

    async def _loop(self) -> None:
        while True:
            pending = await self._queue.get()
            try:
                await self._execute(pending)
            except asyncio.CancelledError:
                await self._mark(pending.job_id, status="failed", error="interrupted")
                raise
            except Exception:
                logger.warning("job %s failed unexpectedly", pending.job_id)
                await self._mark(pending.job_id, status="failed", error="job_failed")
            finally:
                self._queue.task_done()

    async def _execute(self, pending: _Pending) -> None:
        if self.store is None:
            return
        async with self._lock:
            record = self.store.read(pending.job_id)
            if record is None or record.get("status") not in ("queued", "running"):
                return
            self._inflight.add(pending.job_id)
            self.store.update(
                pending.job_id,
                status="running",
                started_at=record.get("started_at") or _utc_now(),
            )
        question = pending.question
        context = pending.context
        pending.question = ""
        pending.context = None
        try:
            result = await self._call_ask(question, context)
            if getattr(result, "ok", False):
                answer, truncated = _bound_answer(
                    str(getattr(result, "answer", "") or ""),
                    bool(getattr(result, "truncated", False)),
                )
                await self._mark(
                    pending.job_id,
                    status="done",
                    answer=answer,
                    error=None,
                    elapsed_ms=getattr(result, "elapsed_ms", None),
                    truncated=truncated,
                )
                logger.info("job %s done", pending.job_id)
            else:
                await self._mark(
                    pending.job_id,
                    status="failed",
                    error=_safe_error(getattr(result, "error", None)),
                    elapsed_ms=getattr(result, "elapsed_ms", None),
                )
                logger.info("job %s failed", pending.job_id)
        except asyncio.CancelledError:
            await self._mark(pending.job_id, status="failed", error="interrupted")
            raise
        except Exception:
            logger.warning("job %s ask failed", pending.job_id)
            await self._mark(pending.job_id, status="failed", error="job_failed")
        finally:
            async with self._lock:
                self._inflight.discard(pending.job_id)

    async def _mark(self, job_id: str, **fields: Any) -> None:
        if self.store is None:
            return
        fields.setdefault("finished_at", _utc_now())
        async with self._lock:
            self.store.update(job_id, **fields)

    async def _call_ask(self, question: str, context: Optional[str]) -> Any:
        fn = self._agent.ask_async
        kwargs: dict[str, Any] = {"question": question, "context": context}
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        accepts_timeout = "timeout" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        if accepts_timeout:
            kwargs["timeout"] = self.job_timeout_seconds
        return await fn(**kwargs)
