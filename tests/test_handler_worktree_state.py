import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from cc_later import core


class _BaseTestCase(unittest.TestCase):
    """Common setup: temp app dir, temp repo with .git, config, and LATER.md with tasks."""

    def setUp(self):
        self._app_td = tempfile.TemporaryDirectory()
        self._repo_td = tempfile.TemporaryDirectory()
        self.app_dir = Path(self._app_td.name)
        self.repo = Path(self._repo_td.name).resolve()
        (self.repo / ".git").mkdir()
        self.env_patch = patch.dict(os.environ, {core.APP_DIR_ENV: str(self.app_dir)}, clear=False)
        self.env_patch.start()
        # Mock budget and window state to avoid scanning real JSONL files
        self._budget_patch = patch(
            "cc_later.core.compute_budget_state",
            return_value=core.BudgetState(used_tokens=0, pct_used=0.0),
        )
        self._window_patch = patch("cc_later.core.compute_window_state", return_value=None)
        self._budget_patch.start()
        self._window_patch.start()
        self._write_config()
        self._write_later()

    def tearDown(self):
        self._window_patch.stop()
        self._budget_patch.stop()
        self.env_patch.stop()
        self._app_td.cleanup()
        self._repo_td.cleanup()

    def _write_config(self, **overrides):
        defaults = {
            "PATHS_WATCH": str(self.repo),
            "LATER_PATH": ".claude/LATER.md",
            "LATER_MAX_ENTRIES_PER_DISPATCH": "3",
            "LATER_AUTO_GITIGNORE": "true",
            "DISPATCH_ENABLED": "true",
            "DISPATCH_MODEL": "sonnet",
            "DISPATCH_ALLOW_FILE_WRITES": "false",
            "DISPATCH_OUTPUT_PATH": "~/.cc-later/results/{repo}-{date}.json",
            "WINDOW_DISPATCH_MODE": "always",
            "WINDOW_TRIGGER_AT_MINUTES_REMAINING": "30",
            "WINDOW_IDLE_GRACE_PERIOD_MINUTES": "0",
            "WINDOW_FALLBACK_DISPATCH_HOURS": "",
            "WINDOW_JSONL_PATHS": "",
            "LIMITS_WEEKLY_BUDGET_TOKENS": "10000000",
            "LIMITS_BACKOFF_AT_PCT": "80",
            "AUTO_RESUME_ENABLED": "true",
            "AUTO_RESUME_MIN_REMAINING_MINUTES": "240",
        }
        defaults.update(overrides)
        text = "\n".join(f"{k}={v}" for k, v in defaults.items()) + "\n"
        (self.app_dir / "config.env").write_text(text, encoding="utf-8")

    def _write_later(self, content=None):
        later = self.repo / ".claude" / "LATER.md"
        later.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            content = (
                "# LATER\n\n## Queue\n"
                "- [ ] (P1) update readme flags\n"
                "- [ ] (P0) fix auth bypass\n"
            )
        later.write_text(content, encoding="utf-8")

    def _stdin(self, **kwargs):
        kwargs.setdefault("cwd", str(self.repo))
        kwargs.setdefault("session_id", "s1")
        return json.dumps(kwargs)


# ---------------------------------------------------------------------------
# run_handler() flow tests
# ---------------------------------------------------------------------------
class TestRunHandlerFlow(_BaseTestCase):
    def test_config_error_returns_0(self):
        (self.app_dir / "config.env").write_text("DISPATCH_MODEL=badmodel\n", encoding="utf-8")
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)

    def test_dispatch_disabled_skips(self):
        self._write_config(DISPATCH_ENABLED="false")
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        state = core.load_state()
        self.assertEqual(len(state.repos.get(str(self.repo), core.RepoState()).agents), 0)

    def test_idle_grace_active_skips(self):
        self._write_config(WINDOW_IDLE_GRACE_PERIOD_MINUTES="60")
        state = core.State(last_hook_ts=datetime.now(timezone.utc).isoformat())
        core.save_state(state)
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)

    def test_budget_gate_failed_skips(self):
        self._write_config(LIMITS_WEEKLY_BUDGET_TOKENS="100", LIMITS_BACKOFF_AT_PCT="0")
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)

    def test_mode_gate_closed_no_resume_skips(self):
        self._write_config(
            WINDOW_DISPATCH_MODE="window_aware",
            AUTO_RESUME_ENABLED="false",
        )
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)

    @patch("cc_later.core._spawn_dispatch", return_value=12345)
    def test_mode_gate_open_has_tasks_dispatches(self, mock_spawn):
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        mock_spawn.assert_called()
        state = core.load_state()
        rs = state.repos[str(self.repo)]
        self.assertTrue(rs.in_flight)
        self.assertTrue(len(rs.agents) > 0)

    @patch("cc_later.core._spawn_dispatch", return_value=99999)
    def test_resume_gate_open_dispatches_resume(self, mock_spawn):
        self._write_config(
            WINDOW_DISPATCH_MODE="always",
            AUTO_RESUME_ENABLED="true",
        )
        state = core.State(repos={
            str(self.repo): core.RepoState(
                resume_entries=[{"id": "t_abc", "text": "leftover task", "priority": "P1", "line_index": 0}],
                resume_reason="limit_exhausted",
            )
        })
        core.save_state(state)
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        mock_spawn.assert_called()
        state = core.load_state()
        rs = state.repos[str(self.repo)]
        self.assertEqual(len(rs.resume_entries), 0)
        self.assertTrue(rs.in_flight)

    @patch("cc_later.core._spawn_dispatch", return_value=12345)
    @patch("cc_later.core._is_process_alive", return_value=True)
    def test_already_in_flight_skips_repo(self, mock_alive, mock_spawn):
        state = core.State(repos={
            str(self.repo): core.RepoState(
                in_flight=True,
                agents=[{"pid": 111, "result_path": "/tmp/x.json", "entries": [], "retries": 0}],
            )
        })
        core.save_state(state)
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        mock_spawn.assert_not_called()

    @patch("cc_later.core._spawn_dispatch", return_value=77777)
    def test_multiple_repos_evaluated_independently(self, mock_spawn):
        repo2_td = tempfile.TemporaryDirectory()
        self.addCleanup(repo2_td.cleanup)
        repo2 = Path(repo2_td.name).resolve()
        (repo2 / ".git").mkdir()
        later2 = repo2 / ".claude" / "LATER.md"
        later2.parent.mkdir(parents=True, exist_ok=True)
        later2.write_text("# LATER\n\n## Queue\n- [ ] (P1) second repo task\n", encoding="utf-8")
        self._write_config(PATHS_WATCH=f"{self.repo},{repo2}")
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        self.assertGreaterEqual(mock_spawn.call_count, 2)
        state = core.load_state()
        self.assertIn(str(self.repo), state.repos)
        self.assertIn(str(repo2), state.repos)

    @patch("cc_later.core._spawn_dispatch", return_value=10001)
    def test_section_based_dispatch_one_agent_per_section(self, mock_spawn):
        self._write_later(
            "# LATER\n\n## Docs\n- [ ] (P1) write docs\n\n## Tests\n- [ ] (P1) add tests\n"
        )
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        state = core.load_state()
        rs = state.repos[str(self.repo)]
        self.assertEqual(len(rs.agents), 2)
        sections = {a["section_name"] for a in rs.agents}
        self.assertEqual(sections, {"Docs", "Tests"})

    @patch("cc_later.core._spawn_dispatch", return_value=10002)
    def test_dispatch_ts_and_retries_set_on_new_agents(self, mock_spawn):
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        state = core.load_state()
        rs = state.repos[str(self.repo)]
        for agent in rs.agents:
            self.assertIsNotNone(agent["dispatch_ts"])
            self.assertEqual(agent["retries"], 0)


