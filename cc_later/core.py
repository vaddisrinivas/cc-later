from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

APP_DIR_ENV = "CC_LATER_APP_DIR"
DEFAULT_WINDOW_MINUTES = 300
LIMIT_MARKERS = (
    "rate limit",
    "usage limit",
    "quota",
    "too many requests",
    "429",
    "5-hour window",
    "window exhausted",
    "try again later",
)
TASK_RE = re.compile(
    r"^(?P<prefix>\s*-\s*)\[(?P<mark>[ xX!])\](?P<space>\s*)(?:(?P<prio>\(P[0-2]\))\s*)?(?P<text>.+?)\s*$"
)
RESULT_RE = re.compile(r"^(DONE|SKIPPED|NEEDS_HUMAN|FAILED)(?:\s+\([^)]+\))?\s+([A-Za-z0-9_-]+)\s*:")
CAPTURE_RE = re.compile(
    r"(?i)(?:later|add\s+(?:this\s+)?to\s+later|note\s+(?:this\s+)?for\s+later|"
    r"queue\s+(?:this\s+)?for\s+later|for\s+later)\s*(\[!\])?\s*:\s*(.+?)(?=$|\n)"
)


@dataclass
class PathsConfig:
    watch: list[str] = field(default_factory=list)


@dataclass
class LaterConfig:
    path: str = ".claude/LATER.md"
    max_entries_per_dispatch: int = 3
    auto_gitignore: bool = True


@dataclass
class DispatchConfig:
    enabled: bool = True
    model: str = "sonnet"
    allow_file_writes: bool = False
    output_path: str = "~/.cc-later/results/{repo}-{date}.json"


@dataclass
class WindowConfig:
    dispatch_mode: str = "window_aware"  # window_aware | time_based | always
    trigger_at_minutes_remaining: int = 30
    idle_grace_period_minutes: int = 10
    fallback_dispatch_hours: list[str] = field(default_factory=list)
    jsonl_paths: list[str] = field(default_factory=list)


@dataclass
class LimitsConfig:
    weekly_budget_tokens: int = 10_000_000
    backoff_at_pct: int = 80


