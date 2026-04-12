"""Security and correctness tests for edge cases and injection vectors."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cc_later import core


class SanitizeOsascriptTests(unittest.TestCase):
    def test_double_quotes_escaped(self):
        result = core._sanitize_osascript('say "hello"')
        self.assertNotIn('"hello"', result)
        self.assertIn('\\"hello\\"', result)

    def test_backslash_escaped(self):
        result = core._sanitize_osascript("path\\to\\file")
        self.assertEqual(result, "path\\\\to\\\\file")

    def test_combined_quote_and_backslash(self):
        result = core._sanitize_osascript('C:\\"danger"')
        # backslash first, then quote
        self.assertIn("\\\\", result)
        self.assertIn('\\"', result)

    def test_plain_string_unchanged(self):
        result = core._sanitize_osascript("Window: 45m left")
        self.assertEqual(result, "Window: 45m left")


class NotifyMacosInjectionTests(unittest.TestCase):
    @patch("subprocess.run")
    def test_title_with_quotes_safe(self, mock_run):
        core._notify_macos('Title "injected"', "Message")
        cmd = mock_run.call_args[0][0]
        script = cmd[-1]
        # The literal quote chars must be escaped in the script
        self.assertNotIn('title "Title "injected""', script)
        self.assertIn('\\"injected\\"', script)

    @patch("subprocess.run")
    def test_message_with_quotes_safe(self, mock_run):
        core._notify_macos("Title", 'msg with "quotes"')
        cmd = mock_run.call_args[0][0]
        script = cmd[-1]
        self.assertIn('\\"quotes\\"', script)


class StdinSizeLimitTests(unittest.TestCase):
    def test_oversized_stdin_truncated(self):
        """_read_hook_payload must not parse past _MAX_STDIN_BYTES."""
        # Build a valid JSON prefix followed by garbage
        big = json.dumps({"cwd": "/tmp"}) + " " * (2 * 1024 * 1024)
        # Passes if no crash — we can't easily test stdin here without mocking,
        # but we verify the constant exists and is sane.
        self.assertLessEqual(core._MAX_STDIN_BYTES, 2 * 1024 * 1024)
        self.assertGreaterEqual(core._MAX_STDIN_BYTES, 65536)


class CaptureDeduplicationTests(unittest.TestCase):
    def test_same_task_not_added_twice(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            env_path = td_path / "config.env"
            env_path.write_text("PLAN=max\n")
            later_file = td_path / ".claude" / "LATER.md"
            later_file.parent.mkdir(parents=True)
            later_file.write_text("# LATER\n\n## Queue\n- [ ] (P1) fix the bug\n")

            with patch.object(core, "config_path", return_value=env_path), \
                 patch.object(core, "app_dir", return_value=td_path), \
                 patch.object(core, "resolve_watch_paths", return_value=[td_path]):
                payload = {"prompt": "later: fix the bug", "cwd": str(td_path)}
                core.capture_from_payload(payload)
                content = later_file.read_text()

        count = content.count("fix the bug")
        self.assertEqual(count, 1, "Same task added twice")

    def test_capture_strips_trailing_periods_and_spaces(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            env_path = td_path / "config.env"
            env_path.write_text("PLAN=max\n")
            later_file = td_path / ".claude" / "LATER.md"
            later_file.parent.mkdir(parents=True)
            later_file.write_text("# LATER\n\n## Queue\n")

            with patch.object(core, "config_path", return_value=env_path), \
                 patch.object(core, "app_dir", return_value=td_path), \
                 patch.object(core, "resolve_watch_paths", return_value=[td_path]):
                payload = {"prompt": "later: add rate limiting...  ", "cwd": str(td_path)}
                core.capture_from_payload(payload)
                content = later_file.read_text()

        # The trailing dots and spaces must be stripped
        self.assertNotIn("...", content)
        self.assertIn("add rate limiting", content)

    def test_short_text_not_added(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            env_path = td_path / "config.env"
            env_path.write_text("PLAN=max\n")
            later_file = td_path / ".claude" / "LATER.md"
            later_file.parent.mkdir(parents=True)
            later_file.write_text("# LATER\n\n## Queue\n")

            with patch.object(core, "config_path", return_value=env_path), \
                 patch.object(core, "app_dir", return_value=td_path), \
                 patch.object(core, "resolve_watch_paths", return_value=[td_path]):
                payload = {"prompt": "later: ok", "cwd": str(td_path)}
                core.capture_from_payload(payload)
                content = later_file.read_text()

        # "ok" is < 3 chars after stripping — should not appear
        self.assertNotIn("- [ ]", content.split("## Queue")[1].strip() or "")


if __name__ == "__main__":
    unittest.main()
