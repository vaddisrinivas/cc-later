import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _parse_frontmatter(path: Path) -> dict:
    """Extract YAML-like frontmatter from a markdown file."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    result: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip()
    return result


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
        self.assertIn("SessionStart", hooks)

    def test_status_command_exists_and_no_stale_stats_command(self):
        self.assertTrue((ROOT / "commands" / "status.md").exists())
        self.assertFalse((ROOT / "commands" / "stats.md").exists())

    def test_monitor_command_exists(self):
        self.assertTrue((ROOT / "commands" / "monitor.md").exists())

    def test_dashboard_command_exists(self):
        self.assertTrue((ROOT / "commands" / "dashboard.md").exists())

    def test_status_command_has_user_invocable(self):
        fm = _parse_frontmatter(ROOT / "commands" / "status.md")
        self.assertEqual(fm.get("name"), "status")
        self.assertEqual(fm.get("user_invocable"), "true")

    def test_monitor_command_has_user_invocable(self):
        fm = _parse_frontmatter(ROOT / "commands" / "monitor.md")
        self.assertEqual(fm.get("user_invocable"), "true")

    def test_dashboard_command_has_user_invocable(self):
        fm = _parse_frontmatter(ROOT / "commands" / "dashboard.md")
        self.assertEqual(fm.get("name"), "dashboard")
        self.assertEqual(fm.get("user_invocable"), "true")

    def test_version_consistency_across_files(self):
        """plugin.json, marketplace.json, and pyproject.toml must all have the same version."""
        import tomllib

        plugin = self._load_json(ROOT / ".claude-plugin" / "plugin.json")
        marketplace = self._load_json(ROOT / ".claude-plugin" / "marketplace.json")
        with (ROOT / "pyproject.toml").open("rb") as f:
            pyproject = tomllib.load(f)

        plugin_ver = plugin.get("version")
        marketplace_ver = marketplace.get("metadata", {}).get("version")
        pyproject_ver = pyproject.get("project", {}).get("version")

        self.assertEqual(plugin_ver, pyproject_ver,
                         f"plugin.json ({plugin_ver}) != pyproject.toml ({pyproject_ver})")
        self.assertEqual(marketplace_ver, pyproject_ver,
                         f"marketplace.json ({marketplace_ver}) != pyproject.toml ({pyproject_ver})")

    def test_hooks_stop_timeout_sufficient(self):
        """Stop hook timeout should be at least 15s — it does disk I/O."""
        hooks_payload = self._load_json(ROOT / "hooks" / "hooks.json")
        stop_hooks = hooks_payload["hooks"]["Stop"]
        for group in stop_hooks:
            for hook in group.get("hooks", []):
                timeout = hook.get("timeout", 0)
                self.assertGreaterEqual(timeout, 15000,
                                        f"Stop hook timeout {timeout}ms is too low")

    def test_capture_hook_matches_for_later_pattern(self):
        """UserPromptSubmit hook matcher must cover the 'for later:' pattern."""
        hooks_payload = self._load_json(ROOT / "hooks" / "hooks.json")
        ups = hooks_payload["hooks"]["UserPromptSubmit"]
        matchers = [g.get("matcher", "") for g in ups]
        combined = " ".join(matchers)
        self.assertIn("for\\s+later", combined,
                      "UserPromptSubmit matcher missing 'for later:' pattern")

    def test_default_config_env_exists(self):
        self.assertTrue((ROOT / "scripts" / "default_config.env").exists())


if __name__ == "__main__":
    unittest.main()
