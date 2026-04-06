#!/usr/bin/env python3
"""
cc-later v4 migration: convert config.toml → config.env

Run once after upgrading to v4:
  python3 scripts/migrate_v4.py

Safe to re-run — skips if config.env already exists.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_DIR_ENV = "CC_LATER_APP_DIR"


def app_dir() -> Path:
    return Path(os.environ.get(APP_DIR_ENV, "~/.cc-later")).expanduser()


def _parse_toml_simple(text: str) -> dict[str, dict[str, str]]:
    """Minimal TOML parser for the flat cc-later config subset."""
    result: dict[str, dict[str, str]] = {}
    section = "_root"
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            result.setdefault(section, {})[key.strip()] = val.strip().strip('"').strip("'")
    return result


def toml_to_env(toml_text: str) -> str:
    data = _parse_toml_simple(toml_text)

    def get(section: str, key: str, default: str) -> str:
        return data.get(section, {}).get(key, default)

    def fmt_list(val: str) -> str:
        # TOML array like ["a", "b"] → a,b
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            items = [i.strip().strip('"').strip("'") for i in val[1:-1].split(",")]
            return ",".join(i for i in items if i)
        return val

    watch = fmt_list(get("paths", "watch", ""))
    later_path = get("later", "path", ".claude/LATER.md")
    max_entries = get("later", "max_entries_per_dispatch", "3")
    auto_gitignore = get("later", "auto_gitignore", "true")
    dispatch_enabled = get("dispatch", "enabled", "true")
    dispatch_model = get("dispatch", "model", "sonnet")
    allow_writes = get("dispatch", "allow_file_writes", "false")
    output_path = get("dispatch", "output_path", "~/.cc-later/results/{repo}-{date}.json")
    dispatch_mode = get("window", "dispatch_mode", "window_aware")
    trigger_min = get("window", "trigger_at_minutes_remaining", "30")
    idle_grace = get("window", "idle_grace_period_minutes", "10")
    fallback_hours = fmt_list(get("window", "fallback_dispatch_hours", ""))
    jsonl_paths = fmt_list(get("window", "jsonl_paths", ""))
    weekly_budget = get("limits", "weekly_budget_tokens", "10000000").replace("_", "")
    backoff_pct = get("limits", "backoff_at_pct", "80")
    auto_resume_enabled = get("auto_resume", "enabled", "true")
    min_remaining = get("auto_resume", "min_remaining_minutes", "240")

    return f"""\
# cc-later configuration (migrated from config.toml by migrate_v4.py)
# Empty PATHS_WATCH means: auto-watch the current repo where the hook runs.
PATHS_WATCH={watch}

LATER_PATH={later_path}
LATER_MAX_ENTRIES_PER_DISPATCH={max_entries}
LATER_AUTO_GITIGNORE={auto_gitignore}

# Dispatch settings
DISPATCH_ENABLED={dispatch_enabled}
DISPATCH_MODEL={dispatch_model}
DISPATCH_ALLOW_FILE_WRITES={allow_writes}
DISPATCH_OUTPUT_PATH={output_path}

# window_aware: use Claude JSONL usage window
# time_based: only dispatch inside WINDOW_FALLBACK_DISPATCH_HOURS
# always: dispatch whenever idle
WINDOW_DISPATCH_MODE={dispatch_mode}
WINDOW_TRIGGER_AT_MINUTES_REMAINING={trigger_min}
WINDOW_IDLE_GRACE_PERIOD_MINUTES={idle_grace}
# Comma-separated HH:MM-HH:MM ranges, e.g. 09:00-17:00,22:00-24:00
WINDOW_FALLBACK_DISPATCH_HOURS={fallback_hours}
# Comma-separated paths to JSONL files (leave empty to auto-detect)
WINDOW_JSONL_PATHS={jsonl_paths}

LIMITS_WEEKLY_BUDGET_TOKENS={weekly_budget}
LIMITS_BACKOFF_AT_PCT={backoff_pct}

# Resume tasks that failed due to rate/usage limits in the next fresh window.
AUTO_RESUME_ENABLED={auto_resume_enabled}
AUTO_RESUME_MIN_REMAINING_MINUTES={min_remaining}
"""


def main() -> int:
    adir = app_dir()
    env_path = adir / "config.env"
    toml_path = adir / "config.toml"

    if env_path.exists():
        print(f"[migrate_v4] config.env already exists at {env_path} — nothing to do.")
        return 0

    if not toml_path.exists():
        print(f"[migrate_v4] No config.toml found at {toml_path} — will be created fresh on next run.")
        return 0

    toml_text = toml_path.read_text(encoding="utf-8")
    env_text = toml_to_env(toml_text)
    env_path.write_text(env_text, encoding="utf-8")

    # Archive the old toml
    archive = toml_path.with_name("config.toml.v3-backup")
    shutil.move(str(toml_path), str(archive))

    print(f"[migrate_v4] Migrated {toml_path} → {env_path}")
    print(f"[migrate_v4] Old config archived to {archive}")
    print("[migrate_v4] Done. Review config.env and adjust as needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
