#!/usr/bin/env python3
"""Run the privacy scanner against every text blob in Git history."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from privacy_scan import scan_text  # noqa: E402

SKIP = {
    "src/privacy_scan.py",
    "tests/test_privacy_scan.py",
}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def main() -> int:
    try:
        objects = git("rev-list", "--objects", "--all").decode("utf-8").splitlines()
    except subprocess.CalledProcessError:
        print("git-history-audit: repository has no readable history", file=sys.stderr)
        return 2

    findings: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in objects:
        object_id, _, path = line.partition(" ")
        if not path or path in SKIP or object_id in seen:
            continue
        seen.add(object_id)
        if git("cat-file", "-t", object_id).strip() != b"blob":
            continue
        data = git("cat-file", "blob", object_id)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(text, relpath=f"{object_id[:12]}:{path}"))

    if findings:
        print(f"git-history-audit: {len(findings)} finding(s)")
        for item in findings:
            print(f"- {item['file']}: {item['rule']}: {item['excerpt']}")
        return 1
    print(f"git-history-audit: clean ({len(seen)} text blob(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
