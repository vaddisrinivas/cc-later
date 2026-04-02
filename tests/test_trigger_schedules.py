"""Tests for time-aware trigger schedules."""

import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from cc_later.window import resolve_trigger_threshold
from cc_later.models import DEFAULT_WINDOW_MINUTES


class TriggerScheduleTests(unittest.TestCase):

    def _at_hour(self, hour: int, minute: int = 0) -> datetime:
        """Create a local datetime at the given hour."""
        return datetime(2026, 4, 2, hour, minute, tzinfo=timezone(timedelta(hours=-7)))

    def test_disabled_returns_default(self):
        result = resolve_trigger_threshold(
            now_local=self._at_hour(2),  # 2am — would match night schedule
            trigger_at_minutes_remaining=30,
            trigger_schedules=[{"hours": "01:00-05:00", "remaining_pct": 10}],
            trigger_schedules_enabled=False,
        )
        self.assertEqual(result, 30)

    def test_empty_schedules_returns_default(self):
        result = resolve_trigger_threshold(
            now_local=self._at_hour(2),
            trigger_at_minutes_remaining=30,
            trigger_schedules=[],
            trigger_schedules_enabled=True,
        )
        self.assertEqual(result, 30)

    def test_night_schedule_10pct(self):
        """1am-5am at 10% → 30 minutes (10% of 300)."""
        result = resolve_trigger_threshold(
            now_local=self._at_hour(2, 30),
            trigger_at_minutes_remaining=90,  # default 30%
            trigger_schedules=[{"hours": "01:00-05:00", "remaining_pct": 10}],
            trigger_schedules_enabled=True,
        )
        self.assertEqual(result, 30)  # 10% of 300 = 30

    def test_day_schedule_30pct(self):
        """Outside night window at 30% → 90 minutes (30% of 300)."""
        schedules = [
            {"hours": "01:00-05:00", "remaining_pct": 10},
            {"hours": "05:00-01:00", "remaining_pct": 30},
        ]
        result = resolve_trigger_threshold(
            now_local=self._at_hour(14, 0),  # 2pm
            trigger_at_minutes_remaining=30,
            trigger_schedules=schedules,
            trigger_schedules_enabled=True,
        )
        self.assertEqual(result, 90)  # 30% of 300 = 90

    def test_first_match_wins(self):
        """When multiple schedules overlap, the first match is used."""
        schedules = [
            {"hours": "00:00-06:00", "remaining_pct": 5},
            {"hours": "01:00-05:00", "remaining_pct": 50},
        ]
        result = resolve_trigger_threshold(
            now_local=self._at_hour(3),
            trigger_at_minutes_remaining=30,
            trigger_schedules=schedules,
            trigger_schedules_enabled=True,
        )
        self.assertEqual(result, 15)  # 5% of 300 = 15 (first match)

    def test_overnight_window(self):
        """Schedule crossing midnight: 22:00-06:00."""
        schedules = [{"hours": "22:00-06:00", "remaining_pct": 10}]
        # At 23:00 — inside overnight window
        result = resolve_trigger_threshold(
            now_local=self._at_hour(23),
            trigger_at_minutes_remaining=90,
            trigger_schedules=schedules,
            trigger_schedules_enabled=True,
        )
        self.assertEqual(result, 30)

        # At 3:00 — inside overnight window (past midnight)
        result = resolve_trigger_threshold(
            now_local=self._at_hour(3),
            trigger_at_minutes_remaining=90,
            trigger_schedules=schedules,
            trigger_schedules_enabled=True,
        )
        self.assertEqual(result, 30)

        # At 12:00 — outside overnight window
        result = resolve_trigger_threshold(
            now_local=self._at_hour(12),
            trigger_at_minutes_remaining=90,
            trigger_schedules=schedules,
            trigger_schedules_enabled=True,
        )
        self.assertEqual(result, 90)  # falls back to default

    def test_no_matching_schedule_returns_default(self):
        schedules = [{"hours": "01:00-05:00", "remaining_pct": 10}]
        result = resolve_trigger_threshold(
            now_local=self._at_hour(12),
            trigger_at_minutes_remaining=45,
            trigger_schedules=schedules,
            trigger_schedules_enabled=True,
        )
        self.assertEqual(result, 45)

    def test_malformed_schedule_skipped(self):
        schedules = [
            {"hours": "invalid", "remaining_pct": 10},
            {"remaining_pct": 10},  # missing hours
            {"hours": "01:00-05:00"},  # missing remaining_pct
            {"hours": "01:00-05:00", "remaining_pct": 10},  # valid
        ]
        result = resolve_trigger_threshold(
            now_local=self._at_hour(3),
            trigger_at_minutes_remaining=90,
            trigger_schedules=schedules,
            trigger_schedules_enabled=True,
        )
        self.assertEqual(result, 30)  # 10% of 300 from valid entry

    def test_boundary_exactly_at_start(self):
        """At exactly 01:00, should match 01:00-05:00."""
        result = resolve_trigger_threshold(
            now_local=self._at_hour(1, 0),
            trigger_at_minutes_remaining=90,
            trigger_schedules=[{"hours": "01:00-05:00", "remaining_pct": 10}],
            trigger_schedules_enabled=True,
        )
        self.assertEqual(result, 30)

    def test_boundary_exactly_at_end(self):
        """At exactly 05:00, should NOT match 01:00-05:00."""
        result = resolve_trigger_threshold(
            now_local=self._at_hour(5, 0),
            trigger_at_minutes_remaining=90,
            trigger_schedules=[{"hours": "01:00-05:00", "remaining_pct": 10}],
            trigger_schedules_enabled=True,
        )
        self.assertEqual(result, 90)  # falls back to default


if __name__ == "__main__":
    unittest.main()
