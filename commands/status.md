---
description: Show cc-later window, gate, queue, and recent run state.
---

# /cc-later:status

Show current cc-later operational state.

## Steps
1. Read `~/.cc-later/config.toml` if it exists.
2. Read `~/.cc-later/run_log.jsonl` and summarize the last 5 entries.
3. Show current window state:
   - If `dispatch_mode = "window_aware"`, inspect Claude JSONL data directories and report elapsed/remaining minutes.
   - If `dispatch_mode = "time_based"` or `"always"`, explain mode and active dispatch gate.
4. For each path in `[paths].watch`, show:
   - LATER.md location
   - number of pending entries (`[ ]` and `[!]`)
   - up to 3 entry previews
5. Print gate checklist: dispatch enabled, watch list non-empty, idle grace satisfied, mode gate satisfied, peak window allowed.

## Output format
Use concise markdown with sections:
- Window
- Gates
- Queue
- Recent Runs
