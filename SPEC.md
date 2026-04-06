# cc-later Technical Specification

Internal architecture and component contracts for contributors.

---

## Architecture overview

All logic lives in `cc_later/core.py`. The three entry-point scripts (`handler.py`, `capture.py`, `status.py`) each add the plugin root to `sys.path` and call one function in `core`. No external Python dependencies — pure stdlib.

```
Claude Code (hook system)
    │
    ├─ Stop event ──────────────────► scripts/handler.py
    │                                       │
    │                                 core.run_handler()
    │                                       │
    │                     ┌─────────────────┼──────────────────┐
    │                     │                 │                  │
    │                load_config()     load_state()   resolve_watch_paths()
    │                                        │
    │                                  _reconcile()
    │                                  ├── check PIDs alive
    │                                  ├── parse result files
    │                                  ├── detect_limit_exhaustion()
    │                                  ├── mark_done_in_content()
    │                                  ├── _merge_worktree() [if file writes]
    │                                  └── accumulate resume_entries
    │                                        │
    │                                  gate sequence
    │                                        │ (all pass)
    │                                        │
    │                                  for each repo:
    │                                  for each section:
    │                                    select_tasks()
    │                                    _create_worktree() [if file writes]
    │                                    _render_prompt()
    │                                    _spawn_dispatch()  ← non-blocking Popen
    │                                        │
    │                                  save_state()
    │
    └─ UserPromptSubmit ─────────► scripts/capture.py
                                         │
                                   core.capture_from_payload()
                                         │
                                   CAPTURE_RE.finditer(prompt)
                                         │
                                   append to .claude/LATER.md
```

---

## Component responsibilities

### `core.run_handler()`

Main entry point for the `Stop` hook. Sequence:

1. Load config.
2. Read hook payload from stdin (JSON: `cwd`, `session_id`).
3. Load state.
4. Run `_reconcile()` — processes all completed or failed in-flight agents.
5. Resolve watch paths.
6. Update `last_hook_ts` for idle grace tracking.
7. Execute gate sequence (see below). Any failed gate logs a skip event and returns 0.
8. For each watched repo, for each section in LATER.md:
   - If `allow_file_writes`: create a worktree on a new branch.
   - Render prompt, spawn agent.
   - Append agent record to `repo_state.agents`.
9. Save state.

### `core._reconcile(cfg, state, now_utc) -> int`

Runs at the start of every handler invocation. For each repo with `in_flight=True`:

- Iterates `agents`. Agents whose PID is still alive (via `os.kill(pid, 0)`) are kept in `remaining`.
- For dead-PID agents (including `pid=None`, which is the test injection path):
  1. Read result file. If missing: attempt worktree merge/cleanup anyway, count as completed.
  2. Parse structured output lines via `parse_result_summary()`.
  3. Default any unparsed task IDs to `FAILED`.
  4. If `branch` is set: call `_merge_worktree()`.
     - On conflict: override all task statuses to `NEEDS_HUMAN`, log `merge_conflict`, print worktree path.
     - On success or no-op: worktree and branch cleaned up automatically.
  5. Run `detect_limit_exhaustion()`. If triggered: append FAILED/NEEDS_HUMAN tasks to `resume_entries`.
  6. Mark DONE task IDs as `[x]` in LATER.md via `mark_done_in_content()`.
- After all agents processed: `in_flight = bool(remaining)`.

Returns count of completed agents.

### `core.capture_from_payload(payload)`

Called by the `UserPromptSubmit` hook. Searches `payload["prompt"]` with `CAPTURE_RE`. For each match:

- Extracts urgency flag (`[!]`) and task text.
- Skips texts under 3 characters or already present in LATER.md (case-insensitive substring).
- Appends `- [ ] (P0|P1) <text>` to LATER.md.

### `core.build_status()` / `core.run_status()`

Collects window state, budget state, per-repo queue depths and in-flight agent records, gate pass/fail, and recent `run_log.jsonl` entries. Returns a formatted multi-section string for `/cc-later:status`.

