#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""cc-later CLI — the command center.

Usage:
    cc-later status          Show window, gates, queue, and recent runs
    cc-later stats           Analytics dashboard (success rates, tokens, trends)
    cc-later inspect [N]     Inspect recent dispatch results (default: last 5)
    cc-later dispatch        Force a dispatch cycle (bypasses idle/window gates)
    cc-later dry-run         Show what would dispatch without doing it
    cc-later init [path]     Initialize a repo for cc-later
    cc-later queue [path]    Show pending queue for a repo
    cc-later import-log      Backfill analytics from existing run_log.jsonl
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _setup_imports() -> None:
    """Set up import path so cc_later package is importable."""
    pkg_root = Path(__file__).resolve().parent.parent
    if str(pkg_root) not in sys.path:
        sys.path.insert(0, str(pkg_root))


_setup_imports()

from cc_later.analytics import AnalyticsDB
from cc_later.config import load_or_create_config
from cc_later.models import ConfigError, RepoState
from cc_later.paths import APP_DIR, RUN_LOG_PATH
from cc_later.parser import estimate_complexity, parse_later_entries, route_model
from cc_later.reporter import generate_stats_dashboard
from cc_later.window import (
    compute_budget_state,
    compute_window_state,
    expand_watch_paths,
    is_in_peak_window,
    is_within_time_ranges,
    parse_iso8601,
    resolve_jsonl_roots,
)


