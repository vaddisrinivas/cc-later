#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""cc-later window probe — initiates a 5hr billing window when none is active.

Run this on a cron schedule (e.g. every 30 min) to ensure a window is always
open when the user may need it. Uses a minimal haiku ping to start the clock
with the fewest tokens possible.

Cron example (every 30 min):
    */30 * * * * python3 ~/.claude/plugins/cache/cc-later/cc-later/0.2.0/scripts/probe.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running directly or as part of the package
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))

from scripts.handler import (  # noqa: E402
    APP_DIR,
    BudgetConfig,
    DEFAULT_WINDOW_MINUTES,
    RUN_LOG_PATH,
    compute_budget_state,
    compute_window_state,
    load_or_create_config,
    load_state,
    log_event,
    _resolve_jsonl_roots,
)


def main() -> int:
    cfg, first_run_msg = load_or_create_config()
    if cfg is None:
        # First run — config just created, no window to probe
        return 0

    now_utc = datetime.now(timezone.utc)
    roots = _resolve_jsonl_roots(cfg.window)

    # --- Budget gate: never probe if at/above backoff threshold ---
    budget_state = compute_budget_state(roots, now_utc, cfg.budget.weekly_token_budget)
    if budget_state.pct_used >= cfg.budget.backoff_at_pct / 100:
        log_event(
            "probe_skipped",
            reason="budget_limit",
            pct_used=round(budget_state.pct_used * 100, 1),
        )
        print(
            f"[cc-later probe] Budget {budget_state.pct_used * 100:.1f}% used — skipping probe."
        )
        return 0

    # --- Check if a window is already active and healthy ---
    window_state = compute_window_state(roots, now_utc)
    trigger = cfg.window.trigger_at_minutes_remaining

    if window_state is not None and window_state.remaining_minutes > trigger:
        log_event(
            "probe_skipped",
            reason="window_active",
            remaining_minutes=window_state.remaining_minutes,
        )
        print(
            f"[cc-later probe] Window active — {window_state.remaining_minutes} min remaining. "
            "No probe needed."
        )
        return 0

    # --- Idle grace: don't probe if the Stop hook fired recently ---
    state = load_state()
    if state.last_hook_ts:
        from scripts.handler import _parse_iso8601
        last_ts = _parse_iso8601(state.last_hook_ts)
        if last_ts is not None:
            idle_minutes = (now_utc - last_ts).total_seconds() / 60
            if idle_minutes < cfg.window.idle_grace_period_minutes:
                log_event("probe_skipped", reason="idle_grace_active")
                print(
                    f"[cc-later probe] Session ended {idle_minutes:.1f} min ago — "
                    "still within grace period."
                )
                return 0

    # --- Initiate a window: spawn a minimal haiku ping ---
    probe_model = cfg.budget.probe_model
    result_path = APP_DIR / f"probe-{now_utc.strftime('%Y%m%d-%H%M%S')}.json"

    cmd = [
        "claude",
        "-p", ".",
        "--output-format", "json",
        "--model", probe_model,
    ]

    try:
        out_fh = result_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=out_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        out_fh.close()
    except OSError as exc:
        log_event("probe_error", reason=str(exc))
        print(f"[cc-later probe] Failed to spawn probe: {exc}")
        return 1

    log_event(
        "probe_initiated",
        model=probe_model,
        pid=proc.pid,
        result_path=str(result_path),
        window_reason="no_window" if window_state is None else "window_expiring",
    )
    print(
        f"[cc-later probe] Window initiated via {probe_model} ping "
        f"(pid {proc.pid}). 5hr window clock started."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
