import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cc_later import core


class ConfigAndFormatTests(unittest.TestCase):
    def test_first_run_creates_config_and_auto_watch(self):
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir).resolve()
            (repo / ".git").mkdir()
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                self.assertTrue((Path(app) / "config.env").exists())
                watches = core.resolve_watch_paths(cfg, str(repo))
                self.assertEqual([str(watches[0].resolve())], [str(repo)])

    def test_parse_select_and_mark_done(self):
        content = (
            "# LATER\n\n"
            "## Queue\n"
            "- [ ] (P2) low\n"
            "- [ ] (P0) urgent\n"
            "- [ ] (P1) normal\n"
            "- [!] legacy urgent\n"
            "- [x] done\n"
        )
        sections = core.parse_tasks(content)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].name, "Queue")
        ordered = core.select_tasks(sections[0], limit=4)
        self.assertEqual([t.priority for t in ordered], ["P0", "P0", "P1", "P2"])

        done = {ordered[0].id}
        updated = core.mark_done_in_content(content, done)
        self.assertIn("- [x] (P0) urgent", updated)

    def test_config_env_is_loaded(self):
        with tempfile.TemporaryDirectory() as app:
            app_dir = Path(app)
            cfg_file = app_dir / "config.env"
            cfg_file.write_text(
                "DISPATCH_MODEL=opus\n"
                "WINDOW_DISPATCH_MODE=always\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                self.assertEqual(cfg.dispatch.model, "opus")
                self.assertEqual(cfg.window.dispatch_mode, "always")


if __name__ == "__main__":
    unittest.main()
