#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2.0", "pydantic-settings>=2.0", "filelock>=3.0", "pendulum>=3.0"]
# ///
"""cc-later UserPromptSubmit capture hook."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from cc_later.core import capture_from_payload


_MAX_STDIN_BYTES = 1_048_576  # 1MB — hook payloads should never exceed this


def _read_payload() -> dict:
    raw = sys.stdin.read(_MAX_STDIN_BYTES).strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


if __name__ == "__main__":
    raise SystemExit(capture_from_payload(_read_payload()))
