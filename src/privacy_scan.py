#!/usr/bin/env python3
"""Scan the public export tree for high-entropy blobs and privacy denylist hits.

This scanner is intentionally conservative. It looks for operator leftovers
that must never ship in a public folder: tokens, private addresses, chat
handles, user home paths, and internal identifiers.
"""

from __future__ import annotations

import argparse
import math
import re
from collections.abc import Iterable
from pathlib import Path

SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".so", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2"}
TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".py",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".example",
    ".gitignore",
    ".license",
    ".sh",
}

# Token-like leftovers. Patterns are generic; they do not encode any live secret.
DENYLIST = (
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github-pat", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("github-fine-grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("openai-sk", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "bearer-header",
        re.compile(r"\bBearer\s+(?!resource_metadata\b)[A-Za-z0-9._\-]{16,}\b", re.I),
    ),
    (
        "assignment-secret",
        re.compile(
            r"(?im)^\s*(?:export\s+)?(?:api[_-]?key|secret|token|password|passwd)"
            r"\s*[:=]\s*(?:['\"][^'\"\r\n]{8,}['\"]|[A-Za-z0-9+/=_\-]{12,})"
            r"\s*(?:#.*)?$"
        ),
    ),
    ("cgnat-or-overlay-ip", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[0-1]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")),
    ("rfc1918-ip", re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3})\b")),
    ("public-ipv4", re.compile(r"\b(?!127\.|0\.)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
    ("ipv6", re.compile(r"\b(?:[0-9a-f]{1,4}:){4,}[0-9a-f]{1,4}\b", re.I)),
    ("user-home-unix", re.compile(r"(?i)/(?:home|Users)/[A-Za-z0-9._-]+")),
    ("chat-handle", re.compile(r"(?i)(?:t\.me/|discord(?:app)?\.com/users/|(?:chat|user)\s*handle\s*[:=]\s*@\w{4,32})")),
    ("tailscale-word", re.compile(r"(?i)\btailscale\b")),
    ("internal-task-id", re.compile(r"\bt_[a-f0-9]{8}\b")),
    ("snowflake-id", re.compile(r"\b\d{17,20}\b")),
    ("operator-hostname", re.compile(r"(?i)\b(?:hermes-main|localhost\.localdomain)\b")),
)

ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")
ENTROPY_THRESHOLD = 4.5
# Public schema identifiers and license legalese are expected high-ish entropy.
ENTROPY_ALLOW_SUBSTRINGS = (
    "agent-plugins.org/schemas",
    "Apache-2.0",
    "SPDX",
)


def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def iter_text_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.suffix == "" or path.name.startswith("."):
            yield path


def scan_text(text: str, *, relpath: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for name, pattern in DENYLIST:
        for match in pattern.finditer(text):
            # The scanner source and its tests document pattern names; skip self-hits
            # only when the file is this module and the hit is the regex definition.
            findings.append(
                {
                    "file": relpath,
                    "rule": name,
                    "excerpt": _excerpt(text, match.start(), match.end()),
                }
            )
    for match in ENTROPY_TOKEN.finditer(text):
        token = match.group(0)
        # Environment variable names are identifiers, not secret material.
        if re.fullmatch(r"[A-Z][A-Z0-9_]{15,}=?", token):
            continue
        if "=" in token and re.fullmatch(r"[A-Z][A-Z0-9_]{7,}", token.split("=", 1)[0]):
            continue
        if any(allowed in token or allowed in relpath for allowed in ENTROPY_ALLOW_SUBSTRINGS):
            continue
        if shannon_entropy(token) >= ENTROPY_THRESHOLD:
            findings.append(
                {
                    "file": relpath,
                    "rule": "high-entropy-token",
                    "excerpt": token[:48],
                }
            )
    return findings


def _excerpt(text: str, start: int, end: int, radius: int = 24) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return re.sub(r"\s+", " ", text[left:right]).strip()


def scan_tree(root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in iter_text_files(root):
        relpath = str(path.relative_to(root)).replace("\\", "/")
        if relpath.endswith("src/privacy_scan.py") or relpath.endswith("tests/test_privacy_scan.py"):
            # Pattern catalog lives here on purpose.
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append({"file": relpath, "rule": "binary-or-non-utf8", "excerpt": "unreadable as utf-8"})
            continue
        findings.extend(scan_text(text, relpath=relpath))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a public export tree for privacy leftovers")
    parser.add_argument("--root", default=".", help="Directory to scan")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    findings = scan_tree(root)
    if not findings:
        print(f"privacy-scan: clean ({root})")
        return 0
    print(f"privacy-scan: {len(findings)} finding(s)")
    for item in findings:
        print(f"- {item['file']}: {item['rule']}: {item['excerpt']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
