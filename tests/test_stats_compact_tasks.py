import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cc_later import core


class StableTaskIdTests(unittest.TestCase):
    def test_deterministic_same_input_same_output(self):
        a = core.stable_task_id(0, "fix the bug")
        b = core.stable_task_id(0, "fix the bug")
        self.assertEqual(a, b)

    def test_different_text_different_id(self):
        a = core.stable_task_id(0, "fix the bug")
        b = core.stable_task_id(0, "add the feature")
        self.assertNotEqual(a, b)

    def test_different_line_index_different_id(self):
        a = core.stable_task_id(0, "same text")
        b = core.stable_task_id(5, "same text")
        self.assertNotEqual(a, b)

    def test_format_prefix_and_hex(self):
        tid = core.stable_task_id(3, "hello")
        self.assertTrue(tid.startswith("t_"))
        self.assertEqual(len(tid), 12)  # "t_" + 10 hex chars
        # Validate hex chars after prefix
        int(tid[2:], 16)

    def test_empty_text(self):
        tid = core.stable_task_id(0, "")
        self.assertTrue(tid.startswith("t_"))
        self.assertEqual(len(tid), 12)


class ParseTasksTests(unittest.TestCase):
    def test_empty_content(self):
        sections = core.parse_tasks("")
        self.assertEqual(sections, [])

    def test_no_sections_flat_list(self):
        content = "- [ ] task one\n- [ ] task two\n"
        sections = core.parse_tasks(content)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].name, "")
        self.assertEqual(len(sections[0].tasks), 2)

    def test_multiple_sections(self):
        content = (
            "## Alpha\n"
            "- [ ] alpha task\n"
            "## Beta\n"
            "- [ ] beta task\n"
        )
        sections = core.parse_tasks(content)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0].name, "Alpha")
        self.assertEqual(sections[1].name, "Beta")
        self.assertEqual(len(sections[0].tasks), 1)
        self.assertEqual(len(sections[1].tasks), 1)

    def test_completed_tasks_filtered_out(self):
        content = (
            "## Queue\n"
            "- [ ] pending\n"
            "- [x] done one\n"
            "- [X] done two\n"
        )
        sections = core.parse_tasks(content)
        self.assertEqual(len(sections), 1)
        self.assertEqual(len(sections[0].tasks), 1)
        self.assertEqual(sections[0].tasks[0].text, "pending")

    def test_urgent_tasks_get_p0(self):
        content = "- [!] urgent fix\n"
        sections = core.parse_tasks(content)
        self.assertEqual(sections[0].tasks[0].priority, "P0")

    def test_priority_override_p0(self):
        content = "- [ ] (P0) critical task\n"
        sections = core.parse_tasks(content)
        self.assertEqual(sections[0].tasks[0].priority, "P0")

    def test_priority_override_p1(self):
        content = "- [ ] (P1) normal task\n"
        sections = core.parse_tasks(content)
        self.assertEqual(sections[0].tasks[0].priority, "P1")

    def test_priority_override_p2(self):
        content = "- [ ] (P2) low task\n"
        sections = core.parse_tasks(content)
        self.assertEqual(sections[0].tasks[0].priority, "P2")

    def test_default_priority_is_p1(self):
        content = "- [ ] no priority specified\n"
        sections = core.parse_tasks(content)
        self.assertEqual(sections[0].tasks[0].priority, "P1")

    def test_non_task_lines_ignored(self):
        content = (
            "## Queue\n"
            "Some descriptive text\n"
            "- [ ] real task\n"
            "another random line\n"
        )
        sections = core.parse_tasks(content)
        self.assertEqual(len(sections[0].tasks), 1)

    def test_task_ids_use_stable_task_id(self):
        content = "- [ ] my task\n"
        sections = core.parse_tasks(content)
        task = sections[0].tasks[0]
        expected = core.stable_task_id(0, "my task")
        self.assertEqual(task.id, expected)

    def test_section_with_no_tasks_not_included(self):
        content = "## Empty Section\nno tasks here\n## Has Tasks\n- [ ] one\n"
        sections = core.parse_tasks(content)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].name, "Has Tasks")

    def test_tasks_before_first_header(self):
        content = "- [ ] orphan task\n## Section\n- [ ] section task\n"
        sections = core.parse_tasks(content)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0].name, "")
        self.assertEqual(sections[1].name, "Section")


class SelectTasksTests(unittest.TestCase):
    def _make_section(self, tasks_data):
        tasks = [
            core.Task(
                id=core.stable_task_id(i, text),
                text=text,
                priority=prio,
                line_index=i,
            )
            for i, (text, prio) in enumerate(tasks_data)
        ]
        return core.Section(name="Test", tasks=tasks)

    def test_selects_up_to_limit(self):
        section = self._make_section([("a", "P1"), ("b", "P1"), ("c", "P1")])
        result = core.select_tasks(section, limit=2)
        self.assertEqual(len(result), 2)

    def test_p0_sorted_first(self):
        section = self._make_section([("low", "P2"), ("urgent", "P0"), ("normal", "P1")])
        result = core.select_tasks(section, limit=3)
        self.assertEqual([t.priority for t in result], ["P0", "P1", "P2"])

    def test_same_priority_sorted_by_line_index(self):
        section = self._make_section([("first", "P1"), ("second", "P1"), ("third", "P1")])
        result = core.select_tasks(section, limit=3)
        self.assertEqual([t.text for t in result], ["first", "second", "third"])

    def test_limit_zero_empty(self):
        section = self._make_section([("a", "P1")])
        result = core.select_tasks(section, limit=0)
        self.assertEqual(result, [])

    def test_fewer_tasks_than_limit_returns_all(self):
        section = self._make_section([("a", "P1"), ("b", "P0")])
        result = core.select_tasks(section, limit=10)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].priority, "P0")

    def test_mixed_priorities_ordering(self):
        section = self._make_section([
            ("p2a", "P2"), ("p0a", "P0"), ("p1a", "P1"),
            ("p0b", "P0"), ("p2b", "P2"), ("p1b", "P1"),
        ])
        result = core.select_tasks(section, limit=6)
        self.assertEqual(
            [t.priority for t in result],
            ["P0", "P0", "P1", "P1", "P2", "P2"],
        )


