import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class PluginLayoutTests(unittest.TestCase):
    def _load_json(self, path: Path) -> dict:
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        return payload

    def test_plugin_manifest_and_marketplace_are_valid_json(self):
        plugin_manifest = self._load_json(ROOT / ".claude-plugin" / "plugin.json")
        marketplace = self._load_json(ROOT / ".claude-plugin" / "marketplace.json")
        self.assertEqual(plugin_manifest.get("name"), "cc-later")
        self.assertEqual(marketplace.get("name"), "cc-later")

    def test_hook_config_has_required_events(self):
        hooks_payload = self._load_json(ROOT / "hooks" / "hooks.json")
        hooks = hooks_payload.get("hooks", {})
        self.assertIn("Stop", hooks)
        self.assertIn("UserPromptSubmit", hooks)

    def test_status_command_exists_and_no_stale_stats_command(self):
        self.assertTrue((ROOT / "commands" / "status.md").exists())
        self.assertFalse((ROOT / "commands" / "stats.md").exists())


if __name__ == "__main__":
    unittest.main()