### `core.compute_window_state(roots, now_utc, session_id)`

Reads JSONL files from the Claude projects directories. Scans rows within the last 5 hours, finds the earliest timestamp, computes elapsed/remaining against a 300-minute window. Returns `None` if no recent data.

### `core.compute_budget_state(roots, now_utc, weekly_budget)`

Scans JSONL files modified within the last 7 days. Sums all token counts. Returns `BudgetState(used_tokens, pct_used)`.

---

## LATER.md parsing rules

`parse_tasks(content: str) -> list[Section]`

1. Iterate lines with index (0-based, used for stable task IDs).
2. A line matching `^##\s+(.+)` starts a new named section. Flush any accumulated tasks into the previous section first.
3. A task line must match `TASK_RE`:
   ```
   ^(?P<prefix>\s*-\s*)\[(?P<mark>[ xX!])\](?P<space>\s*)(?:(?P<prio>\(P[0-2]\))\s*)?(?P<text>.+?)\s*$
   ```
4. Lines where `mark` is `x` or `X` are skipped — already completed.
5. Priority mapping:
   - `mark == "!"` → `P0`
   - `mark == " "` → use explicit `(P0|P1|P2)` prefix, default `P1`
6. Task IDs: `"t_" + sha1(f"{line_index}:{text}".encode())[:10]`. Stable on content+position. Moving a task in the file changes its ID.
7. Tasks before the first `##` header collect into `Section(name="")`.
8. Trailing tasks after the last section header are flushed at end of input.

`select_tasks(section: Section, limit: int) -> list[Task]`

Sorts by `(rank[priority], line_index)` where `rank = {"P0": 0, "P1": 1, "P2": 2}`. Returns at most `limit` tasks.

---

## Dispatch gate logic

All gates are evaluated in `run_handler()` before any per-repo work.

```
Gate 1: DISPATCH_ENABLED
  cfg.dispatch.enabled == False
  → log skip(dispatch_disabled), return 0

Gate 2: Idle grace
  previous_hook is not None AND
  (now_utc - previous_hook).total_seconds() / 60 < idle_grace_period_minutes
  → log skip(idle_grace_active), return 0

Gate 3: Weekly budget
  budget.pct_used >= backoff_at_pct / 100
  → log skip(budget_limit), return 0

Gate 4: Mode gate OR auto-resume gate
  mode_open = _mode_gate_open(cfg, now_local, window_state)
  resume_open = _auto_resume_gate_open(cfg, watch_paths, state, window_state)
  if not mode_open and not resume_open:
    → log skip(mode_gate_closed), return 0
```

Within the per-repo loop:
- If `resume_open` and repo has `resume_entries`: dispatch resume batch, `continue` (skip section dispatch for this repo this cycle).
- If `not mode_open`: skip repo.

---

## Mode gate logic

`_mode_gate_open(cfg, now_local, window_state) -> bool`

- `always` → `True`
- `time_based` → `_in_time_windows(now_local, fallback_dispatch_hours)`. Ranges are `HH:MM-HH:MM`. Overnight ranges (start > end) handled correctly. `24:00` parses as 1440 minutes.
- `window_aware` → `window_state is not None and window_state.remaining_minutes <= trigger_at_minutes_remaining`

## Auto-resume gate logic

`_auto_resume_gate_open(cfg, watch_paths, state, window_state) -> bool`

1. `AUTO_RESUME_ENABLED` must be `true`.
2. At least one watched repo must have non-empty `resume_entries`.
3. `window_aware` mode: `window_state.remaining_minutes >= auto_resume.min_remaining_minutes`.
4. Other modes: condition 3 always passes.

---

## Parallel agent model and worktrees

When `DISPATCH_ALLOW_FILE_WRITES=true`, each section agent requires an isolated working directory. Without isolation, agents writing to the same files simultaneously produce undefined results — the last writer wins and intermediate commits are lost.