def cmd_status() -> int:
    """Show current window, gates, queue, and recent runs."""
    from cc_later.dispatcher import load_state

    cfg, cfg_err = load_or_create_config()
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone()

    print("## cc-later Status\n")

    # ── Window ──
    print("### Window")
    if cfg is None:
        print(f"Config error: {cfg_err}\n")
    else:
        mode = cfg.window.dispatch_mode
        roots = resolve_jsonl_roots(cfg.window)
        budget_state = compute_budget_state(roots, now_utc, cfg.budget.weekly_token_budget)
        backoff_tokens = int(cfg.budget.weekly_token_budget * cfg.budget.backoff_at_pct / 100)
        if mode == "window_aware":
            ws = compute_window_state(roots, now_utc=now_utc)
            if ws is None:
                print(f"  Mode: {mode}")
                print("  State: unknown (no JSONL data)")
                print("  Next window: starts on your next Claude request")
            else:
                total = ws.elapsed_minutes + ws.remaining_minutes
                pct = 100 * ws.elapsed_minutes // total if total else 0
                bar = _progress_bar(pct, 20)
                end_local = (now_utc + timedelta(minutes=ws.remaining_minutes)).astimezone()
                print(f"  Mode: {mode}")
                print(f"  {bar} {ws.elapsed_minutes}m elapsed / {ws.remaining_minutes}m remaining")
                print(f"  Tokens: {ws.total_input_tokens:,} in / {ws.total_output_tokens:,} out")
                print(f"  Window ends: {end_local.strftime('%Y-%m-%d %H:%M %Z')}")
                if ws.remaining_minutes <= 0:
                    print("  Next window: starts on your next Claude request")
                else:
                    print(
                        "  Next window: starts on your first Claude request "
                        f"after {end_local.strftime('%H:%M %Z')}"
                    )
        elif mode == "time_based":
            in_window = is_within_time_ranges(now_local, cfg.window.fallback_dispatch_hours)
            state_str = "IN dispatch window" if in_window else "outside dispatch window"
            print(f"  Mode: {mode} — {state_str}")
        else:
            print(f"  Mode: {mode} — dispatches whenever idle")
        print(
            f"  Weekly budget: {budget_state.tokens_used_this_week:,} / "
            f"{cfg.budget.weekly_token_budget:,} ({budget_state.pct_used*100:.1f}%)"
        )
        print(
            f"  Backoff at: {cfg.budget.backoff_at_pct}% "
            f"({backoff_tokens:,} tokens)"
        )
    print()

    # ── Gates ──
    print("### Gates")
    if cfg is None:
        print("  Cannot evaluate: config error\n")
    else:
        state = load_state()

        def gate(label: str, ok: bool) -> None:
            print(f"  {'[pass]' if ok else '[FAIL]'} {label}")

        gate("dispatch.enabled", cfg.dispatch.enabled)
        gate(f"paths.watch ({len(cfg.paths.watch)} paths)", bool(cfg.paths.watch))

        in_peak = cfg.window.respect_peak_hours and is_in_peak_window(now_local, cfg.window.peak_windows)
        gate("not in peak window", not in_peak)

        # Idle gate
        idle_gate_ts = now_utc
        for rs in state.repos.values():
            if rs.dispatch_ts:
                try:
                    last_ts = datetime.fromisoformat(rs.dispatch_ts)
                    if last_ts > idle_gate_ts:
                        idle_gate_ts = last_ts
                except ValueError:
                    pass
        idle_since = (now_utc - idle_gate_ts).total_seconds() / 60
        gate(f"idle grace ({idle_since:.1f}m >= {cfg.window.idle_grace_period_minutes}m)",
             idle_since >= cfg.window.idle_grace_period_minutes)

        # Budget gate
        roots = resolve_jsonl_roots(cfg.window)
        budget_state = compute_budget_state(roots, now_utc, cfg.budget.weekly_token_budget)
        budget_ok = budget_state.pct_used < cfg.budget.backoff_at_pct / 100
        gate(f"budget ({budget_state.pct_used*100:.1f}% / {cfg.budget.backoff_at_pct}% limit)",
             budget_ok)

        # Mode gate
        from cc_later.window import resolve_trigger_threshold
        effective_trigger = resolve_trigger_threshold(
            now_local=now_local,
            trigger_at_minutes_remaining=cfg.window.trigger_at_minutes_remaining,
            trigger_schedules=cfg.window.trigger_schedules,
            trigger_schedules_enabled=cfg.window.trigger_schedules_enabled,
        )
        schedule_note = ""
        if cfg.window.trigger_schedules_enabled and effective_trigger != cfg.window.trigger_at_minutes_remaining:
            remaining_pct = int(effective_trigger * 100 / 300)
            schedule_note = f" (schedule: {remaining_pct}%)"

        mode = cfg.window.dispatch_mode
        if mode == "window_aware":
            ws = compute_window_state(roots, now_utc=now_utc)
            if ws is None:
                gate("mode: window_aware (no JSONL)", False)
            else:
                gate(f"mode: window_aware ({ws.remaining_minutes}m left <= {effective_trigger}m trigger{schedule_note})",
                     ws.remaining_minutes <= effective_trigger)
        elif mode == "time_based":
            time_ok = is_within_time_ranges(now_local, cfg.window.fallback_dispatch_hours)
            gate(f"mode: time_based ({'in' if time_ok else 'outside'} window)", time_ok)
        else:
            gate("mode: always", True)

        print(
            "\n  Routing: "
            f"{cfg.dispatch.model_routing} | "
            f"Retry: {'on' if cfg.retry.enabled else 'off'} | "
            f"Auto-resume: {'on' if cfg.auto_resume.enabled else 'off'} "
            f"(min {cfg.auto_resume.min_remaining_minutes}m) | "
            f"Verify: {'on' if cfg.verify.enabled else 'off'}"
        )
    print()

    # ── Queue ──
    print("### Queue")
    if cfg is None:
        print("  Config error\n")
    elif not cfg.paths.watch:
        print("  No watch paths configured")
    else:
        state = load_state()
        total_pending = 0
        for raw_path in cfg.paths.watch:
            repo_path = Path(raw_path).expanduser()
            repo_state = state.repos.get(str(repo_path))
            later_path = repo_path / cfg.later_md.path

            inflight = " [in-flight]" if (repo_state and repo_state.in_flight) else ""
            print(f"\n  {repo_path.name}/{inflight}")

            if not later_path.exists():
                print(f"    No LATER.md")
                continue
            try:
                content = later_path.read_text(encoding="utf-8")
            except OSError:
                print(f"    Could not read LATER.md")
                continue

            entries = parse_later_entries(content, priority_marker=cfg.later_md.priority_marker)
            if not entries:
                print("    Empty queue")
                continue

            total_pending += len(entries)
            urgent = sum(1 for e in entries if e.is_priority)
            retrying = sum(1 for e in entries if e.attempts > 0)

            parts = [f"{len(entries)} pending"]
            if urgent:
                parts.append(f"{urgent} priority")
            if retrying:
                parts.append(f"{retrying} retrying")
            print(f"    {', '.join(parts)}")

            previews = sorted(entries, key=lambda e: (0 if e.is_priority else 1, e.line_index))[:5]
            for entry in previews:
                marker = "[!]" if entry.is_priority else "[ ]"
                model = route_model(entry, cfg.dispatch.model, cfg.dispatch.model_routing)
                complexity = estimate_complexity(entry)
                retry = f" (try {entry.attempts + 1})" if entry.attempts > 0 else ""
                print(f"    {marker} {entry.text[:60]}{'...' if len(entry.text) > 60 else ''}")
                print(f"        c={complexity} model={model}{retry}")

        if total_pending == 0:
            print("\n  All queues empty")
    print()

    # ── Recent Runs ──
    print("### Recent Runs")
    if not RUN_LOG_PATH.exists():
        print("  No runs recorded")
    else:
        try:
            lines = RUN_LOG_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []

        recent: list[dict] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                recent.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(recent) >= 8:
                break

        if not recent:
            print("  No runs recorded")
        else:
            for entry in recent:
                ts_raw = entry.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_raw).astimezone().strftime("%m-%d %H:%M")
                except (ValueError, TypeError):
                    ts = ts_raw[:16] if ts_raw else "?"

                event = entry.get("event", "?")
                parts = [f"{ts} {event:18s}"]

                if event == "dispatch":
                    repo = Path(entry.get("repo", "")).name
                    n = entry.get("entries_dispatched", "?")
                    model = entry.get("model", "")
                    parts.append(f"{repo} {n} tasks ({model})")
                elif event == "reconcile":
                    parts.append(f"completed={entry.get('completed', '?')}")
                elif event in ("skip", "error", "dispatch_failed"):
                    parts.append(entry.get("reason", entry.get("detail", "?")))
                elif event == "verify_downgrade":
                    parts.append(f"task {entry.get('task_id', '?')} → NEEDS_HUMAN ({entry.get('confidence', '?')})")
                elif event == "rotated":
                    parts.append(Path(entry.get("repo", "")).name)

                print(f"  {'  '.join(parts)}")

    # ── Analytics Summary ──
    try:
        db = AnalyticsDB()
        stats = db.get_stats(days=7)
        db.close()
        if stats.total_dispatched > 0:
            print(f"\n### Analytics (7d)")
            print(f"  {stats.total_completed}/{stats.total_dispatched} completed ({stats.success_rate:.0%})")
            print(f"  {stats.total_input_tokens + stats.total_output_tokens:,} tokens used")
            if stats.streak > 0:
                print(f"  Streak: {stats.streak} consecutive successes")
    except Exception:
        pass

    return 0


