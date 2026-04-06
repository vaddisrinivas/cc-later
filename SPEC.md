# cc-later Technical Specification

Internal architecture and component contracts for contributors.

---

## Architecture overview

All logic lives in `cc_later/core.py`. Entry-point scripts (`handler.py`, `capture.py`, `compact.py`, `status.py`, `stats.py`) each add the plugin root to `sys.path` and call one function in `core`. No external Python dependencies -- pure stdlib, Python 3.10+.

```
Claude Code (hook system)
    |
    |-- Stop event -----------------> scripts/handler.py
    |                                       |
    |                                 core.run_handler()
    |                                       |
    |                     +----------------+------------------+
    |                     |                 |                  |
    |                load_config()     load_state()   resolve_watch_paths()
    |                                        |
    |                                  _reconcile()
    |                                  |-- check PIDs alive
    |                                  |-- nudge stale agents (kill + redispatch)
    |                                  |-- nudge dead agents (re-queue)
    |                                  |-- parse result files
    |                                  |-- detect_limit_exhaustion()
    |                                  |-- mark_done_in_content()
    |                                  |-- _merge_worktree() [if file writes]
    |                                  +-- accumulate resume_entries
    |                                        |
    |                                  self-calibrating window detection
    |                                  |-- window_limit_ts tracking
    |                                  +-- window_start_ts auto-calibration
    |                                        |
    |                                  gate sequence
    |                                        | (all pass)
    |                                  for each repo:
    |                                  for each section:
    |                                    select_tasks()
    |                                    _create_worktree() [if file writes]
    |                                    _render_prompt()
    |                                    _spawn_dispatch()  <- non-blocking Popen
    |                                        |
    |                                  save_state()
    |
    |-- UserPromptSubmit -----------> scripts/capture.py
    |                                       |
    |                                 core.capture_from_payload()
    |                                       |
    |                                 CAPTURE_RE.finditer(prompt)
    |                                       |
    |                                 append to .claude/LATER.md
    |
    +-- SessionStart (compact) -----> scripts/compact.py
                                            |
                                      core.run_compact_inject()
                                            |
                                      inject window state + LATER.md queue
                                      into Claude's context
```

---

## Component responsibilities

### `core.run_handler()`

Main entry point for the `Stop` hook. Sequence:

1. Load config.
2. Read hook payload from stdin (JSON: `cwd`, `session_id`).
3. Load state.
4. Run `_reconcile()` -- processes all completed, failed, stale, and dead in-flight agents.
5. Resolve watch paths.
6. Update `last_hook_ts` for idle grace tracking.
7. Self-calibrating window detection: track `window_limit_ts` and `window_start_ts`.
8. Execute gate sequence (see below). Any failed gate logs a skip event and returns 0.
9. For each watched repo, for each section in LATER.md:
   - If `allow_file_writes`: create a worktree on a new branch.
   - Render prompt, spawn agent.
   - Append agent record to `repo_state.agents`.
10. Save state.

### `core._reconcile(cfg, state, now_utc) -> int`

Runs at the start of every handler invocation. For each repo with `in_flight=True`:

- Iterates `agents`. Agents whose PID is still alive (via `os.kill(pid, 0)`) are kept in `remaining`, unless stale (see nudge below).
- **Nudge -- stale agents**: If `NUDGE_ENABLED` and a live agent's result file hasn't been modified for `NUDGE_STALE_MINUTES`, the agent is killed (SIGTERM), its old worktree cleaned up, and re-dispatched with `retries + 1`. Logged as `nudge_stale`.
- **Nudge -- dead agents**: If an agent's process has exited but produced no output, and retries remain, it is re-queued. Logged as `nudge_dead`. If retries exhausted, logged as `agent_abandoned`.
- For dead-PID agents with output:
  1. Read result file. Parse structured output lines via `parse_result_summary()`.
  2. Default any unparsed task IDs to `FAILED`.
  3. If `branch` is set: call `_merge_worktree()`.
     - On conflict: override all task statuses to `NEEDS_HUMAN`, log `merge_conflict`, print worktree path.
     - On success or no-op: worktree and branch cleaned up automatically.
  4. Run `detect_limit_exhaustion()`. If triggered: set `window_limit_ts`, append FAILED/NEEDS_HUMAN tasks to `resume_entries`.
  5. Mark DONE task IDs as `[x]` in LATER.md via `mark_done_in_content()`.
