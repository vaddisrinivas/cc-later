# cc-later

A Claude Code plugin that dispatches queued follow-up tasks as parallel background agents. Tasks accumulate in `.claude/LATER.md` during a session. Near the end of each usage window, cc-later spawns one `claude -p` agent per `## Section` — each in its own git worktree to prevent conflicts. Failed tasks auto-resume in the next fresh window. Stuck agents are detected and restarted.

Python 3.10+ with [uv](https://docs.astral.sh/uv/) for dependency management. Deps are resolved automatically on first hook run.

---

## Table of Contents

- [Install](#install)
- [Quick start](#quick-start)
- [What it does](#what-it-does)
- [LATER.md format](#latermd-format)
- [Capture shortcut](#capture-shortcut)
- [Dispatch modes](#dispatch-modes)
- [Parallel agents and worktrees](#parallel-agents-and-worktrees)
- [Auto-resume](#auto-resume)
- [Nudge (stuck agent detection)](#nudge-stuck-agent-detection)
- [Context recovery (compact)](#context-recovery-compact)
- [Token analytics (stats)](#token-analytics-stats)
- [Configuration reference](#configuration-reference)
- [Status command](#status-command)
- [File layout](#file-layout)
- [Development and testing](#development-and-testing)

---

## Install

### Prerequisites

- [uv](https://docs.astral.sh/uv/) (dependency manager — `curl -LsSf https://astral.sh/uv/install.sh | sh`)

### Plugin install

```bash
claude plugin marketplace add vaddisrinivas/cc-later
claude plugin install cc-later
```

On first run, `~/.cc-later/config.env` is created from the bundled template. Dependencies (pydantic, filelock, pendulum) are resolved automatically by uv on the first hook invocation — no manual `pip install` needed.

### Companion plugin

[cc-retrospect](https://github.com/vaddisrinivas/cc-retrospect) — real-time cost monitoring, waste interception, and token analytics for Claude Code. Complements cc-later: retrospect monitors _what work costs now_, later handles _what work to do later_.

---

## Quick start

1. Install the plugin (above).
2. Set your plan in `~/.cc-later/config.env`:
   ```
   PLAN=max
   ```
3. During any session, add tasks to `.claude/LATER.md`:
   ```markdown
   # LATER

   ## Auth
   - [ ] (P0) fix token refresh in src/auth/service.py
   - [ ] (P1) add rate limiting to POST /api/refresh

   ## Tests
   - [ ] (P1) add integration test for retry path
   ```
4. Or use the capture shortcut in your prompt:
   ```
   later: add integration test for the retry path
   ```
5. Near window end, cc-later dispatches one agent per section in parallel.
6. Check status anytime: `/cc-later:status`
7. View token analytics: `uv run scripts/stats.py`

---

## What it does

| Capability | How it works |
|---|---|
| **Parallel dispatch** | Each `## Section` in LATER.md gets its own `claude -p` agent. All sections run simultaneously. |
| **Worktree isolation** | When `DISPATCH_ALLOW_FILE_WRITES=true`, each agent gets its own git worktree + branch. Merged back on completion. |
| **Window awareness** | Self-calibrating detection of the 5-hour usage window. Dispatches near window end to use otherwise-idle capacity. |
| **Auto-resume** | Tasks that fail due to rate limits are re-queued and dispatched in the next fresh window. |
| **Nudge** | Detects stuck agents (no output for N minutes) and restarts them. Detects crashed agents and re-queues. |
| **Budget gate** | Rolling 7-day token budget with configurable backoff threshold. |
| **Context recovery** | After `/compact`, re-injects LATER.md queue + window state into Claude's context. |
| **Capture** | `later: fix the auth bug` in any prompt auto-appends to LATER.md. |
| **Stats** | Per-model token analytics with API-equivalent cost breakdown. |

---

## LATER.md format

LATER.md lives at `.claude/LATER.md` in your repo root (configurable via `LATER_PATH`). It is a plain Markdown file:

```markdown
# LATER

## Auth
- [ ] (P1) fix token refresh in src/auth/service.py
- [ ] (P0) handle expired sessions in middleware

## Payments
- [ ] (P1) add retry logic to webhook handler
- [ ] (P2) clean up stripe client initialization

- [x] (P1) migrate database schema    <-- completed, marked by cc-later
```

### Section headers (`##`)

Each `##` heading defines a group of related tasks. When dispatch fires, **one background agent is spawned per section, all running in parallel**. The Auth agent and Payments agent above start simultaneously, each in its own git worktree and branch, so they cannot conflict.

Tasks that appear before the first `##` header are collected into a single unnamed agent.

### Task syntax

```
- [ ] (P0) <description>    <-- urgent: dispatched first within the section
- [ ] (P1) <description>    <-- normal priority (default)
- [ ] (P2) <description>    <-- nice-to-have
- [!] <description>         <-- shorthand for P0
- [x] <description>         <-- completed (written by cc-later, not by you)
```

Priority controls ordering within a section. P0 before P1, P1 before P2. Within the same priority, tasks run in file order. `LATER_MAX_ENTRIES_PER_DISPATCH` caps how many tasks from each section are selected per cycle.

---

## Capture shortcut

Add tasks to LATER.md directly from your prompt. The `UserPromptSubmit` hook watches for these patterns:

| Prompt pattern | Result |
|---|---|
| `later: fix the auth bug` | Appends as `(P1)` task |
| `add to later: update readme` | Appends as `(P1)` task |
| `note for later: clean up tests` | Appends as `(P1)` task |
| `queue for later: refactor parser` | Appends as `(P1)` task |
| `for later: add pagination` | Appends as `(P1)` task |
| `later[!]: SQL injection in filter` | Appends as `(P0)` urgent task |

Multiple captures in one prompt are all appended. Duplicates (case-insensitive substring match) are suppressed.

Captured tasks are appended without a section header. To place them in a specific section, write LATER.md directly.

---

## Dispatch modes

`WINDOW_DISPATCH_MODE` controls when the dispatch gate opens.

| Mode | Behavior | Best for |
|---|---|---|
| `window_aware` | Fires when the current usage window has <= `WINDOW_TRIGGER_AT_MINUTES_REMAINING` minutes remaining. Requires active JSONL session data. | Most users -- tasks run near window end, using otherwise-idle capacity |
| `time_based` | Fires only inside the time ranges in `WINDOW_FALLBACK_DISPATCH_HOURS` (local time). No window data required. | Teams with predictable off-hours or deterministic scheduling |
| `always` | Fires whenever the idle grace period passes. | Development, testing, or continuous dispatch |

### Gate sequence

All gates fire in order. The first failure skips the cycle.

1. **`DISPATCH_ENABLED`** -- master on/off switch
2. **Idle grace** -- at least `WINDOW_IDLE_GRACE_PERIOD_MINUTES` since the last hook run (prevents thrashing)
3. **Weekly budget** -- stop if `LIMITS_BACKOFF_AT_PCT`% of `LIMITS_WEEKLY_BUDGET_TOKENS` is consumed
4. **Mode gate** -- fires as described above

The auto-resume gate is evaluated separately and can trigger even when the mode gate is closed.

---

## Parallel agents and worktrees

When `DISPATCH_ALLOW_FILE_WRITES=true`, cc-later creates an isolated git worktree for each section agent before spawning it. This prevents agents from conflicting on the same files.

### Worktree lifecycle

```
dispatch:
  timestamp = YYYYMMDD-HHMMSS (same for all sections in one cycle)
  for each section:
    git worktree add ~/.cc-later/worktrees/{repo}-{slug}-{ts}
                     -b cc-later/{slug}-{ts}
    spawn agent with cwd = worktree path

reconcile (agent finishes):
  if branch has no new commits -> skip merge, clean up
  git -C {repo} merge --no-ff cc-later/{slug}-{ts}
                       -m "cc-later: {section} tasks"
  if merge OK  -> git worktree remove + git branch -d
  if conflict  -> git merge --abort
                 preserve worktree, mark tasks NEEDS_HUMAN
                 log merge_conflict event with conflicting files
```

When `DISPATCH_ALLOW_FILE_WRITES=false` (default), no worktrees are created -- agents run in the repo directory and are instructed not to modify files.

### Branch naming

`cc-later/{section-slug}-{YYYYMMDD-HHMMSS}`

Section names are slugified: non-alphanumeric characters replaced with `_`.

### Merge conflicts

If a merge conflict occurs during reconcile:
- The failed merge is aborted immediately -- the repo is left in a clean state
- The worktree is preserved at `~/.cc-later/worktrees/` for inspection
- All tasks from that agent are marked `NEEDS_HUMAN`
- A `merge_conflict` event is logged with branch name and conflicting file list

To resolve manually:

```bash
cd ~/.cc-later/worktrees/{repo}-{slug}-{ts}
git diff HEAD~1

cd /path/to/repo
git merge --no-ff cc-later/{slug}-{ts}
# resolve conflicts, then commit

git worktree remove ~/.cc-later/worktrees/{repo}-{slug}-{ts}
git branch -d cc-later/{slug}-{ts}
```

---

## Self-calibrating window detection

cc-later tracks usage window boundaries without relying on external APIs:

1. On each Stop hook: if `remaining <= 0`, record `window_limit_ts` in state.
2. Next activity after the limit (idle grace has passed): detect the window has reset, set `window_start_ts` to now. Clear `window_limit_ts`.
3. Auto-resume dispatch also sets `window_start_ts` (fresh window confirmed by successful dispatch).
4. First window uses clamp (`now - duration`) as a conservative estimate. Subsequent windows use the calibrated `window_start_ts` for accurate timing.

This means dispatch timing improves automatically after the first window cycle.

---

## Auto-resume

When a background agent's output contains rate or usage limit signals (`"rate limit"`, `"usage limit"`, `"quota"`, `"429"`, `"5-hour window"`, `"window exhausted"`, `"try again later"`), its failed tasks are saved to `resume_entries` in state.json.

On the next handler run, if `AUTO_RESUME_ENABLED=true` and (in `window_aware` mode) at least `AUTO_RESUME_MIN_REMAINING_MINUTES` remain, those tasks are re-dispatched as a single agent.

Resume dispatch happens before normal section dispatch. The auto-resume agent runs in its own worktree if file writes are enabled.

---

## Nudge (stuck agent detection)

When `NUDGE_ENABLED=true`, cc-later monitors dispatched agents for signs of being stuck:

- **Stale agents**: If a live agent's result file (or dispatch timestamp if no file yet) hasn't been modified for `NUDGE_STALE_MINUTES`, the agent is killed (`SIGTERM`) and re-dispatched with an incremented retry counter.
- **Dead agents**: If an agent's process has exited but produced no output file, it is re-queued for dispatch.
- **Max retries**: After `NUDGE_MAX_RETRIES` attempts, the agent is abandoned and logged as `agent_abandoned`.

Nudged agents get fresh worktrees (old ones are cleaned up) and the retry count is tracked per agent.

---

## Context recovery (compact)

When `COMPACT_ENABLED=true`, the SessionStart hook (with `compact` matcher) fires after `/compact` or auto-compaction. It injects into Claude's context:

- Current window state (remaining minutes, mode, elapsed time)
- All pending LATER.md tasks grouped by section
- In-flight dispatch status
- Auto-resume queue status

This ensures Claude retains awareness of the task queue after context compaction.

---

## Token analytics (stats)

```bash
uv run scripts/stats.py              # default: 7d and 30d
uv run scripts/stats.py 60           # custom single range
uv run scripts/stats.py 7 30 90      # multiple ranges
```

Output includes per-model breakdowns (input, cache creation, cache read, output tokens), API-equivalent cost at current pricing, session count, and a comparison against Max plan subscription cost.

Supported models: claude-opus-4-6, claude-opus-4-5, claude-sonnet-4-6, claude-sonnet-4-5, claude-haiku-4-5.

---

## Configuration reference

Config lives at `~/.cc-later/config.env` as plain `KEY=VALUE`. Comment lines start with `#`. Created automatically on first run from the bundled template.

### Plan

| Key | Type | Default | Description |
|---|---|---|---|
| `PLAN` | string | `max` | Your Claude plan. Sets window duration defaults. One of: `free`, `pro`, `max`, `team`, `enterprise`. |

### Paths

| Key | Type | Default | Description |
|---|---|---|---|
| `PATHS_WATCH` | comma-separated paths | _(empty)_ | Repos to watch. Empty = auto-detect from hook cwd. |

### LATER.md

| Key | Type | Default | Description |
|---|---|---|---|
| `LATER_PATH` | string | `.claude/LATER.md` | Path to LATER.md relative to repo root. |
| `LATER_MAX_ENTRIES_PER_DISPATCH` | integer | `3` | Max tasks selected per section per dispatch cycle. |
| `LATER_AUTO_GITIGNORE` | bool | `true` | Auto-add `LATER_PATH` to `.gitignore`. |

### Dispatch

| Key | Type | Default | Description |
|---|---|---|---|
| `DISPATCH_ENABLED` | bool | `true` | Master on/off switch. |
| `DISPATCH_MODEL` | string | `sonnet` | Model for background agents. One of: `sonnet`, `opus`, `haiku`. |
| `DISPATCH_ALLOW_FILE_WRITES` | bool | `false` | When `true`, agents may edit files directly (each in its own git worktree). When `false`, agents report findings only. |
| `DISPATCH_OUTPUT_PATH` | string | `~/.cc-later/results/{repo}-{date}.json` | Path template for agent result files. |

### Window

| Key | Type | Default | Description |
|---|---|---|---|
| `WINDOW_DISPATCH_MODE` | string | `window_aware` | One of: `window_aware`, `time_based`, `always`. |
| `WINDOW_DURATION_MINUTES` | integer | _(plan default)_ | Override window duration in minutes. Leave blank to use plan default (all plans: 300m). |
| `WINDOW_TRIGGER_AT_MINUTES_REMAINING` | integer | `30` | (`window_aware`) Dispatch when <= this many minutes remain. |
| `WINDOW_IDLE_GRACE_PERIOD_MINUTES` | integer | `10` | Minimum minutes between dispatch attempts. |
| `WINDOW_FALLBACK_DISPATCH_HOURS` | comma-separated ranges | _(empty)_ | (`time_based`) Local-time ranges as `HH:MM-HH:MM`. Overnight ranges supported. |
| `WINDOW_JSONL_PATHS` | comma-separated paths | _(empty)_ | Override JSONL paths for window/budget detection. |

### Limits

| Key | Type | Default | Description |
|---|---|---|---|
| `LIMITS_WEEKLY_BUDGET_TOKENS` | integer | `10000000` | Rolling 7-day token budget across all repos. |
| `LIMITS_BACKOFF_AT_PCT` | integer | `80` | Pause dispatch at this % of weekly budget consumed. |

### Auto-resume

| Key | Type | Default | Description |
|---|---|---|---|
| `AUTO_RESUME_ENABLED` | bool | `true` | Re-dispatch limit-failed tasks in the next fresh window. |
| `AUTO_RESUME_MIN_REMAINING_MINUTES` | integer | `240` | (`window_aware`) Only auto-resume when >= this many minutes remain. |

### Compact

| Key | Type | Default | Description |
|---|---|---|---|
| `COMPACT_ENABLED` | bool | `true` | Inject LATER.md queue into Claude's context after `/compact` or auto-compaction. |

### Nudge

| Key | Type | Default | Description |
|---|---|---|---|
| `NUDGE_ENABLED` | bool | `true` | Detect and restart stuck/dead dispatched agents. |
| `NUDGE_STALE_MINUTES` | integer | `10` | Minutes of no output before an agent is considered stuck. |
| `NUDGE_MAX_RETRIES` | integer | `2` | Maximum re-dispatch attempts per agent before abandoning. |

---

## Status command

```bash
/cc-later:status
```

Example output:

```
## cc-later Status

### Window
  Mode: window_aware
  Elapsed/Remaining: 142m / 158m
  Tokens: 84,201 in / 12,450 out
  Window ends: 2026-04-06 14:22 PDT
  Next window: starts on first Claude request after 14:22 PDT
  Weekly budget: 1,240,000 / 10,000,000 (12.4%)
  Backoff at: 80% (8,000,000 tokens)

### Queue
  myrepo/ [in-flight]
    pending: 4
    agent [Auth]: pid=84312  branch=cc-later/Auth-20260406-100000
    agent [Payments]: pid=84313  branch=cc-later/Payments-20260406-100000

### Gates
  dispatch.enabled: pass
  mode gate: FAIL
  auto-resume gate: closed
  budget gate: pass

### Recent Runs
  04-06 10:18 dispatch
  04-06 09:55 skip             idle_grace_active
  04-06 09:32 dispatch
  04-05 23:41 skip             mode_gate_closed
```

---

## File layout

```
~/.cc-later/
  config.env                              <-- your configuration
  state.json                              <-- in-flight agent and resume queue tracking
  run_log.jsonl                           <-- append-only event log
  results/
    myrepo-Auth-20260406-100000.json      <-- per-agent structured output
    myrepo-Payments-20260406-100000.json
  worktrees/                              <-- only when DISPATCH_ALLOW_FILE_WRITES=true
    myrepo-Auth-20260406-100000/          <-- isolated worktree, removed on clean merge
    myrepo-Payments-20260406-100000/      <-- preserved on conflict for manual resolution

<repo-root>/
  .claude/
    LATER.md                    <-- task queue (auto-added to .gitignore)
```

Plugin source:

```
cc_later/
  __init__.py
  core.py                       <-- all logic (pydantic models, filelock, pendulum)

scripts/
  handler.py                    <-- Stop hook -> core.run_handler()
  capture.py                    <-- UserPromptSubmit hook -> core.capture_from_payload()
  compact.py                    <-- SessionStart/compact hook -> core.run_compact_inject()
  status.py                     <-- /cc-later:status -> core.run_status()
  stats.py                      <-- Token analytics -> core.run_stats()
  default_config.env            <-- config template (copied on first run)

hooks/hooks.json                <-- Hook definitions (all use uv run --project)
commands/status.md
skills/later/SKILL.md
.claude-plugin/plugin.json
.claude-plugin/marketplace.json
pyproject.toml                  <-- uv project config + dependencies
uv.lock                        <-- lockfile
```

---

## Development and testing

Requires [uv](https://docs.astral.sh/uv/).

```bash
# Install deps (including test group)
uv sync --group test

# Run all 513 tests
uv run pytest tests/ -v

# Smoke-test the handler without spawning real agents
echo '{}' | uv run scripts/handler.py

# Check status
uv run scripts/status.py

# Token analytics (7d and 30d)
uv run scripts/stats.py
```

### Dependencies

| Package | Purpose |
|---|---|
| [pydantic](https://docs.pydantic.dev/) | Config models with declarative validation (`Literal`, `Field(gt=0)`) |
| [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Env file loading support |
| [filelock](https://py-filelock.readthedocs.io/) | Cross-platform file locking (macOS, Linux, Windows, NFS) |
| [pendulum](https://pendulum.eustace.io/) | Timezone-aware datetime parsing and arithmetic |

### Test suite

513 tests across 10 modules:

| File | What it tests |
|---|---|
| `test_config_and_format.py` | Config loading, task/section parsing, priority ordering, mark-done |
| `test_handler_status_capture.py` | Full capture -> dispatch -> reconcile -> status flow |
| `test_handler_worktree_state.py` | Worktree creation, merge, cleanup, state management |
| `test_reconcile_resume.py` | Limit-fail detection, resume scheduling, done-marking |
| `test_reconcile_nudge.py` | Stale agent detection, dead agent re-queue, retry limits |
| `test_window_budget.py` | JSONL window calculation, stale row filtering, weekly budget |
| `test_window_gates_budget.py` | Window gate logic, budget gate, dispatch mode gates |
| `test_stats_compact_tasks.py` | Stats output, compact injection, task parsing edge cases |
| `test_utils_and_config.py` | Pydantic validation, utility functions, plan defaults |
| `test_plugin_layout.py` | Plugin manifest validity, hook config, command presence |
