#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(relpath: str) -> dict:
    return json.loads((ROOT / relpath).read_text(encoding="utf-8"))


class PluginManifestTests(unittest.TestCase):
    def test_agent_plugin_root_manifest(self) -> None:
        data = _load("plugin.json")
        self.assertEqual(data["$schema"], "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
        self.assertEqual(data["name"], "grokbot-hermes-bridge")
        self.assertEqual(data["license"], "MIT")
        self.assertEqual(data["mcpServers"], "./mcp.json")

    def test_vendor_manifests_share_identity(self) -> None:
        for relpath in (
            ".codex-plugin/plugin.json",
            ".cursor-plugin/plugin.json",
            ".grok-plugin/plugin.json",
        ):
            with self.subTest(relpath=relpath):
                data = _load(relpath)
                self.assertEqual(data["name"], "grokbot-hermes-bridge")
                self.assertEqual(data["version"], "0.1.0")
                self.assertEqual(data["license"], "MIT")

    def test_grok_and_cursor_reference_remote_config(self) -> None:
        for relpath in (".grok-plugin/plugin.json", ".cursor-plugin/plugin.json"):
            self.assertEqual(_load(relpath)["mcpServers"], "./mcp.json")

    def test_codex_points_at_dot_mcp(self) -> None:
        data = _load(".codex-plugin/plugin.json")
        self.assertEqual(data["mcpServers"], "./.mcp.json")
        self.assertEqual(data["skills"], "./skills/")

    def test_mcp_entrypoints_use_oauth_discovery_without_static_header(self) -> None:
        for relpath in ("mcp.json", ".mcp.json"):
            with self.subTest(relpath=relpath):
                data = _load(relpath)
                server = data["mcpServers"]["hermes-bridge"]
                self.assertEqual(server["type"], "http")
                self.assertEqual(server["url"], "https://mcp.example.com/mcp")
                self.assertNotIn("headers", server)

    def test_overview_image_is_documented_and_committed(self) -> None:
        assets_readme = (ROOT / "assets" / "README.md").read_text(encoding="utf-8")
        self.assertIn("bridge-overview.jpg", assets_readme)
        self.assertIn("ImageGen", assets_readme)
        image = ROOT / "assets" / "bridge-overview.jpg"
        self.assertTrue(image.is_file())
        self.assertGreater(image.stat().st_size, 10_000)

    def test_required_docs_exist(self) -> None:
        for name in (
            "README.md",
            "ARCHITECTURE.md",
            "SECURITY.md",
            "LICENSE",
            ".gitignore",
            "skills/hermes-bridge/SKILL.md",
            "examples/env.example",
            "hermes_gateway/mcp/gateway.py",
            "scripts/configure_plugin.py",
            "scripts/install.sh",
            "scripts/doctor.py",
            "docs/TUTORIAL.md",
            "assets/README.md",
            "assets/bridge-overview.jpg",
        ):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_export_has_no_nested_git(self) -> None:
        nested = [path for path in ROOT.rglob(".git") if path != ROOT / ".git"]
        self.assertEqual(nested, [])


if __name__ == "__main__":
    unittest.main()
