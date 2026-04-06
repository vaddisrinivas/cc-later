# cc-later

A Claude Code plugin that automatically dispatches follow-up tasks as background agents near the end of each Claude usage window.

During a session, tasks accumulate in `.claude/LATER.md`. When a usage window is about to expire, cc-later spawns one parallel `claude -p` agent per `##` section in that file — each in its own git worktree and branch to prevent conflicts. When agents finish, their branches are merged back. If an agent hits a rate or usage limit, its unfinished tasks are queued and replayed automatically in the next fresh window.

No external Python dependencies. Runs entirely on the standard library.

---

## Table of Contents

- [How it works](#how-it-works)
- [Install](#install)
- [LATER.md format](#latermd-format)
- [Capture shortcut](#capture-shortcut)
- [Dispatch modes](#dispatch-modes)
- [Parallel agents and worktrees](#parallel-agents-and-worktrees)
- [Auto-resume](#auto-resume)
- [Configuration reference](#configuration-reference)
- [Status command](#status-command)
- [File layout](#file-layout)
- [Development and testing](#development-and-testing)

---

## How it works

1. You (or Claude) write tasks into `.claude/LATER.md`, grouped under `##` section headers.
2. Every time a Claude session ends, the `Stop` hook fires `scripts/handler.py`.
3. The handler checks a gate sequence (enabled, idle grace, budget, dispatch mode). If all pass, it spawns one background `claude -p` subprocess per section — all in parallel. When file writes are enabled, each agent runs in its own isolated git worktree on a dedicated branch.
4. Each agent works through its tasks and writes a structured result file.
5. On the next hook invocation, completed tasks are marked `[x]` in LATER.md, worktrees are merged back into the main branch and cleaned up, and any limit-failed tasks are re-queued for the next window.

---

## Install

```bash
claude plugin marketplace add vaddisrinivas/cc-later
claude plugin install cc-later
```

On first run, `~/.cc-later/config.env` is created from the bundled template. Edit it to configure cc-later for your workflow.

---

## LATER.md format

LATER.md lives at `.claude/LATER.md` in your repo root (configurable). It is a plain Markdown file:

```markdown
# LATER

## Auth
- [ ] (P1) fix token refresh in src/auth/service.py
- [ ] (P0) handle expired sessions in middleware

## Payments
- [ ] (P1) add retry logic to webhook handler
- [ ] (P2) clean up stripe client initialization

- [x] (P1) migrate database schema    ← completed, marked by cc-later
```

### Section headers (`##`)

Each `##` heading defines a group of related tasks. When dispatch fires, **one background agent is spawned per section, all running in parallel**. The Auth agent and Payments agent above start simultaneously, each in its own git worktree and branch, so they cannot conflict.

Tasks that appear before the first `##` header are collected into a single unnamed agent.

### Task syntax

```
- [ ] (P0) <description>    ← urgent: dispatched first within the section
- [ ] (P1) <description>    ← normal priority (default)
- [ ] (P2) <description>    ← nice-to-have
- [!] <description>         ← shorthand for P0
- [x] <description>         ← completed (written by cc-later, not by you)
```

Priority controls ordering within a section. P0 before P1, P1 before P2. Within the same priority, tasks run in file order. `LATER_MAX_ENTRIES_PER_DISPATCH` caps how many tasks from each section are selected per cycle.

### Full annotated example

```markdown
# LATER

## Security
- [ ] (P0) SQL injection in report filter — src/reports/filter.py:42
- [ ] (P1) Add CSRF token validation to /api/upload endpoint

## Performance
- [ ] (P1) Replace N+1 query in ReportGenerator.fetch_reports (src/reports/service.py)
- [ ] (P2) Cache user profile lookups in src/users/views.py

## Docs
- [ ] (P2) Update CLI flags in README to match src/cli.py argument parser
- [x] (P1) Fix broken link in CONTRIBUTING.md
```

Two agents run in parallel: one for Security, one for Performance (and one for Docs if tasks remain). Each works on a separate branch and cannot overwrite the other's changes.

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

Example:

```
That looks good. later: add integration test for the retry path
later[!]: fix the exposed debug endpoint before deploying
```

Capture tasks are appended without a section header. To place them in a specific section, write LATER.md directly.

---

## Dispatch modes

`WINDOW_DISPATCH_MODE` controls when the dispatch gate opens.

| Mode | Behavior | Best for |
|---|---|---|
| `window_aware` | Fires when the current 5-hour Claude usage window has ≤ `WINDOW_TRIGGER_AT_MINUTES_REMAINING` minutes remaining. Requires active JSONL session data. | Most users — tasks run near window end, using otherwise-idle capacity |
| `time_based` | Fires only inside the time ranges in `WINDOW_FALLBACK_DISPATCH_HOURS` (local time). No window data required. | Teams with predictable off-hours or deterministic scheduling |
| `always` | Fires whenever the idle grace period passes. | Development, testing, or continuous dispatch |

### Gate sequence

All gates fire in order. The first failure skips the cycle.

1. **`DISPATCH_ENABLED`** — master on/off switch
2. **Idle grace** — at least `WINDOW_IDLE_GRACE_PERIOD_MINUTES` since the last hook run (prevents thrashing)
3. **Weekly budget** — stop if `LIMITS_BACKOFF_AT_PCT`% of `LIMITS_WEEKLY_BUDGET_TOKENS` is consumed
4. **Mode gate** — fires as described above

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
  if branch has no new commits → skip merge, clean up
  git -C {repo} merge --no-ff cc-later/{slug}-{ts}
                       -m "cc-later: {section} tasks"
  if merge OK  → git worktree remove + git branch -d
  if conflict  → git merge --abort
                 preserve worktree, mark tasks NEEDS_HUMAN
                 log merge_conflict event with conflicting files
```

Each agent commits its changes to its own branch. On reconcile, branches are merged back one at a time in completion order. If two sections edited the same file and the second merge conflicts, the merge is aborted (repo left clean), the worktree is preserved for manual resolution, and the affected tasks are marked `NEEDS_HUMAN`.

When `DISPATCH_ALLOW_FILE_WRITES=false` (default), no worktrees are created — agents run in the repo directory and are instructed not to modify files.

### Branch naming

`cc-later/{section-slug}-{YYYYMMDD-HHMMSS}`

Section names are slugified: non-alphanumeric characters replaced with `_`. All sections in a single dispatch cycle share the same timestamp suffix.

| Section name | Branch |
|---|---|
| `Auth` | `cc-later/Auth-20260406-100000` |
| `Auth & Tokens` | `cc-later/Auth___Tokens-20260406-100000` |
| _(no header)_ | `cc-later/default-20260406-100000` |
| resume | `cc-later/resume-20260406-100000` |

### Merge conflicts

If a merge conflict occurs during reconcile:
- The failed merge is aborted immediately — the repo is left in a clean state
- The worktree is **preserved** at `~/.cc-later/worktrees/` for inspection
- All tasks from that agent are marked `NEEDS_HUMAN`
- A `merge_conflict` event is logged with branch name and conflicting file list
- cc-later prints the worktree path to stdout

To resolve manually:

```bash
# Inspect what the agent changed
cd ~/.cc-later/worktrees/{repo}-{slug}-{ts}
git diff HEAD~1

# Manually merge into your repo
cd /path/to/repo
git merge --no-ff cc-later/{slug}-{ts}
# resolve conflicts, then commit

# Clean up
git worktree remove ~/.cc-later/worktrees/{repo}-{slug}-{ts}
git branch -d cc-later/{slug}-{ts}
```

---

## Auto-resume

When a background agent's output contains rate or usage limit signals (`"rate limit"`, `"usage limit"`, `"quota"`, `"429"`, `"5-hour window"`, `"window exhausted"`, `"try again later"`), its failed tasks are saved to `resume_entries` in state.json.

On the next handler run, if `AUTO_RESUME_ENABLED=true` and (in `window_aware` mode) at least `AUTO_RESUME_MIN_REMAINING_MINUTES` remain, those tasks are re-dispatched as a single agent.

Resume dispatch happens before normal section dispatch. The auto-resume agent runs in its own worktree if file writes are enabled.

---

## Configuration reference

Config lives at `~/.cc-later/config.env` as plain `KEY=VALUE`. Comment lines start with `#`. Created automatically on first run.

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
| `DISPATCH_OUTPUT_PATH` | string | `~/.cc-later/results/{repo}-{date}.json` | Path template for agent result files. `{repo}` includes section slug when applicable. |

### Window

| Key | Type | Default | Description |
|---|---|---|---|
| `WINDOW_DISPATCH_MODE` | string | `window_aware` | One of: `window_aware`, `time_based`, `always`. |
| `WINDOW_TRIGGER_AT_MINUTES_REMAINING` | integer | `30` | (`window_aware`) Dispatch when ≤ this many minutes remain. |
| `WINDOW_IDLE_GRACE_PERIOD_MINUTES` | integer | `10` | Minimum minutes between dispatch attempts. |
| `WINDOW_FALLBACK_DISPATCH_HOURS` | comma-separated ranges | _(empty)_ | (`time_based`) Local-time ranges as `HH:MM-HH:MM`. Overnight ranges supported. Example: `22:00-06:00`. |
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
| `AUTO_RESUME_MIN_REMAINING_MINUTES` | integer | `240` | (`window_aware`) Only auto-resume when ≥ this many minutes remain. |

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
  config.env                              ← your configuration
  state.json                              ← in-flight agent and resume queue tracking
  run_log.jsonl                           ← append-only event log
  results/
    myrepo-Auth-20260406-100000.json      ← per-agent structured output
    myrepo-Payments-20260406-100000.json
  worktrees/                              ← only when DISPATCH_ALLOW_FILE_WRITES=true
    myrepo-Auth-20260406-100000/          ← isolated worktree, removed on clean merge
    myrepo-Payments-20260406-100000/      ← preserved on conflict for manual resolution

<repo-root>/
  .claude/
    LATER.md                    ← task queue (auto-added to .gitignore)
```

Plugin source:

```
cc_later/
  __init__.py
  core.py                       ← all logic: config, state, parsing, dispatch, worktrees, status, capture

scripts/
  handler.py                    ← Stop hook → core.run_handler()
  capture.py                    ← UserPromptSubmit hook → core.capture_from_payload()
  status.py                     ← /cc-later:status → core.run_status()
  default_config.env            ← config template (copied on first run)

hooks/hooks.json
commands/status.md
skills/later/SKILL.md
.claude-plugin/plugin.json
.claude-plugin/marketplace.json
```

---

## Development and testing

No external packages required.

```bash
# Run all tests
python3 -m pytest tests/ -v

# Smoke-test the handler without spawning real agents
echo '{}' | python3 scripts/handler.py

# Check status
python3 scripts/status.py
```

Test files:

| File | What it tests |
|---|---|
| `test_config_and_format.py` | Config loading, task/section parsing, priority ordering, mark-done |
| `test_handler_status_capture.py` | Full capture → dispatch → reconcile → status flow |
| `test_reconcile_resume.py` | Limit-fail detection, resume scheduling, done-marking |
| `test_window_budget.py` | JSONL window calculation, stale row filtering, weekly budget |
| `test_plugin_layout.py` | Plugin manifest validity, hook config, command presence |

To test dispatch without spawning real agents:

```python
from unittest.mock import patch
with patch("cc_later.core._spawn_dispatch", return_value=12345):
    core.run_handler(json.dumps({"cwd": str(repo)}))
```

To isolate tests from `~/.cc-later`:

```python
import os
from unittest.mock import patch
with patch.dict(os.environ, {"CC_LATER_APP_DIR": "/tmp/test-cc-later"}):
    cfg = core.load_config()
```