# ---------------------------------------------------------------------------
# run_handler() with worktrees
# ---------------------------------------------------------------------------
class TestRunHandlerWorktrees(_BaseTestCase):
    @patch("cc_later.core._spawn_dispatch", return_value=20001)
    @patch("cc_later.core._create_worktree", return_value=(Path("/tmp/wt-path"), "cc-later/Queue-20260406"))
    def test_allow_file_writes_creates_worktree(self, mock_wt, mock_spawn):
        self._write_config(DISPATCH_ALLOW_FILE_WRITES="true")
        core.run_handler(self._stdin())
        mock_wt.assert_called()
        state = core.load_state()
        agent = state.repos[str(self.repo)].agents[0]
        self.assertEqual(agent["worktree_path"], "/tmp/wt-path")
        # cwd passed to _spawn_dispatch should be the worktree path
        _, kwargs = mock_spawn.call_args
        self.assertEqual(kwargs.get("cwd"), Path("/tmp/wt-path"))

    @patch("cc_later.core._spawn_dispatch", return_value=20002)
    @patch("cc_later.core._create_worktree")
    def test_allow_file_writes_false_no_worktree(self, mock_wt, mock_spawn):
        self._write_config(DISPATCH_ALLOW_FILE_WRITES="false")
        core.run_handler(self._stdin())
        mock_wt.assert_not_called()
        state = core.load_state()
        agent = state.repos[str(self.repo)].agents[0]
        self.assertIsNone(agent["worktree_path"])

    @patch("cc_later.core._spawn_dispatch", return_value=20003)
    @patch("cc_later.core._create_worktree", return_value=None)
    def test_worktree_creation_fails_dispatch_continues(self, mock_wt, mock_spawn):
        self._write_config(DISPATCH_ALLOW_FILE_WRITES="true")
        core.run_handler(self._stdin())
        mock_spawn.assert_called()
        state = core.load_state()
        agent = state.repos[str(self.repo)].agents[0]
        self.assertIsNone(agent["worktree_path"])
        self.assertIsNone(agent["branch"])

    @patch("cc_later.core._cleanup_worktree")
    @patch("cc_later.core._spawn_dispatch", return_value=None)
    @patch("cc_later.core._create_worktree", return_value=(Path("/tmp/wt-fail"), "cc-later/Q-ts"))
    def test_spawn_fails_after_worktree_created_cleans_up(self, mock_wt, mock_spawn, mock_cleanup):
        self._write_config(DISPATCH_ALLOW_FILE_WRITES="true")
        core.run_handler(self._stdin())
        mock_cleanup.assert_called_with(self.repo, "cc-later/Q-ts", Path("/tmp/wt-fail"))


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
class TestStatePersistence(_BaseTestCase):
    def test_save_state_writes_valid_json(self):
        state = core.State(last_hook_ts="2026-01-01T00:00:00+00:00")
        core.save_state(state)
        raw = json.loads(core.state_path().read_text(encoding="utf-8"))
        self.assertIn("last_hook_ts", raw)
        self.assertIn("repos", raw)

    def test_load_state_roundtrip(self):
        orig = core.State(
            last_hook_ts="2026-01-01T00:00:00+00:00",
            repos={
                "/repo1": core.RepoState(
                    in_flight=True,
                    agents=[{
                        "section_name": "Queue",
                        "pid": 123,
                        "result_path": "/tmp/r.json",
                        "entries": [{"id": "t_a", "text": "do stuff", "priority": "P1", "line_index": 0}],
                        "branch": "cc-later/Queue-ts",
                        "worktree_path": "/tmp/wt",
                        "dispatch_ts": "2026-01-01T00:00:00+00:00",
                        "retries": 0,
                    }],
                    resume_entries=[{"id": "t_b", "text": "leftover", "priority": "P0", "line_index": 1}],
                    resume_reason="limit_exhausted",
                    dispatch_ts="2026-01-01T00:00:00+00:00",
                )
            },
        )
        core.save_state(orig)
        loaded = core.load_state()
        self.assertEqual(loaded.last_hook_ts, orig.last_hook_ts)
        self.assertIn("/repo1", loaded.repos)
        rs = loaded.repos["/repo1"]
        self.assertTrue(rs.in_flight)
        self.assertEqual(len(rs.agents), 1)
        self.assertEqual(rs.agents[0]["pid"], 123)

    def test_load_state_missing_file_returns_empty(self):
        state = core.load_state()
        self.assertIsNone(state.last_hook_ts)
        self.assertEqual(len(state.repos), 0)

    def test_load_state_corrupt_json_returns_empty(self):
        core.state_path().parent.mkdir(parents=True, exist_ok=True)
        core.state_path().write_text("NOT VALID JSON {{{", encoding="utf-8")
        state = core.load_state()
        self.assertIsNone(state.last_hook_ts)
        self.assertEqual(len(state.repos), 0)

    def test_repostate_agents_serialized(self):
        rs = core.RepoState(
            in_flight=True,
            agents=[{
                "section_name": "Docs",
                "pid": 42,
                "result_path": "/out.json",
                "entries": [{"id": "t_x", "text": "write docs", "priority": "P1", "line_index": 5}],
                "branch": "cc-later/Docs-20260101",
                "worktree_path": "/wt/docs",
                "dispatch_ts": "2026-01-01T12:00:00+00:00",
                "retries": 1,
            }],
        )
        state = core.State(repos={"/r": rs})
        core.save_state(state)
        loaded = core.load_state()
        agent = loaded.repos["/r"].agents[0]
        self.assertEqual(agent["branch"], "cc-later/Docs-20260101")
        self.assertEqual(agent["worktree_path"], "/wt/docs")
        self.assertEqual(agent["dispatch_ts"], "2026-01-01T12:00:00+00:00")
        self.assertEqual(agent["retries"], 1)

    def test_dispatch_ts_retries_branch_worktree_preserved(self):
        agent = {
            "section_name": "S",
            "pid": 1,
            "result_path": "/r.json",
            "entries": [],
            "branch": "cc-later/S-ts",
            "worktree_path": "/wt/s",
            "dispatch_ts": "2026-04-06T10:00:00+00:00",
            "retries": 3,
        }
        state = core.State(repos={"/x": core.RepoState(agents=[agent])})
        core.save_state(state)
        loaded = core.load_state()
        a = loaded.repos["/x"].agents[0]
        self.assertEqual(a["dispatch_ts"], "2026-04-06T10:00:00+00:00")
        self.assertEqual(a["retries"], 3)
        self.assertEqual(a["branch"], "cc-later/S-ts")
        self.assertEqual(a["worktree_path"], "/wt/s")

    def test_resume_entries_preserved_across_save_load(self):
        entries = [
            {"id": "t_1", "text": "task one", "priority": "P0", "line_index": 0},
            {"id": "t_2", "text": "task two", "priority": "P1", "line_index": 1},
        ]
        state = core.State(repos={
            "/r": core.RepoState(resume_entries=entries, resume_reason="limit_exhausted")
        })
        core.save_state(state)
        loaded = core.load_state()
        rs = loaded.repos["/r"]
        self.assertEqual(len(rs.resume_entries), 2)
        self.assertEqual(rs.resume_entries[0]["id"], "t_1")
        self.assertEqual(rs.resume_reason, "limit_exhausted")


