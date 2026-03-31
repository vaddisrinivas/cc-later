#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""cc-later Stop hook handler."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Any
from zoneinfo import ZoneInfo


APP_DIR = Path("~/.cc-later").expanduser()
CONFIG_PATH = APP_DIR / "config.toml"
RUN_LOG_PATH = APP_DIR / "run_log.jsonl"
STATE_PATH = APP_DIR / "state.json"
LOCK_PATH = APP_DIR / "handler.lock"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.toml")
DEFAULT_WINDOW_MINUTES = 300
TASK_LINE_PATTERN = re.compile(r"^(\s*-\s*)\[(.)\](\s+)(.+)$")
RESULT_LINE_PATTERN = re.compile(
    r"^(DONE|SKIPPED|NEEDS_HUMAN)(?:\s+\([^)]+\))?\s+([A-Za-z0-9_-]+)\s*:"
)


class ConfigError(Exception):
    """Raised when config is invalid."""


@dataclass
class WindowState:
    elapsed_minutes: int
    remaining_minutes: int
    total_input_tokens: int
    total_output_tokens: int
    source_path: str | None = None


@dataclass
class WindowConfig:
    trigger_at_minutes_remaining: int = 30
    idle_grace_period_minutes: int = 10
    respect_peak_hours: bool = True
    peak_windows: list[dict[str, Any]] = field(default_factory=list)
    dispatch_mode: str = "window_aware"
    fallback_dispatch_hours: list[str] = field(default_factory=list)
    jsonl_paths: list[str] = field(default_factory=list)


@dataclass
class PathsConfig:
    watch: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(
        default_factory=lambda: [
            "node_modules",
            ".git",
            "__pycache__",
            "dist",
            "build",
            ".venv",
            "vendor",
        ]
    )
    max_files_per_scan: int = 200


@dataclass
class LaterConfig:
    path: str = ".claude/LATER.md"
    auto_gitignore: bool = True
    max_entries_per_dispatch: int = 3
    mark_completed: str = "check"
    priority_marker: str = "[!]"


@dataclass
class DispatchConfig:
    enabled: bool = False
    model: str = "sonnet"
    allow_file_writes: bool = False
    max_files_written_per_task: int = 5
    prompt_template: str = ""
    output_path: str = "~/.cc-later/results/{repo}-{date}.json"


@dataclass
class SkillConfig:
    suggest_threshold: str = "balanced"
    auto_append: bool = True
    end_of_session_note: bool = False


@dataclass
class NotificationConfig:
    desktop: bool = False
    on_dispatch: bool = True
    on_complete: bool = True
    on_error: bool = True


