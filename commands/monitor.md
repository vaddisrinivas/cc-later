---
name: monitor
description: Check window, budget, agents, and plan info
user_invocable: true
---

Run the cc-later monitor to check current window state, budget usage, agent health, and plan info.

```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} python3 ${CLAUDE_PLUGIN_ROOT}/scripts/monitor.py --once
```

Display the output to the user.

To set up periodic monitoring in the current session, suggest using CronCreate:
- Example: every 15 minutes, run `/cc-later:monitor` to check status
- This auto-cleans up when the session ends

To install always-on monitoring (survives across sessions):
```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} python3 ${CLAUDE_PLUGIN_ROOT}/scripts/monitor.py --install --interval 15
```
