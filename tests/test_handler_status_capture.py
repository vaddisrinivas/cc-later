import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cc_later import core


class HandlerStatusCaptureTests(unittest.TestCase):
    def _write_config(self, app_dir: Path, repo: Path) -> None:
        (app_dir / "config.env").write_text(
            "\n".join(
                [
                    f"PATHS_WATCH={repo}",
                    "LATER_PATH=.claude/LATER.md",
                    "LATER_MAX_ENTRIES_PER_DISPATCH=3",
                    "LATER_AUTO_GITIGNORE=true",
                    "DISPATCH_ENABLED=true",
                    "DISPATCH_MODEL=sonnet",
                    "DISPATCH_ALLOW_FILE_WRITES=false",
                    "DISPATCH_OUTPUT_PATH=~/.cc-later/results/{repo}-{date}.json",
                    "WINDOW_DISPATCH_MODE=always",
                    "WINDOW_TRIGGER_AT_MINUTES_REMAINING=30",
                    "WINDOW_IDLE_GRACE_PERIOD_MINUTES=0",
                    "WINDOW_FALLBACK_DISPATCH_HOURS=",
                    "WINDOW_JSONL_PATHS=",
                    "LIMITS_WEEKLY_BUDGET_TOKENS=10000000",
                    "LIMITS_BACKOFF_AT_PCT=80",
                    "AUTO_RESUME_ENABLED=true",
                    "AUTO_RESUME_MIN_REMAINING_MINUTES=240",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def test_capture_then_dispatch_then_status(self):
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir = Path(app)
            repo = Path(repo_dir).resolve()
            (repo / ".git").mkdir()

            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)

                core.capture_from_payload(
                    {"cwd": str(repo), "prompt": "later: update readme flags\nlater[!]: fix auth bypass"}
                )
                later = repo / ".claude" / "LATER.md"
                text = later.read_text(encoding="utf-8")
                self.assertIn("(P1) update readme flags", text)
                self.assertIn("(P0) fix auth bypass", text)

                with patch("cc_later.core._spawn_dispatch", return_value=12345), \
                     patch("cc_later.core.compute_budget_state", return_value=core.BudgetState(used_tokens=0, pct_used=0.0)):
                    code = core.run_handler(json.dumps({"cwd": str(repo), "session_id": "s1"}))
                self.assertEqual(code, 0)

                state = core.load_state()
                rs = state.repos[str(repo.resolve())]
                self.assertTrue(rs.in_flight)
                self.assertTrue(len(rs.agents) > 0)
                result_path = Path(rs.agents[0]["result_path"])

                first_task = core.Task.from_dict(rs.agents[0]["entries"][0])
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(f"DONE {first_task.id}: done\n", encoding="utf-8")

                with patch("cc_later.core._spawn_dispatch", return_value=None):
                    core.run_handler(json.dumps({"cwd": str(repo), "session_id": "s1"}))
                self.assertIn("[x]", later.read_text(encoding="utf-8"))

                status = core.build_status(str(repo))
                self.assertIn("## cc-later Status", status)
                self.assertIn("Weekly budget", status)
                self.assertIn("Queue", status)


if __name__ == "__main__":
    unittest.main()
