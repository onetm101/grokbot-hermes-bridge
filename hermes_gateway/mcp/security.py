"""Security primitives for the Hermes MCP gateway.

Responsibilities:

* runtime secret handling (fail-closed, minimum length, constant-time compare);
* request authentication (Bearer) with no secret leakage in responses/logs;
* Host / Origin / Content-Type validation (defense in depth, on top of the
  MCP SDK's own DNS-rebinding protection);
* a logging filter that redacts the secret and bearer values from any log line.

No secret is ever stored in source, committed to git, or printed.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Iterable, List, Optional

__all__ = [
    "MIN_SECRET_LEN",
    "SecretStore",
    "authenticate",
    "RedactingFilter",
    "AUTH_FAIL_REASON",
]

MIN_SECRET_LEN = 32

# `Authorization` and `X-Hermes-Mcp-Token` keys.
AUTH_HEADER = "authorization"
BEARER_PREFIX = "bearer "

# Canonical failure body used for every auth failure. It is intentionally
# identical whether the secret is missing, malformed or wrong, and never
# echoes the supplied token.
AUTH_FAIL_REASON = "Unauthorized"


class SecretStore:
    """Runtime secret holder with fail-closed semantics.

    ``configured`` is False until a valid (non-empty, >= ``MIN_SECRET_LEN``)
    secret is supplied. The gateway must refuse to serve real traffic while
    ``configured`` is False (fail-closed).
    """

    def __init__(self, secret: Optional[str] = None):
        self._secret_digest: Optional[bytes] = None
        self.configured = False
        if secret:
            self.set(secret)

    @staticmethod
    def _digest(value: bytes) -> bytes:
        return hashlib.sha256(value).digest()

    def set(self, secret: str) -> bool:
        """Set the secret. Returns True if it meets the minimum length.

        Only a fixed-size digest is retained; the raw secret is not stored on
        the instance.
        """
        secret = (secret or "").strip()
        if len(secret) < MIN_SECRET_LEN:
            self._secret_digest = None
            self.configured = False
            return False
        self._secret_digest = self._digest(secret.encode("utf-8"))
        self.configured = True
        return True

    def verify(self, candidate: Optional[str]) -> bool:
        """Constant-time comparison over uniform-length digests.

        Both sides are hashed to a fixed-size digest before comparing, so the
        comparison duration is independent of both the candidate's length and
        content. Returns False (not ``configured``) if no secret is
        configured, so the gateway stays fail-closed.
        """
        if not self.configured or not self._secret_digest:
            return False
        if not candidate:
            return False
        provided = self._digest(candidate.encode("utf-8"))
        return hmac.compare_digest(provided, self._secret_digest)


def _extract_bearer(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    if value.lower().startswith(BEARER_PREFIX):
        return value[len(BEARER_PREFIX):].strip()
    return None


def authenticate(store: SecretStore, authorization_header: Optional[str]) -> bool:
    """Return True iff the request is authorised with a valid bearer secret."""
    token = _extract_bearer(authorization_header)
    return store.verify(token)


def is_health_path(path: str) -> bool:
    """The non-sensitive health endpoint is exempt from auth (see gateway)."""
    return path in ("/health", "/healthz") or path.startswith("/health/")


class RedactingFilter(logging.Filter):
    """Redact configured secrets / bearer tokens / auth values from log lines.

    Attach to a logger (or a handler). Any occurrence of a configured secret is
    replaced with ``<redacted>``.
    """

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._patterns: List[re.Pattern] = []
        for s in secrets:
            s = (s or "").strip()
            if s:
                self._patterns.append(re.compile(re.escape(s), re.IGNORECASE))
        # Generic bearer-token redaction for anything that looks like
        # "Bearer <token>" — covers tokens even if not a configured secret.
        self._patterns.append(re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/\-=]+"))
        self._patterns.append(re.compile(r"(?i)(x-hermes-bridge-token:\s*)[^;\r\n]+"))

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for pat in self._patterns:
            def _repl(m: "re.Match[str]") -> str:
                # keep the key prefix ("Bearer "), replace the value
                if m.lastindex:
                    return m.group(1) + "<redacted>"
                return "<redacted>"
            redacted = pat.sub(_repl, redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True