def cmd_stats(days: int = 30) -> int:
    """Show analytics dashboard."""
    db = AnalyticsDB()
    stats = db.get_stats(days=days)

    if stats.total_dispatched == 0:
        print("No dispatch data yet. Run some tasks first, or use `cc-later import-log` to backfill.")
        db.close()
        return 0

    print(generate_stats_dashboard(db, days=days))
    db.close()
    return 0


def cmd_inspect(limit: int = 5) -> int:
    """Inspect recent dispatch results."""
    db = AnalyticsDB()
    recent = db.recent_dispatches(limit=limit)
    db.close()

    if not recent:
        # Fall back to result files
        results_dir = APP_DIR / "results"
        if not results_dir.exists():
            print("No dispatch results found.")
            return 0
        result_files = sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not result_files:
            print("No dispatch results found.")
            return 0

        for f in result_files[:limit]:
            print(f"\n--- {f.name} ---")
            try:
                content = f.read_text(encoding="utf-8")
                # Try to extract just the text content from JSON
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        text = data.get("result", data.get("text", data.get("content", content)))
                        if isinstance(text, str):
                            # Show first 500 chars
                            print(text[:500])
                            if len(text) > 500:
                                print(f"... ({len(text)} chars total)")
                        else:
                            print(json.dumps(data, indent=2)[:500])
                    else:
                        print(content[:500])
                except json.JSONDecodeError:
                    print(content[:500])
            except OSError:
                print("  Could not read file")
        return 0

    print(f"## Recent Dispatches (last {limit})\n")
    for r in recent:
        ts = r["ts"][:16].replace("T", " ")
        status = r["status"] or "in-flight"
        status_icon = {"DONE": "done", "FAILED": "FAIL", "NEEDS_HUMAN": "human",
                       "SKIPPED": "skip"}.get(status, status)
        print(f"  [{status_icon:6s}] {ts}  {r['task_text'][:60]}")
        details = []
        if r["model"]:
            details.append(f"model={r['model']}")
        if r["duration_s"]:
            details.append(f"duration={r['duration_s']:.1f}s")
        tokens = (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
        if tokens:
            details.append(f"tokens={tokens:,}")
        if r["attempts"] and r["attempts"] > 1:
            details.append(f"attempt={r['attempts']}")
        if r["error"]:
            details.append(f"error={r['error'][:40]}")
        if details:
            print(f"           {' | '.join(details)}")
    return 0


def cmd_init(path: str | None = None) -> int:
    """Initialize a repo for cc-later."""
    repo_path = Path(path).resolve() if path else Path.cwd()
    later_path = repo_path / ".claude" / "LATER.md"

    if later_path.exists():
        print(f"LATER.md already exists at {later_path}")
        return 0

    later_path.parent.mkdir(parents=True, exist_ok=True)
    later_path.write_text("# LATER\n\n## Security\n\n## Bugs\n\n## Tests\n\n## Docs\n\n## Refactor\n\n## Reports\n", encoding="utf-8")
    print(f"Created {later_path}")

    # Add to .gitignore
    gitignore = repo_path / ".gitignore"
    existing = ""
    if gitignore.exists():
        existing = gitignore.read_text(encoding="utf-8")
    if ".claude/LATER.md" not in existing:
        with gitignore.open("a", encoding="utf-8") as f:
            if not existing.endswith("\n"):
                f.write("\n")
            f.write(".claude/LATER.md\n.claude/LATER-*.md\n.claude/reports/\n")
        print("Updated .gitignore")

    # Offer to add to config
    cfg, _ = load_or_create_config()
    if cfg and str(repo_path) not in cfg.paths.watch:
        print(f"\nTo enable dispatching, add to ~/.cc-later/config.toml:")
        print(f'  [paths]')
        print(f'  watch = ["{repo_path}"]')
        print(f'  ')
        print(f'  [dispatch]')
        print(f'  enabled = true')

    return 0


def cmd_queue(path: str | None = None) -> int:
    """Show pending queue for a specific repo."""
    repo_path = Path(path).resolve() if path else Path.cwd()
    later_path = repo_path / ".claude" / "LATER.md"

    if not later_path.exists():
        print(f"No LATER.md at {later_path}")
        return 1

    content = later_path.read_text(encoding="utf-8")
    cfg, _ = load_or_create_config()
    priority_marker = cfg.later_md.priority_marker if cfg else "[!]"

    entries = parse_later_entries(content, priority_marker=priority_marker)
    if not entries:
        print("Queue is empty")
        return 0

    print(f"## {repo_path.name} — {len(entries)} pending\n")
    current_section = None
    for entry in sorted(entries, key=lambda e: (0 if e.is_priority else 1, e.line_index)):
        if entry.section != current_section:
            current_section = entry.section
            if current_section:
                print(f"\n### {current_section}")

        marker = "[!]" if entry.is_priority else "[ ]"
        model = route_model(entry, "sonnet", cfg.dispatch.model_routing if cfg else "fixed")
        complexity = estimate_complexity(entry)
        retry = f" (attempt {entry.attempts + 1})" if entry.attempts > 0 else ""
        dep = f" (after: {entry.depends_on})" if entry.depends_on else ""
        print(f"  {marker} {entry.id}: {entry.text}{dep}")
        print(f"       complexity={complexity} → {model}{retry}")

    return 0


def cmd_import_log() -> int:
    """Backfill analytics from run_log.jsonl."""
    db = AnalyticsDB()
    count = db.import_from_run_log()
    db.close()
    print(f"Imported {count} dispatch records into analytics DB.")
    return 0


def _progress_bar(pct: int, width: int = 20) -> str:
    filled = int(width * pct / 100)
    bar = "=" * filled + "-" * (width - filled)
    return f"[{bar}] {pct}%"


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0

    cmd = args[0]

    if cmd == "status":
        return cmd_status()
    elif cmd == "stats":
        days = int(args[1]) if len(args) > 1 else 30
        return cmd_stats(days)
    elif cmd == "inspect":
        limit = int(args[1]) if len(args) > 1 else 5
        return cmd_inspect(limit)
    elif cmd == "dispatch":
        from cc_later.dispatcher import main as dispatch_main
        return dispatch_main()
    elif cmd == "dry-run":
        sys.argv.append("--dry-run")
        from cc_later.dispatcher import main as dispatch_main
        return dispatch_main()
    elif cmd == "init":
        path = args[1] if len(args) > 1 else None
        return cmd_init(path)
    elif cmd == "queue":
        path = args[1] if len(args) > 1 else None
        return cmd_queue(path)
    elif cmd == "import-log":
        return cmd_import_log()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