- After all agents processed: `in_flight = bool(remaining)`.

Returns count of completed agents.

### `core.capture_from_payload(payload)`

Called by the `UserPromptSubmit` hook. Searches `payload["prompt"]` with `CAPTURE_RE`. For each match:

- Extracts urgency flag (`[!]`) and task text.
- Skips texts under 3 characters or already present in LATER.md (case-insensitive substring).
- Appends `- [ ] (P0|P1) <text>` to LATER.md.

### `core.run_compact_inject(cwd_hint)`

Called by the `SessionStart` hook (matcher: `compact`). When `COMPACT_ENABLED=true`:

- Loads config, state, and window state.
- Outputs to stdout (which Claude Code injects into context):
  - Current window state (remaining minutes, mode, elapsed)
  - All pending LATER.md tasks grouped by section
  - In-flight dispatch status and agent count
  - Auto-resume queue status

### `core.build_status()` / `core.run_status()`

Collects window state, budget state, per-repo queue depths and in-flight agent records, gate pass/fail, and recent `run_log.jsonl` entries. Returns a formatted multi-section string for `/cc-later:status`.

### `core.run_stats(days)`

Reads all JSONL files (recursive) within the specified day range. Accumulates per-model token counts (input, cache creation, cache read, output). Computes API-equivalent cost using `_MODEL_PRICING` table. Outputs per-model breakdown, grand totals, session count, and comparison to Max plan subscription cost.

### `core.compute_window_state(roots, now_utc, ...)`

Reads JSONL files from the Claude projects directories. Uses session gap detection (gaps >= `session_gap_minutes`) to find the current window's start. Supports `window_start_hint` from state for calibrated accuracy. Computes elapsed/remaining against configurable `window_duration`. Returns `None` if no recent data.

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
4. Lines where `mark` is `x` or `X` are skipped -- already completed.
5. Priority mapping:
   - `mark == "!"` -> `P0`
   - `mark == " "` -> use explicit `(P0|P1|P2)` prefix, default `P1`
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
  -> log skip(dispatch_disabled), return 0

Gate 2: Idle grace
  previous_hook is not None AND
  (now_utc - previous_hook).total_seconds() / 60 < idle_grace_period_minutes
  -> log skip(idle_grace_active), return 0

Gate 3: Weekly budget
  budget.pct_used >= backoff_at_pct / 100
  -> log skip(budget_limit), return 0

Gate 4: Mode gate OR auto-resume gate
  mode_open = _mode_gate_open(cfg, now_local, window_state)
  resume_open = _auto_resume_gate_open(cfg, watch_paths, state, window_state)
  if not mode_open and not resume_open:
    -> log skip(mode_gate_closed), return 0
```

Within the per-repo loop:
- If `resume_open` and repo has `resume_entries`: dispatch resume batch, `continue` (skip section dispatch for this repo this cycle).
- If `not mode_open`: skip repo.

---

## Self-calibrating window detection

Implemented in `run_handler()` after computing initial window state:

```
1. If window_state.remaining <= 0 AND window_limit_ts is None:
     state.window_limit_ts = now_utc   (record exhaustion)
     log "window_exhausted"

2. If window_limit_ts is not None AND
   (now_utc - limit_ts) > idle_grace_period_minutes:
     state.window_start_ts = now_utc   (fresh window detected)
     state.window_limit_ts = None
     log "window_reset_detected"
     recompute window_state with now_utc as window_start_hint

3. Auto-resume dispatch also sets window_start_ts = now_utc
   (fresh window confirmed by successful dispatch)