class MarkDoneInContentTests(unittest.TestCase):
    def test_single_task_marked_done(self):
        content = "## Queue\n- [ ] (P1) fix bug\n"
        tid = core.stable_task_id(1, "fix bug")
        result = core.mark_done_in_content(content, {tid})
        self.assertIn("- [x] (P1) fix bug", result)

    def test_multiple_tasks_marked_done(self):
        content = "## Q\n- [ ] (P0) task a\n- [ ] (P1) task b\n"
        id_a = core.stable_task_id(1, "task a")
        id_b = core.stable_task_id(2, "task b")
        result = core.mark_done_in_content(content, {id_a, id_b})
        self.assertIn("- [x] (P0) task a", result)
        self.assertIn("- [x] (P1) task b", result)

    def test_id_not_found_unchanged(self):
        content = "- [ ] (P1) task\n"
        result = core.mark_done_in_content(content, {"nonexistent_id"})
        self.assertEqual(result, content)

    def test_already_completed_unchanged(self):
        content = "- [x] (P1) done task\n"
        result = core.mark_done_in_content(content, set())
        self.assertEqual(result, content)

    def test_preserves_other_content(self):
        content = "# Header\nSome text\n- [ ] (P1) task\nMore text\n"
        tid = core.stable_task_id(2, "task")
        result = core.mark_done_in_content(content, {tid})
        self.assertIn("# Header", result)
        self.assertIn("Some text", result)
        self.assertIn("More text", result)
        self.assertIn("- [x] (P1) task", result)

    def test_trailing_newline_preserved(self):
        content = "- [ ] task\n"
        result = core.mark_done_in_content(content, set())
        self.assertTrue(result.endswith("\n"))

    def test_no_trailing_newline_preserved(self):
        content = "- [ ] task"
        result = core.mark_done_in_content(content, set())
        self.assertFalse(result.endswith("\n"))

    def test_urgent_mark_converted(self):
        content = "- [!] urgent thing\n"
        tid = core.stable_task_id(0, "urgent thing")
        result = core.mark_done_in_content(content, {tid})
        self.assertIn("- [x] (P0) urgent thing", result)


class ParseResultSummaryTests(unittest.TestCase):
    def test_done_marker_extracted(self):
        raw = "DONE task_1: fixed the bug\n"
        result = core.parse_result_summary(raw)
        self.assertEqual(result, {"task_1": "DONE"})

    def test_failed_marker_extracted(self):
        raw = "FAILED (timeout) task_2: could not complete\n"
        result = core.parse_result_summary(raw)
        self.assertEqual(result, {"task_2": "FAILED"})

    def test_multiple_markers(self):
        raw = "DONE t_abc: ok\nFAILED (err) t_def: nope\nSKIPPED (reason) t_ghi: skip\n"
        result = core.parse_result_summary(raw)
        self.assertEqual(result["t_abc"], "DONE")
        self.assertEqual(result["t_def"], "FAILED")
        self.assertEqual(result["t_ghi"], "SKIPPED")

    def test_no_markers_empty(self):
        result = core.parse_result_summary("just some random text\n")
        self.assertEqual(result, {})

    def test_malformed_empty(self):
        result = core.parse_result_summary("")
        self.assertEqual(result, {})

    def test_needs_human_marker(self):
        raw = "NEEDS_HUMAN (unclear) task_x: needs review\n"
        result = core.parse_result_summary(raw)
        self.assertEqual(result, {"task_x": "NEEDS_HUMAN"})


