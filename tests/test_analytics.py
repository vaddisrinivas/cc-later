"""Tests for the SQLite analytics engine."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys
_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from cc_later.analytics import AnalyticsDB


class AnalyticsDBTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_analytics.db"
        self.db = AnalyticsDB(db_path=self.db_path)

    def tearDown(self):
        self.db.close()

    def test_record_dispatch_returns_row_id(self):
        row_id = self.db.record_dispatch(
            repo="/fake/repo", task_id="t_abc", task_text="fix bug",
            section="Bugs", model="sonnet",
        )
        self.assertIsNotNone(row_id)
        self.assertGreater(row_id, 0)

    def test_record_and_retrieve_outcome(self):
        self.db.record_dispatch(
            repo="/repo", task_id="t_123", task_text="test task",
            section=None, model="opus",
        )
        self.db.record_outcome(
            task_id="t_123", repo="/repo", status="DONE",
            duration_s=42.5, input_tokens=1000, output_tokens=500,
        )
        recent = self.db.recent_dispatches(limit=1)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["status"], "DONE")
        self.assertEqual(recent[0]["duration_s"], 42.5)

    def test_stats_with_no_data(self):
        stats = self.db.get_stats()
        self.assertEqual(stats.total_dispatched, 0)
        self.assertEqual(stats.success_rate, 0.0)

    def test_stats_success_rate(self):
        for i in range(3):
            self.db.record_dispatch(
                repo="/repo", task_id=f"t_{i}", task_text=f"task {i}",
                section=None, model="sonnet",
            )
        self.db.record_outcome(task_id="t_0", repo="/repo", status="DONE")
        self.db.record_outcome(task_id="t_1", repo="/repo", status="DONE")
        self.db.record_outcome(task_id="t_2", repo="/repo", status="FAILED")

        stats = self.db.get_stats()
        self.assertEqual(stats.total_dispatched, 3)
        self.assertEqual(stats.total_completed, 2)
        self.assertEqual(stats.total_failed, 1)
        self.assertAlmostEqual(stats.success_rate, 2/3, places=2)

    def test_stats_by_model(self):
        self.db.record_dispatch(repo="/r", task_id="t_1", task_text="t", section=None, model="sonnet")
        self.db.record_dispatch(repo="/r", task_id="t_2", task_text="t", section=None, model="opus")
        self.db.record_outcome(task_id="t_1", repo="/r", status="DONE")
        self.db.record_outcome(task_id="t_2", repo="/r", status="DONE")

        stats = self.db.get_stats()
        self.assertIn("sonnet", stats.by_model)
        self.assertIn("opus", stats.by_model)

    def test_stats_by_section(self):
        self.db.record_dispatch(repo="/r", task_id="t_1", task_text="t", section="Security", model="s")
        self.db.record_dispatch(repo="/r", task_id="t_2", task_text="t", section="Tests", model="s")
        self.db.record_outcome(task_id="t_1", repo="/r", status="DONE")
        self.db.record_outcome(task_id="t_2", repo="/r", status="FAILED")

        stats = self.db.get_stats()
        self.assertIn("Security", stats.by_section)
        self.assertEqual(stats.by_section["Security"].completed, 1)

    def test_streak_counts_consecutive_successes(self):
        for i in range(5):
            self.db.record_dispatch(repo="/r", task_id=f"t_{i}", task_text="t", section=None, model="s")
            self.db.record_outcome(task_id=f"t_{i}", repo="/r", status="DONE")

        stats = self.db.get_stats()
        self.assertEqual(stats.streak, 5)

    def test_streak_broken_by_failure(self):
        for i in range(3):
            self.db.record_dispatch(repo="/r", task_id=f"t_{i}", task_text="t", section=None, model="s")

        self.db.record_outcome(task_id="t_0", repo="/r", status="DONE")
        self.db.record_outcome(task_id="t_1", repo="/r", status="FAILED")
        self.db.record_outcome(task_id="t_2", repo="/r", status="DONE")

        stats = self.db.get_stats()
        self.assertEqual(stats.streak, 1)  # only the last DONE


if __name__ == "__main__":
    unittest.main()
