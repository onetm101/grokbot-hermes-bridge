"""Request limits for the Hermes MCP gateway.

Pure-ASGI-friendly helpers:

* :class:`RateLimiter` — per-client sliding-window rate limiting (in-memory);
* :class:`ConcurrencyGate` — bounded in-flight requests (fast-fail over limit);
* payload-size enforcement with a hard cap on bytes read from the ASGI body.

These are deliberately simple, dependency-free and testable in isolation.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

__all__ = [
    "RateLimiter",
    "ConcurrencyGate",
    "read_body_capped",
]


class RateLimiter:
    """Sliding-window rate limiter keyed by client identifier (e.g. IP).

    Thread/async-safe for practical single-event-loop use; bounded memory via
    pruning of idle keys.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = int(max_requests)
        self.window_seconds = float(window_seconds)
        if self.max_requests <= 0 or self.window_seconds <= 0:
            raise ValueError("max_requests and window_seconds must be > 0")
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._hits[key]
        cutoff = now - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            # prune stale keys opportunistically
            self._prune(now)
            return False
        bucket.append(now)
        return True

    def _prune(self, now: float) -> None:
        if len(self._hits) > 10_000:
            cutoff = now - self.window_seconds
            empty = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
            for k in empty:
                del self._hits[k]

    def reset(self) -> None:
        self._hits.clear()


class ConcurrencyGate:
    """Bounded concurrent in-flight requests (fast-fail when saturated)."""

    def __init__(self, max_concurrent: int):
        self.max_concurrent = int(max_concurrent)
        if self.max_concurrent <= 0:
            raise ValueError("max_concurrent must be > 0")
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self.active = 0

    async def try_acquire(self) -> bool:
        if self._sem.locked():
            return False
        await self._sem.acquire()
        self.active += 1
        return True

    def release(self) -> None:
        if self.active > 0:
            self.active -= 1
        self._sem.release()


async def read_body_capped(receive, max_bytes: int) -> Tuple[bytes, bool]:
    """Read the ASGI request body up to ``max_bytes`` bytes.

    Returns ``(body_bytes, too_large)``. Reading stops as soon as the cap is
    exceeded so an oversized body is rejected without buffering it entirely.
    ``receive`` is the ASGI ``receive`` awaitable callable.
    """
    chunks: list = []
    total = 0
    too_large = False
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        body = message.get("body", b"")
        if body:
            total += len(body)
            if total > max_bytes:
                too_large = True
            if total <= max_bytes:
                chunks.append(body)
        if not message.get("more_body", False):
            break
    if too_large:
        return b"", True
    return b"".join(chunks), False
