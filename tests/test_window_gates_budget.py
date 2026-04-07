import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cc_later import core
from cc_later.core import (
    AutoResumeConfig,
    BudgetState,
    Config,
    LimitsConfig,
    RepoState,
    State,
    WindowConfig,
    WindowState,
    _auto_resume_gate_open,
    _in_time_windows,
    _mode_gate_open,
    compute_budget_state,
    compute_window_state,
)


def _make_config(**overrides) -> Config:
    cfg = Config()
    for key, val in overrides.items():
        parts = key.split("__")
        obj = cfg
        for p in parts[:-1]:
            obj = getattr(obj, p)
        setattr(obj, parts[-1], val)
    return cfg


class TestComputeWindowState(unittest.TestCase):
    """Tests for compute_window_state()."""

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    # --- Fresh / None cases ---

    def test_fresh_session_last_row_too_old_returns_none(self):
        """If the most recent row is older than session_gap_minutes, returns None."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            # Row is 60 minutes old, gap default is 30 min
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:00:00Z", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNone(ws)

    def test_empty_jsonl_returns_none(self):
        """An empty JSONL file yields None."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            f.write_text("", encoding="utf-8")
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNone(ws)

    def test_rows_without_timestamps_skipped(self):
        """Rows missing all timestamp keys are ignored."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"usage": {"input_tokens": 100, "output_tokens": 50}},
                {"some_field": "no ts here"},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNone(ws)

    def test_no_jsonl_files_returns_none(self):
        """No JSONL files in root at all."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNone(ws)

    # --- Single session ---

    def test_single_session_elapsed_remaining(self):
        """Single session with multiple rows (gaps < 30min) computes correct elapsed/remaining."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            # All consecutive gaps < 30 min so they form one session starting at 14:00
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T14:00:00Z", "usage": {"input_tokens": 10, "output_tokens": 5}},
                {"timestamp": "2026-04-05T14:25:00Z", "usage": {"input_tokens": 20, "output_tokens": 10}},
                {"timestamp": "2026-04-05T14:50:00Z", "usage": {"input_tokens": 30, "output_tokens": 15}},
                {"timestamp": "2026-04-05T15:10:00Z", "usage": {"input_tokens": 5, "output_tokens": 2}},
                {"timestamp": "2026-04-05T15:30:00Z", "usage": {"input_tokens": 5, "output_tokens": 2}},
                {"timestamp": "2026-04-05T15:55:00Z", "usage": {"input_tokens": 5, "output_tokens": 2}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.elapsed_minutes, 120)  # 14:00 -> 16:00
        self.assertEqual(ws.remaining_minutes, 180)  # 300 - 120
        self.assertEqual(ws.elapsed_minutes + ws.remaining_minutes, 300)

    def test_single_session_token_totals(self):
        """Token sums across multiple rows in a single session."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:40:00Z", "usage": {"input_tokens": 100, "output_tokens": 50}},
                {"timestamp": "2026-04-05T15:50:00Z", "usage": {"input_tokens": 200, "output_tokens": 100}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.total_input_tokens, 300)
        self.assertEqual(ws.total_output_tokens, 150)

    # --- Multiple sessions with gaps ---

    def test_gap_picks_current_session_only(self):
        """A gap >= 30min splits sessions; only the latest session is counted."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                # Old session
                {"timestamp": "2026-04-05T13:00:00Z", "usage": {"input_tokens": 999, "output_tokens": 999}},
                {"timestamp": "2026-04-05T13:10:00Z", "usage": {"input_tokens": 888, "output_tokens": 888}},
                # Gap of 2+ hours
                # Current session
                {"timestamp": "2026-04-05T15:30:00Z", "usage": {"input_tokens": 10, "output_tokens": 5}},
                {"timestamp": "2026-04-05T15:50:00Z", "usage": {"input_tokens": 20, "output_tokens": 10}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        # Only current session tokens
        self.assertEqual(ws.total_input_tokens, 30)
        self.assertEqual(ws.total_output_tokens, 15)
        # Elapsed from 15:30 to 16:00 = 30 min
        self.assertEqual(ws.elapsed_minutes, 30)
        self.assertEqual(ws.remaining_minutes, 270)

    def test_multiple_gaps_picks_latest(self):
        """With multiple gaps, the session after the LAST gap is used."""
        now = datetime(2026, 4, 5, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T14:00:00Z", "usage": {"input_tokens": 1, "output_tokens": 1}},
                # gap 1 (1 hour) -- splits here
                {"timestamp": "2026-04-05T15:00:00Z", "usage": {"input_tokens": 2, "output_tokens": 2}},
                # gap 2 (2 hours) -- splits here
                # Current session: 3 rows all < 30min apart
                {"timestamp": "2026-04-05T17:00:00Z", "usage": {"input_tokens": 50, "output_tokens": 25}},
                {"timestamp": "2026-04-05T17:15:00Z", "usage": {"input_tokens": 50, "output_tokens": 25}},
                {"timestamp": "2026-04-05T17:40:00Z", "usage": {"input_tokens": 50, "output_tokens": 25}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        # Only the last session (after gap 2): 3 rows with 50+50+50 input, 25+25+25 output
        self.assertEqual(ws.total_input_tokens, 150)
        self.assertEqual(ws.total_output_tokens, 75)
        self.assertEqual(ws.elapsed_minutes, 60)  # 17:00 -> 18:00

    # --- Usage extraction paths ---

    def test_usage_from_message_usage(self):
        """Usage at row['message']['usage'] is the preferred path."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {
                    "timestamp": "2026-04-05T15:50:00Z",
                    "message": {"usage": {"input_tokens": 42, "output_tokens": 17}},
                },
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.total_input_tokens, 42)
        self.assertEqual(ws.total_output_tokens, 17)

    def test_usage_fallback_to_message_usage_key(self):
        """Falls back to row['message_usage'] when message.usage is absent."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {
                    "timestamp": "2026-04-05T15:50:00Z",
                    "message_usage": {"input_tokens": 33, "output_tokens": 11},
                },
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.total_input_tokens, 33)
        self.assertEqual(ws.total_output_tokens, 11)

    def test_usage_fallback_to_row_usage(self):
        """Falls back to row['usage'] when both message.usage and message_usage are absent."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {
                    "timestamp": "2026-04-05T15:50:00Z",
                    "usage": {"input_tokens": 55, "output_tokens": 22},
                },
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.total_input_tokens, 55)
        self.assertEqual(ws.total_output_tokens, 22)

    def test_cache_creation_input_tokens_included(self):
        """cache_creation_input_tokens is added to input count."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {
                    "timestamp": "2026-04-05T15:50:00Z",
                    "usage": {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 75},
                },
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.total_input_tokens, 175)  # 100 + 75
        self.assertEqual(ws.total_output_tokens, 50)

    # --- Filtering ---

    def test_future_timestamps_filtered(self):
        """Rows with timestamps > now + 5min are excluded."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:50:00Z", "usage": {"input_tokens": 10, "output_tokens": 5}},
                # 30 min in the future -- should be filtered
                {"timestamp": "2026-04-05T16:30:00Z", "usage": {"input_tokens": 999, "output_tokens": 999}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.total_input_tokens, 10)
        self.assertEqual(ws.total_output_tokens, 5)

    def test_old_rows_filtered_5h_cutoff(self):
        """Rows older than 5 hours are excluded."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                # 6 hours ago -- should be filtered
                {"timestamp": "2026-04-05T10:00:00Z", "usage": {"input_tokens": 999, "output_tokens": 999}},
                {"timestamp": "2026-04-05T15:50:00Z", "usage": {"input_tokens": 20, "output_tokens": 10}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.total_input_tokens, 20)
        self.assertEqual(ws.total_output_tokens, 10)

    def test_stale_file_mtime_skipped(self):
        """JSONL files with mtime > 5h old are skipped entirely."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "old.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:50:00Z", "usage": {"input_tokens": 50, "output_tokens": 25}},
            ])
            stale_ts = (now - timedelta(hours=6)).timestamp()
            os.utime(f, (stale_ts, stale_ts))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNone(ws)

    def test_300_minute_window_elapsed_plus_remaining(self):
        """Elapsed + remaining always equals DEFAULT_WINDOW_MINUTES (300)."""
        now = datetime(2026, 4, 5, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:00:00Z", "usage": {"input_tokens": 1, "output_tokens": 1}},
                {"timestamp": "2026-04-05T17:55:00Z", "usage": {"input_tokens": 1, "output_tokens": 1}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.elapsed_minutes + ws.remaining_minutes, 300)

    def test_window_remaining_clamps_at_zero(self):
        """If elapsed exceeds 300, remaining is clamped to 0."""
        now = datetime(2026, 4, 5, 22, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            # Build a contiguous session spanning ~5h with rows every 20 min.
            # Start at 17:01 (just inside the 5h cutoff from 22:00), end at 21:55.
            rows = []
            start = datetime(2026, 4, 5, 17, 1, tzinfo=timezone.utc)
            t = start
            while t <= datetime(2026, 4, 5, 21, 55, tzinfo=timezone.utc):
                rows.append({"timestamp": t.isoformat(), "usage": {"input_tokens": 1, "output_tokens": 1}})
                t += timedelta(minutes=20)
            self._write_jsonl(f, rows)
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        # elapsed = 22:00 - 17:01 = 299 min
        self.assertEqual(ws.elapsed_minutes, 299)
        self.assertEqual(ws.remaining_minutes, 1)

    def test_usage_message_is_not_dict_fallback(self):
        """When row['message'] is not a dict, fall back to row-level keys."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {
                    "timestamp": "2026-04-05T15:50:00Z",
                    "message": "not a dict",
                    "usage": {"input_tokens": 77, "output_tokens": 33},
                },
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.total_input_tokens, 77)
        self.assertEqual(ws.total_output_tokens, 33)

    def test_multiple_roots(self):
        """Rows from multiple root directories are combined."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            r1, r2 = Path(td1), Path(td2)
            f1 = r1 / "a.jsonl"
            f2 = r2 / "b.jsonl"
            self._write_jsonl(f1, [
                {"timestamp": "2026-04-05T15:40:00Z", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ])
            self._write_jsonl(f2, [
                {"timestamp": "2026-04-05T15:50:00Z", "usage": {"input_tokens": 20, "output_tokens": 10}},
            ])
            os.utime(f1, (now.timestamp(), now.timestamp()))
            os.utime(f2, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([r1, r2], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.total_input_tokens, 30)
        self.assertEqual(ws.total_output_tokens, 15)

    def test_no_usage_in_row_zero_tokens(self):
        """Rows with a timestamp but no usage data contribute 0 tokens."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:50:00Z"},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.total_input_tokens, 0)
        self.assertEqual(ws.total_output_tokens, 0)


class TestComputeBudgetState(unittest.TestCase):
    """Tests for compute_budget_state()."""

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_7_day_cutoff_recent_counted(self):
        """Only files modified within the last 7 days are counted."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            recent = root / "recent.jsonl"
            stale = root / "stale.jsonl"
            self._write_jsonl(recent, [
                {"timestamp": "2026-04-05T14:00:00Z", "usage": {"input_tokens": 100, "output_tokens": 50}},
            ])
            self._write_jsonl(stale, [
                {"timestamp": "2026-03-01T10:00:00Z", "usage": {"input_tokens": 9999, "output_tokens": 9999}},
            ])
            os.utime(recent, (now.timestamp(), now.timestamp()))
            stale_ts = (now - timedelta(days=10)).timestamp()
            os.utime(stale, (stale_ts, stale_ts))
            bs = compute_budget_state([root], now, weekly_budget=1000)
        self.assertEqual(bs.used_tokens, 150)

    def test_usage_from_message_usage_path(self):
        """Budget extraction uses row['message']['usage'] first."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {
                    "timestamp": "2026-04-05T15:00:00Z",
                    "message": {"usage": {"input_tokens": 200, "output_tokens": 100, "cache_creation_input_tokens": 50}},
                },
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            bs = compute_budget_state([root], now, weekly_budget=1000)
        # 200 input + 50 cache_creation + 100 output = 350
        self.assertEqual(bs.used_tokens, 350)

    def test_pct_used_calculation(self):
        """pct_used = used / budget."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:00:00Z", "usage": {"input_tokens": 250, "output_tokens": 250}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            bs = compute_budget_state([root], now, weekly_budget=1000)
        self.assertAlmostEqual(bs.pct_used, 0.5, places=5)

    def test_pct_used_capped_at_1(self):
        """pct_used is capped at 1.0 even if usage exceeds budget."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:00:00Z", "usage": {"input_tokens": 2000, "output_tokens": 0}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            bs = compute_budget_state([root], now, weekly_budget=1000)
        self.assertEqual(bs.pct_used, 1.0)

    def test_zero_budget_no_division_error(self):
        """Zero budget does not cause ZeroDivisionError (max(1, budget) guard)."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:00:00Z", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            bs = compute_budget_state([root], now, weekly_budget=0)
        self.assertEqual(bs.used_tokens, 15)
        # max(1, 0) = 1, 15/1 capped at 1.0
        self.assertEqual(bs.pct_used, 1.0)

    def test_no_jsonl_files_zero_used(self):
        """No JSONL files yields 0 used tokens."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bs = compute_budget_state([root], now, weekly_budget=5000)
        self.assertEqual(bs.used_tokens, 0)
        self.assertAlmostEqual(bs.pct_used, 0.0, places=5)

    def test_budget_usage_fallback_to_message_usage(self):
        """Budget falls back to row['message_usage'] when message.usage absent."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:00:00Z", "message_usage": {"input_tokens": 80, "output_tokens": 20}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            bs = compute_budget_state([root], now, weekly_budget=1000)
        self.assertEqual(bs.used_tokens, 100)

    def test_budget_usage_fallback_to_row_usage(self):
        """Budget falls back to row['usage'] as final option."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:00:00Z", "usage": {"input_tokens": 60, "output_tokens": 40}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            bs = compute_budget_state([root], now, weekly_budget=1000)
        self.assertEqual(bs.used_tokens, 100)


class TestModeGateOpen(unittest.TestCase):
    """Tests for _mode_gate_open()."""

    def test_always_mode_returns_true(self):
        cfg = _make_config(window__dispatch_mode="always")
        self.assertTrue(_mode_gate_open(cfg, datetime.now(), None))

    def test_always_mode_ignores_window_state(self):
        cfg = _make_config(window__dispatch_mode="always")
        ws = WindowState(elapsed_minutes=10, remaining_minutes=290, total_input_tokens=0, total_output_tokens=0)
        self.assertTrue(_mode_gate_open(cfg, datetime.now(), ws))

    def test_window_aware_none_window_returns_false(self):
        cfg = _make_config(window__dispatch_mode="window_aware")
        self.assertFalse(_mode_gate_open(cfg, datetime.now(), None))

    def test_window_aware_remaining_above_trigger_returns_false(self):
        cfg = _make_config(window__dispatch_mode="window_aware", window__trigger_at_minutes_remaining=30)
        ws = WindowState(elapsed_minutes=100, remaining_minutes=200, total_input_tokens=0, total_output_tokens=0)
        self.assertFalse(_mode_gate_open(cfg, datetime.now(), ws))

    def test_window_aware_remaining_at_trigger_returns_true(self):
        cfg = _make_config(window__dispatch_mode="window_aware", window__trigger_at_minutes_remaining=30)
        ws = WindowState(elapsed_minutes=270, remaining_minutes=30, total_input_tokens=0, total_output_tokens=0)
        self.assertTrue(_mode_gate_open(cfg, datetime.now(), ws))

    def test_window_aware_remaining_below_trigger_returns_true(self):
        cfg = _make_config(window__dispatch_mode="window_aware", window__trigger_at_minutes_remaining=30)
        ws = WindowState(elapsed_minutes=280, remaining_minutes=20, total_input_tokens=0, total_output_tokens=0)
        self.assertTrue(_mode_gate_open(cfg, datetime.now(), ws))

    def test_time_based_in_window_returns_true(self):
        cfg = _make_config(
            window__dispatch_mode="time_based",
            window__fallback_dispatch_hours=["09:00-17:00"],
        )
        noon = datetime(2026, 4, 5, 12, 0)
        self.assertTrue(_mode_gate_open(cfg, noon, None))

    def test_time_based_outside_window_returns_false(self):
        cfg = _make_config(
            window__dispatch_mode="time_based",
            window__fallback_dispatch_hours=["09:00-17:00"],
        )
        evening = datetime(2026, 4, 5, 20, 0)
        self.assertFalse(_mode_gate_open(cfg, evening, None))


class TestAutoResumeGateOpen(unittest.TestCase):
    """Tests for _auto_resume_gate_open()."""

    def _state_with_resume(self, repo_path: str, entries: list[dict]) -> State:
        return State(repos={repo_path: RepoState(resume_entries=entries)})

    def test_has_entries_enough_remaining_returns_true(self):
        cfg = _make_config(
            auto_resume__enabled=True,
            auto_resume__min_remaining_minutes=240,
            window__dispatch_mode="window_aware",
        )
        repo = Path("/tmp/fake-repo")
        state = self._state_with_resume(str(repo), [{"task": "resume_me"}])
        ws = WindowState(elapsed_minutes=30, remaining_minutes=270, total_input_tokens=0, total_output_tokens=0)
        self.assertTrue(_auto_resume_gate_open(cfg, [repo], state, ws))

    def test_no_entries_returns_false(self):
        cfg = _make_config(auto_resume__enabled=True, window__dispatch_mode="window_aware")
        repo = Path("/tmp/fake-repo")
        state = State(repos={str(repo): RepoState(resume_entries=[])})
        ws = WindowState(elapsed_minutes=30, remaining_minutes=270, total_input_tokens=0, total_output_tokens=0)
        self.assertFalse(_auto_resume_gate_open(cfg, [repo], state, ws))

    def test_remaining_too_low_returns_false(self):
        cfg = _make_config(
            auto_resume__enabled=True,
            auto_resume__min_remaining_minutes=240,
            window__dispatch_mode="window_aware",
        )
        repo = Path("/tmp/fake-repo")
        state = self._state_with_resume(str(repo), [{"task": "resume_me"}])
        ws = WindowState(elapsed_minutes=200, remaining_minutes=100, total_input_tokens=0, total_output_tokens=0)
        self.assertFalse(_auto_resume_gate_open(cfg, [repo], state, ws))

    def test_disabled_returns_false(self):
        cfg = _make_config(auto_resume__enabled=False, window__dispatch_mode="window_aware")
        repo = Path("/tmp/fake-repo")
        state = self._state_with_resume(str(repo), [{"task": "resume_me"}])
        ws = WindowState(elapsed_minutes=30, remaining_minutes=270, total_input_tokens=0, total_output_tokens=0)
        self.assertFalse(_auto_resume_gate_open(cfg, [repo], state, ws))

    def test_non_window_aware_mode_ignores_remaining(self):
        """When dispatch_mode is not window_aware, auto_resume just needs entries."""
        cfg = _make_config(
            auto_resume__enabled=True,
            auto_resume__min_remaining_minutes=240,
            window__dispatch_mode="always",
        )
        repo = Path("/tmp/fake-repo")
        state = self._state_with_resume(str(repo), [{"task": "resume_me"}])
        # window_state is None, but mode is "always" so it should still return True
        self.assertTrue(_auto_resume_gate_open(cfg, [repo], state, None))

    def test_window_state_none_window_aware_returns_false(self):
        """window_aware mode with no window state yields False even with entries."""
        cfg = _make_config(
            auto_resume__enabled=True,
            auto_resume__min_remaining_minutes=240,
            window__dispatch_mode="window_aware",
        )
        repo = Path("/tmp/fake-repo")
        state = self._state_with_resume(str(repo), [{"task": "resume_me"}])
        self.assertFalse(_auto_resume_gate_open(cfg, [repo], state, None))

    def test_repo_not_in_state_returns_false(self):
        """If the repo has no entry in state.repos, no pending resume."""
        cfg = _make_config(auto_resume__enabled=True, window__dispatch_mode="window_aware")
        repo = Path("/tmp/not-in-state")
        state = State(repos={})
        ws = WindowState(elapsed_minutes=30, remaining_minutes=270, total_input_tokens=0, total_output_tokens=0)
        self.assertFalse(_auto_resume_gate_open(cfg, [repo], state, ws))


class TestInTimeWindows(unittest.TestCase):
    """Tests for _in_time_windows()."""

    def test_within_daytime_window(self):
        dt = datetime(2026, 4, 5, 12, 0)
        self.assertTrue(_in_time_windows(dt, ["09:00-17:00"]))

    def test_outside_daytime_window(self):
        dt = datetime(2026, 4, 5, 20, 0)
        self.assertFalse(_in_time_windows(dt, ["09:00-17:00"]))

    def test_at_start_boundary_inclusive(self):
        dt = datetime(2026, 4, 5, 9, 0)
        self.assertTrue(_in_time_windows(dt, ["09:00-17:00"]))

    def test_at_end_boundary_exclusive(self):
        dt = datetime(2026, 4, 5, 17, 0)
        self.assertFalse(_in_time_windows(dt, ["09:00-17:00"]))

    def test_overnight_window_at_night(self):
        dt = datetime(2026, 4, 5, 23, 0)
        self.assertTrue(_in_time_windows(dt, ["22:00-06:00"]))

    def test_overnight_window_early_morning(self):
        dt = datetime(2026, 4, 6, 3, 0)
        self.assertTrue(_in_time_windows(dt, ["22:00-06:00"]))

    def test_overnight_window_at_midday_false(self):
        dt = datetime(2026, 4, 5, 12, 0)
        self.assertFalse(_in_time_windows(dt, ["22:00-06:00"]))

    def test_multiple_windows_match_second(self):
        dt = datetime(2026, 4, 5, 20, 30)
        self.assertTrue(_in_time_windows(dt, ["09:00-12:00", "20:00-22:00"]))

    def test_empty_windows_list(self):
        dt = datetime(2026, 4, 5, 12, 0)
        self.assertFalse(_in_time_windows(dt, []))

    def test_invalid_window_format_skipped(self):
        dt = datetime(2026, 4, 5, 12, 0)
        self.assertFalse(_in_time_windows(dt, ["not_a_window", "garbage"]))

    def test_same_start_end_skipped(self):
        """A window like 09:00-09:00 (zero length) matches nothing."""
        dt = datetime(2026, 4, 5, 9, 0)
        self.assertFalse(_in_time_windows(dt, ["09:00-09:00"]))


class TestComputeWindowStateEdgeCases(unittest.TestCase):
    """Edge cases for compute_window_state."""

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_window_start_hint_in_future(self):
        """compute_window_state with window_start_hint in the future.

        When the hint is in the future, filtering rows by ts >= future_hint yields
        no rows, so the code falls back to gap-based session detection.
        """
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        future_hint = datetime(2026, 4, 5, 17, 0, tzinfo=timezone.utc)  # 1h in future
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:50:00Z", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now, window_start_hint=future_hint)
        self.assertIsNotNone(ws)
        # Future hint causes no rows to pass ts >= earliest filter,
        # fallback uses gap-based: session_start = 15:50, elapsed = 10min
        self.assertEqual(ws.elapsed_minutes, 10)
        self.assertEqual(ws.remaining_minutes, 290)

    def test_window_start_hint_older_than_cutoff(self):
        """compute_window_state with window_start_hint older than max_start (clamped)."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        old_hint = datetime(2026, 4, 5, 8, 0, tzinfo=timezone.utc)  # 8h ago
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:50:00Z", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now, window_start_hint=old_hint)
        self.assertIsNotNone(ws)
        # Old hint is before max_start (11:00 UTC), so gap detection is used instead
        # Gap detection: single row at 15:50, so session_start = 15:50
        # elapsed = 16:00 - 15:50 = 10 min
        self.assertEqual(ws.elapsed_minutes, 10)
        self.assertEqual(ws.remaining_minutes, 290)

    def test_window_duration_zero(self):
        """compute_window_state with window_duration=0."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:50:00Z", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now, window_duration=0)
        self.assertIsNotNone(ws)
        # With duration=0, max_start = now, so earliest = max(gap_start, now)
        # elapsed = now - now = 0, remaining = max(0, 0 - 0) = 0
        self.assertEqual(ws.remaining_minutes, 0)


class TestComputeBudgetStateEdgeCases(unittest.TestCase):
    """Edge cases for compute_budget_state."""

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_non_dict_message_field(self):
        """compute_budget_state with row['message'] being a non-dict (string)."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {
                    "timestamp": "2026-04-05T15:00:00Z",
                    "message": "just a string, not a dict",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            bs = compute_budget_state([root], now, weekly_budget=1000)
        # message is not a dict, so it should fall back to row['usage']
        self.assertEqual(bs.used_tokens, 150)


class TestComputeWindowStateHardening(unittest.TestCase):
    """Hardening tests for compute_window_state()."""

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_10000_rows_performance(self):
        """compute_window_state with 10000+ JSONL rows should complete reasonably fast."""
        import time
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "big.jsonl"
            # Generate 10000 rows all within the window
            rows = []
            base = datetime(2026, 4, 5, 15, 0, tzinfo=timezone.utc)
            for i in range(10000):
                ts = base + timedelta(seconds=i * 0.3)  # ~50 min spread
                rows.append({
                    "timestamp": ts.isoformat(),
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                })
            self._write_jsonl(f, rows)
            os.utime(f, (now.timestamp(), now.timestamp()))
            start = time.monotonic()
            ws = compute_window_state([root], now_utc=now)
            elapsed = time.monotonic() - start
        self.assertIsNotNone(ws)
        self.assertLess(elapsed, 5.0, f"compute_window_state took {elapsed:.2f}s for 10000 rows")
        self.assertEqual(ws.total_input_tokens, 10000)

    def test_all_rows_no_timestamp(self):
        """compute_window_state when ALL rows have no timestamp returns None."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "no_ts.jsonl"
            self._write_jsonl(f, [
                {"usage": {"input_tokens": 100, "output_tokens": 50}},
                {"usage": {"input_tokens": 200, "output_tokens": 100}},
                {"some_field": "no timestamp"},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNone(ws)


class TestComputeBudgetStateHardening(unittest.TestCase):
    """Hardening tests for compute_budget_state()."""

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_negative_token_values(self):
        """compute_budget_state with negative token values in usage."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "neg.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:00:00Z", "usage": {"input_tokens": -100, "output_tokens": -50}},
                {"timestamp": "2026-04-05T15:10:00Z", "usage": {"input_tokens": 200, "output_tokens": 100}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            bs = compute_budget_state([root], now, weekly_budget=1000)
        # Negative values will be added as-is; used_tokens = -100 + -50 + 200 + 100 = 150
        self.assertIsInstance(bs.used_tokens, int)


class TestModeGateOpenHardening(unittest.TestCase):
    """Hardening tests for _mode_gate_open()."""

    def test_unknown_dispatch_mode(self):
        """_mode_gate_open with unknown dispatch_mode value should return False (default)."""
        cfg = _make_config(window__dispatch_mode="unknown_mode_xyz")
        # With an unknown mode, the function should not crash
        # It falls through to the last return which checks window_state
        result = _mode_gate_open(cfg, datetime.now(), None)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Negative tests: window state denial conditions
# ---------------------------------------------------------------------------
class TestComputeWindowStateNegative(unittest.TestCase):
    """Negative tests verifying compute_window_state correctly denies/returns None."""

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_empty_roots_returns_none(self):
        """compute_window_state with roots=[] (no roots) returns None."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        ws = compute_window_state([], now_utc=now)
        self.assertIsNone(ws)

    def test_roots_pointing_to_empty_directory_returns_none(self):
        """compute_window_state with roots pointing to an empty directory returns None."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNone(ws)

    def test_roots_pointing_to_nonexistent_directory_returns_none(self):
        """compute_window_state with roots pointing to a non-existent directory returns None."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        ws = compute_window_state([Path("/tmp/nonexistent_cc_later_test_dir_xyz")], now_utc=now)
        self.assertIsNone(ws)

    def test_all_rows_outside_5h_cutoff_returns_none(self):
        """compute_window_state with only rows from 6 hours ago (all outside 5h cutoff) returns None."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T09:00:00Z", "usage": {"input_tokens": 100, "output_tokens": 50}},
                {"timestamp": "2026-04-05T09:30:00Z", "usage": {"input_tokens": 200, "output_tokens": 100}},
            ])
            # Set mtime to recent so file is not skipped by mtime check
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNone(ws)

    def test_window_duration_1_activity_from_2_min_ago_remaining_zero(self):
        """compute_window_state with window_duration=1 and activity from 2 min ago has remaining=0."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:58:00Z", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now, window_duration=1)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.remaining_minutes, 0)

    def test_all_rows_zero_tokens_elapsed_correct(self):
        """compute_window_state with all rows having zero tokens yields correct elapsed but 0 tokens."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:30:00Z", "usage": {"input_tokens": 0, "output_tokens": 0}},
                {"timestamp": "2026-04-05T15:50:00Z", "usage": {"input_tokens": 0, "output_tokens": 0}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.total_input_tokens, 0)
        self.assertEqual(ws.total_output_tokens, 0)
        self.assertEqual(ws.elapsed_minutes, 30)  # 15:30 -> 16:00

    def test_only_future_timestamps_returns_none(self):
        """compute_window_state when JSONL has rows with future timestamps only returns None."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T17:00:00Z", "usage": {"input_tokens": 100, "output_tokens": 50}},
                {"timestamp": "2026-04-05T18:00:00Z", "usage": {"input_tokens": 200, "output_tokens": 100}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now)
        self.assertIsNone(ws)

    def test_window_start_hint_none_no_gaps_uses_earliest_row(self):
        """compute_window_state with window_start_hint=None and no gaps uses earliest row."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            # All gaps < 30 min so they form one session starting at 15:00
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:00:00Z", "usage": {"input_tokens": 10, "output_tokens": 5}},
                {"timestamp": "2026-04-05T15:15:00Z", "usage": {"input_tokens": 20, "output_tokens": 10}},
                {"timestamp": "2026-04-05T15:35:00Z", "usage": {"input_tokens": 30, "output_tokens": 15}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now, window_start_hint=None)
        self.assertIsNotNone(ws)
        # No gaps >= 30 min, no hint -> uses earliest row at 15:00
        self.assertEqual(ws.elapsed_minutes, 60)  # 15:00 -> 16:00
        self.assertEqual(ws.total_input_tokens, 60)

    def test_session_id_filter_matches_no_files_returns_none(self):
        """compute_window_state when session_id filter matches no files returns None."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "session_abc.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:50:00Z", "usage": {"input_tokens": 100, "output_tokens": 50}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = compute_window_state([root], now_utc=now, session_id="nonexistent_session_xyz")
        self.assertIsNone(ws)


# ---------------------------------------------------------------------------
# Negative tests: budget denial conditions
# ---------------------------------------------------------------------------
class TestComputeBudgetStateNegative(unittest.TestCase):
    """Negative tests verifying compute_budget_state correctly denies/returns 0."""

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_empty_roots_returns_zero(self):
        """compute_budget_state with roots=[] returns 0 used, 0%."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        bs = compute_budget_state([], now, weekly_budget=10_000_000)
        self.assertEqual(bs.used_tokens, 0)
        self.assertAlmostEqual(bs.pct_used, 0.0, places=5)

    def test_nonexistent_dir_returns_zero(self):
        """compute_budget_state with roots pointing to non-existent dir returns 0."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        bs = compute_budget_state([Path("/tmp/nonexistent_cc_later_budget_test_xyz")], now, weekly_budget=10_000_000)
        self.assertEqual(bs.used_tokens, 0)

    def test_all_files_older_than_7_days_returns_zero(self):
        """compute_budget_state with all files older than 7 days returns 0."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-03-01T10:00:00Z", "usage": {"input_tokens": 500, "output_tokens": 250}},
            ])
            stale_ts = (now - timedelta(days=10)).timestamp()
            os.utime(f, (stale_ts, stale_ts))
            bs = compute_budget_state([root], now, weekly_budget=10_000_000)
        self.assertEqual(bs.used_tokens, 0)

    def test_weekly_budget_1_and_10_tokens_pct_capped(self):
        """compute_budget_state with weekly_budget=1 and 10 tokens used -> pct_used capped at 1.0."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:00:00Z", "usage": {"input_tokens": 5, "output_tokens": 5}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            bs = compute_budget_state([root], now, weekly_budget=1)
        self.assertEqual(bs.used_tokens, 10)
        self.assertEqual(bs.pct_used, 1.0)

    def test_rows_with_no_usage_data_returns_zero(self):
        """compute_budget_state with rows containing no usage data returns 0."""
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "s.jsonl"
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:00:00Z", "some_field": "no usage"},
                {"timestamp": "2026-04-05T15:10:00Z"},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            bs = compute_budget_state([root], now, weekly_budget=10_000_000)
        self.assertEqual(bs.used_tokens, 0)
        self.assertAlmostEqual(bs.pct_used, 0.0, places=5)


# ---------------------------------------------------------------------------
# Negative tests: gate logic denial conditions
# ---------------------------------------------------------------------------
class TestModeGateOpenNegative(unittest.TestCase):
    """Negative tests verifying _mode_gate_open correctly denies dispatch."""

    def test_window_aware_window_state_none_returns_false(self):
        """_mode_gate_open with window_aware and window_state=None returns False."""
        cfg = _make_config(window__dispatch_mode="window_aware")
        self.assertFalse(_mode_gate_open(cfg, datetime.now(), None))

    def test_window_aware_remaining_300_returns_false(self):
        """_mode_gate_open with window_aware and remaining=300 returns False (full window)."""
        cfg = _make_config(window__dispatch_mode="window_aware", window__trigger_at_minutes_remaining=30)
        ws = WindowState(elapsed_minutes=0, remaining_minutes=300, total_input_tokens=0, total_output_tokens=0)
        self.assertFalse(_mode_gate_open(cfg, datetime.now(), ws))

    def test_window_aware_remaining_zero_returns_true(self):
        """_mode_gate_open with window_aware and remaining=0 returns True (window exhausted)."""
        cfg = _make_config(window__dispatch_mode="window_aware", window__trigger_at_minutes_remaining=30)
        ws = WindowState(elapsed_minutes=300, remaining_minutes=0, total_input_tokens=0, total_output_tokens=0)
        self.assertTrue(_mode_gate_open(cfg, datetime.now(), ws))

    def test_time_based_empty_fallback_hours_returns_false(self):
        """_mode_gate_open with time_based and empty fallback_dispatch_hours returns False."""
        cfg = _make_config(window__dispatch_mode="time_based", window__fallback_dispatch_hours=[])
        self.assertFalse(_mode_gate_open(cfg, datetime(2026, 4, 5, 12, 0), None))


class TestAutoResumeGateOpenNegative(unittest.TestCase):
    """Negative tests verifying _auto_resume_gate_open correctly denies dispatch."""

    def _state_with_resume(self, repo_path: str, entries: list[dict]) -> State:
        return State(repos={repo_path: RepoState(resume_entries=entries)})

    def test_window_aware_window_state_none_returns_false(self):
        """_auto_resume_gate_open with window_aware and window_state=None returns False."""
        cfg = _make_config(
            auto_resume__enabled=True,
            auto_resume__min_remaining_minutes=240,
            window__dispatch_mode="window_aware",
        )
        repo = Path("/tmp/fake-repo-neg")
        state = self._state_with_resume(str(repo), [{"task": "resume_me"}])
        self.assertFalse(_auto_resume_gate_open(cfg, [repo], state, None))

    def test_auto_resume_disabled_returns_false(self):
        """_auto_resume_gate_open with resume entries but auto_resume disabled returns False."""
        cfg = _make_config(auto_resume__enabled=False, window__dispatch_mode="window_aware")
        repo = Path("/tmp/fake-repo-neg")
        state = self._state_with_resume(str(repo), [{"task": "resume_me"}])
        ws = WindowState(elapsed_minutes=30, remaining_minutes=270, total_input_tokens=0, total_output_tokens=0)
        self.assertFalse(_auto_resume_gate_open(cfg, [repo], state, ws))

    def test_remaining_below_min_returns_false(self):
        """_auto_resume_gate_open with resume entries but remaining < min_remaining returns False."""
        cfg = _make_config(
            auto_resume__enabled=True,
            auto_resume__min_remaining_minutes=240,
            window__dispatch_mode="window_aware",
        )
        repo = Path("/tmp/fake-repo-neg")
        state = self._state_with_resume(str(repo), [{"task": "resume_me"}])
        ws = WindowState(elapsed_minutes=200, remaining_minutes=100, total_input_tokens=0, total_output_tokens=0)
        self.assertFalse(_auto_resume_gate_open(cfg, [repo], state, ws))

    def test_empty_resume_entries_across_all_repos_returns_false(self):
        """_auto_resume_gate_open with empty resume entries across ALL repos returns False."""
        cfg = _make_config(
            auto_resume__enabled=True,
            auto_resume__min_remaining_minutes=240,
            window__dispatch_mode="window_aware",
        )
        repo1 = Path("/tmp/fake-repo1-neg")
        repo2 = Path("/tmp/fake-repo2-neg")
        state = State(repos={
            str(repo1): RepoState(resume_entries=[]),
            str(repo2): RepoState(resume_entries=[]),
        })
        ws = WindowState(elapsed_minutes=30, remaining_minutes=270, total_input_tokens=0, total_output_tokens=0)
        self.assertFalse(_auto_resume_gate_open(cfg, [repo1, repo2], state, ws))


class TestInTimeWindowsNegative(unittest.TestCase):
    """Negative tests verifying _in_time_windows correctly denies."""

    def test_current_time_exactly_at_window_end_exclusive(self):
        """_in_time_windows with current time exactly at window end returns False (exclusive)."""
        dt = datetime(2026, 4, 5, 17, 0)
        self.assertFalse(_in_time_windows(dt, ["09:00-17:00"]))

    def test_single_invalid_entry_garbage_returns_false(self):
        """_in_time_windows with single invalid entry 'garbage' returns False."""
        dt = datetime(2026, 4, 5, 12, 0)
        self.assertFalse(_in_time_windows(dt, ["garbage"]))


if __name__ == "__main__":
    unittest.main()
