# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""cc-later status command — prints current window, gate, queue, and run state."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _load_handler():
    """Load handler module, reusing cached copy if already imported."""
    name = "cc_later_handler"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("handler.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load handler.py from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    h = _load_handler()

    cfg, cfg_err = h.load_or_create_config()
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone()

    print("## cc-later Status\n")

    # ── Window ──────────────────────────────────────────────────────────────
    print("### Window")
    if cfg is None:
        print(f"Config error: {cfg_err}\n")
    else:
        mode = cfg.window.dispatch_mode
        if mode == "window_aware":
            roots = h._resolve_jsonl_roots(cfg.window)
            ws = h.compute_window_state(roots, now_utc=now_utc)
            if ws is None:
                print(f"Mode: {mode}")
                print("Window state: unknown (no JSONL data found)")
            else:
                total = ws.elapsed_minutes + ws.remaining_minutes
                pct = 100 * ws.elapsed_minutes // total if total else 0
                print(f"Mode: {mode}")
                print(
                    f"Elapsed: {ws.elapsed_minutes} min | "
                    f"Remaining: {ws.remaining_minutes} min ({pct}% used)"
                )
                print(f"Tokens: {ws.total_input_tokens:,} in / {ws.total_output_tokens:,} out")
                if ws.source_path:
                    print(f"Source: {ws.source_path}")
        elif mode == "time_based":
            in_window = h.is_within_time_ranges(now_local, cfg.window.fallback_dispatch_hours)
            state_str = "IN dispatch window" if in_window else "outside dispatch window"
            print(f"Mode: {mode} — {state_str}")
            hours = cfg.window.fallback_dispatch_hours or "(none configured)"
            print(f"Dispatch hours: {hours}")
        else:
            print(f"Mode: {mode} — dispatches whenever idle grace is satisfied")
        print()

    # ── Gates ────────────────────────────────────────────────────────────────
    print("### Gates")
    if cfg is None:
        print("Cannot evaluate gates: config error\n")
    else:
        state = h.load_state()

        def gate(label: str, ok: bool) -> None:
            print(f"  {'[✓]' if ok else '[✗]'} {label}")

        gate("dispatch.enabled", cfg.dispatch.enabled)
        gate(
            f"paths.watch non-empty ({len(cfg.paths.watch)} paths)",
            bool(cfg.paths.watch),
        )

        in_peak = cfg.window.respect_peak_hours and h._is_in_peak_window(
            now_local, cfg.window.peak_windows
        )
        gate("not in peak window", not in_peak)

        idle_gate_ts = now_utc
        for repo_state in state.repos.values():
            if repo_state.dispatch_ts:
                try:
                    last_ts = datetime.fromisoformat(repo_state.dispatch_ts)
                    if last_ts > idle_gate_ts:
                        idle_gate_ts = last_ts
                except ValueError:
                    pass
        idle_since = (now_utc - idle_gate_ts).total_seconds() / 60
        idle_ok = idle_since >= cfg.window.idle_grace_period_minutes
        gate(
            f"idle grace ({idle_since:.1f} min elapsed, need "
            f"{cfg.window.idle_grace_period_minutes} min)",
            idle_ok,
        )

        mode = cfg.window.dispatch_mode
        if mode == "window_aware":
            roots = h._resolve_jsonl_roots(cfg.window)
            ws = h.compute_window_state(roots, now_utc=now_utc)
            if ws is None:
                gate("mode gate: window_aware (JSONL readable)", False)
            else:
                mode_ok = ws.remaining_minutes <= cfg.window.trigger_at_minutes_remaining
                gate(
                    f"mode gate: window_aware ({ws.remaining_minutes} min remaining,"
                    f" trigger ≤ {cfg.window.trigger_at_minutes_remaining})",
                    mode_ok,
                )
        elif mode == "time_based":
            time_ok = h.is_within_time_ranges(now_local, cfg.window.fallback_dispatch_hours)
            gate(
                f"mode gate: time_based (now {'in' if time_ok else 'outside'} dispatch hours)",
                time_ok,
            )
        else:
            gate("mode gate: always (no time restriction)", True)
        print()

    # ── Queue ────────────────────────────────────────────────────────────────
    print("### Queue")
    if cfg is None:
        print("Cannot show queue: config error\n")
    else:
        if not cfg.paths.watch:
            print("  No watch paths configured")
        else:
            state = h.load_state()
            for raw_path in cfg.paths.watch:
                repo_path = Path(raw_path).expanduser()
                repo_state = state.repos.get(str(repo_path))
                later_path = repo_path / cfg.later_md.path

                inflight = " [in-flight]" if (repo_state and repo_state.in_flight) else ""
                print(f"  {repo_path}{inflight}:")

                if not later_path.exists():
                    print(f"    No LATER.md at {cfg.later_md.path}")
                    continue
                try:
                    content = later_path.read_text(encoding="utf-8")
                except OSError:
                    print(f"    Could not read {cfg.later_md.path}")
                    continue

                entries = h.parse_later_entries(
                    content, priority_marker=cfg.later_md.priority_marker
                )
                if not entries:
                    print("    No pending entries")
                    continue

                urgent = sum(1 for e in entries if e.is_priority)
                plural = "entry" if len(entries) == 1 else "entries"
                urgent_str = f", {urgent} urgent" if urgent else ""
                print(f"    {len(entries)} pending {plural}{urgent_str}")

                previews = sorted(
                    entries, key=lambda e: (0 if e.is_priority else 1, e.line_index)
                )[:3]
                for entry in previews:
                    marker = "[!]" if entry.is_priority else "[ ]"
                    print(f"    {marker} {entry.text}")
        print()

    # ── Recent Runs ──────────────────────────────────────────────────────────
    print("### Recent Runs")
    run_log = h.RUN_LOG_PATH
    if not run_log.exists():
        print("  No runs recorded yet")
    else:
        try:
            lines = run_log.read_text(encoding="utf-8").splitlines()
        except OSError:
            print("  Could not read run log")
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
            if len(recent) >= 5:
                break

        if not recent:
            print("  No runs recorded yet")
        else:
            for entry in recent:
                ts_raw = entry.get("ts", "")
                try:
                    ts = (
                        datetime.fromisoformat(ts_raw)
                        .astimezone()
                        .strftime("%Y-%m-%d %H:%M:%S")
                    )
                except (ValueError, TypeError):
                    ts = ts_raw[:19] if ts_raw else "unknown"

                event = entry.get("event", "unknown")
                parts = [f"{ts} | {event}"]

                if event == "dispatch":
                    repo = Path(entry.get("repo", "")).name
                    n = entry.get("entries_dispatched", "?")
                    model = entry.get("model", "")
                    parts.append(f"repo={repo} entries={n} model={model}")
                elif event == "reconcile":
                    parts.append(f"completed={entry.get('completed', '?')}")
                elif event in ("skip", "error", "dispatch_failed"):
                    parts.append(f"reason={entry.get('reason', entry.get('detail', '?'))}")

                print(f"  {' | '.join(parts)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
