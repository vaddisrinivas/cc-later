import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cc_later import core


class PlanLimitsTests(unittest.TestCase):
    def test_plan_limits_has_all_plans(self):
        for plan in ("free", "pro", "max", "team", "enterprise"):
            self.assertIn(plan, core.PLAN_LIMITS)
            self.assertIn("window_minutes", core.PLAN_LIMITS[plan])
            self.assertIn("models", core.PLAN_LIMITS[plan])

    def test_plan_window_minutes_compat(self):
        """PLAN_WINDOW_MINUTES is derived from PLAN_LIMITS."""
        for plan, minutes in core.PLAN_WINDOW_MINUTES.items():
            self.assertEqual(minutes, core.PLAN_LIMITS[plan]["window_minutes"])

    def test_max_plan_has_extended_thinking(self):
        self.assertTrue(core.PLAN_LIMITS["max"].get("extended_thinking"))

    def test_free_plan_no_opus(self):
        self.assertNotIn("opus", core.PLAN_LIMITS["free"]["models"])


class MonitorConfigTests(unittest.TestCase):
    def test_monitor_config_defaults(self):
        cfg = core.MonitorConfig()
        self.assertEqual(cfg.warn_window_minutes, 60)
        self.assertEqual(cfg.warn_budget_pct, 70)
        self.assertTrue(cfg.notify_enabled)
        self.assertFalse(cfg.query_claude)

    def test_config_includes_monitor(self):
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / "config.env"
            env_path.write_text(
                "PLAN=max\nMONITOR_WARN_WINDOW_MINUTES=30\nMONITOR_WARN_BUDGET_PCT=80\n"
                "MONITOR_NOTIFY_ENABLED=false\nMONITOR_QUERY_CLAUDE=true\n"
            )
            with patch.object(core, "config_path", return_value=env_path), \
                 patch.object(core, "app_dir", return_value=Path(td)):
                cfg = core.load_config()
        self.assertEqual(cfg.monitor.warn_window_minutes, 30)
        self.assertEqual(cfg.monitor.warn_budget_pct, 80)
        self.assertFalse(cfg.monitor.notify_enabled)
        self.assertTrue(cfg.monitor.query_claude)


class WindowStateBurnRateTests(unittest.TestCase):
    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_burn_rate_computed(self):
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "session.jsonl"
            # 10 minutes elapsed, 300 tokens total → 30 t/min
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T15:50:00Z", "message": {"usage": {"input_tokens": 200, "output_tokens": 100}}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = core.compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.burn_rate_tpm, 30)

    def test_burn_rate_zero_at_start(self):
        now = datetime(2026, 4, 5, 16, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "session.jsonl"
            # Row at exact now → 0 elapsed
            self._write_jsonl(f, [
                {"timestamp": "2026-04-05T16:00:00Z", "message": {"usage": {"input_tokens": 100, "output_tokens": 50}}},
            ])
            os.utime(f, (now.timestamp(), now.timestamp()))
            ws = core.compute_window_state([root], now_utc=now)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.burn_rate_tpm, 0)


