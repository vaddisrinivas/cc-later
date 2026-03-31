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

    def test_window_state_returns_none_for_empty_directory(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.handler.compute_window_state([Path(td)], now_utc=datetime.now(timezone.utc))
        self.assertIsNone(state)

    def test_window_state_ignores_stale_jsonl(self):
        """JSONL files older than 5 hours are excluded from window calculation."""
        now = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
        stale_ts = "2026-03-30T06:00:00Z"  # exactly 6 hours before now — stale
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "old.jsonl"
            row = {"timestamp": stale_ts, "usage": {"input_tokens": 100, "output_tokens": 50}}
            f.write_text(json.dumps(row) + "\n", encoding="utf-8")
            import os
            # backdate file mtime to 6 hours before now_utc (not wall-clock time)
            six_hours_before_now = now.timestamp() - (6 * 3600 + 60)
            os.utime(f, (six_hours_before_now, six_hours_before_now))
            state = self.handler.compute_window_state([root], now_utc=now)
        self.assertIsNone(state)

    def test_time_ranges_empty_list_returns_false(self):
        now = datetime(2026, 3, 30, 2, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(self.handler.is_within_time_ranges(now, []))

    def test_time_ranges_overnight_window_wraps_correctly(self):
        # 22:00-02:00: at 23:30 should be inside; at 03:00 should be outside
        inside = datetime(2026, 3, 30, 23, 30, tzinfo=ZoneInfo("America/New_York"))
        outside = datetime(2026, 3, 30, 3, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(self.handler.is_within_time_ranges(inside, ["22:00-02:00"]))
        self.assertFalse(self.handler.is_within_time_ranges(outside, ["22:00-02:00"]))

    def test_time_ranges_exact_boundary(self):
        # Range 09:00-17:00: exactly 09:00 is inside; exactly 17:00 is outside
        at_start = datetime(2026, 3, 30, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        at_end = datetime(2026, 3, 30, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(self.handler.is_within_time_ranges(at_start, ["09:00-17:00"]))
        self.assertFalse(self.handler.is_within_time_ranges(at_end, ["09:00-17:00"]))

    def test_peak_window_detects_active_window(self):
        # Monday 10:00 AM PT — should be in a mon-fri 09:00-18:00 PT window
        now = datetime(2026, 3, 30, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))  # Monday
        windows = [{"days": "mon-fri", "start": "09:00", "end": "18:00", "tz": "America/Los_Angeles"}]
        self.assertTrue(self.handler._is_in_peak_window(now, windows))

    def test_peak_window_outside_returns_false(self):
        # Saturday 10:00 AM — outside mon-fri window
        now = datetime(2026, 4, 4, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))  # Saturday
        windows = [{"days": "mon-fri", "start": "09:00", "end": "18:00", "tz": "America/Los_Angeles"}]
        self.assertFalse(self.handler._is_in_peak_window(now, windows))

    def test_peak_window_empty_list_returns_false(self):
        now = datetime(2026, 3, 30, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        self.assertFalse(self.handler._is_in_peak_window(now, []))

    # ── Auto-trigger threshold tests (trigger_at_minutes_remaining = 85) ────

    def test_trigger_at_85_fires_when_remaining_lte_85(self):
        now = datetime(2026, 3, 30, 1, 30, tzinfo=ZoneInfo("America/New_York"))
        # Exactly at threshold — should fire
        self.assertTrue(
            self.handler.should_dispatch_by_mode(
                dispatch_mode="window_aware",
                now_local=now,
                fallback_dispatch_hours=[],
                remaining_minutes=85,
                trigger_at_minutes_remaining=85,
            )
        )
        # One minute inside threshold — should fire
        self.assertTrue(
            self.handler.should_dispatch_by_mode(
                dispatch_mode="window_aware",
                now_local=now,
                fallback_dispatch_hours=[],
                remaining_minutes=60,
                trigger_at_minutes_remaining=85,
            )
        )

    def test_trigger_at_85_does_not_fire_when_remaining_gt_85(self):
        now = datetime(2026, 3, 30, 1, 30, tzinfo=ZoneInfo("America/New_York"))
        # One minute outside threshold — must NOT fire
        self.assertFalse(
            self.handler.should_dispatch_by_mode(
                dispatch_mode="window_aware",
                now_local=now,
                fallback_dispatch_hours=[],
                remaining_minutes=86,
                trigger_at_minutes_remaining=85,
            )
        )
        # Plenty of time left — must not fire
        self.assertFalse(
            self.handler.should_dispatch_by_mode(
                dispatch_mode="window_aware",
                now_local=now,
                fallback_dispatch_hours=[],
                remaining_minutes=200,
                trigger_at_minutes_remaining=85,
            )
        )

    def test_auto_trigger_from_jsonl_fires_at_85_remaining(self):
        """End-to-end: a session started 215 min ago gives 85 remaining → triggers."""
        # Window is 300 min total. 215 elapsed → 85 remaining → exactly at threshold.
        now = datetime(2026, 3, 30, 4, 0, tzinfo=timezone.utc)
        session_start = "2026-03-30T00:25:00Z"  # 215 min before now
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "session.jsonl"
            f.write_text(
                json.dumps({"timestamp": session_start, "usage": {"input_tokens": 100, "output_tokens": 50}}) + "\n",
                encoding="utf-8",
            )
            state = self.handler.compute_window_state([root], now_utc=now)
        self.assertIsNotNone(state)
        self.assertEqual(state.elapsed_minutes, 215)
        self.assertEqual(state.remaining_minutes, 85)
        self.assertTrue(
            self.handler.should_dispatch_by_mode(
                dispatch_mode="window_aware",
                now_local=now.astimezone(),
                fallback_dispatch_hours=[],
                remaining_minutes=state.remaining_minutes,
                trigger_at_minutes_remaining=85,
            )
        )

    def test_auto_trigger_does_not_fire_early_in_session(self):
        """A fresh session (10 min elapsed) must not trigger at threshold=85."""
        now = datetime(2026, 3, 30, 4, 0, tzinfo=timezone.utc)
        session_start = "2026-03-30T03:50:00Z"  # only 10 min ago
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "session.jsonl"
            f.write_text(
                json.dumps({"timestamp": session_start, "usage": {"input_tokens": 50, "output_tokens": 20}}) + "\n",
                encoding="utf-8",
            )
            state = self.handler.compute_window_state([root], now_utc=now)
        self.assertIsNotNone(state)
        self.assertEqual(state.remaining_minutes, 290)
        self.assertFalse(
            self.handler.should_dispatch_by_mode(
                dispatch_mode="window_aware",
                now_local=now.astimezone(),
                fallback_dispatch_hours=[],
                remaining_minutes=state.remaining_minutes,
                trigger_at_minutes_remaining=85,
            )
        )

    def test_resolve_jsonl_roots_auto_discovers_claude_dir(self):
        """When jsonl_paths is empty, roots should include ~/.claude/projects."""
        cfg = self.handler.WindowConfig(jsonl_paths=[])
        roots = self.handler._resolve_jsonl_roots(cfg)
        root_strs = [str(r) for r in roots]
        # At least one of the auto-discovered paths should reference a claude directory
        self.assertTrue(
            any("claude" in s for s in root_strs),
            f"Expected a claude path in roots, got: {root_strs}",
        )

    def test_resolve_jsonl_roots_uses_explicit_paths_when_set(self):
        """When jsonl_paths is set, those exact paths are used (no auto-discovery)."""
        cfg = self.handler.WindowConfig(jsonl_paths=["/tmp/my-jsonl"])
        roots = self.handler._resolve_jsonl_roots(cfg)
        self.assertEqual(roots, [Path("/tmp/my-jsonl")])


if __name__ == "__main__":
    unittest.main()
