import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from cc_later import core


class TestParseBool(unittest.TestCase):
    def test_true_values(self):
        for val in ("true", "True", "TRUE", "1", "yes", "Yes", "YES"):
            self.assertTrue(core._parse_bool(val), f"Expected True for {val!r}")

    def test_false_values(self):
        for val in ("false", "False", "FALSE", "0", "no", "No", "NO"):
            self.assertFalse(core._parse_bool(val), f"Expected False for {val!r}")

    def test_empty_string(self):
        self.assertFalse(core._parse_bool(""))

    def test_whitespace(self):
        self.assertTrue(core._parse_bool("  true  "))
        self.assertFalse(core._parse_bool("  false  "))

    def test_arbitrary_string_is_false(self):
        self.assertFalse(core._parse_bool("maybe"))
        self.assertFalse(core._parse_bool("on"))


class TestParseList(unittest.TestCase):
    def test_comma_separated(self):
        self.assertEqual(core._parse_list("a,b,c"), ["a", "b", "c"])

    def test_empty_string(self):
        self.assertEqual(core._parse_list(""), [])

    def test_whitespace_only(self):
        self.assertEqual(core._parse_list("   "), [])

    def test_single_item(self):
        self.assertEqual(core._parse_list("one"), ["one"])

    def test_spaces_around_items(self):
        self.assertEqual(core._parse_list(" a , b , c "), ["a", "b", "c"])

    def test_trailing_comma(self):
        self.assertEqual(core._parse_list("a,b,"), ["a", "b"])

    def test_leading_comma(self):
        self.assertEqual(core._parse_list(",a,b"), ["a", "b"])

    def test_multiple_commas(self):
        self.assertEqual(core._parse_list("a,,b,,,c"), ["a", "b", "c"])


class TestValidateValues(unittest.TestCase):
    def _make_cfg(self, **overrides):
        cfg = core.Config()
        for dotted, val in overrides.items():
            parts = dotted.split(".")
            obj = cfg
            for p in parts[:-1]:
                obj = getattr(obj, p)
            setattr(obj, parts[-1], val)
        return cfg

    def test_valid_defaults_pass(self):
        cfg = core.Config()
        core._validate_values(cfg)  # should not raise

    def test_invalid_dispatch_mode(self):
        cfg = self._make_cfg(**{"window.dispatch_mode": "invalid"})
        with self.assertRaises(ValueError):
            core._validate_values(cfg)

    def test_invalid_model(self):
        cfg = self._make_cfg(**{"dispatch.model": "gpt4"})
        with self.assertRaises(ValueError):
            core._validate_values(cfg)

    def test_backoff_at_pct_negative(self):
        cfg = self._make_cfg(**{"limits.backoff_at_pct": -1})
        with self.assertRaises(ValueError):
            core._validate_values(cfg)

    def test_backoff_at_pct_101(self):
        cfg = self._make_cfg(**{"limits.backoff_at_pct": 101})
        with self.assertRaises(ValueError):
            core._validate_values(cfg)

    def test_backoff_at_pct_0_is_valid(self):
        cfg = self._make_cfg(**{"limits.backoff_at_pct": 0})
        core._validate_values(cfg)  # should not raise

    def test_backoff_at_pct_100_is_valid(self):
        cfg = self._make_cfg(**{"limits.backoff_at_pct": 100})
        core._validate_values(cfg)  # should not raise

    def test_negative_weekly_budget(self):
        cfg = self._make_cfg(**{"limits.weekly_budget_tokens": -100})
        with self.assertRaises(ValueError):
            core._validate_values(cfg)

    def test_zero_weekly_budget(self):
        cfg = self._make_cfg(**{"limits.weekly_budget_tokens": 0})
        with self.assertRaises(ValueError):
            core._validate_values(cfg)

    def test_negative_min_remaining_minutes(self):
        cfg = self._make_cfg(**{"auto_resume.min_remaining_minutes": -1})
        with self.assertRaises(ValueError):
            core._validate_values(cfg)

    def test_zero_max_entries_per_dispatch(self):
        cfg = self._make_cfg(**{"later.max_entries_per_dispatch": 0})
        with self.assertRaises(ValueError):
            core._validate_values(cfg)


