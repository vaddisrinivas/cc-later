# cc-later

**Your Claude Code sessions have a 5-hour window. Use every minute of it.**

cc-later is a Claude Code plugin that dispatches `.claude/LATER.md` tasks as background `claude -p` jobs during idle session time. Zero extra tokens — it spends capacity that would otherwise expire.

```
You end a session with 30 min left on the clock
    --> cc-later picks up your queued maintenance tasks
    --> Runs them as headless background agents
    --> Marks them done, retries failures, generates reports
    --> Next session: your queue is lighter
```

## Install

```bash
/plugin marketplace add vaddisrinivas/cc-later
/plugin install cc-later@cc-later
```

Then enable:

```bash
# Initialize your repo
python3 ~/.claude/plugins/cache/cc-later/cc-later/0.3.0/cc_later/cli.py init

# Edit config
vi ~/.cc-later/config.toml
```

```toml
[paths]
watch = ["~/projects/my-repo"]

[dispatch]
enabled = true
```

## Quick Start

**1. Queue tasks** during any Claude session:

```
later: fix the N+1 query in ReportGenerator
later[!]: SQL injection risk in filter builder
add to later: update README install steps
```

**2. Tasks dispatch automatically** when your session window is near expiry.

**3. Check status** any time:

```
/cc-later:status
```

**4. View analytics:**

```
/cc-later:stats
```

## What's New in v0.3.0

| Feature | What it does |
|---------|-------------|
| **Modular architecture** | Clean package structure (`cc_later/`) — config, parser, dispatcher, analytics, verify as separate modules |
| **Smart retry** | Failed tasks retry with exponential backoff (30m, 2h, 8h). After 3 attempts → `[?]` needs human |
| **Adaptive model routing** | `model_routing = "auto"` routes simple tasks to haiku, complex ones to opus |
| **Completion verification** | Scores result quality before marking DONE. Weak results get flagged |
| **SQLite analytics** | Track success rates, token usage, model efficiency. View with `/cc-later:stats` |
| **Rich reports** | Each dispatch generates `.claude/reports/later-{date}.md` with full results |
| **Task dependencies** | Chain tasks: `- [ ] Fix X (after: t_abc123)` — won't run until dependency is done |
| **Webhook notifications** | POST JSON to Slack/Discord on dispatch, complete, error events |
| **CLI tool** | `cc-later status`, `stats`, `inspect`, `dispatch`, `init`, `queue`, `dry-run` |
| **Auto-section routing** | Captured tasks auto-sorted into Security/Bugs/Tests/Docs/Refactor/Reports sections |

## LATER.md Format

```markdown
# LATER

## Security
- [!] Fix SQL injection in ReportFilter.build_query() (src/reports/filter.py)

## Tests
- [ ] Add integration tests for auth flow (tests/test_auth.py)
  <!-- cc-later: attempts=1, last=2026-04-01T12:00:00Z -->
- [ ] Add edge case tests for pagination (after: t_abc123)

## Docs
- [ ] Update README to match current CLI flags
- [?] Document the new webhook config — needs human decision on format
```

| Marker | Meaning |
|--------|---------|
| `- [ ]` | Pending — will be dispatched |
| `- [!]` | Priority — dispatched first, routed to stronger model |
| `- [x]` | Completed — marked by handler |
| `- [?]` | Needs human — failed after max retries |

## Key Phrase Capture

Type these during a session to auto-queue:

| Phrase | Example |
|--------|---------|
| `later:` | `later: fix the N+1 query in reports` |
| `later[!]:` | `later[!]: SQL injection in filter builder` |
| `add to later:` | `add to later: update README install steps` |
| `note for later:` | `note for later: UserService.delete() swallows exceptions` |
| `queue for later:` | `queue for later: add rate limiting to /refresh` |
| `for later:` | `for later: check the migration script` |

Tasks are auto-sorted into the matching section (Security, Bugs, Tests, etc.).

