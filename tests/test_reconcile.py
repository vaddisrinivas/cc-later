"""Tests for reconciliation — failure surfacing and completion marking."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests._loader import load_handler_module


class ReconcileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def _cfg(self, mark_completed: str = "check"):
        return self.handler.validate_config_dict(
            {"later_md": {"mark_completed": mark_completed}}
        )

    def _cfg_auto_resume(self, enabled: bool):
        return self.handler.validate_config_dict(
            {
                "later_md": {"mark_completed": "check"},
                "auto_resume": {"enabled": enabled, "min_remaining_minutes": 240},
            }
        )

    def _mock_db(self):
        db = MagicMock()
        db.record_outcome = MagicMock()
        return db

    def _inflight_state(self, repo_key: str, pid=None, result_path=None):
        state = self.handler.AppState()
        state.repos[repo_key] = self.handler.RepoState(
            in_flight=True,
            pid=pid,
            result_path=result_path,
            dispatch_ts="2026-03-30T00:00:00+00:00",
            entries=[],
        )
        return state

    def test_skips_repos_not_in_flight(self):
        state = self.handler.AppState()
        state.repos["/fake/repo"] = self.handler.RepoState(in_flight=False)
        completed = self.handler._reconcile_in_flight(self._cfg(), state, self._mock_db())
        self.assertEqual(completed, 0)

    def test_skips_still_alive_process(self):
        state = self._inflight_state("/fake/repo", pid=99999, result_path="/fake/result.json")
        with patch("cc_later.dispatcher._is_process_alive", return_value=True):
            completed = self.handler._reconcile_in_flight(self._cfg(), state, self._mock_db())
        self.assertEqual(completed, 0)
        self.assertTrue(state.repos["/fake/repo"].in_flight)

    def test_failed_dispatch_clears_in_flight_state(self):
        state = self._inflight_state("/fake/repo", pid=12345, result_path="/no/such/result.json")
        with patch("cc_later.dispatcher._is_process_alive", return_value=False), \
             patch("cc_later.dispatcher.log_event"), \
             patch("cc_later.dispatcher.notify"):
            self.handler._reconcile_in_flight(self._cfg(), state, self._mock_db())
        repo_state = state.repos["/fake/repo"]
        self.assertFalse(repo_state.in_flight)
        self.assertIsNone(repo_state.pid)

    def test_successful_completion_marks_done_entries(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            later_dir = repo / ".claude"
            later_dir.mkdir()
            later_path = later_dir / "LATER.md"
            later_path.write_text("- [ ] fix the bug\n- [ ] update docs\n", encoding="utf-8")

            content = "- [ ] fix the bug\n"
            entries = self.handler.parse_later_entries(content)
            entry = entries[0]

            result_file = repo / "result.json"
            result_file.write_text(f"DONE {entry.id}: fix the bug\n", encoding="utf-8")

            state = self.handler.AppState()
            state.repos[str(repo)] = self.handler.RepoState(
                in_flight=True,
                pid=12345,
                result_path=str(result_file),
                entries=[{
                    "id": entry.id,
                    "text": "fix the bug",
                    "is_priority": False,
                    "line_index": 0,
                    "raw_line": "- [ ] fix the bug",
                }],
            )

            with patch("cc_later.dispatcher._is_process_alive", return_value=False):
                completed = self.handler._reconcile_in_flight(self._cfg(), state, self._mock_db())

            self.assertEqual(completed, 1)
            updated = later_path.read_text(encoding="utf-8")
            self.assertIn("- [x] fix the bug", updated)
            self.assertIn("- [ ] update docs", updated)

    def test_limit_failure_schedules_auto_resume(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            later_dir = repo / ".claude"
            later_dir.mkdir()
            later_path = later_dir / "LATER.md"
            later_path.write_text("- [ ] fix auth timeout\n", encoding="utf-8")

            content = "- [ ] fix auth timeout\n"
            entry = self.handler.parse_later_entries(content)[0]

            result_file = repo / "result.json"
            result_file.write_text(
                "Rate limit reached for your current 5-hour window. Try again later.\n",
                encoding="utf-8",
            )

            state = self.handler.AppState()
            state.repos[str(repo)] = self.handler.RepoState(
                in_flight=True,
                pid=12345,
                result_path=str(result_file),
                entries=[{
                    "id": entry.id,
                    "text": entry.text,
                    "is_priority": entry.is_priority,
                    "line_index": entry.line_index,
                    "raw_line": entry.raw_line,
                }],
            )

            db = self._mock_db()
            with patch("cc_later.dispatcher._is_process_alive", return_value=False):
                completed = self.handler._reconcile_in_flight(
                    self._cfg_auto_resume(True),
                    state,
                    db,
                )

            self.assertEqual(completed, 1)
            repo_state = state.repos[str(repo)]
            self.assertFalse(repo_state.in_flight)
            self.assertEqual(len(repo_state.resume_entries), 1)
            self.assertIsNotNone(repo_state.resume_reason)
            db.record_outcome.assert_called_with(
                task_id=entry.id,
                repo=str(repo),
                status="SKIPPED",
                error="auto_resume:limit_exhausted",
            )

    def test_limit_failure_does_not_schedule_when_auto_resume_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            later_dir = repo / ".claude"
            later_dir.mkdir()
            later_path = later_dir / "LATER.md"
            later_path.write_text("- [ ] fix auth timeout\n", encoding="utf-8")

            content = "- [ ] fix auth timeout\n"
            entry = self.handler.parse_later_entries(content)[0]

            result_file = repo / "result.json"
            result_file.write_text(
                "Rate limit reached for your current 5-hour window. Try again later.\n",
                encoding="utf-8",
            )

            state = self.handler.AppState()
            state.repos[str(repo)] = self.handler.RepoState(
                in_flight=True,
                pid=12345,
                result_path=str(result_file),
                entries=[{
                    "id": entry.id,
                    "text": entry.text,
                    "is_priority": entry.is_priority,
                    "line_index": entry.line_index,
                    "raw_line": entry.raw_line,
                }],
            )

            db = self._mock_db()
            with patch("cc_later.dispatcher._is_process_alive", return_value=False):
                completed = self.handler._reconcile_in_flight(
                    self._cfg_auto_resume(False),
                    state,
                    db,
                )

            self.assertEqual(completed, 1)
            repo_state = state.repos[str(repo)]
            self.assertFalse(repo_state.in_flight)
            self.assertEqual(len(repo_state.resume_entries), 0)


if __name__ == "__main__":
    unittest.main()