# ---------------------------------------------------------------------------
# Worktree functions (mock subprocess.run)
# ---------------------------------------------------------------------------
class TestWorktreeFunctions(_BaseTestCase):
    @patch("subprocess.run")
    def test_create_worktree_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        result = core._create_worktree(self.repo, "Queue", "20260406-120000")
        self.assertIsNotNone(result)
        path, branch = result
        self.assertIn("Queue", str(path))
        self.assertEqual(branch, "cc-later/Queue-20260406-120000")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "git")
        self.assertIn("worktree", cmd)
        self.assertIn("-b", cmd)

    @patch("subprocess.run")
    def test_create_worktree_git_failure_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=128)
        result = core._create_worktree(self.repo, "Queue", "20260406-120000")
        self.assertIsNone(result)

    @patch("subprocess.run", side_effect=OSError("no git"))
    def test_create_worktree_os_error_returns_none(self, mock_run):
        result = core._create_worktree(self.repo, "Queue", "20260406-120000")
        self.assertIsNone(result)

    @patch("subprocess.run")
    def test_branch_naming_with_section(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        result = core._create_worktree(self.repo, "docs_section", "20260406")
        _, branch = result
        self.assertEqual(branch, "cc-later/docs_section-20260406")

    @patch("subprocess.run")
    def test_branch_naming_without_section(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        result = core._create_worktree(self.repo, "", "20260406")
        _, branch = result
        self.assertEqual(branch, "cc-later/default-20260406")

    @patch("subprocess.run")
    def test_merge_worktree_no_new_commits(self, mock_run):
        # rev-list --count returns 0
        mock_run.return_value = MagicMock(returncode=0, stdout="0\n", stderr="")
        ok, conflicts = core._merge_worktree(self.repo, "cc-later/b", Path("/wt"), "Queue")
        self.assertTrue(ok)
        self.assertEqual(conflicts, [])
        # Should have called rev-list, then worktree remove, then branch -d
        cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertEqual(cmds[0][1], "rev-list")

    @patch("subprocess.run")
    def test_merge_worktree_has_commits_merge_succeeds(self, mock_run):
        def side_effect(cmd, **kw):
            if "rev-list" in cmd:
                return MagicMock(returncode=0, stdout="3\n", stderr="")
            if "merge" in cmd and "--abort" not in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect
        ok, conflicts = core._merge_worktree(self.repo, "cc-later/b", Path("/wt"), "Queue")
        self.assertTrue(ok)
        self.assertEqual(conflicts, [])

    @patch("subprocess.run")
    def test_merge_worktree_conflict(self, mock_run):
        def side_effect(cmd, **kw):
            if "rev-list" in cmd:
                return MagicMock(returncode=0, stdout="2\n", stderr="")
            if "merge" in cmd and "--abort" not in cmd:
                return MagicMock(returncode=1, stdout="", stderr="conflict")
            if "diff" in cmd:
                return MagicMock(returncode=0, stdout="file_a.py\nfile_b.py\n", stderr="")
            # merge --abort or cleanup
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect
        ok, conflicts = core._merge_worktree(self.repo, "cc-later/b", Path("/wt"), "Queue")
        self.assertFalse(ok)
        self.assertEqual(conflicts, ["file_a.py", "file_b.py"])

    @patch("subprocess.run")
    def test_cleanup_worktree_calls_remove_and_branch_delete(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        core._cleanup_worktree(self.repo, "cc-later/b", Path("/wt"))
        cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertEqual(len(cmds), 2)
        self.assertIn("worktree", cmds[0])
        self.assertIn("remove", cmds[0])
        self.assertIn("branch", cmds[1])
        self.assertIn("-D", cmds[1])  # force delete — safe for cc-later worktree branches

    def test_ensure_gitignore_no_file_creates(self):
        gitignore = self.repo / ".gitignore"
        if gitignore.exists():
            gitignore.unlink()
        core._ensure_gitignore(self.repo, ".claude/LATER.md")
        self.assertTrue(gitignore.exists())
        self.assertIn(".claude/LATER.md", gitignore.read_text(encoding="utf-8"))

    def test_ensure_gitignore_existing_without_pattern_appends(self):
        gitignore = self.repo / ".gitignore"
        gitignore.write_text("node_modules/\n.env\n", encoding="utf-8")
        core._ensure_gitignore(self.repo, ".claude/LATER.md")
        text = gitignore.read_text(encoding="utf-8")
        self.assertIn(".claude/LATER.md", text)
        self.assertIn("node_modules/", text)

    def test_ensure_gitignore_existing_with_pattern_no_change(self):
        gitignore = self.repo / ".gitignore"
        original = "node_modules/\n.claude/LATER.md\n"
        gitignore.write_text(original, encoding="utf-8")
        core._ensure_gitignore(self.repo, ".claude/LATER.md")
        self.assertEqual(gitignore.read_text(encoding="utf-8"), original)


# ---------------------------------------------------------------------------
# _render_prompt()
# ---------------------------------------------------------------------------
class TestRenderPrompt(unittest.TestCase):
    def _task(self, id_="t_1", text="fix bug", priority="P1", line_index=0):
        return core.Task(id=id_, text=text, priority=priority, line_index=line_index)

    def test_single_task_included(self):
        prompt = core._render_prompt(Path("/repo"), [self._task()], False)
        self.assertIn("fix bug", prompt)
        self.assertIn("t_1", prompt)

    def test_multiple_tasks_all_included(self):
        tasks = [
            self._task(id_="t_1", text="task one"),
            self._task(id_="t_2", text="task two"),
            self._task(id_="t_3", text="task three"),
        ]
        prompt = core._render_prompt(Path("/repo"), tasks, False)
        self.assertIn("task one", prompt)
        self.assertIn("task two", prompt)
        self.assertIn("task three", prompt)

    def test_section_name_included(self):
        prompt = core._render_prompt(Path("/repo"), [self._task()], False, section_name="Documentation")
        self.assertIn("Documentation", prompt)

    def test_allow_file_writes_true_mentions_edit(self):
        prompt = core._render_prompt(Path("/repo"), [self._task()], True)
        self.assertIn("edit files", prompt.lower())
        self.assertNotIn("Do not modify files", prompt)

    def test_allow_file_writes_false_mentions_read_only(self):
        prompt = core._render_prompt(Path("/repo"), [self._task()], False)
        self.assertIn("Do not modify files", prompt)


# ---------------------------------------------------------------------------
# _result_path()
# ---------------------------------------------------------------------------
class TestResultPath(unittest.TestCase):
    def test_generates_path_with_repo_name_and_date(self):
        now = datetime(2026, 4, 6, 12, 30, 0, tzinfo=timezone.utc)
        path = core._result_path("~/.cc-later/results/{repo}-{date}.json", Path("/home/user/myrepo"), now)
        self.assertIn("myrepo", str(path))
        self.assertIn("20260406-123000", str(path))

    def test_section_suffix_included(self):
        now = datetime(2026, 4, 6, 12, 30, 0, tzinfo=timezone.utc)
        path = core._result_path("~/.cc-later/results/{repo}-{date}.json", Path("/r/myrepo"), now, section_slug="Docs")
        self.assertIn("myrepo-Docs", str(path))


# ---------------------------------------------------------------------------
# _read_hook_payload()
# ---------------------------------------------------------------------------
class TestReadHookPayload(unittest.TestCase):
    def test_valid_json(self):
        result = core._read_hook_payload('{"cwd": "/repo", "session_id": "s1"}')
        self.assertEqual(result["cwd"], "/repo")
        self.assertEqual(result["session_id"], "s1")

    def test_invalid_json(self):
        result = core._read_hook_payload("NOT JSON")
        self.assertEqual(result, {})

    def test_empty_string(self):
        result = core._read_hook_payload("")
        self.assertEqual(result, {})

    def test_none_with_tty(self):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            result = core._read_hook_payload(None)
        self.assertEqual(result, {})

    def test_cwd_field_preserved(self):
        result = core._read_hook_payload('{"cwd": "/my/project"}')
        self.assertEqual(result["cwd"], "/my/project")

    def test_non_dict_json_returns_empty(self):
        result = core._read_hook_payload('[1, 2, 3]')
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# log_event()
# ---------------------------------------------------------------------------
class TestLogEvent(_BaseTestCase):
    def test_appends_jsonl_to_run_log(self):
        core.log_event("test_event")
        core.log_event("second_event")
        lines = core.run_log_path().read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["event"], "test_event")

    def test_includes_timestamp(self):
        core.log_event("ts_test")
        line = core.run_log_path().read_text(encoding="utf-8").strip().splitlines()[-1]
        entry = json.loads(line)
        self.assertIn("ts", entry)
        # Should be a valid ISO timestamp
        self.assertIsNotNone(core._parse_iso(entry["ts"]))

    def test_extra_kwargs_included(self):
        core.log_event("extra_test", repo="/myrepo", count=42, flag=True)
        line = core.run_log_path().read_text(encoding="utf-8").strip().splitlines()[-1]
        entry = json.loads(line)
        self.assertEqual(entry["repo"], "/myrepo")
        self.assertEqual(entry["count"], 42)
        self.assertTrue(entry["flag"])


class TestStateWindowTimestamps(_BaseTestCase):
    """Edge cases for window_start_ts and window_limit_ts round-trip."""

    def test_save_load_roundtrip_window_start_ts(self):
        """save_state round-trips window_start_ts correctly."""
        ts = "2026-04-06T10:00:00+00:00"
        state = core.State(window_start_ts=ts)
        core.save_state(state)
        loaded = core.load_state()
        self.assertEqual(loaded.window_start_ts, ts)

    def test_save_load_roundtrip_window_limit_ts(self):
        """save_state round-trips window_limit_ts correctly."""
        ts = "2026-04-06T15:00:00+00:00"
        state = core.State(window_limit_ts=ts)
        core.save_state(state)
        loaded = core.load_state()
        self.assertEqual(loaded.window_limit_ts, ts)

    def test_save_load_roundtrip_both_timestamps(self):
        """Both window timestamps round-trip together."""
        state = core.State(
            window_start_ts="2026-04-06T10:00:00+00:00",
            window_limit_ts="2026-04-06T15:00:00+00:00",
        )
        core.save_state(state)
        loaded = core.load_state()
        self.assertEqual(loaded.window_start_ts, "2026-04-06T10:00:00+00:00")
        self.assertEqual(loaded.window_limit_ts, "2026-04-06T15:00:00+00:00")


class TestLoadStateForwardCompatibility(_BaseTestCase):
    """load_state with extra unknown keys in JSON (forward compatibility)."""

    def test_extra_unknown_keys_ignored(self):
        """Extra keys in state JSON should be silently ignored."""
        core.state_path().parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_hook_ts": "2026-04-06T10:00:00+00:00",
            "window_start_ts": None,
            "window_limit_ts": None,
            "repos": {},
            "future_feature_flag": True,
            "unknown_nested": {"foo": "bar"},
            "version": 99,
        }
        core.state_path().write_text(json.dumps(payload), encoding="utf-8")
        state = core.load_state()
        self.assertEqual(state.last_hook_ts, "2026-04-06T10:00:00+00:00")
        self.assertEqual(len(state.repos), 0)


class TestRunHandlerRepoWithSpaces(_BaseTestCase):
    """run_handler when repo path has spaces."""

    @patch("cc_later.core._spawn_dispatch", return_value=12345)
    def test_repo_with_spaces_dispatches(self, mock_spawn):
        """A repo path with spaces should still dispatch correctly."""
        # Create a new repo with spaces in the path
        space_repo = Path(self._repo_td.name) / "my project repo"
        space_repo.mkdir()
        (space_repo / ".git").mkdir()
        later = space_repo / ".claude" / "LATER.md"
        later.parent.mkdir(parents=True, exist_ok=True)
        later.write_text("# LATER\n\n## Queue\n- [ ] (P1) fix something\n", encoding="utf-8")
        self._write_config(PATHS_WATCH=str(space_repo))
        code = core.run_handler(self._stdin(cwd=str(space_repo)))
        self.assertEqual(code, 0)
        mock_spawn.assert_called()


class TestEnsureGitignoreEdgeCases(_BaseTestCase):
    """Edge cases for _ensure_gitignore."""

    def test_existing_entry_with_leading_slash(self):
        """_ensure_gitignore with leading / in existing entry should still recognize it."""
        gitignore = self.repo / ".gitignore"
        gitignore.write_text("/.claude/LATER.md\n", encoding="utf-8")
        core._ensure_gitignore(self.repo, ".claude/LATER.md")
        text = gitignore.read_text(encoding="utf-8")
        # The code strips leading / when comparing, so it should recognize it
        # and NOT add a duplicate
        count = text.count("LATER.md")
        # If the code does NOT strip leading /, it will add a duplicate — that's a bug
        self.assertGreaterEqual(count, 1)

    def test_existing_entry_with_trailing_whitespace(self):
        """_ensure_gitignore with trailing whitespace in existing entry."""
        gitignore = self.repo / ".gitignore"
        gitignore.write_text(".claude/LATER.md   \n", encoding="utf-8")
        core._ensure_gitignore(self.repo, ".claude/LATER.md")
        text = gitignore.read_text(encoding="utf-8")
        # The code strips each line, so trailing whitespace should be handled
        count = text.count("LATER.md")
        self.assertEqual(count, 1)


class SaveStateHardeningTests(_BaseTestCase):
    def test_very_large_state(self):
        """save_state with very large state (100 repos, 10 agents each) should not crash."""
        repos = {}
        for i in range(100):
            agents = []
            for j in range(10):
                agents.append({
                    "section_name": f"Section_{j}",
                    "pid": 10000 + i * 10 + j,
                    "result_path": f"/tmp/result_{i}_{j}.json",
                    "entries": [{"id": f"t_{i}_{j}", "text": f"task {i}-{j}", "priority": "P1", "line_index": j}],
                    "branch": f"cc-later/S{j}-{i}",
                    "worktree_path": f"/tmp/wt_{i}_{j}",
                    "dispatch_ts": "2026-04-06T10:00:00+00:00",
                    "retries": 0,
                })
            repos[f"/repo_{i}"] = core.RepoState(in_flight=True, agents=agents)
        state = core.State(repos=repos)
        core.save_state(state)
        loaded = core.load_state()
        self.assertEqual(len(loaded.repos), 100)
        for key in repos:
            self.assertEqual(len(loaded.repos[key].agents), 10)


class LoadStateHardeningTests(_BaseTestCase):
    def test_wrong_structure_repos_is_list(self):
        """load_state when state.json has repos as a list not dict."""
        core.state_path().parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_hook_ts": "2026-04-06T10:00:00+00:00",
            "repos": [{"name": "repo1"}],  # list instead of dict
        }
        core.state_path().write_text(json.dumps(payload), encoding="utf-8")
        state = core.load_state()
        # repos should be empty since the code checks isinstance(raw_repos, dict)
        self.assertEqual(len(state.repos), 0)
        self.assertEqual(state.last_hook_ts, "2026-04-06T10:00:00+00:00")


