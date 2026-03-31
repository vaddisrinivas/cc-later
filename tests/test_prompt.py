"""Tests for _render_prompt and _resolve_output_path."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests._loader import load_handler_module


class PromptRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def _cfg(self, allow_file_writes: bool = False, prompt_template: str = ""):
        return self.handler.validate_config_dict(
            {
                "dispatch": {
                    "allow_file_writes": allow_file_writes,
                    "prompt_template": prompt_template,
                }
            }
        )

    def _entries(self, texts: list[str]):
        content = "\n".join(f"- [ ] {t}" for t in texts)
        return self.handler.parse_later_entries(content)

    def test_prompt_contains_repo_path(self):
        cfg = self._cfg()
        entries = self._entries(["fix auth bug"])
        prompt = self.handler._render_prompt(Path("/home/user/myrepo"), cfg, entries)
        self.assertIn("/home/user/myrepo", prompt)

    def test_prompt_contains_entry_ids_and_text(self):
        cfg = self._cfg()
        entries = self._entries(["fix auth bug", "update README"])
        prompt = self.handler._render_prompt(Path("/repo"), cfg, entries)
        for entry in entries:
            self.assertIn(entry.id, prompt)
            self.assertIn(entry.text, prompt)

    def test_read_only_mode_forbids_file_modification(self):
        cfg = self._cfg(allow_file_writes=False)
        entries = self._entries(["audit something"])
        prompt = self.handler._render_prompt(Path("/repo"), cfg, entries)
        self.assertIn("Do not modify files", prompt)

    def test_write_mode_permits_file_modification(self):
        cfg = self._cfg(allow_file_writes=True)
        entries = self._entries(["fix the bug directly"])
        prompt = self.handler._render_prompt(Path("/repo"), cfg, entries)
        self.assertIn("You may edit files", prompt)

    def test_prompt_instructs_done_output_format(self):
        cfg = self._cfg()
        entries = self._entries(["some task"])
        prompt = self.handler._render_prompt(Path("/repo"), cfg, entries)
        self.assertIn("DONE", prompt)
        self.assertIn("SKIPPED", prompt)
        self.assertIn("NEEDS_HUMAN", prompt)

    def test_custom_template_is_used_when_configured(self):
        with tempfile.TemporaryDirectory() as td:
            tmpl = Path(td) / "tmpl.txt"
            tmpl.write_text("Repo={repo_path} Tasks={entries}", encoding="utf-8")
            cfg = self._cfg(prompt_template=str(tmpl))
            entries = self._entries(["some task"])
            prompt = self.handler._render_prompt(Path("/myrepo"), cfg, entries)
        self.assertIn("Repo=/myrepo", prompt)
        self.assertIn("Tasks=", prompt)


class OutputPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def test_output_path_uses_repo_slug(self):
        now = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)
        path = self.handler._resolve_output_path(
            "~/.cc-later/results/{repo}-{date}.json",
            Path("/home/user/my-project"),
            now,
        )
        self.assertIn("my-project", str(path))

    def test_output_path_special_chars_slugified(self):
        now = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)
        path = self.handler._resolve_output_path(
            "~/.cc-later/results/{repo}-{date}.json",
            Path("/home/user/my project & stuff!"),
            now,
        )
        # Spaces and special chars become dashes
        self.assertNotIn(" ", str(path.name))
        self.assertNotIn("!", str(path.name))

    def test_output_path_is_absolute(self):
        now = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)
        path = self.handler._resolve_output_path(
            "~/.cc-later/results/{repo}-{date}.json",
            Path("/repo"),
            now,
        )
        self.assertTrue(path.is_absolute())


if __name__ == "__main__":
    unittest.main()
