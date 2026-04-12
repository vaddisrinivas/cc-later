from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import pendulum
from filelock import FileLock
from pydantic import BaseModel, Field, field_validator

APP_DIR_ENV = "CC_LATER_APP_DIR"
DEFAULT_WINDOW_MINUTES = 300
PLAN_LIMITS: dict[str, dict[str, Any]] = {
    "free":       {"window_minutes": 300, "context_k": 200, "models": ["sonnet", "haiku"]},
    "pro":        {"window_minutes": 300, "context_k": 200, "models": ["opus", "sonnet", "haiku"]},
    "max":        {"window_minutes": 300, "context_k": 200, "models": ["opus", "sonnet", "haiku"], "extended_thinking": True},
    "team":       {"window_minutes": 300, "context_k": 200, "models": ["opus", "sonnet", "haiku"]},
    "enterprise": {"window_minutes": 300, "context_k": 200, "models": ["opus", "sonnet", "haiku"]},
    # All plans use a 5-hour (300m) rolling window per Anthropic docs.
    # Enterprise/usage-based plans may differ — override with WINDOW_DURATION_MINUTES.
}
# Compat alias: dict[str, int] mapping plan -> window_minutes
PLAN_WINDOW_MINUTES: dict[str, int] = {k: v["window_minutes"] for k, v in PLAN_LIMITS.items()}
LIMIT_MARKERS = (
    "rate limit",
    "usage limit",
    "quota",
    "too many requests",
    "too many messages",
    "429",
    "5-hour window",
    "window exhausted",
    "try again later",
    "exceeded your current quota",
)
_MAX_STDIN_BYTES = 1_048_576  # 1MB cap on hook stdin payloads
TASK_RE = re.compile(
    r"^(?P<prefix>\s*-\s*)\[(?P<mark>[ xX!])\](?P<space>\s*)(?:(?P<prio>\(P[0-2]\))\s*)?(?P<text>.+?)\s*$"
)
RESULT_RE = re.compile(r"^(DONE|SKIPPED|NEEDS_HUMAN|FAILED)(?:\s+\([^)]+\))?\s+([A-Za-z0-9_-]+)\s*:")
CAPTURE_RE = re.compile(
    r"(?i)(?:later|add\s+(?:this\s+)?to\s+later|note\s+(?:this\s+)?for\s+later|"
    r"queue\s+(?:this\s+)?for\s+later|for\s+later)\s*(\[!\])?\s*:\s*(.+?)(?=$|\n)"
)


class PathsConfig(BaseModel):
    watch: list[str] = []


class LaterConfig(BaseModel):
    path: str = ".claude/LATER.md"
    max_entries_per_dispatch: int = Field(default=3, gt=0)
    auto_gitignore: bool = True

    @field_validator("path")
    @classmethod
    def _relative_path(cls, v: str) -> str:
        if os.path.isabs(v):
            raise ValueError("later.path must be a relative path (joined with repo root), not absolute")
        return v


class DispatchConfig(BaseModel):
    enabled: bool = True
    model: Literal["sonnet", "opus", "haiku"] = "sonnet"
    allow_file_writes: bool = False
    output_path: str = "~/.cc-later/results/{repo}-{date}.json"


class WindowConfig(BaseModel):
    dispatch_mode: Literal["window_aware", "time_based", "always"] = "window_aware"
    duration_minutes: int = Field(default=DEFAULT_WINDOW_MINUTES, gt=0)
    trigger_at_minutes_remaining: int = 30
    idle_grace_period_minutes: int = 10
    fallback_dispatch_hours: list[str] = []
    jsonl_paths: list[str] = []


class LimitsConfig(BaseModel):
    weekly_budget_tokens: int = Field(default=10_000_000, gt=0)
    backoff_at_pct: int = Field(default=80, ge=0, le=100)


class AutoResumeConfig(BaseModel):
    enabled: bool = True
    min_remaining_minutes: int = Field(default=240, ge=0)


class CompactConfig(BaseModel):
    enabled: bool = True


class NudgeConfig(BaseModel):
    enabled: bool = True
    stale_minutes: int = 10
    max_retries: int = 2


class MonitorConfig(BaseModel):
    warn_window_minutes: int = Field(default=60, ge=0)
    warn_budget_pct: int = Field(default=70, ge=0, le=100)
    notify_enabled: bool = True
    query_claude: bool = False