@dataclass
class AutoResumeConfig:
    enabled: bool = True
    min_remaining_minutes: int = 240


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    later: LaterConfig = field(default_factory=LaterConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    auto_resume: AutoResumeConfig = field(default_factory=AutoResumeConfig)


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
    repos: dict[str, RepoState] = field(default_factory=dict)


@dataclass
class WindowState:
    elapsed_minutes: int
    remaining_minutes: int
    total_input_tokens: int
    total_output_tokens: int


@dataclass
class BudgetState:
    used_tokens: int
    pct_used: float


def app_dir() -> Path:
    return Path(os.environ.get(APP_DIR_ENV, "~/.cc-later")).expanduser()


def config_path() -> Path:
    return app_dir() / "config.env"


def state_path() -> Path:
    return app_dir() / "state.json"


def run_log_path() -> Path:
    return app_dir() / "run_log.jsonl"


def default_config_template_path() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts" / "default_config.env"


def log_event(event: str, **fields: Any) -> None:
    app_dir().mkdir(parents=True, exist_ok=True)
    payload = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
    payload.update(fields)
    with run_log_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip()
    return result


def _parse_bool(val: str) -> bool:
    return val.strip().lower() in {"true", "1", "yes"}


def _parse_list(val: str) -> list[str]:
    val = val.strip()
    if not val:
        return []
    return [item.strip() for item in val.split(",") if item.strip()]


def _validate_values(cfg: Config) -> None:
    if cfg.window.dispatch_mode not in {"window_aware", "time_based", "always"}:
        raise ValueError("window.dispatch_mode must be one of: window_aware, time_based, always")
    if cfg.dispatch.model not in {"sonnet", "opus", "haiku"}:
        raise ValueError("dispatch.model must be one of: sonnet, opus, haiku")
    if cfg.limits.weekly_budget_tokens <= 0:
        raise ValueError("limits.weekly_budget_tokens must be > 0")
    if not (0 <= cfg.limits.backoff_at_pct <= 100):
        raise ValueError("limits.backoff_at_pct must be between 0 and 100")
    if cfg.auto_resume.min_remaining_minutes < 0:
        raise ValueError("auto_resume.min_remaining_minutes must be >= 0")
    if cfg.later.max_entries_per_dispatch <= 0:
        raise ValueError("later.max_entries_per_dispatch must be > 0")


def load_config() -> Config:
    app_dir().mkdir(parents=True, exist_ok=True)
    path = config_path()
    if not path.exists():
        shutil.copy2(default_config_template_path(), path)
        log_event("config_created", path=str(path))

    raw = _read_env(path)
    cfg = Config()
    cfg.paths.watch = _parse_list(raw.get("PATHS_WATCH", ""))
    cfg.later.path = raw.get("LATER_PATH", cfg.later.path)
    cfg.later.max_entries_per_dispatch = int(raw.get("LATER_MAX_ENTRIES_PER_DISPATCH", cfg.later.max_entries_per_dispatch))
    cfg.later.auto_gitignore = _parse_bool(raw.get("LATER_AUTO_GITIGNORE", str(cfg.later.auto_gitignore)))
    cfg.dispatch.enabled = _parse_bool(raw.get("DISPATCH_ENABLED", str(cfg.dispatch.enabled)))
    cfg.dispatch.model = raw.get("DISPATCH_MODEL", cfg.dispatch.model)
    cfg.dispatch.allow_file_writes = _parse_bool(raw.get("DISPATCH_ALLOW_FILE_WRITES", str(cfg.dispatch.allow_file_writes)))
    cfg.dispatch.output_path = raw.get("DISPATCH_OUTPUT_PATH", cfg.dispatch.output_path)
    cfg.window.dispatch_mode = raw.get("WINDOW_DISPATCH_MODE", cfg.window.dispatch_mode)
    cfg.window.trigger_at_minutes_remaining = int(raw.get("WINDOW_TRIGGER_AT_MINUTES_REMAINING", cfg.window.trigger_at_minutes_remaining))
    cfg.window.idle_grace_period_minutes = int(raw.get("WINDOW_IDLE_GRACE_PERIOD_MINUTES", cfg.window.idle_grace_period_minutes))
    cfg.window.fallback_dispatch_hours = _parse_list(raw.get("WINDOW_FALLBACK_DISPATCH_HOURS", ""))
    cfg.window.jsonl_paths = _parse_list(raw.get("WINDOW_JSONL_PATHS", ""))
    cfg.limits.weekly_budget_tokens = int(raw.get("LIMITS_WEEKLY_BUDGET_TOKENS", cfg.limits.weekly_budget_tokens))
    cfg.limits.backoff_at_pct = int(raw.get("LIMITS_BACKOFF_AT_PCT", cfg.limits.backoff_at_pct))
    cfg.auto_resume.enabled = _parse_bool(raw.get("AUTO_RESUME_ENABLED", str(cfg.auto_resume.enabled)))
    cfg.auto_resume.min_remaining_minutes = int(raw.get("AUTO_RESUME_MIN_REMAINING_MINUTES", cfg.auto_resume.min_remaining_minutes))
    _validate_values(cfg)
    return cfg


def _coerce_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _parse_iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
    return State(last_hook_ts=_coerce_str(payload.get("last_hook_ts")), repos=repos)


def save_state(state: State) -> None:
    app_dir().mkdir(parents=True, exist_ok=True)
    payload = {
        "last_hook_ts": state.last_hook_ts,
        "repos": {repo: asdict(repo_state) for repo, repo_state in state.repos.items()},
    }
    state_path().write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _safe_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
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


def ensure_later_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    path.write_text(
        "# LATER\n\n"
        "Use this format:\n"
        "- [ ] (P1) concise actionable task\n"
        "- [ ] (P0) urgent production/security task\n"
        "- [x] completed task\n\n"
        "## Queue\n",
        encoding="utf-8",
    )


def stable_task_id(line_index: int, text: str) -> str:
    digest = hashlib.sha1(f"{line_index}:{text}".encode("utf-8")).hexdigest()[:10]
    return f"t_{digest}"


def parse_tasks(content: str) -> list[Section]:
    sections: list[Section] = []
    current_name = ""
    current_tasks: list[Task] = []
    for idx, line in enumerate(content.splitlines()):
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
    return sorted(section.tasks, key=lambda t: (rank.get(t.priority, 1), t.line_index))[:limit]


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
    data = "\n".join(out)
    if content.endswith("\n"):
        data += "\n"
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
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _row_timestamp(row: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "ts", "created_at"):
        dt = _parse_iso(row.get(key))
        if dt is not None:
            return dt
    return None


def _as_int(value: Any) -> int:
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


def _jsonl_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [root] if root.is_file() else list(root.rglob("*.jsonl"))


def compute_window_state(roots: list[Path], now_utc: datetime, session_id: str | None = None) -> WindowState | None:
    cutoff = now_utc - timedelta(hours=5)
    future_cutoff = now_utc + timedelta(minutes=5)
    earliest: datetime | None = None
    input_tokens = 0
    output_tokens = 0
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
                earliest = ts if earliest is None or ts < earliest else earliest
                usage = row.get("message_usage") or row.get("usage") or {}
                if isinstance(usage, dict):
                    input_tokens += _as_int(usage.get("input_tokens")) + _as_int(usage.get("cache_creation_input_tokens"))
                    output_tokens += _as_int(usage.get("output_tokens"))
    if earliest is None:
        return None
    elapsed = max(0, int((now_utc - earliest).total_seconds() // 60))
    return WindowState(
        elapsed_minutes=elapsed,
        remaining_minutes=max(0, DEFAULT_WINDOW_MINUTES - elapsed),
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
    )


def compute_budget_state(roots: list[Path], now_utc: datetime, weekly_budget: int) -> BudgetState:
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
                usage = row.get("message_usage") or row.get("usage") or {}
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
    return shutil.which("claude") or "claude"


def _spawn_dispatch(cfg: Config, repo_path: Path, prompt: str, result_path: Path, cwd: Path | None = None) -> int | None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_find_claude_bin(), "-p", prompt, "--output-format", "json", "--model", cfg.dispatch.model]
    if cfg.dispatch.allow_file_writes:
        cmd.append("--dangerously-skip-permissions")
    try:
        fh = result_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd if cwd is not None else repo_path),
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    except OSError:
        return None
    finally:
        try:
            fh.close()  # type: ignore[name-defined]
        except Exception:
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
        if not isinstance(item, str) or "-" not in item:
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
    if not cfg.auto_resume.enabled:
        return False
    has_pending = any(bool(state.repos.get(str(repo), RepoState()).resume_entries) for repo in watch_paths)
    if not has_pending:
        return False
    if cfg.window.dispatch_mode == "window_aware":
        return bool(window_state and window_state.remaining_minutes >= cfg.auto_resume.min_remaining_minutes)
    return True


def _result_path(template: str, repo: Path, now_utc: datetime, section_slug: str = "") -> Path:
    name = f"{repo.name}-{section_slug}" if section_slug else repo.name
    return Path(template.format(repo=name, date=now_utc.strftime("%Y%m%d-%H%M%S"))).expanduser().resolve()


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
    return "\n".join(lines)


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
        )
        if result.returncode != 0:
            return None
    except OSError:
        return None
    return worktree_path, branch


