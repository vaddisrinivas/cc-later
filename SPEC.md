# cc-later v0.1.0 Specification and Implementation Plan

## 1. Purpose
`cc-later` is a Claude Code plugin that drains `.claude/LATER.md` when a session window is near expiry (or in configured fallback modes), launching background `claude -p` runs using local auth already managed by Claude Code.

## 2. Architecture Principles
1. Thin plugin surface, deterministic core logic.
2. Test-first core: parsing, gating, idempotency, and completion marking are pure functions.
3. Safe-by-default: dispatch disabled until explicit opt-in.
4. Idempotent dispatch: never duplicate background runs for the same repo while one is in flight.
5. Deterministic completion matching: each dispatched task has a stable ID, not raw text matching only.
6. Strict scope boundaries: only configured watched paths and `~/.cc-later` are writable.
7. Graceful degradation: window detection failures never crash hooks.

## 3. Key Invariants (Gap Tightening)
### 3.1 Idempotency and race control
- Hook invocations acquire a non-blocking global lock file (`~/.cc-later/handler.lock`).
- If lock is held, current invocation exits cleanly with a `skip` run-log entry.
- Per-repo state tracks `in_flight` dispatch metadata; repo dispatch is skipped when already in flight.

### 3.2 Stable task identity
- When selecting LATER entries, handler assigns deterministic short IDs (`t_<hash>`).
- Prompt includes task IDs and requires summary output keyed by ID.
- Completion marking maps `DONE <id>:` back to dispatched metadata, then updates LATER deterministically.

### 3.3 Write safety
- Background subprocess runs with `cwd=<repo>`.
- If writes enabled, prompt enforces max file count and repo-local scope.
- Handler records changed-mode intent but does not grant broader filesystem scope itself.

### 3.4 Failure/backoff behavior
- Config/JSONL/parse errors produce `error` or `skip` log events and exit `0`.
- Optional notification on error is controlled by config.
- Repeated noisy dispatch is prevented by `idle_grace_period_minutes` and in-flight state checks.

## 4. File-by-File Plan

### Root
- `README.md`: user install/setup docs (concise, <=100 lines target).
- `SPEC.md`: this architecture and implementation plan.
- `.claude-plugin/marketplace.json`: self-hosted marketplace catalog.

### Plugin metadata
- `.claude-plugin/plugin.json`: plugin manifest metadata.

### Hook wiring
- `hooks/hooks.json`: `Stop` hook command to run `scripts/handler.py` via `python3`.

### Skill
- `skills/later/SKILL.md`: conventions for writing to `.claude/LATER.md`.

### Slash command
- `commands/status.md`: instructions to inspect window, gates, queue, and last runs.

### Runtime scripts
- `scripts/default_config.toml`: complete defaults with comments.
- `scripts/handler.py`: single-file runtime with stdlib only.
- `scripts/__init__.py`: allows importing `scripts.handler` in tests.

### Tests
- `tests/test_config.py`: config schema validation, unknown-key rejection.
- `tests/test_later_entries.py`: LATER parsing, priority ordering, deterministic IDs.
- `tests/test_window_modes.py`: `window_aware`, `time_based`, `always` gate behavior.
- `tests/test_completion.py`: result parsing and deterministic LATER completion marking.
- `tests/test_locking.py`: lock semantics (second acquire fails while held).

## 5. Handler Internal Design (`scripts/handler.py`)

## 5.1 Data model
- `WindowConfig`, `PathsConfig`, `LaterConfig`, `DispatchConfig`, `NotificationConfig`, `AppConfig`
- `LaterEntry(id, text, marker, line_index)`
- `DispatchRecord(repo, entries, model, result_path, pid, ts)`
- `RepoState(in_flight, dispatch_record)`
- `AppState(last_hook_ts, repos)`

## 5.2 Core pure functions (unit tested)
- `validate_config_dict(raw: dict) -> AppConfig`
- `parse_later_entries(content: str, priority_marker: str) -> list[LaterEntry]`
- `select_entries(entries, max_entries) -> list[LaterEntry]`
- `render_prompt(...) -> str`
- `parse_result_summary(text: str) -> dict[id, status]`
- `apply_completion(content, done_entry_ids, dispatched_entries, mark_mode) -> str`
- `is_within_time_ranges(now_local, ranges) -> bool`
- `compute_window_state(jsonl_roots, now_utc) -> WindowState | None`

## 5.3 Side-effect adapters (thin wrappers)
- FS adapter: read/write config/state/log/results/LATER.
- Process adapter: spawn detached `claude -p`.
- Notification adapter: best-effort desktop notifications.
- Hook adapter: read stdin payload safely.

## 5.4 Execution pipeline
1. Acquire global lock.
2. Load/copy config, validate strict schema.
3. Load state, run completion reconciliation for in-flight repos.
4. Evaluate global gates (`dispatch.enabled`, `watch`, idle, peak/mode).
5. For each watched repo: load LATER, collect entries, skip if none/in-flight.
6. Dispatch selected entries; record state + run log.
7. Persist updated state and append run log events.
8. Exit quickly with one-line status.

## 6. Test-First Implementation Order
1. Add failing tests for config schema, LATER parser, window mode gates, completion logic, lock semantics.
2. Implement pure functions until tests pass.
3. Implement side-effect wrappers and `main()` glue.
4. Add integration-like tests for dispatch command construction (without actually running `claude`).
5. Wire plugin manifests/hooks/skill/command/docs.

## 7. Acceptance Criteria for v0.1.0
1. `python3 -m unittest discover -s tests -v` passes.
2. First run copies config to `~/.cc-later/config.toml` and exits cleanly.
3. Unknown config keys fail with explicit error and no dispatch.
4. `dispatch_mode` values behave as documented with graceful fallback.
5. Duplicate Stop events do not create duplicate dispatches while in-flight.
6. Completed results mark the intended LATER entries deterministically.
7. Hook command and plugin install metadata are valid JSON and path-correct.