### Worktree creation — `_create_worktree(repo, section_slug, timestamp)`

```python
branch = f"cc-later/{section_slug}-{timestamp}"
worktree_path = app_dir() / "worktrees" / f"{repo.name}-{section_slug}-{timestamp}"
worktree_path.parent.mkdir(parents=True, exist_ok=True)
result = subprocess.run(
    ["git", "worktree", "add", str(worktree_path), "-b", branch],
    cwd=str(repo), capture_output=True, text=True
)
# returns (worktree_path, branch) or None on failure
```

The agent is spawned with `cwd=worktree_path`. The worktree shares the repo's object store — the agent sees all committed files and commits changes to its own branch, invisible to other agents.

All sections in one dispatch cycle share the same `timestamp` (computed once at the top of the repo loop), so branches from the same cycle are identifiable as a group.

### No-op detection

Before merging, `_merge_worktree` checks whether the branch has any commits ahead of HEAD:

```python
diff = subprocess.run(["git", "rev-list", "--count", f"HEAD..{branch}"], cwd=str(repo), ...)
if diff.stdout.strip() == "0":
    # agent made no commits — skip merge, just clean up
    _cleanup_worktree(repo, branch, worktree_path)
    return True, []
```

This avoids empty merge commits when an agent completed tasks by reporting only (no file writes despite `allow_file_writes=true`).

### Merge on reconcile — `_merge_worktree(repo, branch, worktree_path, section_name)`

```python
result = subprocess.run(
    ["git", "merge", "--no-ff", branch, "-m", f"cc-later: {section_name} tasks"],
    cwd=str(repo), capture_output=True, text=True
)
if result.returncode == 0:
    _cleanup_worktree(repo, branch, worktree_path)
    return True, []
else:
    # collect conflicting files, then abort so repo is left clean
    conflict_result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=str(repo), ...
    )
    subprocess.run(["git", "merge", "--abort"], cwd=str(repo))
    return False, conflicting_files
```

Branches are merged in agent completion order (first-finished-first-merged). On conflict: the merge is aborted immediately, repo is clean, worktree preserved. `_reconcile` marks all that agent's tasks `NEEDS_HUMAN`, logs `merge_conflict` with branch and file list, and prints the preserved worktree path.

### Cleanup — `_cleanup_worktree(repo, branch, worktree_path)`

```python
subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)], ...)
subprocess.run(["git", "branch", "-d", branch], cwd=str(repo), ...)
```

Called automatically on successful merge. Also called if spawn fails after worktree creation to avoid orphaned worktrees.

### When file writes are disabled

No worktrees are created (`branch=None`, `worktree_path=None` in agent record). Agents run with repo as `cwd` but the prompt instructs them to report only. This is the default.

### Section slug format

`re.sub(r"[^a-zA-Z0-9_-]", "_", name)`. Empty section name (tasks before first `##`) uses `"default"`. Resume dispatch uses `"resume"`.

| Section | Slug | Branch |
|---|---|---|
| `Auth` | `Auth` | `cc-later/Auth-20260406-100000` |
| `Auth & Tokens` | `Auth___Tokens` | `cc-later/Auth___Tokens-20260406-100000` |
| _(none)_ | `default` | `cc-later/default-20260406-100000` |
| resume | `resume` | `cc-later/resume-20260406-100000` |

---

## RepoState machine

```
(absent)
    │ first dispatch
    ▼
in_flight=True
agents=[{section_name, pid, result_path, entries, branch, worktree_path}]
resume_entries=[]
    │
    │ _reconcile() — some agents still alive
    ▼
in_flight=True, agents=[surviving agents]
    │
    │ _reconcile() — all agents dead, no limit signals, all merges OK
    ▼
in_flight=False, agents=[], resume_entries=[]
    │
    │ _reconcile() — agent dead, limit signals detected
    ▼
in_flight=False, agents=[], resume_entries=[failed tasks], resume_reason="limit_exhausted"
    │
    │ auto-resume gate opens
    ▼
in_flight=True, agents=[resume_agent], resume_entries=[]
    │
    │ (cycles back)
```