class ScanLimitEventsTests(unittest.TestCase):
    def test_scan_empty_log(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(core, "run_log_path", return_value=Path(td) / "log.jsonl"):
                events = core._scan_limit_events(hours=24)
        self.assertEqual(events["window_exhausted"], 0)

    def test_scan_counts_recent_events(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log.jsonl"
            now = datetime.now(timezone.utc)
            recent_ts = (now - timedelta(hours=1)).isoformat()
            old_ts = (now - timedelta(hours=48)).isoformat()
            log.write_text(
                json.dumps({"ts": recent_ts, "event": "window_exhausted"}) + "\n"
                + json.dumps({"ts": recent_ts, "event": "window_exhausted"}) + "\n"
                + json.dumps({"ts": recent_ts, "event": "budget_limit"}) + "\n"
                + json.dumps({"ts": old_ts, "event": "window_exhausted"}) + "\n"  # too old
                + json.dumps({"ts": recent_ts, "event": "dispatch"}) + "\n"  # not a limit event
            )
            with patch.object(core, "run_log_path", return_value=log):
                events = core._scan_limit_events(hours=24)
        self.assertEqual(events["window_exhausted"], 2)
        self.assertEqual(events["budget_limit"], 1)
        self.assertEqual(events["nudge_stale"], 0)


class MonitorSnapshotTests(unittest.TestCase):
    def test_snapshot_creation(self):
        snap = core.MonitorSnapshot(
            ts="2026-04-05T16:00:00Z",
            window=core.WindowState(60, 240, 1000, 500, 25),
            budget=core.BudgetState(5000000, 0.5),
            plan="max",
            plan_limits=core.PLAN_LIMITS["max"],
            agents_in_flight=2,
            agents_stale=0,
            limit_events_24h={"window_exhausted": 1, "budget_limit": 0},
        )
        self.assertEqual(snap.plan, "max")
        self.assertEqual(snap.agents_in_flight, 2)

    def test_format_compact(self):
        snap = core.MonitorSnapshot(
            ts="2026-04-05T16:00:00Z",
            window=core.WindowState(60, 240, 1000, 500, 25),
            budget=core.BudgetState(5000000, 0.5),
            plan="max",
            plan_limits=core.PLAN_LIMITS["max"],
            agents_in_flight=2,
            agents_stale=1,
            limit_events_24h={},
        )
        line = core.format_monitor_compact(snap)
        self.assertTrue(line.startswith("[cc-later]"))
        self.assertIn("Plan: max", line)
        self.assertIn("240m left", line)
        self.assertIn("50%", line)
        self.assertIn("2 agent(s)", line)
        self.assertIn("1 stale", line)

    def test_format_compact_no_window(self):
        snap = core.MonitorSnapshot(
            ts="2026-04-05T16:00:00Z",
            window=None,
            budget=core.BudgetState(0, 0.0),
            plan="pro",
            plan_limits=core.PLAN_LIMITS["pro"],
            agents_in_flight=0,
            agents_stale=0,
            limit_events_24h={},
        )
        line = core.format_monitor_compact(snap)
        self.assertIn("Window: unknown", line)

    def test_format_full(self):
        snap = core.MonitorSnapshot(
            ts="2026-04-05T16:00:00Z",
            window=core.WindowState(60, 240, 1000, 500, 25),
            budget=core.BudgetState(5000000, 0.5),
            plan="max",
            plan_limits=core.PLAN_LIMITS["max"],
            agents_in_flight=0,
            agents_stale=0,
            limit_events_24h={"window_exhausted": 2, "budget_limit": 0},
        )
        out = core.format_monitor_full(snap)
        self.assertIn("## cc-later Monitor", out)
        self.assertIn("Plan: max", out)
        self.assertIn("Extended thinking: yes", out)
        self.assertIn("240m remaining", out)
        self.assertIn("25 tokens/min", out)  # burn rate displayed when > 0
        self.assertIn("window_exhausted: 2", out)


class NotifyMacosTests(unittest.TestCase):
    @patch("subprocess.run")
    def test_notify_calls_osascript(self, mock_run):
        core._notify_macos("Test Title", "Test Message")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "osascript")

    @patch("subprocess.run", side_effect=OSError("no osascript"))
    def test_notify_handles_error(self, mock_run):
        # Should not raise
        core._notify_macos("Title", "Message")


class BuildStatusPlanInfoTests(unittest.TestCase):
    def test_build_status_includes_plan_section(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            env_path = td_path / "config.env"
            env_path.write_text("PLAN=max\n")
            state_path = td_path / "state.json"
            state_path.write_text("{}")
            later_file = td_path / ".claude" / "LATER.md"
            later_file.parent.mkdir(parents=True)
            later_file.write_text("# LATER\n")
            with patch.object(core, "config_path", return_value=env_path), \
                 patch.object(core, "app_dir", return_value=td_path), \
                 patch.object(core, "state_path", return_value=state_path), \
                 patch.object(core, "resolve_watch_paths", return_value=[td_path]), \
                 patch.object(core, "resolve_jsonl_roots", return_value=[]):
                status = core.build_status()
        self.assertIn("### Plan", status)
        self.assertIn("Tier: max", status)
        self.assertIn("Extended thinking: yes", status)
        self.assertIn("### Limit Events (24h)", status)


class ParseUsageScreenTests(unittest.TestCase):
    def test_parses_session_pct(self):
        info = core._parse_usage_screen("43% used  Resets 1pm")
        self.assertIsNotNone(info)
        self.assertEqual(info.session_pct, 43)
        self.assertEqual(info.session_reset, "1pm")

    def test_parses_weekly_pct(self):
        info = core._parse_usage_screen("Weekly: 25% used")
        self.assertIsNotNone(info)
        self.assertEqual(info.weekly_pct, 25)

    def test_parses_extra_usage(self):
        info = core._parse_usage_screen("$12.50 extra usage this week")
        self.assertIsNotNone(info)
        self.assertEqual(info.extra_usage_usd, 12.50)

    def test_returns_none_for_empty(self):
        result = core._parse_usage_screen("some unrelated text")
        self.assertIsNone(result)

    def test_parses_full_screen(self):
        screen = (
            "Usage Statistics\n"
            "Session: 43% used\n"
            "Resets at 1pm\n"
            "Weekly usage: 72% used\n"
            "Weekly resets Monday\n"
            "$5.00 extra\n"
        )
        info = core._parse_usage_screen(screen)
        self.assertIsNotNone(info)
        self.assertEqual(info.session_pct, 43)
        self.assertEqual(info.session_reset, "1pm")
        self.assertEqual(info.weekly_pct, 72)
        self.assertEqual(info.weekly_reset, "Monday")
        self.assertEqual(info.extra_usage_usd, 5.00)


class QueryClaudePlanInfoTests(unittest.TestCase):
    def test_returns_none_without_pty(self):
        """Should not raise even when pty/pyte unavailable."""
        with patch.dict("sys.modules", {"pty": None}):
            result = core.query_claude_plan_info()
        self.assertIsNone(result)

    def test_uses_cache_when_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "usage_info.json"
            from datetime import datetime, timezone
            now_ts = datetime.now(timezone.utc).isoformat()
            cache_path.write_text(json.dumps({
                "_cached_at": now_ts,
                "session_pct": 55,
                "session_reset": "2pm",
                "weekly_pct": None,
                "weekly_reset": None,
                "extra_usage_usd": None,
            }))
            with patch.object(core, "app_dir", return_value=Path(td)):
                result = core.query_claude_plan_info()
        self.assertIsNotNone(result)
        self.assertEqual(result.session_pct, 55)
        self.assertEqual(result.session_reset, "2pm")

    def test_unavailable_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "usage_info.json"
            from datetime import datetime, timezone
            now_ts = datetime.now(timezone.utc).isoformat()
            cache_path.write_text(json.dumps({"_unavailable": True, "_cached_at": now_ts}))
            with patch.object(core, "app_dir", return_value=Path(td)):
                result = core.query_claude_plan_info()
        self.assertIsNone(result)


class LaunchdTests(unittest.TestCase):
    def test_plist_path(self):
        from cc_later.launchd import _plist_path

        path = _plist_path()
        self.assertTrue(str(path).endswith("com.cc-later.monitor.plist"))
        self.assertIn("LaunchAgents", str(path))

    def test_plugin_root(self):
        from cc_later.launchd import _plugin_root

        root = _plugin_root()
        self.assertTrue((root / "cc_later").is_dir())

    def test_plist_uses_python3_not_sys_executable(self):
        """ProgramArguments must use 'python3', not a hardcoded sys.executable path."""
        import plistlib
        import sys
        import tempfile
        from unittest.mock import patch as _patch
        from cc_later import launchd

        with tempfile.TemporaryDirectory() as td:
            fake_plist = Path(td) / "com.cc-later.monitor.plist"
            fake_log = Path(td) / "logs"
            fake_log.mkdir()

            with _patch.object(launchd, "_plist_path", return_value=fake_plist), \
                 _patch.object(launchd, "_log_dir", return_value=fake_log), \
                 _patch("subprocess.run"):
                launchd.install_launchd_plist(interval_minutes=15)

            with fake_plist.open("rb") as f:
                plist = plistlib.load(f)

        args = plist["ProgramArguments"]
        self.assertIn("python3", args, "ProgramArguments must use 'python3'")
        self.assertNotIn(sys.executable, args,
                         "ProgramArguments must NOT hardcode sys.executable")

    def test_plist_has_run_at_load(self):
        """Plist must have RunAtLoad=True so first check runs immediately."""
        import plistlib
        import tempfile
        from unittest.mock import patch as _patch
        from cc_later import launchd

        with tempfile.TemporaryDirectory() as td:
            fake_plist = Path(td) / "com.cc-later.monitor.plist"
            fake_log = Path(td) / "logs"
            fake_log.mkdir()

            with _patch.object(launchd, "_plist_path", return_value=fake_plist), \
                 _patch.object(launchd, "_log_dir", return_value=fake_log), \
                 _patch("subprocess.run"):
                launchd.install_launchd_plist(interval_minutes=15)

            with fake_plist.open("rb") as f:
                plist = plistlib.load(f)

        self.assertTrue(plist.get("RunAtLoad"), "Plist must have RunAtLoad=True")

    def test_plist_interval_seconds(self):
        """StartInterval must be interval_minutes * 60."""
        import plistlib
        import tempfile
        from unittest.mock import patch as _patch
        from cc_later import launchd

        with tempfile.TemporaryDirectory() as td:
            fake_plist = Path(td) / "com.cc-later.monitor.plist"
            fake_log = Path(td) / "logs"
            fake_log.mkdir()

            with _patch.object(launchd, "_plist_path", return_value=fake_plist), \
                 _patch.object(launchd, "_log_dir", return_value=fake_log), \
                 _patch("subprocess.run"):
                launchd.install_launchd_plist(interval_minutes=20)

            with fake_plist.open("rb") as f:
                plist = plistlib.load(f)

        self.assertEqual(plist["StartInterval"], 1200)  # 20 * 60


class RunMonitorTests(unittest.TestCase):
    def test_run_monitor_writes_snapshot_file(self):
        """run_monitor must write monitor.json to app_dir."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            env_path = td_path / "config.env"
            env_path.write_text("PLAN=max\nMONITOR_NOTIFY_ENABLED=false\n")
            state_path_val = td_path / "state.json"
            state_path_val.write_text("{}")

            with patch.object(core, "config_path", return_value=env_path), \
                 patch.object(core, "app_dir", return_value=td_path), \
                 patch.object(core, "state_path", return_value=state_path_val), \
                 patch.object(core, "resolve_watch_paths", return_value=[td_path]), \
                 patch.object(core, "resolve_jsonl_roots", return_value=[]):
                snap = core.run_monitor(notify=False)

            monitor_json = td_path / "monitor.json"
            self.assertTrue(monitor_json.exists(), "monitor.json was not written")
            data = json.loads(monitor_json.read_text())
            self.assertEqual(data["plan"], "max")

    def test_run_monitor_no_notify_below_threshold(self):
        """No macOS notification when window and budget are fine."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            env_path = td_path / "config.env"
            env_path.write_text(
                "PLAN=max\n"
                "MONITOR_NOTIFY_ENABLED=true\n"
                "MONITOR_WARN_WINDOW_MINUTES=30\n"
                "MONITOR_WARN_BUDGET_PCT=90\n"
            )
            state_path_val = td_path / "state.json"
            state_path_val.write_text("{}")

            with patch.object(core, "config_path", return_value=env_path), \
                 patch.object(core, "app_dir", return_value=td_path), \
                 patch.object(core, "state_path", return_value=state_path_val), \
                 patch.object(core, "resolve_watch_paths", return_value=[td_path]), \
                 patch.object(core, "resolve_jsonl_roots", return_value=[]), \
                 patch.object(core, "_notify_macos") as mock_notify:
                # window=None (no data), budget=0% — both below thresholds
                core.run_monitor(notify=True)
                mock_notify.assert_not_called()

    def test_format_monitor_full_with_usage_info(self):
        usage = core.UsageInfo(session_pct=55, session_reset="3pm",
                               weekly_pct=30, weekly_reset="Monday",
                               extra_usage_usd=2.50)
        snap = core.MonitorSnapshot(
            ts="2026-04-11T10:00:00Z",
            window=core.WindowState(60, 240, 1000, 500, 25),
            budget=core.BudgetState(1000000, 0.1),
            plan="max",
            plan_limits=core.PLAN_LIMITS["max"],
            agents_in_flight=0,
            agents_stale=0,
            limit_events_24h={},
            usage_info=usage,
        )
        out = core.format_monitor_full(snap)
        self.assertIn("55%", out)
        self.assertIn("3pm", out)
        self.assertIn("Monday", out)
        self.assertIn("$2.50", out)

    def test_format_monitor_compact_with_usage_info(self):
        usage = core.UsageInfo(session_pct=72, session_reset="2pm")
        snap = core.MonitorSnapshot(
            ts="2026-04-11T10:00:00Z",
            window=None,
            budget=core.BudgetState(0, 0.0),
            plan="pro",
            plan_limits=core.PLAN_LIMITS["pro"],
            agents_in_flight=0,
            agents_stale=0,
            limit_events_24h={},
            usage_info=usage,
        )
        line = core.format_monitor_compact(snap)
        self.assertIn("72%", line)
        self.assertIn("2pm", line)

    def test_build_status_includes_limit_events_section(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            env_path = td_path / "config.env"
            env_path.write_text("PLAN=max\n")
            state_path_val = td_path / "state.json"
            state_path_val.write_text("{}")

            with patch.object(core, "config_path", return_value=env_path), \
                 patch.object(core, "app_dir", return_value=td_path), \
                 patch.object(core, "state_path", return_value=state_path_val), \
                 patch.object(core, "resolve_watch_paths", return_value=[td_path]), \
                 patch.object(core, "resolve_jsonl_roots", return_value=[]):
                status = core.build_status()

        self.assertIn("### Limit Events (24h)", status)

    def test_scan_limit_events_new_markers(self):
        """window_exhausted, budget_limit tracked correctly."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log.jsonl"
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            recent = (now - timedelta(hours=1)).isoformat()
            log.write_text(
                json.dumps({"ts": recent, "event": "window_exhausted"}) + "\n"
                + json.dumps({"ts": recent, "event": "nudge_dead"}) + "\n"
                + json.dumps({"ts": recent, "event": "agent_abandoned"}) + "\n"
            )
            with patch.object(core, "run_log_path", return_value=log):
                events = core._scan_limit_events(hours=24)
        self.assertEqual(events["window_exhausted"], 1)
        self.assertEqual(events["nudge_dead"], 1)
        self.assertEqual(events["agent_abandoned"], 1)


if __name__ == "__main__":
    unittest.main()
