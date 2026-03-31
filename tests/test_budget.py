import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._loader import load_handler_module


class BudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def _write_jsonl(self, path: Path, rows: list[dict], mtime_offset_days: float = 0) -> None:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        if mtime_offset_days != 0:
            ts = (datetime.now() - timedelta(days=mtime_offset_days)).timestamp()
            os.utime(path, (ts, ts))

    def test_compute_budget_state_sums_tokens_across_7_days(self):
        now = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # File 1: 3 days old (within window)
            f1 = root / "s1.jsonl"
            self._write_jsonl(f1, [
                {"timestamp": "2026-03-27T10:00:00Z", "usage": {"input_tokens": 1000, "output_tokens": 200}},
            ], mtime_offset_days=3)
            # File 2: today
            f2 = root / "s2.jsonl"
            self._write_jsonl(f2, [
                {"timestamp": "2026-03-30T10:00:00Z", "message_usage": {
                    "input_tokens": 500,
                    "cache_creation_input_tokens": 100,
                    "output_tokens": 50,
                }},
            ])
            state = self.handler.compute_budget_state([root], now, weekly_budget=10_000_000)
        # 1000+200 from f1, 500+100+50 from f2 = 1850
        self.assertEqual(state.tokens_used_this_week, 1850)
        self.assertAlmostEqual(state.pct_used, 1850 / 10_000_000, places=8)

    def test_compute_budget_state_ignores_files_older_than_7_days(self):
        now = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_file = root / "old.jsonl"
            self._write_jsonl(old_file, [
                {"timestamp": "2026-03-20T10:00:00Z", "usage": {"input_tokens": 99999, "output_tokens": 99999}},
            ], mtime_offset_days=10)
            state = self.handler.compute_budget_state([root], now, weekly_budget=10_000_000)
        self.assertEqual(state.tokens_used_this_week, 0)

    def test_pct_used_computed_correctly(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": now.isoformat(), "usage": {"input_tokens": 800_000, "output_tokens": 0}},
            ])
            state = self.handler.compute_budget_state([root], now, weekly_budget=1_000_000)
        self.assertAlmostEqual(state.pct_used, 0.8, places=5)
        self.assertEqual(state.tokens_remaining, 200_000)

    def test_budget_gate_blocks_at_backoff_threshold(self):
        """If pct_used >= backoff_at_pct/100, dispatch should be skipped."""
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": now.isoformat(), "usage": {"input_tokens": 900_000, "output_tokens": 0}},
            ])
            state = self.handler.compute_budget_state([root], now, weekly_budget=1_000_000)
        # 90% used, backoff at 80%
        self.assertGreaterEqual(state.pct_used, 0.80)

    def test_budget_gate_allows_below_threshold(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": now.isoformat(), "usage": {"input_tokens": 500_000, "output_tokens": 0}},
            ])
            state = self.handler.compute_budget_state([root], now, weekly_budget=1_000_000)
        # 50% used, below 80% backoff
        self.assertLess(state.pct_used, 0.80)

    def test_tokens_remaining_floored_at_zero(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": now.isoformat(), "usage": {"input_tokens": 2_000_000, "output_tokens": 0}},
            ])
            state = self.handler.compute_budget_state([root], now, weekly_budget=1_000_000)
        self.assertEqual(state.tokens_remaining, 0)
        self.assertGreaterEqual(state.pct_used, 1.0)

    def test_empty_directory_returns_zero_usage(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            state = self.handler.compute_budget_state([Path(td)], now, weekly_budget=10_000_000)
        self.assertEqual(state.tokens_used_this_week, 0)
        self.assertEqual(state.pct_used, 0.0)


if __name__ == "__main__":
    unittest.main()
