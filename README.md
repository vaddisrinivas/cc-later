# cc-later

Runs your `.claude/LATER.md` tasks when your Claude Code window is near expiry.

cc-later does not create extra compute. It spends idle capacity that would otherwise expire at the end of your 5-hour window.

## Install

```bash
/plugin marketplace add vaddisrinivas/cc-later
/plugin install cc-later@cc-later
```

## First Run

On first Stop hook execution, cc-later creates `~/.cc-later/config.toml` from defaults and exits safely.

## Setup

Minimal config:

```toml
[paths]
watch = ["~/projects/my-repo"]

[dispatch]
enabled = true
```

Everything else uses safe defaults.

## How It Works

1. Stop hook runs `scripts/handler.py`.
2. Handler checks gates (config enabled, watched paths, idle/peak, dispatch mode).
3. If eligible, it selects pending items from `LATER.md` and launches detached `claude -p` jobs.
4. On later hook runs, it reconciles completed results and marks done items in `LATER.md`.

## LATER.md Format

```markdown
# LATER

- [ ] Add rate limiting to /refresh endpoint
- [!] Fix SQL query N+1 in ReportGenerator
- [x] Remove dead import in utils.py
```

- `- [ ]` pending
- `- [!]` priority pending
- `- [x]` completed

## Dispatch Modes

Set in `[window].dispatch_mode`:

- `window_aware` (default): uses Claude JSONL window reconstruction.
- `time_based`: ignores JSONL; dispatches only in `fallback_dispatch_hours`.
- `always`: ignores JSONL and time windows; dispatches when idle gate is open.

## Slash Command

`/cc-later:status` prints:

- window state
- gate status
- pending queue summary
- last run-log entries

## Development

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

Core design/implementation details live in `SPEC.md`.