def _merge_worktree(repo: Path, branch: str, worktree_path: Path, section_name: str) -> tuple[bool, list[str]]:
    """Merge a section branch back into HEAD. Returns (success, conflicting_files)."""
    # First check if the branch has any commits ahead of the current HEAD
    try:
        diff = subprocess.run(
            ["git", "rev-list", "--count", f"HEAD..{branch}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        if diff.returncode == 0 and diff.stdout.strip() == "0":
            # No commits — agent made no changes, nothing to merge
            _cleanup_worktree(repo, branch, worktree_path)
            return True, []
    except OSError:
        pass

    try:
        result = subprocess.run(
            ["git", "merge", "--no-ff", branch, "-m", f"cc-later: {section_name or 'resume'} tasks"],
            cwd=str(repo),
            capture_output=True,
            text=True,
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
        )
        conflicting = [f.strip() for f in conflict_result.stdout.splitlines() if f.strip()]
        # Abort the failed merge so the repo is not left in a broken state
        subprocess.run(["git", "merge", "--abort"], cwd=str(repo), capture_output=True)
        return False, conflicting
    except OSError:
        return False, []


def _cleanup_worktree(repo: Path, branch: str, worktree_path: Path) -> None:
    """Remove a worktree and delete its branch."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(repo),
            capture_output=True,
        )
    except OSError:
        pass
    try:
        subprocess.run(
            ["git", "branch", "-d", branch],
            cwd=str(repo),
            capture_output=True,
        )
    except OSError:
        pass


def _ensure_gitignore(repo: Path, later_path: str) -> None:
    gitignore = repo / ".gitignore"
    existing = _safe_read(gitignore) or ""
    lines = existing.splitlines()
    if later_path not in lines:
        lines.append(later_path)
        try:
            gitignore.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except OSError:
            pass


def _reconcile(cfg: Config, state: State, now_utc: datetime) -> int:
    completed = 0
    for repo_key, repo_state in state.repos.items():
        if not repo_state.in_flight:
            continue
        remaining: list[dict[str, Any]] = []
        for agent in repo_state.agents:
            pid = _coerce_int(agent.get("pid"))
            if _is_process_alive(pid):
                remaining.append(agent)
                continue

            entries = [Task.from_dict(e) for e in agent.get("entries", []) if isinstance(e, dict)]
            result_path_str = _coerce_str(agent.get("result_path"))
            branch = _coerce_str(agent.get("branch"))
            worktree_path_str = _coerce_str(agent.get("worktree_path"))
            section_name = _coerce_str(agent.get("section_name")) or ""

            raw = _safe_read(Path(result_path_str).expanduser()) if result_path_str else None
            if raw is None:
                # No output — agent may have crashed; try to merge/clean up worktree anyway
                if branch and worktree_path_str:
                    ok, _ = _merge_worktree(Path(repo_key), branch, Path(worktree_path_str), section_name)
                    if not ok:
                        log_event("merge_conflict", repo=repo_key, branch=branch, section=section_name, files=[])
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
                        later_path.write_text(updated, encoding="utf-8")
            completed += 1

        repo_state.agents = remaining
        repo_state.in_flight = bool(remaining)
    return completed


def _read_hook_payload(stdin_text: str | None = None) -> dict[str, Any]:
    data = stdin_text if stdin_text is not None else (sys.stdin.read() if not sys.stdin.isatty() else "")
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
    now_utc = datetime.now(timezone.utc)
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
    window_state = compute_window_state(roots, now_utc, session_id=str(session_id) if session_id else None)
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
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone()
    roots = resolve_jsonl_roots(cfg)
    window_state = compute_window_state(roots, now_utc)
    budget = compute_budget_state(roots, now_utc, cfg.limits.weekly_budget_tokens)
    watch_paths = resolve_watch_paths(cfg, cwd_hint)

    lines = ["## cc-later Status", "", "### Window", f"  Mode: {cfg.window.dispatch_mode}"]
    if window_state is None:
        lines.extend(["  State: unknown", "  Next window: starts on next Claude request"])
    else:
        end_local = (now_utc + timedelta(minutes=window_state.remaining_minutes)).astimezone()
        lines.append(f"  Elapsed/Remaining: {window_state.elapsed_minutes}m / {window_state.remaining_minutes}m")
        lines.append(f"  Tokens: {window_state.total_input_tokens:,} in / {window_state.total_output_tokens:,} out")
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
        ensure_later_file(later_file)
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
    for row in (run_log_path().read_text(encoding="utf-8").splitlines() if run_log_path().exists() else []):
        if not row.strip():
            continue
        try:
            recent.append(json.loads(row))
        except json.JSONDecodeError:
            continue
    for item in recent[-8:][::-1]:
        ts = _parse_iso(item.get("ts"))
        when = ts.astimezone().strftime("%m-%d %H:%M") if ts else "??"
        lines.append(f"  {when} {item.get('event','?'):16} {item.get('reason') or ''}")
    return "\n".join(lines) + "\n"


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
    lowered = existing.lower()
    added = 0
    for match in CAPTURE_RE.finditer(prompt):
        urgent = bool(match.group(1))
        text = match.group(2).strip().rstrip(".")
        if len(text) < 3 or text.lower() in lowered:
            continue
        lines.append(f"- [ ] ({'P0' if urgent else 'P1'}) {text}")
        lowered += "\n" + text.lower()
        added += 1

    if added:
        later_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        log_event("capture", repo=str(repo), added=added)
        print(f"[cc-later] added {added} task(s) to {later_file}")
    return 0


__all__ = [
    "APP_DIR_ENV",
    "AutoResumeConfig",
    "BudgetState",
    "Config",
    "DispatchConfig",
    "LaterConfig",
    "LimitsConfig",
    "PathsConfig",
    "RepoState",
    "Section",
    "State",
    "Task",
    "WindowConfig",
    "WindowState",
    "build_status",
    "capture_from_payload",
    "compute_budget_state",
    "compute_window_state",
    "detect_limit_exhaustion",
    "ensure_later_file",
    "load_config",
    "load_state",
    "mark_done_in_content",
    "parse_result_summary",
    "parse_tasks",
    "resolve_watch_paths",
    "run_handler",
    "run_status",
    "save_state",
    "select_tasks",
    "stable_task_id",
    "_create_worktree",
    "_merge_worktree",
    "_cleanup_worktree",
]