class DetectLimitExhaustionTests(unittest.TestCase):
    def test_rate_limit_detected(self):
        self.assertIsNotNone(core.detect_limit_exhaustion("Error: rate limit exceeded"))

    def test_usage_limit_detected(self):
        self.assertIsNotNone(core.detect_limit_exhaustion("usage limit reached"))

    def test_quota_detected(self):
        self.assertIsNotNone(core.detect_limit_exhaustion("quota exceeded"))

    def test_too_many_requests_detected(self):
        self.assertIsNotNone(core.detect_limit_exhaustion("too many requests"))

    def test_429_detected(self):
        self.assertIsNotNone(core.detect_limit_exhaustion("HTTP 429 response"))

    def test_5_hour_window_detected(self):
        self.assertIsNotNone(core.detect_limit_exhaustion("5-hour window exhausted"))

    def test_window_exhausted_detected(self):
        self.assertIsNotNone(core.detect_limit_exhaustion("window exhausted"))

    def test_try_again_later_detected(self):
        self.assertIsNotNone(core.detect_limit_exhaustion("please try again later"))

    def test_normal_output_returns_none(self):
        self.assertIsNone(core.detect_limit_exhaustion("Task completed successfully"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(core.detect_limit_exhaustion(""))

    def test_returns_reason_string(self):
        result = core.detect_limit_exhaustion("rate limit")
        self.assertEqual(result, "limit_exhausted")


class EnsureLaterFileTests(unittest.TestCase):
    def test_creates_file_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude" / "LATER.md"
            core.ensure_later_file(path)
            self.assertTrue(path.exists())
            content = path.read_text(encoding="utf-8")
            self.assertIn("# LATER", content)

    def test_existing_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "LATER.md"
            path.write_text("custom content\n", encoding="utf-8")
            core.ensure_later_file(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "custom content\n")

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deep" / "nested" / "LATER.md"
            core.ensure_later_file(path)
            self.assertTrue(path.exists())


class NormalizeModelTests(unittest.TestCase):
    def test_opus_4_6(self):
        self.assertEqual(core._normalize_model("claude-opus-4-6"), "claude-opus-4-6")

    def test_opus_4_5(self):
        self.assertEqual(core._normalize_model("claude-opus-4-5"), "claude-opus-4-5")

    def test_sonnet_4_6(self):
        self.assertEqual(core._normalize_model("claude-sonnet-4-6"), "claude-sonnet-4-6")

    def test_date_suffixed_opus(self):
        result = core._normalize_model("claude-opus-4-6-20260401")
        self.assertEqual(result, "claude-opus-4-6")

    def test_date_suffixed_sonnet(self):
        result = core._normalize_model("claude-sonnet-4-5-20250101")
        self.assertEqual(result, "claude-sonnet-4-5")

    def test_date_suffixed_haiku(self):
        result = core._normalize_model("claude-haiku-4-5-20250101")
        self.assertEqual(result, "claude-haiku-4-5")

    def test_unknown_model_returns_as_is(self):
        result = core._normalize_model("gpt-4-turbo")
        self.assertEqual(result, "gpt-4-turbo")


class RunStatsTests(unittest.TestCase):
    """Tests for run_stats() using temp dirs with JSONL data."""

    def _write_jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def _make_row(self, model, input_tokens=0, cache_create=0, cache_read=0, output_tokens=0, session_id=None, ts=None):
        row = {
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_creation_input_tokens": cache_create,
                    "cache_read_input_tokens": cache_read,
                    "output_tokens": output_tokens,
                },
            },
        }
        if session_id:
            row["sessionId"] = session_id
        if ts:
            row["timestamp"] = ts
        return row

    def test_per_model_token_breakdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            self._write_jsonl(fp, [
                self._make_row("claude-opus-4-6", input_tokens=1000, output_tokens=500),
                self._make_row("claude-sonnet-4-6", input_tokens=2000, output_tokens=800),
                self._make_row("claude-haiku-4-5", input_tokens=500, output_tokens=200),
            ])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("claude-opus-4-6", output)
                    self.assertIn("claude-sonnet-4-6", output)
                    self.assertIn("claude-haiku-4-5", output)
                    self.assertIn("1,000", output)  # opus input
                    self.assertIn("2,000", output)  # sonnet input

    def test_api_cost_calculation_opus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            # 1M input tokens at $15/M = $15.00
            self._write_jsonl(fp, [
                self._make_row("claude-opus-4-6", input_tokens=1_000_000),
            ])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("15.00", output)

    def test_api_cost_calculation_sonnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            # 1M input tokens at $3/M = $3.00
            self._write_jsonl(fp, [
                self._make_row("claude-sonnet-4-6", input_tokens=1_000_000),
            ])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("3.00", output)

    def test_session_counting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            self._write_jsonl(fp, [
                self._make_row("claude-sonnet-4-6", input_tokens=100, session_id="sess-1"),
                self._make_row("claude-sonnet-4-6", input_tokens=100, session_id="sess-1"),
                self._make_row("claude-sonnet-4-6", input_tokens=100, session_id="sess-2"),
            ])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("2", output)  # 2 unique sessions

    def test_file_counting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(3):
                fp = root / f"file{i}.jsonl"
                self._write_jsonl(fp, [self._make_row("claude-sonnet-4-6", input_tokens=10)])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("3", output)  # 3 JSONL files

    def test_day_range_filtering_old_files_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Recent file
            recent = root / "recent.jsonl"
            self._write_jsonl(recent, [
                self._make_row("claude-sonnet-4-6", input_tokens=1000),
            ])
            # Old file with mtime set to 30 days ago
            old = root / "old.jsonl"
            self._write_jsonl(old, [
                self._make_row("claude-sonnet-4-6", input_tokens=9999),
            ])
            old_mtime = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
            os.utime(str(old), (old_mtime, old_mtime))

            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("1,000", output)
                    self.assertNotIn("9,999", output)

    def test_empty_data_no_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    ret = core.run_stats(days=7)
                    self.assertEqual(ret, 0)
                    output = mock_print.call_args[0][0]
                    self.assertIn("0", output)

    def test_max_plan_cost_calculation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=30)
                    output = mock_print.call_args[0][0]
                    # 30/30 * 200 = $200.00
                    self.assertIn("200.00", output)

    def test_max_plan_cost_7_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    expected = 7 / 30 * 200  # ~$46.67
                    self.assertIn("46.67", output)

    def test_savings_percentage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            # Generate enough tokens for a meaningful cost:
            # 10M input tokens opus @ $15/M = $150 API cost
            # 7d plan cost = 7/30 * 200 = ~$46.67
            # savings = (1 - 46.67/150) * 100 = ~68.9%
            self._write_jsonl(fp, [
                self._make_row("claude-opus-4-6", input_tokens=10_000_000),
            ])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("Savings:", output)

    def test_zero_usage_rows_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            self._write_jsonl(fp, [
                self._make_row("claude-opus-4-6", input_tokens=0, output_tokens=0),
                self._make_row("claude-sonnet-4-6", input_tokens=100, output_tokens=50),
            ])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    # opus section should be skipped since all zeros
                    self.assertNotIn("claude-opus-4-6", output)
                    self.assertIn("claude-sonnet-4-6", output)

    def test_unknown_model_uses_default_pricing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            # 1M input at default $3/M = $3.00
            self._write_jsonl(fp, [
                self._make_row("some-unknown-model", input_tokens=1_000_000),
            ])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("some-unknown-model", output)
                    self.assertIn("3.00", output)