class Config(BaseModel):
    plan: str = "max"
    paths: PathsConfig = Field(default_factory=PathsConfig)
    later: LaterConfig = Field(default_factory=LaterConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    window: WindowConfig = Field(default_factory=WindowConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    auto_resume: AutoResumeConfig = Field(default_factory=AutoResumeConfig)
    compact: CompactConfig = Field(default_factory=CompactConfig)
    nudge: NudgeConfig = Field(default_factory=NudgeConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)

    @field_validator("plan")
    @classmethod
    def _valid_plan(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in PLAN_WINDOW_MINUTES:
            raise ValueError(f"plan must be one of: {', '.join(sorted(PLAN_WINDOW_MINUTES))}")
        return v


@dataclass
class Task:
    id: str
    text: str
    priority: str
    line_index: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Task":
        return cls(
            id=str(raw.get("id", "")),
            text=str(raw.get("text", "")),
            priority=str(raw.get("priority", "P1")),
            line_index=int(raw.get("line_index", 0)),
        )


@dataclass
class Section:
    name: str
    tasks: list[Task]


@dataclass
class RepoState:
    in_flight: bool = False
    agents: list[dict[str, Any]] = field(default_factory=list)
    resume_entries: list[dict[str, Any]] = field(default_factory=list)
    resume_reason: str | None = None
    dispatch_ts: str | None = None


@dataclass
class State:
    last_hook_ts: str | None = None
    window_start_ts: str | None = None   # set when we detect a fresh window (e.g. after resume dispatch)
    window_limit_ts: str | None = None   # set when we detect limit exhaustion
    repos: dict[str, RepoState] = field(default_factory=dict)


@dataclass
class WindowState:
    elapsed_minutes: int
    remaining_minutes: int
    total_input_tokens: int
    total_output_tokens: int
    burn_rate_tpm: int = 0
    projected_exhaustion_minutes: int | None = None


@dataclass
class BudgetState:
    used_tokens: int
    pct_used: float


@dataclass
class UsageInfo:
    """Live usage data scraped from Claude CLI /usage TUI."""
    session_pct: int | None = None        # % of session window used
    session_reset: str | None = None      # reset time string, e.g. "1pm"
    weekly_pct: int | None = None         # % of weekly budget used
    weekly_reset: str | None = None       # weekly reset string
    extra_usage_usd: float | None = None  # overage in USD


@dataclass
class MonitorSnapshot:
    ts: str
    window: WindowState | None
    budget: BudgetState
    plan: str
    plan_limits: dict[str, Any]
    agents_in_flight: int
    agents_stale: int
    limit_events_24h: dict[str, int]
    usage_info: UsageInfo | None = None


def app_dir() -> Path:
    return Path(os.environ.get(APP_DIR_ENV, "~/.cc-later")).expanduser()


def config_path() -> Path:
    return app_dir() / "config.env"


def state_path() -> Path:
    return app_dir() / "state.json"


def _flock(name: str) -> FileLock:
    """Process-level advisory lock scoped to app_dir.

    Prevents concurrent hooks (e.g. two Claude sessions) from corrupting
    shared resources like state.json or running simultaneous git merges.
    Cross-platform (works on macOS, Linux, Windows, NFS).
    """
    app_dir().mkdir(parents=True, exist_ok=True)
    return FileLock(app_dir() / f".{name}.lock", timeout=10)


def run_log_path() -> Path:
    return app_dir() / "run_log.jsonl"


def default_config_template_path() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts" / "default_config.env"


def _rotate_log_if_needed(log_path: Path, max_bytes: int = 1_000_000) -> None:
    """Rotate run_log.jsonl when it exceeds max_bytes (~1MB)."""
    try:
        if log_path.exists() and log_path.stat().st_size > max_bytes:
            rotated = log_path.with_suffix(".jsonl.1")
            log_path.replace(rotated)
    except OSError:
        pass


def log_event(event: str, **fields: Any) -> None:
    try:
        app_dir().mkdir(parents=True, exist_ok=True)
        log_path = run_log_path()
        _rotate_log_if_needed(log_path)
        payload = {"ts": pendulum.now("UTC").isoformat(), "event": event}
        payload.update(fields)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        pass


_MAX_CONFIG_BYTES = 64 * 1024  # 64KB — config files should be tiny

# Env var names kept for backward compatibility with existing config.env files.
_BOOL_TRUTHY = {"true", "1", "yes"}


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result
    if len(raw) > _MAX_CONFIG_BYTES:
        raw = raw[:_MAX_CONFIG_BYTES]
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip()
    return result


def _parse_bool(val: str) -> bool:
    return val.strip().lower() in _BOOL_TRUTHY


def _parse_list(val: str) -> list[str]:
    val = val.strip()
    if not val:
        return []
    return [item.strip() for item in val.split(",") if item.strip()]


def _safe_int(val: str, default: int) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def load_config() -> Config:
    app_dir().mkdir(parents=True, exist_ok=True)
    path = config_path()
    if not path.exists():
        shutil.copy2(default_config_template_path(), path)
        log_event("config_created", path=str(path))

    raw = _read_env(path)

    plan = raw.get("PLAN", "max").strip().lower()
    _dur_raw = raw.get("WINDOW_DURATION_MINUTES", "").strip()
    if _dur_raw and _dur_raw.lower() != "auto":
        duration = _safe_int(_dur_raw, DEFAULT_WINDOW_MINUTES)
    else:
        duration = PLAN_WINDOW_MINUTES.get(plan, DEFAULT_WINDOW_MINUTES)

    return Config(
        plan=plan,
        paths=PathsConfig(watch=_parse_list(raw.get("PATHS_WATCH", ""))),
        later=LaterConfig(
            path=raw.get("LATER_PATH", ".claude/LATER.md"),
            max_entries_per_dispatch=_safe_int(raw.get("LATER_MAX_ENTRIES_PER_DISPATCH", ""), 3),
            auto_gitignore=_parse_bool(raw.get("LATER_AUTO_GITIGNORE", "true")),
        ),
        dispatch=DispatchConfig(
            enabled=_parse_bool(raw.get("DISPATCH_ENABLED", "true")),
            model=raw.get("DISPATCH_MODEL", "sonnet"),
            allow_file_writes=_parse_bool(raw.get("DISPATCH_ALLOW_FILE_WRITES", "false")),
            output_path=raw.get("DISPATCH_OUTPUT_PATH", "~/.cc-later/results/{repo}-{date}.json"),
        ),
        window=WindowConfig(
            dispatch_mode=raw.get("WINDOW_DISPATCH_MODE", "window_aware"),
            duration_minutes=duration,
            trigger_at_minutes_remaining=_safe_int(raw.get("WINDOW_TRIGGER_AT_MINUTES_REMAINING", ""), 30),
            idle_grace_period_minutes=_safe_int(raw.get("WINDOW_IDLE_GRACE_PERIOD_MINUTES", ""), 10),
            fallback_dispatch_hours=_parse_list(raw.get("WINDOW_FALLBACK_DISPATCH_HOURS", "")),
            jsonl_paths=_parse_list(raw.get("WINDOW_JSONL_PATHS", "")),
        ),
        limits=LimitsConfig(
            weekly_budget_tokens=_safe_int(raw.get("LIMITS_WEEKLY_BUDGET_TOKENS", ""), 10_000_000),
            backoff_at_pct=_safe_int(raw.get("LIMITS_BACKOFF_AT_PCT", ""), 80),
        ),
        auto_resume=AutoResumeConfig(
            enabled=_parse_bool(raw.get("AUTO_RESUME_ENABLED", "true")),
            min_remaining_minutes=_safe_int(raw.get("AUTO_RESUME_MIN_REMAINING_MINUTES", ""), 240),
        ),
        compact=CompactConfig(enabled=_parse_bool(raw.get("COMPACT_ENABLED", "true"))),
        nudge=NudgeConfig(
            enabled=_parse_bool(raw.get("NUDGE_ENABLED", "true")),
            stale_minutes=_safe_int(raw.get("NUDGE_STALE_MINUTES", ""), 10),
            max_retries=_safe_int(raw.get("NUDGE_MAX_RETRIES", ""), 2),
        ),
        monitor=MonitorConfig(
            warn_window_minutes=_safe_int(raw.get("MONITOR_WARN_WINDOW_MINUTES", ""), 60),
            warn_budget_pct=_safe_int(raw.get("MONITOR_WARN_BUDGET_PCT", ""), 70),
            notify_enabled=_parse_bool(raw.get("MONITOR_NOTIFY_ENABLED", "true")),
            query_claude=_parse_bool(raw.get("MONITOR_QUERY_CLAUDE", "false")),
        ),
    )


def _coerce_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


_MAX_SAFE_INT = 2**53  # JSON safe integer range


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value if abs(value) <= _MAX_SAFE_INT else None
    if isinstance(value, float):
        if abs(value) > _MAX_SAFE_INT or value != value:  # NaN check
            return None
        return int(value)
    return None


def _parse_iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = pendulum.parse(raw, strict=False)
    except Exception:
        return None
    return dt.in_tz("UTC")


def load_state() -> State:
    path = state_path()
    if not path.exists():
        return State()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return State()

    repos: dict[str, RepoState] = {}
    raw_repos = payload.get("repos", {})
    if isinstance(raw_repos, dict):
        for repo_key, raw_repo in raw_repos.items():
            if not isinstance(raw_repo, dict):
                continue
            agents = raw_repo.get("agents", []) if isinstance(raw_repo.get("agents"), list) else []
            repos[repo_key] = RepoState(
                in_flight=bool(raw_repo.get("in_flight", False)),
                agents=agents,
                resume_entries=raw_repo.get("resume_entries", []) if isinstance(raw_repo.get("resume_entries"), list) else [],
                resume_reason=_coerce_str(raw_repo.get("resume_reason")),
                dispatch_ts=_coerce_str(raw_repo.get("dispatch_ts")),
            )
    return State(
        last_hook_ts=_coerce_str(payload.get("last_hook_ts")),
        window_start_ts=_coerce_str(payload.get("window_start_ts")),
        window_limit_ts=_coerce_str(payload.get("window_limit_ts")),
        repos=repos,
    )


def save_state(state: State) -> None:
    app_dir().mkdir(parents=True, exist_ok=True)
    payload = {
        "last_hook_ts": state.last_hook_ts,
        "window_start_ts": state.window_start_ts,
        "window_limit_ts": state.window_limit_ts,
        "repos": {repo: asdict(repo_state) for repo, repo_state in state.repos.items()},
    }
    # Atomic write under advisory lock to prevent corruption from
    # concurrent hooks (e.g. two Claude sessions stopping simultaneously).
    with _flock("state"):
        target = state_path()
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)


def _safe_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _repo_root_from(path: Path) -> Path:
    current = path.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def resolve_watch_paths(cfg: Config, cwd_hint: str | None = None) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for raw in cfg.paths.watch:
        if not isinstance(raw, str) or not raw.strip():
            continue
        p = _repo_root_from(Path(raw).expanduser())
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    if out:
        return out
    cwd = Path(cwd_hint).expanduser() if isinstance(cwd_hint, str) and cwd_hint else Path.cwd()
    p = _repo_root_from(cwd)
    log_event("auto_watch", repo=str(p))
    return [p]


def _atomic_write(path: Path, data: str) -> None:
    """Write data to path atomically via a temp file + rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def ensure_later_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    _atomic_write(
        path,
        "# LATER\n\n"
        "Use this format:\n"
        "- [ ] (P1) concise actionable task\n"
        "- [ ] (P0) urgent production/security task\n"
        "- [x] completed task\n\n"
        "## Queue\n",
    )


def stable_task_id(line_index: int, text: str | None) -> str:
    text = text or ""
    digest = hashlib.sha1(f"{line_index}:{text}".encode("utf-8")).hexdigest()[:10]
    return f"t_{digest}"


_MAX_LATER_LINES = 10_000


def parse_tasks(content: str) -> list[Section]:
    sections: list[Section] = []
    current_name = ""
    current_tasks: list[Task] = []
    for idx, line in enumerate(content.splitlines()[:_MAX_LATER_LINES]):
        header = re.match(r"^##\s+(.+)", line)
        if header:
            if current_tasks:
                sections.append(Section(name=current_name, tasks=current_tasks))
                current_tasks = []
            current_name = header.group(1).strip()
            continue
        m = TASK_RE.match(line)
        if not m:
            continue
        text = (m.group("text") or "").strip()
        mark = m.group("mark")
        if not text or mark in {"x", "X"}:
            continue
        prio = "P0" if mark == "!" else ((m.group("prio") or "(P1)").strip("()"))
        current_tasks.append(Task(id=stable_task_id(idx, text), text=text, priority=prio, line_index=idx))
    if current_tasks:
        sections.append(Section(name=current_name, tasks=current_tasks))
    return sections


def select_tasks(section: Section, limit: int) -> list[Task]:
    rank = {"P0": 0, "P1": 1, "P2": 2}
    return sorted(section.tasks, key=lambda t: (rank.get(t.priority, 1), t.line_index, t.text))[:limit]


def mark_done_in_content(content: str, done_ids: set[str]) -> str:
    out: list[str] = []
    for idx, line in enumerate(content.splitlines()):
        m = TASK_RE.match(line)
        if not m:
            out.append(line)
            continue
        text = (m.group("text") or "").strip()
        task_id = stable_task_id(idx, text)
        if task_id not in done_ids:
            out.append(line)
            continue
        prio = m.group("prio") or ("(P0)" if m.group("mark") == "!" else "")
        out.append(f"{m.group('prefix')}[x] {prio + ' ' if prio else ''}{text}".rstrip())
    # Detect the line ending style used in the original content.
    line_sep = "\r\n" if "\r\n" in content else "\n"
    data = line_sep.join(out)
    if content.endswith(("\n", "\r\n")):
        data += line_sep
    return data


def parse_result_summary(raw: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for line in raw.splitlines():
        m = RESULT_RE.match(line.strip())
        if m:
            statuses[m.group(2)] = m.group(1)
    return statuses


def detect_limit_exhaustion(raw: str) -> str | None:
    return "limit_exhausted" if any(marker in raw.lower() for marker in LIMIT_MARKERS) else None


_MAX_JSONL_LINE_BYTES = 10 * 1024 * 1024  # 10MB — skip corrupt/oversized lines


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if len(line) > _MAX_JSONL_LINE_BYTES:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _row_timestamp(row: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "ts", "created_at", "time"):
        dt = _parse_iso(row.get(key))
        if dt is not None:
            return dt
    return None


def _as_int(value: Any) -> int:
    """Coerce a JSON value to an integer. Returns 0 for non-numeric types."""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def resolve_jsonl_roots(cfg: Config) -> list[Path]:
    if cfg.window.jsonl_paths:
        return [Path(p).expanduser() for p in cfg.window.jsonl_paths if isinstance(p, str)]
    candidates: list[Path] = []
    env_root = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_root:
        candidates.append(Path(env_root).expanduser() / "projects")
    candidates.extend([Path("~/.config/claude/projects").expanduser(), Path("~/.claude/projects").expanduser()])
    seen: set[str] = set()
    out: list[Path] = []
    for p in candidates:
        k = str(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _jsonl_files(root: Path, recursive: bool = False) -> list[Path]:
    """Return JSONL files under root.

    By default only returns top-level session files (non-recursive) to avoid
    pulling in subagent logs that skew window/budget calculations.
    Use recursive=True when you genuinely need all files (e.g. budget totals).
    """
    try:
        if not root.exists():
            return []
        if root.is_file():
            return [root]
    except OSError:
        return []
    # Top-level: only direct children of each project directory
    try:
        if recursive:
            return sorted(root.rglob("*.jsonl"))
        results: list[Path] = []
        for project_dir in sorted(root.iterdir()):
            try:
                if project_dir.is_dir():
                    results.extend(sorted(project_dir.glob("*.jsonl")))
                elif project_dir.suffix == ".jsonl":
                    results.append(project_dir)
            except OSError:
                continue
        return results
    except OSError:
        return []


def compute_window_state(roots: list[Path], now_utc: datetime, session_id: str | None = None, session_gap_minutes: int = 30, window_duration: int = DEFAULT_WINDOW_MINUTES, window_start_hint: datetime | None = None) -> WindowState | None:
    try:
        return _compute_window_state_inner(roots, now_utc, session_id, session_gap_minutes, window_duration, window_start_hint)
    except Exception:
        return None


def _compute_window_state_inner(roots: list[Path], now_utc: datetime, session_id: str | None = None, session_gap_minutes: int = 30, window_duration: int = DEFAULT_WINDOW_MINUTES, window_start_hint: datetime | None = None) -> WindowState | None:
    cutoff = now_utc - timedelta(hours=5)
    future_cutoff = now_utc + timedelta(minutes=5)

    # Collect all timestamped rows within the last 5 hours
    timed_rows: list[tuple[datetime, dict]] = []
    for root in roots:
        for fp in _jsonl_files(root):
            if session_id and session_id not in fp.name and session_id not in str(fp):
                continue
            try:
                mtime = datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if now_utc - mtime > timedelta(hours=5):
                continue
            for row in _iter_jsonl(fp):
                ts = _row_timestamp(row)
                if ts is None or ts < cutoff or ts > future_cutoff:
                    continue
                timed_rows.append((ts, row))

    if not timed_rows:
        return None

    # Sort by timestamp and find the start of the current session by detecting
    # the last gap larger than session_gap_minutes between consecutive rows.
    # Everything after the last gap belongs to the current window.
    timed_rows.sort(key=lambda x: x[0])

    # If the most recent row is older than the gap threshold, the current session
    # hasn't produced any JSONL rows yet — treat as a fresh window.
    last_row_age = (now_utc - timed_rows[-1][0]).total_seconds() / 60
    if last_row_age >= session_gap_minutes:
        return None

    # Determine window start from best available signal:
    # 1. window_start_hint (from auto-resume dispatch — most accurate)
    # 2. Last gap >= session_gap_minutes (gap-based detection)
    # 3. Clamp: window can be at most window_duration old
    session_start_idx = 0
    for i in range(1, len(timed_rows)):
        gap = (timed_rows[i][0] - timed_rows[i - 1][0]).total_seconds() / 60
        if gap >= session_gap_minutes:
            session_start_idx = i

    gap_start = timed_rows[session_start_idx][0]
    max_start = now_utc - timedelta(minutes=window_duration)

    if window_start_hint is not None and window_start_hint > max_start:
        # Use the known window start from auto-resume.
        # Clamp to now_utc so a future hint (clock skew) doesn't produce
        # negative elapsed time or filter out all rows.
        earliest = min(window_start_hint, now_utc)
    else:
        # Use gap detection, but clamp so window never exceeds duration
        earliest = max(gap_start, max_start)

    # Filter rows to only those in the current window
    current_rows = [(ts, row) for ts, row in timed_rows if ts >= earliest]
    if not current_rows:
        current_rows = timed_rows[session_start_idx:]
        earliest = current_rows[0][0]

    input_tokens = 0
    output_tokens = 0
    for _, row in current_rows:
        msg = row.get("message")
        usage = (
            (msg.get("usage") if isinstance(msg, dict) else None)
            or row.get("message_usage")
            or row.get("usage")
            or {}
        )
        if isinstance(usage, dict):
            input_tokens += _as_int(usage.get("input_tokens")) + _as_int(usage.get("cache_creation_input_tokens"))
            output_tokens += _as_int(usage.get("output_tokens"))

    elapsed = max(0, int((now_utc - earliest).total_seconds() // 60))
    total_tokens = input_tokens + output_tokens
    burn_rate = total_tokens // max(1, elapsed) if elapsed > 0 else 0
    return WindowState(
        elapsed_minutes=elapsed,
        remaining_minutes=max(0, window_duration - elapsed),
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        burn_rate_tpm=burn_rate,
    )


def compute_budget_state(roots: list[Path], now_utc: datetime, weekly_budget: int) -> BudgetState:
    try:
        return _compute_budget_state_inner(roots, now_utc, weekly_budget)
    except Exception:
        return BudgetState(used_tokens=0, pct_used=0.0)


def _compute_budget_state_inner(roots: list[Path], now_utc: datetime, weekly_budget: int) -> BudgetState:
    cutoff = now_utc - timedelta(days=7)
    used = 0
    for root in roots:
        for fp in _jsonl_files(root):
            try:
                mtime = datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            for row in _iter_jsonl(fp):
                msg = row.get("message")
                usage = (
                    (msg.get("usage") if isinstance(msg, dict) else None)
                    or row.get("message_usage")
                    or row.get("usage")
                    or {}
                )
                if isinstance(usage, dict):
                    used += _as_int(usage.get("input_tokens"))
                    used += _as_int(usage.get("cache_creation_input_tokens"))
                    used += _as_int(usage.get("output_tokens"))
    return BudgetState(used_tokens=used, pct_used=min(1.0, used / max(1, weekly_budget)))


def _is_process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _find_claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    # Check common install locations before falling back to bare name
    for candidate in (
        Path.home() / ".claude" / "local" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ):
        if candidate.is_file():
            return str(candidate)
    return "claude"


def _spawn_dispatch(cfg: Config, repo_path: Path, prompt: str, result_path: Path, cwd: Path | None = None) -> int | None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_find_claude_bin(), "-p", prompt, "--output-format", "json", "--model", cfg.dispatch.model]
    if cfg.dispatch.allow_file_writes:
        cmd.append("--dangerously-skip-permissions")
    # Guard against prompts that exceed OS ARG_MAX (~256KB macOS, ~2MB Linux).
    # When the prompt is large, write it to a temp file and pipe via stdin instead.
    _ARG_MAX_SAFE = 200_000  # conservative limit in bytes
    use_stdin = len(prompt.encode("utf-8")) > _ARG_MAX_SAFE
    if use_stdin:
        cmd = [_find_claude_bin(), "--output-format", "json", "--model", cfg.dispatch.model]
        if cfg.dispatch.allow_file_writes:
            cmd.append("--dangerously-skip-permissions")
    fh = None
    try:
        fh = result_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd if cwd is not None else repo_path),
            stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        if use_stdin:
            # Write prompt to stdin and close (non-blocking for the parent).
            # The child process will read until EOF.
            try:
                proc.stdin.write(prompt)  # type: ignore[union-attr]
                proc.stdin.close()  # type: ignore[union-attr]
            except OSError:
                pass
    except OSError:
        return None
    finally:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
    return proc.pid


def _parse_hhmm(raw: str, allow_24: bool = False) -> int:
    hh, mm = [part.strip() for part in raw.split(":", 1)]
    hour, minute = int(hh), int(mm)
    if minute < 0 or minute > 59:
        raise ValueError("invalid minute")
    if allow_24 and hour == 24 and minute == 0:
        return 1440
    if hour < 0 or hour > 23:
        raise ValueError("invalid hour")
    return hour * 60 + minute


def _in_time_windows(now_local: datetime, windows: list[str]) -> bool:
    current = now_local.hour * 60 + now_local.minute
    for item in windows:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if "-" not in item:
            continue
        s, e = item.split("-", 1)
        try:
            start, end = _parse_hhmm(s), _parse_hhmm(e, allow_24=True)
        except ValueError:
            continue
        if start == end:
            continue
        if (start < end and start <= current < end) or (start > end and (current >= start or current < end)):
            return True
    return False


def _mode_gate_open(cfg: Config, now_local: datetime, window_state: WindowState | None) -> bool:
    if cfg.window.dispatch_mode == "always":
        return True
    if cfg.window.dispatch_mode == "time_based":
        return _in_time_windows(now_local, cfg.window.fallback_dispatch_hours)
    return bool(window_state and window_state.remaining_minutes <= cfg.window.trigger_at_minutes_remaining)


def _auto_resume_gate_open(cfg: Config, watch_paths: list[Path], state: State, window_state: WindowState | None) -> bool:
    if not cfg.auto_resume.enabled or not watch_paths:
        return False
    has_pending = any(bool(state.repos.get(str(repo), RepoState()).resume_entries) for repo in watch_paths)
    if not has_pending:
        return False
    if cfg.window.dispatch_mode == "window_aware":
        return bool(window_state and window_state.remaining_minutes >= cfg.auto_resume.min_remaining_minutes)
    return True


def _result_path(template: str, repo: Path, now_utc: datetime, section_slug: str = "") -> Path:
    name = f"{repo.name}-{section_slug}" if section_slug else repo.name
    # Expand both ~ and shell variables like $HOME / ${HOME} in the template.
    try:
        expanded = os.path.expandvars(template.format(repo=name, date=now_utc.strftime("%Y%m%d-%H%M%S")))
        return Path(expanded).expanduser().resolve()
    except (OSError, ValueError, KeyError):
        # Fallback to a safe default path if template expansion fails
        return app_dir() / "results" / f"{name}-{now_utc.strftime('%Y%m%d-%H%M%S')}.json"


_MAX_PROMPT_BYTES = 500_000  # 500KB


def _render_prompt(repo: Path, tasks: list[Task], allow_file_writes: bool, section_name: str = "") -> str:
    lines = [f"You are running background maintenance in repository: {repo}"]
    if section_name:
        lines.append(f"Section: {section_name}")
    lines.extend(["", "Tasks:"])
    lines.extend(f"- {t.id} | {t.priority} | {t.text}" for t in tasks)
    lines.extend(
        [
            "",
            "Rules:",
            "- Keep changes minimal and directly related to each task.",
            "- If uncertain, return NEEDS_HUMAN with reason.",
            "- Output one line per task in this exact format:",
            "DONE <task_id>: <summary>",
            "SKIPPED (<reason>) <task_id>: <summary>",
            "NEEDS_HUMAN (<reason>) <task_id>: <summary>",
            "FAILED (<reason>) <task_id>: <summary>",
            "- Do not modify files. Report findings/fixes only." if not allow_file_writes else "- You may edit files directly.",
        ]
    )
    result = "\n".join(lines)
    if len(result.encode("utf-8")) > _MAX_PROMPT_BYTES:
        result = result[:_MAX_PROMPT_BYTES]
    return result


def _worktrees_dir() -> Path:
    return app_dir() / "worktrees"


def _create_worktree(repo: Path, section_slug: str, timestamp: str) -> tuple[Path, str] | None:
    """Create an isolated git worktree for a section agent. Returns (worktree_path, branch) or None on failure."""
    branch = f"cc-later/{section_slug}-{timestamp}" if section_slug else f"cc-later/default-{timestamp}"
    worktree_path = _worktrees_dir() / f"{repo.name}-{section_slug or 'default'}-{timestamp}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    return worktree_path, branch


def _merge_worktree(repo: Path, branch: str, worktree_path: Path, section_name: str) -> tuple[bool, list[str]]:
    """Merge a section branch back into HEAD. Returns (success, conflicting_files).

    Uses an advisory lock to prevent concurrent merges into the same repo
    (e.g. two section agents finishing simultaneously).
    """
    # Scope the lock to this specific repo to avoid cross-repo contention.
    lock_name = f"merge-{repo.name}"
    with _flock(lock_name):
        # First check if the branch has any commits ahead of the current HEAD
        try:
            diff = subprocess.run(
                ["git", "rev-list", "--count", f"HEAD..{branch}"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if diff.returncode == 0 and diff.stdout.strip() == "0":
                # No commits — agent made no changes, nothing to merge
                _cleanup_worktree(repo, branch, worktree_path)
                return True, []
        except (OSError, subprocess.TimeoutExpired):
            pass

        try:
            result = subprocess.run(
                ["git", "merge", "--no-ff", branch, "-m", f"cc-later: {section_name or 'resume'} tasks"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                _cleanup_worktree(repo, branch, worktree_path)
                return True, []
            # Merge failed — collect conflicting files
            conflict_result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
            )
            conflicting = [f.strip() for f in conflict_result.stdout.splitlines() if f.strip()]
            # Abort the failed merge so the repo is not left in a broken state
            subprocess.run(["git", "merge", "--abort"], cwd=str(repo), capture_output=True, timeout=30)
            return False, conflicting
        except (OSError, subprocess.TimeoutExpired):
            return False, []


def _cleanup_worktree(repo: Path, branch: str, worktree_path: Path) -> None:
    """Remove a worktree and delete its branch."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(repo),
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=str(repo),
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _ensure_gitignore(repo: Path, later_path: str) -> None:
    gitignore = repo / ".gitignore"
    existing = _safe_read(gitignore) or ""
    # Normalize both CRLF and LF line endings before splitting
    lines = existing.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    stripped = {line.strip().lstrip("/") for line in lines}
    if later_path not in stripped:
        lines.append(later_path)
        try:
            gitignore.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except OSError:
            pass


def _is_agent_stale(agent: dict[str, Any], now_utc: datetime, stale_minutes: int) -> bool:
    """Check if a live agent has produced no output progress for too long."""
    result_path_str = _coerce_str(agent.get("result_path"))
    if result_path_str:
        rp = Path(result_path_str).expanduser()
        if rp.exists():
            try:
                mtime = datetime.fromtimestamp(rp.stat().st_mtime, tz=timezone.utc)
                return (now_utc - mtime).total_seconds() / 60 >= stale_minutes
            except OSError:
                pass
    # No result file yet — check dispatch time
    dispatch_ts = _parse_iso(agent.get("dispatch_ts"))
    if dispatch_ts:
        return (now_utc - dispatch_ts).total_seconds() / 60 >= stale_minutes
    # No timestamps available — cannot determine staleness, assume not stale
    return False


def _kill_agent(pid: int | None) -> None:
    if pid is None or pid <= 0:
        return
    try:
        sig = getattr(signal, "SIGTERM", 15)
        os.kill(pid, sig)
    except (OSError, AttributeError):
        pass


def _reconcile(cfg: Config, state: State, now_utc: datetime) -> int:
    completed = 0
    for repo_key, repo_state in state.repos.items():
        if not repo_state.in_flight:
            continue
        remaining: list[dict[str, Any]] = []
        nudge_queue: list[dict[str, Any]] = []
        for agent in repo_state.agents:
            pid = _coerce_int(agent.get("pid"))
            retries = int(agent.get("retries", 0))

            if _is_process_alive(pid):
                # Nudge: check if alive but stale
                if cfg.nudge.enabled and retries < cfg.nudge.max_retries and _is_agent_stale(agent, now_utc, cfg.nudge.stale_minutes):
                    _kill_agent(pid)
                    log_event("nudge_stale", repo=repo_key, pid=pid, section=agent.get("section_name"), retries=retries)
                    nudge_queue.append(agent)
                else:
                    remaining.append(agent)
                continue

            entries = [Task.from_dict(e) for e in agent.get("entries", []) if isinstance(e, dict)]
            result_path_str = _coerce_str(agent.get("result_path"))
            branch = _coerce_str(agent.get("branch"))
            worktree_path_str = _coerce_str(agent.get("worktree_path"))
            section_name = _coerce_str(agent.get("section_name")) or ""

            raw = _safe_read(Path(result_path_str).expanduser()) if result_path_str else None
            if raw is None:
                # No output — agent crashed. Nudge: re-queue if retries remain
                if cfg.nudge.enabled and retries < cfg.nudge.max_retries:
                    log_event("nudge_dead", repo=repo_key, pid=pid, section=section_name, retries=retries)
                    nudge_queue.append(agent)
                else:
                    # Exhausted retries or nudge disabled — clean up
                    if branch and worktree_path_str:
                        ok, _ = _merge_worktree(Path(repo_key), branch, Path(worktree_path_str), section_name)
                        if not ok:
                            log_event("merge_conflict", repo=repo_key, branch=branch, section=section_name, files=[])
                    log_event("agent_abandoned", repo=repo_key, pid=pid, section=section_name, retries=retries)
                completed += 1
                continue

            statuses = parse_result_summary(raw)
            for entry in entries:
                statuses.setdefault(entry.id, "FAILED")

            # Merge worktree branch back before doing anything else
            if branch and worktree_path_str:
                ok, conflicting = _merge_worktree(Path(repo_key), branch, Path(worktree_path_str), section_name)
                if not ok:
                    # Mark all entries as NEEDS_HUMAN due to conflict
                    for entry in entries:
                        statuses[entry.id] = "NEEDS_HUMAN"
                    log_event(
                        "merge_conflict",
                        repo=repo_key,
                        branch=branch,
                        section=section_name,
                        files=conflicting,
                        worktree=worktree_path_str,
                    )
                    print(
                        f"[cc-later] merge conflict: branch {branch}\n"
                        f"  conflicting files: {', '.join(conflicting) or 'unknown'}\n"
                        f"  worktree preserved at: {worktree_path_str}"
                    )

            reason = detect_limit_exhaustion(raw)
            if reason:
                state.window_limit_ts = now_utc.isoformat()
            if cfg.auto_resume.enabled and reason:
                resume = [e for e in entries if statuses.get(e.id) in {"FAILED", "NEEDS_HUMAN"}]
                if resume:
                    repo_state.resume_entries.extend([t.to_dict() for t in resume])
                    repo_state.resume_reason = reason
                    for task in resume:
                        statuses[task.id] = "SKIPPED"
                    log_event("resume_scheduled", repo=repo_key, reason=reason, entries=[t.text for t in resume])

            done_ids = {task_id for task_id, status in statuses.items() if status == "DONE"}
            if done_ids:
                later_path = Path(repo_key) / cfg.later.path
                content = _safe_read(later_path)
                if content is not None:
                    updated = mark_done_in_content(content, done_ids)
                    if updated != content:
                        _atomic_write(later_path, updated)
            completed += 1

        # Re-dispatch nudged agents
        for agent in nudge_queue:
            retries = int(agent.get("retries", 0)) + 1
            entries = [Task.from_dict(e) for e in agent.get("entries", []) if isinstance(e, dict)]
            section_name = _coerce_str(agent.get("section_name")) or ""
            old_branch = _coerce_str(agent.get("branch"))
            old_wt = _coerce_str(agent.get("worktree_path"))

            # Clean up old worktree if any
            if old_branch and old_wt:
                _cleanup_worktree(Path(repo_key), old_branch, Path(old_wt))

            timestamp = now_utc.strftime("%Y%m%d-%H%M%S")
            section_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", section_name) if section_name else "default"
            result_path = _result_path(cfg.dispatch.output_path, Path(repo_key), now_utc, f"{section_slug}-r{retries}")
            prompt = _render_prompt(Path(repo_key), entries, cfg.dispatch.allow_file_writes, section_name=section_name)

            branch: str | None = None
            worktree_path: Path | None = None
            cwd = Path(repo_key)
            if cfg.dispatch.allow_file_writes:
                wt = _create_worktree(Path(repo_key), section_slug, timestamp)
                if wt:
                    worktree_path, branch = wt
                    cwd = worktree_path

            pid = _spawn_dispatch(cfg, Path(repo_key), prompt, result_path, cwd=cwd)
            if pid is not None:
                remaining.append({
                    "section_name": section_name,
                    "pid": pid,
                    "result_path": str(result_path),
                    "entries": [t.to_dict() for t in entries],
                    "branch": branch,
                    "worktree_path": str(worktree_path) if worktree_path else None,
                    "dispatch_ts": now_utc.isoformat(),
                    "retries": retries,
                })
                log_event(
                    "nudge_redispatch",
                    repo=repo_key,
                    section=section_name,
                    retries=retries,
                    pid=pid,
                )
            elif worktree_path and branch:
                _cleanup_worktree(Path(repo_key), branch, worktree_path)

        repo_state.agents = remaining
        repo_state.in_flight = bool(remaining)
    return completed


def _read_hook_payload(stdin_text: str | None = None) -> dict[str, Any]:
    if stdin_text is not None:
        data = stdin_text
    elif not sys.stdin.isatty():
        data = sys.stdin.read(_MAX_STDIN_BYTES)
    else:
        data = ""
    data = (data or "").strip()
    if not data:
        return {}
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def run_handler(stdin_text: str | None = None) -> int:
    try:
        cfg = load_config()
    except Exception as exc:
        print(f"[cc-later] config error: {exc}")
        log_event("error", reason="config_error", detail=str(exc))
        return 0

    payload = _read_hook_payload(stdin_text)
    now_utc = pendulum.now("UTC")
    now_local = now_utc.astimezone()
    state = load_state()
    completed = _reconcile(cfg, state, now_utc)
    if completed:
        log_event("reconcile", completed=completed)

    watch_paths = resolve_watch_paths(cfg, payload.get("cwd"))
    previous_hook = _parse_iso(state.last_hook_ts)
    state.last_hook_ts = now_utc.isoformat()

    if not cfg.dispatch.enabled:
        save_state(state)
        log_event("skip", reason="dispatch_disabled")
        print("[cc-later] dispatch disabled")
        return 0

    if previous_hook is not None:
        idle_minutes = (now_utc - previous_hook).total_seconds() / 60
        if idle_minutes < cfg.window.idle_grace_period_minutes:
            save_state(state)
            log_event("skip", reason="idle_grace_active")
            print("[cc-later] idle grace active")
            return 0

    roots = resolve_jsonl_roots(cfg)
    budget = compute_budget_state(roots, now_utc, cfg.limits.weekly_budget_tokens)
    if budget.pct_used >= cfg.limits.backoff_at_pct / 100:
        save_state(state)
        log_event("skip", reason="budget_limit", pct_used=round(budget.pct_used * 100, 2))
        print(f"[cc-later] budget gate: {budget.pct_used*100:.1f}% used")
        return 0

    session_id = payload.get("session_id") or payload.get("sessionId")
    window_start_hint = _parse_iso(state.window_start_ts)
    window_state = compute_window_state(roots, now_utc, session_id=str(session_id) if session_id else None, window_duration=cfg.window.duration_minutes, window_start_hint=window_start_hint)

    # Self-calibrating window detection:
    # 1. If window is exhausted → record limit timestamp
    # 2. If we previously recorded a limit AND enough time has passed → window has reset
    if window_state is not None:
        if window_state.remaining_minutes <= 0 and state.window_limit_ts is None:
            state.window_limit_ts = now_utc.isoformat()
            log_event("window_exhausted")

        limit_ts = _parse_iso(state.window_limit_ts)
        if limit_ts is not None and (now_utc - limit_ts).total_seconds() / 60 > cfg.window.idle_grace_period_minutes:
            # Activity after a limit hit → fresh window. The current moment is the new window start.
            state.window_start_ts = now_utc.isoformat()
            state.window_limit_ts = None
            log_event("window_reset_detected")
            # Recompute with updated hint
            window_state = compute_window_state(roots, now_utc, session_id=str(session_id) if session_id else None, window_duration=cfg.window.duration_minutes, window_start_hint=now_utc)

    mode_open = _mode_gate_open(cfg, now_local, window_state)
    resume_open = _auto_resume_gate_open(cfg, watch_paths, state, window_state)
    if not mode_open and not resume_open:
        save_state(state)
        log_event("skip", reason="mode_gate_closed", mode=cfg.window.dispatch_mode)
        print("[cc-later] dispatch gate closed")
        return 0

    dispatched = 0
    for repo in watch_paths:
        key = str(repo)
        repo_state = state.repos.get(key, RepoState())
        state.repos[key] = repo_state
        if repo_state.in_flight:
            continue

        later_file = repo / cfg.later.path
        ensure_later_file(later_file)
        if cfg.later.auto_gitignore:
            _ensure_gitignore(repo, cfg.later.path)

        timestamp = now_utc.strftime("%Y%m%d-%H%M%S")

        if resume_open and repo_state.resume_entries:
            # Fresh window detected — auto-resume is dispatching
            state.window_start_ts = now_utc.isoformat()
            state.window_limit_ts = None
            selected = [Task.from_dict(item) for item in repo_state.resume_entries if isinstance(item, dict)]
            if selected:
                result_path = _result_path(cfg.dispatch.output_path, repo, now_utc)
                prompt = _render_prompt(repo, selected, cfg.dispatch.allow_file_writes)
                branch: str | None = None
                worktree_path: Path | None = None
                cwd = repo
                if cfg.dispatch.allow_file_writes:
                    wt = _create_worktree(repo, "resume", timestamp)
                    if wt:
                        worktree_path, branch = wt
                        cwd = worktree_path
                pid = _spawn_dispatch(cfg, repo, prompt, result_path, cwd=cwd)
                if pid is not None:
                    repo_state.agents.append({
                        "section_name": "",
                        "pid": pid,
                        "result_path": str(result_path),
                        "entries": [t.to_dict() for t in selected],
                        "branch": branch,
                        "worktree_path": str(worktree_path) if worktree_path else None,
                        "dispatch_ts": now_utc.isoformat(),
                        "retries": 0,
                    })
                    repo_state.resume_entries = []
                    repo_state.resume_reason = None
                    repo_state.dispatch_ts = now_utc.isoformat()
                    dispatched += 1
                    log_event(
                        "dispatch",
                        repo=key,
                        section="",
                        entries_dispatched=len(selected),
                        entries=[t.text for t in selected],
                        remaining_minutes=window_state.remaining_minutes if window_state else None,
                        model=cfg.dispatch.model,
                        result_path=str(result_path),
                        branch=branch,
                        auto_resume=True,
                    )
                elif worktree_path and branch:
                    _cleanup_worktree(repo, branch, worktree_path)
            repo_state.in_flight = bool(repo_state.agents)
            continue

        if not mode_open:
            continue

        sections = parse_tasks(_safe_read(later_file) or "")
        for section in sections:
            selected = select_tasks(section, cfg.later.max_entries_per_dispatch)
            if not selected:
                continue
            section_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", section.name) if section.name else "default"
            result_path = _result_path(cfg.dispatch.output_path, repo, now_utc, section_slug)
            prompt = _render_prompt(repo, selected, cfg.dispatch.allow_file_writes, section_name=section.name)
            branch = None
            worktree_path = None
            cwd = repo
            if cfg.dispatch.allow_file_writes:
                wt = _create_worktree(repo, section_slug, timestamp)
                if wt:
                    worktree_path, branch = wt
                    cwd = worktree_path
            pid = _spawn_dispatch(cfg, repo, prompt, result_path, cwd=cwd)
            if pid is None:
                if worktree_path and branch:
                    _cleanup_worktree(repo, branch, worktree_path)
                log_event("error", reason="dispatch_spawn_failed", repo=key, section=section.name)
                continue
            repo_state.agents.append({
                "section_name": section.name,
                "pid": pid,
                "result_path": str(result_path),
                "entries": [t.to_dict() for t in selected],
                "branch": branch,
                "worktree_path": str(worktree_path) if worktree_path else None,
                "dispatch_ts": now_utc.isoformat(),
                "retries": 0,
            })
            dispatched += 1
            log_event(
                "dispatch",
                repo=key,
                section=section.name,
                entries_dispatched=len(selected),
                entries=[t.text for t in selected],
                remaining_minutes=window_state.remaining_minutes if window_state else None,
                model=cfg.dispatch.model,
                result_path=str(result_path),
                branch=branch,
                auto_resume=False,
            )

        repo_state.in_flight = bool(repo_state.agents)
        if repo_state.in_flight:
            repo_state.dispatch_ts = now_utc.isoformat()

    save_state(state)
    print(f"[cc-later] dispatched {dispatched} agent(s)" if dispatched else "[cc-later] no eligible LATER entries")
    return 0


def build_status(cwd_hint: str | None = None) -> str:
    cfg = load_config()
    state = load_state()
    now_utc = pendulum.now("UTC")
    now_local = now_utc.astimezone()
    roots = resolve_jsonl_roots(cfg)
    window_start_hint = _parse_iso(state.window_start_ts)
    window_state = compute_window_state(roots, now_utc, window_duration=cfg.window.duration_minutes, window_start_hint=window_start_hint)
    budget = compute_budget_state(roots, now_utc, cfg.limits.weekly_budget_tokens)
    watch_paths = resolve_watch_paths(cfg, cwd_hint)

    plan_limits = PLAN_LIMITS.get(cfg.plan, PLAN_LIMITS["max"])
    lines = ["## cc-later Status", ""]

    # Plan info
    lines.append("### Plan")
    lines.append(f"  Tier: {cfg.plan}")
    lines.append(f"  Models: {', '.join(plan_limits.get('models', []))}")
    lines.append(f"  Context: {plan_limits.get('context_k', '?')}k")
    if plan_limits.get("extended_thinking"):
        lines.append("  Extended thinking: yes")

    lines.extend(["", "### Window", f"  Mode: {cfg.window.dispatch_mode}"])
    if window_state is None:
        lines.extend(["  State: unknown", "  Next window: starts on next Claude request"])
    else:
        end_local = (now_utc + timedelta(minutes=window_state.remaining_minutes)).astimezone()
        lines.append(f"  Elapsed/Remaining: {window_state.elapsed_minutes}m / {window_state.remaining_minutes}m")
        lines.append(f"  Tokens: {window_state.total_input_tokens:,} in / {window_state.total_output_tokens:,} out")
        if window_state.burn_rate_tpm > 0:
            lines.append(f"  Burn rate: {window_state.burn_rate_tpm:,} tokens/min")
        lines.append(f"  Window ends: {end_local.strftime('%Y-%m-%d %H:%M %Z')}")
        if window_state.remaining_minutes <= 0:
            lines.append("  Next window: starts on next Claude request")
        else:
            lines.append(f"  Next window: starts on first Claude request after {end_local.strftime('%H:%M %Z')}")

    lines.append(
        f"  Weekly budget: {budget.used_tokens:,} / {cfg.limits.weekly_budget_tokens:,} ({budget.pct_used*100:.1f}%)"
    )
    lines.append(
        f"  Backoff at: {cfg.limits.backoff_at_pct}% ({int(cfg.limits.weekly_budget_tokens*cfg.limits.backoff_at_pct/100):,} tokens)"
    )

    lines.extend(["", "### Queue"])
    total_pending = 0
    for repo in watch_paths:
        repo_state = state.repos.get(str(repo), RepoState())
        later_file = repo / cfg.later.path
        sections = parse_tasks(_safe_read(later_file) or "")
        pending_count = sum(len(s.tasks) for s in sections)
        total_pending += pending_count
        lines.append(f"  {repo.name}/{' [in-flight]' if repo_state.in_flight else ''}")
        lines.append(f"    pending: {pending_count}")
        if repo_state.in_flight:
            for agent in repo_state.agents:
                sname = agent.get("section_name") or "(unsectioned)"
                branch = agent.get("branch")
                branch_str = f"  branch={branch}" if branch else ""
                lines.append(f"    agent [{sname}]: pid={agent.get('pid')}{branch_str}")
        if repo_state.resume_entries:
            lines.append(f"    auto-resume pending: {len(repo_state.resume_entries)}")
    if total_pending == 0:
        lines.append("\n  All queues empty")

    lines.extend(["", "### Gates"])
    lines.append(f"  dispatch.enabled: {'pass' if cfg.dispatch.enabled else 'FAIL'}")
    lines.append(f"  mode gate: {'pass' if _mode_gate_open(cfg, now_local, window_state) else 'FAIL'}")
    lines.append(
        f"  auto-resume gate: {'pass' if _auto_resume_gate_open(cfg, watch_paths, state, window_state) else 'closed'}"
    )
    lines.append(f"  budget gate: {'pass' if budget.pct_used < cfg.limits.backoff_at_pct/100 else 'FAIL'}")

    lines.extend(["", "### Recent Runs"])
    recent: list[dict[str, Any]] = []
    log_path = run_log_path()
    if log_path.exists():
        # Read only the tail (~4KB) to avoid loading the entire append-only log
        try:
            size = log_path.stat().st_size
            with log_path.open("r", encoding="utf-8") as fh:
                if size > 4096:
                    fh.seek(size - 4096)
                    fh.readline()  # skip partial line
                for row in fh:
                    row = row.strip()
                    if not row:
                        continue
                    try:
                        recent.append(json.loads(row))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
    for item in recent[-8:][::-1]:
        ts = _parse_iso(item.get("ts"))
        when = ts.astimezone().strftime("%m-%d %H:%M") if ts else "??"
        lines.append(f"  {when} {item.get('event','?'):16} {item.get('reason') or ''}")

    lines.extend(["", "### Limit Events (24h)"])
    limit_events = _scan_limit_events(hours=24)
    active = {k: v for k, v in limit_events.items() if v > 0}
    if active:
        for event, count in sorted(active.items()):
            lines.append(f"  {event}: {count}")
    else:
        lines.append("  none")

    return "\n".join(lines) + "\n"


def run_compact_inject(cwd_hint: str | None = None) -> int:
    """Output LATER.md context for injection after compaction (SessionStart/compact hook)."""
    cfg = load_config()
    if not cfg.compact.enabled:
        return 0

    now_utc = pendulum.now("UTC")
    state = load_state()
    roots = resolve_jsonl_roots(cfg)
    window_start_hint = _parse_iso(state.window_start_ts)
    window_state = compute_window_state(roots, now_utc, window_duration=cfg.window.duration_minutes, window_start_hint=window_start_hint)
    watch_paths = resolve_watch_paths(cfg, cwd_hint)

    lines = ["=== cc-later context (post-compaction) ==="]

    if window_state is None:
        lines.append("Window: unknown (fresh window)")
    else:
        lines.append(
            f"Window: {window_state.remaining_minutes}m remaining"
            f" ({cfg.window.dispatch_mode}, {window_state.elapsed_minutes}m elapsed)"
        )

    lines.append("")
    has_tasks = False
    for repo in watch_paths:
        repo_state = state.repos.get(str(repo), RepoState())
        later_file = repo / cfg.later.path
        if not later_file.exists():
            continue
        sections = parse_tasks(_safe_read(later_file) or "")
        pending = [s for s in sections if s.tasks]
        if not pending:
            continue
        has_tasks = True
        total = sum(len(s.tasks) for s in pending)
        lines.append(f"Pending LATER.md tasks ({total}) — {repo.name}/:")
        for section in pending:
            if section.name:
                lines.append(f"  ## {section.name}")
            for task in section.tasks:
                lines.append(f"  - [ ] ({task.priority}) {task.text}")
        if repo_state.in_flight:
            lines.append(f"  [dispatch in progress: {len(repo_state.agents)} agent(s)]")
        if repo_state.resume_entries:
            lines.append(f"  [auto-resume queued: {len(repo_state.resume_entries)} task(s)]")

    if not has_tasks:
        lines.append("LATER.md queue: empty")

    print("\n".join(lines) + "\n", end="")
    return 0


# -- Pricing per million tokens (USD) --
_MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-6": {"input": 15.0, "cache_create": 18.75, "cache_read": 1.50, "output": 75.0},
    "claude-opus-4-5": {"input": 15.0, "cache_create": 18.75, "cache_read": 1.50, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "cache_create": 3.75, "cache_read": 0.30, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "cache_create": 3.75, "cache_read": 0.30, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "cache_create": 1.0, "cache_read": 0.08, "output": 4.0},
}
_DEFAULT_PRICING = {"input": 3.0, "cache_create": 3.75, "cache_read": 0.30, "output": 15.0}


def _normalize_model(model: str) -> str:
    """Normalize model ID to a pricing key (strip date suffixes)."""
    for key in _MODEL_PRICING:
        if model.startswith(key):
            return key
    return model


def run_stats(days: int = 7) -> int:
    """Print detailed token analytics with per-model cost breakdown."""
    cfg = load_config()
    roots = resolve_jsonl_roots(cfg)
    now_utc = pendulum.now("UTC")
    cutoff = now_utc - timedelta(days=days)

    # Accumulators: model -> {input, cache_create, cache_read, output}
    by_model: dict[str, dict[str, int]] = {}
    session_ids: set[str] = set()
    file_count = 0

    for root in roots:
        for fp in _jsonl_files(root, recursive=True):
            try:
                mtime = datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            file_count += 1
            for row in _iter_jsonl(fp):
                sid = row.get("sessionId")
                if sid:
                    session_ids.add(sid)
                msg = row.get("message")
                if not isinstance(msg, dict):
                    continue
                model_raw = msg.get("model") or "unknown"
                model_key = _normalize_model(model_raw)
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                bucket = by_model.setdefault(model_key, {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0})
                bucket["input"] += _as_int(usage.get("input_tokens"))
                bucket["cache_create"] += _as_int(usage.get("cache_creation_input_tokens"))
                bucket["cache_read"] += _as_int(usage.get("cache_read_input_tokens"))
                bucket["output"] += _as_int(usage.get("output_tokens"))

    lines = [f"## cc-later Stats ({days}d)", ""]

    total_cost = 0.0
    total_input = total_cc = total_cr = total_output = 0
    for model_key in sorted(by_model.keys()):
        b = by_model[model_key]
        if not any(b.values()):
            continue
        pricing = _MODEL_PRICING.get(model_key, _DEFAULT_PRICING)
        cost = (
            b["input"] * pricing["input"] / 1_000_000
            + b["cache_create"] * pricing["cache_create"] / 1_000_000
            + b["cache_read"] * pricing["cache_read"] / 1_000_000
            + b["output"] * pricing["output"] / 1_000_000
        )
        total_cost += cost
        total_input += b["input"]
        total_cc += b["cache_create"]
        total_cr += b["cache_read"]
        total_output += b["output"]
        lines.append(f"### {model_key}")
        lines.append(f"  Input:          {b['input']:>15,}")
        lines.append(f"  Cache creation: {b['cache_create']:>15,}")
        lines.append(f"  Cache read:     {b['cache_read']:>15,}")
        lines.append(f"  Output:         {b['output']:>15,}")
        lines.append(f"  API cost:       ${cost:>14,.2f}")
        lines.append("")

    grand = total_input + total_cc + total_cr + total_output
    lines.append("### Totals")
    lines.append(f"  Input:          {total_input:>15,}")
    lines.append(f"  Cache creation: {total_cc:>15,}")
    lines.append(f"  Cache read:     {total_cr:>15,}")
    lines.append(f"  Output:         {total_output:>15,}")
    lines.append(f"  Grand total:    {grand:>15,}  (~{grand/1_000_000:.1f}M)")
    lines.append(f"  API equiv cost: ${total_cost:>14,.2f}")
    lines.append(f"  Sessions:       {len(session_ids):>15,}")
    lines.append(f"  JSONL files:    {file_count:>15,}")
    sub_cost = days / 30 * 200  # Max plan prorated
    lines.append("")
    lines.append(f"  Max plan cost:  ${sub_cost:>14,.2f}  ({days}d @ $200/mo)")
    if total_cost > 0 and sub_cost > 0:
        savings_pct = (1 - sub_cost / total_cost) * 100
        if savings_pct > 0:
            lines.append(f"  Savings vs API: {savings_pct:>14.0f}%  (subscription cheaper)")
        else:
            lines.append(f"  API is cheaper: {abs(savings_pct):>13.0f}%  vs Max plan")

    print("\n".join(lines))
    return 0


def run_status() -> int:
    print(build_status())
    return 0


def capture_from_payload(payload: dict[str, Any]) -> int:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    cfg = load_config()
    repo = resolve_watch_paths(cfg, payload.get("cwd"))[0]
    later_file = repo / cfg.later.path
    ensure_later_file(later_file)

    existing = _safe_read(later_file) or ""
    lines = existing.splitlines()
    # Extract existing task texts for dedup (not full file content — avoids false positives)
    existing_tasks: set[str] = set()
    for line in lines:
        m = TASK_RE.match(line)
        if m and m.group("text"):
            existing_tasks.add(m.group("text").strip().lower())
    added = 0
    for match in CAPTURE_RE.finditer(prompt):
        urgent = bool(match.group(1))
        text = match.group(2).strip().rstrip(". \t")
        # Strip leading priority markers to avoid doubled priority like (P0) (P0)
        text = re.sub(r"^\(P[012]\)\s*", "", text).strip()
        if len(text) < 3 or text.lower() in existing_tasks:
            continue
        lines.append(f"- [ ] ({'P0' if urgent else 'P1'}) {text}")
        existing_tasks.add(text.lower())
        added += 1

    if added:
        _atomic_write(later_file, "\n".join(lines).rstrip() + "\n")
        log_event("capture", repo=str(repo), added=added)
        print(f"[cc-later] added {added} task(s) to {later_file}")
    return 0


# ---------------------------------------------------------------------------
# Monitor — periodic window/budget/agent monitoring
# ---------------------------------------------------------------------------

_LIMIT_EVENT_TYPES = frozenset({
    "window_exhausted", "window_reset_detected", "budget_limit",
    "nudge_stale", "nudge_dead", "agent_abandoned",
})


def _scan_limit_events(hours: int = 24) -> dict[str, int]:
    """Count limit-related events in run_log.jsonl within the last *hours*."""
    counts: dict[str, int] = {e: 0 for e in _LIMIT_EVENT_TYPES}
    log_path = run_log_path()
    if not log_path.exists():
        return counts
    cutoff = pendulum.now("UTC") - timedelta(hours=hours)
    try:
        size = log_path.stat().st_size
        # Read tail (~128KB) to avoid loading full log while capturing 24h of events
        with log_path.open("r", encoding="utf-8") as fh:
            if size > 131072:
                fh.seek(size - 131072)
                fh.readline()  # skip partial line
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = row.get("event", "")
                if event not in _LIMIT_EVENT_TYPES:
                    continue
                ts = _parse_iso(row.get("ts"))
                if ts is not None and ts >= cutoff:
                    counts[event] = counts.get(event, 0) + 1
    except OSError:
        pass
    return counts


def _sanitize_osascript(s: str) -> str:
    """Escape a string for safe embedding in an AppleScript double-quoted string."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _notify_macos(title: str, message: str) -> None:
    """Send macOS notification via osascript."""
    try:
        safe_title = _sanitize_osascript(title)
        safe_msg = _sanitize_osascript(message)
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe_msg}" with title "{safe_title}"'],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _parse_usage_screen(text: str) -> UsageInfo | None:
    """Parse /usage TUI rendered text into UsageInfo. Returns None if no data found."""
    info = UsageInfo()
    found = False

    # Session percentage: "43% used" or "43% of session used"
    m = re.search(r'(\d+)%\s+(?:of session\s+)?used', text, re.IGNORECASE)
    if m:
        info.session_pct = int(m.group(1))
        found = True

    # Reset time: "Resets 1pm", "Resets at 1:00 PM", "Reset at 13:00", "Resets 13:00"
    m = re.search(r'[Rr]esets?\s+(?:at\s+)?(\d+(?::\d+)?(?:\s*[aApP][mM])?)', text)
    if not m:
        m = re.search(r'[Rr]eset\s+at\s+(\d+(?::\d+)?(?:\s*[aApP][mM])?)', text)
    if m:
        info.session_reset = m.group(1).strip()
        found = True

    # Weekly percentage: "Weekly: 25% used" or "Weekly usage: 25%"
    m = re.search(r'[Ww]eekly[^0-9]*(\d+)%', text)
    if m:
        info.weekly_pct = int(m.group(1))
        found = True

    # Weekly reset: "Weekly resets Monday" or "Resets Monday"
    m = re.search(r'[Ww]eekly[^.]*[Rr]esets?\s+(\w+)', text)
    if m:
        info.weekly_reset = m.group(1)
        found = True

    # Extra usage / overage: "$12.34 extra" or "Extra usage: $12.34"
    m = re.search(r'\$(\d+(?:\.\d+)?)\s+(?:extra|overage|additional)', text, re.IGNORECASE)
    if not m:
        m = re.search(r'[Ee]xtra\s+usage[:\s]+\$(\d+(?:\.\d+)?)', text)
    if m:
        info.extra_usage_usd = float(m.group(1))
        found = True

    return info if found else None


def query_claude_plan_info() -> UsageInfo | None:
    """Scrape /usage from Claude CLI via PTY to get live session/weekly usage.

    Works only for subscription (claude.ai) accounts. Org/API accounts return None.
    Results cached to ~/.cc-later/usage_info.json for 5 minutes.
    """
    import select
    import time

    try:
        import pty
    except ImportError:
        return None  # Windows or environments without PTY

    try:
        import pyte
    except ImportError:
        return None

    # Check cache (5m TTL — short enough to be useful, long enough to avoid hammering)
    cache_path = app_dir() / "usage_info.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_ts = _parse_iso(cached.get("_cached_at"))
            if cached_ts and (pendulum.now("UTC") - cached_ts).total_seconds() < 300:
                if cached.get("_unavailable"):
                    return None
                return UsageInfo(
                    session_pct=cached.get("session_pct"),
                    session_reset=cached.get("session_reset"),
                    weekly_pct=cached.get("weekly_pct"),
                    weekly_reset=cached.get("weekly_reset"),
                    extra_usage_usd=cached.get("extra_usage_usd"),
                )
        except (json.JSONDecodeError, OSError):
            pass

    claude = _find_claude_bin()
    if not claude:
        return None

    COLS, ROWS = 120, 40
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)

    def read_and_feed(fd: int, duration: float) -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            r, _, _ = select.select([fd], [], [], min(0.1, remaining))
            if r:
                try:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    stream.feed(chunk)
                except OSError:
                    break

    pid = fd = None
    try:
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["COLUMNS"] = str(COLS)
            os.environ["LINES"] = str(ROWS)
            os.environ["TERM"] = "xterm-256color"
            os.execvp(claude, [claude])
            os._exit(1)

        # parent: let claude start up
        read_and_feed(fd, 5.0)

        # send /usage command
        os.write(fd, b"/usage\r")

        # wait for backend to respond and TUI to render
        read_and_feed(fd, 8.0)

        # render the screen buffer
        display_lines = screen.display  # list of fixed-width strings
        text = "\n".join(line.rstrip() for line in display_lines)

        # gracefully exit
        try:
            os.write(fd, b"/exit\r")
            read_and_feed(fd, 1.0)
        except OSError:
            pass

        # detect org/API mode (no subscription)
        if any(phrase in text.lower() for phrase in [
            "only available for subscription",
            "not available for",
            "claude api",
        ]):
            try:
                cache_path.write_text(
                    json.dumps({"_unavailable": True, "_cached_at": pendulum.now("UTC").isoformat()}),
                    encoding="utf-8",
                )
            except OSError:
                pass
            return None

        info = _parse_usage_screen(text)
        if info:
            cached_data = {
                "_cached_at": pendulum.now("UTC").isoformat(),
                "session_pct": info.session_pct,
                "session_reset": info.session_reset,
                "weekly_pct": info.weekly_pct,
                "weekly_reset": info.weekly_reset,
                "extra_usage_usd": info.extra_usage_usd,
            }
            try:
                cache_path.write_text(json.dumps(cached_data), encoding="utf-8")
            except OSError:
                pass
        return info

    except OSError:
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass


def run_monitor(cwd_hint: str | None = None, notify: bool = True) -> MonitorSnapshot:
    """Compute full monitoring snapshot. Used by external cron and CronCreate."""
    cfg = load_config()
    state = load_state()
    now_utc = pendulum.now("UTC")
    roots = resolve_jsonl_roots(cfg)
    window_start_hint = _parse_iso(state.window_start_ts)
    window_state = compute_window_state(
        roots, now_utc,
        window_duration=cfg.window.duration_minutes,
        window_start_hint=window_start_hint,
    )
    budget = compute_budget_state(roots, now_utc, cfg.limits.weekly_budget_tokens)

    # Count in-flight and stale agents
    agents_in_flight = 0
    agents_stale = 0
    for repo_state_raw in state.repos.values():
        repo_state = repo_state_raw if isinstance(repo_state_raw, RepoState) else RepoState()
        for agent in repo_state.agents:
            agents_in_flight += 1
            if _is_agent_stale(agent, now_utc, cfg.nudge.stale_minutes):
                agents_stale += 1

    limit_events = _scan_limit_events(hours=24)
    plan_limits = PLAN_LIMITS.get(cfg.plan, PLAN_LIMITS["max"])

    # Optionally query live usage from Claude CLI via PTY (/usage TUI scraping).
    # Only works for subscription accounts; org/API accounts return None gracefully.
    usage_info: UsageInfo | None = None
    if cfg.monitor.query_claude:
        usage_info = query_claude_plan_info()

    snap = MonitorSnapshot(
        ts=now_utc.isoformat(),
        window=window_state,
        budget=budget,
        plan=cfg.plan,
        plan_limits=plan_limits,
        agents_in_flight=agents_in_flight,
        agents_stale=agents_stale,
        limit_events_24h=limit_events,
        usage_info=usage_info,
    )

    # Write snapshot to disk
    monitor_path = app_dir() / "monitor.json"
    try:
        monitor_path.write_text(json.dumps(asdict(snap), default=str), encoding="utf-8")
    except OSError:
        pass

    # macOS notifications on threshold crossings
    if notify and cfg.monitor.notify_enabled:
        warnings: list[str] = []
        if window_state and window_state.remaining_minutes <= cfg.monitor.warn_window_minutes:
            warnings.append(f"Window: {window_state.remaining_minutes}m left")
        if budget.pct_used * 100 >= cfg.monitor.warn_budget_pct:
            warnings.append(f"Budget: {budget.pct_used*100:.0f}%")
        if agents_stale > 0:
            warnings.append(f"{agents_stale} stale agent(s)")
        if warnings:
            _notify_macos("cc-later", " | ".join(warnings))

    return snap


def format_monitor_compact(snap: MonitorSnapshot) -> str:
    """One-line status for CronCreate/session output."""
    parts = [f"Plan: {snap.plan}"]
    if snap.window:
        parts.append(f"Window: {snap.window.remaining_minutes}m left")
        if snap.window.burn_rate_tpm > 0:
            parts.append(f"Burn: {snap.window.burn_rate_tpm:,}t/min")
    else:
        parts.append("Window: unknown")
    parts.append(f"Budget: {snap.budget.pct_used*100:.0f}%")
    if snap.usage_info and snap.usage_info.session_pct is not None:
        reset_str = f" (resets {snap.usage_info.session_reset})" if snap.usage_info.session_reset else ""
        parts.append(f"Session: {snap.usage_info.session_pct}%{reset_str}")
    if snap.agents_in_flight:
        parts.append(f"{snap.agents_in_flight} agent(s)")
    if snap.agents_stale:
        parts.append(f"{snap.agents_stale} stale")
    return "[cc-later] " + " | ".join(parts)


def format_monitor_full(snap: MonitorSnapshot) -> str:
    """Multi-line status for external cron/verbose output."""
    lines = ["## cc-later Monitor", ""]

    # Plan info
    lines.append(f"Plan: {snap.plan}")
    pl = snap.plan_limits
    models = ", ".join(pl.get("models", []))
    lines.append(f"  Models: {models}")
    lines.append(f"  Context: {pl.get('context_k', '?')}k")
    if pl.get("extended_thinking"):
        lines.append("  Extended thinking: yes")

    # Window
    lines.append("")
    if snap.window:
        lines.append(f"Window: {snap.window.elapsed_minutes}m elapsed / {snap.window.remaining_minutes}m remaining")
        lines.append(f"  Tokens: {snap.window.total_input_tokens:,} in / {snap.window.total_output_tokens:,} out")
        if snap.window.burn_rate_tpm > 0:
            lines.append(f"  Burn rate: {snap.window.burn_rate_tpm:,} tokens/min")
    else:
        lines.append("Window: no active session detected")

    # Budget
    lines.append(f"\nBudget: {snap.budget.used_tokens:,} tokens ({snap.budget.pct_used*100:.1f}%)")

    # Live usage from Claude CLI /usage (only if query_claude=true and subscription account)
    if snap.usage_info:
        ui = snap.usage_info
        lines.append("\nLive usage (from Claude CLI):")
        if ui.session_pct is not None:
            reset_str = f", resets {ui.session_reset}" if ui.session_reset else ""
            lines.append(f"  Session: {ui.session_pct}% used{reset_str}")
        if ui.weekly_pct is not None:
            weekly_reset_str = f", resets {ui.weekly_reset}" if ui.weekly_reset else ""
            lines.append(f"  Weekly: {ui.weekly_pct}% used{weekly_reset_str}")
        if ui.extra_usage_usd is not None:
            lines.append(f"  Extra usage: ${ui.extra_usage_usd:.2f}")

    # Agents
    if snap.agents_in_flight:
        lines.append(f"\nAgents: {snap.agents_in_flight} in-flight ({snap.agents_stale} stale)")
    else:
        lines.append("\nAgents: none")

    # Limit events
    active_events = {k: v for k, v in snap.limit_events_24h.items() if v > 0}
    if active_events:
        lines.append("\nLimit events (24h):")
        for event, count in sorted(active_events.items()):
            lines.append(f"  {event}: {count}")
    else:
        lines.append("\nLimit events (24h): none")

    return "\n".join(lines) + "\n"


def run_monitor_cli(cwd_hint: str | None = None) -> int:
    """Entry point for /cc-later:monitor command and scripts/monitor.py --once."""
    snap = run_monitor(cwd_hint=cwd_hint, notify=False)
    print(format_monitor_full(snap))
    return 0


__all__ = [
    "APP_DIR_ENV",
    "AutoResumeConfig",
    "BudgetState",
    "CompactConfig",
    "Config",
    "DispatchConfig",
    "LaterConfig",
    "LimitsConfig",
    "MonitorConfig",
    "MonitorSnapshot",
    "NudgeConfig",
    "UsageInfo",
    "PLAN_LIMITS",
    "PathsConfig",
    "RepoState",
    "Section",
    "State",
    "Task",
    "WindowConfig",
    "WindowState",
    "_sanitize_osascript",
    "build_status",
    "capture_from_payload",
    "compute_budget_state",
    "compute_window_state",
    "detect_limit_exhaustion",
    "ensure_later_file",
    "format_monitor_compact",
    "format_monitor_full",
    "load_config",
    "load_state",
    "mark_done_in_content",
    "parse_result_summary",
    "parse_tasks",
    "query_claude_plan_info",
    "resolve_watch_paths",
    "run_compact_inject",
    "run_handler",
    "run_monitor",
    "run_monitor_cli",
    "run_stats",
    "run_status",
    "save_state",
    "select_tasks",
    "stable_task_id",
]