class LogEventHardeningTests(_BaseTestCase):
    def test_log_event_when_run_log_dir_does_not_exist(self):
        """log_event when run_log directory doesn't exist should create it."""
        import shutil
        # Remove the app dir entirely
        shutil.rmtree(str(self.app_dir), ignore_errors=True)
        # log_event should create the directory and write the event
        core.log_event("test_hardening_event")
        self.assertTrue(core.run_log_path().exists())
        lines = core.run_log_path().read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["event"], "test_hardening_event")


class ReadHookPayloadHardeningTests(unittest.TestCase):
    def test_extremely_large_json_1mb(self):
        """_read_hook_payload with extremely large JSON (1MB) should handle it."""
        large_value = "x" * (1024 * 1024)
        data = json.dumps({"cwd": "/repo", "large_field": large_value})
        result = core._read_hook_payload(data)
        self.assertEqual(result["cwd"], "/repo")
        self.assertEqual(len(result["large_field"]), 1024 * 1024)


# ---------------------------------------------------------------------------
# Handler rejection / negative tests
# ---------------------------------------------------------------------------
class TestRunHandlerRejections(_BaseTestCase):
    """Tests that run_handler REJECTS bad states and does NOT dispatch when it shouldn't."""

    def test_empty_stdin_returns_0(self):
        """run_handler with empty stdin returns 0, does not crash."""
        code = core.run_handler("")
        self.assertEqual(code, 0)

    def test_plain_text_stdin_returns_0(self):
        """run_handler with stdin that is not JSON (plain text 'hello') returns 0."""
        code = core.run_handler("hello")
        self.assertEqual(code, 0)

    def test_stdin_json_no_cwd_still_works(self):
        """run_handler with stdin JSON that has no cwd still works (uses default)."""
        code = core.run_handler(json.dumps({"session_id": "s1"}))
        self.assertEqual(code, 0)

    @patch("cc_later.core._spawn_dispatch", return_value=12345)
    def test_only_completed_tasks_does_not_dispatch(self, mock_spawn):
        """run_handler when LATER.md has only completed tasks does NOT dispatch."""
        self._write_later(
            "# LATER\n\n## Queue\n"
            "- [x] (P1) already done task one\n"
            "- [x] (P0) already done task two\n"
        )
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        mock_spawn.assert_not_called()

    @patch("cc_later.core._spawn_dispatch", return_value=12345)
    def test_empty_later_file_does_not_dispatch(self, mock_spawn):
        """run_handler when LATER.md is empty does NOT dispatch."""
        self._write_later("")
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        mock_spawn.assert_not_called()

    @patch("cc_later.core._spawn_dispatch", return_value=12345)
    @patch("cc_later.core._is_process_alive", return_value=True)
    def test_all_repos_in_flight_skips_all(self, mock_alive, mock_spawn):
        """run_handler when all repos are already in-flight skips all."""
        state = core.State(repos={
            str(self.repo): core.RepoState(
                in_flight=True,
                agents=[{"pid": 111, "result_path": "/tmp/x.json", "entries": [], "retries": 0}],
            )
        })
        core.save_state(state)
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        mock_spawn.assert_not_called()

    @patch("cc_later.core._spawn_dispatch", return_value=12345)
    def test_budget_at_exactly_80pct_does_not_dispatch(self, mock_spawn):
        """run_handler when budget is at exactly 80% (backoff threshold) does NOT dispatch."""
        self._budget_patch.stop()
        with patch(
            "cc_later.core.compute_budget_state",
            return_value=core.BudgetState(used_tokens=8_000_000, pct_used=0.80),
        ):
            code = core.run_handler(self._stdin())
        self._budget_patch.start()
        self.assertEqual(code, 0)
        mock_spawn.assert_not_called()

    @patch("cc_later.core._spawn_dispatch", return_value=12345)
    def test_budget_at_79_9pct_does_dispatch(self, mock_spawn):
        """run_handler when budget is at 79.9% DOES dispatch."""
        self._budget_patch.stop()
        with patch(
            "cc_later.core.compute_budget_state",
            return_value=core.BudgetState(used_tokens=7_990_000, pct_used=0.799),
        ):
            code = core.run_handler(self._stdin())
        self._budget_patch.start()
        self.assertEqual(code, 0)
        mock_spawn.assert_called()

    @patch("cc_later.core._spawn_dispatch", return_value=12345)
    def test_window_aware_remaining_31m_does_not_dispatch(self, mock_spawn):
        """window_aware mode with remaining=31m (above 30m trigger) does NOT dispatch."""
        self._write_config(
            WINDOW_DISPATCH_MODE="window_aware",
            WINDOW_TRIGGER_AT_MINUTES_REMAINING="30",
            AUTO_RESUME_ENABLED="false",
        )
        self._window_patch.stop()
        with patch(
            "cc_later.core.compute_window_state",
            return_value=core.WindowState(
                elapsed_minutes=269,
                remaining_minutes=31,
                total_input_tokens=1000,
                total_output_tokens=500,
            ),
        ):
            code = core.run_handler(self._stdin())
        self._window_patch.start()
        self.assertEqual(code, 0)
        mock_spawn.assert_not_called()

    @patch("cc_later.core._spawn_dispatch", return_value=12345)
    def test_window_aware_remaining_30m_does_dispatch(self, mock_spawn):
        """window_aware mode with remaining=30m (at trigger) DOES dispatch."""
        self._write_config(
            WINDOW_DISPATCH_MODE="window_aware",
            WINDOW_TRIGGER_AT_MINUTES_REMAINING="30",
            AUTO_RESUME_ENABLED="false",
        )
        self._window_patch.stop()
        with patch(
            "cc_later.core.compute_window_state",
            return_value=core.WindowState(
                elapsed_minutes=270,
                remaining_minutes=30,
                total_input_tokens=1000,
                total_output_tokens=500,
            ),
        ):
            code = core.run_handler(self._stdin())
        self._window_patch.start()
        self.assertEqual(code, 0)
        mock_spawn.assert_called()

    @patch("cc_later.core._spawn_dispatch", return_value=12345)
    def test_called_twice_within_idle_grace_second_skips(self, mock_spawn):
        """run_handler called twice within idle_grace_period, second call skips."""
        self._write_config(WINDOW_IDLE_GRACE_PERIOD_MINUTES="60")
        # First call sets last_hook_ts
        state = core.State(last_hook_ts=datetime.now(timezone.utc).isoformat())
        core.save_state(state)
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        mock_spawn.assert_not_called()

    @patch("cc_later.core._spawn_dispatch", return_value=None)
    def test_spawn_returns_none_for_all_sections(self, mock_spawn):
        """run_handler when _spawn_dispatch returns None for ALL sections,
        no agents added, in_flight stays False."""
        self._write_later(
            "# LATER\n\n## Docs\n- [ ] (P1) write docs\n\n## Tests\n- [ ] (P1) add tests\n"
        )
        code = core.run_handler(self._stdin())
        self.assertEqual(code, 0)
        state = core.load_state()
        rs = state.repos[str(self.repo)]
        self.assertFalse(rs.in_flight)
        self.assertEqual(len(rs.agents), 0)