`in_flight = bool(remaining_agents)` after every reconcile pass.

---

## Config parsing

File: `~/.cc-later/config.env` (or `$CC_LATER_APP_DIR/config.env`). Created from `scripts/default_config.env` if absent.

Format: `KEY=VALUE` per line. `#`-prefixed lines and lines without `=` are ignored. Values are not quoted.

Type coercions:
- `bool`: `true`, `1`, `yes` → `True`; anything else → `False`
- `list`: comma-split, stripped, empty entries removed
- `int`: Python `int()`

Post-load validation (`_validate_values`):
- `dispatch_mode` ∈ `{window_aware, time_based, always}`
- `dispatch.model` ∈ `{sonnet, opus, haiku}`
- `limits.weekly_budget_tokens > 0`
- `0 ≤ limits.backoff_at_pct ≤ 100`
- `auto_resume.min_remaining_minutes ≥ 0`
- `later.max_entries_per_dispatch > 0`

---

## Window detection algorithm

`compute_window_state(roots, now_utc, session_id) -> WindowState | None`

1. `cutoff = now_utc - 5h`, `future_cutoff = now_utc + 5m`.
2. Collect all `.jsonl` files recursively under each root. If root is a file, use it directly.
3. Skip files with `mtime > 5h` ago.
4. If `session_id` provided: skip files whose path does not contain `session_id`.
5. For each row: parse timestamp from `timestamp`, `ts`, or `created_at` keys.
6. Skip rows outside `[cutoff, future_cutoff]`.
7. Track earliest timestamp in window → window start.
8. Accumulate `input_tokens + cache_creation_input_tokens + output_tokens` from `message_usage` or `usage`.
9. `elapsed = floor((now_utc - earliest).total_seconds() / 60)`, minimum 0.
10. `remaining = max(0, 300 - elapsed)`.

Returns `None` if no eligible rows found (no active session).

JSONL root auto-detection order:
1. `$CLAUDE_CONFIG_DIR/projects`
2. `~/.config/claude/projects`
3. `~/.claude/projects`

---

## Budget calculation

`compute_budget_state(roots, now_utc, weekly_budget) -> BudgetState`

1. `cutoff = now_utc - 7 days`.
2. Collect all `.jsonl` files under each root.
3. Skip files with `mtime < cutoff` (file-level filter — efficient, avoids row-by-row date parsing for old files).
4. Sum all token counts from all rows in qualifying files.
5. `pct_used = min(1.0, used / max(1, weekly_budget))`.

---

## Auto-resume flow

1. `detect_limit_exhaustion(raw) -> str | None`: returns `"limit_exhausted"` if any of these strings appear (case-insensitive) in the agent output: `"rate limit"`, `"usage limit"`, `"quota"`, `"too many requests"`, `"429"`, `"5-hour window"`, `"window exhausted"`, `"try again later"`.
2. In `_reconcile()`: if limit exhaustion detected, tasks with status `FAILED` or `NEEDS_HUMAN` are appended to `repo_state.resume_entries`. `resume_entries.extend(...)` — accumulates across multiple finishing agents.
3. `resume_reason = "limit_exhausted"`.
4. On next handler run: auto-resume gate opens → all `resume_entries` dispatched as one agent (no section grouping; uses worktree if `allow_file_writes`).
5. `resume_entries = []`, `resume_reason = None` after successful spawn.
6. Resume dispatch is followed by `continue` — prevents double-dispatch with normal sections in the same cycle.

---

## Agent output protocol

Agents write one line per task to stdout (captured to result file). Parsed by `RESULT_RE`:

```
^(DONE|SKIPPED|NEEDS_HUMAN|FAILED)(?:\s+\([^)]+\))?\s+([A-Za-z0-9_-]+)\s*:
```