@dataclass
class AppConfig:
    window: WindowConfig = field(default_factory=WindowConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    later_md: LaterConfig = field(default_factory=LaterConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    skill: SkillConfig = field(default_factory=SkillConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)


@dataclass
class LaterEntry:
    id: str
    text: str
    is_priority: bool
    line_index: int
    raw_line: str


@dataclass
class RepoState:
    in_flight: bool = False
    dispatch_ts: str | None = None
    result_path: str | None = None
    pid: int | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None


@dataclass
class AppState:
    last_hook_ts: str | None = None
    repos: dict[str, RepoState] = field(default_factory=dict)


class NonBlockingFileLock:
    """Atomic lock based on O_EXCL file creation."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False

        payload = {"pid": os.getpid(), "ts": datetime.now(timezone.utc).isoformat()}
        os.write(self.fd, json.dumps(payload).encode("utf-8"))
        return True

    def release(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "NonBlockingFileLock":
        acquired = self.acquire()
        if not acquired:
            raise RuntimeError(f"could not acquire lock {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.release()


def validate_config_dict(raw: dict[str, Any]) -> AppConfig:
    """Validate config with strict unknown-key rejection and defaults."""
    schema: dict[str, set[str]] = {
        "window": {
            "trigger_at_minutes_remaining",
            "idle_grace_period_minutes",
            "respect_peak_hours",
            "peak_windows",
            "dispatch_mode",
            "fallback_dispatch_hours",
            "jsonl_paths",
        },
        "paths": {"watch", "exclude_patterns", "max_files_per_scan"},
        "later_md": {
            "path",
            "auto_gitignore",
            "max_entries_per_dispatch",
            "mark_completed",
            "priority_marker",
        },
        "dispatch": {
            "enabled",
            "model",
            "allow_file_writes",
            "max_files_written_per_task",
            "prompt_template",
            "output_path",
        },
        "skill": {"suggest_threshold", "auto_append", "end_of_session_note"},
        "notifications": {"desktop", "on_dispatch", "on_complete", "on_error"},
    }

    unknown_sections = set(raw) - set(schema)
    if unknown_sections:
        bad = ", ".join(sorted(unknown_sections))
        raise ConfigError(f"Unknown config sections: {bad}")

    for section, allowed_keys in schema.items():
        candidate = raw.get(section, {})
        if candidate is None:
            candidate = {}
        if not isinstance(candidate, dict):
            raise ConfigError(f"[{section}] must be a table")
        unknown_keys = set(candidate) - allowed_keys
        if unknown_keys:
            bad = ", ".join(sorted(unknown_keys))
            raise ConfigError(f"Unknown keys in [{section}]: {bad}")

    cfg = AppConfig()
    _merge_dataclass_from_dict(cfg.window, raw.get("window", {}))
    _merge_dataclass_from_dict(cfg.paths, raw.get("paths", {}))
    _merge_dataclass_from_dict(cfg.later_md, raw.get("later_md", {}))
    _merge_dataclass_from_dict(cfg.dispatch, raw.get("dispatch", {}))
    _merge_dataclass_from_dict(cfg.skill, raw.get("skill", {}))
    _merge_dataclass_from_dict(cfg.notifications, raw.get("notifications", {}))

    if cfg.window.dispatch_mode not in {"window_aware", "time_based", "always"}:
        raise ConfigError(
            "window.dispatch_mode must be one of: window_aware, time_based, always"
        )
    if cfg.later_md.mark_completed not in {"check", "delete"}:
        raise ConfigError("later_md.mark_completed must be one of: check, delete")
    if cfg.dispatch.model not in {"sonnet", "opus"}:
        raise ConfigError("dispatch.model must be one of: sonnet, opus")
    if not isinstance(cfg.paths.watch, list):
        raise ConfigError("paths.watch must be a list")
    if not isinstance(cfg.paths.exclude_patterns, list):
        raise ConfigError("paths.exclude_patterns must be a list")
    if not isinstance(cfg.window.fallback_dispatch_hours, list):
        raise ConfigError("window.fallback_dispatch_hours must be a list")
    if not isinstance(cfg.window.jsonl_paths, list):
        raise ConfigError("window.jsonl_paths must be a list")

    return cfg


def parse_later_entries(content: str, priority_marker: str = "[!]") -> list[LaterEntry]:
    """Parse pending entries from LATER.md."""
    entries: list[LaterEntry] = []
    priority_char = _extract_marker_char(priority_marker)
    for idx, line in enumerate(content.splitlines()):
        match = TASK_LINE_PATTERN.match(line)
        if not match:
            continue
        marker = match.group(2)
        text = match.group(4).strip()
        if not text:
            continue
        if marker in {"x", "X"}:
            continue
        if marker == " ":
            is_priority = False
        elif marker == priority_char:
            is_priority = True
        else:
            continue
        task_id = _stable_task_id(idx, text)
        entries.append(
            LaterEntry(
                id=task_id,
                text=text,
                is_priority=is_priority,
                line_index=idx,
                raw_line=line,
            )
        )
    return entries


def select_entries(entries: list[LaterEntry], max_entries: int) -> list[LaterEntry]:
    if max_entries <= 0:
        return []
    ordered = sorted(entries, key=lambda e: (0 if e.is_priority else 1, e.line_index))
    return ordered[:max_entries]


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
            # Overnight window, e.g. 22:00-02:00.
            if current_minute >= start or current_minute < end:
                return True
    return False


def compute_window_state(
    jsonl_roots: list[Path], now_utc: datetime
) -> WindowState | None:
    earliest: datetime | None = None
    input_tokens = 0
    output_tokens = 0
    selected_source: str | None = None

    for root in jsonl_roots:
        if not root.exists():
            continue
        if root.is_file():
            files = [root]
        else:
            files = list(root.rglob("*.jsonl"))
        if not files:
            continue

        root_had_recent = False
        for file_path in files:
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
                usage = row.get("usage", {})
                if isinstance(usage, dict):
                    input_tokens += _coerce_int(usage.get("input_tokens"))
                    output_tokens += _coerce_int(usage.get("output_tokens"))
        if root_had_recent and selected_source is None:
            selected_source = str(root)

    if earliest is None:
        return None

    elapsed = int(max(0, (now_utc - earliest).total_seconds() // 60))
    remaining = max(0, DEFAULT_WINDOW_MINUTES - elapsed)
    return WindowState(
        elapsed_minutes=elapsed,
        remaining_minutes=remaining,
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        source_path=selected_source,
    )


def parse_result_summary(text: str) -> dict[str, str]:
    """Parse DONE/SKIPPED/NEEDS_HUMAN lines keyed by task id."""
    output: dict[str, str] = {}
    for candidate_text in _extract_text_blobs(text):
        for line in candidate_text.splitlines():
            match = RESULT_LINE_PATTERN.match(line.strip())
            if not match:
                continue
            status = match.group(1)
            task_id = match.group(2)
            output[task_id] = status
    return output


def apply_completion(
    content: str,
    done_ids: set[str],
    dispatched_entries: list[LaterEntry],
    mark_mode: str,
) -> str:
    original_lines = content.splitlines()
    targets = [entry for entry in dispatched_entries if entry.id in done_ids]
    if not targets:
        return content

    resolved: list[int] = []
    used_indexes: set[int] = set()
    for entry in targets:
        resolved_index = _resolve_entry_line_index(original_lines, entry, used_indexes)
        if resolved_index is None:
            continue
        resolved.append(resolved_index)
        used_indexes.add(resolved_index)

    if not resolved:
        return content

    lines = list(original_lines)
    if mark_mode == "delete":
        for idx in sorted(resolved, reverse=True):
            if 0 <= idx < len(lines):
                lines.pop(idx)
    elif mark_mode == "check":
        for idx in resolved:
            if 0 <= idx < len(lines):
                lines[idx] = _mark_line_done(lines[idx])
    else:
        raise ConfigError(f"Unsupported mark mode: {mark_mode}")

    rewritten = "\n".join(lines)
    if content.endswith("\n"):
        rewritten += "\n"
    return rewritten


def load_or_create_config() -> tuple[AppConfig | None, str | None]:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        shutil.copy2(DEFAULT_CONFIG_PATH, CONFIG_PATH)
        return (
            None,
            "[cc-later] First run. Config created at ~/.cc-later/config.toml\n"
            "Add your repo paths to [paths].watch to enable dispatching.",
        )

    raw = _read_toml(CONFIG_PATH)
    cfg = validate_config_dict(raw)
    return cfg, None


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
        "repos": {repo: asdict(repo_state) for repo, repo_state in state.repos.items()},
    }
    STATE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def log_event(event: str, **fields: Any) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    payload.update(fields)
    with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def main() -> int:
    lock = NonBlockingFileLock(LOCK_PATH)
    if not lock.acquire():
        print("[cc-later] Handler busy; skipping this Stop event.")
        log_event("skip", reason="lock_held")
        return 0

    try:
        hook_payload = _read_hook_stdin()
        _ = hook_payload  # currently reserved for future use
        cfg, first_run_message = load_or_create_config()
        if first_run_message:
            print(first_run_message)
            log_event("skip", reason="first_run")
            return 0
        if cfg is None:
            return 0

        state = load_state()
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone()

        completed = _reconcile_in_flight(cfg, state)
        if completed:
            log_event("complete", completed_dispatches=completed)
            _maybe_notify(cfg.notifications, "cc-later", f"Completed {completed} dispatch(es)", "on_complete")

        previous_hook_ts = _parse_iso8601(state.last_hook_ts) if state.last_hook_ts else None
        state.last_hook_ts = now_utc.isoformat()

        if not cfg.dispatch.enabled:
            save_state(state)
            log_event("skip", reason="dispatch_disabled")
            print("[cc-later] Dispatch disabled in config.")
            return 0
        if not cfg.paths.watch:
            save_state(state)
            log_event("skip", reason="empty_watch_list")
            print("[cc-later] No watched paths configured.")
            return 0
        if previous_hook_ts is not None:
            if now_utc - previous_hook_ts < timedelta(
                minutes=cfg.window.idle_grace_period_minutes
            ):
                save_state(state)
                log_event("skip", reason="idle_grace_active")
                print("[cc-later] Idle grace period active; skipping.")
                return 0

        if cfg.window.respect_peak_hours and _is_in_peak_window(now_local, cfg.window.peak_windows):
            save_state(state)
            log_event("skip", reason="peak_window")
            print("[cc-later] Peak window active; skipping.")
            return 0

        window_state: WindowState | None = None
        if cfg.window.dispatch_mode == "window_aware":
            roots = _resolve_jsonl_roots(cfg.window)
            window_state = compute_window_state(roots, now_utc=now_utc)
            if window_state is None:
                save_state(state)
                log_event("skip", reason="window_unknown", mode="window_aware")
                print(
                    "[cc-later] WARN: No JSONL files found in any known Claude data directory. "
                    "Window state unknown. Skipping dispatch. "
                    "Set dispatch_mode = \"time_based\" or \"always\" to bypass window detection."
                )
                return 0

        remaining = window_state.remaining_minutes if window_state else None
        if not should_dispatch_by_mode(
            dispatch_mode=cfg.window.dispatch_mode,
            now_local=now_local,
            fallback_dispatch_hours=cfg.window.fallback_dispatch_hours,
            remaining_minutes=remaining,
            trigger_at_minutes_remaining=cfg.window.trigger_at_minutes_remaining,
        ):
            save_state(state)
            log_event("skip", reason="mode_gate_closed", mode=cfg.window.dispatch_mode)
            print("[cc-later] Dispatch gate closed for current mode.")
            return 0

        dispatched_count = 0
        for repo_path in _expand_watch_paths(cfg.paths.watch):
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

            content = _safe_read_text(later_path)
            if content is None:
                continue
            entries = parse_later_entries(content, priority_marker=cfg.later_md.priority_marker)
            selected = select_entries(entries, cfg.later_md.max_entries_per_dispatch)
            if not selected:
                continue

            prompt = _render_prompt(repo_path, cfg, selected)
            result_path = _resolve_output_path(cfg.dispatch.output_path, repo_path, now_utc)
            pid = _spawn_dispatch(cfg, repo_path, prompt, result_path)
            if pid is None:
                log_event("error", repo=repo_key, reason="dispatch_spawn_failed")
                _maybe_notify(cfg.notifications, "cc-later", f"Dispatch failed for {repo_path.name}", "on_error")
                continue

            repo_state.in_flight = True
            repo_state.dispatch_ts = now_utc.isoformat()
            repo_state.result_path = str(result_path)
            repo_state.pid = pid
            repo_state.entries = [asdict(entry) for entry in selected]
            repo_state.model = cfg.dispatch.model
            dispatched_count += 1

            log_event(
                "dispatch",
                repo=repo_key,
                entries_dispatched=len(selected),
                entries=[entry.text for entry in selected],
                remaining_minutes=remaining,
                model=cfg.dispatch.model,
                result_path=str(result_path),
            )
            _maybe_notify(
                cfg.notifications,
                "cc-later",
                f"Dispatched {len(selected)} item(s) in {repo_path.name}",
                "on_dispatch",
            )

        save_state(state)
        if dispatched_count == 0:
            print("[cc-later] No pending LATER.md entries eligible for dispatch.")
        else:
            suffix = (
                f" ({remaining} min left)" if remaining is not None else ""
            )
            print(f"[cc-later] Dispatched {dispatched_count} repo(s){suffix}.")
        return 0
    except ConfigError as exc:
        log_event("error", reason="config_error", detail=str(exc))
        print(f"[cc-later] ERROR: {exc}")
        return 0
    except Exception as exc:  # pragma: no cover - defensive top-level safety
        log_event("error", reason="unexpected_exception", detail=str(exc))
        print(f"[cc-later] ERROR: {exc}")
        return 0
    finally:
        lock.release()


def _merge_dataclass_from_dict(target: Any, values: dict[str, Any]) -> None:
    if not isinstance(values, dict):
        raise ConfigError("Config section must be a table")
    for key, value in values.items():
        setattr(target, key, value)


def _extract_marker_char(marker: str) -> str:
    marker = marker.strip()
    if len(marker) == 3 and marker.startswith("[") and marker.endswith("]"):
        return marker[1]
    raise ConfigError(f"Invalid priority marker format: {marker}")


def _stable_task_id(line_index: int, text: str) -> str:
    digest = hashlib.sha1(f"{line_index}|{text}".encode("utf-8")).hexdigest()[:10]
    return f"t_{digest}"


def _parse_hhmm(value: str, allow_24: bool = False) -> int:
    value = value.strip()
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time format: {value}")
    hour = int(parts[0])
    minute = int(parts[1])
    if minute < 0 or minute > 59:
        raise ValueError(f"invalid minutes in time: {value}")
    if allow_24 and hour == 24 and minute == 0:
        return 24 * 60
    if hour < 0 or hour > 23:
        raise ValueError(f"invalid hour in time: {value}")
    return hour * 60 + minute


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
    candidates = [row.get("timestamp"), row.get("ts")]
    for raw in candidates:
        ts = _parse_iso8601(raw)
        if ts is not None:
            return ts
    return None


def _parse_iso8601(raw: Any) -> datetime | None:
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


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


def _extract_text_blobs(raw_text: str) -> list[str]:
    blobs = [raw_text]
    raw_text = raw_text.strip()
    if not raw_text:
        return blobs
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return blobs

    extracted: list[str] = []
    _walk_json_for_text(payload, extracted)
    if extracted:
        blobs.extend(extracted)
    return blobs


def _walk_json_for_text(node: Any, sink: list[str]) -> None:
    if isinstance(node, str):
        sink.append(node)
        return
    if isinstance(node, dict):
        for value in node.values():
            _walk_json_for_text(value, sink)
        return
    if isinstance(node, list):
        for value in node:
            _walk_json_for_text(value, sink)


def _resolve_entry_line_index(
    lines: list[str], entry: LaterEntry, used_indexes: set[int]
) -> int | None:
    if (
        0 <= entry.line_index < len(lines)
        and entry.line_index not in used_indexes
        and _line_text_matches(lines[entry.line_index], entry.text)
    ):
        return entry.line_index

    for idx, line in enumerate(lines):
        if idx in used_indexes:
            continue
        if _line_text_matches(line, entry.text):
            return idx
    return None


def _line_text_matches(line: str, expected_text: str) -> bool:
    parsed = TASK_LINE_PATTERN.match(line)
    if not parsed:
        return False
    marker = parsed.group(2)
    if marker in {"x", "X"}:
        return False
    text = parsed.group(4).strip()
    return text == expected_text


def _mark_line_done(line: str) -> str:
    return TASK_LINE_PATTERN.sub(r"\1[x]\3\4", line, count=1)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            payload = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"Unable to read config: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in config: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("Config root must be a table")
    return payload


def _read_hook_stdin() -> dict[str, Any]:
    data = sys.stdin.read().strip()
    if not data:
        return {}
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _reconcile_in_flight(cfg: AppConfig, state: AppState) -> int:
    completed = 0
    for repo_key, repo_state in state.repos.items():
        if not repo_state.in_flight:
            continue
        if repo_state.pid and _is_process_alive(repo_state.pid):
            continue
        result_path = Path(repo_state.result_path).expanduser() if repo_state.result_path else None
        if result_path is None or not result_path.exists():
            repo_state.in_flight = False
            repo_state.pid = None
            repo_state.result_path = None
            repo_state.entries = []
            continue

        raw = _safe_read_text(result_path)
        if raw is None:
            continue
        summary = parse_result_summary(raw)
        done_ids = {task_id for task_id, status in summary.items() if status == "DONE"}

        if done_ids and repo_state.entries:
            later_path = Path(repo_key) / cfg.later_md.path
            content = _safe_read_text(later_path)
            if content is not None:
                dispatched_entries = [
                    LaterEntry(
                        id=str(entry.get("id", "")),
                        text=str(entry.get("text", "")),
                        is_priority=bool(entry.get("is_priority", False)),
                        line_index=int(entry.get("line_index", 0)),
                        raw_line=str(entry.get("raw_line", "")),
                    )
                    for entry in repo_state.entries
                    if isinstance(entry, dict)
                ]
                updated = apply_completion(
                    content=content,
                    done_ids=done_ids,
                    dispatched_entries=dispatched_entries,
                    mark_mode=cfg.later_md.mark_completed,
                )
                if updated != content:
                    later_path.parent.mkdir(parents=True, exist_ok=True)
                    later_path.write_text(updated, encoding="utf-8")

        repo_state.in_flight = False
        repo_state.pid = None
        repo_state.result_path = None
        repo_state.entries = []
        completed += 1
    return completed


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


def _is_in_peak_window(now_local: datetime, windows: list[dict[str, Any]]) -> bool:
    weekday_map = {
        "mon": 0,
        "tue": 1,
        "wed": 2,
        "thu": 3,
        "fri": 4,
        "sat": 5,
        "sun": 6,
    }
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


def _resolve_jsonl_roots(window_cfg: WindowConfig) -> list[Path]:
    if window_cfg.jsonl_paths:
        return [Path(path).expanduser() for path in window_cfg.jsonl_paths]

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
        if key in seen:
            continue
        seen.add(key)
        roots.append(candidate)
    return roots


def _expand_watch_paths(paths: list[str]) -> list[Path]:
    expanded: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str):
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = candidate.resolve()
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        expanded.append(candidate)
    return expanded


def _render_prompt(repo_path: Path, cfg: AppConfig, entries: list[LaterEntry]) -> str:
    entry_block = "\n".join(f"- {entry.id}: {entry.text}" for entry in entries)
    if cfg.dispatch.allow_file_writes:
        write_instruction = (
            "You may edit files in this repository. "
            f"Maximum {cfg.dispatch.max_files_written_per_task} files."
        )
    else:
        write_instruction = "Do not modify files. Report findings and proposed fixes only."

    replacements = {
        "repo_path": str(repo_path),
        "entries": entry_block,
        "max_files": str(cfg.dispatch.max_files_written_per_task),
        "write_instruction": write_instruction,
    }

    if cfg.dispatch.prompt_template:
        template_path = Path(cfg.dispatch.prompt_template).expanduser()
        if not template_path.is_absolute():
            template_path = CONFIG_PATH.parent / template_path
        template_text = _safe_read_text(template_path)
        if template_text:
            try:
                return template_text.format(**replacements)
            except KeyError as exc:
                raise ConfigError(f"Unknown placeholder in prompt template: {exc}") from exc

    return (
        "You are running as a background maintenance agent.\n"
        f"Repository: {repo_path}\n\n"
        "The following LATER.md items were selected:\n\n"
        f"{entry_block}\n\n"
        "Instructions:\n"
        "- Address each item using repository context.\n"
        "- Be surgical and do not refactor unrelated code.\n"
        f"- Maximum {cfg.dispatch.max_files_written_per_task} files total.\n"
        f"- {write_instruction}\n\n"
        "When finished, output one summary line per item using this format:\n"
        "DONE <id>: <item text>\n"
        "SKIPPED (<reason>) <id>: <item text>\n"
        "NEEDS_HUMAN (<reason>) <id>: <item text>\n"
    )


def _resolve_output_path(template: str, repo_path: Path, now_utc: datetime) -> Path:
    repo_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_path.name) or "repo"
    date_slug = now_utc.strftime("%Y%m%d-%H%M%S")
    rendered = template.format(repo=repo_slug, date=date_slug)
    result = Path(rendered).expanduser()
    if not result.is_absolute():
        result = APP_DIR / result
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


def _spawn_dispatch(cfg: AppConfig, repo_path: Path, prompt: str, result_path: Path) -> int | None:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        cfg.dispatch.model,
    ]
    if cfg.dispatch.allow_file_writes:
        cmd.append("--dangerously-skip-permissions")

    try:
        out_fh = result_path.open("w", encoding="utf-8")
    except OSError:
        return None

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=repo_path,
            stdout=out_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    except OSError:
        out_fh.close()
        return None

    out_fh.close()
    return proc.pid


def _safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _ensure_gitignore_entry(repo_path: Path, relative_entry: str) -> None:
    gitignore = repo_path / ".gitignore"
    existing = _safe_read_text(gitignore) or ""
    lines = existing.splitlines()
    if relative_entry in lines:
        return
    lines.append(relative_entry)
    content = "\n".join(lines).strip() + "\n"
    try:
        gitignore.write_text(content, encoding="utf-8")
    except OSError:
        return


def _maybe_notify(
    cfg: NotificationConfig,
    title: str,
    message: str,
    channel: str,
) -> None:
    if not cfg.desktop:
        return
    enabled = getattr(cfg, channel, False)
    if not enabled:
        return

    system = platform.system()
    if system == "Darwin":
        cmd = [
            "osascript",
            "-e",
            f'display notification "{message}" with title "{title}"',
        ]
    elif system == "Linux":
        cmd = ["notify-send", title, message]
    else:
        return

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
