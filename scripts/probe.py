#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""cc-later window probe — initiates a 5hr billing window when none is active.

Run on cron (e.g. every 30 min) to ensure a window is always open.
Uses a minimal haiku ping to start the clock with fewest tokens.

Cron example:
    */30 * * * * python3 ~/.claude/plugins/cache/cc-later/cc-later/0.3.0/scripts/probe.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from cc_later.config import load_or_create_config
from cc_later.dispatcher import load_state, log_event
from cc_later.paths import APP_DIR
from cc_later.window import (
    compute_budget_state,
    compute_window_state,
    parse_iso8601,
    resolve_jsonl_roots,
)


def main() -> int:
    cfg, _ = load_or_create_config()
    if cfg is None:
        return 0

    now_utc = datetime.now(timezone.utc)
    roots = resolve_jsonl_roots(cfg.window)

    # Budget gate
    budget_state = compute_budget_state(roots, now_utc, cfg.budget.weekly_token_budget)
    if budget_state.pct_used >= cfg.budget.backoff_at_pct / 100:
        log_event("probe_skipped", reason="budget_limit", pct_used=round(budget_state.pct_used * 100, 1))
        print(f"[cc-later probe] Budget {budget_state.pct_used*100:.1f}% — skipping.")
        return 0

    # Window check
    window_state = compute_window_state(roots, now_utc)
    trigger = cfg.window.trigger_at_minutes_remaining
    if window_state is not None and window_state.remaining_minutes > trigger:
        log_event("probe_skipped", reason="window_active", remaining_minutes=window_state.remaining_minutes)
        print(f"[cc-later probe] Window active — {window_state.remaining_minutes}m remaining.")
        return 0

    # Idle grace
    state = load_state()
    if state.last_hook_ts:
        last_ts = parse_iso8601(state.last_hook_ts)
        if last_ts:
            idle_min = (now_utc - last_ts).total_seconds() / 60
            if idle_min < cfg.window.idle_grace_period_minutes:
                log_event("probe_skipped", reason="idle_grace_active")
                print(f"[cc-later probe] Session ended {idle_min:.1f}m ago — grace period.")
                return 0

    # Spawn probe
    from cc_later.dispatcher import _find_claude_binary
    probe_model = cfg.budget.probe_model
    result_path = APP_DIR / f"probe-{now_utc.strftime('%Y%m%d-%H%M%S')}.json"
    claude_bin = _find_claude_binary()
    cmd = [claude_bin, "-p", ".", "--output-format", "json", "--model", probe_model]

    try:
        out_fh = result_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdout=out_fh, stderr=subprocess.STDOUT, start_new_session=True)
        out_fh.close()
    except OSError as exc:
        log_event("probe_error", reason=str(exc))
        print(f"[cc-later probe] Failed: {exc}")
        return 1

    log_event("probe_initiated", model=probe_model, pid=proc.pid,
              window_reason="no_window" if window_state is None else "window_expiring")
    print(f"[cc-later probe] Window initiated via {probe_model} (pid {proc.pid}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