| Status | Meaning | Reconcile outcome |
|---|---|---|
| `DONE <id>: <summary>` | Task completed | Marked `[x]` in LATER.md |
| `SKIPPED (<reason>) <id>: ...` | Not attempted | Not marked, not queued for resume |
| `NEEDS_HUMAN (<reason>) <id>: ...` | Needs human | Queued for resume if limit signals present |
| `FAILED (<reason>) <id>: ...` | Attempted and failed | Queued for resume if limit signals present |

If an agent produces no parseable lines: all its tasks default to `FAILED`.

Rendered prompt format:

```
You are running background maintenance in repository: /path/to/repo
Section: Auth

Tasks:
- t_abc1234567 | P0 | handle expired sessions in middleware
- t_def8901234 | P1 | fix token refresh in src/auth/service.py

Rules:
- Keep changes minimal and directly related to each task.
- If uncertain, return NEEDS_HUMAN with reason.
- Output one line per task in this exact format:
DONE <task_id>: <summary>
SKIPPED (<reason>) <task_id>: <summary>
NEEDS_HUMAN (<reason>) <task_id>: <summary>
FAILED (<reason>) <task_id>: <summary>
- Do not modify files. Report findings/fixes only.
```

Last rule becomes `"You may edit files directly."` when `DISPATCH_ALLOW_FILE_WRITES=true`.

---

## State schema

`~/.cc-later/state.json`:

```json
{
  "last_hook_ts": "2026-04-06T10:00:00+00:00",
  "repos": {
    "/absolute/path/to/repo": {
      "in_flight": true,
      "agents": [
        {
          "section_name": "Auth",
          "pid": 12345,
          "result_path": "/Users/user/.cc-later/results/myrepo-Auth-20260406-100000.json",
          "branch": "cc-later/Auth-20260406-100000",
          "worktree_path": "/Users/user/.cc-later/worktrees/myrepo-Auth-20260406-100000",
          "entries": [
            {
              "id": "t_abc1234567",
              "line_index": 4,
              "priority": "P0",
              "text": "handle expired sessions in middleware"
            }
          ]
        }
```

`branch` and `worktree_path` are `null` when `DISPATCH_ALLOW_FILE_WRITES=false`.

```json
      ],
      "resume_entries": [],
      "resume_reason": null,
      "dispatch_ts": "2026-04-06T10:00:00+00:00"
    }
  }
}
```

`branch` and `worktree_path` are `null` when `allow_file_writes=false`.

---

## Run log events

`~/.cc-later/run_log.jsonl` — append-only, one JSON object per line. All events include `ts` (ISO 8601 UTC).

| Event | Key fields |
|---|---|
| `config_created` | `path` |
| `auto_watch` | `repo` |
| `reconcile` | `completed` |
| `resume_scheduled` | `repo`, `reason`, `entries` |
| `dispatch` | `repo`, `section`, `entries_dispatched`, `entries`, `remaining_minutes`, `model`, `result_path`, `branch` (null if no worktree), `auto_resume` |
| `merge_conflict` | `repo`, `branch`, `section`, `files` (list of conflicting paths), `worktree` (preserved path) |
| `capture` | `repo`, `added` |
| `skip` | `reason` (`dispatch_disabled` \| `idle_grace_active` \| `budget_limit` \| `mode_gate_closed`) |
| `error` | `reason`, `detail` or `repo`, `section` |

---

## Test coverage

| File | Areas |
|---|---|
| `test_config_and_format.py` | Config loading from `.env`, task/section parsing, priority ordering, mark-done rewrite |
| `test_handler_status_capture.py` | Full flow: capture → dispatch (mocked spawn) → reconcile (written result) → mark done → status |
| `test_reconcile_resume.py` | Limit-fail detection → resume_entries populated; DONE → LATER.md updated |
| `test_window_budget.py` | Stale row exclusion, elapsed/remaining math, token accumulation, budget percentage |
| `test_plugin_layout.py` | plugin.json / marketplace.json valid JSON; Stop and UserPromptSubmit hooks present; status command exists |