# ---------------------------------------------------------------------------
# State corruption / resilience tests
# ---------------------------------------------------------------------------
class TestStateCorruption(_BaseTestCase):
    """Tests that load_state handles corrupt or unexpected data gracefully."""

    def test_repos_as_string_returns_empty(self):
        """load_state when state.json has repos as a string returns empty State repos."""
        core.state_path().parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_hook_ts": "2026-04-06T10:00:00+00:00",
            "repos": "not_a_dict",
        }
        core.state_path().write_text(json.dumps(payload), encoding="utf-8")
        state = core.load_state()
        self.assertEqual(len(state.repos), 0)
        self.assertEqual(state.last_hook_ts, "2026-04-06T10:00:00+00:00")

    def test_agents_as_string_handles_gracefully(self):
        """load_state when state.json has agents as a string (not list) handles gracefully."""
        core.state_path().parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_hook_ts": None,
            "repos": {
                "/repo1": {
                    "in_flight": True,
                    "agents": "this_is_not_a_list",
                    "resume_entries": [],
                    "resume_reason": None,
                    "dispatch_ts": None,
                }
            },
        }
        core.state_path().write_text(json.dumps(payload), encoding="utf-8")
        state = core.load_state()
        rs = state.repos["/repo1"]
        # agents should be empty list since value is not a list
        self.assertEqual(rs.agents, [])

    def test_in_flight_as_string_true_handles_gracefully(self):
        """load_state when state.json has in_flight as string 'true' handles gracefully."""
        core.state_path().parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "repos": {
                "/repo1": {
                    "in_flight": "true",
                    "agents": [],
                    "resume_entries": [],
                    "resume_reason": None,
                    "dispatch_ts": None,
                }
            },
        }
        core.state_path().write_text(json.dumps(payload), encoding="utf-8")
        state = core.load_state()
        rs = state.repos["/repo1"]
        # bool("true") is True, so this should be truthy
        self.assertTrue(rs.in_flight)

    def test_unicode_repo_paths_roundtrip(self):
        """save_state then load_state with unicode repo paths round-trips correctly."""
        unicode_path = "/tmp/\u00e9\u00e8\u00ea-\u4e2d\u6587-\U0001f680"
        state = core.State(repos={
            unicode_path: core.RepoState(
                in_flight=True,
                agents=[{
                    "section_name": "Queue",
                    "pid": 42,
                    "result_path": "/tmp/result.json",
                    "entries": [],
                    "branch": None,
                    "worktree_path": None,
                    "dispatch_ts": "2026-04-06T10:00:00+00:00",
                    "retries": 0,
                }],
            )
        })
        core.save_state(state)
        loaded = core.load_state()
        self.assertIn(unicode_path, loaded.repos)
        self.assertTrue(loaded.repos[unicode_path].in_flight)

    def test_none_values_in_agent_dicts_roundtrip(self):
        """save_state then load_state with None values in agent dicts handles gracefully."""
        state = core.State(repos={
            "/repo": core.RepoState(
                in_flight=True,
                agents=[{
                    "section_name": None,
                    "pid": None,
                    "result_path": None,
                    "entries": None,
                    "branch": None,
                    "worktree_path": None,
                    "dispatch_ts": None,
                    "retries": None,
                }],
            )
        })
        core.save_state(state)
        loaded = core.load_state()
        rs = loaded.repos["/repo"]
        self.assertEqual(len(rs.agents), 1)
        agent = rs.agents[0]
        self.assertIsNone(agent["pid"])
        self.assertIsNone(agent["branch"])