class RunCompactInjectTests(unittest.TestCase):
    def _setup_env(self, tmp, compact_enabled=True, later_content=None, state=None, window_state=None):
        app = Path(tmp) / "app"
        app.mkdir()
        repo = Path(tmp) / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        cfg_content = f"COMPACT_ENABLED={'true' if compact_enabled else 'false'}\n"
        (app / "config.env").write_text(cfg_content, encoding="utf-8")

        later_dir = repo / ".claude"
        later_dir.mkdir()
        later_file = later_dir / "LATER.md"
        if later_content is not None:
            later_file.write_text(later_content, encoding="utf-8")

        if state is not None:
            (app / "state.json").write_text(json.dumps(state), encoding="utf-8")

        return app, repo

    def test_compact_disabled_returns_zero_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, repo = self._setup_env(tmp, compact_enabled=False)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch("builtins.print") as mock_print:
                    result = core.run_compact_inject(cwd_hint=str(repo))
                    self.assertEqual(result, 0)
                    mock_print.assert_not_called()

    def test_compact_enabled_no_tasks_says_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            later_content = "# LATER\n\n## Queue\n"
            app, repo = self._setup_env(tmp, later_content=later_content)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch("builtins.print") as mock_print:
                    core.run_compact_inject(cwd_hint=str(repo))
                    output = mock_print.call_args[0][0]
                    self.assertIn("empty", output.lower())

    def test_compact_enabled_with_tasks_shows_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            later_content = "# LATER\n\n## Queue\n- [ ] (P1) fix the tests\n- [ ] (P0) deploy hotfix\n"
            app, repo = self._setup_env(tmp, later_content=later_content)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch("builtins.print") as mock_print:
                    core.run_compact_inject(cwd_hint=str(repo))
                    output = mock_print.call_args[0][0]
                    self.assertIn("fix the tests", output)
                    self.assertIn("deploy hotfix", output)
                    self.assertIn("## Queue", output)

    def test_window_state_none_shows_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            later_content = "# LATER\n\n## Queue\n- [ ] task\n"
            app, repo = self._setup_env(tmp, later_content=later_content)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch.object(core, "compute_window_state", return_value=None):
                    with patch("builtins.print") as mock_print:
                        core.run_compact_inject(cwd_hint=str(repo))
                        output = mock_print.call_args[0][0]
                        self.assertIn("unknown (fresh window)", output)

    def test_window_state_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            later_content = "# LATER\n\n## Queue\n- [ ] task\n"
            app, repo = self._setup_env(tmp, later_content=later_content)
            ws = core.WindowState(elapsed_minutes=60, remaining_minutes=240, total_input_tokens=100, total_output_tokens=50)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch.object(core, "compute_window_state", return_value=ws):
                    with patch("builtins.print") as mock_print:
                        core.run_compact_inject(cwd_hint=str(repo))
                        output = mock_print.call_args[0][0]
                        self.assertIn("240m remaining", output)
                        self.assertIn("60m elapsed", output)

    def test_in_flight_agents_mentioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            later_content = "# LATER\n\n## Queue\n- [ ] task\n"
            app, repo = self._setup_env(tmp, later_content=later_content)
            resolved_repo = repo.resolve()
            state_data = {
                "repos": {
                    str(resolved_repo): {
                        "in_flight": True,
                        "agents": [{"pid": 123, "section_name": "Queue"}],
                        "resume_entries": [],
                    }
                }
            }
            (app / "state.json").write_text(json.dumps(state_data), encoding="utf-8")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch.object(core, "compute_window_state", return_value=None):
                    with patch("builtins.print") as mock_print:
                        core.run_compact_inject(cwd_hint=str(repo))
                        output = mock_print.call_args[0][0]
                        self.assertIn("dispatch in progress", output)
                        self.assertIn("1 agent", output)

    def test_resume_entries_mentioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            later_content = "# LATER\n\n## Queue\n- [ ] task\n"
            app, repo = self._setup_env(tmp, later_content=later_content)
            resolved_repo = repo.resolve()
            state_data = {
                "repos": {
                    str(resolved_repo): {
                        "in_flight": False,
                        "agents": [],
                        "resume_entries": [{"id": "t_abc", "text": "resume me", "priority": "P1", "line_index": 0}],
                    }
                }
            }
            (app / "state.json").write_text(json.dumps(state_data), encoding="utf-8")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch.object(core, "compute_window_state", return_value=None):
                    with patch("builtins.print") as mock_print:
                        core.run_compact_inject(cwd_hint=str(repo))
                        output = mock_print.call_args[0][0]
                        self.assertIn("auto-resume queued", output)
                        self.assertIn("1 task", output)

    def test_multiple_repos_all_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            app.mkdir()
            (app / "config.env").write_text("COMPACT_ENABLED=true\n", encoding="utf-8")

            repo1 = Path(tmp) / "repo1"
            repo1.mkdir()
            (repo1 / ".git").mkdir()
            later1 = repo1 / ".claude"
            later1.mkdir()
            (later1 / "LATER.md").write_text("## Q1\n- [ ] task from repo1\n", encoding="utf-8")

            repo2 = Path(tmp) / "repo2"
            repo2.mkdir()
            (repo2 / ".git").mkdir()
            later2 = repo2 / ".claude"
            later2.mkdir()
            (later2 / "LATER.md").write_text("## Q2\n- [ ] task from repo2\n", encoding="utf-8")

            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch.object(core, "resolve_watch_paths", return_value=[repo1, repo2]):
                    with patch.object(core, "compute_window_state", return_value=None):
                        with patch("builtins.print") as mock_print:
                            core.run_compact_inject(cwd_hint=str(repo1))
                            output = mock_print.call_args[0][0]
                            self.assertIn("task from repo1", output)
                            self.assertIn("task from repo2", output)


class RunStatsEdgeCasesTests(unittest.TestCase):
    """Additional edge case tests for run_stats."""

    def _write_jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def test_cache_tokens_included_in_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            # 1M cache_create at opus $18.75/M = $18.75
            self._write_jsonl(fp, [{
                "message": {
                    "model": "claude-opus-4-6",
                    "usage": {
                        "input_tokens": 0,
                        "cache_creation_input_tokens": 1_000_000,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 0,
                    },
                },
            }])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("18.75", output)

    def test_haiku_pricing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            # 1M output at haiku $4/M = $4.00
            self._write_jsonl(fp, [{
                "message": {
                    "model": "claude-haiku-4-5",
                    "usage": {
                        "input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 1_000_000,
                    },
                },
            }])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("4.00", output)

    def test_no_savings_line_when_zero_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertNotIn("Savings:", output)

    def test_rows_without_message_dict_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            self._write_jsonl(fp, [
                {"message": "not a dict"},
                {"no_message": True},
                {"message": {"model": "claude-sonnet-4-6", "usage": {"input_tokens": 500, "output_tokens": 100}}},
            ])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("500", output)