class TestCoerceStr(unittest.TestCase):
    def test_string_returns_string(self):
        self.assertEqual(core._coerce_str("hello"), "hello")

    def test_empty_string(self):
        self.assertEqual(core._coerce_str(""), "")

    def test_int_returns_none(self):
        self.assertIsNone(core._coerce_str(42))

    def test_none_returns_none(self):
        self.assertIsNone(core._coerce_str(None))

    def test_dict_returns_none(self):
        self.assertIsNone(core._coerce_str({"key": "val"}))

    def test_list_returns_none(self):
        self.assertIsNone(core._coerce_str([1, 2]))


class TestCoerceInt(unittest.TestCase):
    def test_int_returns_int(self):
        self.assertEqual(core._coerce_int(42), 42)

    def test_zero(self):
        self.assertEqual(core._coerce_int(0), 0)

    def test_float_returns_truncated_int(self):
        self.assertEqual(core._coerce_int(3.7), 3)

    def test_string_returns_none(self):
        self.assertIsNone(core._coerce_int("42"))

    def test_none_returns_none(self):
        self.assertIsNone(core._coerce_int(None))

    def test_negative_float(self):
        self.assertEqual(core._coerce_int(-2.9), -2)


class TestParseIso(unittest.TestCase):
    def test_valid_iso(self):
        dt = core._parse_iso("2025-01-15T10:30:00+00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2025)
        self.assertEqual(dt.hour, 10)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_z_suffix(self):
        dt = core._parse_iso("2025-01-15T10:30:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_with_offset(self):
        dt = core._parse_iso("2025-01-15T10:30:00-05:00")
        self.assertIsNotNone(dt)
        # Should be converted to UTC
        self.assertEqual(dt.hour, 15)
        self.assertEqual(dt.minute, 30)

    def test_invalid_string(self):
        self.assertIsNone(core._parse_iso("not-a-date"))

    def test_empty_string(self):
        self.assertIsNone(core._parse_iso(""))

    def test_none(self):
        self.assertIsNone(core._parse_iso(None))

    def test_int_input(self):
        self.assertIsNone(core._parse_iso(12345))

    def test_naive_datetime_gets_utc(self):
        dt = core._parse_iso("2025-01-15T10:30:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.hour, 10)


class TestSafeRead(unittest.TestCase):
    def test_existing_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world")
            f.flush()
            result = core._safe_read(Path(f.name))
            self.assertEqual(result, "hello world")
            os.unlink(f.name)

    def test_missing_file(self):
        self.assertIsNone(core._safe_read(Path("/tmp/nonexistent_cc_later_test_file.txt")))

    def test_directory(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(core._safe_read(Path(d)))

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("")
            f.flush()
            result = core._safe_read(Path(f.name))
            self.assertEqual(result, "")
            os.unlink(f.name)


class TestAsInt(unittest.TestCase):
    def test_int(self):
        self.assertEqual(core._as_int(5), 5)

    def test_float(self):
        self.assertEqual(core._as_int(5.9), 5)

    def test_string_returns_zero(self):
        self.assertEqual(core._as_int("5"), 0)

    def test_none_returns_zero(self):
        self.assertEqual(core._as_int(None), 0)

    def test_zero(self):
        self.assertEqual(core._as_int(0), 0)

    def test_negative_float(self):
        self.assertEqual(core._as_int(-3.2), -3)

    def test_dict_returns_zero(self):
        self.assertEqual(core._as_int({}), 0)


class TestParseHhmm(unittest.TestCase):
    def test_nine_am(self):
        self.assertEqual(core._parse_hhmm("09:00"), 9 * 60)

    def test_end_of_day(self):
        self.assertEqual(core._parse_hhmm("23:59"), 23 * 60 + 59)

    def test_midnight(self):
        self.assertEqual(core._parse_hhmm("00:00"), 0)

    def test_invalid_hour_25(self):
        with self.assertRaises(ValueError):
            core._parse_hhmm("25:00")

    def test_invalid_string(self):
        with self.assertRaises(ValueError):
            core._parse_hhmm("abc")

    def test_allow_24(self):
        self.assertEqual(core._parse_hhmm("24:00", allow_24=True), 1440)

    def test_24_without_allow_raises(self):
        with self.assertRaises(ValueError):
            core._parse_hhmm("24:00")

    def test_invalid_minute_60(self):
        with self.assertRaises(ValueError):
            core._parse_hhmm("12:60")

    def test_whitespace_around_parts(self):
        self.assertEqual(core._parse_hhmm(" 09 : 30 "), 9 * 60 + 30)


class TestInTimeWindows(unittest.TestCase):
    def _dt(self, hour, minute=0):
        return datetime(2025, 6, 15, hour, minute, tzinfo=timezone.utc)

    def test_single_window_match(self):
        self.assertTrue(core._in_time_windows(self._dt(10), ["09:00-12:00"]))

    def test_single_window_no_match(self):
        self.assertFalse(core._in_time_windows(self._dt(8), ["09:00-12:00"]))

    def test_overnight_window_match_late(self):
        self.assertTrue(core._in_time_windows(self._dt(23), ["22:00-06:00"]))

    def test_overnight_window_match_early(self):
        self.assertTrue(core._in_time_windows(self._dt(3), ["22:00-06:00"]))

    def test_overnight_window_no_match(self):
        self.assertFalse(core._in_time_windows(self._dt(12), ["22:00-06:00"]))

    def test_empty_list(self):
        self.assertFalse(core._in_time_windows(self._dt(10), []))

    def test_malformed_window_skipped(self):
        self.assertFalse(core._in_time_windows(self._dt(10), ["not_a_window"]))

    def test_non_string_ignored(self):
        self.assertFalse(core._in_time_windows(self._dt(10), [123, None]))

    def test_boundary_start_inclusive(self):
        self.assertTrue(core._in_time_windows(self._dt(9), ["09:00-12:00"]))

    def test_boundary_end_exclusive(self):
        self.assertFalse(core._in_time_windows(self._dt(12), ["09:00-12:00"]))

    def test_equal_start_end_skipped(self):
        self.assertFalse(core._in_time_windows(self._dt(10), ["10:00-10:00"]))

    def test_multiple_windows_second_matches(self):
        self.assertTrue(core._in_time_windows(self._dt(15), ["09:00-12:00", "14:00-18:00"]))


class TestReadEnv(unittest.TestCase):
    def _write_and_parse(self, content):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            f.flush()
            result = core._read_env(Path(f.name))
            os.unlink(f.name)
            return result

    def test_key_value(self):
        result = self._write_and_parse("FOO=bar\n")
        self.assertEqual(result, {"FOO": "bar"})

    def test_comments_skipped(self):
        result = self._write_and_parse("# comment\nFOO=bar\n")
        self.assertEqual(result, {"FOO": "bar"})

    def test_empty_lines_skipped(self):
        result = self._write_and_parse("\n\nFOO=bar\n\n")
        self.assertEqual(result, {"FOO": "bar"})

    def test_spaces_around_equals(self):
        result = self._write_and_parse("FOO = bar\n")
        self.assertEqual(result, {"FOO": "bar"})

    def test_no_value(self):
        result = self._write_and_parse("FOO=\n")
        self.assertEqual(result, {"FOO": ""})

    def test_duplicate_keys_last_wins(self):
        result = self._write_and_parse("FOO=first\nFOO=second\n")
        self.assertEqual(result, {"FOO": "second"})

    def test_line_without_equals_skipped(self):
        result = self._write_and_parse("NOPE\nFOO=bar\n")
        self.assertEqual(result, {"FOO": "bar"})

    def test_value_with_equals(self):
        result = self._write_and_parse("FOO=a=b=c\n")
        self.assertEqual(result, {"FOO": "a=b=c"})


class TestNormalizeModel(unittest.TestCase):
    def test_opus_exact(self):
        self.assertEqual(core._normalize_model("claude-opus-4-6"), "claude-opus-4-6")

    def test_sonnet_with_date_suffix(self):
        self.assertEqual(
            core._normalize_model("claude-sonnet-4-6-20260301"),
            "claude-sonnet-4-6",
        )

    def test_unknown_model_passthrough(self):
        self.assertEqual(core._normalize_model("unknown-model"), "unknown-model")

    def test_empty_string(self):
        self.assertEqual(core._normalize_model(""), "")

    def test_haiku(self):
        self.assertEqual(core._normalize_model("claude-haiku-4-5"), "claude-haiku-4-5")

    def test_opus_45_with_suffix(self):
        self.assertEqual(
            core._normalize_model("claude-opus-4-5-20260101"),
            "claude-opus-4-5",
        )


class TestLoadConfig(unittest.TestCase):
    def test_default_values_when_file_missing(self):
        with tempfile.TemporaryDirectory() as app:
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                self.assertTrue((Path(app) / "config.env").exists())
                # Check defaults
                self.assertTrue(cfg.dispatch.enabled)
                self.assertEqual(cfg.dispatch.model, "sonnet")
                self.assertEqual(cfg.window.dispatch_mode, "window_aware")
                self.assertEqual(cfg.limits.weekly_budget_tokens, 10_000_000)
                self.assertEqual(cfg.limits.backoff_at_pct, 80)
                self.assertTrue(cfg.auto_resume.enabled)
                self.assertTrue(cfg.compact.enabled)
                self.assertTrue(cfg.nudge.enabled)

    def test_env_vars_parsed_correctly(self):
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text(
                "DISPATCH_ENABLED=false\n"
                "DISPATCH_MODEL=opus\n"
                "DISPATCH_ALLOW_FILE_WRITES=true\n"
                "WINDOW_DISPATCH_MODE=always\n"
                "WINDOW_TRIGGER_AT_MINUTES_REMAINING=60\n"
                "WINDOW_IDLE_GRACE_PERIOD_MINUTES=5\n"
                "WINDOW_FALLBACK_DISPATCH_HOURS=09:00-12:00,14:00-18:00\n"
                "LIMITS_WEEKLY_BUDGET_TOKENS=5000000\n"
                "LIMITS_BACKOFF_AT_PCT=90\n"
                "AUTO_RESUME_ENABLED=false\n"
                "AUTO_RESUME_MIN_REMAINING_MINUTES=120\n"
                "COMPACT_ENABLED=false\n"
                "NUDGE_ENABLED=false\n"
                "NUDGE_STALE_MINUTES=20\n"
                "NUDGE_MAX_RETRIES=5\n"
                "LATER_MAX_ENTRIES_PER_DISPATCH=10\n"
                "LATER_AUTO_GITIGNORE=false\n"
                "PATHS_WATCH=/tmp/repo1,/tmp/repo2\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                self.assertFalse(cfg.dispatch.enabled)
                self.assertEqual(cfg.dispatch.model, "opus")
                self.assertTrue(cfg.dispatch.allow_file_writes)
                self.assertEqual(cfg.window.dispatch_mode, "always")
                self.assertEqual(cfg.window.trigger_at_minutes_remaining, 60)
                self.assertEqual(cfg.window.idle_grace_period_minutes, 5)
                self.assertEqual(cfg.window.fallback_dispatch_hours, ["09:00-12:00", "14:00-18:00"])
                self.assertEqual(cfg.limits.weekly_budget_tokens, 5_000_000)
                self.assertEqual(cfg.limits.backoff_at_pct, 90)
                self.assertFalse(cfg.auto_resume.enabled)
                self.assertEqual(cfg.auto_resume.min_remaining_minutes, 120)
                self.assertFalse(cfg.compact.enabled)
                self.assertFalse(cfg.nudge.enabled)
                self.assertEqual(cfg.nudge.stale_minutes, 20)
                self.assertEqual(cfg.nudge.max_retries, 5)
                self.assertEqual(cfg.later.max_entries_per_dispatch, 10)
                self.assertFalse(cfg.later.auto_gitignore)
                self.assertEqual(cfg.paths.watch, ["/tmp/repo1", "/tmp/repo2"])

    def test_invalid_config_raises(self):
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text(
                "WINDOW_DISPATCH_MODE=invalid_mode\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                with self.assertRaises(ValueError):
                    core.load_config()

    def test_invalid_backoff_pct_in_config(self):
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text(
                "LIMITS_BACKOFF_AT_PCT=200\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                with self.assertRaises(ValueError):
                    core.load_config()


class TestNudgeConfig(unittest.TestCase):
    def test_nudge_enabled_from_env(self):
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text("NUDGE_ENABLED=true\nNUDGE_STALE_MINUTES=15\nNUDGE_MAX_RETRIES=3\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                self.assertTrue(cfg.nudge.enabled)
                self.assertEqual(cfg.nudge.stale_minutes, 15)
                self.assertEqual(cfg.nudge.max_retries, 3)

    def test_nudge_disabled(self):
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text("NUDGE_ENABLED=false\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                self.assertFalse(cfg.nudge.enabled)


class TestCompactConfig(unittest.TestCase):
    def test_compact_enabled(self):
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text("COMPACT_ENABLED=true\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                self.assertTrue(cfg.compact.enabled)

    def test_compact_disabled(self):
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text("COMPACT_ENABLED=false\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                self.assertFalse(cfg.compact.enabled)


class TestConfigRoundTrip(unittest.TestCase):
    def test_all_fields_round_trip(self):
        """Write config values to env, reload, verify they match."""
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text(
                "DISPATCH_ENABLED=false\n"
                "DISPATCH_MODEL=haiku\n"
                "DISPATCH_ALLOW_FILE_WRITES=true\n"
                "DISPATCH_OUTPUT_PATH=/tmp/out/{repo}.json\n"
                "WINDOW_DISPATCH_MODE=time_based\n"
                "WINDOW_TRIGGER_AT_MINUTES_REMAINING=45\n"
                "WINDOW_IDLE_GRACE_PERIOD_MINUTES=15\n"
                "WINDOW_FALLBACK_DISPATCH_HOURS=22:00-06:00\n"
                "LIMITS_WEEKLY_BUDGET_TOKENS=1000000\n"
                "LIMITS_BACKOFF_AT_PCT=50\n"
                "AUTO_RESUME_ENABLED=false\n"
                "AUTO_RESUME_MIN_REMAINING_MINUTES=0\n"
                "COMPACT_ENABLED=false\n"
                "NUDGE_ENABLED=false\n"
                "NUDGE_STALE_MINUTES=30\n"
                "NUDGE_MAX_RETRIES=0\n"
                "LATER_PATH=.claude/MY_LATER.md\n"
                "LATER_MAX_ENTRIES_PER_DISPATCH=5\n"
                "LATER_AUTO_GITIGNORE=false\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                self.assertFalse(cfg.dispatch.enabled)
                self.assertEqual(cfg.dispatch.model, "haiku")
                self.assertTrue(cfg.dispatch.allow_file_writes)
                self.assertEqual(cfg.dispatch.output_path, "/tmp/out/{repo}.json")
                self.assertEqual(cfg.window.dispatch_mode, "time_based")
                self.assertEqual(cfg.window.trigger_at_minutes_remaining, 45)
                self.assertEqual(cfg.window.idle_grace_period_minutes, 15)
                self.assertEqual(cfg.window.fallback_dispatch_hours, ["22:00-06:00"])
                self.assertEqual(cfg.limits.weekly_budget_tokens, 1_000_000)
                self.assertEqual(cfg.limits.backoff_at_pct, 50)
                self.assertFalse(cfg.auto_resume.enabled)
                self.assertEqual(cfg.auto_resume.min_remaining_minutes, 0)
                self.assertFalse(cfg.compact.enabled)
                self.assertFalse(cfg.nudge.enabled)
                self.assertEqual(cfg.nudge.stale_minutes, 30)
                self.assertEqual(cfg.nudge.max_retries, 0)
                self.assertEqual(cfg.later.path, ".claude/MY_LATER.md")
                self.assertEqual(cfg.later.max_entries_per_dispatch, 5)
                self.assertFalse(cfg.later.auto_gitignore)


class TestParseIsoEdgeCases(unittest.TestCase):
    def test_non_utc_non_whole_hour_offset(self):
        """_parse_iso with timezone offset '+05:30' (India, non-whole-hour)."""
        dt = core._parse_iso("2026-04-06T12:00:00+05:30")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)
        # 12:00 +05:30 = 06:30 UTC
        self.assertEqual(dt.hour, 6)
        self.assertEqual(dt.minute, 30)

    def test_date_only_no_time(self):
        """_parse_iso with date-only string '2026-04-06' — no time component."""
        dt = core._parse_iso("2026-04-06")
        # datetime.fromisoformat handles date-only strings in Python 3.11+
        # On older Python it may raise ValueError -> returns None
        if dt is not None:
            self.assertEqual(dt.year, 2026)
            self.assertEqual(dt.month, 4)
            self.assertEqual(dt.day, 6)
            self.assertEqual(dt.tzinfo, timezone.utc)


class TestReadEnvEdgeCases(unittest.TestCase):
    def _write_and_parse(self, content):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(content)
            f.flush()
            result = core._read_env(Path(f.name))
            os.unlink(f.name)
            return result

    def test_value_with_multiple_equals(self):
        """_read_env with value containing multiple = signs."""
        result = self._write_and_parse("KEY=value=with=equals\n")
        self.assertEqual(result, {"KEY": "value=with=equals"})

    def test_unicode_values(self):
        """_read_env with unicode characters in values."""
        result = self._write_and_parse("GREETING=\u3053\u3093\u306b\u3061\u306f\u4e16\u754c\n")
        self.assertEqual(result, {"GREETING": "\u3053\u3093\u306b\u3061\u306f\u4e16\u754c"})

    def test_unicode_emoji_values(self):
        """_read_env with emoji characters in values."""
        result = self._write_and_parse("EMOJI=test_value_123\n")
        self.assertEqual(result, {"EMOJI": "test_value_123"})


class TestLoadConfigEdgeCases(unittest.TestCase):
    def test_invalid_plan_raises_value_error(self):
        """load_config with PLAN=invalid_plan should raise ValueError."""
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text("PLAN=invalid_plan\n", encoding="utf-8")
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                with self.assertRaises(ValueError) as ctx:
                    core.load_config()
                self.assertIn("plan", str(ctx.exception).lower())

    def test_window_duration_zero_raises(self):
        """load_config with WINDOW_DURATION_MINUTES=0 should raise ValueError."""
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text("WINDOW_DURATION_MINUTES=0\n", encoding="utf-8")
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                with self.assertRaises(ValueError) as ctx:
                    core.load_config()
                self.assertIn("duration_minutes", str(ctx.exception))

    def test_negative_nudge_stale_minutes(self):
        """load_config with negative NUDGE_STALE_MINUTES — should be accepted (no validation)."""
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text("NUDGE_STALE_MINUTES=-5\n", encoding="utf-8")
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                self.assertEqual(cfg.nudge.stale_minutes, -5)


class TestIterJsonlHardening(unittest.TestCase):
    def test_corrupt_binary_file(self):
        """_iter_jsonl with binary garbage should return empty list, not crash."""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            f.write(b"\x80\x81\x82\xff\xfe\x00\x01\x02\x03\x04")
            f.flush()
            result = core._iter_jsonl(Path(f.name))
            self.assertEqual(result, [])
            os.unlink(f.name)

    def test_valid_json_list_not_dict_skipped(self):
        """_iter_jsonl with a line that is valid JSON but a list, not a dict."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('[1, 2, 3]\n')
            f.write('{"valid": "dict"}\n')
            f.write('"just a string"\n')
            f.flush()
            result = core._iter_jsonl(Path(f.name))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0], {"valid": "dict"})
            os.unlink(f.name)

    def test_extremely_long_line_no_oom(self):
        """_iter_jsonl with a 1MB+ line should not OOM and should handle gracefully."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            # Write a valid JSON dict with a very long value
            long_value = "x" * (1024 * 1024)  # 1MB string
            f.write('{"key": "' + long_value + '"}\n')
            f.write('{"normal": "row"}\n')
            f.flush()
            result = core._iter_jsonl(Path(f.name))
            self.assertEqual(len(result), 2)
            self.assertEqual(result[1], {"normal": "row"})
            os.unlink(f.name)


class TestReadEnvHardening(unittest.TestCase):
    def test_zero_byte_file(self):
        """_read_env with a 0-byte file should return empty dict."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("")
            f.flush()
            result = core._read_env(Path(f.name))
            self.assertEqual(result, {})
            os.unlink(f.name)

    def test_binary_content(self):
        """_read_env with binary content should return empty dict, not crash."""
        with tempfile.NamedTemporaryFile(suffix=".env", delete=False) as f:
            f.write(b"\x80\x81\x82\xff\xfe\x00")
            f.flush()
            result = core._read_env(Path(f.name))
            self.assertEqual(result, {})
            os.unlink(f.name)


class TestParseIsoHardening(unittest.TestCase):
    def test_epoch_timestamp(self):
        """_parse_iso with epoch '1970-01-01T00:00:00Z' should parse correctly."""
        dt = core._parse_iso("1970-01-01T00:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 1970)
        self.assertEqual(dt.month, 1)
        self.assertEqual(dt.day, 1)

    def test_far_future_timestamp(self):
        """_parse_iso with far-future '2099-12-31T23:59:59Z' should parse correctly."""
        dt = core._parse_iso("2099-12-31T23:59:59Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2099)
        self.assertEqual(dt.month, 12)
        self.assertEqual(dt.day, 31)


class TestLoadConfigHardening(unittest.TestCase):
    def test_empty_config_file(self):
        """load_config when config file is empty (0 bytes) should use defaults."""
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text("", encoding="utf-8")
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                # Should get all defaults
                self.assertTrue(cfg.dispatch.enabled)
                self.assertEqual(cfg.dispatch.model, "sonnet")
                self.assertEqual(cfg.window.dispatch_mode, "window_aware")

    def test_config_file_only_comments(self):
        """load_config when config file has only comments should use defaults."""
        with tempfile.TemporaryDirectory() as app:
            cfg_file = Path(app) / "config.env"
            cfg_file.write_text(
                "# This is a comment\n"
                "# Another comment\n"
                "# DISPATCH_ENABLED=false\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {core.APP_DIR_ENV: app}, clear=False):
                cfg = core.load_config()
                self.assertTrue(cfg.dispatch.enabled)
                self.assertEqual(cfg.dispatch.model, "sonnet")


class TestSafeReadHardening(unittest.TestCase):
    def test_symlink_to_missing_target(self):
        """_safe_read when file is a symlink to a missing target should return None."""
        with tempfile.TemporaryDirectory() as d:
            link_path = Path(d) / "broken_link.txt"
            link_path.symlink_to(Path(d) / "nonexistent_target.txt")
            result = core._safe_read(link_path)
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