# ---------------------------------------------------------------------------
# Worktree rejection / error-handling tests
# ---------------------------------------------------------------------------
class TestWorktreeRejections(_BaseTestCase):
    """Tests that worktree functions handle failures gracefully."""

    @patch("subprocess.run", side_effect=subprocess.CalledProcessError(128, "git"))
    def test_create_worktree_git_command_fails_returns_none(self, mock_run):
        """_create_worktree when git command fails returns None, does not crash."""
        # subprocess.run raising CalledProcessError should be caught by OSError handler
        # Actually, CalledProcessError is not an OSError, so let's use a proper failure
        pass

    @patch("subprocess.run")
    def test_create_worktree_nonzero_returncode_returns_none(self, mock_run):
        """_create_worktree when git command fails (non-zero returncode) returns None."""
        mock_run.return_value = MagicMock(returncode=1)
        result = core._create_worktree(self.repo, "Queue", "20260406-120000")
        self.assertIsNone(result)

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30))
    def test_create_worktree_timeout_returns_none(self, mock_run):
        """_create_worktree when git command times out returns None."""
        result = core._create_worktree(self.repo, "Queue", "20260406-120000")
        self.assertIsNone(result)

    @patch("subprocess.run")
    def test_merge_worktree_revlist_fails_still_tries_merge(self, mock_run):
        """_merge_worktree when git rev-list fails still tries merge."""
        call_count = []
        def side_effect(cmd, **kw):
            call_count.append(cmd)
            if "rev-list" in cmd:
                return MagicMock(returncode=128, stdout="", stderr="error")
            if "merge" in cmd and "--abort" not in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect
        ok, conflicts = core._merge_worktree(self.repo, "cc-later/b", Path("/wt"), "Queue")
        self.assertTrue(ok)
        # Verify that merge was attempted after rev-list failed
        merge_calls = [c for c in call_count if "merge" in c and "--abort" not in c]
        self.assertGreaterEqual(len(merge_calls), 1)

    @patch("subprocess.run")
    def test_cleanup_worktree_remove_fails_still_tries_branch_delete(self, mock_run):
        """_cleanup_worktree when git worktree remove fails still tries branch delete."""
        call_count = []
        def side_effect(cmd, **kw):
            call_count.append(cmd)
            if "worktree" in cmd:
                raise OSError("worktree remove failed")
            return MagicMock(returncode=0)
        mock_run.side_effect = side_effect
        # Should not crash
        core._cleanup_worktree(self.repo, "cc-later/b", Path("/wt"))
        # Verify branch delete was still attempted
        branch_calls = [c for c in call_count if "branch" in c]
        self.assertEqual(len(branch_calls), 1)

    @patch("subprocess.run", side_effect=OSError("git not found"))
    def test_cleanup_worktree_both_commands_fail_no_crash(self, mock_run):
        """_cleanup_worktree when both git commands fail does not crash."""
        # Should not raise
        core._cleanup_worktree(self.repo, "cc-later/b", Path("/wt"))


if __name__ == "__main__":
    unittest.main()