```

`compute_window_state()` uses `window_start_hint` when available, falling back to gap-based detection with clamp (`now - duration`) as a conservative bound for the first window.

---

## Mode gate logic

`_mode_gate_open(cfg, now_local, window_state) -> bool`

- `always` -> `True`
- `time_based` -> `_in_time_windows(now_local, fallback_dispatch_hours)`. Ranges are `HH:MM-HH:MM`. Overnight ranges (start > end) handled correctly. `24:00` parses as 1440 minutes.
- `window_aware` -> `window_state is not None and window_state.remaining_minutes <= trigger_at_minutes_remaining`

## Auto-resume gate logic

`_auto_resume_gate_open(cfg, watch_paths, state, window_state) -> bool`

1. `AUTO_RESUME_ENABLED` must be `true`.
2. At least one watched repo must have non-empty `resume_entries`.
3. `window_aware` mode: `window_state.remaining_minutes >= auto_resume.min_remaining_minutes`.
4. Other modes: condition 3 always passes.

---

## Nudge logic

Evaluated during `_reconcile()` for each in-flight agent.

### Stale agent detection

`_is_agent_stale(agent, now_utc, stale_minutes) -> bool`

Checks the result file's mtime (or `dispatch_ts` if no file yet). If the age exceeds `NUDGE_STALE_MINUTES`, the agent is considered stale.

For stale agents (PID alive, retries < max):
1. Kill agent with `SIGTERM`
2. Clean up old worktree/branch if any
3. Create fresh worktree, render prompt, spawn new agent
4. Increment retry counter, log `nudge_stale` and `nudge_redispatch`

### Dead agent detection

For agents whose PID is dead and produced no output file (retries < max):
1. Log `nudge_dead`
2. Re-queue into `nudge_queue`
3. Same re-dispatch flow as stale agents

### Retry exhaustion

After `NUDGE_MAX_RETRIES` attempts:
- Agent is abandoned, worktree cleaned up
- Logged as `agent_abandoned`

---

## Parallel agent model and worktrees

When `DISPATCH_ALLOW_FILE_WRITES=true`, each section agent requires an isolated working directory.

### Worktree creation -- `_create_worktree(repo, section_slug, timestamp)`

```python
branch = f"cc-later/{section_slug}-{timestamp}"
worktree_path = app_dir() / "worktrees" / f"{repo.name}-{section_slug}-{timestamp}"
git worktree add {worktree_path} -b {branch}
```

### No-op detection

Before merging, `_merge_worktree` checks whether the branch has any commits ahead of HEAD via `git rev-list --count HEAD..{branch}`. If 0, skip merge and clean up.

### Merge on reconcile -- `_merge_worktree(repo, branch, worktree_path, section_name)`

```python
git merge --no-ff {branch} -m "cc-later: {section_name} tasks"
if success: cleanup worktree + branch
if conflict: collect conflicting files, git merge --abort, preserve worktree
```

### Cleanup -- `_cleanup_worktree(repo, branch, worktree_path)`

```python
git worktree remove --force {worktree_path}
git branch -d {branch}
```

### Section slug format

`re.sub(r"[^a-zA-Z0-9_-]", "_", name)`. Empty section name uses `"default"`. Resume dispatch uses `"resume"`.

---

## State schema

`~/.cc-later/state.json`:

```json
{
  "last_hook_ts": "2026-04-06T10:00:00+00:00",
  "window_start_ts": "2026-04-06T10:00:00+00:00",
  "window_limit_ts": null,
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
          ],
          "dispatch_ts": "2026-04-06T10:00:00+00:00",
          "retries": 0
        }
      ],
      "resume_entries": [],
      "resume_reason": null,
      "dispatch_ts": "2026-04-06T10:00:00+00:00"
    }
  }
}
```

`branch` and `worktree_path` are `null` when `DISPATCH_ALLOW_FILE_WRITES=false`.

---

## RepoState machine

```
(absent)
    | first dispatch
    v
in_flight=True
agents=[{section_name, pid, result_path, entries, branch, worktree_path, dispatch_ts, retries}]
resume_entries=[]
    |
    | _reconcile() -- some agents still alive
    v
