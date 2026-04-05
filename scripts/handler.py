#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""cc-later Stop hook handler — thin shim into cc_later.dispatcher."""

from __future__ import annotations

import sys
from pathlib import Path

# Add the plugin root to sys.path so cc_later package is importable
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

# Re-export everything from the package for backward compatibility with tests
# that import from handler.py via _loader.py
from cc_later.models import (  # noqa: F401
    AppConfig,
    AppState,
    BudgetConfig,
    BudgetState,
    ConfigError,
    CONTEXT_WINDOW_TOKENS,
    DEFAULT_WINDOW_MINUTES,
    DispatchConfig,
    LaterConfig,
    LaterEntry,
    NotificationConfig,
    PathsConfig,
    RepoState,
    RetryConfig,
    AutoResumeConfig,
    SkillConfig,
    VerifyConfig,
    WindowConfig,
    WindowState,
)
from cc_later.config import validate_config_dict, load_or_create_config  # noqa: F401
from cc_later.lock import NonBlockingFileLock  # noqa: F401
from cc_later.parser import (  # noqa: F401
    RESULT_LINE_PATTERN,
    SECTION_PATTERN,
    TASK_LINE_PATTERN,
    apply_completion,
    apply_retry_metadata,
    estimate_complexity,
    extract_pending_for_rotation,
    parse_later_entries,
    parse_result_summary,
    rotate_later_if_needed,
    route_model,
    select_entries,
    stable_task_id as _stable_task_id,
)
from cc_later.window import (  # noqa: F401
    compute_budget_state,
    compute_window_state,
    expand_watch_paths as _expand_watch_paths,
    is_in_peak_window as _is_in_peak_window,
    is_within_time_ranges,
    parse_iso8601 as _parse_iso8601,
    resolve_jsonl_roots as _resolve_jsonl_roots,
    resolve_trigger_threshold,
    should_dispatch_by_mode,
)
from cc_later.dispatcher import (  # noqa: F401
    _is_auto_resume_gate_open,
    _reconcile_in_flight,
    _is_process_alive,
    load_state,
    log_event,
    main,
    save_state,
)
from cc_later.notify import notify as _maybe_notify  # noqa: F401
from cc_later.paths import APP_DIR, LOCK_PATH, RUN_LOG_PATH, STATE_PATH  # noqa: F401
from cc_later.prompt import render_prompt as _render_prompt, resolve_output_path as _resolve_output_path  # noqa: F401

# Legacy alias used by old tests
_extract_pending_for_rotation = extract_pending_for_rotation


if __name__ == "__main__":
    raise SystemExit(main())
