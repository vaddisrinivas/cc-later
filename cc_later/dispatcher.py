"""Core dispatch orchestration — the main handler loop."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .analytics import AnalyticsDB
from .config import load_or_create_config
from .lock import NonBlockingFileLock
from .models import (
    AppConfig,
    AppState,
    ConfigError,
    LaterEntry,
    RepoState,
    WindowState,
)
from .notify import notify
from .parser import (
    apply_completion,
    apply_retry_metadata,
    parse_later_entries,
    parse_result_summary,
    rotate_later_if_needed,
    route_model,
    select_entries,
)
from .paths import APP_DIR, LOCK_PATH, RUN_LOG_PATH, STATE_PATH
from .prompt import render_prompt, resolve_output_path
from .verify import passes_threshold, verify_result
from .window import (
    compute_budget_state,
    compute_window_state,
    expand_watch_paths,
    is_in_peak_window,
    is_within_time_ranges,
    parse_iso8601,
    resolve_jsonl_roots,
    resolve_trigger_threshold,
    should_dispatch_by_mode,
)


def load_state() -> AppState:
    if not STATE_PATH.exists():
        return AppState()
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return AppState()

    repos: dict[str, RepoState] = {}
    raw_repos = payload.get("repos", {})
    if isinstance(raw_repos, dict):
        for repo, data in raw_repos.items():
            if not isinstance(data, dict):
                continue
            repos[repo] = RepoState(
                in_flight=bool(data.get("in_flight", False)),
                dispatch_ts=_coerce_optional_str(data.get("dispatch_ts")),
                result_path=_coerce_optional_str(data.get("result_path")),
                pid=_coerce_optional_int(data.get("pid")),
                entries=data.get("entries", []) if isinstance(data.get("entries"), list) else [],
                model=_coerce_optional_str(data.get("model")),
            )
    return AppState(last_hook_ts=_coerce_optional_str(payload.get("last_hook_ts")), repos=repos)


def save_state(state: AppState) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_hook_ts": state.last_hook_ts,
        "repos": {repo: asdict(rs) for repo, rs in state.repos.items()},
    }
    STATE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def log_event(event: str, **fields: Any) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
    payload.update(fields)
    with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def main() -> int:
    """Main handler entry point — called by the Stop hook."""
    dry_run = "--dry-run" in sys.argv
    lock = NonBlockingFileLock(LOCK_PATH)
    if not lock.acquire():
        print("[cc-later] Handler busy; skipping this Stop event.")
        log_event("skip", reason="lock_held")
        return 0

    try:
        hook_payload = _read_hook_stdin()
        session_id = hook_payload.get("session_id") or hook_payload.get("sessionId")

        cfg, first_run_message = load_or_create_config()
        if first_run_message:
            print(first_run_message)
            log_event("skip", reason="first_run")
            return 0
        if cfg is None:
            return 0

        if dry_run:
            return _dry_run_report(cfg)

        state = load_state()
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone()

        # Initialize analytics
        db = AnalyticsDB()

        # Reconcile completed dispatches
        completed = _reconcile_in_flight(cfg, state, db)
        if completed:
            log_event("reconcile", completed=completed)
            notify(cfg.notifications, "cc-later", f"Completed {completed} dispatch(es)", "complete")

        previous_hook_ts = parse_iso8601(state.last_hook_ts) if state.last_hook_ts else None
        state.last_hook_ts = now_utc.isoformat()

        # --- Gate sequence ---
        if not cfg.dispatch.enabled:
            save_state(state)
            log_event("skip", reason="dispatch_disabled")
            print("[cc-later] Dispatch disabled in config.")
            db.close()
            return 0

        if not cfg.paths.watch:
            save_state(state)
            log_event("skip", reason="empty_watch_list")
            print("[cc-later] No watched paths configured.")
            db.close()
            return 0

        if previous_hook_ts is not None:
            if now_utc - previous_hook_ts < timedelta(minutes=cfg.window.idle_grace_period_minutes):
                save_state(state)
                log_event("skip", reason="idle_grace_active")
                print("[cc-later] Idle grace period active; skipping.")
                db.close()
                return 0

        if cfg.window.respect_peak_hours and is_in_peak_window(now_local, cfg.window.peak_windows):
            save_state(state)
            log_event("skip", reason="peak_window")
            print("[cc-later] Peak window active; skipping.")
            db.close()
            return 0

        roots = resolve_jsonl_roots(cfg.window)
        budget_state = compute_budget_state(roots, now_utc, cfg.budget.weekly_token_budget)
        if budget_state.pct_used >= cfg.budget.backoff_at_pct / 100:
            save_state(state)
            log_event("skip", reason="budget_limit",
                      pct_used=round(budget_state.pct_used * 100, 1))
            print(f"[cc-later] Budget limit: {budget_state.pct_used*100:.1f}% used. Skipping.")
            db.close()
            return 0

        window_state: WindowState | None = None
        if cfg.window.dispatch_mode == "window_aware":
            window_state = compute_window_state(roots, now_utc=now_utc, session_id=session_id)
            if window_state is None:
                save_state(state)
                log_event("skip", reason="window_unknown", mode="window_aware")
                print("[cc-later] WARN: No JSONL data. Set dispatch_mode = \"time_based\" or \"always\" to bypass.")
                db.close()
                return 0

        remaining = window_state.remaining_minutes if window_state else None
        effective_trigger = resolve_trigger_threshold(
            now_local=now_local,
            trigger_at_minutes_remaining=cfg.window.trigger_at_minutes_remaining,
            trigger_schedules=cfg.window.trigger_schedules,
            trigger_schedules_enabled=cfg.window.trigger_schedules_enabled,
        )
        if not should_dispatch_by_mode(
            dispatch_mode=cfg.window.dispatch_mode,
            now_local=now_local,
            fallback_dispatch_hours=cfg.window.fallback_dispatch_hours,
            remaining_minutes=remaining,
            trigger_at_minutes_remaining=effective_trigger,
        ):
            save_state(state)
            log_event("skip", reason="mode_gate_closed", mode=cfg.window.dispatch_mode)
            print("[cc-later] Dispatch gate closed for current mode.")
            db.close()
            return 0

        # --- Dispatch loop ---
        dispatched_count = 0
        for repo_path in expand_watch_paths(cfg.paths.watch):
            repo_key = str(repo_path)
            repo_state = state.repos.get(repo_key, RepoState())
            state.repos[repo_key] = repo_state

            if repo_state.in_flight:
                continue

            later_path = repo_path / cfg.later_md.path
            if not later_path.exists():
                continue

            if cfg.later_md.auto_gitignore:
                _ensure_gitignore_entry(repo_path, cfg.later_md.path)

            rotated = rotate_later_if_needed(later_path, now_local)
            if rotated:
                log_event("rotated", repo=repo_key)

            content = _safe_read(later_path)
            if content is None:
                continue

            entries = parse_later_entries(content, priority_marker=cfg.later_md.priority_marker)

            # Filter by retry eligibility
            if cfg.retry.enabled:
                eligible = []
                for e in entries:
                    if e.attempts >= cfg.retry.max_attempts:
                        continue
                    if e.last_attempt and cfg.retry.backoff_minutes:
                        backoff_idx = min(e.attempts, len(cfg.retry.backoff_minutes) - 1)
                        backoff_min = cfg.retry.backoff_minutes[backoff_idx]
                        last_ts = parse_iso8601(e.last_attempt)
                        if last_ts and now_utc - last_ts < timedelta(minutes=backoff_min):
                            continue
                    eligible.append(e)
                entries = eligible

            selected = select_entries(entries, cfg.later_md.max_entries_per_dispatch)
            if not selected:
                continue

            # Model routing: pick best model per task (use highest complexity for batch)
            if cfg.dispatch.model_routing == "auto":
                model = max(
                    (route_model(e, cfg.dispatch.model, "auto") for e in selected),
                    key=lambda m: {"haiku": 0, "sonnet": 1, "opus": 2}.get(m, 1),
                )
            else:
                model = cfg.dispatch.model

            prompt = render_prompt(repo_path, cfg, selected)
            result_path = resolve_output_path(cfg.dispatch.output_path, repo_path, now_utc)
            pid = _spawn_dispatch(model, repo_path, prompt, result_path, cfg.dispatch.allow_file_writes)
            if pid is None:
                log_event("error", repo=repo_key, reason="dispatch_spawn_failed")
                notify(cfg.notifications, "cc-later", f"Dispatch failed for {repo_path.name}", "error")
                continue

            repo_state.in_flight = True
            repo_state.dispatch_ts = now_utc.isoformat()
            repo_state.result_path = str(result_path)
            repo_state.pid = pid
            repo_state.entries = [e.to_dict() for e in selected]
            repo_state.model = model
            dispatched_count += 1

            # Record in analytics
            for entry in selected:
                db.record_dispatch(
                    repo=repo_key,
                    task_id=entry.id,
                    task_text=entry.text,
                    section=entry.section,
                    model=model,
                    attempts=entry.attempts + 1,
                    result_path=str(result_path),
                )

            log_event(
                "dispatch",
                repo=repo_key,
                entries_dispatched=len(selected),
                entries=[e.text for e in selected],
                remaining_minutes=remaining,
                model=model,
                result_path=str(result_path),
            )
            notify(
                cfg.notifications,
                "cc-later",
                f"Dispatched {len(selected)} task(s) in {repo_path.name} via {model}",
                "dispatch",
                {"repo": repo_path.name, "model": model, "tasks": [e.text for e in selected]},
            )

        save_state(state)
        db.close()

        if dispatched_count == 0:
            print("[cc-later] No pending entries eligible for dispatch.")
        else:
            suffix = f" ({remaining} min left)" if remaining is not None else ""
            print(f"[cc-later] Dispatched {dispatched_count} repo(s){suffix}.")
        return 0

    except ConfigError as exc:
        log_event("error", reason="config_error", detail=str(exc))
        print(f"[cc-later] ERROR: {exc}")
        return 0
    except Exception as exc:
        log_event("error", reason="unexpected_exception", detail=str(exc))
        print(f"[cc-later] ERROR: {exc}")
        return 0
    finally:
        lock.release()


def _reconcile_in_flight(cfg: AppConfig, state: AppState, db: AnalyticsDB) -> int:
    """Check completed dispatches, verify results, mark entries, update analytics."""
    completed = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for repo_key, repo_state in state.repos.items():
        if not repo_state.in_flight:
            continue
        if repo_state.pid and _is_process_alive(repo_state.pid):
            continue

        result_path = Path(repo_state.result_path).expanduser() if repo_state.result_path else None
        if result_path is None or not result_path.exists():
            if repo_state.pid is not None:
                log_event("dispatch_failed", repo=repo_key, pid=repo_state.pid)
                notify(cfg.notifications, "cc-later", f"Dispatch failed for {Path(repo_key).name}", "error")
            repo_state.in_flight = False
            repo_state.pid = None
            repo_state.result_path = None
            repo_state.entries = []
            continue

        raw = _safe_read(result_path)
        if raw is None:
            continue

        summary = parse_result_summary(raw)
        dispatched_entries = [LaterEntry.from_dict(e) for e in repo_state.entries if isinstance(e, dict)]

        # Verification pipeline
        verify_results = {}
        if cfg.verify.enabled and dispatched_entries:
            for entry in dispatched_entries:
                if entry.id in summary and summary[entry.id] == "DONE":
                    vr = verify_result(
                        task_id=entry.id,
                        entry=entry,
                        result_text=raw,
                        repo_path=Path(repo_key),
                        config=cfg.verify,
                        allow_file_writes=cfg.dispatch.allow_file_writes,
                    )
                    verify_results[entry.id] = vr
                    if not passes_threshold(vr, cfg.verify.min_confidence):
                        # Downgrade from DONE to NEEDS_HUMAN
                        summary[entry.id] = "NEEDS_HUMAN"
                        log_event("verify_downgrade", task_id=entry.id,
                                  confidence=vr.confidence, repo=repo_key)

        done_ids = {tid for tid, status in summary.items() if status == "DONE"}
        failed_ids = {tid: status for tid, status in summary.items()
                      if status in ("FAILED", "NEEDS_HUMAN")}

        # Mark completed entries
        if done_ids and dispatched_entries:
            later_path = Path(repo_key) / cfg.later_md.path
            content = _safe_read(later_path)
            if content is not None:
                updated = apply_completion(
                    content=content,
                    done_ids=done_ids,
                    dispatched_entries=dispatched_entries,
                    mark_mode=cfg.later_md.mark_completed,
                )
                if updated != content:
                    later_path.parent.mkdir(parents=True, exist_ok=True)
                    later_path.write_text(updated, encoding="utf-8")

        # Handle retries for failed tasks
        if cfg.retry.enabled and failed_ids and dispatched_entries:
            later_path = Path(repo_key) / cfg.later_md.path
            content = _safe_read(later_path)
            if content is not None:
                updated = apply_retry_metadata(
                    content=content,
                    failed_ids=failed_ids,
                    dispatched_entries=dispatched_entries,
                    max_attempts=cfg.retry.max_attempts,
                    escalate_to_priority=cfg.retry.escalate_to_priority,
                    now_iso=now_iso,
                )
                if updated != content:
                    later_path.write_text(updated, encoding="utf-8")

        # Generate report
        from .reporter import generate_dispatch_report, save_report
        report = generate_dispatch_report(
            repo_path=Path(repo_key),
            entries=dispatched_entries,
            results=summary,
            verify_results=verify_results if verify_results else None,
            model=repo_state.model or "sonnet",
        )
        save_report(Path(repo_key), report)

        # Record analytics outcomes
        for entry in dispatched_entries:
            status = summary.get(entry.id, "FAILED")
            db.record_outcome(
                task_id=entry.id,
                repo=repo_key,
                status=status,
            )

        repo_state.in_flight = False
        repo_state.pid = None
        repo_state.result_path = None
        repo_state.entries = []
        completed += 1

    return completed


def _dry_run_report(cfg: AppConfig) -> int:
    """Print gate evaluation and queue preview without dispatching."""
    state = load_state()
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone()

    print("[cc-later --dry-run]\n")

    def gate(label: str, ok: bool) -> None:
        print(f"  {'[pass]' if ok else '[FAIL]'} {label}")

    gate("dispatch.enabled", cfg.dispatch.enabled)
    gate(f"paths.watch ({len(cfg.paths.watch)} paths)", bool(cfg.paths.watch))

    if cfg.window.respect_peak_hours:
        in_peak = is_in_peak_window(now_local, cfg.window.peak_windows)
        gate("not in peak window", not in_peak)

    previous_hook_ts = parse_iso8601(state.last_hook_ts) if state.last_hook_ts else None
    if previous_hook_ts is not None:
        idle_since = (now_utc - previous_hook_ts).total_seconds() / 60
        gate(f"idle grace ({idle_since:.1f}m >= {cfg.window.idle_grace_period_minutes}m)",
             idle_since >= cfg.window.idle_grace_period_minutes)
    else:
        gate("idle grace (no previous hook)", True)

    roots = resolve_jsonl_roots(cfg.window)
    budget_state = compute_budget_state(roots, now_utc, cfg.budget.weekly_token_budget)
    gate(f"budget ({budget_state.pct_used*100:.1f}% of {cfg.budget.weekly_token_budget:,}, backoff {cfg.budget.backoff_at_pct}%)",
         budget_state.pct_used < cfg.budget.backoff_at_pct / 100)

    effective_trigger = resolve_trigger_threshold(
        now_local=now_local,
        trigger_at_minutes_remaining=cfg.window.trigger_at_minutes_remaining,
        trigger_schedules=cfg.window.trigger_schedules,
        trigger_schedules_enabled=cfg.window.trigger_schedules_enabled,
    )
    schedule_note = ""
    if cfg.window.trigger_schedules_enabled and effective_trigger != cfg.window.trigger_at_minutes_remaining:
        schedule_note = f" (schedule override: {effective_trigger}m)"

    mode = cfg.window.dispatch_mode
    if mode == "window_aware":
        ws = compute_window_state(roots, now_utc=now_utc)
        if ws is None:
            gate("mode: window_aware (no JSONL)", False)
        else:
            gate(f"mode: window_aware ({ws.remaining_minutes}m left, trigger <= {effective_trigger}m{schedule_note})",
                 ws.remaining_minutes <= effective_trigger)
    elif mode == "time_based":
        time_ok = is_within_time_ranges(now_local, cfg.window.fallback_dispatch_hours)
        gate(f"mode: time_based ({'in' if time_ok else 'outside'} window)", time_ok)
    else:
        gate("mode: always", True)

    print(f"\n  Model routing: {cfg.dispatch.model_routing} (default: {cfg.dispatch.model})")
    print(f"  Retry: {'enabled' if cfg.retry.enabled else 'disabled'} (max {cfg.retry.max_attempts} attempts)")
    print(f"  Verify: {'enabled' if cfg.verify.enabled else 'disabled'} (min confidence: {cfg.verify.min_confidence})")

    print()
    for repo_path in expand_watch_paths(cfg.paths.watch):
        repo_key = str(repo_path)
        repo_state = state.repos.get(repo_key, RepoState())
        later_path = repo_path / cfg.later_md.path

        print(f"  {repo_path.name}/")
        if repo_state.in_flight:
            print(f"    [in-flight — pid {repo_state.pid}]")
            continue
        if not later_path.exists():
            print(f"    No LATER.md")
            continue
        content = _safe_read(later_path)
        if content is None:
            print(f"    Could not read LATER.md")
            continue

        entries = parse_later_entries(content, priority_marker=cfg.later_md.priority_marker)
        selected = select_entries(entries, cfg.later_md.max_entries_per_dispatch)
        if not selected:
            print("    No pending entries")
        else:
            print(f"    Would dispatch {len(selected)}/{len(entries)} entries:")
            for entry in selected:
                marker = "[!]" if entry.is_priority else "[ ]"
                model = route_model(entry, cfg.dispatch.model, cfg.dispatch.model_routing)
                retry_info = f" (attempt {entry.attempts + 1})" if entry.attempts > 0 else ""
                print(f"      {marker} {entry.id}: {entry.text} → {model}{retry_info}")

    return 0


# ---------------------------------------------------------------------------
# Side-effect helpers
# ---------------------------------------------------------------------------

def _find_claude_binary() -> str:
    """Resolve the claude CLI binary path."""
    import shutil
    found = shutil.which("claude")
    if found:
        return found
    # Common install locations not always on PATH in subprocess environments
    for candidate in [
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path.home() / ".claude" / "bin" / "claude",
    ]:
        if candidate.exists():
            return str(candidate)
    return "claude"  # fall back, will fail at Popen


def _spawn_dispatch(
    model: str,
    repo_path: Path,
    prompt: str,
    result_path: Path,
    allow_file_writes: bool,
) -> int | None:
    claude_bin = _find_claude_binary()
    cmd = [claude_bin, "-p", prompt, "--output-format", "json", "--model", model]
    if allow_file_writes:
        cmd.append("--dangerously-skip-permissions")
    try:
        out_fh = result_path.open("w", encoding="utf-8")
    except OSError:
        return None
    try:
        proc = subprocess.Popen(
            cmd, cwd=repo_path, stdout=out_fh, stderr=subprocess.STDOUT,
            start_new_session=True, text=True,
        )
    except OSError:
        out_fh.close()
        return None
    out_fh.close()
    return proc.pid


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_hook_stdin() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    data = sys.stdin.read().strip()
    if not data:
        return {}
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _ensure_gitignore_entry(repo_path: Path, relative_entry: str) -> None:
    gitignore = repo_path / ".gitignore"
    existing = _safe_read(gitignore) or ""
    if relative_entry in existing.splitlines():
        return
    lines = existing.splitlines()
    lines.append(relative_entry)
    try:
        gitignore.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    except OSError:
        pass


def _coerce_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _coerce_optional_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None