in_flight=True, agents=[surviving agents]
    |
    | _reconcile() -- stale agent detected (nudge enabled)
    v
stale agent killed, re-dispatched with retries+1, remains in_flight=True
    |
    | _reconcile() -- all agents dead, no limit signals, all merges OK
    v
in_flight=False, agents=[], resume_entries=[]
    |
    | _reconcile() -- agent dead, limit signals detected
    v
in_flight=False, agents=[], resume_entries=[failed tasks], resume_reason="limit_exhausted"
    |
    | auto-resume gate opens
    v
in_flight=True, agents=[resume_agent], resume_entries=[]
    |
    | (cycles back)
```

`in_flight = bool(remaining_agents)` after every reconcile pass.

---

## Config parsing

File: `~/.cc-later/config.env` (or `$CC_LATER_APP_DIR/config.env`). Created from `scripts/default_config.env` if absent.

Format: `KEY=VALUE` per line. `#`-prefixed lines and lines without `=` are ignored. Values are not quoted.

Type coercions:
- `bool`: `true`, `1`, `yes` -> `True`; anything else -> `False`
- `list`: comma-split, stripped, empty entries removed
- `int`: Python `int()`

Post-load validation (`_validate_values`):
- `dispatch_mode` in `{window_aware, time_based, always}`
- `dispatch.model` in `{sonnet, opus, haiku}`
- `limits.weekly_budget_tokens > 0`
- `0 <= limits.backoff_at_pct <= 100`
- `auto_resume.min_remaining_minutes >= 0`
- `later.max_entries_per_dispatch > 0`

### Plan-based window duration

`PLAN_WINDOW_MINUTES` maps plan names to window durations. All current plans default to 300 minutes. If `WINDOW_DURATION_MINUTES` is left blank, the plan default is used.

---

## Window detection algorithm

`compute_window_state(roots, now_utc, session_id, session_gap_minutes, window_duration, window_start_hint) -> WindowState | None`

1. `cutoff = now_utc - 5h`, `future_cutoff = now_utc + 5m`.
2. Collect all `.jsonl` files (non-recursive, top-level session files only to avoid subagent skew).
3. Skip files with `mtime > 5h` ago.
4. If `session_id` provided: skip files whose path does not contain `session_id`.
5. For each row: parse timestamp from `timestamp`, `ts`, or `created_at` keys.
6. Skip rows outside `[cutoff, future_cutoff]`.
7. Sort rows by timestamp. Find last gap >= `session_gap_minutes` to detect current session start.
8. If most recent row is older than gap threshold: return `None` (no active session).
9. Determine window start from best available signal:
   - `window_start_hint` (from self-calibrating detection -- most accurate)
   - Last gap-based detection
   - Clamp: `now - window_duration` (conservative first-window bound)
10. Accumulate `input_tokens + cache_creation_input_tokens + output_tokens` from `usage`.
11. `elapsed = floor((now_utc - earliest).total_seconds() / 60)`, minimum 0.
12. `remaining = max(0, window_duration - elapsed)`.

Returns `None` if no eligible rows found.

JSONL root auto-detection order:
1. `$CLAUDE_CONFIG_DIR/projects`
2. `~/.config/claude/projects`
3. `~/.claude/projects`

---

## Budget calculation

`compute_budget_state(roots, now_utc, weekly_budget) -> BudgetState`

1. `cutoff = now_utc - 7 days`.
2. Collect all `.jsonl` files under each root (non-recursive).
3. Skip files with `mtime < cutoff` (file-level filter).
4. Sum all token counts from all rows in qualifying files.
5. `pct_used = min(1.0, used / max(1, weekly_budget))`.

---

## Auto-resume flow

