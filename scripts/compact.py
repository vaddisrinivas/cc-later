#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2.0", "pydantic-settings>=2.0", "filelock>=3.0", "pendulum>=3.0"]
# ///
"""cc-later compact hook — injects LATER.md context after compaction.

Registered as a SessionStart hook with matcher "compact". Claude Code
fires it when a session resumes after /compact or auto-compaction,
and injects this script's stdout into Claude's context.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from cc_later.core import run_compact_inject


if __name__ == "__main__":
    payload: dict = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        pass
    raise SystemExit(run_compact_inject(cwd_hint=payload.get("cwd")))