class ParseTasksEdgeCases(unittest.TestCase):
    def test_crlf_line_endings(self):
        """parse_tasks with Windows \\r\\n line endings."""
        content = "## Queue\r\n- [ ] (P1) task one\r\n- [ ] (P0) task two\r\n"
        sections = core.parse_tasks(content)
        self.assertEqual(len(sections), 1)
        self.assertEqual(len(sections[0].tasks), 2)
        self.assertEqual(sections[0].tasks[0].text, "task one")
        self.assertEqual(sections[0].tasks[1].text, "task two")

    def test_task_text_with_markdown(self):
        """parse_tasks with task text containing markdown links, bold, code."""
        content = (
            "- [ ] (P1) fix [this link](https://example.com) in docs\n"
            "- [ ] (P1) update **bold** text in README\n"
            "- [ ] (P1) refactor `inline_code` usage\n"
        )
        sections = core.parse_tasks(content)
        self.assertEqual(len(sections[0].tasks), 3)
        self.assertIn("[this link]", sections[0].tasks[0].text)
        self.assertIn("**bold**", sections[0].tasks[1].text)
        self.assertIn("`inline_code`", sections[0].tasks[2].text)

    def test_duplicate_task_ids_same_text_different_lines(self):
        """parse_tasks with identical task text on different lines — IDs should differ."""
        content = "- [ ] (P1) fix bug\n- [ ] (P1) fix bug\n"
        sections = core.parse_tasks(content)
        self.assertEqual(len(sections[0].tasks), 2)
        # IDs should be different because line_index differs
        self.assertNotEqual(sections[0].tasks[0].id, sections[0].tasks[1].id)


class StableTaskIdEdgeCases(unittest.TestCase):
    def test_empty_text(self):
        """stable_task_id with empty text produces valid ID."""
        tid = core.stable_task_id(0, "")
        self.assertTrue(tid.startswith("t_"))
        self.assertEqual(len(tid), 12)

    def test_very_long_text(self):
        """stable_task_id with 1000+ char text produces valid fixed-length ID."""
        long_text = "a" * 1500
        tid = core.stable_task_id(0, long_text)
        self.assertTrue(tid.startswith("t_"))
        self.assertEqual(len(tid), 12)
        # Should be deterministic
        self.assertEqual(tid, core.stable_task_id(0, long_text))


class MarkDoneInContentEdgeCases(unittest.TestCase):
    def test_task_id_in_comment_not_checkbox(self):
        """mark_done_in_content should only mark checkboxes, not comments/headers."""
        content = (
            "# Header mentioning fix bug\n"
            "Some text about fix bug\n"
            "- [ ] (P1) fix bug\n"
        )
        tid = core.stable_task_id(2, "fix bug")
        result = core.mark_done_in_content(content, {tid})
        self.assertIn("- [x] (P1) fix bug", result)
        # Header should be unchanged
        self.assertIn("# Header mentioning fix bug", result)
        # Only one [x] should exist
        self.assertEqual(result.count("[x]"), 1)


class DetectLimitExhaustionEdgeCases(unittest.TestCase):
    def test_partial_match_rate_limiting(self):
        """detect_limit_exhaustion with 'rate limiting' should match 'rate limit'."""
        result = core.detect_limit_exhaustion("Error: rate limiting in effect")
        self.assertIsNotNone(result)

    def test_partial_match_no_match(self):
        """detect_limit_exhaustion with 'limited' should not match 'rate limit'."""
        result = core.detect_limit_exhaustion("This is a limited edition")
        # 'rate limit' is the marker, 'limited' alone should not match
        self.assertIsNone(result)


