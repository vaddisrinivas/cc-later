"""Window state computation, JSONL reading, budget tracking, and time utilities."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    BudgetState,
    ConfigError,
    CONTEXT_WINDOW_TOKENS,
    DEFAULT_WINDOW_MINUTES,
    WindowConfig,
    WindowState,
)


def compute_window_state(
    jsonl_roots: list[Path],
    now_utc: datetime,
    session_id: str | None = None,
) -> WindowState | None:
    """Compute window state from JSONL files."""
    earliest: datetime | None = None
    input_tokens = 0
    output_tokens = 0
    selected_source: str | None = None
    matched_session_id: str | None = None

    for root in jsonl_roots:
        if not root.exists():
            continue
        files = [root] if root.is_file() else list(root.rglob("*.jsonl"))
        if not files:
            continue

        root_had_recent = False
        for file_path in files:
            if session_id is not None:
                if session_id not in file_path.stem and session_id not in str(file_path):
                    continue
            try:
                mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if now_utc - mtime > timedelta(hours=5):
                continue
            root_had_recent = True
            for row in _iter_jsonl(file_path):
                ts = _extract_timestamp(row)
                if ts is None:
                    continue
                if earliest is None or ts < earliest:
                    earliest = ts
                usage = row.get("message_usage") or row.get("usage") or {}
                if isinstance(usage, dict):
                    input_tokens += _coerce_int(usage.get("input_tokens"))
                    input_tokens += _coerce_int(usage.get("cache_creation_input_tokens"))
                    output_tokens += _coerce_int(usage.get("output_tokens"))
                if matched_session_id is None:
                    sid = row.get("sessionId") or row.get("session_id")
                    if sid:
                        matched_session_id = str(sid)
        if root_had_recent and selected_source is None:
            selected_source = str(root)

    if earliest is None:
        return None

    elapsed = int(max(0, (now_utc - earliest).total_seconds() // 60))
    remaining = max(0, DEFAULT_WINDOW_MINUTES - elapsed)
    total_tokens = input_tokens + output_tokens
    context_pct = min(1.0, total_tokens / CONTEXT_WINDOW_TOKENS)
    return WindowState(
        elapsed_minutes=elapsed,
        remaining_minutes=remaining,
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        context_pct_used=context_pct,
        session_id=matched_session_id or session_id,
        source_path=selected_source,
    )


def compute_budget_state(
    jsonl_roots: list[Path],
    now_utc: datetime,
    weekly_budget: int,
) -> BudgetState:
    """Sum tokens across all JSONL files from the last 7 days."""
    cutoff = now_utc - timedelta(days=7)
    total_tokens = 0

    for root in jsonl_roots:
        if not root.exists():
            continue
        files = [root] if root.is_file() else list(root.rglob("*.jsonl"))
        for file_path in files:
            try:
                mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            for row in _iter_jsonl(file_path):
                usage = row.get("message_usage") or row.get("usage") or {}
                if isinstance(usage, dict):
                    total_tokens += _coerce_int(usage.get("input_tokens"))
                    total_tokens += _coerce_int(usage.get("cache_creation_input_tokens"))
                    total_tokens += _coerce_int(usage.get("output_tokens"))

    budget = max(1, weekly_budget)
    pct = min(1.0, total_tokens / budget)
    return BudgetState(
        tokens_used_this_week=total_tokens,
        weekly_budget=weekly_budget,
        pct_used=pct,
        tokens_remaining=max(0, weekly_budget - total_tokens),
    )


def resolve_trigger_threshold(
    now_local: datetime,
    trigger_at_minutes_remaining: int,
    trigger_schedules: list[dict[str, Any]],
    trigger_schedules_enabled: bool,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> int:
    """Return the effective trigger threshold in minutes for the current time.

    If trigger_schedules_enabled and a schedule matches the current time,
    convert its remaining_pct to minutes. Otherwise return the default.
    """
    if not trigger_schedules_enabled or not trigger_schedules:
        return trigger_at_minutes_remaining

    current_minute = now_local.hour * 60 + now_local.minute
    for schedule in trigger_schedules:
        if not isinstance(schedule, dict):
            continue
        hours = schedule.get("hours", "")
        if not isinstance(hours, str) or "-" not in hours:
            continue
        remaining_pct = schedule.get("remaining_pct")
        if not isinstance(remaining_pct, (int, float)) or remaining_pct < 0:
            continue

        start_s, end_s = hours.split("-", 1)
        try:
            start = _parse_hhmm(start_s)
            end = _parse_hhmm(end_s, allow_24=True)
        except ValueError:
            continue

        matched = False
        if start == end:
            continue
        if start < end:
            matched = start <= current_minute < end
        else:
            # Overnight window
            matched = current_minute >= start or current_minute < end

        if matched:
            return int(window_minutes * remaining_pct / 100)

    return trigger_at_minutes_remaining


def should_dispatch_by_mode(
    dispatch_mode: str,
    now_local: datetime,
    fallback_dispatch_hours: list[str],
    remaining_minutes: int | None,
    trigger_at_minutes_remaining: int,
) -> bool:
    if dispatch_mode == "always":
        return True
    if dispatch_mode == "time_based":
        return is_within_time_ranges(now_local, fallback_dispatch_hours)
    if dispatch_mode == "window_aware":
        if remaining_minutes is None:
            return False
        return remaining_minutes <= trigger_at_minutes_remaining
    raise ConfigError(f"Unsupported dispatch mode: {dispatch_mode}")


def is_within_time_ranges(now_local: datetime, ranges: list[str]) -> bool:
    current_minute = now_local.hour * 60 + now_local.minute
    for window in ranges:
        if not isinstance(window, str) or "-" not in window:
            continue
        start_s, end_s = window.split("-", 1)
        try:
            start = _parse_hhmm(start_s)
            end = _parse_hhmm(end_s, allow_24=True)
        except ValueError:
            continue
        if start == end:
            continue
        if start < end:
            if start <= current_minute < end:
                return True
        else:
            if current_minute >= start or current_minute < end:
                return True
    return False


def is_in_peak_window(now_local: datetime, windows: list[dict[str, Any]]) -> bool:
    weekday_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    for window in windows:
        if not isinstance(window, dict):
            continue
        tz_name = window.get("tz")
        local_now = now_local
        if isinstance(tz_name, str) and tz_name:
            try:
                local_now = now_local.astimezone(ZoneInfo(tz_name))
            except Exception:
                continue
        days = _expand_days(window.get("days"), weekday_map)
        if local_now.weekday() not in days:
            continue
        start_raw = window.get("start")
        end_raw = window.get("end")
        if not isinstance(start_raw, str) or not isinstance(end_raw, str):
            continue
        try:
            start = _parse_hhmm(start_raw)
            end = _parse_hhmm(end_raw, allow_24=True)
        except ValueError:
            continue
        current = local_now.hour * 60 + local_now.minute
        if start < end:
            if start <= current < end:
                return True
        elif start > end:
            if current >= start or current < end:
                return True
    return False


def resolve_jsonl_roots(window_cfg: WindowConfig) -> list[Path]:
    if window_cfg.jsonl_paths:
        return [Path(p).expanduser() for p in window_cfg.jsonl_paths]
    roots: list[Path] = []
    seen: set[str] = set()
    env_root = os.environ.get("CLAUDE_CONFIG_DIR")
    candidates = []
    if env_root:
        candidates.append(Path(env_root).expanduser() / "projects")
    candidates.append(Path("~/.config/claude/projects").expanduser())
    candidates.append(Path("~/.claude/projects").expanduser())
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            roots.append(candidate)
    return roots


def expand_watch_paths(paths: list[str]) -> list[Path]:
    expanded: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str):
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = candidate.resolve()
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            expanded.append(candidate)
    return expanded


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_hhmm(value: str, allow_24: bool = False) -> int:
    value = value.strip()
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time format: {value}")
    hour = int(parts[0])
    minute = int(parts[1])
    if minute < 0 or minute > 59:
        raise ValueError(f"invalid minutes: {value}")
    if allow_24 and hour == 24 and minute == 0:
        return 24 * 60
    if hour < 0 or hour > 23:
        raise ValueError(f"invalid hour: {value}")
    return hour * 60 + minute


def _expand_days(value: Any, mapping: dict[str, int]) -> set[int]:
    if not isinstance(value, str):
        return set(range(7))
    tokens = [part.strip().lower() for part in value.split(",") if part.strip()]
    days: set[int] = set()
    for token in tokens:
        if "-" in token:
            start_s, end_s = token.split("-", 1)
            if start_s not in mapping or end_s not in mapping:
                continue
            start = mapping[start_s]
            end = mapping[end_s]
            current = start
            while True:
                days.add(current)
                if current == end:
                    break
                current = (current + 1) % 7
        else:
            day = mapping.get(token)
            if day is not None:
                days.add(day)
    return days if days else set(range(7))


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
    except OSError:
        return []
    return rows


def _extract_timestamp(row: dict[str, Any]) -> datetime | None:
    for raw in [row.get("timestamp"), row.get("ts")]:
        ts = parse_iso8601(raw)
        if ts is not None:
            return ts
    return None


def parse_iso8601(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0
