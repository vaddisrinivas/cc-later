# cc-later v0.3.0 Architecture

## Purpose

cc-later is a Claude Code plugin that drains `.claude/LATER.md` when a session window is near expiry, launching background `claude -p` runs using local auth. v0.3.0 adds retry intelligence, analytics, verification, adaptive model routing, and a proper package architecture.

## Architecture Principles

1. **Modular package**: Core logic in `cc_later/`, hook shims in `scripts/`.
2. **Test-first core**: All parsing, gating, routing, retry, and verification are pure functions.
3. **Safe-by-default**: Dispatch disabled until explicit opt-in. Read-only by default.
4. **Idempotent dispatch**: Non-blocking lock + in-flight state prevent duplicates.
5. **Deterministic matching**: Stable task IDs based on line index + text hash.
6. **Smart retry**: Failed tasks get exponential backoff, then escalate to needs-human.
7. **Verification gate**: Result quality scored before marking DONE.
8. **Adaptive routing**: Task complexity drives model selection.
9. **Observable**: SQLite analytics, rich reports, webhook notifications.

## Module Map

### `cc_later/models.py`
All dataclasses: `AppConfig`, `WindowState`, `BudgetState`, `LaterEntry`, `RepoState`, `AppState`, plus config sub-models (`WindowConfig`, `RetryConfig`, `VerifyConfig`, etc.).

### `cc_later/config.py`
Strict schema validation with unknown-key rejection. Loads from `~/.cc-later/config.toml` with TOML parsing via compat shim (supports Python 3.9+).

### `cc_later/parser.py`
Pure functions for LATER.md:
- `parse_later_entries()` — section tracking, retry metadata, dependency parsing
- `select_entries()` — priority ordering + dependency filtering
- `apply_completion()` — mark DONE entries as [x] or delete
- `apply_retry_metadata()` — update attempt counts, escalate at max retries
- `rotate_later_if_needed()` — daily archiving with pending extraction
- `estimate_complexity()` — score 1-5 for model routing
- `route_model()` — pick model based on complexity

### `cc_later/dispatcher.py`
Main handler loop:
1. Acquire lock
2. Load config/state
3. Reconcile in-flight dispatches (verify, mark, retry, report, analytics)
4. Evaluate gate sequence (enabled → watch → idle → peak → budget → mode)
5. For each repo: parse → filter retries → select → route model → render prompt → spawn
6. Record analytics, save state

### `cc_later/window.py`
Window state computation from JSONL files, budget tracking, time range utilities, peak window detection.

### `cc_later/analytics.py`
SQLite engine (`~/.cc-later/analytics.db`):
- `record_dispatch()` / `record_outcome()` — event recording
- `get_stats()` — aggregate metrics (success rate, by-model, by-repo, by-section, streak)
- `import_from_run_log()` — one-time backfill from JSONL

### `cc_later/verify.py`
Post-dispatch verification:
- Score result confidence: high/medium/low/none
- Work signals (modified, fixed, found) vs punt signals (cannot, unable)
- Task-specific term matching
- Git diff check when writes enabled
- Downgrade DONE → NEEDS_HUMAN if below threshold

### `cc_later/reporter.py`
Report generation:
- Per-dispatch markdown report → `.claude/reports/later-{date}.md`
- Analytics dashboard rendering for `/cc-later:stats`

### `cc_later/prompt.py`
Dispatch prompt rendering:
- Per-task instruction blocks with contextual hints
- Verb-based strategy hints (audit → "read thoroughly", fix → "locate first")
- Section-based hints (security → "high priority", tests → "follow patterns")
- FAILED status in output format (not just DONE/SKIPPED/NEEDS_HUMAN)

### `cc_later/notify.py`
Desktop notifications (macOS/Linux) + webhook POST (Slack/Discord/custom).

### `cc_later/cli.py`
Subcommands: status, stats, inspect, dispatch, dry-run, init, queue, import-log.

## Dispatch Pipeline

```
Stop hook → handler.py
    → cc_later.dispatcher.main()
        → Acquire lock
        → Load config + state
        → Reconcile in-flight:
            ├── Check process alive
            ├── Parse result summary
            ├── Verify result quality
            │   ├── Score confidence
            │   └── Downgrade if below threshold
            ├── Mark completed in LATER.md
            ├── Update retry metadata for failures
            ├── Generate dispatch report
            └── Record analytics outcomes
        → Gate sequence:
            ├── dispatch.enabled
            ├── paths.watch non-empty
            ├── idle grace period
            ├── not in peak window
            ├── budget under threshold
            └── mode gate (window_aware / time_based / always)
        → For each repo:
            ├── Rotate LATER.md if new day
            ├── Parse entries + retry metadata
            ├── Filter by retry eligibility (backoff timing)
            ├── Filter by dependency completion
            ├── Select top N by priority
            ├── Route model (fixed or auto by complexity)
            ├── Render prompt with task hints
            ├── Spawn detached claude -p
            └── Record dispatch in analytics + state
        → Save state + release lock
```

## Data Flow

```
User types "later: fix bug"
    → UserPromptSubmit hook → capture.py
    → Auto-detect section → Insert under ## Bugs
    → Write to .claude/LATER.md

Session ends
    → Stop hook → handler.py → dispatcher.main()
    → Select entries, route model, spawn claude -p

Background agent completes
    → Result written to ~/.cc-later/results/

Next Stop hook
    → Reconcile: verify → mark → retry → report → analytics
```

## Test Coverage

150 tests across 19 modules covering:
- Config validation + schema rejection
- LATER parsing, priority, sections, retry metadata, dependencies
- Window state computation, budget tracking
- Completion marking (check/delete modes)
- Retry logic (backoff, escalation, metadata)
- Model routing (complexity scoring, auto vs fixed)
- Verification pipeline (confidence scoring, thresholds)
- Report generation
- Analytics (SQLite queries, stats aggregation)
- Lock semantics, dry-run, status output
- Capture hook regex + integration
- Rotation with metadata preservation
