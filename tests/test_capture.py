"""Tests for scripts/capture.py — key phrase detection and LATER.md appending."""

import importlib.util
import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch


def load_capture_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "capture.py"
    name = "cc_later_capture"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load capture.py from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _stdin(prompt: str) -> StringIO:
    return StringIO(json.dumps({"prompt": prompt}))


class CaptureRegexTests(unittest.TestCase):
    """Tests for the CAPTURE_RE pattern — no file I/O."""

    @classmethod
    def setUpClass(cls):
        cls.capture = load_capture_module()

    def _matches(self, text: str) -> list[tuple[str | None, str]]:
        return [
            (m.group(1), m.group(2).strip())
            for m in self.capture.CAPTURE_RE.finditer(text)
        ]

    def test_later_colon_triggers(self):
        hits = self._matches("later: fix the auth bug")
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0][0])
        self.assertEqual(hits[0][1], "fix the auth bug")

    def test_add_to_later_triggers(self):
        hits = self._matches("add to later: update README install steps")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], "update README install steps")

    def test_add_this_to_later_triggers(self):
        hits = self._matches("add this to later: refactor the parser")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], "refactor the parser")

    def test_note_for_later_triggers(self):
        hits = self._matches("note for later: UserService.delete() swallows exceptions")
        self.assertEqual(len(hits), 1)

    def test_queue_for_later_triggers(self):
        hits = self._matches("queue for later: add rate limiting to /refresh")
        self.assertEqual(len(hits), 1)

    def test_for_later_triggers(self):
        hits = self._matches("for later: check the migration script")
        self.assertEqual(len(hits), 1)

    def test_priority_flag_captured(self):
        hits = self._matches("later[!]: SQL injection in filter builder")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], "[!]")
        self.assertEqual(hits[0][1], "SQL injection in filter builder")

    def test_bare_later_without_colon_does_not_trigger(self):
        """'see you later' or 'handle this later' must not fire."""
        hits = self._matches("I'll handle this later when I have time")
        self.assertEqual(hits, [])

    def test_multiple_phrases_in_one_prompt(self):
        prompt = (
            "later: fix the N+1 query\n"
            "also note for later: update the docs"
        )
        hits = self._matches(prompt)
        self.assertEqual(len(hits), 2)


class CaptureIntegrationTests(unittest.TestCase):
    """Tests for main() — verifies LATER.md is written correctly."""

    @classmethod
    def setUpClass(cls):
        cls.capture = load_capture_module()

    def _run(self, prompt: str, repo: Path) -> int:
        with patch.object(sys, "stdin", _stdin(prompt)), \
             patch.object(self.capture, "_repo_root", return_value=repo), \
             patch("sys.stdout", new_callable=StringIO):
            return self.capture.main()

    def test_appends_entry_to_later_md(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._run("later: fix the auth bug", repo)
            later = repo / ".claude" / "LATER.md"
            self.assertTrue(later.exists())
            content = later.read_text(encoding="utf-8")
            self.assertIn("- [ ] fix the auth bug", content)

    def test_urgent_entry_uses_priority_marker(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._run("later[!]: SQL injection in filter builder", repo)
            content = (repo / ".claude" / "LATER.md").read_text(encoding="utf-8")
            self.assertIn("- [!] SQL injection in filter builder", content)

    def test_duplicate_not_appended(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            later = repo / ".claude" / "LATER.md"
            later.parent.mkdir(parents=True)
            later.write_text("# LATER\n- [ ] fix the auth bug\n", encoding="utf-8")
            self._run("later: fix the auth bug", repo)
            content = later.read_text(encoding="utf-8")
            self.assertEqual(content.count("fix the auth bug"), 1)

    def test_empty_prompt_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            result = self._run("", repo)
            self.assertEqual(result, 0)
            self.assertFalse((repo / ".claude" / "LATER.md").exists())

    def test_non_matching_prompt_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._run("just a regular question about Python", repo)
            self.assertFalse((repo / ".claude" / "LATER.md").exists())

    def test_creates_later_md_if_missing(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._run("add to later: update README", repo)
            self.assertTrue((repo / ".claude" / "LATER.md").exists())

    def test_preserves_existing_later_md_content(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            later = repo / ".claude" / "LATER.md"
            later.parent.mkdir(parents=True)
            later.write_text("# LATER\n- [ ] existing task\n", encoding="utf-8")
            self._run("later: new task", repo)
            content = later.read_text(encoding="utf-8")
            self.assertIn("existing task", content)
            self.assertIn("new task", content)


if __name__ == "__main__":
    unittest.main()
