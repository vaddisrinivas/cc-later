"""Tests for _reconcile_in_flight — failure surfacing and completion marking."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from tests._loader import load_handler_module


class ReconcileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def _cfg(self, mark_completed: str = "check"):
        return self.handler.validate_config_dict(
            {"later_md": {"mark_completed": mark_completed}}
        )

    def _inflight_state(
        self, repo_key: str, pid: int | None, result_path: str | None
    ):
        state = self.handler.AppState()
        state.repos[repo_key] = self.handler.RepoState(
            in_flight=True,
            pid=pid,
            result_path=result_path,
            dispatch_ts="2026-03-30T00:00:00+00:00",
            entries=[],
        )
        return state

    # ── idle / alive guards ──────────────────────────────────────────────────

    def test_skips_repos_not_in_flight(self):
        state = self.handler.AppState()
        state.repos["/fake/repo"] = self.handler.RepoState(in_flight=False)
        completed = self.handler._reconcile_in_flight(self._cfg(), state)
        self.assertEqual(completed, 0)

    def test_skips_still_alive_process(self):
        state = self._inflight_state("/fake/repo", pid=99999, result_path="/fake/result.json")
        with patch.object(self.handler, "_is_process_alive", return_value=True):
            completed = self.handler._reconcile_in_flight(self._cfg(), state)
        self.assertEqual(completed, 0)
        self.assertTrue(state.repos["/fake/repo"].in_flight)

    # ── failure surfacing ────────────────────────────────────────────────────

    def test_failed_dispatch_logs_dispatch_failed_event(self):
        state = self._inflight_state("/fake/repo", pid=12345, result_path="/no/such/result.json")
        with patch.object(self.handler, "_is_process_alive", return_value=False), \
             patch.object(self.handler, "log_event") as mock_log, \
             patch.object(self.handler, "_maybe_notify"):
            self.handler._reconcile_in_flight(self._cfg(), state)
        mock_log.assert_called_once_with(
            "dispatch_failed",
            repo="/fake/repo",
            pid=12345,
            result_path="/no/such/result.json",
        )

    def test_failed_dispatch_calls_on_error_notification(self):
        state = self._inflight_state("/fake/repo", pid=12345, result_path="/no/such/result.json")
        with patch.object(self.handler, "_is_process_alive", return_value=False), \
             patch.object(self.handler, "log_event"), \
             patch.object(self.handler, "_maybe_notify") as mock_notify:
            self.handler._reconcile_in_flight(self._cfg(), state)
        self.assertEqual(mock_notify.call_count, 1)
        _cfg_arg, _title, _msg, channel = mock_notify.call_args[0]
        self.assertEqual(channel, "on_error")

    def test_failed_dispatch_clears_in_flight_state(self):
        state = self._inflight_state("/fake/repo", pid=12345, result_path="/no/such/result.json")
        with patch.object(self.handler, "_is_process_alive", return_value=False), \
             patch.object(self.handler, "log_event"), \
             patch.object(self.handler, "_maybe_notify"):
            self.handler._reconcile_in_flight(self._cfg(), state)
        repo_state = state.repos["/fake/repo"]
        self.assertFalse(repo_state.in_flight)
        self.assertIsNone(repo_state.pid)
        self.assertIsNone(repo_state.result_path)

    def test_no_pid_no_notification(self):
        """State with no pid (e.g. corruption) should clear without notifying."""
        state = self._inflight_state("/fake/repo", pid=None, result_path="/no/such/result.json")
        with patch.object(self.handler, "_is_process_alive", return_value=False), \
             patch.object(self.handler, "log_event"), \
             patch.object(self.handler, "_maybe_notify") as mock_notify:
            self.handler._reconcile_in_flight(self._cfg(), state)
        mock_notify.assert_not_called()

    # ── successful completion ────────────────────────────────────────────────

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
                entries=[
                    {
                        "id": entry.id,
                        "text": "fix the bug",
                        "is_priority": False,
                        "line_index": 0,
                        "raw_line": "- [ ] fix the bug",
                    }
                ],
            )

            with patch.object(self.handler, "_is_process_alive", return_value=False):
                completed = self.handler._reconcile_in_flight(self._cfg(), state)

            self.assertEqual(completed, 1)
            updated = later_path.read_text(encoding="utf-8")
            self.assertIn("- [x] fix the bug", updated)
            self.assertIn("- [ ] update docs", updated)

    def test_completed_count_includes_all_resolved_repos(self):
        with tempfile.TemporaryDirectory() as td:
            result_file = Path(td) / "result.json"
            result_file.write_text("", encoding="utf-8")  # exists but empty = completed=1

            state = self.handler.AppState()
            for i in range(3):
                state.repos[f"/fake/repo{i}"] = self.handler.RepoState(
                    in_flight=True,
                    pid=None,
                    result_path=str(result_file),
                    entries=[],
                )

            with patch.object(self.handler, "_is_process_alive", return_value=False):
                completed = self.handler._reconcile_in_flight(self._cfg(), state)

        self.assertEqual(completed, 3)


if __name__ == "__main__":
    unittest.main()
