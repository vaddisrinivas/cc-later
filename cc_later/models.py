"""Data models for cc-later."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------

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
            "node_modules", ".git", "__pycache__", "dist", "build", ".venv", "vendor",
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
    model_routing: str = "fixed"  # "fixed" | "auto"
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
    webhook_url: str = ""
    webhook_events: list[str] = field(default_factory=lambda: [
        "dispatch", "complete", "error",
    ])


@dataclass
class BudgetConfig:
    plan: str = "pro"
    weekly_token_budget: int = 10_000_000
    backoff_at_pct: int = 80
    probe_model: str = "claude-haiku-4-5-20251001"


@dataclass
class RetryConfig:
    enabled: bool = True
    max_attempts: int = 3
    backoff_minutes: list[int] = field(default_factory=lambda: [30, 120, 480])
    escalate_to_priority: bool = True


@dataclass
class VerifyConfig:
    enabled: bool = True
    require_diff: bool = False  # only when allow_file_writes is on
    min_confidence: str = "low"  # "low" | "medium" | "high"


@dataclass
class AppConfig:
    window: WindowConfig = field(default_factory=WindowConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    later_md: LaterConfig = field(default_factory=LaterConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    skill: SkillConfig = field(default_factory=SkillConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)


# ---------------------------------------------------------------------------
# Runtime models
# ---------------------------------------------------------------------------

CONTEXT_WINDOW_TOKENS = 200_000
DEFAULT_WINDOW_MINUTES = 300


@dataclass
class WindowState:
    elapsed_minutes: int
    remaining_minutes: int
    total_input_tokens: int
    total_output_tokens: int
    context_pct_used: float = 0.0
    session_id: str | None = None
    source_path: str | None = None


@dataclass
class BudgetState:
    tokens_used_this_week: int
    weekly_budget: int
    pct_used: float
    tokens_remaining: int


@dataclass
class LaterEntry:
    id: str
    text: str
    is_priority: bool
    line_index: int
    raw_line: str
    section: str | None = None
    attempts: int = 0
    last_attempt: str | None = None
    depends_on: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LaterEntry":
        return cls(
            id=str(data.get("id", "")),
            text=str(data.get("text", "")),
            is_priority=bool(data.get("is_priority", False)),
            line_index=int(data.get("line_index", 0)),
            raw_line=str(data.get("raw_line", "")),
            section=data.get("section") or None,
            attempts=int(data.get("attempts", 0)),
            last_attempt=data.get("last_attempt"),
            depends_on=data.get("depends_on"),
        )


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


class ConfigError(Exception):
    """Raised when config is invalid."""
