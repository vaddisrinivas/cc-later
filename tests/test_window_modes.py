import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from tests._loader import load_handler_module


class WindowModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def test_should_dispatch_by_mode_always(self):
        now = datetime(2026, 3, 30, 1, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(
            self.handler.should_dispatch_by_mode(
                dispatch_mode="always",
                now_local=now,
                fallback_dispatch_hours=[],
                remaining_minutes=None,
                trigger_at_minutes_remaining=30,
            )
        )

    def test_should_dispatch_by_mode_time_based(self):
        now = datetime(2026, 3, 30, 1, 30, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(
            self.handler.should_dispatch_by_mode(
                dispatch_mode="time_based",
                now_local=now,
                fallback_dispatch_hours=["22:00-02:00"],
                remaining_minutes=None,
                trigger_at_minutes_remaining=30,
            )
        )

    def test_should_dispatch_by_mode_window_aware(self):
        now = datetime(2026, 3, 30, 1, 30, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(
            self.handler.should_dispatch_by_mode(
                dispatch_mode="window_aware",
                now_local=now,
                fallback_dispatch_hours=[],
                remaining_minutes=None,
                trigger_at_minutes_remaining=30,
            )
        )
        self.assertTrue(
            self.handler.should_dispatch_by_mode(
                dispatch_mode="window_aware",
                now_local=now,
                fallback_dispatch_hours=[],
                remaining_minutes=25,
                trigger_at_minutes_remaining=30,
            )
        )

    def test_compute_window_state_from_jsonl(self):
        now = datetime(2026, 3, 30, 2, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project_file = root / "session.jsonl"
            rows = [
                {
                    "timestamp": "2026-03-30T00:00:00Z",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
                {
                    "timestamp": "2026-03-30T01:00:00Z",
                    "usage": {"input_tokens": 20, "output_tokens": 10},
                },
            ]
            project_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            state = self.handler.compute_window_state([root], now_utc=now)
            self.assertIsNotNone(state)
            self.assertEqual(state.elapsed_minutes, 120)
            self.assertEqual(state.remaining_minutes, 180)
            self.assertEqual(state.total_input_tokens, 30)
            self.assertEqual(state.total_output_tokens, 15)


if __name__ == "__main__":
    unittest.main()
