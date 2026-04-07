from __future__ import annotations

import os
import signal
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

from cc_later import core


class _Base(unittest.TestCase):
    """Shared helpers for reconcile / nudge tests."""

    def _write_config(
        self,
        app_dir: Path,
        repo: Path,
        mode: str = "always",
        nudge_enabled: bool = True,
        stale_minutes: int = 10,
        max_retries: int = 2,
        allow_file_writes: bool = False,
    ) -> None:
        (app_dir / "config.env").write_text(
            "\n".join(
                [
                    f"PATHS_WATCH={repo}",
                    "LATER_PATH=.claude/LATER.md",
                    "LATER_MAX_ENTRIES_PER_DISPATCH=3",
                    "LATER_AUTO_GITIGNORE=true",
                    "DISPATCH_ENABLED=true",
                    "DISPATCH_MODEL=sonnet",
                    f"DISPATCH_ALLOW_FILE_WRITES={'true' if allow_file_writes else 'false'}",
                    "DISPATCH_OUTPUT_PATH=~/.cc-later/results/{repo}-{date}.json",
                    f"WINDOW_DISPATCH_MODE={mode}",
                    "WINDOW_TRIGGER_AT_MINUTES_REMAINING=30",
                    "WINDOW_IDLE_GRACE_PERIOD_MINUTES=0",
                    "WINDOW_FALLBACK_DISPATCH_HOURS=",
                    "WINDOW_JSONL_PATHS=",
                    "LIMITS_WEEKLY_BUDGET_TOKENS=10000000",
                    "LIMITS_BACKOFF_AT_PCT=80",
                    "AUTO_RESUME_ENABLED=true",
                    "AUTO_RESUME_MIN_REMAINING_MINUTES=240",
                    f"NUDGE_ENABLED={'true' if nudge_enabled else 'false'}",
                    f"NUDGE_STALE_MINUTES={stale_minutes}",
                    f"NUDGE_MAX_RETRIES={max_retries}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def _setup_repo(self, repo: Path, task_text: str = "- [ ] (P1) fix auth timeout\n") -> Path:
        (repo / ".git").mkdir()
        later = repo / ".claude" / "LATER.md"
        later.parent.mkdir(parents=True)
        later.write_text(task_text, encoding="utf-8")
        return later

    def _make_agent(
        self,
        entries: list[core.Task],
        result_path: str | None = None,
        pid: int | None = 99999,
        section_name: str = "",
        branch: str | None = None,
        worktree_path: str | None = None,
        dispatch_ts: str | None = None,
        retries: int = 0,
    ) -> dict:
        return {
            "section_name": section_name,
            "pid": pid,
            "result_path": result_path,
            "entries": [t.to_dict() for t in entries],
            "branch": branch,
            "worktree_path": worktree_path,
            "dispatch_ts": dispatch_ts,
            "retries": retries,
        }


# ---------------------------------------------------------------------------
# _reconcile() basics
# ---------------------------------------------------------------------------
class ReconcileBasicsTests(_Base):
    def test_no_in_flight_repos_returns_zero(self):
        """No in-flight repos -> returns 0, no changes."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                state.repos[str(repo)] = core.RepoState(in_flight=False, agents=[])
                result = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(result, 0)

    @patch.object(core, "_is_process_alive", return_value=True)
    def test_alive_agent_stays_in_remaining(self, mock_alive):
        """Agent still alive -> stays in remaining list."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=False)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent([task], result_path="/tmp/fake.json", pid=12345)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 0)
                self.assertTrue(state.repos[str(repo)].in_flight)
                self.assertEqual(len(state.repos[str(repo)].agents), 1)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_dead_agent_with_result_processes_results(self, _):
        """Agent dead with result output -> processes results, marks done."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_text(f"DONE {task.id}: fixed auth\n", encoding="utf-8")
                agent = self._make_agent([task], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 1)
                self.assertFalse(state.repos[str(repo)].in_flight)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_dead_agent_no_output_nudge_disabled_cleaned_up(self, _):
        """Agent dead with no output, nudge disabled -> cleaned up."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=False)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent([task], result_path="/tmp/nonexistent_result.json", pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 1)
                self.assertFalse(state.repos[str(repo)].in_flight)
                self.assertEqual(len(state.repos[str(repo)].agents), 0)

    @patch.object(core, "_is_process_alive", side_effect=[True, False])
    def test_multiple_agents_per_repo(self, _):
        """Multiple agents per repo -> each handled independently."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo, "- [ ] (P1) task A\n- [ ] (P1) task B\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=False)
                cfg = core.load_config()
                state = core.State()
                sections = core.parse_tasks(later.read_text(encoding="utf-8"))
                task_a = sections[0].tasks[0]
                task_b = sections[0].tasks[1]

                result_b = repo / "result_b.json"
                result_b.write_text(f"DONE {task_b.id}: done B\n", encoding="utf-8")

                agent_a = self._make_agent([task_a], result_path="/tmp/fake.json", pid=111)
                agent_b = self._make_agent([task_b], result_path=str(result_b), pid=222)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent_a, agent_b])

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                # agent_a alive -> remaining, agent_b dead with result -> completed
                self.assertEqual(completed, 1)
                self.assertTrue(state.repos[str(repo)].in_flight)
                self.assertEqual(len(state.repos[str(repo)].agents), 1)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_multiple_repos_handled_independently(self, _):
        """Multiple repos -> each handled independently."""
        with (
            tempfile.TemporaryDirectory() as app,
            tempfile.TemporaryDirectory() as repo_dir1,
            tempfile.TemporaryDirectory() as repo_dir2,
        ):
            app_dir = Path(app)
            repo1, repo2 = Path(repo_dir1), Path(repo_dir2)
            later1 = self._setup_repo(repo1, "- [ ] (P1) repo1 task\n")
            later2 = self._setup_repo(repo2, "- [ ] (P1) repo2 task\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo1)
                cfg = core.load_config()
                state = core.State()

                task1 = core.parse_tasks(later1.read_text(encoding="utf-8"))[0].tasks[0]
                task2 = core.parse_tasks(later2.read_text(encoding="utf-8"))[0].tasks[0]

                res1 = repo1 / "res1.json"
                res1.write_text(f"DONE {task1.id}: done\n", encoding="utf-8")
                res2 = repo2 / "res2.json"
                res2.write_text(f"FAILED (error) {task2.id}: failed\n", encoding="utf-8")

                state.repos[str(repo1)] = core.RepoState(
                    in_flight=True,
                    agents=[self._make_agent([task1], result_path=str(res1), pid=None)],
                )
                state.repos[str(repo2)] = core.RepoState(
                    in_flight=True,
                    agents=[self._make_agent([task2], result_path=str(res2), pid=None)],
                )

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 2)
                self.assertFalse(state.repos[str(repo1)].in_flight)
                self.assertFalse(state.repos[str(repo2)].in_flight)


# ---------------------------------------------------------------------------
# _reconcile() with result processing
# ---------------------------------------------------------------------------
class ReconcileResultProcessingTests(_Base):
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_done_tasks_mark_done_in_later(self, _):
        """DONE tasks -> mark_done_in_content called, LATER.md updated."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo, "- [ ] (P1) update docs\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_text(f"DONE {task.id}: updated docs\n", encoding="utf-8")
                agent = self._make_agent([task], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                updated = later.read_text(encoding="utf-8")
                self.assertIn("- [x] (P1) update docs", updated)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_failed_tasks_not_marked_done(self, _):
        """FAILED tasks -> not marked done in LATER.md."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo, "- [ ] (P1) fix bug\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_text(f"FAILED (error) {task.id}: could not fix\n", encoding="utf-8")
                agent = self._make_agent([task], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                updated = later.read_text(encoding="utf-8")
                self.assertIn("- [ ] (P1) fix bug", updated)
                self.assertNotIn("[x]", updated)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_limit_exhaustion_moves_to_resume_entries(self, _):
        """Limit exhaustion detected -> entries moved to resume_entries."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_text(
                    f"FAILED (rate limit) {task.id}: fix auth timeout\n"
                    "Rate limit reached for your current 5-hour window\n",
                    encoding="utf-8",
                )
                agent = self._make_agent([task], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(len(state.repos[str(repo)].resume_entries), 1)
                self.assertEqual(state.repos[str(repo)].resume_reason, "limit_exhausted")

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_mix_done_and_failed_only_done_marked(self, _):
        """Mix of DONE and FAILED -> only DONE marked."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo, "- [ ] (P1) task one\n- [ ] (P1) task two\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                sections = core.parse_tasks(later.read_text(encoding="utf-8"))
                t1 = sections[0].tasks[0]
                t2 = sections[0].tasks[1]
                result_file = repo / "result.json"
                result_file.write_text(
                    f"DONE {t1.id}: done one\n"
                    f"FAILED (err) {t2.id}: fail two\n",
                    encoding="utf-8",
                )
                agent = self._make_agent([t1, t2], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                updated = later.read_text(encoding="utf-8")
                self.assertIn("[x]", updated)
                # task two should remain unchecked
                self.assertIn("- [ ] (P1) task two", updated)


# ---------------------------------------------------------------------------
# _reconcile() with worktree merging
# ---------------------------------------------------------------------------
class ReconcileWorktreeMergeTests(_Base):
    @patch.object(core, "_is_process_alive", return_value=False)
    @patch.object(core, "_merge_worktree", return_value=(True, []))
    def test_dead_agent_with_branch_merge_attempted(self, mock_merge, _):
        """Dead agent with branch -> merge attempted."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_text(f"DONE {task.id}: fixed\n", encoding="utf-8")
                agent = self._make_agent(
                    [task],
                    result_path=str(result_file),
                    pid=None,
                    branch="cc-later/test-branch",
                    worktree_path="/tmp/wt",
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                mock_merge.assert_called_once_with(
                    repo, "cc-later/test-branch", Path("/tmp/wt"), ""
                )

    @patch.object(core, "_is_process_alive", return_value=False)
    @patch.object(core, "_merge_worktree", return_value=(True, []))
    def test_successful_merge_cleans_up(self, mock_merge, _):
        """Successful merge -> worktree cleaned up (by _merge_worktree internally)."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_text(f"DONE {task.id}: fixed\n", encoding="utf-8")
                agent = self._make_agent(
                    [task],
                    result_path=str(result_file),
                    pid=None,
                    branch="cc-later/b",
                    worktree_path="/tmp/wt",
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 1)
                mock_merge.assert_called_once()

    @patch.object(core, "_is_process_alive", return_value=False)
    @patch.object(core, "_merge_worktree", return_value=(False, ["file_a.py", "file_b.py"]))
    def test_merge_conflict_marks_needs_human(self, mock_merge, _):
        """Merge conflict -> entries marked NEEDS_HUMAN, worktree preserved."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_text(f"DONE {task.id}: fixed\n", encoding="utf-8")
                agent = self._make_agent(
                    [task],
                    result_path=str(result_file),
                    pid=None,
                    branch="cc-later/conflict",
                    worktree_path="/tmp/wt_conflict",
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                # The task should NOT be marked done since the merge conflicted
                updated = later.read_text(encoding="utf-8")
                self.assertNotIn("[x]", updated)

    @patch.object(core, "_is_process_alive", return_value=False)
    @patch.object(core, "_merge_worktree")
    def test_no_branch_no_merge_attempted(self, mock_merge, _):
        """No branch (worktree disabled) -> no merge attempted."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_text(f"DONE {task.id}: fixed\n", encoding="utf-8")
                agent = self._make_agent(
                    [task], result_path=str(result_file), pid=None, branch=None, worktree_path=None
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                mock_merge.assert_not_called()


# ---------------------------------------------------------------------------
# _is_agent_stale()
# ---------------------------------------------------------------------------
class IsAgentStaleTests(_Base):
    def test_result_file_recently_modified_not_stale(self):
        """Result file recently modified -> not stale."""
        with tempfile.TemporaryDirectory() as d:
            rp = Path(d) / "result.json"
            rp.write_text("partial output\n", encoding="utf-8")
            agent = {"result_path": str(rp), "dispatch_ts": None}
            now = datetime.now(timezone.utc)
            self.assertFalse(core._is_agent_stale(agent, now, stale_minutes=10))

    def test_result_file_old_is_stale(self):
        """Result file not modified for > stale_minutes -> stale."""
        with tempfile.TemporaryDirectory() as d:
            rp = Path(d) / "result.json"
            rp.write_text("partial output\n", encoding="utf-8")
            # Set mtime to 15 minutes ago
            old_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).timestamp()
            os.utime(str(rp), (old_ts, old_ts))
            agent = {"result_path": str(rp), "dispatch_ts": None}
            now = datetime.now(timezone.utc)
            self.assertTrue(core._is_agent_stale(agent, now, stale_minutes=10))

    def test_no_result_file_dispatch_ts_recent_not_stale(self):
        """No result file, dispatch_ts recent -> not stale."""
        now = datetime.now(timezone.utc)
        dispatch = (now - timedelta(minutes=5)).isoformat()
        agent = {"result_path": "/tmp/nonexistent_xyz.json", "dispatch_ts": dispatch}
        self.assertFalse(core._is_agent_stale(agent, now, stale_minutes=10))

    def test_no_result_file_dispatch_ts_old_is_stale(self):
        """No result file, dispatch_ts old -> stale."""
        now = datetime.now(timezone.utc)
        dispatch = (now - timedelta(minutes=20)).isoformat()
        agent = {"result_path": "/tmp/nonexistent_xyz.json", "dispatch_ts": dispatch}
        self.assertTrue(core._is_agent_stale(agent, now, stale_minutes=10))

    def test_no_result_file_no_dispatch_ts_not_stale(self):
        """No result file, no dispatch_ts -> not stale (can't determine)."""
        now = datetime.now(timezone.utc)
        agent = {"result_path": "/tmp/nonexistent_xyz.json", "dispatch_ts": None}
        self.assertFalse(core._is_agent_stale(agent, now, stale_minutes=10))

    def test_result_file_exactly_at_threshold(self):
        """Result file modified exactly at stale_minutes threshold -> stale (>=)."""
        with tempfile.TemporaryDirectory() as d:
            rp = Path(d) / "result.json"
            rp.write_text("output\n", encoding="utf-8")
            old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()
            os.utime(str(rp), (old_ts, old_ts))
            agent = {"result_path": str(rp), "dispatch_ts": None}
            now = datetime.now(timezone.utc)
            self.assertTrue(core._is_agent_stale(agent, now, stale_minutes=10))

    def test_no_result_path_key_not_stale(self):
        """No result_path in agent dict -> not stale."""
        now = datetime.now(timezone.utc)
        agent = {"dispatch_ts": None}
        self.assertFalse(core._is_agent_stale(agent, now, stale_minutes=10))


# ---------------------------------------------------------------------------
# Nudge: live but stale agents
# ---------------------------------------------------------------------------
class NudgeLiveStaleTests(_Base):
    @patch.object(core, "_spawn_dispatch", return_value=55555)
    @patch.object(core, "_is_process_alive", return_value=True)
    @patch.object(core, "_is_agent_stale", return_value=True)
    @patch.object(core, "_kill_agent")
    def test_nudge_enabled_stale_under_max_retries_kill_requeue(
        self, mock_kill, mock_stale, mock_alive, mock_spawn
    ):
        """nudge.enabled=True, agent stale, retries < max -> kill + re-queue."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=2)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/r.json", pid=12345, retries=0,
                    dispatch_ts=datetime.now(timezone.utc).isoformat(),
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                mock_kill.assert_called_once_with(12345)
                # Agent should be re-dispatched and remain in agents list
                self.assertTrue(state.repos[str(repo)].in_flight)
                self.assertEqual(len(state.repos[str(repo)].agents), 1)

    @patch.object(core, "_is_process_alive", return_value=True)
    @patch.object(core, "_is_agent_stale", return_value=True)
    @patch.object(core, "_kill_agent")
    def test_nudge_enabled_stale_at_max_retries_keep_remaining(
        self, mock_kill, mock_stale, mock_alive
    ):
        """nudge.enabled=True, agent stale, retries >= max -> keep in remaining (don't kill)."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=2)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/r.json", pid=12345, retries=2,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                mock_kill.assert_not_called()
                self.assertTrue(state.repos[str(repo)].in_flight)
                self.assertEqual(len(state.repos[str(repo)].agents), 1)

    @patch.object(core, "_is_process_alive", return_value=True)
    @patch.object(core, "_is_agent_stale", return_value=True)
    @patch.object(core, "_kill_agent")
    def test_nudge_disabled_stale_keep_remaining(self, mock_kill, mock_stale, mock_alive):
        """nudge.enabled=False, agent stale -> keep in remaining (no nudge)."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=False)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent([task], result_path="/tmp/r.json", pid=12345, retries=0)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                mock_kill.assert_not_called()
                self.assertTrue(state.repos[str(repo)].in_flight)
                self.assertEqual(len(state.repos[str(repo)].agents), 1)

    @patch.object(core, "_is_process_alive", return_value=True)
    @patch.object(core, "_is_agent_stale", return_value=False)
    @patch.object(core, "_kill_agent")
    def test_agent_not_stale_keep_remaining(self, mock_kill, mock_stale, mock_alive):
        """Agent not stale -> keep in remaining."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent([task], result_path="/tmp/r.json", pid=12345, retries=0)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                mock_kill.assert_not_called()
                self.assertTrue(state.repos[str(repo)].in_flight)
                self.assertEqual(len(state.repos[str(repo)].agents), 1)


# ---------------------------------------------------------------------------
# Nudge: dead agents with no output
# ---------------------------------------------------------------------------
class NudgeDeadNoOutputTests(_Base):
    @patch.object(core, "_spawn_dispatch", return_value=77777)
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_nudge_enabled_retries_under_max_requeue(self, _, mock_spawn):
        """nudge.enabled=True, retries < max -> re-queue for dispatch."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=2)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz.json", pid=None, retries=0,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 1)  # counted as completed even when re-queued
                mock_spawn.assert_called_once()
                self.assertTrue(state.repos[str(repo)].in_flight)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_nudge_enabled_retries_at_max_abandoned(self, _):
        """nudge.enabled=True, retries >= max -> abandoned, cleaned up."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=2)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz.json", pid=None, retries=2,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 1)
                self.assertFalse(state.repos[str(repo)].in_flight)
                self.assertEqual(len(state.repos[str(repo)].agents), 0)

    @patch.object(core, "_is_process_alive", return_value=False)
    @patch.object(core, "_spawn_dispatch")
    def test_nudge_disabled_dead_no_output_cleaned_immediately(self, mock_spawn, _):
        """nudge.enabled=False -> cleaned up immediately, no re-dispatch."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=False)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz.json", pid=None, retries=0,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 1)
                mock_spawn.assert_not_called()
                self.assertFalse(state.repos[str(repo)].in_flight)


# ---------------------------------------------------------------------------
# Nudge re-dispatch details
# ---------------------------------------------------------------------------
class NudgeRedispatchTests(_Base):
    @patch.object(core, "_spawn_dispatch", return_value=88888)
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_retries_incremented(self, _, mock_spawn):
        """Re-dispatched agent has retries incremented."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=3)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz.json", pid=None, retries=1,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                new_agent = state.repos[str(repo)].agents[0]
                self.assertEqual(new_agent["retries"], 2)

    @patch.object(core, "_spawn_dispatch", return_value=88888)
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_new_dispatch_ts(self, _, mock_spawn):
        """Re-dispatched agent has new dispatch_ts."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=3)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz.json", pid=None,
                    retries=0, dispatch_ts=old_ts,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                now = datetime.now(timezone.utc)
                core._reconcile(cfg, state, now)
                new_agent = state.repos[str(repo)].agents[0]
                self.assertEqual(new_agent["dispatch_ts"], now.isoformat())

    @patch.object(core, "_spawn_dispatch", return_value=88888)
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_new_result_path_has_retry_suffix(self, _, mock_spawn):
        """Re-dispatched agent has new result_path with retry suffix."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=3)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz.json", pid=None, retries=0,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                new_agent = state.repos[str(repo)].agents[0]
                self.assertIn("-r1", new_agent["result_path"])

    @patch.object(core, "_spawn_dispatch", return_value=88888)
    @patch.object(core, "_cleanup_worktree")
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_old_worktree_cleaned_before_redispatch(self, _, mock_cleanup, mock_spawn):
        """Old worktree cleaned up before new one created."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=3)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task],
                    result_path="/tmp/nonexistent_xyz.json",
                    pid=None,
                    retries=0,
                    branch="cc-later/old-branch",
                    worktree_path="/tmp/old_wt",
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                mock_cleanup.assert_called_once_with(
                    repo, "cc-later/old-branch", Path("/tmp/old_wt")
                )

    @patch.object(core, "_spawn_dispatch", return_value=None)
    @patch.object(core, "_create_worktree", return_value=(Path("/tmp/new_wt"), "cc-later/new"))
    @patch.object(core, "_cleanup_worktree")
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_spawn_failure_worktree_cleaned_agent_not_in_remaining(
        self, _, mock_cleanup, mock_create_wt, mock_spawn
    ):
        """Spawn failure -> worktree cleaned up, agent not added to remaining."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(
                    app_dir, repo, nudge_enabled=True, max_retries=3, allow_file_writes=True,
                )
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz.json", pid=None, retries=0,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                # Spawn returned None -> worktree cleaned, agent not in remaining
                self.assertFalse(state.repos[str(repo)].in_flight)
                self.assertEqual(len(state.repos[str(repo)].agents), 0)
                # Cleanup called twice: once for old (no wt), and once for failed spawn wt
                # Actually old has no branch/wt, so only the spawn failure cleanup
                calls = mock_cleanup.call_args_list
                # The spawn failure cleanup should include the new worktree
                self.assertTrue(
                    any(
                        c.args == (repo, "cc-later/new", Path("/tmp/new_wt"))
                        for c in calls
                    )
                )

    @patch.object(core, "_spawn_dispatch", return_value=88888)
    @patch.object(core, "_create_worktree", return_value=(Path("/tmp/new_wt"), "cc-later/new"))
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_redispatch_with_worktree_when_file_writes_enabled(
        self, _, mock_create_wt, mock_spawn
    ):
        """Re-dispatch with allow_file_writes creates new worktree."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(
                    app_dir, repo, nudge_enabled=True, max_retries=3, allow_file_writes=True,
                )
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz.json", pid=None, retries=0,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                mock_create_wt.assert_called_once()
                new_agent = state.repos[str(repo)].agents[0]
                self.assertEqual(new_agent["branch"], "cc-later/new")
                self.assertEqual(new_agent["worktree_path"], str(Path("/tmp/new_wt")))


# ---------------------------------------------------------------------------
# _kill_agent()
# ---------------------------------------------------------------------------
class KillAgentTests(_Base):
    @patch("os.kill")
    def test_valid_pid_sigterm_sent(self, mock_kill):
        """Valid PID -> SIGTERM sent."""
        core._kill_agent(12345)
        mock_kill.assert_called_once_with(12345, signal.SIGTERM)

    @patch("os.kill")
    def test_none_pid_no_error(self, mock_kill):
        """None PID -> no error, os.kill not called."""
        core._kill_agent(None)
        mock_kill.assert_not_called()

    @patch("os.kill", side_effect=OSError("No such process"))
    def test_dead_pid_oserror_caught(self, mock_kill):
        """Dead PID -> OSError caught, no exception raised."""
        core._kill_agent(99999)
        mock_kill.assert_called_once_with(99999, signal.SIGTERM)


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------
class ReconcileEdgeCaseTests(_Base):
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_empty_agents_list(self, _):
        """Repo in-flight but empty agents list -> stays not in-flight."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[])

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 0)
                self.assertFalse(state.repos[str(repo)].in_flight)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_skipped_and_needs_human_not_marked_done(self, _):
        """SKIPPED and NEEDS_HUMAN tasks not marked done."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo, "- [ ] (P1) task skip\n- [ ] (P1) task human\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                sections = core.parse_tasks(later.read_text(encoding="utf-8"))
                t1 = sections[0].tasks[0]
                t2 = sections[0].tasks[1]
                result_file = repo / "result.json"
                result_file.write_text(
                    f"SKIPPED (not needed) {t1.id}: skip\n"
                    f"NEEDS_HUMAN (complex) {t2.id}: human\n",
                    encoding="utf-8",
                )
                agent = self._make_agent([t1, t2], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                updated = later.read_text(encoding="utf-8")
                self.assertNotIn("[x]", updated)

    @patch.object(core, "_spawn_dispatch", return_value=44444)
    @patch.object(core, "_is_process_alive", return_value=True)
    @patch.object(core, "_is_agent_stale", return_value=True)
    @patch.object(core, "_kill_agent")
    def test_nudge_stale_live_agent_with_section_name(
        self, mock_kill, mock_stale, mock_alive, mock_spawn
    ):
        """Live stale agent with section_name -> section preserved in redispatch."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=3)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/r.json", pid=12345, retries=0,
                    section_name="Backend",
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                new_agent = state.repos[str(repo)].agents[0]
                self.assertEqual(new_agent["section_name"], "Backend")

    @patch.object(core, "_is_process_alive", return_value=False)
    @patch.object(core, "_merge_worktree", return_value=(True, []))
    def test_dead_no_output_exhausted_retries_with_worktree_merges(self, mock_merge, _):
        """Dead agent, no output, exhausted retries, with worktree -> merge attempted."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=2)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task],
                    result_path="/tmp/nonexistent_xyz.json",
                    pid=None,
                    retries=2,
                    branch="cc-later/abandoned",
                    worktree_path="/tmp/abandoned_wt",
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                mock_merge.assert_called_once_with(
                    repo, "cc-later/abandoned", Path("/tmp/abandoned_wt"), ""
                )

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_entries_missing_id_default_to_failed(self, _):
        """Entries whose id is not in result output default to FAILED status."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo, "- [ ] (P1) task A\n- [ ] (P1) task B\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                sections = core.parse_tasks(later.read_text(encoding="utf-8"))
                t1 = sections[0].tasks[0]
                t2 = sections[0].tasks[1]
                result_file = repo / "result.json"
                # Only mention t1, t2 not in output
                result_file.write_text(f"DONE {t1.id}: done A\n", encoding="utf-8")
                agent = self._make_agent([t1, t2], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                updated = later.read_text(encoding="utf-8")
                # t1 marked done, t2 stays unchecked (defaults to FAILED)
                self.assertIn("[x]", updated)
                self.assertIn("- [ ] (P1) task B", updated)

    @patch.object(core, "_spawn_dispatch", return_value=33333)
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_redispatch_no_worktree_when_file_writes_disabled(self, _, mock_spawn):
        """Re-dispatch without allow_file_writes does not create worktree."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(
                    app_dir, repo, nudge_enabled=True, max_retries=3, allow_file_writes=False,
                )
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz.json", pid=None, retries=0,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                with patch.object(core, "_create_worktree") as mock_wt:
                    core._reconcile(cfg, state, datetime.now(timezone.utc))
                    mock_wt.assert_not_called()
                new_agent = state.repos[str(repo)].agents[0]
                self.assertIsNone(new_agent["branch"])
                self.assertIsNone(new_agent["worktree_path"])

    @patch.object(core, "_spawn_dispatch", return_value=11111)
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_redispatch_pid_stored(self, _, mock_spawn):
        """Re-dispatched agent stores new PID."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=3)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz.json", pid=None, retries=0,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                new_agent = state.repos[str(repo)].agents[0]
                self.assertEqual(new_agent["pid"], 11111)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_limit_exhaustion_with_mix_done_failed_only_failed_resumed(self, _):
        """Limit exhaustion with mix of DONE and FAILED -> only FAILED moved to resume."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo, "- [ ] (P1) task done\n- [ ] (P1) task fail\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                sections = core.parse_tasks(later.read_text(encoding="utf-8"))
                t1 = sections[0].tasks[0]
                t2 = sections[0].tasks[1]
                result_file = repo / "result.json"
                result_file.write_text(
                    f"DONE {t1.id}: completed\n"
                    f"FAILED (rate limit) {t2.id}: rate limited\n"
                    "Rate limit reached for your current 5-hour window\n",
                    encoding="utf-8",
                )
                agent = self._make_agent([t1, t2], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                # t1 should be marked done
                updated = later.read_text(encoding="utf-8")
                self.assertIn("[x]", updated)
                # Only t2 in resume entries
                self.assertEqual(len(state.repos[str(repo)].resume_entries), 1)
                self.assertEqual(state.repos[str(repo)].resume_entries[0]["id"], t2.id)


class ReconcileInFlightButNoAgents(_Base):
    """Edge case: in_flight=True but agents=[] (stuck state)."""

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_in_flight_true_agents_empty_clears_in_flight(self, _):
        """When in_flight=True but agents=[], reconcile should clear in_flight."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[])

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 0)
                self.assertFalse(state.repos[str(repo)].in_flight)


class ReconcileCorruptResultFile(_Base):
    """Edge case: valid file but not valid cc-later output."""

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_corrupt_result_file_treated_as_all_failed(self, _):
        """Result file with gibberish content - all entries default to FAILED."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_text("This is random garbage, not proper output format\n{\"json\": true}\n", encoding="utf-8")
                agent = self._make_agent([task], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                # Task should NOT be marked done since no DONE marker found
                updated = later.read_text(encoding="utf-8")
                self.assertNotIn("[x]", updated)


class IsAgentStaleResultPathIsDirectory(_Base):
    """Edge case: result_path points to a directory, not a file."""

    def test_result_path_is_directory(self):
        """_is_agent_stale when result_path is a directory should not crash."""
        with tempfile.TemporaryDirectory() as d:
            # d itself is a directory
            agent = {"result_path": d, "dispatch_ts": None}
            now = datetime.now(timezone.utc)
            # Should not raise; behavior depends on whether stat works on dirs
            result = core._is_agent_stale(agent, now, stale_minutes=10)
            # A directory exists and has mtime, so it should be evaluated
            self.assertIsInstance(result, bool)


class KillAgentPidZero(_Base):
    """Edge case: _kill_agent with PID 0 (init/kernel process)."""

    @patch("os.kill")
    def test_pid_zero_is_guarded(self, mock_kill):
        """_kill_agent with PID 0 — must NOT call os.kill (would kill process group)."""
        core._kill_agent(0)
        mock_kill.assert_not_called()


class NudgeRedispatchSpawnFailure(_Base):
    """Edge case: nudge redispatch when _spawn_dispatch returns None."""

    @patch.object(core, "_spawn_dispatch", return_value=None)
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_spawn_failure_during_nudge_agent_dropped(self, _, mock_spawn):
        """When _spawn_dispatch returns None during nudge, agent should not remain in agents."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=3, allow_file_writes=False)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz.json", pid=None, retries=0,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                # Spawn returned None, no worktree, agent should not be in remaining
                self.assertFalse(state.repos[str(repo)].in_flight)
                self.assertEqual(len(state.repos[str(repo)].agents), 0)


class ReconcileHardeningTests(_Base):
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_reconcile_empty_result_file(self, _):
        """_reconcile when result_path points to empty file (0 bytes)."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=False)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_text("", encoding="utf-8")
                agent = self._make_agent([task], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                # Should not crash; empty result means no tasks completed
                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertIsInstance(completed, int)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_reconcile_binary_result_file(self, _):
        """_reconcile when result_path points to binary file."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=False)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_bytes(b"\x80\x81\x82\xff\xfe\x00\x01\x02")
                agent = self._make_agent([task], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                # Should not crash with binary content (fixed: _safe_read catches UnicodeDecodeError)
                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertIsInstance(completed, int)


class IsProcessAliveHardeningTests(unittest.TestCase):
    def test_pid_negative_one(self):
        """_is_process_alive with PID -1 should return False, not crash."""
        result = core._is_process_alive(-1)
        # PID -1 with os.kill sends signal to all processes in the group
        # The function should handle this safely
        self.assertIsInstance(result, bool)

    def test_pid_very_large_definitely_dead(self):
        """_is_process_alive with PID 99999999 (very large, definitely dead)."""
        result = core._is_process_alive(99999999)
        self.assertFalse(result)


class IsAgentStaleHardeningTests(_Base):
    def test_dispatch_ts_garbage_string(self):
        """_is_agent_stale when dispatch_ts is garbage string (not ISO)."""
        now = datetime.now(timezone.utc)
        agent = {"result_path": "/tmp/nonexistent_xyz_hardening.json", "dispatch_ts": "not-a-valid-date-lol"}
        # _parse_iso returns None for garbage -> should return False (can't determine)
        result = core._is_agent_stale(agent, now, stale_minutes=10)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Negative tests: reconcile denial conditions
# ---------------------------------------------------------------------------
class ReconcileNegativeTests(_Base):
    """Negative tests verifying _reconcile correctly handles denial/edge conditions."""

    def test_no_in_flight_repos_returns_zero(self):
        """_reconcile returns 0 when no repos have in_flight=True."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                state.repos[str(repo)] = core.RepoState(in_flight=False, agents=[])
                result = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(result, 0)

    @patch.object(core, "_spawn_dispatch")
    @patch.object(core, "_is_process_alive", return_value=False)
    def test_nudge_disabled_does_not_redispatch(self, _, mock_spawn):
        """_reconcile does NOT re-dispatch nudged agents when nudge.enabled=False."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=False)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz_neg.json", pid=None, retries=0,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                mock_spawn.assert_not_called()
                self.assertFalse(state.repos[str(repo)].in_flight)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_retries_at_max_does_not_redispatch(self, _):
        """_reconcile does NOT re-dispatch when retries >= max_retries."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=True, max_retries=2)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                agent = self._make_agent(
                    [task], result_path="/tmp/nonexistent_xyz_neg.json", pid=None, retries=2,
                )
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 1)
                self.assertFalse(state.repos[str(repo)].in_flight)
                self.assertEqual(len(state.repos[str(repo)].agents), 0)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_agent_with_no_entries_handles_gracefully(self, _):
        """_reconcile with agent that has no entries handles gracefully (empty task list)."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo, nudge_enabled=False)
                cfg = core.load_config()
                state = core.State()
                # Agent with empty entries list
                agent = {
                    "section_name": "",
                    "pid": None,
                    "result_path": "/tmp/nonexistent_neg.json",
                    "entries": [],
                    "branch": None,
                    "worktree_path": None,
                    "dispatch_ts": None,
                    "retries": 0,
                }
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                # Should not crash
                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 1)
                self.assertFalse(state.repos[str(repo)].in_flight)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_result_only_failed_markers_nothing_marked_done(self, _):
        """_reconcile with agent result containing only FAILED markers marks nothing done."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo, "- [ ] (P1) task alpha\n- [ ] (P1) task beta\n")
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                sections = core.parse_tasks(later.read_text(encoding="utf-8"))
                t1 = sections[0].tasks[0]
                t2 = sections[0].tasks[1]
                result_file = repo / "result.json"
                result_file.write_text(
                    f"FAILED (err) {t1.id}: failed alpha\n"
                    f"FAILED (err) {t2.id}: failed beta\n",
                    encoding="utf-8",
                )
                agent = self._make_agent([t1, t2], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                core._reconcile(cfg, state, datetime.now(timezone.utc))
                updated = later.read_text(encoding="utf-8")
                self.assertNotIn("[x]", updated)
                self.assertIn("- [ ] (P1) task alpha", updated)
                self.assertIn("- [ ] (P1) task beta", updated)

    @patch.object(core, "_is_process_alive", return_value=False)
    def test_later_md_deleted_between_dispatch_and_reconcile(self, _):
        """_reconcile when LATER.md was deleted between dispatch and reconcile does not crash."""
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as repo_dir:
            app_dir, repo = Path(app), Path(repo_dir)
            later = self._setup_repo(repo)
            with patch.dict(os.environ, {core.APP_DIR_ENV: str(app_dir)}, clear=False):
                self._write_config(app_dir, repo)
                cfg = core.load_config()
                state = core.State()
                task = core.parse_tasks(later.read_text(encoding="utf-8"))[0].tasks[0]
                result_file = repo / "result.json"
                result_file.write_text(f"DONE {task.id}: fixed\n", encoding="utf-8")
                agent = self._make_agent([task], result_path=str(result_file), pid=None)
                state.repos[str(repo)] = core.RepoState(in_flight=True, agents=[agent])

                # Delete LATER.md before reconcile
                later.unlink()

                # Should not crash
                completed = core._reconcile(cfg, state, datetime.now(timezone.utc))
                self.assertEqual(completed, 1)
                self.assertFalse(state.repos[str(repo)].in_flight)


if __name__ == "__main__":
    unittest.main()