## Dispatch Modes

| Mode | Behavior |
|------|----------|
| `window_aware` (default) | Dispatches when <=30 min remain in 5-hr session |
| `time_based` | Dispatches during configured hours (e.g. `["22:00-02:00"]`) |
| `always` | Dispatches whenever idle gate is open |

## Model Routing

Set `model_routing = "auto"` to route tasks by complexity:

| Complexity | Model | Examples |
|-----------|-------|---------|
| 1-2 (simple) | haiku | `Check import`, `Remove dead code` |
| 3 (medium) | sonnet | `Fix bug in auth.py`, `Add type hints` |
| 4-5 (complex) | opus | `Audit auth flow`, `Refactor + multi-file` |

Complexity is scored by: verb weight, file count, section (Security/Bugs = higher), description length, priority flag.

## Configuration

All settings in `~/.cc-later/config.toml`:

```toml
[dispatch]
enabled = true
model = "sonnet"               # default model
model_routing = "fixed"        # "fixed" | "auto"
allow_file_writes = false      # read-only by default

[retry]
enabled = true
max_attempts = 3
backoff_minutes = [30, 120, 480]
escalate_to_priority = true    # mark [?] after max attempts

[verify]
enabled = true
min_confidence = "low"         # "low" | "medium" | "high"

[notifications]
desktop = false
webhook_url = ""               # Slack/Discord incoming webhook
webhook_events = ["dispatch", "complete", "error"]

[budget]
weekly_token_budget = 10_000_000
backoff_at_pct = 80            # stop at 80% of weekly budget
```

Full config reference: `scripts/default_config.toml`

## CLI

```bash
cc-later status       # Window, gates, queue, recent runs, analytics summary
cc-later stats        # Full analytics dashboard
cc-later inspect      # Inspect recent dispatch results
cc-later dry-run      # See what would dispatch
cc-later init [path]  # Initialize a repo
cc-later queue [path] # Show pending queue with complexity scores
cc-later dispatch     # Force a dispatch cycle
cc-later import-log   # Backfill analytics from run_log.jsonl
```

## Architecture

```
cc_later/               # Core package
  models.py             # All dataclasses (config, state, entries)
  config.py             # Config loading + validation
  parser.py             # LATER.md parsing, completion, retry, rotation
  dispatcher.py         # Main handler loop + reconciliation
  window.py             # Window state, budget, time utilities
  analytics.py          # SQLite analytics engine
  verify.py             # Completion verification pipeline
  reporter.py           # Rich report generation
  prompt.py             # Dispatch prompt rendering
  notify.py             # Desktop + webhook notifications
  lock.py               # Non-blocking file lock
  cli.py                # CLI entry point
  compat.py             # Python 3.9+ compatibility

scripts/                # Hook entry points (thin shims)
  handler.py            # Stop hook → cc_later.dispatcher
  capture.py            # UserPromptSubmit hook
  status.py             # /cc-later:status command
  probe.py              # Cron-based window probe

tests/                  # 150 tests across 19 modules
```

## Troubleshooting

**Tasks never dispatch** — run `cc-later dry-run` to see which gate is blocking.

**Capture doesn't fire** — verify the phrase includes a colon (`later: fix this`, not `fix this later`).

**Results not marking LATER.md** — check `~/.cc-later/results/` for output files and `~/.cc-later/run_log.jsonl` for events.

**Verification too strict** — lower `verify.min_confidence` to `"low"` or disable with `verify.enabled = false`.

**Retries not working** — check retry metadata comments in LATER.md. Tasks at max attempts show `[?]`.

## Development

```bash
# Run all tests (150 tests)
python3 -m unittest discover -s tests -v

# Dry-run gate check
python3 scripts/handler.py --dry-run

# Status dashboard
python3 scripts/status.py

# CLI
python3 cc_later/cli.py status
python3 cc_later/cli.py stats
python3 cc_later/cli.py inspect
```
