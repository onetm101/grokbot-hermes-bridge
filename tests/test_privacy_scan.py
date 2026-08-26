#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import privacy_scan  # noqa: E402


class PrivacyScanSelfTest(unittest.TestCase):
    def test_detects_planted_leftovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "leak.md"
            sample.write_text(
                "\n".join(
                    [
                        "overlay 100.64.1.2",
                        "home /home/someuser/bin",
                        "chat handle=@abcduser",
                        "task t_deadbeef",
                        "id 12345678901234567",
                        "token: plantedvalueplantedvalue",
                    ]
                ),
                encoding="utf-8",
            )
            findings = {item["rule"] for item in privacy_scan.scan_tree(Path(tmp))}
            self.assertIn("cgnat-or-overlay-ip", findings)
            self.assertIn("user-home-unix", findings)
            self.assertIn("chat-handle", findings)
            self.assertIn("internal-task-id", findings)
            self.assertIn("snowflake-id", findings)
            self.assertIn("assignment-secret", findings)

    def test_high_entropy_token(self) -> None:
        findings = privacy_scan.scan_text(
            "blob=Zm9vYmFyYmF6cXV4eHl6MTIzNDU2Nzg5MA==",  # pragma: allowlist secret
            relpath="example.txt",
        )
        self.assertTrue(any(item["rule"] == "high-entropy-token" for item in findings))

    def test_export_tree_is_clean(self) -> None:
        findings = privacy_scan.scan_tree(ROOT)
        self.assertEqual(findings, [], msg=findings)

    def test_docs_state_grok_bot_visible_message_rule(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / "hermes-bridge" / "SKILL.md").read_text(encoding="utf-8")
        arch = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        for text in (readme, skill, arch):
            self.assertIn("visible", text.lower())
            self.assertIn("tool", text.lower())


if __name__ == "__main__":
    unittest.main()
