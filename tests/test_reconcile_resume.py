import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cc_later import core


class ReconcileResumeTests(unittest.TestCase):
    def _write_config(self, app_dir: Path, repo: Path, mode: str = "always") -> None:
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
                    f"WINDOW_DISPATCH_MODE={mode}",
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

    def test_limit_failure_schedules_resume_entries(self):
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir = Path(app)
            repo = Path(repo_dir)
            (repo / ".git").mkdir()
            later = repo / ".claude" / "LATER.md"
            later.parent.mkdir(parents=True)
            later.write_text("- [ ] (P1) fix auth timeout\n", encoding="utf-8")

            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result = repo / "result.json"
                result.write_text(
                    f"FAILED ({'rate limit'}) {task.id}: fix auth timeout\n"
                    "Rate limit reached for your current 5-hour window\n",
                    encoding="utf-8",
                )
                state.repos[str(repo)] = core.RepoState(
                    in_flight=True,
                    agents=[{
                        "section_name": "",
                        "pid": None,
                        "result_path": str(result),
                        "entries": [task.to_dict()],
                        "branch": None,
                        "worktree_path": None,
                    }],
                )
                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 1)
                self.assertEqual(len(state.repos[str(repo)].resume_entries), 1)
                self.assertEqual(state.repos[str(repo)].resume_reason, "limit_exhausted")

    def test_done_result_marks_task_done_in_later(self):
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir = Path(app)
            repo = Path(repo_dir)
            (repo / ".git").mkdir()
            later = repo / ".claude" / "LATER.md"
            later.parent.mkdir(parents=True)
            later.write_text("- [ ] (P1) update docs\n", encoding="utf-8")

            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result = repo / "result.json"
                result.write_text(f"DONE {task.id}: updated docs\n", encoding="utf-8")
                state.repos[str(repo)] = core.RepoState(
                    in_flight=True,
                    agents=[{
                        "section_name": "",
                        "pid": None,
                        "result_path": str(result),
                        "entries": [task.to_dict()],
                        "branch": None,
                        "worktree_path": None,
                    }],
                )
                core._reconcile(cfg, state, datetime.now(timezone.utc))
                updated = later.read_text(encoding="utf-8")
                self.assertIn("- [x] (P1) update docs", updated)


if __name__ == "__main__":
    unittest.main()
