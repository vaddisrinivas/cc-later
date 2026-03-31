# cc-later

[![Tests](https://github.com/vaddisrinivas/cc-later/actions/workflows/test.yml/badge.svg)](https://github.com/vaddisrinivas/cc-later/actions/workflows/test.yml)

Runs your `.claude/LATER.md` tasks when your Claude Code window is near expiry.

**No extra tokens.** cc-later spends idle capacity that would otherwise expire at the end of your 5-hour session — use it or lose it.

## Install

```bash
/plugin marketplace add vaddisrinivas/cc-later
/plugin install cc-later@cc-later
```

On first run, cc-later creates `~/.cc-later/config.toml` from defaults. Edit it to add your watched repos:

```toml
[paths]
watch = ["~/projects/my-repo"]

[dispatch]
enabled = true
```

## How It Works

```
User ends session
      │
      ▼
Stop hook → handler.py
      │
      ├─ Gate checks (enabled? watched? idle? window? peak?)
      │         │
      │    [gate closed] → exit 0, no-op
      │
      ▼
Select entries from .claude/LATER.md
      │
      ▼
Launch detached `claude -p` jobs (one per repo)
      │
      ▼
Next hook run → reconcile results → mark [x] in LATER.md
```

## LATER.md Format

```markdown
# LATER

- [ ] Audit error handling in auth.py — several bare except clauses swallow errors silently
- [!] Fix SQL query N+1 in ReportGenerator — causes 30s load on dashboard
- [x] Remove dead import in utils.py
```

| Marker | Meaning |
|--------|---------|
| `- [ ]` | Pending — will be dispatched |
| `- [!]` | Priority pending — dispatched first |
| `- [x]` | Completed — skipped |

**Good entries** are self-contained, codebase-only, and completable in a single `claude -p` run (~5 min):
- "Audit error handling in `auth.py` and report gaps"
- "Add missing type hints to `utils/http.py`"

**Avoid** entries that require credentials, external services, interactive input, or deployment steps.

## Key Phrase Auto-Capture

Type any of these phrases during a session and cc-later automatically appends to `.claude/LATER.md`:

| Phrase | Example |
|--------|---------|
| `later:` | `later: fix the N+1 query in reports` |
| `add to later:` | `add to later: update README install steps` |
| `note for later:` | `note for later: UserService.delete() swallows exceptions` |
| `queue for later:` | `queue for later: add rate limiting to /refresh` |
| `for later:` | `for later: check the migration script` |
| `later[!]:` | `later[!]: SQL injection in filter builder` ← priority |

The colon is required — "handle this later" and "see you later" do not trigger.

## Dispatch Modes

Set `dispatch_mode` in `[window]`:

| Mode | Behavior |
|------|----------|
| `window_aware` (default) | Reads Claude JSONL files to reconstruct the 5-hr window; dispatches when ≤30 min remain |
| `time_based` | Ignores JSONL; dispatches only during `fallback_dispatch_hours` (e.g. `["22:00-02:00"]`) |
| `always` | Dispatches whenever the idle gate is open, regardless of time or window |

## Configuration

Key settings in `~/.cc-later/config.toml`:

| Key | Default | Description |
|-----|---------|-------------|
| `dispatch.enabled` | `false` | Master switch |
| `dispatch.model` | `"sonnet"` | `"sonnet"` or `"opus"` |
| `dispatch.allow_file_writes` | `false` | Let dispatched tasks write files |
| `dispatch.idle_grace_minutes` | `10` | Minutes of inactivity before dispatching |
| `later_md.max_entries_per_dispatch` | `3` | Max tasks per dispatch run |
| `later_md.mark_completed` | `"check"` | `"check"` (→ `[x]`) or `"delete"` |
| `window.dispatch_mode` | `"window_aware"` | See dispatch modes above |
| `window.trigger_at_minutes_remaining` | `30` | Window threshold for `window_aware` |

## Status Command

```
/cc-later:status
```

Example output:

```
## cc-later Status

### Window
Mode: window_aware
Elapsed: 287 min | Remaining: 13 min
Tokens: 42,300 in / 18,900 out

### Gates
[✓] dispatch.enabled
[✓] paths.watch non-empty (1 path)
[✓] not in peak window
[✓] idle grace (14.2 min >= 10 min)
[✓] mode gate: 13 min remaining <= 30 min trigger

### Queue
~/projects/my-repo — 3 pending (1 priority)
  [!] Fix SQL query N+1 in ReportGenerator
  [ ] Audit error handling in auth.py
  [ ] Add missing type hints to utils/http.py

### Recent Runs
2026-03-30T01:44:12Z  dispatch    my-repo  2 entries
2026-03-29T22:11:05Z  complete    my-repo  2 done
```

## Troubleshooting

**Tasks never dispatch**
Run `python3 ~/.claude/plugins/cc-later/scripts/handler.py --dry-run` to see which gate is blocking.

**Capture hook doesn't fire**
Check `hooks/hooks.json` is installed: `/plugin list`. Verify the phrase includes a colon (`later: fix this`, not `fix this later`).

**Tasks dispatched but LATER.md not updated**
The result file is written to `~/.cc-later/results/`. Check for leftover `.lock` file: `ls ~/.cc-later/*.lock` and delete if the process is no longer running.

**`dispatch.allow_file_writes = false` (default)**
Dispatched tasks run read-only. They can report findings but cannot modify files. Set `allow_file_writes = true` to enable mutations.

## Development

```bash
# Run all tests
python3 -m unittest discover -s tests -v

# Check gate decisions without dispatching
python3 scripts/handler.py --dry-run

# Show current status
python3 scripts/status.py
```

Design and implementation details: `SPEC.md`.
