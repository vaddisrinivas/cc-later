import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cc_later import core


class WindowBudgetTests(unittest.TestCase):
    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_window_state_ignores_stale_rows(self):
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "session.jsonl"
            self._write_jsonl(
                f,
                [
                    {"timestamp": "2026-04-05T09:00:00Z", "message": {"usage": {"input_tokens": 999, "output_tokens": 999}}},
                    {"timestamp": "2026-04-05T15:50:00Z", "message": {"usage": {"input_tokens": 20, "output_tokens": 10}}},
                ],
            )
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = core.compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.elapsed_minutes, 10)
        self.assertEqual(ws.remaining_minutes, 290)
        self.assertEqual(ws.total_input_tokens, 20)
        self.assertEqual(ws.total_output_tokens, 10)

    def test_weekly_budget_uses_recent_files(self):
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            recent = root / "recent.jsonl"
            stale = root / "stale.jsonl"
            self._write_jsonl(recent, [{"timestamp": "2026-04-05T14:30:00Z", "message": {"usage": {"input_tokens": 100, "output_tokens": 40}}}])
            self._write_jsonl(stale, [{"timestamp": "2026-03-01T10:00:00Z", "message": {"usage": {"input_tokens": 1000, "output_tokens": 1000}}}])

            os.utime(recent, (now.timestamp(), now.timestamp()))
            stale_ts = (now - timedelta(days=10)).timestamp()
            os.utime(stale, (stale_ts, stale_ts))

            budget = core.compute_budget_state([root], now, weekly_budget=1000)
        self.assertEqual(budget.used_tokens, 140)
        self.assertAlmostEqual(budget.pct_used, 0.14, places=5)


if __name__ == "__main__":
    unittest.main()
