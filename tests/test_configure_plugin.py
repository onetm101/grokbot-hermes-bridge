from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.configure_plugin import configure, validate_endpoint


class ConfigurePluginTests(unittest.TestCase):
    def test_rejects_unsafe_or_wrong_endpoint(self) -> None:
        for value in (
            "http://mcp.example.com/mcp",
            "https://user@mcp.example.com/mcp",
            "https://mcp.example.com/other",
            "https://mcp.example.com/mcp?token=value",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_endpoint(value)

    def test_writes_url_without_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configure(root, "https://bridge.example.org/mcp")
            for name in ("mcp.json", ".mcp.json"):
                server = json.loads((root / name).read_text())["mcpServers"]["hermes-bridge"]
                self.assertEqual(server["url"], "https://bridge.example.org/mcp")
                self.assertNotIn("headers", server)


if __name__ == "__main__":
    unittest.main()
