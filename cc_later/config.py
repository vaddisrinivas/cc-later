"""Configuration loading, validation, and schema enforcement."""

from __future__ import annotations

import shutil
from pathlib import Path

from .compat import tomllib
from typing import Any

from .models import (
    AppConfig,
    ConfigError,
    WindowConfig,
    PathsConfig,
    LaterConfig,
    DispatchConfig,
    SkillConfig,
    NotificationConfig,
    BudgetConfig,
    RetryConfig,
    AutoResumeConfig,
    VerifyConfig,
)
from .paths import APP_DIR, CONFIG_PATH, DEFAULT_CONFIG_PATH


# Strict schema: every section and key must be listed here.
SCHEMA: dict[str, set[str]] = {
    "window": {
        "trigger_at_minutes_remaining",
        "idle_grace_period_minutes",
        "respect_peak_hours",
        "peak_windows",
        "dispatch_mode",
        "fallback_dispatch_hours",
        "jsonl_paths",
        "trigger_schedules",
        "trigger_schedules_enabled",
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
        "model_routing",
        "allow_file_writes",
        "max_files_written_per_task",
        "prompt_template",
        "output_path",
    },
    "skill": {"suggest_threshold", "auto_append", "end_of_session_note"},
    "notifications": {
        "desktop",
        "on_dispatch",
        "on_complete",
        "on_error",
        "webhook_url",
        "webhook_events",
    },
    "budget": {"plan", "weekly_token_budget", "backoff_at_pct", "probe_model"},
    "retry": {"enabled", "max_attempts", "backoff_minutes", "escalate_to_priority"},
    "auto_resume": {"enabled", "min_remaining_minutes"},
    "verify": {"enabled", "require_diff", "min_confidence"},
}


def validate_config_dict(raw: dict[str, Any]) -> AppConfig:
    """Validate config with strict unknown-key rejection and defaults."""
    unknown_sections = set(raw) - set(SCHEMA)
    if unknown_sections:
        raise ConfigError(f"Unknown config sections: {', '.join(sorted(unknown_sections))}")

    for section, allowed_keys in SCHEMA.items():
        candidate = raw.get(section, {})
        if candidate is None:
            candidate = {}
        if not isinstance(candidate, dict):
            raise ConfigError(f"[{section}] must be a table")
        unknown_keys = set(candidate) - allowed_keys
        if unknown_keys:
            raise ConfigError(f"Unknown keys in [{section}]: {', '.join(sorted(unknown_keys))}")

    cfg = AppConfig()
    _merge_dataclass(cfg.window, raw.get("window", {}))
    _merge_dataclass(cfg.paths, raw.get("paths", {}))
    _merge_dataclass(cfg.later_md, raw.get("later_md", {}))
    _merge_dataclass(cfg.dispatch, raw.get("dispatch", {}))
    _merge_dataclass(cfg.skill, raw.get("skill", {}))
    _merge_dataclass(cfg.notifications, raw.get("notifications", {}))
    _merge_dataclass(cfg.budget, raw.get("budget", {}))
    _merge_dataclass(cfg.retry, raw.get("retry", {}))
    _merge_dataclass(cfg.auto_resume, raw.get("auto_resume", {}))
    _merge_dataclass(cfg.verify, raw.get("verify", {}))

    # Value constraints
    if cfg.window.dispatch_mode not in {"window_aware", "time_based", "always"}:
        raise ConfigError("window.dispatch_mode must be one of: window_aware, time_based, always")
    if cfg.later_md.mark_completed not in {"check", "delete"}:
        raise ConfigError("later_md.mark_completed must be one of: check, delete")
    if cfg.dispatch.model not in {"sonnet", "opus", "haiku"}:
        raise ConfigError("dispatch.model must be one of: sonnet, opus, haiku")
    if cfg.dispatch.model_routing not in {"fixed", "auto"}:
        raise ConfigError("dispatch.model_routing must be one of: fixed, auto")
    if cfg.verify.min_confidence not in {"low", "medium", "high"}:
        raise ConfigError("verify.min_confidence must be one of: low, medium, high")
    if not isinstance(cfg.auto_resume.min_remaining_minutes, int) or cfg.auto_resume.min_remaining_minutes < 0:
        raise ConfigError("auto_resume.min_remaining_minutes must be an integer >= 0")
    for field_name in ("watch", "exclude_patterns", "fallback_dispatch_hours", "jsonl_paths"):
        obj = cfg.paths if hasattr(cfg.paths, field_name) else cfg.window
        val = getattr(obj, field_name)
        if not isinstance(val, list):
            raise ConfigError(f"{field_name} must be a list")

    return cfg


def load_or_create_config() -> tuple[AppConfig | None, str | None]:
    """Load config from disk, creating from defaults on first run."""
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


def _merge_dataclass(target: Any, values: dict[str, Any]) -> None:
    if not isinstance(values, dict):
        raise ConfigError("Config section must be a table")
    for key, value in values.items():
        setattr(target, key, value)
