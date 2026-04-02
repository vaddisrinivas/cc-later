"""Shared path constants for cc-later."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(os.environ.get("CC_LATER_APP_DIR", "~/.cc-later")).expanduser()
CONFIG_PATH = APP_DIR / "config.toml"
RUN_LOG_PATH = APP_DIR / "run_log.jsonl"
STATE_PATH = APP_DIR / "state.json"
LOCK_PATH = APP_DIR / "handler.lock"
DB_PATH = APP_DIR / "analytics.db"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "scripts" / "default_config.toml"