class RunStatsNoJsonlTests(unittest.TestCase):
    def test_run_stats_with_no_jsonl_files_at_all(self):
        """run_stats with empty roots that have no JSONL files."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    ret = core.run_stats(days=7)
                    self.assertEqual(ret, 0)
                    output = mock_print.call_args[0][0]
                    # Should still produce output without errors
                    self.assertIn("cc-later Stats", output)
                    self.assertIn("0", output)


class RunCompactInjectEdgeCases(unittest.TestCase):
    def test_compact_disabled_returns_zero_no_output(self):
        """run_compact_inject when compact is disabled returns 0, no print."""
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            app.mkdir()
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            (app / "config.env").write_text("COMPACT_ENABLED=false\n", encoding="utf-8")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch("builtins.print") as mock_print:
                    result = core.run_compact_inject(cwd_hint=str(repo))
                    self.assertEqual(result, 0)
                    mock_print.assert_not_called()


class ParseTasksHardeningTests(unittest.TestCase):
    def test_10000_lines_performance(self):
        """parse_tasks with 10000+ lines should complete in < 1 second and cap at 10000 lines."""
        import time
        lines = ["## Queue\n"]
        for i in range(10001):
            lines.append(f"- [ ] (P1) task number {i}\n")
        content = "".join(lines)
        start = time.monotonic()
        sections = core.parse_tasks(content)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0, f"parse_tasks took {elapsed:.2f}s for 10001 tasks")
        self.assertEqual(len(sections), 1)
        # Hardening: parse_tasks caps at 10000 lines (header + 9999 tasks)
        self.assertEqual(len(sections[0].tasks), 9999)

    def test_task_text_only_whitespace(self):
        """parse_tasks with task text that is ONLY whitespace should be filtered out."""
        content = "- [ ]    \n- [ ] (P1) real task\n"
        sections = core.parse_tasks(content)
        # The whitespace-only task should be filtered because text.strip() is empty
        self.assertEqual(len(sections), 1)
        self.assertEqual(len(sections[0].tasks), 1)
        self.assertEqual(sections[0].tasks[0].text, "real task")

    def test_malformed_checkbox_missing_space(self):
        """parse_tasks with '- [] text' (missing space in checkbox) should not parse as task."""
        content = "- [] malformed task\n- [ ] (P1) good task\n"
        sections = core.parse_tasks(content)
        # '- []' doesn't match TASK_RE which requires [ xX!] (space or x/X/!)
        self.assertEqual(len(sections), 1)
        self.assertEqual(len(sections[0].tasks), 1)
        self.assertEqual(sections[0].tasks[0].text, "good task")


class StableTaskIdHardeningTests(unittest.TestCase):
    def test_none_text_should_not_crash(self):
        """stable_task_id with None text should not crash."""
        result = core.stable_task_id(0, None)
        self.assertTrue(result.startswith("t_"))
        self.assertEqual(len(result), 12)  # "t_" + 10 hex chars


class MarkDoneHardeningTests(unittest.TestCase):
    def test_empty_string_content(self):
        """mark_done_in_content when content is empty string."""
        result = core.mark_done_in_content("", {"some_id"})
        self.assertEqual(result, "")

    def test_no_checkboxes_at_all(self):
        """mark_done_in_content when content has no checkboxes at all."""
        content = "# Header\nSome text\nMore text\n"
        result = core.mark_done_in_content(content, {"some_id"})
        self.assertEqual(result, content)


class DetectLimitExhaustionHardeningTests(unittest.TestCase):
    def test_very_large_input_1mb(self):
        """detect_limit_exhaustion with 1MB string should handle without issues."""
        large_text = "x" * (1024 * 1024)
        result = core.detect_limit_exhaustion(large_text)
        self.assertIsNone(result)

    def test_very_large_input_with_marker(self):
        """detect_limit_exhaustion with 1MB string containing a marker should detect it."""
        large_text = "x" * (512 * 1024) + "rate limit" + "x" * (512 * 1024)
        result = core.detect_limit_exhaustion(large_text)
        self.assertEqual(result, "limit_exhausted")


class EnsureLaterFileHardeningTests(unittest.TestCase):
    def test_parent_dir_read_only(self):
        """ensure_later_file when parent dir is read-only should handle OSError."""
        with tempfile.TemporaryDirectory() as tmp:
            read_only_dir = Path(tmp) / "readonly"
            read_only_dir.mkdir()
            os.chmod(str(read_only_dir), 0o444)
            try:
                path = read_only_dir / "subdir" / "LATER.md"
                try:
                    core.ensure_later_file(path)
                    # If it succeeds (e.g., running as root), that's fine
                except OSError:
                    pass  # Expected — parent dir is read-only
            finally:
                os.chmod(str(read_only_dir), 0o755)


class ParseTasksNegativeTests(unittest.TestCase):
    """Negative tests: parse_tasks must reject/filter bad input gracefully."""

    def test_only_headers_no_tasks(self):
        content = "## Section A\n## Section B\n## Section C\n"
        sections = core.parse_tasks(content)
        self.assertEqual(sections, [])

    def test_prose_no_tasks(self):
        content = "not a task list at all, just prose\nmore prose here\n"
        sections = core.parse_tasks(content)
        self.assertEqual(sections, [])

    def test_completed_tasks_filtered_returns_empty(self):
        content = "- [x] completed task\n- [X] also done\n"
        sections = core.parse_tasks(content)
        self.assertEqual(sections, [])

    def test_checkbox_with_empty_text_filtered(self):
        content = "- [ ] \n"
        sections = core.parse_tasks(content)
        self.assertEqual(sections, [])

    def test_checkbox_with_very_short_text(self):
        # 2-char text "ab" -- parse_tasks filters on `not text` (empty after strip)
        # but "ab" is non-empty, so it SHOULD be captured
        content = "- [ ] ab\n"
        sections = core.parse_tasks(content)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].tasks[0].text, "ab")


class ParseResultSummaryNegativeTests(unittest.TestCase):
    """Negative tests: parse_result_summary with malformed input."""

    def test_done_missing_task_id(self):
        # "DONE: missing task id" — no ID between status and colon
        result = core.parse_result_summary("DONE: missing task id\n")
        self.assertEqual(result, {})

    def test_failed_no_id(self):
        result = core.parse_result_summary("FAILED: \n")
        self.assertEqual(result, {})


class DetectLimitExhaustionNegativeTests(unittest.TestCase):
    """Negative tests: partial marker words should NOT match."""

    def test_rate_alone_no_match(self):
        self.assertIsNone(core.detect_limit_exhaustion("rate"))

    def test_limited_alone_no_match(self):
        self.assertIsNone(core.detect_limit_exhaustion("limited"))


class StableTaskIdNegativeTests(unittest.TestCase):
    """Negative tests: stable_task_id with edge-case input must not crash."""

    def test_whitespace_only_text(self):
        tid = core.stable_task_id(0, " ")
        self.assertTrue(tid.startswith("t_"))
        self.assertEqual(len(tid), 12)


class MarkDoneInContentNegativeTests(unittest.TestCase):
    """Negative tests: mark_done_in_content with empty done_ids changes nothing."""

    def test_empty_done_ids_no_change(self):
        content = "- [ ] (P1) task one\n- [ ] (P0) task two\n"
        result = core.mark_done_in_content(content, set())
        self.assertEqual(result, content)


# ---------------------------------------------------------------------------
# Capture rejection negative tests
# ---------------------------------------------------------------------------
class CaptureFromPayloadNegativeTests(unittest.TestCase):
    """Negative tests: capture_from_payload must reject bad input gracefully."""

    def setUp(self):
        self._app_td = tempfile.TemporaryDirectory()
        self._repo_td = tempfile.TemporaryDirectory()
        self.app_dir = Path(self._app_td.name)
        self.repo = Path(self._repo_td.name).resolve()
        (self.repo / ".git").mkdir()
        self.env_patch = patch.dict(os.environ, {core.APP_DIR_ENV: str(self.app_dir)}, clear=False)
        self.env_patch.start()
        cfg_text = f"PATHS_WATCH={self.repo}\n"
        (self.app_dir / "config.env").write_text(cfg_text, encoding="utf-8")

    def tearDown(self):
        self.env_patch.stop()
        self._app_td.cleanup()
        self._repo_td.cleanup()

    def _later_path(self):
        return self.repo / ".claude" / "LATER.md"

    def test_empty_dict_returns_zero_no_crash(self):
        """capture_from_payload with empty dict returns 0, no crash."""
        result = core.capture_from_payload({})
        self.assertEqual(result, 0)

    def test_prompt_none_returns_zero(self):
        """capture_from_payload with prompt=None returns 0."""
        result = core.capture_from_payload({"prompt": None})
        self.assertEqual(result, 0)

    def test_prompt_not_string_returns_zero(self):
        """capture_from_payload with prompt=123 (not string) returns 0."""
        result = core.capture_from_payload({"prompt": 123})
        self.assertEqual(result, 0)

    def test_prompt_empty_string_returns_zero(self):
        """capture_from_payload with prompt="" (empty string) returns 0."""
        result = core.capture_from_payload({"prompt": ""})
        self.assertEqual(result, 0)

    def test_no_later_keyword_adds_zero(self):
        """capture_from_payload with prompt lacking 'later:' adds 0 tasks."""
        # Pre-create LATER.md so ensure_later_file template tasks don't confuse us
        later = self._later_path()
        later.parent.mkdir(parents=True, exist_ok=True)
        later.write_text("# LATER\n\n## Queue\n", encoding="utf-8")
        result = core.capture_from_payload({"prompt": "no later keyword here", "cwd": str(self.repo)})
        self.assertEqual(result, 0)
        sections = core.parse_tasks(later.read_text(encoding="utf-8"))
        total = sum(len(s.tasks) for s in sections)
        self.assertEqual(total, 0)

    def test_text_too_short_adds_zero(self):
        """capture_from_payload with prompt='later: ab' (text <3 chars) adds 0 tasks."""
        # Pre-create LATER.md so ensure_later_file template tasks don't confuse us
        later = self._later_path()
        later.parent.mkdir(parents=True, exist_ok=True)
        later.write_text("# LATER\n\n## Queue\n", encoding="utf-8")
        result = core.capture_from_payload({"prompt": "later: ab", "cwd": str(self.repo)})
        self.assertEqual(result, 0)
        sections = core.parse_tasks(later.read_text(encoding="utf-8"))
        total = sum(len(s.tasks) for s in sections)
        self.assertEqual(total, 0)

    def test_duplicate_across_calls_deduplicates(self):
        """capture_from_payload called twice with same task deduplicates on second call."""
        payload = {"prompt": "later: fix the bug", "cwd": str(self.repo)}
        core.capture_from_payload(payload)
        later = self._later_path()
        content_after_first = later.read_text(encoding="utf-8")
        count_first = content_after_first.lower().count("fix the bug")

        core.capture_from_payload(payload)
        content_after_second = later.read_text(encoding="utf-8")
        count_second = content_after_second.lower().count("fix the bug")
        self.assertEqual(count_first, count_second, "Second call should not add duplicate")

    def test_same_task_twice_in_one_prompt_adds_only_one(self):
        """capture_from_payload with same task twice in one prompt adds only 1."""
        payload = {"prompt": "later: fix the bug\nlater: fix the bug", "cwd": str(self.repo)}
        core.capture_from_payload(payload)
        later = self._later_path()
        content = later.read_text(encoding="utf-8")
        count = content.lower().count("fix the bug")
        self.assertEqual(count, 1)

    def test_urgent_strips_doubled_priority(self):
        """capture_from_payload with 'later[!]: (P0) urgent fix' strips doubled priority."""
        payload = {"prompt": "later[!]: (P0) urgent fix", "cwd": str(self.repo)}
        core.capture_from_payload(payload)
        later = self._later_path()
        content = later.read_text(encoding="utf-8")
        # Should have P0 from the [!] marker, text should be "urgent fix" not "(P0) urgent fix"
        self.assertIn("(P0) urgent fix", content)
        self.assertNotIn("(P0) (P0)", content)

    def test_missing_cwd_uses_default_repo(self):
        """capture_from_payload when cwd is missing from payload uses default repo."""
        payload = {"prompt": "later: add docs for API"}
        # Should not crash — resolve_watch_paths falls back to config watch paths
        result = core.capture_from_payload(payload)
        self.assertEqual(result, 0)

    def test_later_file_doesnt_exist_creates_it(self):
        """capture_from_payload when LATER.md doesn't exist yet creates it."""
        later = self._later_path()
        self.assertFalse(later.exists())
        payload = {"prompt": "later: create new feature", "cwd": str(self.repo)}
        core.capture_from_payload(payload)
        self.assertTrue(later.exists())
        content = later.read_text(encoding="utf-8")
        self.assertIn("create new feature", content)