1. `detect_limit_exhaustion(raw) -> str | None`: returns `"limit_exhausted"` if any of these strings appear (case-insensitive) in the agent output: `"rate limit"`, `"usage limit"`, `"quota"`, `"too many requests"`, `"429"`, `"5-hour window"`, `"window exhausted"`, `"try again later"`.
2. In `_reconcile()`: if limit exhaustion detected, `window_limit_ts` is set. Tasks with status `FAILED` or `NEEDS_HUMAN` are appended to `repo_state.resume_entries`.
3. `resume_reason = "limit_exhausted"`.
4. On next handler run: auto-resume gate opens -> all `resume_entries` dispatched as one agent (uses worktree if `allow_file_writes`). `window_start_ts` set to now (fresh window confirmed).
5. `resume_entries = []`, `resume_reason = None` after successful spawn.
6. Resume dispatch is followed by `continue` -- prevents double-dispatch with normal sections in the same cycle.

---

## Stats calculation

`run_stats(days) -> int`

1. Collects all JSONL files (recursive) within the specified day range.
2. Groups token usage by normalized model ID (strips date suffixes).
3. Per-model accumulation: input tokens, cache creation tokens, cache read tokens, output tokens.
4. Computes API-equivalent cost using `_MODEL_PRICING` table.
5. Outputs per-model breakdown with cost, grand totals, session count, JSONL file count.
6. Compares against Max plan subscription cost ($200/mo prorated).

Pricing table covers: claude-opus-4-6, claude-opus-4-5, claude-sonnet-4-6, claude-sonnet-4-5, claude-haiku-4-5.

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

---

## Run log events

`~/.cc-later/run_log.jsonl` -- append-only, one JSON object per line. All events include `ts` (ISO 8601 UTC).

| Event | Key fields |
|---|---|
| `config_created` | `path` |
| `auto_watch` | `repo` |
| `reconcile` | `completed` |
| `resume_scheduled` | `repo`, `reason`, `entries` |
| `dispatch` | `repo`, `section`, `entries_dispatched`, `entries`, `remaining_minutes`, `model`, `result_path`, `branch` (null if no worktree), `auto_resume` |
| `merge_conflict` | `repo`, `branch`, `section`, `files` (list), `worktree` (preserved path) |
| `capture` | `repo`, `added` |
| `skip` | `reason` (`dispatch_disabled`, `idle_grace_active`, `budget_limit`, `mode_gate_closed`) |
| `window_exhausted` | _(window limit reached)_ |
| `window_reset_detected` | _(fresh window detected after limit)_ |
| `nudge_stale` | `repo`, `pid`, `section`, `retries` |
| `nudge_dead` | `repo`, `pid`, `section`, `retries` |
| `nudge_redispatch` | `repo`, `section`, `retries`, `pid` |
| `agent_abandoned` | `repo`, `pid`, `section`, `retries` |
| `error` | `reason`, `detail` or `repo`, `section` |

---

## Hooks configuration

`hooks/hooks.json` registers three hooks:

| Hook | Trigger | Script | Timeout |
|---|---|---|---|
| `Stop` | Every session end | `scripts/handler.py` | 10s |
| `UserPromptSubmit` | Prompt matches capture regex | `scripts/capture.py` | 4s |
| `SessionStart` | Session name contains "compact" | `scripts/compact.py` | 5s |

---

## Test coverage

| File | Areas |
|---|---|
| `test_config_and_format.py` | Config loading from `.env`, task/section parsing, priority ordering, mark-done rewrite |
| `test_handler_status_capture.py` | Full flow: capture -> dispatch (mocked spawn) -> reconcile -> mark done -> status |
| `test_handler_worktree_state.py` | Worktree creation, merge, cleanup, state management |
| `test_reconcile_resume.py` | Limit-fail detection -> resume_entries populated; DONE -> LATER.md updated |
| `test_reconcile_nudge.py` | Stale agent detection, dead agent re-queue, retry limits, nudge_redispatch |
| `test_window_budget.py` | Stale row exclusion, elapsed/remaining math, token accumulation, budget percentage |
| `test_window_gates_budget.py` | Window gate logic, budget gate, dispatch mode gates |
| `test_stats_compact_tasks.py` | Stats output, compact injection, task parsing edge cases |
| `test_utils_and_config.py` | Config validation, utility functions, plan defaults |
| `test_plugin_layout.py` | plugin.json / marketplace.json valid JSON; hooks present; status command exists |