# ---------------------------------------------------------------------------
# Compact injection negative tests
# ---------------------------------------------------------------------------
class CompactInjectNegativeTests(unittest.TestCase):
    """Negative tests for run_compact_inject edge cases."""

    def _setup_env(self, tmp, compact_enabled=True, later_content=None):
        app = Path(tmp) / "app"
        app.mkdir()
        repo = Path(tmp) / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        cfg = f"COMPACT_ENABLED={'true' if compact_enabled else 'false'}\n"
        (app / "config.env").write_text(cfg, encoding="utf-8")
        if later_content is not None:
            later_dir = repo / ".claude"
            later_dir.mkdir()
            (later_dir / "LATER.md").write_text(later_content, encoding="utf-8")
        return app, repo

    def test_compact_disabled_returns_zero_prints_nothing(self):
        """run_compact_inject when compact.enabled=False returns 0, prints nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            app, repo = self._setup_env(tmp, compact_enabled=False)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch("builtins.print") as mock_print:
                    result = core.run_compact_inject(cwd_hint=str(repo))
                    self.assertEqual(result, 0)
                    mock_print.assert_not_called()

    def test_no_repos_have_later_md_outputs_empty(self):
        """run_compact_inject when no repos have LATER.md outputs 'queue: empty'."""
        with tempfile.TemporaryDirectory() as tmp:
            app, repo = self._setup_env(tmp, later_content=None)  # no LATER.md
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch.object(core, "compute_window_state", return_value=None):
                    with patch("builtins.print") as mock_print:
                        core.run_compact_inject(cwd_hint=str(repo))
                        output = mock_print.call_args[0][0]
                        self.assertIn("empty", output.lower())

    def test_only_completed_tasks_outputs_empty(self):
        """run_compact_inject when LATER.md has only completed tasks outputs 'queue: empty'."""
        with tempfile.TemporaryDirectory() as tmp:
            later_content = "# LATER\n\n## Queue\n- [x] done task one\n- [X] done task two\n"
            app, repo = self._setup_env(tmp, later_content=later_content)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch.object(core, "compute_window_state", return_value=None):
                    with patch("builtins.print") as mock_print:
                        core.run_compact_inject(cwd_hint=str(repo))
                        output = mock_print.call_args[0][0]
                        self.assertIn("empty", output.lower())

    def test_window_state_none_shows_fresh_window(self):
        """run_compact_inject when window_state is None outputs 'unknown (fresh window)'."""
        with tempfile.TemporaryDirectory() as tmp:
            later_content = "# LATER\n\n## Queue\n- [ ] task\n"
            app, repo = self._setup_env(tmp, later_content=later_content)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch.object(core, "compute_window_state", return_value=None):
                    with patch("builtins.print") as mock_print:
                        core.run_compact_inject(cwd_hint=str(repo))
                        output = mock_print.call_args[0][0]
                        self.assertIn("unknown (fresh window)", output)

    def test_cwd_hint_nonexistent_dir_handles_gracefully(self):
        """run_compact_inject with cwd_hint pointing to non-existent dir handles gracefully."""
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            app.mkdir()
            (app / "config.env").write_text("COMPACT_ENABLED=true\n", encoding="utf-8")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app)}, clear=False):
                with patch.object(core, "compute_window_state", return_value=None):
                    with patch("builtins.print") as mock_print:
                        # Should not crash even with non-existent cwd_hint
                        result = core.run_compact_inject(cwd_hint="/tmp/nonexistent_dir_12345")
                        self.assertEqual(result, 0)


# ---------------------------------------------------------------------------
# Stats negative tests
# ---------------------------------------------------------------------------
class StatsNegativeTests(unittest.TestCase):
    """Negative tests for run_stats edge cases."""

    def _write_jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def _make_row(self, model, input_tokens=0, cache_create=0, cache_read=0, output_tokens=0, session_id=None):
        row = {
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_creation_input_tokens": cache_create,
                    "cache_read_input_tokens": cache_read,
                    "output_tokens": output_tokens,
                },
            },
        }
        if session_id:
            row["sessionId"] = session_id
        return row

    def test_days_zero_outputs_zeros_no_crash(self):
        """run_stats with days=0 outputs zeros, no crash."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    ret = core.run_stats(days=0)
                    self.assertEqual(ret, 0)
                    output = mock_print.call_args[0][0]
                    self.assertIn("cc-later Stats (0d)", output)
                    self.assertIn("0", output)

    def test_days_negative_outputs_zeros(self):
        """run_stats with days=-1 outputs zeros (cutoff is in the future, nothing matches)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            self._write_jsonl(fp, [self._make_row("claude-sonnet-4-6", input_tokens=5000)])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    ret = core.run_stats(days=-1)
                    self.assertEqual(ret, 0)
                    output = mock_print.call_args[0][0]
                    # Cutoff is in the future so no files should match
                    self.assertIn("cc-later Stats", output)

    def test_no_jsonl_files_outputs_zeros(self):
        """run_stats with no JSONL files outputs zeros, correct formatting."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    ret = core.run_stats(days=7)
                    self.assertEqual(ret, 0)
                    output = mock_print.call_args[0][0]
                    self.assertIn("cc-later Stats (7d)", output)
                    self.assertIn("Grand total:", output)
                    self.assertIn("JSONL files:", output)

    def test_all_unknown_model_uses_default_pricing(self):
        """run_stats when all rows have model='unknown-model' uses default pricing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            # 1M input at default $3/M = $3.00
            self._write_jsonl(fp, [self._make_row("unknown-model", input_tokens=1_000_000)])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    self.assertIn("unknown-model", output)
                    self.assertIn("3.00", output)

    def test_all_zero_usage_model_skipped(self):
        """run_stats when rows have usage with all zero values: model skipped in output."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            self._write_jsonl(fp, [
                self._make_row("claude-opus-4-6", input_tokens=0, cache_create=0, cache_read=0, output_tokens=0),
            ])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    # Model with all-zero usage should be skipped
                    self.assertNotIn("claude-opus-4-6", output)

    def test_savings_negative_when_sub_cost_exceeds_api_cost(self):
        """run_stats when savings would be negative (sub cost > api cost) shows negative %."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "test.jsonl"
            # Tiny usage: 100 input tokens at sonnet $3/M = $0.0003
            # 7d sub cost = 7/30 * 200 = ~$46.67
            # savings = (1 - 46.67/0.0003) * 100 = massively negative
            self._write_jsonl(fp, [self._make_row("claude-sonnet-4-6", input_tokens=100)])
            with patch.object(core, "resolve_jsonl_roots", return_value=[root]):
                with patch("builtins.print") as mock_print:
                    core.run_stats(days=7)
                    output = mock_print.call_args[0][0]
                    # Should have a Savings line since total_cost > 0
                    self.assertIn("Savings:", output)
                    # The savings value should be negative
                    # Extract the savings percentage
                    for line in output.split("\n"):
                        if "Savings:" in line:
                            # Should contain a negative number
                            self.assertIn("-", line)
                            break


if __name__ == "__main__":
    unittest.main()
